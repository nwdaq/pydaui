# SPDX-License-Identifier: GPL-3.0-or-later
#
# Ribbon panel
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import logging
import threading

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.applets import registry


class RibbonPanel(QDockWidget):
	"""A ribbon panel with a button for every instance of its applets the connected device offers.

	The panels are dock widgets, so they can be placed side by side, grouped into tabs or floated. When
	an applet is offered more than once (e.g. for the main device and its daughter cards), each of its
	buttons names the unit it opens the applet for.
	"""

	appletRequested = Signal(object)

	def __init__(self, name, panel_icon):
		super().__init__(name)
		# Identifies the panel in the saved window state.
		self.setObjectName(f'ribbon.{name}')
		self.setWindowIcon(panel_icon)
		self.setAllowedAreas(Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea)
		self._title_bar = self._titleBar(name, panel_icon)
		# Shown instead while the panel is grouped into tabs, the tab names the panel already.
		self._no_title_bar = QWidget()
		self.setTitleBarWidget(self._title_bar)

		self._bar = QToolBar()
		self._bar.setIconSize(QSize(30, 30))
		# All the buttons are as high as those with a unit name, so the panel keeps its height whatever it
		# shows.
		probe = QToolButton()
		probe.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
		probe.setIconSize(self._bar.iconSize())
		probe.setIcon(panel_icon)
		probe.setText('\n')
		self._button_height = probe.sizeHint().height()
		margins = self._bar.contentsMargins()
		self._bar.setFixedHeight(self._button_height + margins.top() + margins.bottom() + 4)

		# Separates the panel from the next one while they are side by side.
		self._separator = QFrame()
		self._separator.setFrameShape(QFrame.VLine)
		self._separator.setStyleSheet('color: #bbb;')

		content = QWidget()
		layout = QHBoxLayout(content)
		layout.setContentsMargins(0, 0, 0, 6)
		layout.setSpacing(4)
		layout.addWidget(self._bar)
		layout.addWidget(self._separator)
		# No wider than the buttons, the panels stay packed to the left instead of sharing the width.
		content.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
		self.setWidget(content)
		self.setGroups([], None)

	def _titleBar(self, name, panel_icon):
		"""A compact title bar, the icon and name of the panel in small gray letters."""
		bar = QWidget()
		layout = QHBoxLayout(bar)
		layout.setContentsMargins(4, 2, 4, 0)
		layout.setSpacing(4)
		pixmap = QLabel()
		pixmap.setPixmap(panel_icon.pixmap(14, 14))
		layout.addWidget(pixmap)
		title = QLabel(name)
		title.setStyleSheet('color: #666; font-size: 8pt;')
		layout.addWidget(title, stretch=1)
		return bar

	def setTabbed(self, tabbed):
		title_bar = self._no_title_bar if tabbed else self._title_bar
		if self.titleBarWidget() is not title_bar:
			self.setTitleBarWidget(title_bar)
		self._separator.setVisible(not tabbed and not self.isFloating())

	def setGroups(self, groups, uri, probing=False):
		"""Show a button for every applet instance in ``groups``, as returned by :func:`registry.available`.
		``probing`` tells the device is being asked for more applets of the panel."""
		self._bar.clear()
		for group in groups:
			if self._bar.actions():
				self._bar.addSeparator()
			for instance in group:
				self._bar.addWidget(self._button(instance, marked=len(group) > 1))

		if not groups:
			if uri is None:
				text = self.tr('Not connected')
			else:
				text = self.tr('Checking...') if probing else self.tr('Not available')
			hint = QLabel(text)
			hint.setStyleSheet('color: #888; padding: 8px;')
			self._bar.addWidget(hint)

		# Always show all the buttons, without the overflow menu.
		self._bar.setMinimumWidth(self._bar.sizeHint().width())

	def _button(self, instance, marked):
		button = QToolButton()
		button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
		button.setIcon(instance.applet.icon())
		text = instance.applet.title
		if marked:
			text += '\n' + (instance.unit or self.tr('main device'))
		button.setText(text)
		button.setToolTip(self.tr('%s on interface "%s"') % (instance.applet.title, instance.interface))
		button.setFixedHeight(self._button_height)
		button.clicked.connect(lambda: self.appletRequested.emit(instance))
		return button


