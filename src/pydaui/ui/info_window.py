# SPDX-License-Identifier: GPL-3.0-or-later
#
# Window showing read-only information
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons


class InfoWindow(AppletWindow):
	"""Scrolling content added with :meth:`addText` and :meth:`addWidget`, the Close button at the bottom
	right. Used for the windows of the Help menu."""

	LAYER = AppletWindow.TOOL

	def __init__(self, title, icon, parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(title)
		self.setWindowIcon(icon)

		self._content = QVBoxLayout()
		self._content.setContentsMargins(9, 9, 9, 9)
		# Keeps the content at the top, the added widgets go before it.
		self._content.addStretch(1)

		content = QWidget()
		content.setLayout(self._content)
		scroll = QScrollArea()
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setWidgetResizable(True)
		scroll.setWidget(content)

		self._close = DauiButton(self.tr('Close'), Icons.cancel())
		self._close.clicked.connect(self.close)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._close)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(scroll, stretch=1)
		layout.addWidget(self._tb)

		widget = QWidget()
		widget.setLayout(layout)
		self.setWidget(widget)
		self.resize(560, 640)

	def addWidget(self, widget):
		"""Add ``widget`` below the content."""
		self._content.insertWidget(self._content.count() - 1, widget)

	def addText(self, text, alignment=Qt.AlignHCenter):
		"""Add the rich ``text`` below the content, its links open in the browser."""
		label = QLabel(text)
		label.setWordWrap(True)
		label.setAlignment(alignment | Qt.AlignTop)
		label.setTextFormat(Qt.RichText)
		label.setOpenExternalLinks(True)
		label.setTextInteractionFlags(Qt.TextBrowserInteraction)
		self.addWidget(label)
		return label
