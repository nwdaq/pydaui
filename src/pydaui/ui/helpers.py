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


class Admonition(QFrame):

	MESSAGE = 1
	NOTE = 1
	WARN = 2

	def __init__(self, text, ad_type=0):
		super().__init__()

		if ad_type == Admonition.NOTE:
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

		''')

		l = QHBoxLayout()
		i = QLabel()
		i.setPixmap(ad_icon.pixmap(24, 24))
		l.addWidget(i)
		l.addWidget(QLabel(text), stretch=1)
		self.setLayout(l)



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
