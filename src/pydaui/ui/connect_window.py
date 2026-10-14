# SPDX-License-Identifier: GPL-3.0-or-later
#
# Connect window selecting the target connection
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import html
import importlib.util
import threading
from pathlib import Path
from urllib.parse import urlencode

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse

from pynbus2.uri import parse_uri, split_serial_path

from pydaui import bluetooth, products
from pydaui.connection import *
from pydaui.ui.helpers import Admonition, AppletWindow, DauiButton, WrappedLabel
from pydaui.ui.icons import Icons
from pydaui.ui.settings_window import SshSessionsPage


def describeUri(uri):
	"""The device name a connection URI carries with its transport, or the URI itself."""
	parsed = parse_uri(uri)
	name = parsed.get('name')
	return '%s (%s)' % (name, parsed.scheme) if name else uri


class _ShrinkingComboBox(QComboBox):
	"""A combo box narrower than its longest item if it has to, the items are elided then."""

	def __init__(self):
		super().__init__()
		self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
		self.setMinimumContentsLength(8)


class ConnectWindowTab(QWidget):
	"""Base class for a tab in the connect window.

	Each subclass describes one way of connecting to a target. Its :attr:`NAME` and :attr:`ICON`
	label the tab, :attr:`ENABLED` decides whether it can be selected, and :meth:`uri` returns the
	connection URI the current selection points at (or ``None`` when nothing is selectable yet).
	:class:`ConnectWindow` discovers the tabs by enumerating the subclasses of this class.
	"""

	#: Tab title shown in the tab bar.
	NAME = None

	#: Fonticon name used for the tab icon.
	ICON = None

	#: Colour of the tab icon.
	ICON_COLOR = '#444'

	#: Whether the tab is selectable.
	ENABLED = True

	def __init__(self, connection, settings, parent=None):
		super().__init__(parent=parent)
		self._connection = connection
		self._settings = settings

	@classmethod
	def tabIcon(cls):
		return icon(cls.ICON, color=cls.ICON_COLOR, scale_factor=1)

	def uri(self):
		"""The connection URI selected in this tab, or ``None`` when there is nothing to connect to."""
		return None


class SwdTab(ConnectWindowTab):

	NAME = 'SWD'
	ICON = 'mdi7.bug-outline'
	ENABLED = True

	def __init__(self, connection, settings, parent=None):
		super().__init__(connection, settings, parent=parent)
		# Keep worker references alive until their signal has been delivered.
		self._workers = set()
		self._createUi()
		self._refresh()

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		l.addWidget(WrappedLabel(
			'SWD is a debug interface and protocol used to access ARM Cortex-M '
			'microprocessor cores and perform debug actions or program memories.\n\n'
			'It is possible to use SWD access to recover a broken device bootloader.\n\n'
			'Select debug probe to access the device:'
		))

		# Create a combo box to select debug probe to access the target
		cb = _ShrinkingComboBox()
		cb.setIconSize(QSize(24, 24))
		l.addWidget(cb)
		self._probe_cb = cb
		self._probe_status = WrappedLabel()
		l.addWidget(self._probe_status)

		l.addSpacing(16)

		l.addWidget(WrappedLabel(
			'Select the device/product you are connecting to. It must be known beforehand:'
		))

		# Add list of devices/products
		dev = _ShrinkingComboBox()
		dev.setIconSize(QSize(24, 24))
		# SWD does not identify the product, it must be known.
		for p in products.load():
			dev.addItem(f'{p.pn} ({p.version})', p)
		for i in range(dev.count()):
			dev.setItemIcon(i, icon('mdi7.memory', color='#888', scale_factor=1))
		l.addWidget(dev)
		self._product_cb = dev

		l.addWidget(Admonition(
			'Beware, here be dragons. Selecting the wrong device may damage it.',
			Admonition.WARN
		))
		# The spare room when the window is higher than the tab, e.g. maximized.
		l.addStretch(1)

	def _refresh(self):
		# Probing loads pyocd, which takes a while.
		self._probe_status.setText('Looking for debug probes...')
		worker = _ProbeWorker(self._connection, 'swd://', None)
		worker.done.connect(self._onProbeDone)
		worker.failed.connect(self._onProbeFailed)
		self._workers.add(worker)
		worker.start()

	@Slot(object, list)
	def _onProbeDone(self, item, results):
		self._workers.discard(self.sender())
		self._probe_cb.clear()
		for probe_uri in results:
			self._probe_cb.addItem(icon('mdi7.bug-outline', color='#008', scale_factor=1), probe_uri, probe_uri)
		self._probe_status.setText('' if results else 'No debug probes found.')

	@Slot(object, str)
	def _onProbeFailed(self, item, message):
		self._workers.discard(self.sender())
		self._probe_cb.clear()
		self._probe_status.setText('Looking for debug probes failed: %s' % message)

	def uri(self):
		"""The probe URI with the selected product's target and pack, reaching its virtual flash service. The
		product selects the flash partitions it is split into, see :mod:`pydaui.connection.swd_partitions`."""
		probe_uri = self._probe_cb.currentData()
		product = self._product_cb.currentData()
		if probe_uri is None or product is None:
			return None
		pack = Path(__file__).resolve().parent.parent / product.pack
		return '%s?%s' % (probe_uri, urlencode({'target': product.mcu, 'pack': str(pack), 'product': product.pn,
		                                        'version': product.version}))


