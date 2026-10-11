# SPDX-License-Identifier: GPL-3.0-or-later
#
# plumCore updates applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""plumCore updates applet: fetch the latest firmware files from a plumCore repository into the cache,
write one to the ``update`` flash partition and reset the device for the bootloader to install it.

The applet is offered for a device with a writable flash partition named ``update``, which the plumCore
bootloader installs the firmware from. It talks to an interface speaking the ``flash`` protocol, whether
it has such a partition is known only after asking the device, see :func:`probe`.

A repository is an HTTP(S) URL. Asked with the ``list`` and ``latest=<count>`` query parameters it
returns the latest files as JSON::

    {"path": "/firmware", "entries": [{"name": "...", "url": "/firmware/...", "mtime": "<ISO 8601>",
                                        "size": 48760, "mime": "...", "type": "file"}, ...]}

The firmware images among them (:func:`is_firmware`) are downloaded to a directory of the repository in
the application cache, together with the listing, which is shown again when the window opens.

The device is reset by writing true to the ``/system/reset`` conf node on the conf interface of the same
unit as the flash interface, e.g. ``ff14 conf`` for ``ff14 flash``.
"""

import hashlib
import io
import json
import logging
import lzma
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import cbor2
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from pynbus2.services import flash as flash_service

from pydaui import build_info, tls
from pydaui.applets import conf
from pydaui.connection import SocketConnection
from pydaui.applets.flash import FlashClient, _Operation, can_write, format_size, interface_mtu
from pydaui.ui.credentials_window import CredentialsWindow
from pydaui.ui.helpers import Admonition, AppletWindow, DauiButton, StyledTreeWidget
from pydaui.ui.icons import Icons
from pydaui.ui.progress_window import ByteFormat, ProgressWindow
from pydaui.ui.settings_window import RepositoriesSection

#: The protocol name of the interfaces this applet talks to.
PROTOCOL = flash_service.PROTOCOL

#: The name of the partition the bootloader installs the firmware from.
UPDATE_PARTITION = 'update'

#: A firmware image, the signed application in the xip format, possibly xz compressed. The name ends with
#: the version, e.g. ``plumcore-nwdaq-m-ff14-meas-generic-0.9.0-dev.67+efb27c5.20260927.elf.xip.xz``.
FIRMWARE_NAME = re.compile(r'^(?P<base>.*?)-(?P<version>\d+\.\d+\.\d+\S*?)\.elf\.xip(\.xz)?$')

ELF_MAGIC = b'\x7fELF'
XZ_MAGIC = b'\xfd7zXZ\x00'

#: The conf node resetting the device when true is written to it.
RESET_PATH = ['system', 'reset']

#: Sections named ``.sign.<type>`` carry a signature of the given type.
SIGNATURE_PREFIX = '.sign.'

#: How long a repository request may take, in seconds.
HTTP_TIMEOUT = 15.0


def probe(connection, interface):
	"""The name of a writable ``update`` partition on the flash ``interface`` of the device, ``None`` if
	there is none. It asks the device, run it off the GUI thread.

	A partition registered under several names (e.g. ``fw-update`` aliased as ``update``) is listed under
	the first of them only, so ``update`` is asked for by name and the listed partitions with ``update`` in
	their names are tried too.
	"""
	socket = connection.nbus.socket(interface, flash_service.MAIN_EP)
	try:
		client = FlashClient(SocketConnection(socket))
		names = [UPDATE_PARTITION] + [name for name in client.list() if UPDATE_PARTITION in name]
		for name in dict.fromkeys(names):
			try:
				size, erase, page = client.info(name)
			except ValueError:
				continue
			if can_write({'size': size, 'erase': erase, 'page': page}):
				return name
		return None
	finally:
		socket.close()


def is_firmware(entry):
	"""Whether the repository listing ``entry`` is a firmware image which can be installed."""
	return entry.get('type', 'file') == 'file' and FIRMWARE_NAME.match(entry.get('name', '')) is not None


def firmware_version(name):
	"""The version in the firmware file ``name`` without the build metadata following ``+``, empty if it
	has none."""
	match = FIRMWARE_NAME.match(name)
	return match['version'].partition('+')[0] if match else ''


def firmware_build_date(name):
	"""The build date in the build metadata of the version in the firmware file ``name`` as
	``YYYY-MM-DD``, e.g. ``2026-09-27`` from ``0.9.0-dev.67+efb27c5.20260927``. Empty if it has none."""
	match = FIRMWARE_NAME.match(name)
	metadata = match['version'].partition('+')[2] if match else ''
	for part in reversed(metadata.split('.')):
		try:
			return datetime.strptime(part, '%Y%m%d').date().isoformat()
		except ValueError:
			continue
	return ''


def inspect_image(path):
	"""The format of the firmware image at ``path`` and its signature as ``(format, signature)``. The
	signature is ``(type, value)`` from the ``.sign.<type>`` section, ``None`` if the image is not signed.
	Raises ``ValueError`` if the image is neither an ELF nor an xz compressed one."""
	with open(path, 'rb') as file:
		data = file.read()
	if data.startswith(XZ_MAGIC):
		try:
			data = lzma.decompress(data)
		except lzma.LZMAError as e:
			raise ValueError(f'cannot decompress: {e}')
		if not data.startswith(ELF_MAGIC):
			raise ValueError('not an xz compressed ELF')
		image_format = 'XZ compressed XIP ELF'
	elif data.startswith(ELF_MAGIC):
		image_format = 'XIP ELF'
	else:
		raise ValueError('unknown image format')

	try:
		elf = ELFFile(io.BytesIO(data))
		# The section of an unsigned image is all zeroes, see plumCore scripts/elfsign.py.
		for section in elf.iter_sections():
			if section.name.startswith(SIGNATURE_PREFIX) and any(section.data()):
				return image_format, (section.name[len(SIGNATURE_PREFIX):], section.data())
	except ELFError as e:
		raise ValueError(f'invalid ELF: {e}')
	return image_format, None


def list_url(url, latest):
	"""The ``url`` of a repository asking for the listing of its ``latest`` files."""
	parts = urllib.parse.urlsplit(url)
	query = '&'.join(q for q in (parts.query, 'list', f'latest={latest}') if q)
	return urllib.parse.urlunsplit(parts._replace(query=query))


def _mtime(entry):
	"""The modification time of the repository listing ``entry`` as a timestamp, ``None`` if unknown."""
	try:
		return datetime.fromisoformat(entry['mtime']).timestamp()
	except (KeyError, TypeError, ValueError):
		return None


class AuthenticationRequired(Exception):
	"""The repository asks for a username and password with HTTP basic authentication, or did not accept
	the ones given."""

	def __init__(self, realm, rejected):
		super().__init__(f'authentication required ({realm})' if realm else 'authentication required')
		#: The realm the server names, empty if it names none.
		self.realm = realm
		#: Whether credentials were given and not accepted.
		self.rejected = rejected


def _basic_realm(challenge):
	"""The realm of the HTTP basic authentication ``challenge`` (a ``WWW-Authenticate`` header value).
	Raises ``ValueError`` if the server asks for another authentication scheme."""
	scheme, _, params = (challenge or '').strip().partition(' ')
	if scheme.lower() != 'basic':
		raise ValueError(f'unsupported authentication {scheme or "(none)"}, only basic is supported')
	match = re.search(r'realm="([^"]*)"', params)
	return match[1] if match else ''


class Repository:
	"""A repository and its directory in the application cache, holding the downloaded files and the last
	listing in :attr:`INDEX`.

	``auth`` is ``(username, password)`` for a repository behind HTTP basic authentication, sent with
	every request under the repository URL. Requests raise :class:`AuthenticationRequired` if the
	repository asks for credentials.
	"""

	INDEX = 'index.json'

	def __init__(self, name, url, auth=None):
		self.name = name
		self.url = url
		self.auth = auth
		cache = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation))
		# The name may change or be shared, the URL identifies the repository.
		self.dir = cache / 'repositories' / hashlib.sha256(url.encode()).hexdigest()[:16]

	def path(self, entry):
		"""The path of the listing ``entry`` downloaded to the cache."""
		# The name comes from the server, keep it in the directory.
		return self.dir / os.path.basename(entry['name'])

	def isCurrent(self, entry):
		"""Whether the file of the listing ``entry`` is downloaded and is the one in the repository."""
		try:
			stat = self.path(entry).stat()
		except OSError:
			return False
		mtime = _mtime(entry)
		return stat.st_size == entry.get('size') and (mtime is None or stat.st_mtime == mtime)

	def cachedEntries(self):
		"""The firmware files of the last listing, empty if there is none."""
		try:
			entries = json.loads((self.dir / self.INDEX).read_text())
		except (OSError, ValueError):
			return []
		return [entry for entry in entries if isinstance(entry, dict) and is_firmware(entry)] \
			if isinstance(entries, list) else []

	def fetchList(self, latest):
		"""Ask the repository for its ``latest`` files, return the firmware files among them."""
		data = json.loads(self._get(list_url(self.url, latest)))
		if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
			raise ValueError('invalid repository listing')
		return [entry for entry in data['entries'] if isinstance(entry, dict) and is_firmware(entry)]

	def saveIndex(self, entries):
		"""Keep the listing ``entries`` and remove the files not in it."""
		self.dir.mkdir(parents=True, exist_ok=True)
		keep = {self.path(entry).name for entry in entries} | {self.INDEX}
		for path in self.dir.iterdir():
			if path.name not in keep and path.is_file():
				path.unlink(missing_ok=True)
		tmp = self.dir / (self.INDEX + '.tmp')
		tmp.write_text(json.dumps(entries, indent='\t'))
		os.replace(tmp, self.dir / self.INDEX)

	def download(self, entry, progress=None):
		"""Download the file of the listing ``entry`` to the cache. ``progress`` is called with the bytes
		downloaded so far. A failed download leaves no partial file behind."""
		self.dir.mkdir(parents=True, exist_ok=True)
		path = self.path(entry)
		fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=self.dir)
		try:
			with os.fdopen(fd, 'wb') as file, self._open(urllib.parse.urljoin(self.url, entry['url'])) as response:
				done = 0
				while chunk := response.read(64 * 1024):
					file.write(chunk)
					done += len(chunk)
					if progress is not None:
						progress(done)
			if entry.get('size') is not None and done != entry['size']:
				raise ValueError(f'got {done} B of {entry["size"]} B')
			mtime = _mtime(entry)
			if mtime is not None:
				# Tells whether the cached file is still the one in the repository.
				os.utime(tmp, (mtime, mtime))
			os.replace(tmp, path)
		except BaseException:
			os.remove(tmp)
			raise

	def _open(self, url):
		request = urllib.request.Request(url, headers={'User-Agent': f'pydaui/{build_info.version() or "unknown"}'})
		handlers = [urllib.request.HTTPSHandler(context=tls.context())]
		if self.auth is not None:
			passwords = urllib.request.HTTPPasswordMgrWithPriorAuth()
			# Sent right away instead of after a challenge, and only to the URLs under the repository. The
			# server lists the files with repeated slashes in the path collapsed, e.g. /product/customer/...
			# for a repository at /product//customer/..., so the credentials go to both.
			parts = urllib.parse.urlsplit(self.url)
			normalized = urllib.parse.urlunsplit(parts._replace(path=re.sub('/+', '/', parts.path)))
			passwords.add_password(None, [self.url, normalized], *self.auth, is_authenticated=True)
			handlers.append(urllib.request.HTTPBasicAuthHandler(passwords))
		try:
			return urllib.request.build_opener(*handlers).open(request, timeout=HTTP_TIMEOUT)
		except urllib.error.HTTPError as e:
			if e.code != 401:
				raise
			realm = _basic_realm(e.headers.get('WWW-Authenticate'))
			raise AuthenticationRequired(realm, self.auth is not None) from None

	def _get(self, url):
		with self._open(url) as response:
			return response.read()


class _RepositoryUpdate(QObject):
	"""Lists the repository and downloads the files not in the cache yet, off the GUI thread.

	``listed`` carries the firmware files of the repository, then for each file to download ``progress``
	follows with its name and the bytes downloaded so far and ``downloaded`` with its name and the error
	message (``None`` if it succeeded). ``done`` ends it, ``failed`` ends it if the repository cannot be
	listed, ``authenticationRequired`` with the :class:`AuthenticationRequired` if it asks for credentials.
	"""

	listed = Signal(list)
	progress = Signal(str, int)
	downloaded = Signal(str, object)
	done = Signal()
	failed = Signal(str)
	authenticationRequired = Signal(object)

	def __init__(self, repository, latest):
		super().__init__()
		self._repository = repository
		self._latest = latest

	def start(self):
		threading.Thread(target=self._run, name='repository-update', daemon=True).start()

	def _run(self):
		try:
			entries = self._repository.fetchList(self._latest)
			self._repository.saveIndex(entries)
		except AuthenticationRequired as e:
			self.authenticationRequired.emit(e)
			return
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.listed.emit(entries)
		for entry in entries:
			if self._repository.isCurrent(entry):
				continue
			name = entry['name']
			try:
				self._repository.download(entry, lambda done: self.progress.emit(name, done))
			except Exception as e:
				self.downloaded.emit(name, str(e))
				continue
			self.downloaded.emit(name, None)
		self.done.emit()


class FirmwareList(StyledTreeWidget):
	"""The firmware files of a repository, the newest first, marked when they are downloaded."""

	VERSION_COLUMN = 0
	NAME_COLUMN = 1
	CACHED_COLUMN = 2

	COLUMN_RATIOS = (3, 8, 2)

	#: Colour of the files which could not be downloaded.
	FAILED_COLOR = '#c0392b'

	#: The repository listing entry of a file.
	ENTRY_ROLE = Qt.UserRole

	def __init__(self):
		super().__init__()
		self.setColumnCount(3)
		self.setHeaderLabels([self.tr('Version'), self.tr('File name'), self.tr('Downloaded')])
		self.setRootIsDecorated(False)
		self.setEditTriggers(QAbstractItemView.NoEditTriggers)
		self._items = {}

	def setEntries(self, entries, repository):
		"""Show the listing ``entries`` of ``repository``, marking the files downloaded already. The file
		selected stays selected if it is listed again."""
		selected = self.selectedEntry()
		self.clear()
		self._items = {}
		for entry in sorted(entries, key=lambda entry: _mtime(entry) or 0, reverse=True):
			name = entry['name']
			item = QTreeWidgetItem(self, [firmware_version(name), name, ''])
			self.styleName(item)
			item.setData(0, self.ENTRY_ROLE, entry)
			item.setToolTip(self.NAME_COLUMN, name)
			self._items[name] = item
			self.setCached(name, repository.isCurrent(entry))
			if selected is not None and selected['name'] == name:
				self.setCurrentItem(item)

	def showMessage(self, text):
		self.clear()
		self._items = {}
		QTreeWidgetItem(self, [text]).setFirstColumnSpanned(True)

	def selectedEntry(self):
		"""The repository listing entry of the selected file, ``None`` if there is none."""
		item = self.currentItem()
		return item.data(0, self.ENTRY_ROLE) if item is not None else None

	def setCached(self, name, cached, error=None):
		"""Mark the file ``name`` downloaded if ``cached``, or failed with the ``error``."""
		item = self._items.get(name)
		if item is None:
			return
		item.setIcon(self.CACHED_COLUMN, Icons.confirm() if cached else QIcon())
		item.setText(self.CACHED_COLUMN, self.tr('failed') if error else '')
		item.setForeground(self.CACHED_COLUMN, QColor(self.FAILED_COLOR) if error else QColor())
		item.setToolTip(self.CACHED_COLUMN, error or (self.tr('Downloaded') if cached else self.tr('Not downloaded')))
		if item is self.currentItem():
			# Whether the file is downloaded decides what can be done with it.
			self.currentItemChanged.emit(item, item)

	def setProgress(self, name, done):
		item = self._items.get(name)
		if item is None:
			return
		size = item.data(0, self.ENTRY_ROLE).get('size')
		item.setText(self.CACHED_COLUMN, f'{done * 100 // size} %' if size else self.tr('downloading...'))


class FirmwareDetails(QGroupBox):
	"""The details of the selected firmware file. The image itself is inspected once it is downloaded."""

	def __init__(self):
		super().__init__(self.tr('Firmware details'))
		self._version = self._value()
		self._build_date = self._value()
		self._repository = self._value()
		self._format = self._value()
		self._signature = self._value()
		form = QFormLayout()
		form.addRow(self.tr('Version:'), self._version)
		form.addRow(self.tr('Build date:'), self._build_date)
		form.addRow(self.tr('Source repository:'), self._repository)
		form.addRow(self.tr('Format:'), self._format)
		form.addRow(self.tr('Signature:'), self._signature)
		self.setLayout(form)
		self.showEntry(None, None)

	@staticmethod
	def _value():
		label = QLabel()
		label.setTextInteractionFlags(Qt.TextSelectableByMouse)
		return label

	def showEntry(self, entry, repository):
		"""Show the details of the listing ``entry`` of ``repository``, nothing if ``entry`` is ``None``."""
		for label in (self._version, self._build_date, self._repository, self._format, self._signature):
			label.setText('')
			label.setToolTip('')
		if entry is None:
			return
		name = entry['name']
		self._version.setText(firmware_version(name) or self.tr('unknown'))
		self._build_date.setText(firmware_build_date(name) or self.tr('unknown'))
		self._repository.setText(f'{repository.name} ({repository.url})')
		if not repository.isCurrent(entry):
			self._format.setText(self.tr('not downloaded'))
			self._signature.setText(self.tr('not downloaded'))
			return
		try:
			image_format, signature = inspect_image(repository.path(entry))
		except (OSError, ValueError) as e:
			self._format.setText(self.tr('unknown (%s)') % e)
			self._signature.setText(self.tr('unknown'))
			return
		self._format.setText(image_format)
		if signature is None:
			self._signature.setText(self.tr('not signed'))
		else:
			sig_type, value = signature
			self._signature.setText(f'{sig_type} {value.hex()[:32]}...')
			self._signature.setToolTip(value.hex())


class _ResetCheck(QObject):
	"""Finds out off the GUI thread whether the device can be reset with a writable ``/system/reset``
	node on the conf ``interface``. ``checked`` carries the answer."""

	checked = Signal(bool)

	def __init__(self, connection, interface):
		super().__init__()
		self._connection = connection
		self._interface = interface

	def start(self):
		threading.Thread(target=self._run, name='reset-check', daemon=True).start()

	def _run(self):
		try:
			# The conf service serves a single session, shared with the other users of the interface.
			nodes = conf.ConfClient(self._connection.sharedSide(self._interface))._children(RESET_PATH[:-1])
			node = next((n for n in nodes if n.get('n') == RESET_PATH[-1]), None)
			self.checked.emit(node is not None and node.get('t') == conf.CONF_B and
			                  bool(node.get('f', 0) & conf.CONF_WRITE))
		except Exception:
			self.checked.emit(False)


class UpdatePanel(QWidget):
	"""The repository selection with the Update button over the list of its firmware files, the details of
	the selected one and the buttons writing it to the update partition and resetting the device."""

	def __init__(self, settings, connection, interface, partition):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._settings = settings
		self._connection = connection
		# The flash interface and its update partition, see probe().
		self._interface = interface
		self._partition = partition
		# The conf interface of the same unit, which may reset the device.
		unit = ' '.join(interface.split()[:-1])
		self._conf_interface = f'{unit} {conf.PROTOCOL}'.strip()
		# The update running, None when there is none.
		self._update = None
		# The window asking for the credentials of the repository, None when it is not asking.
		self._prompt = None
		# The credentials entered for the repositories by their URLs, used along with the saved ones.
		self._credentials = {}
		# The URL of the repository the list shows.
		self._shown = None
		# The firmware write running, its progress window and side connection, None when there is none.
		self._operation = None
		self._progress = None
		self._side = None
		# Whether the device can be reset, None until it is known.
		self._can_reset = None
		self._reset_check = None

		self._repositories = QComboBox()
		self._repositories.setMinimumContentsLength(20)
		self._repositories.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
		self._repositories.currentIndexChanged.connect(self._repositoryChanged)
		self._update_btn = DauiButton(self.tr('Update'), Icons.refresh())
		self._update_btn.setToolTip(self.tr('Download the latest firmware files from the repository'))
		self._update_btn.clicked.connect(self.updateRepository)
		self._repositories.setFixedHeight(self._update_btn.sizeHint().height())

		self._tb = QToolBar()
		label = QLabel(self.tr('Repository:'))
		label.setContentsMargins(4, 0, 4, 0)
		self._tb.addWidget(label)
		self._tb.addWidget(self._repositories)
		self._tb.addSeparator()
		self._tb.addWidget(self._update_btn)

		self._hint = Admonition(self.tr('No plumCore repository is configured. Add one in Settings, '
		                                'section plumCore repositories.'), Admonition.NOTE)

		self._list = FirmwareList()
		self._list.currentItemChanged.connect(self._selectionChanged)
		self._details = FirmwareDetails()

		self._write = DauiButton(self.tr('Write firmware update'), Icons.write())
		self._write.setToolTip(self.tr('Write the selected firmware to the flash partition %s') % partition)
		self._write.clicked.connect(self.writeFirmware)
		self._reset = DauiButton(self.tr('Reset && Update'), Icons.reset())
		self._reset.clicked.connect(self.reset)
		self._bottom = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._bottom.addWidget(spacer)
		self._bottom.addWidget(self._write)
		self._bottom.addWidget(self._reset)

		# Keep the hint and the details off the toolbars, the list and the window borders.
		hint_layout = QVBoxLayout()
		hint_layout.setContentsMargins(4, 4, 4, 4)
		hint_layout.addWidget(self._hint)
		details_layout = QVBoxLayout()
		details_layout.setContentsMargins(4, 4, 4, 4)
		details_layout.addWidget(self._details)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.addWidget(self._tb)
		layout.addLayout(hint_layout)
		layout.addWidget(self._list, stretch=1)
		layout.addLayout(details_layout)
		layout.addWidget(self._bottom)
		self.setLayout(layout)

		self.loadRepositories()
		self._checkReset()

	def loadRepositories(self):
		"""Fill the repository selection from the settings, keeping the selected one if it is still there."""
		current = self._repositories.currentData()
		self._repositories.blockSignals(True)
		self._repositories.clear()
		for repository in RepositoriesSection.repositories(self._settings):
			self._repositories.addItem(repository.get('name', ''), repository.get('url', ''))
		index = self._repositories.findData(current)
		self._repositories.setCurrentIndex(max(index, 0))
		self._repositories.blockSignals(False)
		self._hint.setVisible(self._repositories.count() == 0)
		if self._repositories.currentData() != self._shown:
			self._repositoryChanged()
		else:
			self._updateButtons()

	def _repository(self):
		url = self._repositories.currentData()
		if not url:
			return None
		auth = self._credentials.get(url) or RepositoriesSection.auth(self._settings, url)
		return Repository(self._repositories.currentText(), url, auth)

	@Slot()
	def _repositoryChanged(self):
		"""Show the files of the selected repository as they were listed the last time."""
		self._updateButtons()
		if self._update is not None:
			return
		repository = self._repository()
		self._shown = repository.url if repository is not None else None
		if repository is None:
			self._list.clear()
			return
		entries = repository.cachedEntries()
		if entries:
			self._list.setEntries(entries, repository)
		else:
			self._list.showMessage(self.tr('Not updated yet'))

	@Slot()
	def _selectionChanged(self):
		self._details.showEntry(self._list.selectedEntry(), self._repository())
		self._updateButtons()

	def _selectedFirmware(self):
		"""The listing entry of the selected firmware file if it is downloaded, else ``None``."""
		entry = self._list.selectedEntry()
		repository = self._repository()
		return entry if entry is not None and repository is not None and repository.isCurrent(entry) else None

	def _updateButtons(self):
		idle = self._update is None and self._prompt is None
		self._repositories.setEnabled(idle and self._operation is None and self._repositories.count() > 0)
		self._update_btn.setEnabled(idle and self._operation is None and self._repository() is not None)
		self._update_btn.setText(self.tr('Update') if self._update is None else self.tr('Updating...'))
		self._write.setEnabled(idle and self._operation is None and self._selectedFirmware() is not None)
		self._reset.setEnabled(self._operation is None and bool(self._can_reset))
		if self._can_reset is None:
			self._reset.setToolTip(self.tr('Checking whether the device can be reset...'))
		elif self._can_reset:
			self._reset.setToolTip(self.tr('Reset the device, the bootloader installs the firmware written to '
			                               'the flash partition %s') % self._partition)
		else:
			self._reset.setToolTip(self.tr('The device cannot be reset, interface "%s" has no writable %s') % (
				self._conf_interface, '/' + '/'.join(RESET_PATH)))

	@Slot()
	def updateRepository(self):
		"""List the selected repository and download its latest firmware files."""
		repository = self._repository()
		if repository is None or self._update is not None or self._prompt is not None:
			return
		latest = RepositoriesSection.latest(self._settings)
		self._logger.info(f'updating repository {repository.name!r} ({repository.url}), latest {latest} files')
		self._update = _RepositoryUpdate(repository, latest)
		self._update.listed.connect(lambda entries: self._list.setEntries(entries, repository))
		self._update.progress.connect(self._list.setProgress)
		self._update.downloaded.connect(self._downloaded)
		self._update.done.connect(self._done)
		self._update.failed.connect(self._failed)
		self._update.authenticationRequired.connect(self._askCredentials)
		self._update.start()
		self._updateButtons()

	@Slot(str, object)
	def _downloaded(self, name, error):
		if error is None:
			self._logger.info(f'downloaded {name}')
		else:
			self._logger.warning(f'downloading {name} failed: {error}')
		self._list.setCached(name, error is None, error)

	@Slot()
	def _done(self):
		self._update = None
		self._updateButtons()

	@Slot(str)
	def _failed(self, message):
		self._logger.warning(f'updating the repository failed: {message}')
		self._update = None
		self._updateButtons()
		self._repositoryChanged()
		QMessageBox.warning(self, self.tr('Update repository'), self.tr('Updating the repository failed: %s') % message)

	@Slot(object)
	def _askCredentials(self, required):
		"""Ask for the username and password of the repository, which ``required`` them, then update it
		again with them."""
		self._update = None
		repository = self._repository()
		self._logger.info(f'repository {repository.name!r} requires authentication'
		                  f'{", the credentials were not accepted" if required.rejected else ""}')
		where = f'<b>{repository.name}</b> ({repository.url})'
		if required.rejected:
			text = self.tr('The repository %s did not accept the username and password.') % where
		else:
			text = self.tr('The repository %s requires a username and password.') % where
		if required.realm:
			text += '<br><br>' + self.tr('Realm: %s') % required.realm
		self._prompt = CredentialsWindow(self.tr('Repository authentication'), text,
		                                 username=repository.auth[0] if repository.auth else '')
		url = repository.url
		self._prompt.accepted.connect(lambda username, password, save: self._credentialsEntered(url, username, password, save))
		self._prompt.rejected.connect(self._credentialsRejected)
		self._showWindow(self._prompt)
		self._updateButtons()

	def _credentialsEntered(self, url, username, password, save):
		self._prompt = None
		self._credentials[url] = (username, password)
		if save:
			RepositoriesSection.saveAuth(self._settings, url, username, password)
		self.updateRepository()

	@Slot()
	def _credentialsRejected(self):
		self._prompt = None
		self._updateButtons()
		self._repositoryChanged()

	def _checkReset(self):
		if not any(name == self._conf_interface and desc.get('p') == conf.PROTOCOL
		           for name, desc in self._connection.interfaces):
			self._resetChecked(False)
			return
		self._reset_check = _ResetCheck(self._connection, self._conf_interface)
		self._reset_check.checked.connect(self._resetChecked)
		self._reset_check.start()
		self._updateButtons()

	@Slot(bool)
	def _resetChecked(self, can_reset):
		self._reset_check = None
		self._can_reset = can_reset
		self._logger.info(f'device reset {"" if can_reset else "not "}available on interface {self._conf_interface!r}')
		self._updateButtons()

	@Slot()
	def writeFirmware(self):
		"""Write the selected firmware file to the update partition after asking, showing the progress in a
		window. The file is written as it is, the bootloader decompresses an xz compressed image itself."""
		entry = self._selectedFirmware()
		if entry is None or self._operation is not None:
			return
		path = self._repository().path(entry)
		name = entry['name']
		try:
			data = path.read_bytes()
		except OSError as e:
			QMessageBox.warning(self, self.tr('Write firmware update'), self.tr('Cannot read %s: %s') % (path, e))
			return
		answer = QMessageBox.question(
			self, self.tr('Write firmware update'),
			self.tr('Write the firmware %s (version %s, %s) to the flash partition %s? The whole partition is '
			        'erased first.') % (name, firmware_version(name), format_size(len(data)), self._partition),
			QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
		if answer != QMessageBox.Yes:
			return

		try:
			self._side = self._connection.connectSide(self._interface, flash_service.MAIN_EP)
		except Exception as e:
			QMessageBox.warning(self, self.tr('Write firmware update'), self.tr('Cannot open %s: %s') % (self._interface, e))
			return
		client = FlashClient(self._side, interface_mtu(self._connection, self._interface))
		partition = self._partition

		def run(progress, abort, stage):
			size, erase_size, page_size = client.info(partition)
			client.write(partition, size, erase_size, page_size, data, progress, abort, stage)

		stages = {
			'erase': self.tr('Erasing flash partition %s before writing...') % partition,
			'write': self.tr('Writing %s (%s) to flash partition %s...') % (name, format_size(len(data)), partition),
			'verify': self.tr('Verifying %s written to flash partition %s...') % (name, partition),
		}
		self._logger.info(f'writing {name} to flash partition {partition!r} on interface {self._interface!r}')
		self._progress = ProgressWindow(self.tr('Write firmware update'), stages['erase'])
		self._progress.aborted.connect(self._abortOperation)
		self._showWindow(self._progress)

		def stage(what, total):
			if self._progress is not None:
				self._progress.setText(stages.get(what, ''))
				self._progress.setStep(list(stages).index(what) + 1, len(stages))
				self._progress.setFormat(ByteFormat(total))
				self._progress.setProgress(0, total)

		def progress(done):
			if self._progress is not None:
				self._progress.setProgress(done)

		def ended(error, aborted=False):
			self._operation = None
			self._progress.finish()
			self._progress = None
			self._side.disconnectUri()
			self._side = None
			self._updateButtons()
			if aborted:
				self._logger.info(f'writing {name} aborted')
			elif error is not None:
				self._logger.warning(f'writing {name} failed: {error}')
				QMessageBox.warning(self, self.tr('Write firmware update'),
				                    self.tr('Writing the firmware %s failed: %s') % (name, error))
			else:
				self._logger.info(f'{name} written to flash partition {partition!r}')
				QMessageBox.information(self, self.tr('Write firmware update'),
				                        self.tr('The firmware %s is written. Reset the device to install it.') % name)

		self._operation = _Operation(run)
		self._operation.stage.connect(stage)
		self._operation.progress.connect(progress)
		self._operation.done.connect(lambda: ended(None))
		self._operation.aborted.connect(lambda: ended(None, aborted=True))
		self._operation.failed.connect(lambda message: ended(message))
		self._operation.start()
		self._updateButtons()

	def _showWindow(self, window):
		"""Show the progress or credentials window in the MDI area the applet window is in, centered over it."""
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

	@Slot()
	def reset(self):
		"""Reset the device by writing true to ``/system/reset`` and disconnect right away, the device does
		not answer while it resets."""
		if not self._can_reset or self._operation is not None:
			return
		self._logger.info(f'resetting the device over interface {self._conf_interface!r}')
		try:
			side = self._connection.sharedSide(self._conf_interface)
			with side.lock:
				side.send(cbor2.dumps({'c': 'write', 'p': RESET_PATH, 't': conf.CONF_B, 'val': True}))
		except Exception as e:
			QMessageBox.warning(self, self.tr('Reset & Update'), self.tr('Resetting the device failed: %s') % e)
			return
		self._connection.disconnectUri()

	def abort(self):
		"""Report nothing more of the update or write running, e.g. when the window closes. A download
		finishes in the background, a write is aborted."""
		if self._update is not None:
			self._update.blockSignals(True)
			self._update = None
		if self._prompt is not None:
			self._prompt.blockSignals(True)
			self._prompt.close()
			self._prompt = None
		if self._reset_check is not None:
			self._reset_check.blockSignals(True)
			self._reset_check = None
		if self._operation is not None:
			self._operation.blockSignals(True)
			self._operation.abort()
			self._operation = None
			self._progress.finish()
			self._progress = None
			# Already disconnected together with the device connection, if that is why it closes.
			self._side.disconnectUri()
			self._side = None


class UpdateWindow(AppletWindow):
	"""The plumCore updates of a device with an ``update`` partition on the flash ``interface``. The window
	closes when the device is disconnected."""

	def __init__(self, connection, interface, settings, context):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self._logger.info(f'opening plumCore updates, partition {context!r} on interface {interface!r}')
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(self.tr('plumCore updates (%s)') % interface)
		self.setWindowIcon(Icons.update())
		# The context is the name of the update partition, see probe().
		self._panel = UpdatePanel(settings, connection, interface, context)
		self.setWidget(self._panel)
		self.resize(800, 600)
		# The repositories may have been changed in the settings meanwhile.
		self.aboutToActivate.connect(self._panel.loadRepositories)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._panel.abort()
		super().closeEvent(event)
