#!/usr/bin/env python

import sys
import logging
from colorama import init as colorama_init, Fore, Back, Style
import argparse
from dataclasses import dataclass
import tempfile
import os

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse
# from fonticon_fa6 import FA6S

from connection.service_proxy import *
from ui.menu import *
from applets.flash import *

LOG_FORMAT = f"""[{Style.BRIGHT}{Fore.WHITE}%(asctime)s{Style.NORMAL}] {Fore.BLUE}{Style.BRIGHT}%(levelname)-10s{Fore.YELLOW}{Style.NORMAL}%(module)s:%(name)s: {Style.RESET_ALL}%(message)s"""


def assert_depends(val):
	if not val:
		raise ValueError('missing dependency')




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



class MdiArea(QMdiArea):

	def __init__(self):
		super().__init__()




class MainWindow(QMainWindow):

	def __init__(self):
		super().__init__()

		self.resize(QSize(800, 600))

		# Gray overlay to show when a modal is active
		self._overlay = QWidget(self)
		self._overlay.setStyleSheet("background-color: rgba(128, 128, 128, 128);")
		self._overlay.hide()

		self._main_menu = MainMenu()
		self.addToolBar(self._main_menu)
		self.addToolBarBreak()


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

		self.addToolBar(self.tb)

		self._mdi_area = MdiArea()
		self.setCentralWidget(self._mdi_area)

		self._mdi_area.addSubWindow(FlashVolumeList())


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

	def exec(self):
		super().exec()
		ServiceProxy().disconnectUri()



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
