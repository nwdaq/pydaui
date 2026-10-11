# SPDX-License-Identifier: GPL-3.0-or-later
#
# SVG icons
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from pydaui.ui.helpers import SvgIcon


class Icons:
	"""The application's own SVG icons, drawn as outlines in the common colours below."""

	DARK_BLUE = '#1d3f7a'
	BLUE = '#79a6e3'
	LIGHT_BLUE = '#b9d1f2'
	DARK_RED = '#9b1c1c'
	RED = '#e37979'
	LIGHT_RED = '#f9d4d4'
	ORANGE = '#e3a079'
	DARK_GRAY = '#4a4a4a'
	GRAY = '#999'
	LIGHT_GRAY = '#d6d6d6'
	WHITE = '#fff'

	PRIMARY = DARK_BLUE
	PRIMARY_DISABLED = DARK_GRAY
	PRIMARY_LIGHT = LIGHT_BLUE

	@staticmethod
	def cancel():
		"""A subtle cancel icon: a cross covering only the middle of the canvas, a plus shape rotated by
		45 degrees."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<path transform="rotate(45 12 12)" fill="{Icons.LIGHT_RED}" stroke="{Icons.DARK_RED}" stroke-width="1.2" stroke-linejoin="round"
				d="M10.3 4.5 H13.7 V10.3 H19.5 V13.7 H13.7 V19.5 H10.3 V13.7 H4.5 V10.3 H10.3 Z"/>
		</svg>""")

	@staticmethod
	def refresh():
		"""A refresh icon: a thick ring from the lower right clockwise round to the upper right, where it
		ends in an arrowhead pointing on clockwise."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<path fill="{Icons.PRIMARY_LIGHT}" stroke="{Icons.PRIMARY}" stroke-width="1.1" stroke-linejoin="round"
				d="M19.04 16.93 A8.6 8.6 0 1 1 16.30 4.55 L17.35 2.73 L19.14 8.04 L13.65 9.14 L14.70 7.32
				   A5.4 5.4 0 1 0 16.42 15.10 Z"/>
		</svg>""")

	@staticmethod
	def save():
		"""A save icon: a floppy disk, a body with a cut corner, the metal shutter with its slot at the top
		and the label at the bottom."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<g stroke="{Icons.PRIMARY}" stroke-width="1.1" stroke-linejoin="round">
				<path fill="{Icons.PRIMARY_LIGHT}" d="M5 3.5 H16.4 L20.5 7.6 V19 Q20.5 20.5 19 20.5 H5 Q3.5 20.5 3.5 19 V5 Q3.5 3.5 5 3.5 Z"/>
				<rect x="7.5" y="3.5" width="8.5" height="5.5" fill="{Icons.WHITE}"/>
				<rect x="12.6" y="4.7" width="1.9" height="3.1" fill="{Icons.PRIMARY}" stroke-width="0.6"/>
				<rect x="6.5" y="12.3" width="11" height="8.2" rx="0.6" fill="{Icons.WHITE}"/>
				<path d="M8.5 15 H15.5 M8.5 17.6 H15.5" stroke-width="0.9"/>
			</g>
		</svg>""")

	@staticmethod
	def abort():
		"""An abort icon: a stop sign, an octagon with a bar across it."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<g stroke="{Icons.DARK_RED}" stroke-width="1.1" stroke-linejoin="round">
				<path fill="{Icons.LIGHT_RED}" d="M8.3 2.5 H15.7 L21.5 8.3 V15.7 L15.7 21.5 H8.3 L2.5 15.7 V8.3 Z"/>
				<rect x="6.5" y="10.4" width="11" height="3.2" rx="0.6" fill="{Icons.DARK_RED}"/>
			</g>
		</svg>""")

	@staticmethod
	def quit():
		"""A quit icon: an open door with an arrow leaving through it."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<g stroke="{Icons.DARK_RED}" stroke-width="1.1" stroke-linejoin="round">
				<path fill="{Icons.WHITE}" d="M3.5 3.5 H14.5 V20.5 H3.5 Z"/>
				<path fill="{Icons.ORANGE}" d="M3.5 3.5 L10.5 5.5 V22 L3.5 20.5 Z"/>
				<circle cx="8.8" cy="13" r="0.7" fill="{Icons.DARK_RED}" stroke="none"/>
				<path fill="{Icons.RED}" d="M12 10.5 H16.5 V7.3 L21.5 12 L16.5 16.7 V13.5 H12 Z"/>
			</g>
		</svg>""")

	@staticmethod
	def conf():
		"""The configuration icon: a gray gear in the back and a smaller blue one in front of it, lower
		right."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<g stroke-width="1.1" stroke-linejoin="round" fill-rule="evenodd">
				<path fill="{Icons.LIGHT_GRAY}" stroke="{Icons.DARK_GRAY}"
					d="M7.9 2.9 L8.2 1.0 A8.4 8.4 0 0 1 10.4 1.0 L10.7 2.9 A6.5 6.5 0 0 1 12.8 3.8
					   L14.4 2.7 A8.4 8.4 0 0 1 15.9 4.2 L14.8 5.8 A6.5 6.5 0 0 1 15.7 7.9
					   L17.6 8.2 A8.4 8.4 0 0 1 17.6 10.4 L15.7 10.7 A6.5 6.5 0 0 1 14.8 12.8
					   L15.9 14.4 A8.4 8.4 0 0 1 14.4 15.9 L12.8 14.8 A6.5 6.5 0 0 1 10.7 15.7
					   L10.4 17.6 A8.4 8.4 0 0 1 8.2 17.6 L7.9 15.7 A6.5 6.5 0 0 1 5.8 14.8
					   L4.2 15.9 A8.4 8.4 0 0 1 2.7 14.4 L3.8 12.8 A6.5 6.5 0 0 1 2.9 10.7
					   L1.0 10.4 A8.4 8.4 0 0 1 1.0 8.2 L2.9 7.9 A6.5 6.5 0 0 1 3.8 5.8
					   L2.7 4.2 A8.4 8.4 0 0 1 4.2 2.7 L5.8 3.8 A6.5 6.5 0 0 1 7.9 2.9 Z
					   M9.3 6.7 A2.6 2.6 0 1 0 9.3 11.9 A2.6 2.6 0 1 0 9.3 6.7 Z"/>
				<path fill="{Icons.PRIMARY_LIGHT}" stroke="{Icons.PRIMARY}"
					d="M17.6 11.9 L18.5 10.5 A6.4 6.4 0 0 1 20.2 11.3 L19.7 12.9 A4.8 4.8 0 0 1 20.9 14.4
					   L22.6 14.3 A6.4 6.4 0 0 1 23.0 16.1 L21.4 16.7 A4.8 4.8 0 0 1 21.0 18.6
					   L22.1 19.8 A6.4 6.4 0 0 1 21.0 21.3 L19.5 20.4 A4.8 4.8 0 0 1 17.8 21.3
					   L17.5 22.9 A6.4 6.4 0 0 1 15.7 22.9 L15.4 21.3 A4.8 4.8 0 0 1 13.7 20.4
					   L12.2 21.3 A6.4 6.4 0 0 1 11.1 19.8 L12.2 18.6 A4.8 4.8 0 0 1 11.8 16.7
					   L10.2 16.1 A6.4 6.4 0 0 1 10.6 14.3 L12.3 14.4 A4.8 4.8 0 0 1 13.5 12.9
					   L13.0 11.3 A6.4 6.4 0 0 1 14.7 10.5 L15.6 11.9 A4.8 4.8 0 0 1 17.6 11.9 Z
					   M16.6 14.7 A1.9 1.9 0 1 0 16.6 18.5 A1.9 1.9 0 1 0 16.6 14.7 Z"/>
			</g>
		</svg>""")

	@staticmethod
	def storage():
		"""The storage icon: a gray chip with "10101" written on a white background in front of
		it, lower right."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			<g stroke="{Icons.DARK_GRAY}" stroke-width="1.1" stroke-linejoin="round" fill="{Icons.LIGHT_GRAY}">
				<path d="M0.6 5.5 H3.5 M0.6 9 H3.5 M0.6 12.5 H3.5 M16.5 5.5 H19.4 M16.5 9 H19.4 M16.5 12.5 H19.4 M6.5 0.6 V2.5 M10 0.6 V2.5 M13.5 0.6 V2.5"/>
				<rect x="3.5" y="2.5" width="13" height="12.5" rx="1.5"/>
			</g>
			<rect x="4.4" y="13.6" width="19.4" height="9.8" rx="1" fill="{Icons.WHITE}"/>
			<g fill="{Icons.PRIMARY}" fill-rule="evenodd">
				<path d="M6.5 14.5 H8 V22.5 H6.5 V17 L5.4 17.6 V16.1 Z"/>
				<path d="M10.3 14.5 H10.7 Q12.3 14.5 12.3 16.1 V20.9 Q12.3 22.5 10.7 22.5 H10.3 Q8.7 22.5 8.7 20.9 V16.1 Q8.7 14.5 10.3 14.5 Z
				         M10.5 16 Q9.9 16 9.9 16.6 V20.4 Q9.9 21 10.5 21 Q11.1 21 11.1 20.4 V16.6 Q11.1 16 10.5 16 Z"/>
				<path d="M14.1 14.5 H15.6 V22.5 H14.1 V17 L13 17.6 V16.1 Z"/>
				<path d="M17.9 14.5 H18.3 Q19.9 14.5 19.9 16.1 V20.9 Q19.9 22.5 18.3 22.5 H17.9 Q16.3 22.5 16.3 20.9 V16.1 Q16.3 14.5 17.9 14.5 Z
				         M18.1 16 Q17.5 16 17.5 16.6 V20.4 Q17.5 21 18.1 21 Q18.7 21 18.7 20.4 V16.6 Q18.7 16 18.1 16 Z"/>
				<path d="M21.7 14.5 H23.2 V22.5 H21.7 V17 L20.6 17.6 V16.1 Z"/>
			</g>
		</svg>""")

	@staticmethod
	def _chip(pins_top=(6.5, 10, 14, 17.5)):
		"""A gray chip in the lower part of the canvas with its pins above and below, for the flash icons.
		``pins_top`` are the x positions of the pins above, which may make room for what is drawn over it."""
		top = ' '.join(f'M{x} 10.5 V12.5' for x in pins_top)
		return f"""<g stroke="{Icons.DARK_GRAY}" stroke-width="1.1" stroke-linejoin="round" fill="{Icons.LIGHT_GRAY}">
				<path d="{top} M6.5 21 V23 M10 21 V23 M14 21 V23 M17.5 21 V23"/>
				<rect x="3.5" y="12.5" width="17" height="8.5" rx="1.2"/>
			</g>"""

	@staticmethod
	def write():
		"""The flash write icon: an arrow pointing down into a gray chip."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			{Icons._chip(pins_top=(5.5, 18.5))}
			<path fill="{Icons.PRIMARY_LIGHT}" stroke="{Icons.PRIMARY}" stroke-width="1.1" stroke-linejoin="round"
				d="M10.3 1.5 H13.7 V8 H16.8 L12 14 L7.2 8 H10.3 Z"/>
		</svg>""")

	@staticmethod
	def read():
		"""The flash read icon: an arrow pointing up out of a gray chip."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			{Icons._chip(pins_top=(5.5, 18.5))}
			<path fill="{Icons.PRIMARY_LIGHT}" stroke="{Icons.PRIMARY}" stroke-width="1.1" stroke-linejoin="round"
				d="M12 1.5 L16.8 7.5 H13.7 V15 H10.3 V7.5 H7.2 Z"/>
		</svg>""")

	@staticmethod
	def erase():
		"""The flash erase icon: an eraser rubbing a gray chip, tilted with its white rubbing end down to the
		left."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
			{Icons._chip()}
			<g transform="rotate(45 14.5 8.5)" stroke="{Icons.PRIMARY}" stroke-width="1.1" stroke-linejoin="round">
				<rect x="11" y="1.5" width="7" height="10" rx="1" fill="{Icons.PRIMARY_LIGHT}"/>
				<path d="M11 11.5 H18 V14.5 Q18 15.5 17 15.5 H12 Q11 15.5 11 14.5 Z" fill="{Icons.WHITE}"/>
			</g>
		</svg>""")

	@staticmethod
	def expand():
		"""The indicator of a collapsed tree branch: a plus sign in a small box."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">
			<rect x="0.5" y="0.5" width="11" height="11" rx="1.5" fill="{Icons.WHITE}" stroke="{Icons.GRAY}" stroke-width="1"/>
			<path d="M3 6 H9 M6 3 V9" stroke="{Icons.DARK_GRAY}" stroke-width="1.3" stroke-linecap="round"/>
		</svg>""")

	@staticmethod
	def collapse():
		"""The indicator of an expanded tree branch: a minus sign in a small box."""
		return SvgIcon(f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">
			<rect x="0.5" y="0.5" width="11" height="11" rx="1.5" fill="{Icons.WHITE}" stroke="{Icons.GRAY}" stroke-width="1"/>
			<path d="M3 6 H9" stroke="{Icons.DARK_GRAY}" stroke-width="1.3" stroke-linecap="round"/>
		</svg>""")
