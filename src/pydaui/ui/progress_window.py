# SPDX-License-Identifier: GPL-3.0-or-later
#
# Progress window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""A window showing the progress of a long running action, e.g. writing a flash partition."""

import collections
import time

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons


class PlainFormat:
	"""Formats a progress as the plain numbers, e.g. ``37 / 256``, and its speed as e.g. ``12.5 /s``."""

	def __call__(self, value, maximum):
		return f'{value} / {maximum}'

	def rate(self, per_second):
		return f'{per_second:.1f} /s'


class ByteFormat:
	"""Formats a progress in bytes as e.g. ``104.0 / 512.0 KiB`` and its speed as e.g. ``98.4 KiB/s``. The
	unit and the number of decimals are chosen once from the total, so the texts do not change their form
	as the progress goes on."""

	UNITS = ('B', 'KiB', 'MiB', 'GiB')

	def __init__(self, total):
		# The largest unit leaving at least 10 of them in the total, for a fine enough progress.
		self._exp = 0
		while self._exp + 1 < len(self.UNITS) and total >= 10 * 1024 ** (self._exp + 1):
			self._exp += 1
		self._decimals = 0 if self._exp == 0 or total >= 1000 * 1024 ** self._exp else 1

	def __call__(self, value, maximum):
		div = 1024 ** self._exp
		return f'{value / div:.{self._decimals}f} / {maximum / div:.{self._decimals}f} {self.UNITS[self._exp]}'

	def rate(self, per_second):
		return f'{per_second / 1024 ** self._exp:.1f} {self.UNITS[self._exp]}/s'


def format_duration(seconds):
	"""``seconds`` as ``m:ss``, or ``h:mm:ss`` from an hour on."""
	seconds = int(round(seconds))
	h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
	return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'