class UsbTab(ConnectWindowTab):

	NAME = 'USB'
	ICON = 'mdi7.usb-port'
	ENABLED = False


class _ProbeWorker(QObject):
	"""Runs :meth:`Connection.probe` for one URI off the GUI thread and reports the result via a signal.

	The blocking probe (e.g. opening a serial port and waiting for advertisements) must not run on the GUI
	thread, so it runs in a daemon thread; the ``done``/``failed`` signals are delivered back on the GUI
	thread. The originating tree item is carried through untouched so the slot knows where to append.
	"""

	done = Signal(object, list)    # (source item, list of result URI strings)
	failed = Signal(object, str)   # (source item, error message)

	def __init__(self, connection, uri, item):
		super().__init__()
		self._connection = connection
		self._uri = uri
		self._item = item

	def start(self):
		threading.Thread(target=self._run, name='probe', daemon=True).start()

	def _run(self):
		try:
			results = self._connection.probe(self._uri)
		except Exception as e:
			self.failed.emit(self._item, str(e))
			return
		self.done.emit(self._item, results)


class SerialTab(ConnectWindowTab):

	NAME = 'Serial'
	ICON = 'mdi7.serial-port'
	ENABLED = True

	#: The serial transport this tab discovers over.
	SCHEME = 'dgtext+serial'

	#: Item roles: the URI a row points at, and whether that URI is complete enough to connect to.
	URI_ROLE = Qt.UserRole
	CONNECTABLE_ROLE = Qt.UserRole + 1

	#: Tree column holding the per-row refresh button.
	BUTTON_COLUMN = 3

	def __init__(self, connection, settings, parent=None):
		super().__init__(connection, settings, parent=parent)
		# Keep worker references alive until their signal has been delivered.
		self._workers = set()
		self._createUi()
		self._rescanPorts()

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		l.addWidget(WrappedLabel(
			'Connect over a serial diagnostic console. Pick a port and press Refresh '
			'to discover the services advertised on the bus, then select one to connect.'
		))

		self._tree = QTreeWidget()
		self._tree.setColumnCount(4)
		self._tree.setHeaderLabels(['Port / service', 'Protocol', 'Version', ''])
		self._tree.setIconSize(QSize(20, 20))
		header = self._tree.header()
		header.setStretchLastSection(False)
		header.setSectionResizeMode(0, QHeaderView.Stretch)
		header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
		header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
		header.setSectionResizeMode(self.BUTTON_COLUMN, QHeaderView.ResizeToContents)
		l.addWidget(self._tree)

		rescan = QPushButton(icon('mdi7.refresh', color='#888', scale_factor=1), 'Rescan serial ports')
		rescan.clicked.connect(self._rescanPorts)
		l.addWidget(rescan, alignment=Qt.AlignLeft)

	def _rescanPorts(self):
		self._tree.clear()
		for port_uri in self._connection.probe('%s://' % self.SCHEME):
			self._addItem(None, port_uri, 'mdi7.serial-port', connectable=False)

	@staticmethod
	def _describe(uri):
		"""Split a discovered URI into the (name, protocol, version) columns shown for its row.

		A port URI shows the device path with no protocol; a service URI shows its ``<sid>/<ep>`` and
		the protocol name/version carried in the query.
		"""
		parsed = parse_uri(uri)
		device, dst = split_serial_path(parsed)
		name = '%s / %s' % (dst[0], dst[1]) if dst is not None else device
		return name, parsed.get('p') or '', parsed.get('pv') or ''

	def _addItem(self, parent, uri, icon_name, connectable):
		"""Add one row for ``uri`` under ``parent`` (or at top level), with its own refresh button."""
		name, protocol, version = self._describe(uri)
		item = QTreeWidgetItem([name, protocol, version])
		item.setData(0, self.URI_ROLE, uri)
		item.setData(0, self.CONNECTABLE_ROLE, connectable)
		item.setIcon(0, icon(icon_name, color='#448', scale_factor=1))
		item.setToolTip(0, uri)
		if parent is None:
			self._tree.addTopLevelItem(item)
		else:
			parent.addChild(item)

		btn = QToolButton()
		btn.setAutoRaise(True)
		btn.setToolTip('Probe this for services')
		btn.setIcon(icon('mdi7.refresh', color='#888', scale_factor=1))
		btn.clicked.connect(lambda checked=False, it=item, u=uri: self._startProbe(it, u))
		self._tree.setItemWidget(item, self.BUTTON_COLUMN, btn)
		return item

	def _startProbe(self, item, uri):
		btn = self._tree.itemWidget(item, self.BUTTON_COLUMN)
		if btn is not None:
			btn.setEnabled(False)
			btn.setIcon(icon('mdi7.loading', color='#888', animation=spin(btn), scale_factor=1))

		worker = _ProbeWorker(self._connection, uri, item)
		worker.done.connect(self._onProbeDone)
		worker.failed.connect(self._onProbeFailed)
		self._workers.add(worker)
		worker.start()

	@Slot(object, list)
	def _onProbeDone(self, item, results):
		self._finishProbe(item)
		try:
			item.takeChildren()
			for result_uri in results:
				self._addItem(item, result_uri, 'mdi7.access-point', connectable=True)
			item.setExpanded(True)
		except RuntimeError:
			# The row was removed (e.g. the port list was rescanned) while the probe ran.
			pass

	@Slot(object, str)
	def _onProbeFailed(self, item, message):
		self._finishProbe(item)
		try:
			item.takeChildren()
			child = QTreeWidgetItem(['probe failed: %s' % message])
			child.setIcon(0, icon('mdi7.alert-circle-outline', color='#800', scale_factor=1))
			item.addChild(child)
			item.setExpanded(True)
		except RuntimeError:
			pass

	def _finishProbe(self, item):
		try:
			btn = self._tree.itemWidget(item, self.BUTTON_COLUMN)
		except RuntimeError:
			btn = None
		if btn is not None:
			btn.setEnabled(True)
			btn.setIcon(icon('mdi7.refresh', color='#888', scale_factor=1))
		self._workers.discard(self.sender())

	def uri(self):
		item = self._tree.currentItem()
		if item is None or not item.data(0, self.CONNECTABLE_ROLE):
			return None
		return item.data(0, self.URI_ROLE)


