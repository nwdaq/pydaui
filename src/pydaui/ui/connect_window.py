# SPDX-License-Identifier: GPL-3.0-or-later
#
# Connect window selecting the target connection
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse

from pynbus2.uri import parse_uri, split_serial_path

from pydaui.connection import *
from pydaui.ui.helpers import Admonition


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

	def __init__(self, connection, parent=None):
		super().__init__(parent=parent)
		self._connection = connection

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

	@dataclass
	class Product:
		pn: str
		version: str
		#: pyocd target type.
		mcu: str
		#: CMSIS pack with the target and its flash algorithm, relative to the pydaui package.
		pack: str

	#: The products a target can be selected as. SWD does not identify the product, it must be known.
	products = [
		Product('v35v-ctrl', '1.0.0', 'STM32G491CCUx', 'Keil.STM32G4xx_DFP.2.0.0.pack'),
	]

	def __init__(self, connection, parent=None):
		super().__init__(connection, parent=parent)
		self._createUi()

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		l.addWidget(QLabel(
			'SWD is a debug interface and protocol used to access ARM Cortex-M\n'
			'microprocessor cores and perform debug actions or program memories.\n\n'
			'It is possible to use SWD access to recover a broken device bootloader.\n\n'
			'Select debug probe to access the device:'
		))

		# Create a combo box to select debug probe to access the target
		cb = QComboBox()
		cb.setIconSize(QSize(24, 24))
		for probe_uri in self._connection.probe('swd://'):
			cb.addItem(probe_uri, probe_uri)
		for i in range(cb.count()):
			cb.setItemIcon(i, icon('mdi7.bug-outline', color='#008', scale_factor=1))
		l.addWidget(cb)
		self._probe_cb = cb

		l.addSpacing(16)

		l.addWidget(QLabel(
			'Select the device/product you are connecting to. It must be known beforehand:'
		))

		# Add list of devices/products
		dev = QComboBox()
		dev.setIconSize(QSize(24, 24))
		for p in self.products:
			dev.addItem(f'{p.pn} ({p.version})', p)
		for i in range(dev.count()):
			dev.setItemIcon(i, icon('mdi7.memory', color='#888', scale_factor=1))
		l.addWidget(dev)
		self._product_cb = dev

		l.addWidget(Admonition(
			'Beware, here be dragons. Selecting the wrong device may damage it.',
			Admonition.WARN
		))

	def uri(self):
		"""The probe URI with the selected product's target and pack, reaching its virtual flash service."""
		probe_uri = self._probe_cb.currentData()
		product = self._product_cb.currentData()
		if probe_uri is None or product is None:
			return None
		pack = Path(__file__).resolve().parent.parent / product.pack
		return '%s?%s' % (probe_uri, urlencode({'target': product.mcu, 'pack': str(pack)}))


class UsbTab(ConnectWindowTab):

	NAME = 'USB'
	ICON = 'mdi7.usb-port'
	ENABLED = False


class _ProbeWorker(QObject):
	"""Runs :meth:`Connection.probe` for one URI off the GUI thread and reports the result via a signal.

	The blocking probe (opening a serial port and waiting for advertisements) must not run on the GUI
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
		threading.Thread(target=self._run, name='serial-probe', daemon=True).start()

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

	def __init__(self, connection, parent=None):
		super().__init__(connection, parent=parent)
		# Keep worker references alive until their signal has been delivered.
		self._workers = set()
		self._createUi()
		self._rescanPorts()

	def _createUi(self):
		l = QVBoxLayout()
		self.setLayout(l)
		l.addWidget(QLabel(
			'Connect over a serial diagnostic console. Pick a port and press Refresh\n'
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


class ConnectWindow(QDialog):

	def __init__(self, connection, parent=None):
		super().__init__(parent=parent)
		self._connection = connection

		self._check_timer = QTimer()
		self._check_timer.setInterval(500)
		self._check_timer.timeout.connect(self._checkTimerTimeout)
		self._check_timer.start()

		self.finished.connect(self._finished)
		self._createUi()


	def open(self):
		super().open()
		self.parent().overlay.show()
		self.parent().overlay.raise_()

	def _createUi(self):
		# self.resize(QSize(400, 300))
		self.setWindowTitle('Connect')

		self._tabs = QTabWidget()
		self._tabs.setTabPosition(QTabWidget.North)

		# One tab per ConnectWindowTab subclass; each carries its own name, icon and enabled state.
		for i, tab_cls in enumerate(ConnectWindowTab.__subclasses__()):
			tab = tab_cls(self._connection)
			self._tabs.addTab(tab, tab_cls.NAME)
			self._tabs.setTabIcon(i, tab_cls.tabIcon())
			self._tabs.setTabEnabled(i, tab_cls.ENABLED)


		# Add bottom dialog buttons
		self._hbox_btn = QHBoxLayout()
		self._hbox_btn.addStretch(1)

		b_exit = QPushButton(
			icon('mdi7.exit-to-app', color='#888', scale_factor=1),
			'&Exit'
		)
		b_exit.setIconSize(QSize(24, 24))
		b_exit.clicked.connect(self._exitClicked)
		self._hbox_btn.addWidget(b_exit, alignment=Qt.AlignRight)

		b_connect = QPushButton(
			icon('mdi7.link', color='#080', scale_factor=1),
			'&Connect'
		)
		b_connect.setIconSize(QSize(24, 24))
		b_connect.setDefault(True)
		b_connect.clicked.connect(self._connectClicked)
		self._hbox_btn.addWidget(b_connect, alignment=Qt.AlignRight)


		self._vbox = QVBoxLayout()
		self._vbox.addWidget(self._tabs)
		self._vbox.addLayout(self._hbox_btn)
		self.setLayout(self._vbox)

	@Slot()
	def _exitClicked(self):
		QApplication.quit()

	@Slot()
	def _connectClicked(self):
		uri = self._tabs.currentWidget().uri()
		if uri:
			self._connection.connectUri(uri)
			self.close()

	@Slot()
	def _finished(self):
		self.parent().overlay.hide()

	@Slot()
	def _checkTimerTimeout(self):
		if not self._connection.connected and not self.isVisible():
			self.open()
