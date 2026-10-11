#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
#
# pydaui application entry point and main window
#
# Copyright (c) 2025, Marek Koza (qyx@krtko.org)
# All rights reserved.

import sys
import logging
from colorama import init as colorama_init, Fore, Back, Style
import argparse
from dataclasses import dataclass
import tempfile
import threading
import os
from pathlib import Path

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from PySide6.QtSvg import QSvgRenderer
from superqt.fonticon import icon, spin, pulse
# from fonticon_fa6 import FA6S

import pydaui
from pydaui import bluetooth
from pydaui.applets import conf
from pydaui.connection import *
from pydaui.ui.menu import *
from pydaui.ui.connect_window import ConnectWindow, ConnectingWindow
from pydaui.ui.about_window import AboutWindow
from pydaui.ui.changelog_window import ChangelogWindow
from pydaui.ui.settings_window import SettingsWindow, UiSection, DeviceSection
from pydaui.ui.ribbon import Ribbon
from pydaui.ui.style import DauiStyle
from pydaui.settings import Settings

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

	#: The doodle tile repeated over the background, at :attr:`BACKGROUND_OPACITY` over the default colour.
	#: Resolved from the package, as this script is moved out of it when frozen.
	BACKGROUND_TILE = Path(pydaui.__file__).resolve().parent / 'assets' / 'doodle-tile.svg'
	BACKGROUND_OPACITY = 0.1

	def __init__(self):
		super().__init__()
		self.setBackground(self._backgroundBrush())

	def _backgroundBrush(self):
		"""The default background with the doodle tile drawn over it, rendered once and repeated."""
		base = self.background()
		renderer = QSvgRenderer(str(self.BACKGROUND_TILE))
		if not renderer.isValid():
			logging.getLogger(self.__class__.__name__).warning(f'cannot load the background {self.BACKGROUND_TILE}')
			return base

		# Render at the screen pixel density so the doodles stay sharp on HiDPI screens.
		scale = QGuiApplication.primaryScreen().devicePixelRatio()
		tile = QPixmap(renderer.defaultSize() * scale)
		tile.setDevicePixelRatio(scale)
		tile.fill(base.color())
		painter = QPainter(tile)
		painter.setOpacity(self.BACKGROUND_OPACITY)
		renderer.render(painter, QRectF(QPointF(0, 0), renderer.defaultSize()))
		painter.end()
		return QBrush(tile)