class BleTab(ConnectWindowTab):
	"""Connect to a BLE device over the rible transport (RemoteInterfaces over BLE).

	The device URI carries no interface, so connecting walks the RemoteInterfaces the device exports
	(they are logged) and opens a session on the root interface, if the device has one.
	"""

	NAME = 'BLE'
	ICON = 'mdi7.bluetooth'
	ENABLED = True

	#: The BLE transport this tab connects over.
	SCHEME = 'rible'

	def __init__(self, connection, settings, parent=None):
		super().__init__(connection, settings, parent=parent)
		# Keep worker references alive until their signal has been delivered.
		self._workers = set()
		self._createUi()
		self._refresh()

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		if bluetooth.scans():
			howto = ('Nearby devices are listed, the device is paired when connecting '
			         'to it the first time.')
		else:
			howto = ('Only bonded devices are listed, pair and trust the device first '
			         '(e.g. with bluetoothctl).')
		l.addWidget(WrappedLabel('Connect to a device over Bluetooth Low Energy. %s\n\n'
		                   'Select the device to connect to:' % howto))

		# A list, not a combo box: its popup cannot be touched on Android, the touches miss its items.
		row = QHBoxLayout()
		devices = QListWidget()
		devices.setIconSize(QSize(24, 24))
		row.addWidget(devices, 1)
		self._devices = devices

		btn = QToolButton()
		btn.setAutoRaise(True)
		btn.setToolTip('Refresh the devices')
		btn.setIcon(icon('mdi7.refresh', color='#888', scale_factor=1))
		btn.clicked.connect(self._refresh)
		row.addWidget(btn, alignment=Qt.AlignTop)
		self._refresh_btn = btn
		l.addLayout(row, 1)

		self._status = WrappedLabel()
		l.addWidget(self._status)

	@staticmethod
	def _describe(uri):
		"""The list label for a device URI: its name (if it has one) and address."""
		parsed = parse_uri(uri)
		name = parsed.get('name')
		return '%s [%s]' % (name, parsed.host) if name else parsed.host

	@Slot()
	def _refresh(self):
		self._refresh_btn.setEnabled(False)
		bluetooth.withPermission(self, self._probe, self._permissionDenied)

	def _permissionDenied(self):
		self._refresh_btn.setEnabled(True)
		self._devices.clear()
		self._status.setText(bluetooth.DENIED)

	def _probe(self):
		self._status.setText('Looking for devices...' if bluetooth.scans() else 'Looking for bonded devices...')
		worker = _ProbeWorker(self._connection, '%s://' % self.SCHEME, None)
		worker.done.connect(self._onProbeDone)
		worker.failed.connect(self._onProbeFailed)
		self._workers.add(worker)
		worker.start()

	@Slot(object, list)
	def _onProbeDone(self, item, results):
		self._finishProbe()
		self._devices.clear()
		for uri in results:
			item = QListWidgetItem(icon('mdi7.bluetooth', color='#008', scale_factor=1), self._describe(uri))
			item.setData(Qt.UserRole, uri)
			self._devices.addItem(item)
		self._devices.setCurrentRow(0)
		self._status.setText('' if results else 'No devices found.' if bluetooth.scans() else 'No bonded devices found.')

	@Slot(object, str)
	def _onProbeFailed(self, item, message):
		self._finishProbe()
		self._devices.clear()
		self._status.setText('Listing the devices failed: %s' % message)

	def _finishProbe(self):
		self._refresh_btn.setEnabled(True)
		self._workers.discard(self.sender())

	def uri(self):
		item = self._devices.currentItem()
		return item.data(Qt.UserRole) if item is not None else None


