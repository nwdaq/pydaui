# SPDX-License-Identifier: GPL-3.0-or-later
#
# Credentials window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""A window asking for a username and password, e.g. for a repository behind HTTP basic authentication."""

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons


class CredentialsWindow(AppletWindow):
	"""The text saying what the credentials are for, the username and password and a check box offering to
	save them, the OK and Cancel buttons at the bottom right.

	OK emits :attr:`accepted` with the username, password and whether to save them, Cancel or closing the
	window emits :attr:`rejected`. The window closes either way.
	"""

	#: Small, kept in sight above the others.
	MAXIMIZE = False
	ON_TOP = True

	accepted = Signal(str, str, bool)
	rejected = Signal()

	def __init__(self, title, text, username='', parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(title)
		# Whether accepted or rejected was emitted, closing the window then emits nothing more.
		self._answered = False

		self._text = QLabel(text)
		self._text.setWordWrap(True)
		self._text.setAlignment(Qt.AlignLeft | Qt.AlignTop)

		self._username = QLineEdit(username)
		self._username.textChanged.connect(self._updateOk)
		self._password = QLineEdit()
		self._password.setEchoMode(QLineEdit.Password)
		fields = QFormLayout()
		fields.addRow(self.tr('Username:'), self._username)
		fields.addRow(self.tr('Password:'), self._password)

		self._save = QCheckBox(self.tr('Save the username and password in the settings (as plain text)'))

		self._ok = DauiButton(self.tr('OK'), Icons.confirm())
		self._ok.clicked.connect(self._accept)
		self._cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		self._cancel.clicked.connect(self._reject)
		# Enter confirms and Escape cancels from the edit boxes.
		self._username.returnPressed.connect(self._ok.click)
		self._password.returnPressed.connect(self._ok.click)
		QShortcut(QKeySequence(Qt.Key_Escape), self, self._cancel.click, context=Qt.WidgetWithChildrenShortcut)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._ok)
		self._tb.addWidget(self._cancel)

		content = QVBoxLayout()
		content.setContentsMargins(9, 9, 9, 9)
		content.addWidget(self._text, stretch=1)
		content.addLayout(fields)
		content.addWidget(self._save)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(content, stretch=1)
		layout.addWidget(self._tb)

		widget = QWidget()
		widget.setLayout(layout)
		self.setWidget(widget)
		self.resize(440, 240)
		self._updateOk()

	def showEvent(self, event):
		super().showEvent(event)
		# The username is often known already, the password never is.
		(self._password if self._username.text() else self._username).setFocus()

	@Slot()
	def _updateOk(self):
		self._ok.setEnabled(bool(self._username.text()))

	@Slot()
	def _accept(self):
		if not self._ok.isEnabled() or self._answered:
			return
		self._answered = True
		self.accepted.emit(self._username.text(), self._password.text(), self._save.isChecked())
		self.close()

	@Slot()
	def _reject(self):
		if not self._answered:
			self._answered = True
			self.rejected.emit()
		self.close()

	def closeEvent(self, event):
		# Closing the window cancels.
		if not self._answered:
			self._answered = True
			self.rejected.emit()
		super().closeEvent(event)
