# SPDX-License-Identifier: GPL-3.0-or-later
#
# Main menu and toolbar
#
# Copyright (c) 2025, Marek Koza (qyx@krtko.org)
# All rights reserved.

import logging
import threading

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse

from pydaui.applets import conf
from pydaui.connection import *
from pydaui.ui.icons import Icons


class ApplicationMenu(QMenu):

	settingsRequested = Signal()
	quitRequested = Signal()

	def __init__(self):
		super().__init__(self.tr('&Application'))

		settings_action = self.addAction(Icons.conf(), self.tr('&Settings'))
		settings_action.triggered.connect(self.settingsRequested)

		self.addSeparator()

		quit_action = self.addAction(Icons.quit(), self.tr('&Quit'), QKeySequence(Qt.CTRL | Qt.Key_Q))
		quit_action.triggered.connect(self.quitRequested)


class DeviceMenu(QMenu):

	def __init__(self):
		super().__init__(self.tr('&Device'))


class HelpMenu(QMenu):

	changelogRequested = Signal()
	aboutRequested = Signal()

	def __init__(self):
		super().__init__(self.tr('&Help'))

		changelog_action = self.addAction(Icons.changelog(), self.tr('&Change log'))
		changelog_action.triggered.connect(self.changelogRequested)

		about_action = self.addAction(Icons.about(), self.tr('&About'))
		about_action.triggered.connect(self.aboutRequested)


def format_uptime(seconds):
	"""``seconds`` as ``H:MM:SS``, with the days in front if there are any, e.g. ``3d 4:05:06``."""
	days, rest = divmod(seconds, 86400)
	text = f'{rest // 3600}:{rest // 60 % 60:02}:{rest % 60:02}'
	return f'{days}d {text}' if days else text


class DeviceStatus(QWidget):
	"""The heap usage and the uptime of the connected device, each with its icon, read from its
	configuration tree every :attr:`INTERVAL`. :attr:`available` tells whether there is a status to show,
	there is none when the device has no configuration tree."""

	#: How often the status is read, in milliseconds.
	INTERVAL = 5000

	#: The conf nodes of the used heap, the heap size and the uptime in seconds.
	PATHS = (['system', 'memory', 'used'], ['system', 'memory', 'heap'], ['system', 'uptime'])

	ICON_SIZE = 18

	#: Whether there is a status to show. In a toolbar the visibility is up to the widget's action.
	available = Signal(bool)

	#: Delivers the (uri, values of PATHS) read off the GUI thread.
	_read = Signal(str, list)

	def __init__(self, connection):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._connection = connection
		# Whether a read is running, the next one waits for it.
		self._reading = False

		self._heap = QLabel()
		self._uptime = QLabel()
		layout = QHBoxLayout(self)
		layout.setContentsMargins(4, 0, 8, 0)
		layout.setSpacing(4)
		layout.addWidget(self._icon('mdi7.chip'))
		layout.addWidget(self._heap)
		layout.addSpacing(10)
		layout.addWidget(self._icon('mdi7.clock-outline'))
		layout.addWidget(self._uptime)

		self._timer = QTimer(self)
		self._timer.setInterval(self.INTERVAL)
		self._timer.timeout.connect(self._poll)
		self._read.connect(self._show)
		connection.connectionChanged.connect(self._connectionChanged)

	def start(self):
		"""Start showing the status, call once :attr:`available` is connected."""
		self._connectionChanged(self._connection.uri)

	def _icon(self, name):
		label = QLabel()
		label.setPixmap(icon(name, color='#666', scale_factor=1).pixmap(self.ICON_SIZE, self.ICON_SIZE))
		return label

	@Slot(object)
	def _connectionChanged(self, uri):
		self.available.emit(False)
		self._timer.stop()
		if uri is not None and self._connection.findInterface(conf.PROTOCOL) is not None:
			self._timer.start()
			self._poll()

	@Slot()
	def _poll(self):
		interface = self._connection.findInterface(conf.PROTOCOL)
		uri = self._connection.uri
		if self._reading or interface is None or uri is None:
			return
		self._reading = True

		def read():
			try:
				values = conf.read_values(self._connection, interface, self.PATHS)
			except Exception as e:
				self._logger.debug(f'cannot read the device status: {e}')
				values = [None] * len(self.PATHS)
			self._read.emit(uri, values)

		threading.Thread(target=read, name='device-status', daemon=True).start()

	@Slot(str, list)
	def _show(self, uri, values):
		self._reading = False
		# Read for an earlier connection.
		if uri != self._connection.uri:
			return
		used, heap, uptime = (int(value) if value is not None and value.isdigit() else None for value in values)
		if used is not None and heap:
			self._heap.setText(f'{used * 100 // heap} %')
			self.setToolTip(self.tr('Heap: %d of %d B used') % (used, heap))
		else:
			self._heap.setText('-')
		self._uptime.setText(format_uptime(uptime) if uptime is not None else '-')
		self.available.emit(True)


class MainMenu(QToolBar):

	#: The Disconnect button was clicked, the device is to be disconnected.
	disconnectRequested = Signal()
	settingsRequested = Signal()
	quitRequested = Signal()
	changelogRequested = Signal()
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
		self._app_menu.settingsRequested.connect(self.settingsRequested)
		self._app_menu.quitRequested.connect(self.quitRequested)
		self._menu.addMenu(self._app_menu)
		self._menu.addMenu(DeviceMenu())
		self._help_menu = HelpMenu()
		self._help_menu.changelogRequested.connect(self.changelogRequested)
		self._help_menu.aboutRequested.connect(self.aboutRequested)
		self._menu.addMenu(self._help_menu)

		self.addWidget(self._menu)
		self.addSeparator()
		self._addConnectionBar()

	def _addConnectionBar(self):
		self._status = DeviceStatus(self._connection)
		self._status.available.connect(self.addWidget(self._status).setVisible)
		self._status.start()

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
			# Without the query parameters, e.g. the pack path of an SWD target, they are too long.
			self._state_btn.setText(uri.partition('?')[0])
			self._state_btn.setToolTip(uri)
			self._state_btn.setIcon(self._con_ok_icon)
			self._disconnect_btn.setEnabled(True)
		else:
			self._state_btn.setToolTip('')
			self._state_btn.setText('no connection')
			self._state_btn.setIcon(self._con_none_icon)
			self._disconnect_btn.setEnabled(False)

	@Slot()
	def _disconnectClicked(self):
		self.disconnectRequested.emit()