class MainWindow(QMainWindow):

	#: The caption when no device is connected or it cannot be described.
	CAPTION = 'pydaui'

	#: The conf nodes describing the connected device in the caption: its hostname, port and version.
	CAPTION_PATHS = (['system', 'hostname'], ['system', 'firmware', 'port'], ['system', 'firmware', 'version'])

	#: Delivers the (uri, values of CAPTION_PATHS) read off the GUI thread.
	_captionRead = Signal(str, list)

	def __init__(self, connection, settings):
		super().__init__()
		self.setWindowTitle(self.CAPTION)
		self._captionRead.connect(self._setCaption)

		self._connection = connection
		self._settings = settings

		self.resize(QSize(800, 600))
		geometry = self._settings.getBytes('ui.geometry')
		if geometry is not None:
			self.restoreGeometry(geometry)

		# Gray overlay to show when a modal is active
		self._overlay = QWidget(self)
		self._overlay.setStyleSheet("background-color: rgba(128, 128, 128, 128);")
		self._overlay.hide()

		self._main_menu = MainMenu(self._connection)
		self._main_menu.disconnectRequested.connect(self._disconnect)
		self._main_menu.settingsRequested.connect(lambda: self._openWindow(SettingsWindow, self._settings))
		self._main_menu.quitRequested.connect(self.close)
		self._main_menu.changelogRequested.connect(lambda: self._openWindow(ChangelogWindow))
		self._main_menu.aboutRequested.connect(lambda: self._openWindow(AboutWindow))
		self.addToolBar(self._main_menu)
		self.addToolBarBreak()

		self._ribbon = Ribbon(self._connection, self)
		self._ribbon.appletRequested.connect(self._openApplet)
		# The arrangement of the ribbon panels, restored once they are all added.
		state = self._settings.getBytes('ui.state')
		if state is not None:
			self.restoreState(state)

		self._mdi_area = MdiArea()
		self.setCentralWidget(self._mdi_area)

		# The open windows of the menus by their class, only one of each is shown.
		self._windows = {}

		self._connection.connectionChanged.connect(self._updateCaption)

	@Slot(object)
	def _updateCaption(self, uri):
		"""Show the hostname, port and firmware version of the connected device as
		``<hostname> (<port>, <version>)``, read from its conf tree off the GUI thread."""
		self.setWindowTitle(self.CAPTION)
		interface = self._connection.findInterface(conf.PROTOCOL)
		if uri is None or interface is None:
			return

		def read():
			try:
				values = conf.read_values(self._connection, interface, self.CAPTION_PATHS)
			except Exception as e:
				logging.getLogger(self.__class__.__name__).info(f'cannot read the device description: {e}')
				return
			self._captionRead.emit(uri, values)

		threading.Thread(target=read, name='caption', daemon=True).start()

	@Slot(str, list)
	def _setCaption(self, uri, values):
		# Read for an earlier connection.
		if uri != self._connection.uri:
			return
		hostname, port, version = values
		# Without the build metadata, like the firmware list of the updates.
		version = version.partition('+')[0] if version else None
		details = ', '.join(value for value in (port, version) if value)
		caption = hostname or self.CAPTION
		self.setWindowTitle(f'{caption} ({details})' if details else caption)

	@Slot(object)
	def _openApplet(self, instance):
		try:
			window = instance.open(self._connection, self._settings)
		except Exception as e:
			QMessageBox.warning(self, instance.applet.title, 'Cannot open %s:\n\n%s' % (instance.applet.title, e))
			return
		self._mdi_area.addSubWindow(window)
		if not self._settings.get(UiSection.APPLET_ALLOW_MINIMIZING, True):
			window.setWindowFlags(window.windowFlags() & ~Qt.WindowMinimizeButtonHint)
		if self._settings.get(UiSection.APPLET_START_MAXIMIZED, False):
			window.showMaximized()
		else:
			window.show()

	@Slot()
	def _disconnect(self):
		"""Disconnect the device explicitly, it is not connected automatically on the next startup."""
		self._settings.set(DeviceSection.LAST_URI, None)
		self._settings.save()
		self._connection.disconnectUri()

	def _openWindow(self, window_class, *args):
		"""Show the window of ``window_class``, opening it with ``args`` if it is not open yet."""
		window = self._windows.get(window_class)
		if window is None:
			window = window_class(*args)
			window.destroyed.connect(lambda: self._windows.pop(window_class, None))
			self._windows[window_class] = window
			self._mdi_area.addSubWindow(window)
		window.show()
		self._mdi_area.setActiveSubWindow(window)

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self._overlay.resize(self.size())

	def closeEvent(self, event):
		self._settings.setBytes('ui.geometry', self.saveGeometry())
		self._settings.setBytes('ui.state', self.saveState())
		super().closeEvent(event)

	def updateOverlay(self):
		"""Gray the main window while any of its dialogs is open, call when one opens or closes."""
		dialogs = [d for d in self.findChildren(QDialog, options=Qt.FindDirectChildrenOnly) if d.isVisible()]
		self._overlay.setVisible(bool(dialogs))
		self._overlay.raise_()


class DauiApplication(QApplication):

	def __init__(self, argv):
		super().__init__(argv)
		# Determines the settings location, eg. ~/.config/pydaui/
		self.setApplicationName('pydaui')

		self.setStyle(DauiStyle())

		self._settings = Settings()
		self._connection = PyDauiConnection()

		self._main_window = MainWindow(self._connection, self._settings)
		if self._settings.get(UiSection.MAIN_START_MAXIMIZED, False):
			self._main_window.showMaximized()
		else:
			self._main_window.show()

		self._connect_window = ConnectWindow(self._connection, parent=self._main_window)

		self._connection.connectionChanged.connect(self._connectionChanged)
		# After the main window is shown, before the connect window opens.
		QTimer.singleShot(0, self._autoConnect)

	@Slot(object)
	def _connectionChanged(self, uri):
		# Remember the device to connect automatically on the next startup.
		if uri:
			self._settings.set(DeviceSection.LAST_URI, uri)
			self._settings.save()

	@Slot()
	def _autoConnect(self):
		"""Connect the last device if enabled, showing the connecting window over the main window. The
		connect window opens when it fails or is cancelled."""
		uri = self._settings.get(DeviceSection.LAST_URI)
		if not self._settings.get(DeviceSection.AUTO_CONNECT, False) or not uri or self._connection.connected:
			return
		ConnectingWindow(self._connection, uri, parent=self._main_window).open()
		self._connection.connectUriAsync(uri)

	def exec(self):
		super().exec()
		self._connection.disconnectUri()
		self._settings.save()



def main():
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
	bluetooth.install()

	l = logging.INFO
	if args.debug:
		l = logging.DEBUG

	logging.basicConfig(format=LOG_FORMAT, level=l)
	logging.getLogger('werkzeug').setLevel(logging.WARN)


	app = DauiApplication(sys.argv)
	sys.exit(app.exec())


if __name__ == "__main__":
	main()
