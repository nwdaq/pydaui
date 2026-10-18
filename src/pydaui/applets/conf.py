# SPDX-License-Identifier: GPL-3.0-or-later
#
# Configuration tree applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Configuration tree applet: browse the configuration tree of a plumCore device.

It talks to an interface speaking the ``conf`` protocol (plumCore services/proto-conf), the same tree
services expose via ``*_get_conf()``. The client and the tree widget are ported from plumCore
``tools/proto-conf.py`` and ``tools/proto-conf-gui.py``. Subtrees are loaded lazily when expanded,
off the GUI thread, as every request is a round trip to the device.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import cbor2
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons
from pydaui.ui.tree import GuideTreeWidget


# enum conf_type (see services/interfaces/conf.h)
CONF_NONE = 0
CONF_SUBTREE = 1
CONF_F = 2
CONF_U32 = 3
CONF_S32 = 4
CONF_U16 = 5
CONF_S16 = 6
CONF_U8 = 7
CONF_S8 = 8
CONF_B = 9
CONF_BSTR = 10
CONF_STR = 11
CONF_ENUM = 12

TYPE_NAMES = {
	CONF_NONE: 'none',
	CONF_SUBTREE: 'subtree',
	CONF_F: 'float',
	CONF_U32: 'u32',
	CONF_S32: 's32',
	CONF_U16: 'u16',
	CONF_S16: 's16',
	CONF_U8: 'u8',
	CONF_S8: 's8',
	CONF_B: 'bool',
	CONF_BSTR: 'bstr',
	CONF_STR: 'str',
	CONF_ENUM: 'enum',
}

# enum conf_dir
CONF_DIR_NEXT = 0
CONF_DIR_PREV = 1
CONF_DIR_CHILD = 2
CONF_DIR_UP = 3

# enum conf_flag
CONF_READ = (1 << 0)
CONF_WRITE = (1 << 1)
CONF_CONST = (1 << 2)
CONF_STATUS = (1 << 3)
CONF_DETAIL = (1 << 4)
CONF_CREATE = (1 << 5)

#: The flag names shown in the tree, in the order of the bits.
FLAG_NAMES = (
	(CONF_READ, 'READ'),
	(CONF_WRITE, 'WRITE'),
	(CONF_CONST, 'CONST'),
	(CONF_STATUS, 'STATUS'),
	(CONF_DETAIL, 'DETAIL'),
	(CONF_CREATE, 'CREATE'),
)


def format_flags(flags):
	"""The flags set in ``flags`` as a list of bracketed names, e.g. ``[READ] [WRITE]``."""
	return ' '.join('[%s]' % name for bit, name in FLAG_NAMES if flags & bit)

#: The protocol name of the interfaces this applet talks to.
PROTOCOL = 'conf'


