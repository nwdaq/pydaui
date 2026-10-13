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
from pydaui.ui.helpers import AppletWindow
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



class _VisibilityWatcher(QObject):
	"""Emits :attr:`changed` when a widget it filters the events of is shown or hidden."""

	changed = Signal()

	def eventFilter(self, obj, event):
		if event.type() in (QEvent.Show, QEvent.Hide):
			self.changed.emit()
		return False



class MdiArea(QMdiArea):

	#: The doodle tile repeated over the background, at :attr:`BACKGROUND_OPACITY` over the default colour.
	#: Resolved from the package, as this script is moved out of it when frozen.
	BACKGROUND_TILE = Path(pydaui.__file__).resolve().parent / 'assets' / 'doodle-tile.svg'
	BACKGROUND_OPACITY = 0.1

	#: Covers the applet windows while a window of the AppletWindow.CONNECT layer is shown.
	DIM_COLOR = 'rgba(128, 128, 128, 128)'

	def __init__(self, settings):
		super().__init__()
		self._settings = settings
		self.setBackground(self._backgroundBrush())
		# The windows decide themselves whether they are maximized, see AppletWindow.MAXIMIZE. QMdiArea
		# would maximize every window activated while a maximized one is active otherwise.
		self.setOption(QMdiArea.DontMaximizeSubWindowOnActivation)

		# Above the applet windows, catching the clicks meant for them.
		self._dim = QWidget(self.viewport())
		self._dim.setStyleSheet(f'background-color: {self.DIM_COLOR};')
		self._dim.hide()
		self.subWindowActivated.connect(self._restack)
		self._watcher = _VisibilityWatcher(self)
		self._watcher.changed.connect(self._restack, Qt.QueuedConnection)

	@staticmethod
	def _layer(window):
		return getattr(window, 'LAYER', AppletWindow.APPLET)

	@Slot()
	def _restack(self):
		"""Stack the windows in their layers, see :class:`AppletWindow`, keeping their order within a layer.
		Activating a window raises it above all the others, the layers above it are raised back over it."""
		windows = self.subWindowList(QMdiArea.StackingOrder)
		dimmed = any(window.isVisible() and self._layer(window) == AppletWindow.CONNECT for window in windows)
		self._dim.setVisible(dimmed)
		self._dim.raise_()
		# Not dimmed, there are no connect windows and the tool windows may go below the applets.
		layers = (AppletWindow.CONNECT, AppletWindow.TOOL) if dimmed else ()
		for layer in layers:
			for window in windows:
				if self._layer(window) == layer:
					window.raise_()
		for window in windows:
			if getattr(window, 'ON_TOP', False):
				window.raise_()

	def addSubWindow(self, widget, flags=Qt.WindowFlags()):
		window = super().addSubWindow(widget, flags)
		window.installEventFilter(self._watcher)
		if not UiSection.allowMinimizing(self._settings):
			window.setWindowFlags(window.windowFlags() & ~Qt.WindowMinimizeButtonHint)
		return window

	def maximizesWindows(self):
		"""Whether the windows are shown maximized, see :meth:`AppletWindow.setVisible`."""
		return UiSection.startMaximized(self._settings)

	def viewportEvent(self, event):
		if event.type() == QEvent.Resize:
			self._dim.resize(event.size())
		return super().viewportEvent(event)

	def closeLayer(self, layer):
		"""Close the windows of the ``layer``."""
		for window in self.subWindowList():
			if self._layer(window) == layer:
				window.close()

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

		self._main_menu = MainMenu(self._connection)
		self._main_menu.disconnectRequested.connect(self._disconnect)
		self._main_menu.settingsRequested.connect(
			lambda: self._openWindow(SettingsWindow, lambda: SettingsWindow(self._settings)))
		self._main_menu.quitRequested.connect(self.close)
		self._main_menu.changelogRequested.connect(lambda: self._openWindow(ChangelogWindow, ChangelogWindow))
		self._main_menu.aboutRequested.connect(lambda: self._openWindow(AboutWindow, AboutWindow))
		self.addToolBar(self._main_menu)
		self.addToolBarBreak()

		self._ribbon = Ribbon(self._connection, self)
		self._ribbon.appletRequested.connect(self._openApplet)

		self._mdi_area = MdiArea(self._settings)
		self.setCentralWidget(self._mdi_area)

		# The arrangement of the ribbon panels, restored once they are all added. After the central widget
		# is set, a state restored without one loses it when it was saved with no room left for it.
		state = self._settings.getBytes('ui.state')
		if state is not None:
			self.restoreState(state)

		# The open windows of the menus by their class and of the applets by their (applet, interface), only one
		# of each is shown.
		self._windows = {}

		self._connection.connectionChanged.connect(self._updateCaption)
		self._connection.connectionChanged.connect(self._closeApplets)

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
	def _closeApplets(self, uri):
		"""Close the applets once the device is disconnected, they talk to it."""
		if uri is None:
			self._mdi_area.closeLayer(AppletWindow.APPLET)

	def addWindow(self, window):
		"""Add ``window`` to the MDI area, it is shown with its ``show()`` or ``showCentered()``."""
		self._mdi_area.addSubWindow(window)

	@Slot(object)
	def _openApplet(self, instance):
		"""Open the applet ``instance``, or bring its window to the front when it is open already. An applet
		is opened once for an interface."""

		def create():
			try:
				return instance.open(self._connection, self._settings)
			except Exception as e:
				QMessageBox.warning(self, instance.applet.title, 'Cannot open %s:\n\n%s' % (instance.applet.title, e))
				return None

		self._openWindow((instance.applet, instance.interface), create)

	@Slot()
	def _disconnect(self):
		"""Disconnect the device explicitly, it is not connected automatically on the next startup."""
		self._settings.set(DeviceSection.LAST_URI, None)
		self._settings.save()
		self._connection.disconnectUri()

	def _openWindow(self, key, create):
		"""Bring the window open for ``key`` to the front, or open it with ``create()`` if there is none. A
		``create()`` returning ``None`` opens nothing. The window deletes itself when closed."""
		window = self._windows.get(key)
		# Closed, waiting to be deleted.
		if window is not None and not window.isVisible():
			window = None
		if window is None:
			window = create()
			if window is None:
				return
			window.destroyed.connect(lambda: self._windows.pop(key, None) if self._windows.get(key) is window else None)
			self._windows[key] = window
			self._mdi_area.addSubWindow(window)
			window.show()
		elif window.isMinimized():
			window.showNormal()
		self._mdi_area.setActiveSubWindow(window)

	def closeEvent(self, event):
		self._settings.setBytes('ui.geometry', self.saveGeometry())
		self._settings.setBytes('ui.state', self.saveState())
		super().closeEvent(event)


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

		self._connect_window = ConnectWindow(self._connection, self._settings)
		self._main_window.addWindow(self._connect_window)

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
		"""Connect the last device if enabled, showing the connecting window. The connect window opens when
		it fails or is cancelled."""
		uri = self._settings.get(DeviceSection.LAST_URI)
		if not self._settings.get(DeviceSection.AUTO_CONNECT, False) or not uri or self._connection.connected:
			return
		ConnectingWindow.start(self._connection, uri, self._connect_window.mdiArea())

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
