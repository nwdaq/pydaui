# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tree widget with guide lines
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import StyledTreeWidget
from pydaui.ui.icons import Icons


class GuideTreeWidget(StyledTreeWidget):
	"""The look of the applet trees: :class:`StyledTreeWidget` with guide lines connecting the items to their
	parents and boxed plus/minus expand/collapse indicators over them."""

	#: Size of the expand/collapse indicators, in pixels.
	BRANCH_ICON_SIZE = 12

	#: Colour of the tree guide lines.
	LINE_COLOR = '#c8c8c8'

	STYLE = StyledTreeWidget.STYLE + """
		/* The expand/collapse indicators are painted by drawBranches(). */
		QTreeView::branch:has-children:closed,
		QTreeView::branch:has-children:open {
			image: none;
			border-image: none;
		}
	"""

	def __init__(self):
		super().__init__()
		self._expand_icon = Icons.expand()
		self._collapse_icon = Icons.collapse()
		self.setIndentation(22)

	def _has_next_visible(self, index):
		"""Whether a sibling shown in the tree follows the item at ``index``."""
		parent = index.parent()
		for row in range(index.row() + 1, self.model().rowCount(parent)):
			if not self.isRowHidden(row, parent):
				return True
		return False

	def _has_prev_visible(self, index):
		"""Whether a sibling shown in the tree precedes the item at ``index``."""
		parent = index.parent()
		return any(not self.isRowHidden(row, parent) for row in range(index.row()))

	def drawBranches(self, painter, rect, index):
		# The row background of the branch area, the default indicators are hidden by the style sheet.
		super().drawBranches(painter, rect, index)

		# The item itself occupies the last indentation step right before it, its ancestors the steps
		# to the left, one per level.
		step = self.indentation()
		left = rect.right() + 1 - step
		cx = left + step // 2
		# Centered on the cells, not on the detail line below them.
		cy = rect.top() + (rect.height() - self.detailHeight(index)) // 2

		# Guide lines: a vertical line through the levels which continue below this row and a short
		# horizontal leaf to the item.
		painter.save()
		painter.setPen(QPen(QColor(self.LINE_COLOR), 1))
		# The first top level item has nothing above it to connect to.
		top = rect.top() if index.parent().isValid() or self._has_prev_visible(index) else cy
		bottom = rect.bottom() if self._has_next_visible(index) else cy
		if top != bottom:
			painter.drawLine(cx, top, cx, bottom)
		painter.drawLine(cx, cy, left + step - 2, cy)
		ancestor, x = index.parent(), cx - step
		while ancestor.isValid():
			if self._has_next_visible(ancestor):
				painter.drawLine(x, rect.top(), x, rect.bottom())
			ancestor, x = ancestor.parent(), x - step
		painter.restore()

		if not self.model().hasChildren(index):
			return
		# The expand/collapse indicator of the item, over the guide lines.
		icon = self._collapse_icon if self.isExpanded(index) else self._expand_icon
		icon.paint(painter, self._indicator_rect(cx, cy))

	def _indicator_rect(self, cx, cy):
		"""The rectangle of an expand/collapse indicator centered at (cx, cy)."""
		size = self.BRANCH_ICON_SIZE
		return QRect(cx - size // 2, cy - size // 2, size, size)

	def _over_indicator(self, pos):
		"""Whether the viewport position ``pos`` is over the expand/collapse indicator of an item."""
		index = self.indexAt(pos)
		if not index.isValid() or not self.model().hasChildren(index):
			return False
		# The indicator sits centered in the indentation step right before the item, see drawBranches().
		rect = self.visualRect(index.siblingAtColumn(0))
		step = self.indentation()
		cx = rect.left() - step + step // 2
		cy = rect.top() + (rect.height() - self.detailHeight(index)) // 2
		return self._indicator_rect(cx, cy).adjusted(-2, -2, 2, 2).contains(pos)

	def mouseMoveEvent(self, event):
		if self._over_indicator(event.position().toPoint()):
			self.viewport().setCursor(Qt.PointingHandCursor)
		else:
			self.viewport().unsetCursor()
		super().mouseMoveEvent(event)


class RowDelegate(QStyledItemDelegate):
	"""Delegate of the cells of a :class:`DetailTreeWidget`: makes the rows with a detail line taller, keeping
	the cell texts on the first line, see :meth:`DetailTreeWidget.detailHeight`."""

	def __init__(self, tree):
		super().__init__(tree)
		self._tree = tree

	def sizeHint(self, option, index):
		size = super().sizeHint(option, index)
		size.setHeight(size.height() + self._tree.detailHeight(index))
		return size

	def paint(self, painter, option, index):
		height = self._tree.detailHeight(index)
		if not height:
			super().paint(painter, option, index)
			return
		# The background (e.g. of a selected row) over the whole cell, the cell itself above the detail line.
		background = QStyleOptionViewItem(option)
		self.initStyleOption(background, index)
		background.text = ''
		style = option.widget.style() if option.widget is not None else QApplication.style()
		style.drawPrimitive(QStyle.PE_PanelItemViewItem, background, painter, option.widget)
		cell = QStyleOptionViewItem(option)
		cell.rect = QRect(option.rect.left(), option.rect.top(), option.rect.width(), option.rect.height() - height)
		super().paint(painter, cell, index)


class DetailTreeWidget(GuideTreeWidget):
	"""A :class:`GuideTreeWidget` which can show the full description of each item below it, in a bubble
	word-wrapped across all the columns (see :meth:`setShowDetails`). The description is the
	:attr:`DETAIL_ROLE` data of the first column. The cells need a :class:`RowDelegate`."""

	#: The role of the full description of an item, empty if it has none.
	DETAIL_ROLE = Qt.UserRole

	#: Colour of the full descriptions, not to dominate over the values.
	DETAIL_COLOR = '#444'

	#: The horizontal padding of the full description bubble, where the cells draw their texts, and its
	#: padding above (below the bottom padding of the cells) and below.
	DETAIL_HPADDING = 4
	DETAIL_TOP_PADDING = 2
	DETAIL_BOTTOM_PADDING = 9

	#: The look of the bubble, of a note Admonition on the edit field background: its border, corner radius,
	#: the size of its icon and its padding around the icon and the text.
	DETAIL_BORDER = '#008'
	DETAIL_RADIUS = 5
	DETAIL_ICON_SIZE = 20
	DETAIL_MARGIN = 8

	def __init__(self):
		super().__init__()
		# Whether the full descriptions are shown below the items.
		self._details = False
		self._detail_icon = Icons.about()

	def setShowDetails(self, show):
		"""Show the full description of each item below it, word-wrapped across all the columns."""
		self._details = show
		# The row heights change.
		self.scheduleDelayedItemsLayout()

	def _detailRect(self, index):
		"""The left and the width of the full description bubble of the item at ``index``, aligned with its
		name."""
		depth = 0
		parent = index.parent()
		while parent.isValid():
			depth, parent = depth + 1, parent.parent()
		indent = self.indentation() * (depth + (1 if self.rootIsDecorated() else 0))
		left = self.header().sectionViewportPosition(0) + indent + self.DETAIL_HPADDING
		return left, max(self.viewport().width() - self.DETAIL_HPADDING - left, 1)

	def detailHeight(self, index):
		if not self._details or not index.isValid():
			return 0
		detail = index.siblingAtColumn(0).data(self.DETAIL_ROLE)
		if not detail:
			return 0
		bubble = self._detailTextHeight(index, detail) + 2 * self.DETAIL_MARGIN
		return self.DETAIL_TOP_PADDING + max(bubble, self.DETAIL_ICON_SIZE + 2 * self.DETAIL_MARGIN) + \
		       self.DETAIL_BOTTOM_PADDING

	def _detailTextWidth(self, index):
		"""The width of the text in the bubble, right of the icon."""
		left, width = self._detailRect(index)
		return max(width - self.DETAIL_ICON_SIZE - 3 * self.DETAIL_MARGIN, 1)

	def _detailTextHeight(self, index, detail):
		rect = QRect(0, 0, self._detailTextWidth(index), 100000)
		return self.fontMetrics().boundingRect(rect, Qt.TextWordWrap, detail).height()

	def drawRow(self, painter, option, index):
		super().drawRow(painter, option, index)
		height = self.detailHeight(index)
		if not height:
			return
		left, width = self._detailRect(index)
		bubble = QRect(left, option.rect.bottom() + 1 - height + self.DETAIL_TOP_PADDING,
		               width, height - self.DETAIL_TOP_PADDING - self.DETAIL_BOTTOM_PADDING)
		painter.save()
		painter.setRenderHint(QPainter.Antialiasing)
		painter.setPen(QPen(QColor(self.DETAIL_BORDER), 1))
		painter.setBrush(option.palette.color(QPalette.Base))
		# On the pixel centres, the border is sharp.
		painter.drawRoundedRect(QRectF(bubble).adjusted(0.5, 0.5, -0.5, -0.5), self.DETAIL_RADIUS, self.DETAIL_RADIUS)
		# The icon centred vertically, as in an Admonition.
		size = self.DETAIL_ICON_SIZE
		self._detail_icon.paint(painter, QRect(bubble.left() + self.DETAIL_MARGIN,
		                                       bubble.top() + (bubble.height() - size) // 2, size, size))
		text_left = bubble.left() + 2 * self.DETAIL_MARGIN + size
		painter.setFont(self.font())
		painter.setPen(QColor(self.DETAIL_COLOR))
		painter.drawText(QRect(text_left, bubble.top() + self.DETAIL_MARGIN, self._detailTextWidth(index),
		                       bubble.height() - 2 * self.DETAIL_MARGIN),
		                 Qt.TextWordWrap | Qt.AlignVCenter, index.siblingAtColumn(0).data(self.DETAIL_ROLE))
		painter.restore()

	def resizeEvent(self, event):
		super().resizeEvent(event)
		if self._details:
			# The full descriptions wrap to the new width.
			self.scheduleDelayedItemsLayout()
