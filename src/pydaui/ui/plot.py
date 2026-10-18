# SPDX-License-Identifier: GPL-3.0-or-later
#
# The look of the graphs
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The look shared by the graphs of the applets, e.g. of the MQ live data and of the calibration
certificate."""

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase


#: The colours of the graph lines from the Tango palette, taken in turn: the middle shades of the hues first
#: (the darker ones of the butter and chameleon, the middle ones are too light on white), then the dark and
#: the light shades.
GRAPH_COLORS = (
	'#3465a4', '#cc0000', '#4e9a06', '#f57900', '#75507b', '#c17d11', '#c4a000', '#555753',
	'#204a87', '#a40000', '#ce5c00', '#5c3566', '#8f5902', '#2e3436',
	'#729fcf', '#ef2929', '#73d216', '#fcaf3e', '#ad7fa8', '#e9b96e', '#888a85',
)


def monospace_font():
	"""The monospace font of the size of the user interface font, of the numbers and the graphs."""
	font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
	font.setPointSizeF(QApplication.font().pointSizeF())
	return font


class Plot(pg.PlotWidget):
	"""The look of the graphs: a white background with a light grid, the axes and the legend in the
	:func:`monospace_font`. The mouse neither moves nor zooms the graph, its menu and the auto range button are
	hidden. ``bottom`` is the :class:`pyqtgraph.AxisItem` of the horizontal axis, the default one if ``None``."""

	#: Colours of the axes, of their text and of the frame of the legend.
	AXIS_COLOR = '#888'
	TEXT_COLOR = '#333'
	BORDER_COLOR = '#d6d6d6'

	#: The width of the lines, in pixels.
	LINE_WIDTH = 1.5

	def __init__(self, bottom=None):
		super().__init__(background='w', axisItems=None if bottom is None else {'bottom': bottom})
		self._font = monospace_font()
		item = self.getPlotItem()
		item.setMenuEnabled(False)
		item.hideButtons()
		item.showGrid(x=True, y=True, alpha=0.3)
		for name in ('bottom', 'left'):
			axis = item.getAxis(name)
			axis.setPen(pg.mkPen(self.AXIS_COLOR))
			axis.setTextPen(pg.mkPen(self.TEXT_COLOR))
			axis.setTickFont(self._font)
			axis.label.setFont(self._font)
			# The values have no units, a scale like "(x0.001)" by the label would be confusing.
			axis.enableAutoSIPrefix(False)
		self._legend = item.addLegend(offset=(10, 10), brush=pg.mkBrush(255, 255, 255, 220),
		                              pen=pg.mkPen(self.BORDER_COLOR), labelTextColor=self.TEXT_COLOR,
		                              labelTextSize=f'{self._font.pointSizeF():g}pt')
		self.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)

	def setAxisLabel(self, axis, text):
		"""Label the ``axis``, ``'bottom'`` or ``'left'``, with ``text``."""
		self.getPlotItem().setLabel(axis, text, color=self.TEXT_COLOR)

	def addLine(self, item):
		"""Add ``item`` and its name to the legend in the monospace font."""
		self.addItem(item)
		label = self._legend.getLabel(item)
		if label is not None:
			label.item.setFont(self._font)
			label.setText(item.name())
