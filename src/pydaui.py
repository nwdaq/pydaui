#!/usr/bin/env python

import sys
import logging
from colorama import init as colorama_init, Fore, Back, Style
import argparse
from dataclasses import dataclass
from uritools import urisplit, uricompose

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse
# from fonticon_fa6 import FA6S

from pyocd.core.helpers import ConnectHelper
from pyocd.flash.loader import MemoryLoader
from pyocd.core.exceptions import ProbeError


LOG_FORMAT = f"""[{Style.BRIGHT}{Fore.WHITE}%(asctime)s{Style.NORMAL}] {Fore.BLUE}{Style.BRIGHT}%(levelname)-10s{Fore.YELLOW}{Style.NORMAL}%(module)s:%(name)s: {Style.RESET_ALL}%(message)s"""


def assert_depends(val):
	if not val:
		raise ValueError('missing dependency')


class ServiceProxyNotConnected(Exception):
	pass


class ServiceProxyConnection:

	def __init__(self):
		self._uri = ''

	def connectUri(self, uri):
		self._uri = uri

	def disconnectUri(self):
		self._uri = ''

	@property
	def uri(self):
		return self._uri


@dataclass
class ServiceProxyTarget:
	uri: str
	vendor: str
	product: str


class ServiceProxyConnectionError(Exception):
	pass

class SwdServiceProxyConnection(ServiceProxyConnection):

	@dataclass
	class Product:
		pn: str
		version: str
		mcu: str
		pack: str

	products = [
		Product('v35v-ctrl', '1.0.0', 'STM32G491CCUx', 'Keil.STM32G4xx_DFP.2.0.0.pack'),
	]


	def __init__(self):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._session = None

	def connectUri(self, uri):
		super().connectUri(uri)
		uris = urisplit(uri)
		self._session = ConnectHelper.session_with_chosen_probe(
			unique_id=uris.host,
			options={
				"target_override": "STM32G491CCUx",
				"pack": "Keil.STM32G4xx_DFP.2.0.0.pack",
			}
		)
		try:
			self._session.open()
		except ProbeError as e:
			self._session.close()
			self._logger.error(f'cannot connect target on {uri}: {str(e)}')
			self._session = None
			raise ServiceProxyConnectionError()

		self._loader = MemoryLoader(self._session)
		print(self._session.board.target.memory_map)

	def disconnectUri(self):
		super().disconnectUri()
		self._session.close()
		self._session = None

	@property
	def connected(self):
		return self._session != None

	@staticmethod
	def probe():
		targets = []
		for dp in ConnectHelper.get_all_connected_probes():
			targets.append(ServiceProxyTarget(
				vendor=dp.vendor_name,
				product=dp.product_name,
				uri=uricompose(scheme='swd', host=dp.unique_id),
			))

		return targets


class Singleton(type(QObject)):
	def __init__(cls, name, bases, dict):
		super().__init__(name, bases, dict)
		cls.instance = None

	def __call__(cls, *args, **kw):
		if cls.instance is None:
			cls.instance = super().__call__(*args, **kw)
		return cls.instance


class ServiceProxy(QObject, metaclass=Singleton):

	connectionChanged = Signal(str)

	def __init__(self):
		super().__init__()
		self._connection = None
		self._logger = logging.getLogger(self.__class__.__name__)


	def connectUri(self, uri):
		uris = urisplit(uri)
		if self._connection:
			self._connection.disconnectUri()
			self._connection = None

		self._logger.info(f'connecting to {uri}')

		try:
			if uris.scheme == 'swd':
				self._connection = SwdServiceProxyConnection()
			self._connection.connectUri(uri)
		except ServiceProxyConnectionError as e:
			self._connection = None
			raise e
		self.connectionChanged.emit(uri)

	def disconnectUri(self):
		self._logger.info(f'disconnecting from {self._connection.uri}')
		self._connection.disconnectUri()
		self._connection = None
		self.connectionChanged.emit(None)

	def probe(self):
		targets = SwdServiceProxyConnection.probe()
		for t in targets:
			self._logger.info(f'discovered target at {t.uri} ({t.vendor} {t.product})')
		return targets

	@property
	def connection(self):
		return self._connection

	@property
	def connected(self):
		return self._connection and self._connection.connected


class ServiceProxySocket:
	pass


