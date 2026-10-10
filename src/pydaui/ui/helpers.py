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


#: The cancel icon: an outlined cross, {stroke} and {fill} are filled in by :func:`cancel_icon`. The cross
#: is a plus shape rotated by 45 degrees, it covers only the middle of the canvas to stay subtle.
CANCEL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
	<path transform="rotate(45 12 12)" fill="{fill}" stroke="{stroke}" stroke-width="1.2" stroke-linejoin="round"
		d="M10.3 4.5 H13.7 V10.3 H19.5 V13.7 H13.7 V19.5 H10.3 V13.7 H4.5 V10.3 H10.3 Z"/>
</svg>"""


def cancel_icon(stroke='#9b1c1c', fill='#f9d4d4'):
	"""A subtle cancel icon: a cross outlined with a dark red stroke and filled with light red."""
	return SvgIcon(CANCEL_SVG.format(stroke=stroke, fill=fill))


#: The refresh icon: a circular arrow drawn as an outline, {stroke} and {fill} are filled in by
#: :func:`refresh_icon`. A thick ring from the lower right clockwise round to the upper right, where it
#: ends in an arrowhead pointing on clockwise.
REFRESH_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
	<path fill="{fill}" stroke="{stroke}" stroke-width="1.1" stroke-linejoin="round"
		d="M19.04 16.93 A8.6 8.6 0 1 1 16.30 4.55 L17.35 2.73 L19.14 8.04 L13.65 9.14 L14.70 7.32
		   A5.4 5.4 0 1 0 16.42 15.10 Z"/>
</svg>"""


def refresh_icon(stroke='#1d3f7a', fill='#79a6e3'):
	"""A refresh icon: a circular arrow outlined with a dark blue stroke and filled with blue."""
	return SvgIcon(REFRESH_SVG.format(stroke=stroke, fill=fill))


#: The save icon: a floppy disk drawn as an outline, {stroke} and {fill} are filled in by :func:`save_icon`.
#: A body with a cut corner, the metal shutter with its slot at the top and the label at the bottom.
SAVE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
	<g stroke="{stroke}" stroke-width="1.1" stroke-linejoin="round">
		<path fill="{fill}" d="M5 3.5 H16.4 L20.5 7.6 V19 Q20.5 20.5 19 20.5 H5 Q3.5 20.5 3.5 19 V5 Q3.5 3.5 5 3.5 Z"/>
		<rect x="7.5" y="3.5" width="8.5" height="5.5" fill="#fff"/>
		<rect x="12.6" y="4.7" width="1.9" height="3.1" fill="{stroke}" stroke-width="0.6"/>
		<rect x="6.5" y="12.3" width="11" height="8.2" rx="0.6" fill="#fff"/>
		<path d="M8.5 15 H15.5 M8.5 17.6 H15.5" stroke-width="0.9"/>
	</g>
</svg>"""


def save_icon(stroke='#1d3f7a', fill='#79a6e3'):
	"""A save icon: a floppy disk outlined with a dark blue stroke and filled with blue."""
	return SvgIcon(SAVE_SVG.format(stroke=stroke, fill=fill))


#: A tree branch indicator: a small box with a plus (collapsed) or a minus (expanded) sign. {sign} is
#: filled in with the path of the sign by :func:`expand_icon` and :func:`collapse_icon`.
BRANCH_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">
	<rect x="0.5" y="0.5" width="11" height="11" rx="1.5" fill="{fill}" stroke="{stroke}" stroke-width="1"/>
	<path d="{sign}" stroke="{sign_stroke}" stroke-width="1.3" stroke-linecap="round"/>
</svg>"""

_PLUS = 'M3 6 H9 M6 3 V9'
_MINUS = 'M3 6 H9'


def expand_icon(stroke='#999', fill='#fff', sign='#555'):
	"""The indicator of a collapsed tree branch: a plus sign in a small box."""
	return SvgIcon(BRANCH_SVG.format(stroke=stroke, fill=fill, sign=_PLUS, sign_stroke=sign))


def collapse_icon(stroke='#999', fill='#fff', sign='#555'):
	"""The indicator of an expanded tree branch: a minus sign in a small box."""
	return SvgIcon(BRANCH_SVG.format(stroke=stroke, fill=fill, sign=_MINUS, sign_stroke=sign))
