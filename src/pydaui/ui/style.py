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


class DauiStyle(QProxyStyle):
	"""Fusion with flat dark MDI subwindow title bars and frames.

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

	def polish(self, widget):
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
		return super().pixelMetric(metric, opt, widget)

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
