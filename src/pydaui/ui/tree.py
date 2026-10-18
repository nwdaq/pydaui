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
