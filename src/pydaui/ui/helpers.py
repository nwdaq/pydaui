# SPDX-License-Identifier: GPL-3.0-or-later
#
# User interface helper widgets
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
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