class _Prober(QObject):
	"""Runs the probes of the applet instances of :func:`registry.candidates` off the GUI thread, one after
	another, the device answers one request at a time anyway. ``found`` carries each instance whose probe
	returns a context, with the context set, ``done`` ends it."""

	found = Signal(object)
	done = Signal()

	def __init__(self, connection, instances):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._connection = connection
		self._instances = instances

	def start(self):
		threading.Thread(target=self._run, name='applet-probe', daemon=True).start()

	def _run(self):
		for instance in self._instances:
			try:
				context = instance.applet.probe(self._connection, instance.interface)
			except Exception as e:
				self._logger.info(f'{instance.applet.title} on interface {instance.interface!r} not available: {e}')
				continue
			if context is None:
				self._logger.info(f'{instance.applet.title} on interface {instance.interface!r} not available')
				continue
			self._logger.info(f'{instance.applet.title} available on interface {instance.interface!r}')
			self.found.emit(registry.probed(instance, context))
		self.done.emit()


class Ribbon(QObject):
	"""Adds a :class:`RibbonPanel` for each of :data:`registry.PANELS` to the top of ``window``, side by
	side, and refills them on every connection change."""

	appletRequested = Signal(object)

	def __init__(self, connection, window):
		super().__init__(window)
		self._connection = connection
		self._window = window

		# Grouped dragging lets a panel be pulled out of a tab group by its tab, the tabbed panels have no
		# title bar to drag them by.
		window.setDockOptions(window.dockOptions() | QMainWindow.AllowNestedDocks | QMainWindow.GroupedDragging)
		window.setTabPosition(Qt.TopDockWidgetArea, QTabWidget.North)

		# The applet instances offered, grouped like registry.available() returns them, and the prober
		# asking the device for more, None when it is not asking.
		self._groups = []
		self._prober = None
		self._probing = set()

		self._panels = {}
		for name, panel_icon in registry.PANELS:
			panel = RibbonPanel(name, panel_icon())
			panel.appletRequested.connect(self.appletRequested)
			window.addDockWidget(Qt.TopDockWidgetArea, panel, Qt.Horizontal)
			# Dragging a panel floats it until dropped, possibly onto another one.
			panel.topLevelChanged.connect(self._arrangementChanged)
			panel.dockLocationChanged.connect(self._arrangementChanged)
			self._panels[name] = panel
		# Also once the window restores the saved arrangement.
		self._arrangementChanged()

		self._connection.connectionChanged.connect(self._connectionChanged)
		self._connectionChanged(self._connection.uri)

	@Slot()
	def _arrangementChanged(self):
		# The window finishes rearranging the panels only after the signal.
		QTimer.singleShot(0, self._updateTitleBars)

	def _updateTitleBars(self):
		for panel in self._panels.values():
			panel.setTabbed(not panel.isFloating() and bool(self._window.tabifiedDockWidgets(panel)))

	@Slot(object)
	def _connectionChanged(self, uri):
		if self._prober is not None:
			# Its results are for the previous connection.
			self._prober.blockSignals(True)
			self._prober = None
		self._groups = registry.available(self._connection.interfaces)
		candidates = registry.candidates(self._connection.interfaces) if uri is not None else []
		# The panels the prober may add to.
		self._probing = {group[0].applet.panel for group in candidates}
		if candidates:
			self._prober = _Prober(self._connection, [instance for group in candidates for instance in group])
			self._prober.found.connect(self._found)
			self._prober.done.connect(self._probed)
			self._prober.start()
		self._updatePanels()

	@Slot(object)
	def _found(self, instance):
		for group in self._groups:
			if group[0].applet is instance.applet:
				group.append(instance)
				break
		else:
			self._groups = registry.order(self._groups + [[instance]])
		self._updatePanels()

	@Slot()
	def _probed(self):
		self._prober = None
		self._probing = set()
		self._updatePanels()

	def _updatePanels(self):
		for name, panel in self._panels.items():
			panel.setGroups([group for group in self._groups if group[0].applet.panel == name], self._connection.uri,
			                probing=name in self._probing)