class ConfigTree(QTreeWidget):

	def __init__(self):
		super().__init__()

		self.setColumnCount(5)
		t = QTreeWidgetItem(['jeden', 'dva', 'tri', 'styri', 'pat'])
		t.setIcon(0, icon('mdi7.forest-outline', color='#080', scale_factor=1))
		tt = QTreeWidgetItem([str(i) for i in range(5)])
		tt.setIcon(0, icon('mdi7.forest-outline', color='#080', scale_factor=1))
		self.setStyleSheet('QTreeWidget::item { padding: 4px }')
		t.addChild(tt)
		self.addTopLevelItem(t)


class MainMenu(QMenu):

	def __init__(self):
		super().__init__()


class MdiArea(QMdiArea):

	def __init__(self):
		super().__init__()


class ConnectionToolBar(QToolBar):

	def __init__(self):
		super().__init__()

		self._con_ok_icon = icon('mdi7.link', color='#44c', scale_factor=1)
		self._con_none_icon = icon('mdi7.link', color='#888', scale_factor=1)

		self._state_btn = QToolButton()
		self._state_btn.setIcon(self._con_none_icon)
		self._state_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
		self._state_btn.setEnabled(False)
		self.addWidget(self._state_btn)

		self._disconnect_btn = QToolButton()
		self._disconnect_btn.setIcon(icon('mdi7.power', color='#c00', scale_factor=1))
		self._disconnect_btn.setText('Disconnect')
		self._disconnect_btn.setEnabled(False)
		self._disconnect_btn.clicked.connect(self._disconnectClicked)
		self.addWidget(self._disconnect_btn)

		self._connectionChanged(None)

		self._sp = ServiceProxy()
		self._sp.connectionChanged.connect(self._connectionChanged)

	@Slot(str)
	def _connectionChanged(self, uri):
		if uri:
			self._state_btn.setText(uri)
			self._state_btn.setIcon(self._con_ok_icon)
			self._disconnect_btn.setEnabled(True)
		else:
			self._state_btn.setText('no connection')
			self._state_btn.setIcon(self._con_none_icon)
			self._disconnect_btn.setEnabled(False)

	@Slot()
	def _disconnectClicked(self):
		self._sp.disconnectUri()



class MainWindow(QMainWindow):

	def __init__(self):
		super().__init__()

		self.resize(QSize(800, 600))

		# Gray overlay to show when a modal is active
		self._overlay = QWidget(self)
		self._overlay.setStyleSheet("background-color: rgba(128, 128, 128, 128);")
		self._overlay.hide()



		self.tb = QToolBar()

		self.rib = QTabWidget()
		self.rib.setTabPosition(QTabWidget.North)

		self.home_tab = QToolBar()
		self.home_tab.setIconSize(QSize(30, 30))

		self.tb1 = QToolButton()
		self.tb1.setIcon(icon('mdi7.cog-outline', color='gray', scale_factor=1))
		self.tb1.setText('Configuration tree')
		self.tb1.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)

		self.tb2 = QToolButton()
		self.tb2.setIcon(icon('mdi7.cog-outline', color='gray', scale_factor=1))
		self.tb2.setText('New')
		self.tb2.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)

		self.home_tab.addWidget(self.tb1)
		self.home_tab.addWidget(self.tb2)
		self.rib.addTab(self.home_tab, 'Configuration')

		self.insert_tab = QWidget()
		self.rib.addTab(self.insert_tab, 'Insert')
		self.rib.setTabIcon(0, icon('mdi7.cog', color='#008', scale_factor=1))

		self.tb.addWidget(self.rib)

		self.addToolBar(ConnectionToolBar())
		self.addToolBarBreak()
		self.addToolBar(self.tb)

		d = QDockWidget()
		d.setWidget(ConfigTree())
		self.addDockWidget(Qt.TopDockWidgetArea, d)
		d.show()

		self._mdi_area = MdiArea()
		self.setCentralWidget(self._mdi_area)

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self._overlay.resize(self.size())

	@property
	def overlay(self):
		return self._overlay


class Admonition(QFrame):

	MESSAGE = 1
	NOTE = 1
	WARN = 2

	def __init__(self, text, ad_type=0):
		super().__init__()

		if ad_type == Admonition.NOTE:
			ad_icon = icon('mdi7.information-outline', color='#008')
			self.setProperty('class', ['admonition', 'admonition-note'])
		elif ad_type == Admonition.WARN:
			ad_icon = icon('mdi7.alert-octagon-outline', color='#800')
			self.setProperty('class', ['admonition', 'admonition-warn'])
		else:
			ad_icon = icon('mdi7.message-outline', color='#888')
			self.setProperty('class', 'admonition')

		self.setStyleSheet('''
			.admonition {
				border: 1px solid #ccc;
				border-radius: 5px;
				background-color: #fff;
			}
			.admonition-note {
				border-color: #008;
			}
			.admonition-warn {
				border-color: #800;
			}

		''')

		l = QHBoxLayout()
		i = QLabel()
		i.setPixmap(ad_icon.pixmap(24, 24))
		l.addWidget(i)
		l.addWidget(QLabel(text), stretch=1)
		self.setLayout(l)