class SshTab(ConnectWindowTab):
	"""Connect to a device through the pynbus2-ssh-proxy of a DAQ controller over the ssh transport.

	SSH hosts cannot be discovered, the sessions are predefined in the settings, see :class:`SshSessionsPage`.
	The devices are those the proxy probes on the controller.
	"""

	NAME = 'SSH'
	ICON = 'mdi7.console-network'
	# Not on Android, which has no paramiko.
	ENABLED = importlib.util.find_spec('paramiko') is not None

	def __init__(self, connection, settings, parent=None):
		super().__init__(connection, settings, parent=parent)
		# Keep worker references alive until their signal has been delivered.
		self._workers = set()
		self._createUi()

	@classmethod
	def sessions(cls, settings):
		"""The (name, uri) of the SSH sessions in the ``settings``."""
		return [(s.get('name') or s['uri'], s['uri']) for s in SshSessionsPage.sessions(settings)]

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		l.addWidget(WrappedLabel(
			'Connect to a device attached to a DAQ controller, through the pynbus2 SSH proxy running on it. '
			'The SSH sessions are configured in Settings, Device connection. The keys of the SSH agent and '
			'those in ~/.ssh are used, a password is asked for when needed.\n\n'
			'Select the SSH session:'
		))

		cb = _ShrinkingComboBox()
		cb.setIconSize(QSize(24, 24))
		for name, uri in self.sessions(self._settings):
			cb.addItem(icon('mdi7.server-network', color='#448', scale_factor=1), name, uri)
			cb.setItemData(cb.count() - 1, uri, Qt.ToolTipRole)
		# None selected at first: listing the devices of a session connects to it and may ask for a password,
		# only once the user picks one, not when the window is created (hidden when connecting on startup).
		cb.setPlaceholderText('Select a session' if cb.count() else 'No SSH sessions configured')
		cb.setCurrentIndex(-1)
		cb.currentIndexChanged.connect(self._refresh)
		l.addWidget(cb)
		self._session_cb = cb

		l.addSpacing(16)

		row = QHBoxLayout()
		row.addWidget(WrappedLabel('Select the device to connect to:'), 1)
		btn = QToolButton()
		btn.setAutoRaise(True)
		btn.setToolTip('Refresh the devices')
		btn.setIcon(icon('mdi7.refresh', color='#888', scale_factor=1))
		btn.clicked.connect(self._refresh)
		row.addWidget(btn, alignment=Qt.AlignBottom)
		self._refresh_btn = btn
		l.addLayout(row)

		devices = QTreeWidget()
		devices.setColumnCount(2)
		devices.setHeaderLabels(['Name', 'URI'])
		devices.setRootIsDecorated(False)
		devices.setIconSize(QSize(20, 20))
		devices.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
		l.addWidget(devices, 1)
		self._devices = devices

		self._status = WrappedLabel()
		l.addWidget(self._status)
		if not cb.count():
			self._status.setText('No SSH sessions configured, add them in Settings, Device connection.')

	@staticmethod
	def _describe(uri):
		"""The name of a device URI: the name it carries, or the remote URI it proxies to."""
		parsed = parse_uri(uri)
		return parsed.get('name') or parsed.path.lstrip('/') or parsed.host

	@Slot()
	def _refresh(self):
		self._devices.clear()
		session = self._session_cb.currentData()
		if session is None:
			return
		self._refresh_btn.setEnabled(False)
		self._refresh_btn.setIcon(icon('mdi7.loading', color='#888', animation=spin(self._refresh_btn),
		                               scale_factor=1))
		self._status.setText('Connecting to %s...' % self._session_cb.currentText())
		# The session goes along, the results of a session no longer selected are dropped.
		worker = _ProbeWorker(self._connection, session, session)
		worker.done.connect(self._onProbeDone)
		worker.failed.connect(self._onProbeFailed)
		self._workers.add(worker)
		worker.start()

	@Slot(object, list)
	def _onProbeDone(self, session, results):
		if not self._finishProbe(session):
			return
		for uri in results:
			item = QTreeWidgetItem([self._describe(uri), uri])
			item.setData(0, Qt.UserRole, uri)
			item.setIcon(0, icon('mdi7.access-point', color='#448', scale_factor=1))
			item.setToolTip(1, uri)
			self._devices.addTopLevelItem(item)
		if results:
			self._devices.setCurrentItem(self._devices.topLevelItem(0))
		self._status.setText('' if results else 'No devices found.')

	@Slot(object, str)
	def _onProbeFailed(self, session, message):
		if self._finishProbe(session):
			self._status.setText('Listing the devices failed: %s' % message)

	def _finishProbe(self, session):
		"""Done probing ``session``, whether it is still the one selected."""
		self._workers.discard(self.sender())
		if session != self._session_cb.currentData():
			return False
		self._refresh_btn.setEnabled(True)
		self._refresh_btn.setIcon(icon('mdi7.refresh', color='#888', scale_factor=1))
		return True

	def uri(self):
		item = self._devices.currentItem()
		return item.data(0, Qt.UserRole) if item is not None else None