class ProgressWindow(AppletWindow):
	"""The step of the action (if it has several), the text describing it, then the progress, the current
	speed and the estimated time left above a progress bar and the Abort button at the bottom right.
	Pressing Abort or closing the window emits :attr:`aborted`, stopping the action is up to whoever runs
	it. The window stays open until :meth:`finish` is called.

	``format`` is called as ``format(value, maximum)`` and returns the progress text, its ``rate(per_second)``
	returns the speed text, e.g. a :class:`ByteFormat`; by default a :class:`PlainFormat`.
	"""

	#: Small, kept in sight above the others.
	MAXIMIZE = False
	ON_TOP = True

	aborted = Signal()

	#: How long the action has to run before the time left is estimated, in seconds.
	ETA_DELAY = 1.0

	#: Shortest interval between the updates of the progress texts, in seconds, to keep them readable.
	UPDATE_INTERVAL = 0.2

	#: The current speed is the average over this long, in seconds.
	SPEED_WINDOW = 2.0

	PROGRESS_STYLE = f'QLabel {{ color: {Icons.DARK_GRAY}; font-weight: bold; }}'

	def __init__(self, title, text='', format=None, parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(title)
		self._finished = False
		self._format = format or PlainFormat()
		# When and at which value the progress started, the ETA comes from the average rate since.
		self._start = None
		self._last_update = 0.0
		# Recent (time, value) samples of the progress, the current speed comes from them.
		self._samples = collections.deque()

		self._step = QLabel()
		self._step.setStyleSheet(self.PROGRESS_STYLE)
		self._step.hide()

		self._text = QLabel(text)
		self._text.setWordWrap(True)
		self._text.setAlignment(Qt.AlignLeft | Qt.AlignTop)

		self._abort = DauiButton(self.tr('Abort'), Icons.abort())
		self._abort.setToolTip(self.tr('Abort the action'))
		self._abort.clicked.connect(self.aborted)

		self._progress = QProgressBar()
		# As tall as the toolbar buttons.
		self._progress.setFixedHeight(self._abort.sizeHint().height())

		self._done = QLabel()
		self._done.setStyleSheet(self.PROGRESS_STYLE)
		self._speed = QLabel()
		self._speed.setStyleSheet(self.PROGRESS_STYLE)
		self._speed.setAlignment(Qt.AlignCenter)
		self._eta = QLabel()
		self._eta.setStyleSheet(self.PROGRESS_STYLE)
		self._eta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
		status = QHBoxLayout()
		# Some room between the status line and the progress bar.
		status.setContentsMargins(0, 0, 0, 4)
		# Equal widths keep the speed in the middle.
		status.addWidget(self._done, stretch=1)
		status.addWidget(self._speed, stretch=1)
		status.addWidget(self._eta, stretch=1)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._abort)

		content = QVBoxLayout()
		content.setContentsMargins(9, 9, 9, 9)
		content.addWidget(self._step)
		content.addWidget(self._text, stretch=1)
		content.addLayout(status)
		content.addWidget(self._progress)

		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(content, stretch=1)
		layout.addWidget(self._tb)

		widget = QWidget()
		widget.setLayout(layout)
		self.setWidget(widget)
		self.resize(400, 200)

	def setText(self, text):
		"""Describe the current action."""
		self._text.setText(text)

	def setStep(self, step, count=None):
		"""Show ``Step <step> of <count>``, or hide it if ``step`` is ``None``."""
		if step is None:
			self._step.hide()
			return
		self._step.setText(self.tr('Step %d of %d') % (step, count))
		self._step.show()

	def setFormat(self, format):
		"""Format the progress with ``format``, see the class description."""
		self._format = format

	def setProgress(self, value, maximum=None):
		"""Show ``value`` out of ``maximum`` done, the maximum stays as it was if not given. A maximum of 0
		shows a busy indicator, for actions whose length is not known. Setting the maximum starts the
		estimate of the time left anew."""
		now = time.monotonic()
		if maximum is not None:
			self._progress.setMaximum(maximum)
			self._start = (now, value)
			self._samples.clear()
		self._progress.setValue(value)
		self._samples.append((now, value))
		# Keep one sample older than the window, to span all of it.
		while len(self._samples) > 2 and now - self._samples[1][0] >= self.SPEED_WINDOW:
			self._samples.popleft()

		maximum = self._progress.maximum()
		if maximum <= 0:
			self._done.setText('')
			self._speed.setText('')
			self._eta.setText('')
			return
		# Every update would make the texts flicker, except the last one.
		if now - self._last_update < self.UPDATE_INTERVAL and value < maximum:
			return
		self._last_update = now
		self._done.setText(self._format(value, maximum))
		self._speed.setText(self._currentSpeed(now, value, maximum))
		self._eta.setText(self._estimate(now, value, maximum))

	def _currentSpeed(self, now, value, maximum):
		"""The text of the speed over the last :attr:`SPEED_WINDOW`, empty until it can be measured."""
		if value >= maximum or not self._samples:
			return ''
		then, then_value = self._samples[0]
		if now - then < self.UPDATE_INTERVAL:
			return ''
		return self._format.rate((value - then_value) / (now - then))

	def _estimate(self, now, value, maximum):
		"""The text of the time left, empty until it can be estimated."""
		if value >= maximum:
			return ''
		if self._start is None:
			return ''
		start_time, start_value = self._start
		elapsed = now - start_time
		if elapsed < self.ETA_DELAY or value <= start_value:
			return self.tr('estimating time left...')
		rate = (value - start_value) / elapsed
		return self.tr('%s left') % format_duration((maximum - value) / rate)

	def setAbortEnabled(self, enabled):
		self._abort.setEnabled(enabled)

	def finish(self):
		"""Close the window, the action has ended."""
		self._finished = True
		self.close()

	def closeEvent(self, event):
		if not self._finished:
			# Closing the window asks to abort, it closes once the action ends.
			event.ignore()
			self.aborted.emit()
			return
		super().closeEvent(event)