class ConfClient:
	"""conf protocol client over a :class:`~pydaui.connection.PyDauiConnection` connected to a conf interface.

	Requests are serialized, so the client may be used from several threads. Changes to the configuration
	(writes, the load and save jobs) are queued with :meth:`submit` and run one after another in the order
	they were submitted, so a save always follows the writes before it.
	"""

	#: Number of attempts of a request before giving up.
	RETRIES = 10

	#: How long the load and save jobs may take, in seconds.
	JOB_TIMEOUT = 10.0

	def __init__(self, connection):
		self._connection = connection
		# Shared by the clients on the same connection, see PyDauiConnection.sharedSide().
		self._lock = connection.lock
		self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conf-change')

	def submit(self, fn):
		"""Queue ``fn`` to run off the GUI thread after the changes queued before it."""
		self._queue.submit(fn)

	def close(self):
		"""Drop the queued changes not started yet."""
		self._queue.shutdown(wait=False, cancel_futures=True)

	def call(self, req: dict, timeout=None, retries=None):
		"""Send a request and return the decoded response map, or ``None`` if there is no valid one.

		Without ``timeout`` the retransmit interval suits the transport, e.g. a BLE link is much slower
		than UDP.
		"""
		try:
			return self.request(req, timeout, retries)
		except TimeoutError:
			return None

	def request(self, req: dict, timeout=None, retries=None):
		"""Like :meth:`call`, but raises ``TimeoutError`` when the device does not respond at all."""
		with self._lock:
			sock = self._connection.socket
			if sock is None:
				return None
			reply = sock.request(cbor2.dumps(req), timeout=timeout or sock.request_timeout,
			                     retries=retries or self.RETRIES)
		if reply is None:
			raise TimeoutError('no response')
		try:
			return cbor2.loads(reply)
		except Exception:
			return None

	def _walk(self, path, dir, limit=None):
		req = {'c': 'walk', 'p': path, 'dir': dir}
		if limit is not None:
			req['limit'] = limit
		r = self.call(req)
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err', 'unknown error'))
		return r.get('nodes', [])

	def _read(self, path, strict=False, retries=None):
		req = {'c': 'read', 'p': path}
		r = self.request(req, retries=retries) if strict else self.call(req, retries=retries)
		if r is None or r.get('err'):
			return None
		return r.get('val')

	def _next_siblings(self, path):
		"""Collect all siblings following the node identified by path. The responder
		caps a single walk to MAX_SIBLINGS, so keep walking from the last returned node."""
		result = []
		cur = path
		while True:
			batch = self._walk(cur, CONF_DIR_NEXT)
			if not batch:
				break
			result += batch
			cur = cur[:-1] + [batch[-1]['n']]
		return result

	def _children(self, path):
		"""Return the list of {n,t,f} child nodes of the node identified by path."""
		first = self._walk(path, CONF_DIR_CHILD)
		if not first:
			return []
		return first + self._next_siblings(path + [first[0]['n']])

	def description(self, path):
		"""The (brief, detail) help strings of the node at path, ``None`` for the ones it has not."""
		r = self.call({'c': 'get_desc', 'p': path})
		if r is None or r.get('err'):
			return None, None
		return r.get('brief'), r.get('detail')

	def _fmt_value(self, val):
		if isinstance(val, bytes):
			return val.hex()
		return str(val)

	def children(self, path=None):
		"""Return a single level of children of the node at path as a list of dicts:
		{'name', 'type', 'flags', 'value', 'is_subtree', 'path', 'brief', 'detail'}. Leaf values and the help
		strings of all the nodes are read here;
		subtrees are not descended into (lazy loading is up to the caller)."""
		path = path or []
		nodes = []
		for ch in self._children(path):
			name = ch.get('n', '')
			type = ch.get('t', CONF_NONE)
			child_path = path + [name]

			value = ''
			is_subtree = (type == CONF_SUBTREE)
			if not is_subtree and type != CONF_NONE:
				val = self._read(child_path)
				if val is not None:
					value = self._fmt_value(val)
			brief, detail = self.description(child_path)

			nodes.append({
				'name': name,
				'type': type,
				'flags': ch.get('f', 0),
				'value': value,
				'is_subtree': is_subtree,
				'path': child_path,
				'brief': brief or '',
				'detail': detail or '',
			})
		return nodes

	def read(self, path, strict=False, retries=None):
		"""Read the value of the node at path formatted for display, or ``None`` if it cannot be read. When
		``strict``, raises ``TimeoutError`` if the device does not respond to any of the ``retries``."""
		val = self._read(path, strict, retries)
		return None if val is None else self._fmt_value(val)

	@staticmethod
	def _parse_value(type_int, s):
		try:
			if type_int == CONF_F:
				return float(s)
			elif type_int in (CONF_U32, CONF_U16, CONF_U8, CONF_ENUM):
				return int(s, 0)
			elif type_int in (CONF_S32, CONF_S16, CONF_S8):
				return int(s, 0)
			elif type_int == CONF_B:
				if s.lower() in ('true', '1', 'yes', 'on'):
					return True
				if s.lower() in ('false', '0', 'no', 'off'):
					return False
				return None
			elif type_int == CONF_STR:
				return s
			elif type_int == CONF_BSTR:
				return bytes.fromhex(s)
		except (ValueError, TypeError):
			return None
		return None

	def parse(self, type_int, s):
		"""Parse the text ``s`` as a value of the given conf type, ``None`` if it is not valid."""
		return self._parse_value(type_int, s.strip() if type_int != CONF_STR else s)

	def write(self, path, type_int, val):
		"""Write the parsed value ``val`` of the given conf type to the node at path. Raises ``ValueError``
		with the reason if the write fails."""
		r = self.call({'c': 'write', 'p': path, 't': type_int, 'val': val})
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err'))

	def run_job(self, cmd):
		"""Run the load or save job on the device. The device replies once the job ends, so the request is
		sent only once with a long timeout, a retransmit would start the job again. Raises ``ValueError``
		with the reason if the job fails."""
		r = self.call({'c': cmd}, timeout=self.JOB_TIMEOUT, retries=1)
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err'))


def read_values(connection, interface, paths, retries=None):
	"""The values of the conf nodes at ``paths`` (lists of names) on the conf ``interface`` of the
	``connection``, formatted for display, ``None`` for a node which cannot be read. It asks the device over
	the shared side connection to the interface, run it off the GUI thread. Raises ``TimeoutError`` when the
	device does not respond to any of the ``retries`` of a read, :attr:`ConfClient.RETRIES` by default."""
	client = ConfClient(connection.sharedSide(interface))
	return [client.read(path, strict=True, retries=retries) for path in paths]