class _TabScrollArea(QScrollArea):
	"""Scrolls a connect window tab when the window is lower than the tab, e.g. on a small screen. Never
	horizontally, the tab wraps its texts to the width of the window.

	A plain QScrollArea caps its size hint, this one asks for the whole tab so the window keeps its natural
	size wherever it fits.
	"""

	def __init__(self, tab):
		super().__init__()
		self.setWidget(tab)
		self.setWidgetResizable(True)
		self.setFrameShape(QFrame.NoFrame)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

	def sizeHint(self):
		return self.widget().sizeHint() + QSize(2 * self.frameWidth(), 2 * self.frameWidth())


class ConnectWindow(AppletWindow):
	"""Selects the target connection, on top of the applets in the MDI area while the device is not connected."""

	LAYER = AppletWindow.CONNECT

	def __init__(self, connection, settings, parent=None):
		super().__init__(parent=parent)
		self._connection = connection
		self._settings = settings
		# The MDI area the window is shown in, it is taken out of it while hidden, see _dismiss().
		self._area = None
		# Only the title, the window shows until connected and Exit quits the application.
		self.setWindowFlags(Qt.SubWindow | Qt.CustomizeWindowHint | Qt.WindowTitleHint)

		self._check_timer = QTimer(self)
		self._check_timer.setInterval(500)
		self._check_timer.timeout.connect(self._checkTimerTimeout)
		self._check_timer.start()

		self._createUi()

		self._connection.connectionChanged.connect(self._connectionChanged)
		self._connection.connectFailed.connect(self._connectFailed)

	def _createUi(self):
		self.setWindowTitle(self.tr('Connect'))
		self.setWindowIcon(Icons.device())

		# Why the last connection attempt failed, hidden until one fails.
		self._error = Admonition('', Admonition.WARN)
		self._error.hide()

		self._tabs = QTabWidget()
		self._tabs.setTabPosition(QTabWidget.North)

		# One tab per ConnectWindowTab subclass; each carries its own name, icon and enabled state.
		for i, tab_cls in enumerate(ConnectWindowTab.__subclasses__()):
			tab = tab_cls(self._connection, self._settings)
			self._tabs.addTab(_TabScrollArea(tab), tab_cls.NAME)
			self._tabs.setTabIcon(i, tab_cls.tabIcon())
			self._tabs.setTabEnabled(i, tab_cls.ENABLED)

		self._exit_btn = DauiButton(self.tr('&Exit'), Icons.quit())
		self._exit_btn.clicked.connect(self._exitClicked)

		self._connect_btn = DauiButton(self.tr('&Connect'), icon('mdi7.link', color='#080', scale_factor=1))
		self._connect_btn.clicked.connect(self._connectClicked)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._exit_btn)
		self._tb.addWidget(self._connect_btn)

		content = QVBoxLayout()
		content.setContentsMargins(9, 9, 9, 9)
		content.addWidget(self._error)
		content.addWidget(self._tabs, stretch=1)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(content, stretch=1)
		layout.addWidget(self._tb)

		widget = QWidget()
		widget.setLayout(layout)
		self.setWidget(widget)

	@Slot()
	def _exitClicked(self):
		QApplication.quit()

	def showError(self, text):
		"""Show ``text`` as the reason the last connection attempt failed."""
		self._error.setText(text)
		self._error.show()

	@Slot()
	def _connectClicked(self):
		uri = self._tabs.currentWidget().widget().uri()
		if not uri:
			return
		self._error.hide()
		# The connecting window shows instead, this one opens again when connecting fails or is cancelled.
		area = self.mdiArea()
		self._dismiss()
		ConnectingWindow.start(self._connection, uri, area)

	def _dismiss(self):
		"""Hide the window until it is shown again with :meth:`showCentered`. Not closed, that closes its
		content too, which stays hidden when the window shows again. Taken out of the MDI area, which shows
		a hidden maximized window again when activating another one (QMdiArea restores the maximized
		windows, showing them)."""
		self._area = self.mdiArea() or self._area
		self.hide()
		if self._area is not None:
			self._area.removeSubWindow(self)

	def showCentered(self):
		if self.mdiArea() is None and self._area is not None:
			self._area.addSubWindow(self)
		super().showCentered()

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri and self.isVisible():
			self._dismiss()

	@Slot(str, str)
	def _connectFailed(self, uri, message):
		self.showError('Connecting to %s failed: %s' % (describeUri(uri), message))
		# Right away when the connection made on startup failed, not on the next check.
		if not self.isVisible():
			self.showCentered()

	@Slot()
	def _checkTimerTimeout(self):
		# Not while connecting, the connection is either made or this window shows why it failed.
		if not self._connection.connected and not self._connection.connecting and not self.isVisible():
			self.showCentered()


