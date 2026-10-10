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
import os
from pathlib import Path

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from PySide6.QtSvg import QSvgRenderer
from superqt.fonticon import icon, spin, pulse
# from fonticon_fa6 import FA6S

from pydaui.connection import *
from pydaui.ui.menu import *
from pydaui.ui.connect_window import ConnectWindow
from pydaui.ui.style import DauiStyle
from pydaui.settings import Settings
from pydaui.applets.flash import *
from pydaui.applets.conf import ConfTreeWindow, PROTOCOL as CONF_PROTOCOL

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
	BACKGROUND_TILE = Path(__file__).resolve().parent / 'assets' / 'doodle-tile.svg'
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

	def __init__(self, connection, settings):
		super().__init__()

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
		self.tb1.clicked.connect(self._openConfTree)

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

		self._connection.connectionChanged.connect(self._connectionChanged)
		self._connectionChanged(self._connection.uri)

	@Slot(object)
	def _connectionChanged(self, uri):
		# The configuration tree needs an interface speaking the conf protocol.
		self.tb1.setEnabled(self._connection.findInterface(CONF_PROTOCOL) is not None)

	@Slot()
	def _openConfTree(self):
		try:
			window = ConfTreeWindow(self._connection)
		except Exception as e:
			QMessageBox.warning(self, 'Configuration tree', 'Cannot open the configuration tree:\n\n%s' % e)
			return
		self._mdi_area.addSubWindow(window)
		window.show()

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self._overlay.resize(self.size())

	def closeEvent(self, event):
		self._settings.setBytes('ui.geometry', self.saveGeometry())
		super().closeEvent(event)

	@property
	def overlay(self):
		return self._overlay


class DauiApplication(QApplication):

	def __init__(self, argv):
		super().__init__(argv)
		# Determines the settings location, eg. ~/.config/pydaui/
		self.setApplicationName('pydaui')

		self.setStyle(DauiStyle())

		self._settings = Settings()
		self._connection = PyDauiConnection()

		self._main_window = MainWindow(self._connection, self._settings)
		self._main_window.show()

		self._connect_window = ConnectWindow(self._connection, parent=self._main_window)

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

	l = logging.INFO
	if args.debug:
		l = logging.DEBUG

	logging.basicConfig(format=LOG_FORMAT, level=l)
	logging.getLogger('werkzeug').setLevel(logging.WARN)


	app = DauiApplication(sys.argv)
	sys.exit(app.exec())


if __name__ == "__main__":
	main()
