# SPDX-License-Identifier: GPL-3.0-or-later
#
# Flash applet
#
# Copyright (c) 2025, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Flash applet: list the flash partitions (volumes) of a plumCore device with their size, erase block
size and a map of the used blocks with the usage.

It talks to an interface speaking the ``flash`` protocol (plumCore services/nbus-flash, or the virtual
flash service of the pynbus2 ``swd`` transport). Every request is a round trip to the device, so the
partitions are loaded off the GUI thread.
"""

import contextlib
import io
import logging
import os
import shutil
import tempfile
import threading

import cbor2
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from pynbus2.services import flash as flash_service

from pydaui.files import display_name, read_file, write_file
from pydaui.ui.helpers import ANDROID, Admonition, AppletWindow, DauiButton, StyledTreeWidget
from pydaui.ui.icons import Icons
from pydaui.ui.progress_window import ByteFormat, ProgressWindow

#: The protocol name of the interfaces this applet talks to.
PROTOCOL = flash_service.PROTOCOL


class Aborted(Exception):
	"""A flash operation was aborted."""


def format_size(size):
	"""``size`` in bytes as a short human readable text in the largest unit it has at least one of, e.g.
	``64 KiB``, with a decimal if it is not a whole number of them, e.g. ``100.0 KiB``."""
	units = ('B', 'KiB', 'MiB', 'GiB')
	exp = 0
	while exp + 1 < len(units) and size >= 1024 ** (exp + 1):
		exp += 1
	value = size / 1024 ** exp
	return f'{size // 1024 ** exp} {units[exp]}' if size % 1024 ** exp == 0 else f'{value:.1f} {units[exp]}'


class FlashClient:
	"""flash protocol client over a :class:`~pydaui.connection.PyDauiConnection` connected to a flash
	interface. Requests are serialized, so the client may be used from several threads."""

	#: Number of attempts of a request before giving up.
	RETRIES = 10

	#: How long the usage estimate may take, in seconds. It reads every erase block of the partition.
	USAGE_TIMEOUT = 10.0

	#: How long erasing a single erase block may take, in seconds.
	ERASE_TIMEOUT = 5.0

	#: Number of attempts to erase a block. Erasing a block again is harmless.
	ERASE_RETRIES = 3

	#: Size of the blocks read and written at once. The device accepts up to NBUS_FLASH_BLOCK_LEN (256 B),
	#: but the proto-dgtext serial link caps a whole datagram at 256 B including the nbus2 header and the
	#: CBOR framing, so 128 B works on every transport (as in plumCore tools/proto-nbus-flash.py).
	TRANSFER_BLOCK = 128

	#: Room for the request framing in a message, the largest block is the interface MTU less this (as in
	#: the firmware, NBUS_FLASH_RX_BUF_LEN - NBUS_FLASH_BLOCK_LEN).
	FRAMING = 64

	def __init__(self, connection, mtu=None):
		"""``mtu`` is the MTU the flash interface advertises. It allows writing pages larger than
		:attr:`TRANSFER_BLOCK` at once, e.g. the whole flash algorithm pages over a debug probe."""
		self._connection = connection
		# Shared by the clients on the same connection.
		self._lock = connection.lock
		self._max_block = mtu - self.FRAMING if mtu else flash_service.BLOCK_LEN

	def call(self, req: dict, timeout=None, retries=None):
		"""Send a request and return the decoded response map. Raises ``ValueError`` if there is no valid
		response or it reports an error.

		Without ``timeout`` the retransmit interval suits the transport, e.g. a BLE link is much slower
		than UDP.
		"""
		with self._lock:
			sock = self._connection.socket
			if sock is None:
				raise ValueError('not connected')
			reply = sock.request(cbor2.dumps(req), timeout=timeout or sock.request_timeout,
			                     retries=retries or self.RETRIES)
		if reply is None:
			raise ValueError('no response')
		try:
			r = cbor2.loads(reply)
		except Exception:
			raise ValueError('invalid response')
		if not isinstance(r, dict):
			raise ValueError('invalid response')
		if r.get('err'):
			raise ValueError(r['err'])
		return r

	def list(self):
		"""The names of the flash partitions on the device."""
		return [d.get('n', '') for d in self.call({'c': 'list'}).get('d', [])]

	def info(self, name):
		"""The geometry of the partition ``name`` as ``(size, erase block size, page size)``. The erase
		block size is the smallest one, which the usage is estimated in, the page size the smallest
		programmable unit; each ``None`` if the partition cannot be erased or written."""
		sizes = [(s.get('s', 0), s.get('ops', 0)) for s in self.call({'c': 'info', 'n': name}).get('sizes', [])]
		if not sizes:
			raise ValueError('no sizes reported')
		erase_sizes = [size for size, ops in sizes[1:] if ops & flash_service.OPS_ERASE]
		page_sizes = [size for size, ops in sizes[1:] if ops & flash_service.OPS_WRITE]
		return sizes[0][0], (erase_sizes[-1] if erase_sizes else None), (page_sizes[-1] if page_sizes else None)

	def usage(self, name, size, erase_size):
		"""The erase blocks of the partition ``name`` in use as ``(share, ranges)``: the share from 0 to 1
		and the used ranges as ``(start, end)`` tuples relative to the partition size, in address order.
		``size`` and ``erase_size`` are the partition geometry from :meth:`info`.

		The flash service supports a single session, so the partition is opened only for the estimate.
		"""
		with self._session(name) as sid:
			# The device replies once the estimate is done, retransmits would only queue more of them.
			r = self.call({'c': 'usage', 'sid': sid}, timeout=self.USAGE_TIMEOUT, retries=1)
		ba = r.get('ba', 1)
		bitmap = r.get('b', b'')
		blocks = size // erase_size
		if not blocks:
			return 0.0, []
		# Each bit covers ba blocks, the last one possibly fewer. Join the adjacent used ones to ranges.
		used = 0
		ranges = []
		for bit in range(-(-blocks // ba)):
			if bit // 8 >= len(bitmap) or not bitmap[bit // 8] & (1 << (bit % 8)):
				continue
			start, end = bit * ba, min(blocks, (bit + 1) * ba)
			used += end - start
			if ranges and ranges[-1][1] == start:
				ranges[-1][1] = end
			else:
				ranges.append([start, end])
		return used / blocks, [(start / blocks, end / blocks) for start, end in ranges]

	def erase(self, name, size, erase_size, progress=None, abort=None):
		"""Erase the whole partition ``name`` one erase block after another. ``size`` and ``erase_size``
		are the partition geometry from :meth:`info`.

		``progress`` is called with the number of bytes erased so far after each block. Setting the
		``abort`` event stops erasing before the next block with :class:`Aborted`. Raises ``ValueError``
		with the reason if erasing fails.
		"""
		with self._session(name) as sid:
			self._erase(sid, size, erase_size, progress, abort)

	def _erase(self, sid, size, erase_size, progress, abort):
		for addr in range(0, size, erase_size):
			if abort is not None and abort.is_set():
				raise Aborted()
			length = min(erase_size, size - addr)
			try:
				self.call({'c': 'erase', 'sid': sid, 'addr': addr, 'len': length},
				          timeout=self.ERASE_TIMEOUT, retries=self.ERASE_RETRIES)
			except ValueError as e:
				raise ValueError(f'erasing at 0x{addr:08x} failed: {e}')
			if progress is not None:
				progress(addr + length)

	def write(self, name, size, erase_size, page_size, data, progress=None, abort=None, stage=None):
		"""Write ``data`` to the start of the partition ``name``, erasing the whole partition first and
		reading the data back to verify it at the end. ``size``, ``erase_size`` and ``page_size`` are the
		partition geometry from :meth:`info`. The data is padded with 0xff to whole pages.

		``stage`` is called as ``stage(what, total)`` when erasing (``'erase'``, the partition size),
		writing and verifying (``'write'`` and ``'verify'``, the padded data size) starts, ``progress`` with the bytes done so far in
		the stage. ``abort`` is like in :meth:`erase`. Raises ``ValueError`` with the reason if it fails.
		"""
		# Whole pages in each transfer block, a single page if it is larger.
		block = page_size * max(1, self.TRANSFER_BLOCK // page_size)
		if block > self._max_block:
			raise ValueError(f'page size {page_size} B is larger than the {self._max_block} B blocks the interface takes')
		data = bytes(data) + b'\xff' * (-len(data) % page_size)
		if len(data) > size:
			raise ValueError(f'{len(data)} B do not fit the partition of {size} B')

		with self._session(name) as sid:
			if stage is not None:
				stage('erase', size)
			self._erase(sid, size, erase_size, progress, abort)
			if stage is not None:
				stage('write', len(data))
			for addr in range(0, len(data), block):
				if abort is not None and abort.is_set():
					raise Aborted()
				chunk = data[addr:addr + block]
				try:
					self.call({'c': 'write', 'sid': sid, 'addr': addr, 'len': len(chunk), 'd': chunk})
				except ValueError as e:
					# If the reply to a write was lost, its retransmit fails as the block is already
					# programmed. The block is fine if it reads back as written.
					if self._readBlock(sid, addr, len(chunk), check=False) != chunk:
						raise ValueError(f'writing at 0x{addr:08x} failed: {e}')
				if progress is not None:
					progress(addr + len(chunk))

			if stage is not None:
				stage('verify', len(data))
			for addr in range(0, len(data), self.TRANSFER_BLOCK):
				if abort is not None and abort.is_set():
					raise Aborted()
				expected = data[addr:addr + self.TRANSFER_BLOCK]
				got = self._readBlock(sid, addr, len(expected))
				if got != expected:
					first = next(i for i in range(len(expected)) if got[i] != expected[i])
					hint = ', the block reads as erased' if got == b'\xff' * len(got) else ''
					raise ValueError(f'verification failed at 0x{addr + first:08x}: read 0x{got[first]:02x}, '
					                 f'expected 0x{expected[first]:02x}{hint}')
				if progress is not None:
					progress(addr + len(expected))

	def read(self, name, size, file, progress=None, abort=None):
		"""Read the whole partition ``name`` of ``size`` bytes into the binary ``file`` object, one block of
		:attr:`TRANSFER_BLOCK` bytes after another. ``progress`` and ``abort`` are like in :meth:`erase`.
		Raises ``ValueError`` with the reason if reading fails."""
		with self._session(name) as sid:
			for addr in range(0, size, self.TRANSFER_BLOCK):
				if abort is not None and abort.is_set():
					raise Aborted()
				length = min(self.TRANSFER_BLOCK, size - addr)
				file.write(self._readBlock(sid, addr, length))
				if progress is not None:
					progress(addr + length)

	def _readBlock(self, sid, addr, length, check=True):
		"""Read ``length`` bytes at ``addr`` in the session ``sid``. Raises ``ValueError`` if it fails, or
		returns ``None`` instead if not ``check``."""
		try:
			r = self.call({'c': 'read', 'sid': sid, 'addr': addr, 'len': length})
			data = r.get('d')
			# A late reply to a retransmitted request may answer an earlier one.
			if r.get('addr') != addr or not isinstance(data, bytes) or len(data) != length:
				raise ValueError('unexpected response')
		except ValueError as e:
			if not check:
				return None
			raise ValueError(f'reading at 0x{addr:08x} failed: {e}')
		return data

	@contextlib.contextmanager
	def _session(self, name):
		"""Open the partition ``name`` for the ``with`` block, giving the session id. The flash service
		supports a single session, opening one closes the previous one."""
		sid = self.call({'c': 'open', 'n': name})['sid']
		try:
			yield sid
		finally:
			try:
				self.call({'c': 'close', 'sid': sid})
			except ValueError:
				pass


def interface_mtu(connection, name):
	"""The MTU the interface ``name`` of the ``connection`` advertises, ``None`` if it advertises none."""
	return next((desc.get('mtu') for n, desc in connection.interfaces if n == name), None)


class _PartitionLoader(QObject):
	"""Loads the partition list and then the details of each partition off the GUI thread.

	``listed`` carries the partition names, then ``detail`` follows for each partition with a dict of
	``size``, ``erase``, ``page``, ``usage`` and the used ``ranges`` (each ``None`` if not known, see
	:meth:`FlashClient.info` and :meth:`FlashClient.usage`) and an error message (``None`` if there is
	none). ``done`` ends the loading, ``failed`` ends it if the partitions cannot be listed.
	"""

	listed = Signal(list)
	detail = Signal(str, dict, object)   # (partition name, details, error message)
	done = Signal()
	failed = Signal(str)

	def __init__(self, client):
		super().__init__()
		self._client = client

	def start(self):
		threading.Thread(target=self._run, name='flash-list', daemon=True).start()

	def _run(self):
		try:
			names = self._client.list()
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.listed.emit(names)
		for name in names:
			detail = {'size': None, 'erase': None, 'page': None, 'usage': None, 'ranges': None}
			error = None
			try:
				detail['size'], detail['erase'], detail['page'] = self._client.info(name)
				if detail['erase'] is not None:
					detail['usage'], detail['ranges'] = self._client.usage(name, detail['size'], detail['erase'])
			except Exception as e:
				error = str(e)
			self.detail.emit(name, detail, error)
		self.done.emit()


class _Operation(QObject):
	"""Runs a long flash operation off the GUI thread: ``fn(progress, abort, stage)``, where ``progress``
	is to be called with the bytes done so far, ``abort`` is a :class:`threading.Event` set to stop it,
	which ``fn`` answers by raising :class:`Aborted`, and ``stage`` may be called as ``stage(what, total)``
	when an operation of several stages starts the next one.

	``stage`` and ``progress`` carry the same, then one of ``done``, ``aborted`` or ``failed`` (with the
	reason) ends it.
	"""

	stage = Signal(str, int)
	progress = Signal(int)
	done = Signal()
	aborted = Signal()
	failed = Signal(str)

	def __init__(self, fn):
		super().__init__()
		self._fn = fn
		self._abort = threading.Event()

	def start(self):
		threading.Thread(target=self._run, name='flash-operation', daemon=True).start()

	def abort(self):
		"""Ask the operation to stop."""
		self._abort.set()

	def _run(self):
		try:
			self._fn(self.progress.emit, self._abort, self.stage.emit)
		except Aborted:
			self.aborted.emit()
			return
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.done.emit()


class FlashMap(QWidget):
	"""A map of a flash partition: a rectangle in the primary colour with a white background, the used
	ranges filled in the light primary colour and the usage in the middle. It fills the height of its row
	less :attr:`V_PADDING` above and below, and is inset by :attr:`H_PADDING` from the sides like the cell
	text."""

	V_PADDING = 2
	H_PADDING = 8

	def __init__(self, usage, ranges=()):
		super().__init__()
		# The share of the partition in use, and the used ranges as (start, end) relative to the
		# partition size, see FlashClient.usage().
		self._usage = usage
		self._ranges = list(ranges)
		# Leave the mouse to the list, which highlights the row under it.
		self.setAttribute(Qt.WA_TransparentForMouseEvents)

	def setUsage(self, usage, ranges):
		self._usage = usage
		self._ranges = list(ranges)
		self.update()

	def paintEvent(self, event):
		painter = QPainter(self)
		rect = QRectF(self.rect()).adjusted(self.H_PADDING, self.V_PADDING, -self.H_PADDING, -self.V_PADDING)
		if rect.width() <= 0 or rect.height() <= 0:
			return
		painter.fillRect(rect, QColor(Icons.WHITE))
		for start, end in self._ranges:
			# At least a pixel wide, so a single used block in a large partition shows.
			width = max(1.0, (end - start) * rect.width())
			painter.fillRect(QRectF(rect.left() + start * rect.width(), rect.top(), width, rect.height()),
			                 QColor(Icons.PRIMARY_LIGHT))
		# The outline inside the rectangle, a cosmetic 1 px pen straddles the edges by half a pixel.
		painter.setPen(QPen(QColor(Icons.PRIMARY), 1))
		painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

		font = painter.font()
		font.setBold(True)
		painter.setFont(font)
		painter.setPen(QColor(Icons.DARK_GRAY))
		painter.drawText(rect, Qt.AlignCenter, f'{self._usage * 100:.0f} %')


class _MapDelegate(QStyledItemDelegate):
	"""Delegate of the Usage column: places the map over the whole cell. The default places item widgets in
	the text area, which the item padding squeezes."""

	def updateEditorGeometry(self, editor, option, index):
		editor.setGeometry(option.rect)


class _PartitionMimeData(QMimeData):
	"""The data of a partition dragged out of the list: the URL of a file holding its content. The file is
	read from the device only when the drop target asks for it, by ``fetch()`` returning its path, or
	``None`` if reading failed or was aborted. The result is kept for the following requests."""

	FORMAT = 'text/uri-list'

	def __init__(self, fetch):
		super().__init__()
		self._fetch = fetch
		self._fetched = False
		self._path = None

	def formats(self):
		return [self.FORMAT]

	def hasFormat(self, mime_type):
		return mime_type == self.FORMAT

	def retrieveData(self, mime_type, type):
		if mime_type != self.FORMAT:
			return None
		if not self._fetched:
			self._fetched = True
			self._path = self._fetch()
		return [QUrl.fromLocalFile(self._path)] if self._path is not None else None


def can_write(detail):
	"""Whether the partition with the ``detail`` (see _PartitionLoader) can be written."""
	return detail is not None and None not in (detail['size'], detail['erase'], detail['page'])


def can_read(detail):
	"""Whether the partition with the ``detail`` (see _PartitionLoader) can be read."""
	return detail is not None and detail['size'] is not None


class PartitionList(StyledTreeWidget):
	"""The flash partitions of the device, a flat list."""

	#: The columns of the size, erase block size and usage, the last one shown as a map of the used blocks.
	SIZE_COLUMN = 1
	ERASE_COLUMN = 2
	USAGE_COLUMN = 3

	#: Relative widths of the Partition, Size, Erase block and Usage columns.
	COLUMN_RATIOS = (3, 2, 2, 5)

	#: Colour of the details which could not be loaded.
	FAILED_COLOR = '#c0392b'

	#: The details of a partition (see _PartitionLoader), set once they are loaded.
	DETAIL_ROLE = Qt.UserRole

	#: Emitted when loading the partitions finishes, whether it succeeded or not.
	loaded = Signal()

	#: Emitted when a file is dropped onto a partition which can be written, with the partition's item and
	#: the file path.
	fileDropped = Signal(QTreeWidgetItem, str)

	#: Emitted when a partition which can be read starts to be dragged, with the partition's item.
	dragRequested = Signal(QTreeWidgetItem)

	def __init__(self, client: FlashClient):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._client = client
		# Keep the loader alive until its signal has been delivered.
		self._loader = None
		self._items = {}

		self.setColumnCount(4)
		self.setHeaderLabels(['Partition', 'Size', 'Erase block', 'Usage'])
		self.setRootIsDecorated(False)
		self.setEditTriggers(QAbstractItemView.NoEditTriggers)
		self._map_delegate = _MapDelegate(self)
		self.setItemDelegateForColumn(self.USAGE_COLUMN, self._map_delegate)
		# A file dropped onto a partition is written to it, a partition dragged out is read, while
		# transfersEnabled.
		self.transfersEnabled = False
		self.setAcceptDrops(True)
		# Where the left button was pressed on a partition, to start dragging it.
		self._press_pos = None

	@property
	def loading(self):
		return self._loader is not None

	def refresh(self):
		"""Load the partitions from the device again."""
		if self._loader is not None:
			return
		self.clear()
		self._items = {}
		QTreeWidgetItem(self, ['loading...'])
		self._loader = _PartitionLoader(self._client)
		self._loader.listed.connect(self._onListed)
		self._loader.detail.connect(self._onDetail)
		self._loader.done.connect(self._onLoaded)
		self._loader.failed.connect(self._onFailed)
		self._loader.start()

	@Slot(list)
	def _onListed(self, names):
		self.clear()
		# The partitions by name, to fill in their details as they arrive.
		self._items = {}
		for name in names:
			item = QTreeWidgetItem(self, [name, '...', '...', '...'])
			self.styleName(item)
			self._items[name] = item

	@Slot(str, dict, object)
	def _onDetail(self, name, detail, error):
		item = self._items.get(name)
		if item is None:
			return
		item.setData(0, self.DETAIL_ROLE, detail)
		item.setText(self.SIZE_COLUMN, format_size(detail['size']) if detail['size'] is not None else '')
		item.setText(self.ERASE_COLUMN, format_size(detail['erase']) if detail['erase'] is not None else '')
		# The map shows the usage itself, the cell text would show around it.
		item.setText(self.USAGE_COLUMN, '')
		if detail['usage'] is not None:
			self.setItemWidget(item, self.USAGE_COLUMN, FlashMap(detail['usage'], detail['ranges']))
		if error is not None:
			self._logger.warning(f'loading the details of flash partition {name!r} failed: {error}')
			for column in (self.SIZE_COLUMN, self.ERASE_COLUMN, self.USAGE_COLUMN):
				if not item.text(column):
					item.setText(column, 'failed')
					item.setForeground(column, QColor(self.FAILED_COLOR))
					item.setToolTip(column, error)

		if item is self.currentItem():
			# The details of the selected partition decide what can be done with it.
			self.currentItemChanged.emit(item, item)

	def _dropTarget(self, event):
		"""The item of the partition a dragged file would be written to, or ``None``. A single local file
		can be dropped onto a partition which can be written."""
		# A partition dragged from the list itself would only be read to be written back.
		if not self.transfersEnabled or event.source() is self or not event.mimeData().hasUrls():
			return None
		urls = event.mimeData().urls()
		if len(urls) != 1 or not urls[0].isLocalFile() or not os.path.isfile(urls[0].toLocalFile()):
			return None
		item = self.itemAt(event.position().toPoint())
		if item is None or item.text(0) not in self._items or not can_write(item.data(0, self.DETAIL_ROLE)):
			return None
		return item

	def dragEnterEvent(self, event):
		# The target is decided as the file moves over the rows.
		if self.transfersEnabled and event.source() is not self and event.mimeData().hasUrls():
			event.acceptProposedAction()
		else:
			event.ignore()

	def dragMoveEvent(self, event):
		item = self._dropTarget(event)
		# Highlight the partition the file would be written to.
		self._setHover(self.indexFromItem(item) if item is not None else QModelIndex())
		if item is not None:
			event.acceptProposedAction()
		else:
			event.ignore()

	def dragLeaveEvent(self, event):
		self._setHover(QModelIndex())
		event.accept()

	def dropEvent(self, event):
		item = self._dropTarget(event)
		self._setHover(QModelIndex())
		if item is None:
			event.ignore()
			return
		event.setDropAction(Qt.CopyAction)
		event.accept()
		path = event.mimeData().urls()[0].toLocalFile()
		# Let the drag finish first, the source waits for it while the write asks for confirmation.
		QTimer.singleShot(0, lambda: self.fileDropped.emit(item, path))

	def mousePressEvent(self, event):
		super().mousePressEvent(event)
		# Not on Android, dragging scrolls the list there and the partitions cannot be dropped into other apps.
		self._press_pos = event.position().toPoint() if event.button() == Qt.LeftButton and not ANDROID else None

	def mouseReleaseEvent(self, event):
		super().mouseReleaseEvent(event)
		self._press_pos = None

	def mouseMoveEvent(self, event):
		super().mouseMoveEvent(event)
		if self._press_pos is None or not event.buttons() & Qt.LeftButton:
			return
		if (event.position().toPoint() - self._press_pos).manhattanLength() < QApplication.startDragDistance():
			return
		item = self.itemAt(self._press_pos)
		self._press_pos = None
		if (self.transfersEnabled and item is not None and item.text(0) in self._items and
		    can_read(item.data(0, self.DETAIL_ROLE))):
			self.dragRequested.emit(item)

	def selectedPartition(self):
		"""The name and details of the selected partition, ``(None, None)`` if there is none. The details
		are ``None`` until they are loaded."""
		item = self.currentItem()
		if item is None or item.text(0) not in self._items:
			return None, None
		return item.text(0), item.data(0, self.DETAIL_ROLE)

	@Slot()
	def _onLoaded(self):
		self._loader = None
		self.loaded.emit()

	@Slot(str)
	def _onFailed(self, message):
		self._loader = None
		self._logger.warning(f'listing the flash partitions failed: {message}')
		self.clear()
		self._items = {}
		QTreeWidgetItem(self, ['loading failed: %s' % message])
		self.loaded.emit()


class FlashPartitions(QWidget):
	"""The partition list with its toolbar and a hint how to write and read the partitions."""

	def __init__(self, client: FlashClient):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._client = client
		self._list = PartitionList(client)
		self._list.loaded.connect(self._loaded)
		self._list.currentItemChanged.connect(self._updateButtons)
		self._list.fileDropped.connect(self._fileDropped)
		self._list.dragRequested.connect(self._startDrag)
		# The temporary directory holding the partition read for the last drag, and the event loop
		# waiting for the read while the drop target waits for the file.
		self._drag_dir = None
		self._drag_loop = None
		# The operation running (erase, read, ...) and its progress window, None when there is none.
		self._operation = None
		self._progress = None

		self._tb = QToolBar()
		self._reload = DauiButton(self.tr('Reload'), Icons.refresh())
		self._reload.setToolTip(self.tr('Load the flash partitions from the device'))
		self._reload.clicked.connect(self.reload)
		self._tb.addWidget(self._reload)
		self._tb.addSeparator()
		self._write = DauiButton(self.tr('Write'), Icons.write())
		self._write.setToolTip(self.tr('Write a file to the flash partition'))
		self._write.clicked.connect(lambda: self.write())
		self._tb.addWidget(self._write)
		self._read = DauiButton(self.tr('Read'), Icons.read())
		self._read.setToolTip(self.tr('Read the flash partition to a file'))
		self._read.clicked.connect(self.read)
		self._tb.addWidget(self._read)
		self._erase = DauiButton(self.tr('Erase'), Icons.erase())
		self._erase.setToolTip(self.tr('Erase the flash partition'))
		self._erase.clicked.connect(self.erase)
		self._tb.addWidget(self._erase)

		self._layout = QVBoxLayout()
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.addWidget(self._tb)
		self._hint = Admonition(self.tr('Drag and drop files onto a partition in the list to write it, or drag a '
		                                'partition out of the list to read it to a file.'), Admonition.NOTE)
		# Keep the hint off the toolbar, the list and the window borders.
		hint_layout = QVBoxLayout()
		hint_layout.setContentsMargins(4, 4, 4, 4)
		hint_layout.addWidget(self._hint)
		# Files are dragged neither in nor out on Android.
		if not ANDROID:
			self._layout.addLayout(hint_layout)
		else:
			self._hint.hide()
		self._layout.addWidget(self._list)
		self.setLayout(self._layout)

		self.reload()

	@Slot()
	def reload(self):
		"""Load the partitions, the buttons are disabled meanwhile."""
		self._reload.setText(self.tr('Loading...'))
		self._list.refresh()
		self._updateButtons()

	@Slot()
	def _loaded(self):
		self._reload.setText(self.tr('Reload'))
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		# The flash service supports a single session, so one thing at a time.
		idle = not self._list.loading and self._operation is None
		self._reload.setEnabled(idle)
		name, detail = self._list.selectedPartition()
		self._write.setEnabled(idle and can_write(detail))
		self._list.transfersEnabled = idle
		self._read.setEnabled(idle and can_read(detail))
		self._erase.setEnabled(idle and detail is not None and detail['erase'] is not None)

	@Slot()
	def erase(self):
		"""Erase the selected partition after asking, showing the progress in a window."""
		name, detail = self._list.selectedPartition()
		if self._operation is not None or detail is None or detail['erase'] is None:
			return
		answer = QMessageBox.warning(
			self, self.tr('Erase partition'),
			self.tr('Erase the whole flash partition %s (%s)? All its content will be lost.') % (
				name, format_size(detail['size'])),
			QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
		if answer != QMessageBox.Yes:
			return

		size, erase_size = detail['size'], detail['erase']
		self._runOperation(
			lambda progress, abort, stage: self._client.erase(name, size, erase_size, progress, abort),
			name, size, self.tr('Erase'), self.tr('Erasing'),
			# Show the usage after erasing, even a partial one.
			finished=lambda ok: self.reload())

	@Slot()
	def read(self):
		"""Read the selected partition to a file chosen by the user, showing the progress in a window."""
		name, detail = self._list.selectedPartition()
		if self._operation is not None or not can_read(detail):
			return
		path, _filter = QFileDialog.getSaveFileName(
			self, self.tr('Read partition %s') % name, f'{name}.bin',
			self.tr('Binary files (*.bin);;All files (*)'))
		if not path:
			return
		self._readToFile(name, detail['size'], path)

	def _readToFile(self, name, size, path, finished=None):
		"""Read the partition ``name`` of ``size`` bytes to the file at ``path``, showing the progress in a
		window. ``finished`` is called with whether it succeeded once it ends, unless the read is dropped
		by :meth:`abort`."""
		# Read into memory, the file is written only once the whole partition is read. An aborted or failed
		# read leaves the file as it was.
		buffer = io.BytesIO()

		def run(progress, abort, stage):
			self._client.read(name, size, buffer, progress, abort)

		def done(ok):
			if ok:
				try:
					write_file(path, buffer.getvalue())
				except OSError as e:
					self._logger.warning(f'cannot write {path}: {e}')
					QMessageBox.warning(self, self.tr('Read partition'),
					                    self.tr('Cannot write %s: %s') % (display_name(path), e))
					ok = False
			if finished is not None:
				finished(ok)

		self._runOperation(run, name, size, self.tr('Read'), self.tr('Reading'), finished=done)

	@Slot(QTreeWidgetItem)
	def _startDrag(self, item):
		"""Drag the partition of ``item`` out of the list. It is read when the drop target asks for it."""
		name, detail = item.text(0), item.data(0, PartitionList.DETAIL_ROLE)
		self._list.setCurrentItem(item)
		drag = QDrag(self._list)
		drag.setMimeData(_PartitionMimeData(lambda: self._readForDrag(name, detail['size'])))
		drag.setPixmap(Icons.read().pixmap(32, 32))
		drag.exec(Qt.CopyAction)

	def _readForDrag(self, name, size):
		"""Read the partition ``name`` of ``size`` bytes to a temporary file named after it and return its
		path, or ``None`` if it cannot be read. It waits for the read, the drop target is waiting for the
		file meanwhile."""
		if self._operation is not None:
			return None
		# The file of the previous drag has been taken over by its drop target by now.
		self._removeDragDir()
		try:
			self._drag_dir = tempfile.mkdtemp(prefix='pydaui-flash-')
		except OSError as e:
			self._logger.warning(f'cannot create a temporary directory: {e}')
			return None
		path = os.path.join(self._drag_dir, f'{name}.bin')

		result = []
		self._drag_loop = QEventLoop()
		loop = self._drag_loop
		self._readToFile(name, size, path, finished=lambda ok: (result.append(ok), loop.quit()))
		if self._operation is not None:
			loop.exec()
		self._drag_loop = None
		return path if result and result[0] else None

	def _removeDragDir(self):
		if self._drag_dir is not None:
			shutil.rmtree(self._drag_dir, ignore_errors=True)
			self._drag_dir = None

	@Slot(QTreeWidgetItem, str)
	def _fileDropped(self, item, path):
		try:
			self._list.setCurrentItem(item)
		except RuntimeError:
			# The list was reloaded meanwhile.
			return
		self.write(path)

	@Slot()
	def write(self, path=None):
		"""Write the file at ``path`` to the selected partition after asking, showing the progress in a
		window. The partition is erased first. Without a ``path`` the user chooses the file."""
		name, detail = self._list.selectedPartition()
		if self._operation is not None or not can_write(detail):
			return
		if path is None:
			path, _filter = QFileDialog.getOpenFileName(
				self, self.tr('Write partition %s') % name, '', self.tr('Binary files (*.bin);;All files (*)'))
			if not path:
				return
		file_name = display_name(path)
		try:
			data = read_file(path)
		except OSError as e:
			QMessageBox.warning(self, self.tr('Write partition'), self.tr('Cannot read %s: %s') % (file_name, e))
			return
		size = detail['size']
		if not data:
			QMessageBox.warning(self, self.tr('Write partition'), self.tr('The file %s is empty.') % file_name)
			return
		if len(data) > size:
			QMessageBox.warning(self, self.tr('Write partition'),
			                    self.tr('The file %s (%s) does not fit the flash partition %s (%s).') % (
				                    file_name, format_size(len(data)), name, format_size(size)))
			return
		answer = QMessageBox.warning(
			self, self.tr('Write partition'),
			self.tr('Write the file %s (%s) to the flash partition %s (%s)? The whole partition is erased '
			        'first, all its content will be lost.') % (file_name, format_size(len(data)), name, format_size(size)),
			QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
		if answer != QMessageBox.Yes:
			return

		erase_size, page_size = detail['erase'], detail['page']
		stages = {
			'erase': self.tr('Erasing flash partition %s (%s) before writing...') % (name, format_size(size)),
			'write': self.tr('Writing %s (%s) to flash partition %s...') % (file_name, format_size(len(data)), name),
			'verify': self.tr('Verifying %s (%s) written to flash partition %s...') % (
				file_name, format_size(len(data)), name),
		}
		self._runOperation(
			lambda progress, abort, stage: self._client.write(name, size, erase_size, page_size, data,
			                                                  progress, abort, stage),
			name, size, self.tr('Write'), self.tr('Writing'), stages=stages,
			# Show the usage after writing, even a partial one.
			finished=lambda ok: self.reload())

	def _runOperation(self, fn, name, size, title, verb, stages=None, finished=None):
		"""Run the operation ``fn(progress, abort, stage)`` (see :class:`_Operation`) on the partition
		``name`` of ``size`` bytes, showing its progress in a window titled ``title``, the text starting
		with ``verb``. ``stages`` maps the stages the operation reports to the texts describing them, in
		the order they come, each shown as a step.
		``finished`` is called with whether the operation succeeded once it ends."""
		self._logger.info(f'{verb.lower()} flash partition {name!r}')
		self._progress = ProgressWindow(f'{title} {name}', self.tr('%s flash partition %s (%s)...') % (
			verb, name, format_size(size)), format=ByteFormat(size))
		self._progress.setProgress(0, size)
		self._progress.aborted.connect(self._abortOperation)
		self._showProgress(self._progress)

		def ended(error, aborted=False):
			self._operation = None
			self._progress.finish()
			self._progress = None
			if aborted:
				self._logger.info(f'{verb.lower()} flash partition {name!r} aborted')
			elif error is None:
				self._logger.info(f'{verb.lower()} flash partition {name!r} done')
			else:
				self._logger.warning(f'{verb.lower()} flash partition {name!r} failed: {error}')
				QMessageBox.warning(self, f'{title} {name}',
				                    self.tr('%s flash partition %s failed: %s') % (verb, name, error))
			if finished is not None:
				finished(error is None and not aborted)
			self._updateButtons()

		def progress(done):
			if self._progress is not None:
				self._progress.setProgress(done)

		def stage(what, total):
			if self._progress is not None:
				self._logger.info(f'{verb.lower()} flash partition {name!r}: {what}')
				if stages and what in stages:
					self._progress.setText(stages[what])
					if len(stages) > 1:
						self._progress.setStep(list(stages).index(what) + 1, len(stages))
				self._progress.setFormat(ByteFormat(total))
				self._progress.setProgress(0, total)

		self._operation = _Operation(fn)
		self._operation.stage.connect(stage)
		self._operation.progress.connect(progress)
		self._operation.done.connect(lambda: ended(None))
		self._operation.aborted.connect(lambda: ended(None, aborted=True))
		self._operation.failed.connect(lambda message: ended(message))
		self._operation.start()
		self._updateButtons()

	def _showProgress(self, window):
		"""Show the progress window in the MDI area the applet window is in, centered over it."""
		subwindow = self.parent()
		while subwindow is not None and not isinstance(subwindow, QMdiSubWindow):
			subwindow = subwindow.parent()
		if subwindow is not None and subwindow.mdiArea() is not None:
			subwindow.mdiArea().addSubWindow(window)
			window.move(subwindow.geometry().center() - window.rect().center())
		window.show()

	@Slot()
	def _abortOperation(self):
		if self._operation is not None:
			self._operation.abort()
			self._progress.setText(self.tr('Aborting...'))
			self._progress.setAbortEnabled(False)

	def abort(self):
		"""Abort the operation running, if any, and close its progress window, e.g. when the applet window
		closes. Nothing is reported of the operation any more."""
		if self._operation is not None:
			self._operation.blockSignals(True)
			self._operation.abort()
			self._operation = None
			self._progress.finish()
			self._progress = None
		# Stop waiting for a read of a dragged partition, the drop target gets nothing.
		if self._drag_loop is not None:
			self._drag_loop.quit()
		self._removeDragDir()


class FlashWindow(AppletWindow):
	"""The flash partitions served by an interface speaking the flash protocol on the connected device.

	It opens its own side connection to the interface, closed with the window. The window closes when
	the device is disconnected.
	"""

	def __init__(self, connection, name):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)

		self._logger.info(f'opening the flash partitions on interface {name!r}')
		self._side = connection.connectSide(name, flash_service.MAIN_EP)
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(f'Flash ({name})')
		self._partitions = FlashPartitions(FlashClient(self._side, interface_mtu(connection, name)))
		self.setWidget(self._partitions)
		self.resize(800, 400)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._partitions.abort()
			# Already disconnected together with the device connection, if that is why it closes.
			self._side.disconnectUri()
		super().closeEvent(event)
