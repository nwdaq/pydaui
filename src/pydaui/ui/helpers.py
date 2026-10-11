# SPDX-License-Identifier: GPL-3.0-or-later
#
# User interface helper widgets
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from PySide6.QtSvg import QSvgRenderer
from superqt.fonticon import icon, spin, pulse


#: Whether running on Android, where the screen is small and touched.
ANDROID = QSysInfo.productType() == 'android'


class Admonition(QFrame):

	MESSAGE = 1
	NOTE = 1
	WARN = 2
	#: Only the text in a gray frame, without an icon.
	NONE = 3

	def __init__(self, text, ad_type=0, center=False):
		"""``center`` centres the text horizontally."""
		super().__init__()

		if ad_type == Admonition.NONE:
			ad_icon = None
			self.setProperty('class', ['admonition', 'admonition-none'])
		elif ad_type == Admonition.NOTE:
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
			.admonition-none {
				border-color: #999;
			}

		''')

		l = QHBoxLayout()
		if ad_icon is not None:
			i = QLabel()
			i.setPixmap(ad_icon.pixmap(24, 24))
			l.addWidget(i)
		label = QLabel(text)
		label.setWordWrap(True)
		label.setOpenExternalLinks(True)
		if center:
			label.setAlignment(Qt.AlignCenter)
		l.addWidget(label, stretch=1)
		self.setLayout(l)
		self._label = label

	def setText(self, text):
		self._label.setText(text)



class _SvgIconEngine(QIconEngine):
	"""Renders an SVG at whatever size and pixel density the icon is requested at."""

	#: Opacity of the icon in the disabled mode.
	DISABLED_OPACITY = 0.4

	def __init__(self, renderer):
		super().__init__()
		self._renderer = renderer

	def paint(self, painter, rect, mode, state):
		if mode == QIcon.Disabled:
			painter.save()
			painter.setOpacity(painter.opacity() * self.DISABLED_OPACITY)
		self._renderer.render(painter, QRectF(rect))
		if mode == QIcon.Disabled:
			painter.restore()

	def pixmap(self, size, mode, state):
		pixmap = QPixmap(size)
		pixmap.fill(Qt.transparent)
		painter = QPainter(pixmap)
		self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
		painter.end()
		return pixmap

	def scaledPixmap(self, size, mode, state, scale):
		# Render directly at the device resolution instead of scaling a smaller pixmap up.
		pixmap = self.pixmap(size * scale, mode, state)
		pixmap.setDevicePixelRatio(scale)
		return pixmap

	def clone(self):
		return _SvgIconEngine(self._renderer)


class SvgIcon(QIcon):
	"""An icon rendered from an SVG, sharp at any size and pixel density.

	``svg`` is either the SVG document itself (a string starting with ``<``, or bytes) or the path of an
	SVG file (a string or a :class:`pathlib.Path`). Raises ``ValueError`` if it cannot be loaded.
	"""

	def __init__(self, svg):
		if isinstance(svg, str) and svg.lstrip().startswith('<'):
			svg = svg.encode()
		if isinstance(svg, (bytes, bytearray)):
			renderer = QSvgRenderer(QByteArray(bytes(svg)))
			source = 'inline document'
		else:
			renderer = QSvgRenderer(str(svg))
			source = str(svg)
		if not renderer.isValid():
			raise ValueError('cannot load the SVG icon (%s)' % source)
		engine = _SvgIconEngine(renderer)
		super().__init__(engine)
		# The icon takes over the engine, keep the Python wrappers alive along with it.
		self._engine = engine
		self._renderer = renderer



class DauiButton(QPushButton):

	def __init__(self, text=None, btn_icon=None, color='#000'):
		super().__init__()

		if type(btn_icon) == str:
			btn_icon = icon(f'mdi7.{btn_icon}', color=color, scale_factor=1)
		self.setIcon(btn_icon)

		if text:
			self.setText(text)

		self.setIconSize(QSize(24, 24))


class AppletWindow(QMdiSubWindow):

	#: The layers the windows are stacked in within the MDI area, from the bottom. The applets of the connected
	#: device and the windows they open.
	APPLET = 0
	#: The windows connecting the device, e.g. the connect window. On top of the applets, which are dimmed while
	#: one is shown.
	CONNECT = 1
	#: The windows of the application menus, e.g. the settings. Above the connect windows, usable while they
	#: are shown.
	TOOL = 2

	#: The layer of the window, one of the above.
	LAYER = APPLET

	#: Whether the window is shown maximized when its MDI area maximizes the windows. The small windows asking
	#: or telling something, e.g. a progress, are not.
	MAXIMIZE = True

	#: Whether the window stays on top of all the others, whatever their layers, e.g. a progress.
	ON_TOP = False

	def __init__(self, parent=None):
		super().__init__(parent=parent)

	def setVisible(self, visible):
		area = self.mdiArea()
		if visible:
			super().setVisible(True)
			if self._maximized() and not self.isMaximized():
				self.showMaximized()
			return
		if area is None or area.activeSubWindow() is not self:
			super().setVisible(False)
			return
		# QMdiArea keeps a hidden window active, deactivating it later shows it again. Deactivated first,
		# the previous window is activated instead once it is hidden.
		area.setActiveSubWindow(None)
		super().setVisible(False)
		area.activatePreviousSubWindow()

	def _maximized(self):
		"""Whether the window is shown maximized, the MDI area of the main window may maximize its windows."""
		area = self.mdiArea()
		return self.MAXIMIZE and area is not None and getattr(area, 'maximizesWindows', lambda: False)()

	def showCentered(self):
		"""Show the window in the middle of its MDI area and activate it, as large as it prefers but no larger
		than the area (e.g. a tablet in portrait). Unless it is shown maximized, resizing it would leave it
		maximized at the new size."""
		area = self.mdiArea()
		if area is not None and not self._maximized():
			viewport = area.viewport().size()
			self.resize(self.sizeHint().boundedTo(viewport))
			self.move(max(0, (viewport.width() - self.width()) // 2), max(0, (viewport.height() - self.height()) // 2))
		self.show()
		if area is not None:
			area.setActiveSubWindow(self)

	def moveEvent(self, event):
		super().moveEvent(event)

		if self.x() < 20:
			self.move(0, self.y())
		elif (self.x() + self.width()) >= (self.parent().width() - 20):
			self.move(self.parent().width() - self.width(), self.y())

		if self.y() < 20:
			self.move(self.x(), 0)
		elif (self.y() + self.height()) >= (self.parent().height() - 20):
			self.move(self.x(), self.parent().height() - self.height())


class StyledTreeWidget(QTreeWidget):
	"""The look shared by the applet trees and lists: padded alternating rows, a translucent tint over the
	row under the mouse, dim lines separating the columns and bold names in the first column."""

	#: Colour of the item names.
	NAME_COLOR = '#888'

	#: Colour of the vertical lines separating the columns.
	COLUMN_LINE_COLOR = '#e2e2e2'

	#: Relative widths of the columns, which always fill the whole width. ``None`` keeps the default widths.
	COLUMN_RATIOS = None

	#: Translucent tint painted over the row under the mouse.
	HOVER_COLOR = QColor(70, 130, 220, 32)

	STYLE = """
		QTreeWidget {
			outline: 0;
		}
		QTreeView::item {
			padding: 9px 8px;
		}
		QHeaderView::section {
			padding: 6px 8px;
			font-weight: bold;
		}
	"""

	def __init__(self):
		super().__init__()
		# The first column index of the row under the mouse, invalid when there is none.
		self._hover = QPersistentModelIndex()
		# Track the mouse to highlight the row under it. The row under a still mouse changes when the
		# tree scrolls too.
		self.setMouseTracking(True)
		self.verticalScrollBar().valueChanged.connect(self._updateHover)
		self.setAlternatingRowColors(True)
		# Scroll smoothly instead of by whole rows.
		self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
		self.setStyleSheet(self.STYLE)

	def styleName(self, item):
		"""Show the first column of ``item`` as a name."""
		font = item.font(0)
		font.setBold(True)
		item.setFont(0, font)
		item.setForeground(0, QColor(self.NAME_COLOR))

	def drawRow(self, painter, option, index):
		super().drawRow(painter, option, index)
		if self._hover.isValid() and index.siblingAtColumn(0) == QModelIndex(self._hover):
			# The whole row including the indentation, which the item hover state does not cover.
			painter.fillRect(option.rect, self.HOVER_COLOR)
		# Separate the columns with dim vertical lines, the rows join them to continuous lines.
		header = self.header()
		painter.save()
		painter.setPen(QPen(QColor(self.COLUMN_LINE_COLOR), 1))
		for column in range(header.count() - 1):
			if header.isSectionHidden(column):
				continue
			x = header.sectionViewportPosition(column) + header.sectionSize(column) - 1
			painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
		painter.restore()

	def mouseMoveEvent(self, event):
		self._updateHover()
		super().mouseMoveEvent(event)

	def leaveEvent(self, event):
		self._setHover(QModelIndex())
		super().leaveEvent(event)

	@Slot()
	def _updateHover(self):
		pos = self.viewport().mapFromGlobal(QCursor.pos())
		index = self.indexAt(pos) if self.viewport().rect().contains(pos) else QModelIndex()
		self._setHover(index)

	def _setHover(self, index):
		index = index.siblingAtColumn(0) if index.isValid() else QModelIndex()
		if index != QModelIndex(self._hover):
			self._hover = QPersistentModelIndex(index)
			self.viewport().update()

	def resizeEvent(self, event):
		super().resizeEvent(event)
		if self.COLUMN_RATIOS is None:
			return
		# Distribute the whole width among the columns, the last one takes the rounding remainder.
		width = self.viewport().width()
		total = sum(self.COLUMN_RATIOS)
		header = self.header()
		for column, ratio in enumerate(self.COLUMN_RATIOS[:-1]):
			header.resizeSection(column, width * ratio // total)