class ConnectWindow(QDialog):

	def __init__(self, parent=None):
		super().__init__(parent=parent)
		self._service_proxy = ServiceProxy()

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

		self._swd_tab = self._createSwdTab()
		self._tabs.addTab(self._swd_tab, 'SWD')
		self._tabs.setTabIcon(0, icon('mdi7.bug-outline', color='#444', scale_factor=1))
		self._tabs.setTabEnabled(0, True)

		self._tabs.addTab(self._createUsbTab(), 'USB')
		self._tabs.setTabIcon(1, icon('mdi7.usb-port', color='#444', scale_factor=1))
		self._tabs.setTabEnabled(1, False)

		self._tabs.addTab(self._createSerialTab(), 'Serial')
		self._tabs.setTabIcon(2, icon('mdi7.serial-port', color='#444', scale_factor=1))
		self._tabs.setTabEnabled(2, False)


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
		uri = ''
		if self._tabs.currentIndex() == 0:
			# Handle SWD connections
			self._service_proxy.connectUri(self._swd_probe_cb.currentData())
			self.close()

	def _createSwdTab(self):
		w = QWidget()
		l = QVBoxLayout()
		w.setLayout(l)
		l.addWidget(QLabel(
			'SWD is a debug interface and protocol used to access ARM Cortex-M\n'
			'microprocessor cores and perform debug actions or program memories.\n\n'
			'It is possible to use SWD access to recover a broken device bootloader.\n\n'
			'Select debug probe to access the device:'
		))

		# Create a combo box to select debug probe to access the target
		cb = QComboBox()
		cb.setIconSize(QSize(24, 24))
		for p in self._service_proxy.probe():
			cb.addItem(f'{p.vendor} {p.product} ({p.uri})', f'{p.uri}')
		for i in range(cb.count()):
			cb.setItemIcon(i, icon('mdi7.bug-outline', color='#008', scale_factor=1))
		l.addWidget(cb)
		self._swd_probe_cb = cb

		l.addSpacing(16)

		l.addWidget(QLabel(
			'Select the device/product you are connecting to. It must be known beforehand:'
		))

		# Add list of devices/products
		dev = QComboBox()
		dev.setIconSize(QSize(24, 24))
		for p in SwdServiceProxyConnection.products:
			dev.addItem(f'{p.pn} ({p.version})', (p.pn, p.version))
		for i in range(dev.count()):
			dev.setItemIcon(i, icon('mdi7.memory', color='#888', scale_factor=1))
		l.addWidget(dev)

		l.addWidget(Admonition(
			'Beware, here be dragons. Selecting the wrong device may damage it.',
			Admonition.WARN
		))

		return w

	def _createUsbTab(self):
		return QWidget()

	def _createSerialTab(self):
		return QWidget()


	@Slot()
	def _finished(self):
		self.parent().overlay.hide()

	@Slot()
	def _checkTimerTimeout(self):
		if not self._service_proxy.connected and not self.isVisible():
			self.open()


class DauiApplication(QApplication):

	def __init__(self, argv):
		super().__init__(argv)

		# self.setStyle('material')

		self._main_window = MainWindow()
		self._main_window.show()

		self._connect_window = ConnectWindow(parent=self._main_window)



if __name__ == "__main__":

	parser = argparse.ArgumentParser(
		description='da_UI DAQ frontend interface',
		epilog='(c) 2024-2025 Marek Koza <qyx@krtko.org>',
		formatter_class=argparse.RawDescriptionHelpFormatter
	)

	parser.add_argument('--host', type=str, default='127.0.0.1', help='API listening host')
	parser.add_argument('--port', type=int, default=8000, help='API listening port (defaults to 8000)')
	parser.add_argument('--debug', action='store_true', help='DEBUG level logging')

	args = parser.parse_args()
	colorama_init()

	l = logging.INFO
	if args.debug:
		l = logging.DEBUG

	logging.basicConfig(format=LOG_FORMAT, level=l)
	logging.getLogger('werkzeug').setLevel(logging.WARN)


	app = DauiApplication(sys.argv)
	sys.exit(app.exec())