#: The conf types which can be edited as text.
EDITABLE_TYPES = (CONF_F, CONF_U32, CONF_S32, CONF_U16, CONF_S16, CONF_U8, CONF_S8, CONF_B, CONF_BSTR, CONF_STR,
                  CONF_ENUM)


def is_editable(type_int, flags):
	"""Whether a node of the given type and flags holds a value the user can change."""
	return bool(type_int in EDITABLE_TYPES and flags & CONF_WRITE and not flags & (CONF_CONST | CONF_STATUS))


class _ValueWriter(QObject):
	"""Writes one value off the GUI thread and reads it back, as the device may adjust what it stores.

	``done`` carries the value read back (``None`` if it cannot be read), ``failed`` the reason and the
	value read back. The tree item is carried through untouched.
	"""

	done = Signal(object, object)          # (tree item, value read back)
	failed = Signal(object, str, object)   # (tree item, error message, value read back)

	def __init__(self, conf, path, type_int, val, item):
		super().__init__()
		self._conf = conf
		self._path = path
		self._type = type_int
		self._val = val
		self._item = item

	def start(self):
		self._conf.submit(self._run)

	def _run(self):
		try:
			self._conf.write(self._path, self._type, self._val)
		except Exception as e:
			self.failed.emit(self._item, str(e), self._conf.read(self._path))
			return
		self.done.emit(self._item, self._conf.read(self._path))


class _JobRunner(QObject):
	"""Runs the load or save job (:meth:`ConfClient.run_job`) after the changes queued before it."""

	done = Signal()
	failed = Signal(str)

	def __init__(self, conf, cmd):
		super().__init__()
		self._conf = conf
		self._cmd = cmd

	def start(self):
		self._conf.submit(self._run)

	def _run(self):
		try:
			self._conf.run_job(self._cmd)
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.done.emit()


class ValueEditor(QLineEdit):
	"""The line edit of a value in the tree. It reports when its text should be written: after
	:attr:`IDLE_MS` without typing (``idle``), on Enter (``commit``) or when it loses the focus
	(``focusLost``, with whether the editor should close).
	"""

	#: Typing pause after which the value is written, in milliseconds.
	IDLE_MS = 2000

	idle = Signal()
	commit = Signal()
	cancel = Signal()
	focusLost = Signal(bool)   # close the editor
	navigate = Signal(int)     # -1 previous row, +1 next row

	# It covers the whole cell; the left padding (with the border) matches the cell text inset, so the
	# value does not move when the editor opens.
	STYLE = """
		QLineEdit {
			background-color: white;
			border: 1px solid %s;
			border-radius: 0;
			padding: 0 7px;
		}
	"""
	BORDER = '#7aa7e3'
	INVALID_BORDER = '#c0392b'

	def __init__(self, text):
		super().__init__(text)
		self._timer = QTimer(self)
		self._timer.setSingleShot(True)
		self._timer.setInterval(self.IDLE_MS)
		self._timer.timeout.connect(self.idle)
		self.textEdited.connect(lambda _text: self._timer.start())
		self.setValid(True)

	def setValid(self, valid):
		"""Show whether the text is a valid value, with a red border if not."""
		self.setStyleSheet(self.STYLE % (self.BORDER if valid else self.INVALID_BORDER))

	def stopIdleTimer(self):
		self._timer.stop()

	def keyPressEvent(self, event):
		key = event.key()
		if key in (Qt.Key_Return, Qt.Key_Enter):
			self._timer.stop()
			self.commit.emit()
		elif key == Qt.Key_Escape:
			self._timer.stop()
			self.cancel.emit()
		elif key in (Qt.Key_Up, Qt.Key_Down):
			self.navigate.emit(-1 if key == Qt.Key_Up else 1)
		else:
			super().keyPressEvent(event)

	def focusOutEvent(self, event):
		super().focusOutEvent(event)
		self._timer.stop()
		# Switching to another window or opening a popup keeps the editor, the value is written anyway.
		self.focusLost.emit(event.reason() not in (Qt.ActiveWindowFocusReason, Qt.PopupFocusReason))


class _RowDelegate(QStyledItemDelegate):
	"""Delegate of the tree cells: makes the rows with a detail line taller, keeping the cell texts on the
	first line, see :meth:`ConfTreeWidget.detailHeight`."""

	def __init__(self, tree):
		super().__init__(tree)
		self._tree = tree

	def sizeHint(self, option, index):
		size = super().sizeHint(option, index)
		size.setHeight(size.height() + self._tree.detailHeight(index))
		return size

	def paint(self, painter, option, index):
		height = self._tree.detailHeight(index)
		if not height:
			super().paint(painter, option, index)
			return
		# The background (e.g. of a selected row) over the whole cell, the cell itself above the detail line.
		background = QStyleOptionViewItem(option)
		self.initStyleOption(background, index)
		background.text = ''
		style = option.widget.style() if option.widget is not None else QApplication.style()
		style.drawPrimitive(QStyle.PE_PanelItemViewItem, background, painter, option.widget)
		cell = QStyleOptionViewItem(option)
		cell.rect = QRect(option.rect.left(), option.rect.top(), option.rect.width(), option.rect.height() - height)
		super().paint(painter, cell, index)


