# SPDX-License-Identifier: GPL-3.0-or-later
#
# Main menu and toolbar
#
# Copyright (c) 2025, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse

from pydaui.connection import *
from pydaui.ui.icons import Icons


class ApplicationMenu(QMenu):

	quitRequested = Signal()

	def __init__(self):
		super().__init__(self.tr('&Application'))

		quit_action = self.addAction(Icons.quit(), self.tr('&Quit'), QKeySequence(Qt.CTRL | Qt.Key_Q))
		quit_action.triggered.connect(self.quitRequested)


class DeviceMenu(QMenu):

	def __init__(self):
		super().__init__(self.tr('&Device'))


class HelpMenu(QMenu):

	aboutRequested = Signal()

	def __init__(self):
		super().__init__(self.tr('&Help'))

		about_action = self.addAction(Icons.about(), self.tr('&About'))
		about_action.triggered.connect(self.aboutRequested)


class MainMenu(QToolBar):

	quitRequested = Signal()
	aboutRequested = Signal()

	def __init__(self, connection):
		super().__init__()
		# Identifies the toolbar in the saved window state.
		self.setObjectName('main-menu')

		self._connection = connection

		self.setMovable(False)
		self.setFloatable(False)

		self._menu = QMenuBar()
		self._app_menu = ApplicationMenu()
		self._app_menu.quitRequested.connect(self.quitRequested)
		self._menu.addMenu(self._app_menu)
		self._menu.addMenu(DeviceMenu())
		self._help_menu = HelpMenu()
		self._help_menu.aboutRequested.connect(self.aboutRequested)
		self._menu.addMenu(self._help_menu)

		self.addWidget(self._menu)
		self.addSeparator()
		self._addConnectionBar()

	def _addConnectionBar(self):
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

		self._connection.connectionChanged.connect(self._connectionChanged)

	@Slot(object)
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
		self._connection.disconnectUri()
