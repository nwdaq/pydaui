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

from pydaui.connection.service_proxy import *


class ApplicationMenu(QMenu):

	def __init__(self):
		super().__init__(self.tr('&Application'))

		self.addAction(icon('mdi7.exit-to-app', scale_factor=1), self.tr('&Quit'))


class DeviceMenu(QMenu):

	def __init__(self):
		super().__init__(self.tr('&Device'))


class AboutMenu(QMenu):

	def __init__(self):
		super().__init__(self.tr('&About'))


class MainMenu(QToolBar):

	def __init__(self):
		super().__init__()

		self.setMovable(False)
		self.setFloatable(False)

		self._menu = QMenuBar()
		self._menu.setStyleSheet('QMenuBar::item { padding: 10px 0px; }')
		self._menu.addMenu(ApplicationMenu())
		self._menu.addMenu(DeviceMenu())
		self._menu.addMenu(AboutMenu())

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