class _ValueDelegate(_RowDelegate):
	"""Delegate of the Value column: places the value editor over the whole cell and hides the text of the
	cell being edited, which would show through around the editor."""

	def updateEditorGeometry(self, editor, option, index):
		# The default places the editor in the text area, which the item padding squeezes. The editor
		# covers the whole cell instead, without the detail line below it.
		rect = QRect(option.rect)
		rect.setHeight(rect.height() - self._tree.detailHeight(index))
		editor.setGeometry(rect)

	def initStyleOption(self, option, index):
		super().initStyleOption(option, index)
		if self._tree.isEditing(index):
			option.text = ''


class _ChildrenLoader(QObject):
	"""Loads one level of the tree (:meth:`ConfClient.children`) off the GUI thread.

	The ``done``/``failed`` signals are delivered on the GUI thread, carrying the tree item the children
	belong to through untouched.
	"""

	done = Signal(object, list)    # (tree item, list of node dicts)
	failed = Signal(object, str)   # (tree item, error message)

	def __init__(self, conf, path, item):
		super().__init__()
		self._conf = conf
		self._path = path
		self._item = item

	def start(self):
		threading.Thread(target=self._run, name='conf-load', daemon=True).start()

	def _run(self):
		try:
			nodes = self._conf.children(self._path)
		except Exception as e:
			self.failed.emit(self._item, str(e))
			return
		self.done.emit(self._item, nodes)


