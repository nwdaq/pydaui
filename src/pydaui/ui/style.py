# SPDX-License-Identifier: GPL-3.0-or-later
#
# Application style
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon

from pydaui.ui.helpers import ANDROID


class DauiStyle(QProxyStyle):
	"""Fusion with flat dark MDI subwindow title bars and frames, and roomier menus.

	Stylesheets cannot do this, QStyleSheetStyle paints the title bar label
	over a hard-coded white rectangle.
	"""

	FRAME_COLOR = QColor('#444')
	TITLE_COLOR = QColor('#444')
	TEXT_COLOR = QColor('white')
	HOVER_COLOR = QColor('#888')
	TITLE_HEIGHT = 26
	TITLE_PADDING = 6
	#: Title bar button width relative to the title bar height
	BUTTON_ASPECT = 1.5
	#: Button icon size relative to the title bar height
	ICON_SCALE = 0.6

	#: Padding around the main menu bar item text, horizontal and vertical
	MENU_BAR_ITEM_PADDING = QSize(10, 7)
	MENU_BAR_HOVER_COLOR = QColor('#ddd')

	#: Popup menu item height
	MENU_ITEM_HEIGHT = 32
	#: Width of the column on the left reserved for icons and check marks
	MENU_ICON_COLUMN = 36
	MENU_ICON_SIZE = 18
	#: Space between the label and the shortcut column and after it
	MENU_ITEM_PADDING = 16
	#: Minimal width of an open menu
	MENU_MIN_WIDTH = 300

	#: The width of the scroll bars on Android, they only indicate the position there, the content scrolls
	#: by touch.
	ANDROID_SCROLL_BAR_EXTENT = 4

	#: Title bar buttons and their icons
	BUTTONS = {
		QStyle.SC_TitleBarCloseButton: 'mdi7.window-close',
		QStyle.SC_TitleBarMaxButton: 'mdi7.window-maximize',
		QStyle.SC_TitleBarMinButton: 'mdi7.window-minimize',
		QStyle.SC_TitleBarNormalButton: 'mdi7.window-restore',
		QStyle.SC_TitleBarShadeButton: 'mdi7.chevron-up',
		QStyle.SC_TitleBarUnshadeButton: 'mdi7.chevron-down',
		QStyle.SC_TitleBarContextHelpButton: 'mdi7.help',
	}

	def __init__(self):
		super().__init__('Fusion')
		self._icons = {}

	def _icon(self, name):
		if name not in self._icons:
			self._icons[name] = icon(name, color=self.TEXT_COLOR.name())
		return self._icons[name]

	@staticmethod
	def _touchScrolled(widget):
		"""Whether ``widget`` is a scroll area scrolled by touch on Android. Not the MDI area, dragging a
		window over it would scroll it."""
		return ANDROID and isinstance(widget, QAbstractScrollArea) and not isinstance(widget, QMdiArea)

	def _scrollTouch(self, area):
		"""Scroll the scroll area ``area`` by dragging its content, with momentum."""
		viewport = area.viewport()
		# On the mouse events Android synthesizes from the touches. The scroller holds a press back until it
		# tells a tap from a drag, a drag scrolls without the press reaching the content (selecting a row). A
		# touch gesture would leave the press to the content and start late.
		QScroller.grabGesture(viewport, QScroller.LeftMouseButtonGesture)
		properties = QScroller.scroller(viewport).scrollerProperties()
		# A light bounce at the ends and a short delay before the press goes to the content, a tap selecting a
		# row stays apart from the start of a scroll.
		properties.setScrollMetric(QScrollerProperties.OvershootDragResistanceFactor, 0.3)
		properties.setScrollMetric(QScrollerProperties.OvershootScrollDistanceFactor, 0.1)
		properties.setScrollMetric(QScrollerProperties.MousePressEventDelay, 0.1)
		QScroller.scroller(viewport).setScrollerProperties(properties)

	def polish(self, widget):
		if isinstance(widget, QMenu):
			widget.setMinimumWidth(self.MENU_MIN_WIDTH)
		if self._touchScrolled(widget):
			self._scrollTouch(widget)
		if ANDROID and isinstance(widget, QLabel):
			# Dragging over a label scrolls, it does not select its text. The links stay clickable.
			widget.setTextInteractionFlags(widget.textInteractionFlags() & ~Qt.TextSelectableByMouse)
		if isinstance(widget, QMdiSubWindow):
			widget.installEventFilter(self)
			for child in widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
				child.installEventFilter(self)
		return super().polish(widget)

	def unpolish(self, widget):
		if isinstance(widget, QMdiSubWindow):
			widget.removeEventFilter(self)
			for child in widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
				child.removeEventFilter(self)
		return super().unpolish(widget)

	def eventFilter(self, obj, event):
		# QMdiSubWindow clears the title bar hover only on a mouse move over itself or
		# when the mouse leaves it. Moving from a button right into the content widget
		# does neither, tell the subwindow the mouse has left.
		if isinstance(obj, QMdiSubWindow):
			if event.type() == QEvent.ChildAdded and event.child().isWidgetType():
				event.child().installEventFilter(self)
		elif event.type() == QEvent.Enter and isinstance(obj.parent(), QMdiSubWindow):
			QApplication.sendEvent(obj.parent(), QEvent(QEvent.Leave))
		return False

	def pixelMetric(self, metric, opt=None, widget=None):
		if metric == QStyle.PM_TitleBarHeight:
			return self.TITLE_HEIGHT
		if metric == QStyle.PM_MdiSubWindowFrameWidth:
			return 1
		if metric == QStyle.PM_MenuBarItemSpacing:
			return 0
		if metric == QStyle.PM_ScrollBarExtent and isinstance(widget, QScrollBar) and self._touchScrolled(
		        self._scrollArea(widget)):
			return self.ANDROID_SCROLL_BAR_EXTENT
		return super().pixelMetric(metric, opt, widget)

	@staticmethod
	def _scrollArea(scroll_bar):
		"""The scroll area ``scroll_bar`` belongs to, if any. Its parent is a container of the area."""
		container = scroll_bar.parentWidget()
		return container.parentWidget() if container is not None else None

	def sizeFromContents(self, ct, opt, size, widget=None):
		size = super().sizeFromContents(ct, opt, size, widget)
		if ct == QStyle.CT_MenuBarItem:
			size += self.MENU_BAR_ITEM_PADDING * 2
		elif self._isMenuItem(opt, widget):
			# The menu adds the widest shortcut on the right itself.
			width = self.MENU_ICON_COLUMN + size.width() + 2 * self.MENU_ITEM_PADDING
			size = QSize(width, self.MENU_ITEM_HEIGHT)
		return size

	@staticmethod
	def _isMenuItem(opt, widget):
		"""Whether ``opt`` is a popup menu item other than a separator, they are drawn by :meth:`_drawMenuItem`."""
		return (isinstance(widget, QMenu) and isinstance(opt, QStyleOptionMenuItem)
			and opt.menuItemType != QStyleOptionMenuItem.Separator)

	def styleHint(self, hint, opt=None, widget=None, ret=None):
		# No rounded corners
		if hint == QStyle.SH_WindowFrame_Mask:
			return 0
		return super().styleHint(hint, opt, widget, ret)

	@staticmethod
	def _buttonVisible(sc, opt):
		"""Whether a title bar button is shown, the same rules QCommonStyle uses."""
		flags = opt.titleBarFlags
		minimized = bool(opt.titleBarState & Qt.WindowMinimized.value)
		maximized = bool(opt.titleBarState & Qt.WindowMaximized.value)
		if sc == QStyle.SC_TitleBarCloseButton:
			return bool(flags & Qt.WindowSystemMenuHint)
		if sc == QStyle.SC_TitleBarMaxButton:
			return bool(flags & Qt.WindowMaximizeButtonHint) and not maximized
		if sc == QStyle.SC_TitleBarMinButton:
			return bool(flags & Qt.WindowMinimizeButtonHint) and not minimized
		if sc == QStyle.SC_TitleBarNormalButton:
			return (bool(flags & Qt.WindowMinimizeButtonHint) and minimized) or (bool(flags & Qt.WindowMaximizeButtonHint) and maximized)
		if sc == QStyle.SC_TitleBarShadeButton:
			return bool(flags & Qt.WindowShadeButtonHint) and not minimized
		if sc == QStyle.SC_TitleBarUnshadeButton:
			return bool(flags & Qt.WindowShadeButtonHint) and minimized
		if sc == QStyle.SC_TitleBarContextHelpButton:
			return bool(flags & Qt.WindowContextHelpButtonHint)
		return False

	def _buttonOrder(self, opt):
		"""Visible title bar buttons from right to left."""
		maximized = bool(opt.titleBarState & Qt.WindowMaximized.value)
		order = [
			QStyle.SC_TitleBarCloseButton,
			QStyle.SC_TitleBarNormalButton if maximized else QStyle.SC_TitleBarMaxButton,
			QStyle.SC_TitleBarMaxButton if maximized else QStyle.SC_TitleBarNormalButton,
			QStyle.SC_TitleBarMinButton,
			QStyle.SC_TitleBarShadeButton,
			QStyle.SC_TitleBarUnshadeButton,
			QStyle.SC_TitleBarContextHelpButton,
		]
		return [sc for sc in order if self._buttonVisible(sc, opt)]

	def subControlRect(self, control, opt, sc, widget=None):
		# QMdiSubWindow asks without the widget when building its move region.
		if control != QStyle.CC_TitleBar or not (widget is None or isinstance(widget, QMdiSubWindow)):
			return super().subControlRect(control, opt, sc, widget)

		# Buttons span the whole title bar height and sit next to each other
		# at the right edge, the label takes the rest.
		r = opt.rect
		width = round(r.height() * self.BUTTON_ASPECT)
		right = r.right() + 1
		for button in self._buttonOrder(opt):
			right -= width
			if button == sc:
				return QRect(right, r.top(), width, r.height())
		if sc == QStyle.SC_TitleBarLabel:
			return QRect(r.left() + self.TITLE_PADDING, r.top(), right - r.left() - 2 * self.TITLE_PADDING, r.height())
		return QRect()

	def drawControl(self, element, opt, painter, widget=None):
		# Fusion highlights only the open menu bar item, highlight the hovered one
		# the same way in gray.
		if element == QStyle.CE_MenuBarItem and opt.state & QStyle.State_Selected and opt.state & QStyle.State_Enabled and not opt.state & QStyle.State_Sunken:
			opt = QStyleOptionMenuItem(opt)
			opt.state |= QStyle.State_Sunken
			opt.palette.setColor(QPalette.Highlight, self.MENU_BAR_HOVER_COLOR)
			opt.palette.setColor(QPalette.HighlightedText, opt.palette.color(QPalette.WindowText))
		elif element == QStyle.CE_MenuItem and self._isMenuItem(opt, widget):
			self._drawMenuItem(opt, painter, widget)
			return
		super().drawControl(element, opt, painter, widget)

	def _drawMenuItem(self, opt, painter, widget):
		"""A popup menu item: an icon or a check mark, the label and the shortcut aligned right."""
		r = opt.rect
		enabled = bool(opt.state & QStyle.State_Enabled)
		selected = enabled and bool(opt.state & QStyle.State_Selected)
		painter.save()

		if selected:
			# The same highlight Fusion draws
			highlight = opt.palette.color(QPalette.Highlight)
			painter.fillRect(r, highlight)
			painter.setPen(highlight.darker(125))
			painter.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))

		icon_col = QRect(r.left(), r.top(), self.MENU_ICON_COLUMN, r.height())
		checked = opt.checkType != QStyleOptionMenuItem.NotCheckable and opt.checked
		if not opt.icon.isNull():
			if checked:
				frame = QStyleOption(opt)
				frame.rect = icon_col.adjusted(3, 3, -3, -3)
				frame.state |= QStyle.State_Sunken
				self.drawPrimitive(QStyle.PE_PanelButtonCommand, frame, painter, widget)
			mode = QIcon.Disabled if not enabled else QIcon.Active if selected else QIcon.Normal
			pixmap = opt.icon.pixmap(QSize(self.MENU_ICON_SIZE, self.MENU_ICON_SIZE), painter.device().devicePixelRatio(), mode)
			pr = QRect(QPoint(0, 0), pixmap.deviceIndependentSize().toSize())
			pr.moveCenter(icon_col.center())
			painter.drawPixmap(pr.topLeft(), pixmap)
		elif opt.checkType != QStyleOptionMenuItem.NotCheckable:
			box = QStyleOptionButton()
			box.initFrom(widget)
			box.state = opt.state | (QStyle.State_On if checked else QStyle.State_Off)
			box.palette = opt.palette
			box.rect = QRect(0, 0, 14, 14)
			box.rect.moveCenter(icon_col.center())
			exclusive = opt.checkType == QStyleOptionMenuItem.Exclusive
			self.drawPrimitive(QStyle.PE_IndicatorRadioButton if exclusive else QStyle.PE_IndicatorCheckBox, box, painter, widget)

		color = opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text)
		flags = Qt.AlignVCenter | Qt.TextSingleLine
		flags |= Qt.TextShowMnemonic if self.styleHint(QStyle.SH_UnderlineShortcut, opt, widget) else Qt.TextHideMnemonic
		label, _, shortcut = opt.text.partition('\t')
		font = QFont(opt.font)
		font.setBold(opt.menuItemType == QStyleOptionMenuItem.DefaultItem)
		painter.setFont(font)

		right = r.right() - self.MENU_ITEM_PADDING
		if opt.menuItemType == QStyleOptionMenuItem.SubMenu:
			arrow = QStyleOption(opt)
			arrow.rect = QRect(right - 8, r.top(), 8, r.height())
			arrow.palette.setColor(QPalette.ButtonText, color)
			self.drawPrimitive(QStyle.PE_IndicatorArrowRight, arrow, painter, widget)
		elif shortcut:
			# Shortcuts are a bit dimmer than the label
			dim = QColor(color)
			if not selected:
				dim.setAlpha(150)
			painter.setPen(dim)
			painter.drawText(QRect(r.left(), r.top(), right - r.left(), r.height()), flags | Qt.AlignRight, shortcut)

		painter.setPen(color)
		label_rect = QRect(icon_col.right() + 1, r.top(), right - icon_col.right(), r.height())
		painter.drawText(label_rect, flags | Qt.AlignLeft, label)
		painter.restore()

	def drawPrimitive(self, element, opt, painter, widget=None):
		if element == QStyle.PE_FrameWindow and isinstance(widget, QMdiSubWindow):
			painter.save()
			painter.setPen(self.FRAME_COLOR)
			painter.setBrush(Qt.NoBrush)
			painter.drawRect(opt.rect.adjusted(0, 0, -1, -1))
			painter.restore()
			return
		super().drawPrimitive(element, opt, painter, widget)

	def drawComplexControl(self, control, opt, painter, widget=None):
		if control != QStyle.CC_TitleBar or not isinstance(widget, QMdiSubWindow):
			super().drawComplexControl(control, opt, painter, widget)
			return

		painter.save()
		painter.fillRect(opt.rect, self.TITLE_COLOR)

		hover = opt.activeSubControls if opt.state & (QStyle.State_MouseOver | QStyle.State_Sunken) else QStyle.SC_None
		for sc, name in self.BUTTONS.items():
			r = self.subControlRect(control, opt, sc, widget)
			if not opt.subControls & sc or not r.isValid():
				continue
			if hover & sc:
				painter.fillRect(r, self.HOVER_COLOR)
			size = round(r.height() * self.ICON_SCALE)
			self._icon(name).paint(painter, QRect(r.center() - QPoint(size // 2, size // 2), QSize(size, size)))

		label = self.subControlRect(control, opt, QStyle.SC_TitleBarLabel, widget)
		font = QFont(painter.font())
		font.setBold(True)
		painter.setFont(font)
		painter.setPen(self.TEXT_COLOR)
		text = QFontMetrics(font).elidedText(opt.text, Qt.ElideRight, label.width())
		painter.drawText(label, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)
		painter.restore()