class ConnectingWindow(AppletWindow):
	"""A spinner and the device being connected, on top of the applets in the MDI area, with a Cancel button. Closes
	when the connection is made, fails or is cancelled."""

	LAYER = AppletWindow.CONNECT
	#: Small, kept in sight above the others.
	MAXIMIZE = False
	ON_TOP = True

	def __init__(self, connection, uri, parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		# Only the title, Cancel closes the window.
		self.setWindowFlags(Qt.SubWindow | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
		self.setWindowTitle(self.tr('Connecting'))
		self.setWindowIcon(Icons.device())
		self._connection = connection
		self._connection.connectionChanged.connect(self._done)
		self._connection.connectFailed.connect(self._done)
		# Whether the connection was made or failed, closing the window cancels it otherwise.
		self._finished = False

		spinner = QToolButton()
		spinner.setAutoRaise(True)
		spinner.setFocusPolicy(Qt.NoFocus)
		spinner.setAttribute(Qt.WA_TransparentForMouseEvents)
		spinner.setIconSize(QSize(32, 32))
		spinner.setIcon(icon('mdi7.loading', color='#44c', animation=spin(spinner)))

		label = QLabel(self.tr('Connecting to<br/><b>%s</b>...') % html.escape(describeUri(uri)))

		row = QHBoxLayout()
		row.setContentsMargins(9, 9, 9, 9)
		row.addWidget(spinner)
		row.addWidget(label, 1)

		cancel = DauiButton(self.tr('&Cancel'), Icons.cancel())
		cancel.clicked.connect(self.close)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(cancel)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(row, stretch=1)
		layout.addWidget(self._tb)

		widget = QWidget()
		widget.setLayout(layout)
		widget.setMinimumWidth(360)
		self.setWidget(widget)

	@classmethod
	def start(cls, connection, uri, area):
		"""Connect ``connection`` to ``uri`` in the background, showing the window in the MDI ``area``
		meanwhile. Connecting may take a while (e.g. a BLE device connects and its interfaces are walked)."""
		window = cls(connection, uri)
		area.addSubWindow(window)
		window.showCentered()
		connection.connectUriAsync(uri)
		return window

	@Slot()
	def _done(self):
		self._finished = True
		self.close()

	def closeEvent(self, event):
		self._connection.connectionChanged.disconnect(self._done)
		self._connection.connectFailed.disconnect(self._done)
		if not self._finished:
			self._connection.cancelConnect()
		super().closeEvent(event)
