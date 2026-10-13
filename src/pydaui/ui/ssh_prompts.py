# SPDX-License-Identifier: GPL-3.0-or-later
#
# Windows answering the questions of the pynbus2 ssh transport
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The passwords and the unknown host keys the ssh transport asks about while connecting, see
:func:`pynbus2.transport.ssh.set_prompts`."""

import html
import threading

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pynbus2.transport import ssh

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons


class _PromptWindow(AppletWindow):
	"""A text, the fields of a subclass under it and the OK and Cancel buttons at the bottom right.

	OK emits :attr:`answered` with :meth:`answer`, Cancel or closing the window emits it with ``None``. The
	window closes either way.
	"""

	LAYER = AppletWindow.CONNECT
	#: Small, kept in sight above all the others, the connection waits for it.
	MAXIMIZE = False
	ON_TOP = True
	TOPMOST = True

	answered = Signal(object)

	def __init__(self, title, text, ok, parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		# Neither minimized nor maximized, closing it cancels.
		self.setWindowFlags(Qt.SubWindow | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
		self.setWindowTitle(title)
		# Whether answered was emitted, closing the window then emits nothing more.
		self._answered = False

		label = QLabel(text)
		label.setWordWrap(True)
		label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
		label.setTextInteractionFlags(Qt.TextSelectableByMouse)

		self._ok = DauiButton(ok, Icons.confirm())
		self._ok.clicked.connect(self._accept)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self.close)
		QShortcut(QKeySequence(Qt.Key_Escape), self, cancel.click, context=Qt.WidgetWithChildrenShortcut)

		tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		tb.addWidget(spacer)
		tb.addWidget(self._ok)
		tb.addWidget(cancel)

		self._content = QVBoxLayout()
		self._content.setContentsMargins(9, 9, 9, 9)
		self._content.addWidget(label, stretch=1)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(self._content, stretch=1)
		layout.addWidget(tb)

		widget = QWidget()
		widget.setLayout(layout)
		widget.setMinimumWidth(360)
		self.setWidget(widget)

	def answer(self):
		return True

	@Slot()
	def _accept(self):
		if not self._answered:
			self._answered = True
			self.answered.emit(self.answer())
		self.close()

	def closeEvent(self, event):
		# Closing the window cancels.
		if not self._answered:
			self._answered = True
			self.answered.emit(None)
		super().closeEvent(event)


class SshPasswordWindow(_PromptWindow):
	"""Asks for the password or the passphrase of a private key the ``prompt`` names."""

	def __init__(self, prompt, parent=None):
		super().__init__(self.tr('SSH authentication'), html.escape(prompt), self.tr('OK'), parent=parent)
		self._password = QLineEdit()
		self._password.setEchoMode(QLineEdit.Password)
		self._password.returnPressed.connect(self._ok.click)
		self._content.addWidget(self._password)

	def showEvent(self, event):
		super().showEvent(event)
		self._password.setFocus()

	def answer(self):
		return self._password.text()


class SshHostKeyWindow(_PromptWindow):
	"""Asks whether to trust the unknown key of a host."""

	def __init__(self, host, key_type, fingerprint, parent=None):
		text = self.tr(
			'The host <b>%s</b> is not known. Its %s key fingerprint is:<br><br><tt>%s</tt><br><br>'
			'Trust it only if this is the fingerprint of the host, it is added to the known hosts then.'
		) % (html.escape(host), html.escape(key_type), html.escape(fingerprint))
		super().__init__(self.tr('Unknown SSH host'), text, self.tr('Trust'), parent=parent)


class _Question:
	"""A window to show on the GUI thread and the answer the asking thread waits for."""

	def __init__(self, create):
		self.create = create
		self.answer = None
		self.done = threading.Event()
		# Quit when answered, if the question is asked on the GUI thread.
		self.loop = None

	def answerWith(self, answer):
		self.answer = answer
		self.done.set()
		if self.loop is not None:
			self.loop.quit()


class SshPrompts(QObject):
	"""Answers the questions of the pynbus2 ssh transport with windows in the MDI area of ``main_window``.

	The transport asks from the thread connecting or probing, which waits until the window is answered.
	"""

	_asked = Signal(object)

	def __init__(self, main_window):
		super().__init__()
		self._main_window = main_window
		# Queued, it is emitted from the asking thread.
		self._asked.connect(self._show)
		ssh.set_prompts(password=self._password, host_key=self._hostKey)

	def _password(self, prompt):
		return self._ask(lambda: SshPasswordWindow(prompt))

	def _hostKey(self, host, key_type, fingerprint):
		return bool(self._ask(lambda: SshHostKeyWindow(host, key_type, fingerprint)))

	def _ask(self, create):
		"""Show the window ``create`` returns on the GUI thread and return its answer, ``None`` if cancelled."""
		question = _Question(create)
		if QThread.currentThread() is self.thread():
			question.loop = QEventLoop()
			self._show(question)
			if not question.done.is_set():
				question.loop.exec()
		else:
			self._asked.emit(question)
			question.done.wait()
		return question.answer

	@Slot(object)
	def _show(self, question):
		window = question.create()
		window.answered.connect(question.answerWith)
		self._main_window.addWindow(window)
		window.showCentered()