class ConfTreeWidget(GuideTreeWidget):

	PATH_ROLE = Qt.UserRole

	#: Set on the placeholder child of an item whose children are being loaded.
	LOADING_ROLE = Qt.UserRole + 1

	#: Set on the placeholder child of an item whose children failed to load.
	FAILED_ROLE = Qt.UserRole + 2

	#: The conf type and flags of a node.
	TYPE_ROLE = Qt.UserRole + 3
	FLAGS_ROLE = Qt.UserRole + 4

	#: The full description of a node, empty if it has none.
	DETAIL_ROLE = Qt.UserRole + 5

	#: The column of the flags, of the values and their editor, and of the brief descriptions.
	FLAGS_COLUMN = 2
	VALUE_COLUMN = 3
	BRIEF_COLUMN = 4

	#: Colour of the flags.
	FLAGS_COLOR = '#999'

	#: Colour of the brief and the full descriptions, not to dominate over the values.
	BRIEF_COLOR = '#444'
	DETAIL_COLOR = '#444'

	#: The horizontal padding of the full description bubble, where the cells draw their texts, and its
	#: padding above (below the bottom padding of the cells) and below.
	DETAIL_HPADDING = 4
	DETAIL_TOP_PADDING = 2
	DETAIL_BOTTOM_PADDING = 9

	#: The look of the bubble, of a note Admonition: its background, border, corner radius, the size of its
	#: icon and its padding around the icon and the text.
	DETAIL_BACKGROUND = '#fff'
	DETAIL_BORDER = '#008'
	DETAIL_RADIUS = 5
	DETAIL_ICON_SIZE = 20
	DETAIL_MARGIN = 8

	#: Colour of a value whose last write failed.
	WRITE_FAILED_COLOR = '#c0392b'

	#: Relative widths of the Name, Type, Flags, Value and Brief description columns, which always fill the
	#: whole width.
	COLUMN_RATIOS = (4, 2, 3, 4, 6)

	def __init__(self, conf: ConfClient):
		super().__init__()
		self._conf = conf
		# Keep loader references alive until their signal has been delivered.
		self._loaders = set()
		# Keep writer references alive until their signal has been delivered.
		self._writers = set()
		# The item whose value is being edited, its editor and the value text the edit started from (or
		# was last written).
		self._edit_item = None
		self._editor = None
		self._edit_base = None
		# The lowercase filter text, empty when not filtering.
		self._filter = ''
		# Whether the full descriptions are shown below the nodes.
		self._details = False
		self._detail_icon = Icons.about()

		self.setColumnCount(5)
		self.setHeaderLabels(['Name', 'Type', 'Flags', 'Value', 'Brief description'])
		self.itemExpanded.connect(self._on_expanded)
		# The value of the current row is edited in place; a click on the current row reopens a closed editor.
		self.setEditTriggers(QAbstractItemView.NoEditTriggers)
		self._row_delegate = _RowDelegate(self)
		self.setItemDelegate(self._row_delegate)
		self._value_delegate = _ValueDelegate(self)
		self.setItemDelegateForColumn(self.VALUE_COLUMN, self._value_delegate)
		self.currentItemChanged.connect(lambda current, previous: self._openEditor(current))
		self.itemClicked.connect(self._itemClicked)

		self.refresh()

	def refresh(self):
		"""Reload the tree from the root, collapsing everything."""
		self._closeEditor(write=True)
		self.clear()
		root = self.invisibleRootItem()
		self._load(root, [], self._placeholder(root))

	@staticmethod
	def _placeholder(parent):
		return QTreeWidgetItem(parent, ['...'])

	def _load(self, item, path, placeholder):
		placeholder.setText(0, 'loading...')
		placeholder.setData(0, self.LOADING_ROLE, True)
		loader = _ChildrenLoader(self._conf, path, item)
		loader.done.connect(self._on_loaded)
		loader.failed.connect(self._on_failed)
		self._loaders.add(loader)
		loader.start()

	def _add_nodes(self, parent, nodes):
		for node in nodes:
			item = QTreeWidgetItem(parent, [
				node['name'],
				# A subtree is apparent from its expand/collapse indicator, it does not need a type.
				'' if node['is_subtree'] else TYPE_NAMES.get(node['type'], str(node['type'])),
				'' if node['is_subtree'] else format_flags(node['flags']),
				node['value'],
				node['brief'],
			])
			item.setForeground(self.FLAGS_COLUMN, QColor(self.FLAGS_COLOR))
			item.setForeground(self.BRIEF_COLUMN, QColor(self.BRIEF_COLOR))
			# The full description, the brief one is cut to the column width.
			item.setToolTip(self.BRIEF_COLUMN, node['detail'] or node['brief'])
			item.setData(0, self.PATH_ROLE, node['path'])
			item.setData(0, self.TYPE_ROLE, node['type'])
			item.setData(0, self.FLAGS_ROLE, node['flags'])
			item.setData(0, self.DETAIL_ROLE, node['detail'])
			self.styleName(item)
			if node['is_subtree']:
				# Add a placeholder child so the expand arrow appears; the real
				# children are loaded lazily when the item is first expanded.
				self._placeholder(item)

	def _on_expanded(self, item):
		# A single child without a stored path is the placeholder, not loaded yet or failed to load.
		if item.childCount() == 1 and item.child(0).data(0, self.PATH_ROLE) is None:
			placeholder = item.child(0)
			if not placeholder.data(0, self.LOADING_ROLE):
				self._load(item, item.data(0, self.PATH_ROLE), placeholder)

	@Slot(object, list)
	def _on_loaded(self, item, nodes):
		self._loaders.discard(self.sender())
		try:
			item.takeChildren()
			self._add_nodes(item, nodes)
		except RuntimeError:
			# The item was removed (e.g. the tree was refreshed) while loading.
			return
		if self._filter:
			self._apply_filter()

	@Slot(object, str)
	def _on_failed(self, item, message):
		self._loaders.discard(self.sender())
		try:
			# Leave a placeholder in place, collapsing and expanding the item again retries.
			item.takeChildren()
			placeholder = self._placeholder(item)
			placeholder.setText(0, 'loading failed: %s' % message)
			# Not loaded again by the filter, only when expanded.
			placeholder.setData(0, self.FAILED_ROLE, True)
		except RuntimeError:
			pass

	def setShowDetails(self, show):
		"""Show the full description of each node below it, word-wrapped across all the columns."""
		self._details = show
		# The row heights change.
		self.scheduleDelayedItemsLayout()

	def _detailRect(self, index):
		"""The left and the width of the full description bubble of the node at ``index``, aligned with its
		name."""
		depth = 0
		parent = index.parent()
		while parent.isValid():
			depth, parent = depth + 1, parent.parent()
		indent = self.indentation() * (depth + (1 if self.rootIsDecorated() else 0))
		left = self.header().sectionViewportPosition(0) + indent + self.DETAIL_HPADDING
		return left, max(self.viewport().width() - self.DETAIL_HPADDING - left, 1)

	def detailHeight(self, index):
		if not self._details or not index.isValid():
			return 0
		detail = index.siblingAtColumn(0).data(self.DETAIL_ROLE)
		if not detail:
			return 0
		bubble = self._detailTextHeight(index, detail) + 2 * self.DETAIL_MARGIN
		return self.DETAIL_TOP_PADDING + max(bubble, self.DETAIL_ICON_SIZE + 2 * self.DETAIL_MARGIN) + \
		       self.DETAIL_BOTTOM_PADDING

	def _detailTextWidth(self, index):
		"""The width of the text in the bubble, right of the icon."""
		left, width = self._detailRect(index)
		return max(width - self.DETAIL_ICON_SIZE - 3 * self.DETAIL_MARGIN, 1)

	def _detailTextHeight(self, index, detail):
		rect = QRect(0, 0, self._detailTextWidth(index), 100000)
		return self.fontMetrics().boundingRect(rect, Qt.TextWordWrap, detail).height()

	def drawRow(self, painter, option, index):
		super().drawRow(painter, option, index)
		height = self.detailHeight(index)
		if not height:
			return
		left, width = self._detailRect(index)
		bubble = QRect(left, option.rect.bottom() + 1 - height + self.DETAIL_TOP_PADDING,
		               width, height - self.DETAIL_TOP_PADDING - self.DETAIL_BOTTOM_PADDING)
		painter.save()
		painter.setRenderHint(QPainter.Antialiasing)
		painter.setPen(QPen(QColor(self.DETAIL_BORDER), 1))
		painter.setBrush(QColor(self.DETAIL_BACKGROUND))
		# On the pixel centres, the border is sharp.
		painter.drawRoundedRect(QRectF(bubble).adjusted(0.5, 0.5, -0.5, -0.5), self.DETAIL_RADIUS, self.DETAIL_RADIUS)
		# The icon centred vertically, as in an Admonition.
		size = self.DETAIL_ICON_SIZE
		self._detail_icon.paint(painter, QRect(bubble.left() + self.DETAIL_MARGIN,
		                                       bubble.top() + (bubble.height() - size) // 2, size, size))
		text_left = bubble.left() + 2 * self.DETAIL_MARGIN + size
		painter.setFont(self.font())
		painter.setPen(QColor(self.DETAIL_COLOR))
		painter.drawText(QRect(text_left, bubble.top() + self.DETAIL_MARGIN, self._detailTextWidth(index),
		                       bubble.height() - 2 * self.DETAIL_MARGIN),
		                 Qt.TextWordWrap | Qt.AlignVCenter, index.siblingAtColumn(0).data(self.DETAIL_ROLE))
		painter.restore()

	def mousePressEvent(self, event):
		# A subtree row clicked anywhere, its indicator included, is expanded or collapsed here, not by the tree.
		item = self.itemAt(event.position().toPoint())
		if event.button() != Qt.LeftButton or item is None or item.data(0, self.TYPE_ROLE) != CONF_SUBTREE:
			super().mousePressEvent(event)
			return
		self.setCurrentItem(item)
		item.setExpanded(not item.isExpanded())

	def mouseDoubleClickEvent(self, event):
		# Double clicks do nothing, their second click is pressed as another click.
		pass

	def resizeEvent(self, event):
		super().resizeEvent(event)
		if self._details:
			# The full descriptions wrap to the new width.
			self.scheduleDelayedItemsLayout()

	def isEditing(self, index):
		"""Whether the value at ``index`` is being edited."""
		return (self._edit_item is not None and index.column() == self.VALUE_COLUMN and
		        index.siblingAtColumn(0) == self.indexFromItem(self._edit_item, 0))

	def _isEditable(self, item):
		return (item is not None and item.data(0, self.PATH_ROLE) is not None and
		        is_editable(item.data(0, self.TYPE_ROLE), item.data(0, self.FLAGS_ROLE) or 0))

	@Slot(QTreeWidgetItem, int)
	def _itemClicked(self, item, column):
		if item is self.currentItem() and self._edit_item is None:
			self._openEditor(item)

	def _openEditor(self, item):
		"""Show an editor of the value in the Value cell of ``item``, if the value can be changed."""
		if item is self._edit_item:
			return
		self._closeEditor(write=True)
		if not self._isEditable(item):
			return

		text = item.text(self.VALUE_COLUMN)
		editor = ValueEditor(text)
		editor.setFont(item.font(self.VALUE_COLUMN))
		editor.idle.connect(lambda: self._writeEdited(close=False))
		editor.commit.connect(lambda: self._writeEdited(close=False))
		editor.cancel.connect(self._cancelEdit)
		editor.focusLost.connect(lambda close: self._writeEdited(close=close))
		editor.navigate.connect(self._navigate)
		editor.textChanged.connect(self._validateEdited)

		# The editor covers the whole cell, see _ValueDelegate.updateEditorGeometry().
		self._edit_item, self._editor, self._edit_base = item, editor, text
		self.setItemWidget(item, self.VALUE_COLUMN, editor)
		editor.setFocus(Qt.OtherFocusReason)
		editor.selectAll()

	def _closeEditor(self, write):
		"""Remove the editor, writing its value first if ``write`` and it changed."""
		if self._edit_item is None:
			return
		if write:
			self._writeEdited(close=False)
		item, editor = self._edit_item, self._editor
		self._edit_item = self._editor = self._edit_base = None
		editor.stopIdleTimer()
		# Removing the editor takes its focus away; its signals must not act on the next editor.
		editor.blockSignals(True)
		try:
			# The widget is deleted later, it is safe to remove it from within its own event handlers.
			# Hide it right away, it would stay in place until deleted.
			widget = self.itemWidget(item, self.VALUE_COLUMN)
			if widget is not None:
				widget.hide()
			self.removeItemWidget(item, self.VALUE_COLUMN)
			# Repaint the cell text hidden while editing.
			self.viewport().update(self.visualItemRect(item))
		except RuntimeError:
			# The item is gone already (e.g. the tree was cleared).
			pass

	def finishEditing(self, write):
		"""Close the editor, writing its value first if ``write`` and it changed."""
		self._closeEditor(write=write)

	@Slot()
	def _cancelEdit(self):
		"""Close the editor without writing, the keyboard focus returns to the tree."""
		self._closeEditor(write=False)
		self.setFocus()

	@Slot(int)
	def _navigate(self, step):
		current = self.currentItem()
		target = self.itemAbove(current) if step < 0 else self.itemBelow(current)
		if target is not None:
			self.setCurrentItem(target)
		# Keep the keyboard focus in the tree when there is no editor on the new row.
		if self._editor is None:
			self.setFocus()

	@Slot(str)
	def _validateEdited(self, text):
		if self._editor is not None:
			self._editor.setValid(self._conf.parse(self._edit_item.data(0, self.TYPE_ROLE), text) is not None)

	def _writeEdited(self, close):
		"""Write the text of the editor if it changed since the edit started or was last written, then
		close the editor if ``close``. Text which is not a valid value is not written; closing the editor
		discards it."""
		item, editor = self._edit_item, self._editor
		if item is None:
			return
		text = editor.text()
		if text != self._edit_base:
			type_int = item.data(0, self.TYPE_ROLE)
			val = self._conf.parse(type_int, text)
			if val is None:
				if close:
					logging.getLogger(self.__class__.__name__).warning(
						f'not writing {"/".join(item.data(0, self.PATH_ROLE))}: {text!r} is not a valid '
						f'{TYPE_NAMES.get(type_int, type_int)}')
			else:
				self._edit_base = text
				item.setText(self.VALUE_COLUMN, text)
				writer = _ValueWriter(self._conf, item.data(0, self.PATH_ROLE), type_int, val, item)
				writer.done.connect(self._on_written)
				writer.failed.connect(self._on_write_failed)
				self._writers.add(writer)
				writer.start()
		if close:
			self._closeEditor(write=False)

	def _showWritten(self, item, value):
		"""Show the value read back after a write, in the cell and in the editor if it was not edited since."""
		if value is None:
			return
		if item is self._edit_item and self._editor.text() == self._edit_base:
			self._editor.setText(value)
			self._edit_base = value
		item.setText(self.VALUE_COLUMN, value)

	@Slot(object, object)
	def _on_written(self, item, value):
		self._writers.discard(self.sender())
		try:
			item.setData(self.VALUE_COLUMN, Qt.ForegroundRole, None)
			item.setToolTip(self.VALUE_COLUMN, '')
			self._showWritten(item, value)
		except RuntimeError:
			pass

	@Slot(object, str, object)
	def _on_write_failed(self, item, message, value):
		self._writers.discard(self.sender())
		try:
			path = '/'.join(item.data(0, self.PATH_ROLE))
			logging.getLogger(self.__class__.__name__).warning(f'writing {path} failed: {message}')
			item.setForeground(self.VALUE_COLUMN, QColor(self.WRITE_FAILED_COLOR))
			item.setToolTip(self.VALUE_COLUMN, f'writing the value failed: {message}')
			self._showWritten(item, value)
		except RuntimeError:
			pass

	def setFilter(self, text):
		"""Show only the items with a path component containing ``text`` (ignoring case), together with
		their subtrees and ancestors. Empty text shows all items.

		The tree is loaded lazily, so while filtering the subtrees not loaded yet are loaded in the
		background to be searched too, and the filter is applied again as they arrive.
		"""
		self._filter = text.strip().lower()
		self._apply_filter()

	def _apply_filter(self):
		root = self.invisibleRootItem()
		for i in range(root.childCount()):
			self._filter_item(root, root.child(i), False)

	def _filter_item(self, parent, item, ancestor_matches):
		"""Show or hide ``item`` and its subtree. Returns whether the item or any of its descendants
		matches the filter by itself."""
		path = item.data(0, self.PATH_ROLE)
		if path is None:
			# The placeholder of children not loaded yet, shown with its parent. Load them to search them.
			item.setHidden(False)
			if self._filter and not item.data(0, self.LOADING_ROLE) and not item.data(0, self.FAILED_ROLE):
				self._load(parent, parent.data(0, self.PATH_ROLE) or [], item)
			return False

		matches = bool(self._filter) and any(self._filter in component.lower() for component in path)
		descendant_matches = False
		for i in range(item.childCount()):
			if self._filter_item(item, item.child(i), ancestor_matches or matches):
				descendant_matches = True

		item.setHidden(bool(self._filter) and not (ancestor_matches or matches or descendant_matches))
		if self._filter and descendant_matches:
			# Reveal the matching descendants.
			item.setExpanded(True)
		return matches or descendant_matches


class ConfTree(QWidget):
	"""The configuration tree with its toolbar: Reload and Save on the left, the filter on the right."""

	def __init__(self, conf: ConfClient):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._conf = conf
		self._tree = ConfTreeWidget(conf)
		# Keep the job runner alive until its signal has been delivered.
		self._job = None

		self._tb = QToolBar()
		self._reload = DauiButton(self.tr('Reload'), Icons.refresh())
		self._reload.setToolTip(self.tr('Load the saved configuration on the device and show it'))
		self._reload.clicked.connect(lambda: self._runJob('load'))
		self._tb.addWidget(self._reload)
		self._save = DauiButton(self.tr('Save'), Icons.save())
		self._save.setToolTip(self.tr('Save the configuration on the device'))
		self._save.clicked.connect(lambda: self._runJob('save'))
		self._tb.addWidget(self._save)

		self._details = QCheckBox(self.tr('Show details'))
		self._details.setToolTip(self.tr('Show the full description of each configuration item below it'))
		# Spaced from the buttons like the filter label.
		self._details.setContentsMargins(6, 0, 0, 0)
		self._details.toggled.connect(self._tree.setShowDetails)
		self._tb.addWidget(self._details)

		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)

		label = QLabel(self.tr('Filter:'))
		label.setContentsMargins(6, 0, 4, 0)
		self._tb.addWidget(label)
		self._filter = QLineEdit()
		self._filter.setPlaceholderText(self.tr('configuration item name'))
		self._filter.setFixedWidth(220)
		# As tall as the buttons next to it.
		self._filter.setFixedHeight(self._reload.sizeHint().height())
		self._filter.textChanged.connect(self._tree.setFilter)
		self._tb.addWidget(self._filter)
		cancel = DauiButton(btn_icon=Icons.cancel())
		cancel.setToolTip(self.tr('Cancel the filter'))
		cancel.clicked.connect(self._filter.clear)
		self._tb.addWidget(cancel)

		self._layout = QVBoxLayout()
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.addWidget(self._tb)
		self._layout.addWidget(self._tree)
		self.setLayout(self._layout)

	_JOB_TEXT = {
		'load': ('Reload', 'Loading...', 'Loading the saved configuration failed'),
		'save': ('Save', 'Saving...', 'Saving the configuration failed'),
	}

	def _runJob(self, cmd):
		"""Run the load or save job on the device, the buttons are disabled meanwhile."""
		if self._job is not None:
			return
		# A value being edited is written before saving; loading replaces it with the saved one.
		self._tree.finishEditing(write=(cmd == 'save'))

		button = self._reload if cmd == 'load' else self._save
		button.setText(self.tr(self._JOB_TEXT[cmd][1]))
		self._reload.setEnabled(False)
		self._save.setEnabled(False)

		self._job = _JobRunner(self._conf, cmd)
		self._job.done.connect(lambda: self._jobFinished(cmd, None))
		self._job.failed.connect(lambda message: self._jobFinished(cmd, message))
		self._job.start()

	def _jobFinished(self, cmd, error):
		self._job = None
		self._reload.setText(self.tr(self._JOB_TEXT['load'][0]))
		self._save.setText(self.tr(self._JOB_TEXT['save'][0]))
		self._reload.setEnabled(True)
		self._save.setEnabled(True)

		if error is not None:
			self._logger.warning(f'{cmd} job failed: {error}')
			QMessageBox.warning(self, self.tr('Configuration'), f'{self.tr(self._JOB_TEXT[cmd][2])}: {error}')
			return
		self._logger.info(f'{cmd} job finished')
		if cmd == 'load':
			# Show the configuration just loaded.
			self._tree.refresh()


class ConfTreeWindow(AppletWindow):
	"""The configuration tree served by an interface speaking the conf protocol on the connected device.

	It talks over the side connection to the interface shared with the other users of the interface, the
	conf service serves a single session. The window closes when the device is disconnected.
	"""

	def __init__(self, connection, name):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)

		self._logger.info(f'opening the configuration tree on interface {name!r}')
		self._side = connection.sharedSide(name)
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(f'Configuration tree ({name})')
		self._conf = ConfClient(self._side)
		self.setWidget(ConfTree(self._conf))
		self.resize(950, 500)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._conf.close()
		super().closeEvent(event)
