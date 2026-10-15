# SPDX-License-Identifier: GPL-3.0-or-later
#
# MQ live data applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""MQ live data applet: watch the values published to the message queue of a plumCore device.

It talks to an interface speaking the ``mq`` protocol (plumCore services/nbus-mq-poll), polling it with the
:class:`pynbus2.mq.MqPollClient` off the GUI thread. Every topic is shown once in a tree split by its
levels, its row is updated as new values arrive. The topic filter replaces the subscription of the remote
message queue. The selected topics are shown in big letters in the Display tab and graphed over time in the
Graph tab.

The module name is not a Python identifier, import it with :func:`importlib.import_module`.
"""

import collections
import logging
import math
import threading
import time

import cbor2
import numpy as np
import pyqtgraph as pg
from pynbus2.mq import MqPollClient, topic_matches
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton
from pydaui.ui.icons import Icons
from pydaui.ui.tree import GuideTreeWidget


#: The protocol name of the interfaces this applet talks to.
PROTOCOL = 'mq'

#: The topic filter the applet starts with, all the topics.
DEFAULT_TOPIC = '#'

#: The poll intervals offered as (label, seconds), and the index of the default one.
POLL_INTERVALS = (
	('50 ms', 0.05),
	('100 ms', 0.1),
	('200 ms', 0.2),
	('500 ms', 0.5),
	('1 s', 1.0),
	('2 s', 2.0),
	('5 s', 5.0),
	('10 s', 10.0),
)
DEFAULT_POLL_INTERVAL = 1

#: Seconds without a value after which a topic is stale.
STALE_S = 10.0

#: The span of the recent values the rate of a topic is computed from, in seconds.
RATE_WINDOW_S = 10.0


def is_valid_filter(filter):
	"""Whether ``filter`` is a valid MQTT-style topic filter: ``+`` and ``#`` take a whole level, ``#`` only
	the last one."""
	if not filter:
		return False
	levels = filter.split('/')
	for i, level in enumerate(levels):
		if '#' in level and (level != '#' or i != len(levels) - 1):
			return False
		if '+' in level and level != '+':
			return False
	return True


def type_name(value):
	"""The name of the type of a received ``value`` shown in the tree."""
	if value is cbor2.undefined:
		return 'undefined'
	if value is None:
		return 'null'
	if isinstance(value, bool):
		return 'bool'
	if isinstance(value, int):
		return 'int'
	if isinstance(value, float):
		return 'float'
	if isinstance(value, str):
		return 'str'
	if isinstance(value, bytes):
		return 'bytes'
	if isinstance(value, (list, tuple)):
		return 'array'
	if isinstance(value, dict):
		return 'map'
	return type(value).__name__


def format_value(value):
	"""A received ``value`` formatted for display."""
	if value is cbor2.undefined or value is None:
		return ''
	if isinstance(value, bool):
		return 'true' if value else 'false'
	if isinstance(value, float):
		return f'{value:.6g}'
	if isinstance(value, bytes):
		return value.hex()
	return str(value)


#: The precisions offered in the display panels, the index is the number of decimals.
PRECISIONS = ('0', '0.0', '0.00', '0.000', '0.0000', '0.00000', '0.000000')
DEFAULT_PRECISION = 2

#: The number styles of the display panels.
STYLE_INTEGRAL = 'Constant integral part'
STYLE_DIGITS = 'Constant number of digits'
STYLE_FRACTIONAL = 'Constant fractional part'
STYLES = (STYLE_INTEGRAL, STYLE_DIGITS, STYLE_FRACTIONAL)
DEFAULT_STYLE = STYLE_FRACTIONAL


def _split(value, decimals):
	"""The sign, the integral and the fractional digits of ``value`` rounded to ``decimals``."""
	text = f'{abs(value):.{decimals}f}'
	integral, _, fraction = text.partition('.')
	sign = '-' if value < 0 and text.strip('0.') else ''
	return sign, integral, fraction


def format_number(value, decimals, style, zero_pad, integral_width=0):
	"""The number ``value`` formatted by a ``style`` of :data:`STYLES` with ``decimals`` decimals:

	- :data:`STYLE_FRACTIONAL`: always ``decimals`` fractional digits, the integral part as wide as needed.
	- :data:`STYLE_INTEGRAL`: like that, with the integral part padded to ``integral_width`` digits.
	- :data:`STYLE_DIGITS`: ``1 + decimals`` digits in total, the decimal point moves. Fewer fractional
	  digits (down to none) as the integral part grows.

	The trailing zeros of the fraction and the padding of the integral part are zeros with ``zero_pad``,
	else spaces, which keep the digits in place in a monospace font. Returns the text and the width of its
	integral part.
	"""
	if style == STYLE_DIGITS:
		total = 1 + decimals
		sign, integral, fraction = _split(value, decimals)
		decimals = max(0, total - len(integral))
		sign, integral, fraction = _split(value, decimals)
		if decimals and len(integral) + decimals > total:
			# Rounding carried into a new integral digit, e.g. 9.99 to 10.0.
			sign, integral, fraction = _split(value, decimals - 1)
	else:
		sign, integral, fraction = _split(value, decimals)

	if not zero_pad and fraction:
		stripped = fraction.rstrip('0')
		fraction = stripped + ' ' * (len(fraction) - len(stripped))
	if fraction:
		point = '.' if fraction.strip() else ' '
		fraction = point + fraction

	width = len(integral)
	if style == STYLE_INTEGRAL and integral_width > len(integral):
		pad = integral_width - len(integral)
		number = sign + '0' * pad + integral if zero_pad else ' ' * pad + sign + integral
		width = integral_width
	else:
		number = sign + integral
	return number + fraction, width


#: The time spans the graph shows as (label, seconds), and the index of the default one.
GRAPH_SPANS = (
	('10 s', 10),
	('30 s', 30),
	('1 min', 60),
	('5 min', 300),
	('10 min', 600),
	('30 min', 1800),
	('60 min', 3600),
)
DEFAULT_GRAPH_SPAN = 2

#: The colours of the graph lines from the Tango palette, taken in turn: the middle shades of the hues first
#: (the darker ones of the butter and chameleon, the middle ones are too light on white), then the dark and
#: the light shades.
GRAPH_COLORS = (
	'#3465a4', '#cc0000', '#4e9a06', '#f57900', '#75507b', '#c17d11', '#c4a000', '#555753',
	'#204a87', '#a40000', '#ce5c00', '#5c3566', '#8f5902', '#2e3436',
	'#729fcf', '#ef2929', '#73d216', '#fcaf3e', '#ad7fa8', '#e9b96e', '#888a85',
)


def graph_value(value):
	"""A received ``value`` as the float graphed, ``None`` for a value which is not a number. A bool is
	graphed as 0 or 1."""
	if isinstance(value, (bool, int, float)):
		return float(value)
	return None


class _Topic:
	"""The values received on one topic: the last one and the times of the recent ones."""

	def __init__(self):
		self.value = None
		#: When the last value arrived, as :func:`time.monotonic`.
		self.seen = None
		# The times the recent values were published at by the device clock, and the times they arrived at.
		self._device_times = collections.deque()
		self._host_times = collections.deque()

	@staticmethod
	def _append(times, t):
		if times and t < times[-1]:
			# The clock went back (e.g. the device restarted), the older times do not compare.
			times.clear()
		times.append(t)
		while times[0] < t - RATE_WINDOW_S:
			times.popleft()

	@staticmethod
	def _rate(times):
		"""The values per second over ``times``, ``None`` if they span no time. The values arriving at the
		first time are not counted, they may have been polled together after a longer wait."""
		if len(times) < 2 or times[-1] <= times[0]:
			return None
		first = sum(1 for t in times if t == times[0])
		return (len(times) - first) / (times[-1] - times[0])

	def update(self, value, ts, now):
		self.value = value
		self.seen = now
		# A device without its clock set publishes zero timestamps.
		if ts:
			self._append(self._device_times, ts)
		self._append(self._host_times, now)

	def stale(self, now):
		return now - self.seen >= STALE_S

	def rate(self, now):
		"""The values per second over the recent values, 0 for a stale topic, ``None`` when not known yet. By
		the device timestamps if they advance, else by the times the values arrived at."""
		if self.stale(now):
			return 0.0
		rate = self._rate(self._device_times)
		return rate if rate is not None else self._rate(self._host_times)


class _Poller(QObject):
	"""Polls the mq interface off the GUI thread until stopped. A backlog is drained right away, otherwise
	the interface is polled every ``interval`` seconds.

	``received`` carries the list of the new :class:`pynbus2.mq.MqValue` and the number of batches missed,
	``noReply`` tells a poll got no answer.
	"""

	received = Signal(list, int)
	noReply = Signal(str)

	def __init__(self, side, topic, interval):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._side = side
		self._client = MqPollClient(side.socket, topic=topic)
		self._interval = interval
		self._stop = threading.Event()
		# Ends the wait for the next poll early.
		self._wake = threading.Event()
		self._running = threading.Event()
		self._running.set()

	def start(self):
		threading.Thread(target=self._run, name='mq-poll', daemon=True).start()

	def stop(self):
		self._stop.set()
		self._wake.set()
		self._running.set()

	def setInterval(self, interval):
		"""Poll every ``interval`` seconds, starting with the next poll."""
		self._interval = interval
		self._wake.set()

	def _wait(self):
		self._wake.wait(self._interval)
		self._wake.clear()

	def setTopic(self, topic):
		"""Send ``topic`` as the filter with the following polls."""
		self._client.topic = topic

	def setPaused(self, paused):
		if paused:
			self._running.clear()
		else:
			self._running.set()

	def _run(self):
		while not self._stop.is_set():
			self._running.wait()
			if self._stop.is_set():
				break
			try:
				with self._side.lock:
					if not self._side.connected:
						break
					reply = self._client.poll()
			except Exception as e:
				self._logger.warning(f'polling failed: {e}')
				self._emit(self.noReply, str(e))
				self._wait()
				continue

			if reply is None:
				self._emit(self.noReply, '')
				self._wait()
				continue
			self._emit(self.received, reply.values, reply.missed)
			if reply.pending == 0:
				self._wait()

	def _emit(self, signal, *args):
		if self._stop.is_set():
			return
		try:
			signal.emit(*args)
		except RuntimeError:
			# The window is gone.
			self._stop.set()


class MqLiveDataTreeWidget(GuideTreeWidget):
	"""The received topics as a tree of their levels, each topic shown once with its last value. Stale
	topics are dimmed, so are the levels holding only stale topics.

	The topics have a check box in the Select column, ``selectionChanged`` carries the topic and whether it
	is selected now. Removing a selected topic deselects it.
	"""

	selectionChanged = Signal(str, bool)

	#: The full topic of an item.
	TOPIC_ROLE = Qt.UserRole

	TOPIC_COLUMN = 0
	VALUE_COLUMN = 1
	TYPE_COLUMN = 2
	SEEN_COLUMN = 3
	RATE_COLUMN = 4
	SELECT_COLUMN = 5

	#: Colour of the type.
	TYPE_COLOR = '#999'

	#: Colour of a stale row.
	STALE_COLOR = '#c4c4c4'

	#: Relative widths of the Topic, Value, Type, Last seen, Rate and Select columns.
	COLUMN_RATIOS = (5, 4, 2, 2, 2, 2)

	#: How often the times and the stale rows are updated, in milliseconds.
	REFRESH_MS = 500

	def __init__(self):
		super().__init__()
		# The tree items and the received values by the full topic of the item. Levels which were not
		# published to have an item but no values.
		self._items = {}
		self._topics = {}
		# The items shown dimmed.
		self._stale = set()
		# The selected topics.
		self._selected = set()

		# The last column takes the rest of the width, which may be narrower than the default.
		self.header().setDefaultSectionSize(40)
		self.setColumnCount(6)
		self.setHeaderLabels(['Topic', 'Value', 'Type', 'Last seen', 'Rate', 'Select'])
		self.itemChanged.connect(self._itemChanged)
		header = self.headerItem()
		for column in (self.SEEN_COLUMN, self.RATE_COLUMN):
			header.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)

		self._timer = QTimer(self)
		self._timer.setInterval(self.REFRESH_MS)
		self._timer.timeout.connect(self._refresh)
		self._timer.start()

	@property
	def topicCount(self):
		return len(self._topics)

	def topic(self, name):
		"""The values received on the topic ``name``, a :class:`_Topic`, ``None`` if there are none."""
		return self._topics.get(name)

	def setSelected(self, name, selected):
		"""Check or uncheck the Select check box of the topic ``name``, if it is shown."""
		item = self._items.get(name)
		if item is not None and name in self._topics:
			item.setCheckState(self.SELECT_COLUMN, Qt.Checked if selected else Qt.Unchecked)

	@Slot(QTreeWidgetItem, int)
	def _itemChanged(self, item, column):
		if column != self.SELECT_COLUMN:
			return
		name = item.data(self.TOPIC_COLUMN, self.TOPIC_ROLE)
		selected = item.checkState(self.SELECT_COLUMN) == Qt.Checked
		if selected != (name in self._selected):
			self._setSelection(name, selected)

	def _setSelection(self, name, selected):
		if selected:
			self._selected.add(name)
		else:
			self._selected.discard(name)
		self.selectionChanged.emit(name, selected)

	def addValues(self, values):
		"""Show the received :class:`pynbus2.mq.MqValue` ``values``, adding the topics not shown yet."""
		now = time.monotonic()
		for v in values:
			topic = self._topics.get(v.topic)
			item = self._item(v.topic)
			if topic is None:
				topic = self._topics[v.topic] = _Topic()
				# Only the topics with values can be selected, not the levels.
				item.setCheckState(self.SELECT_COLUMN, Qt.Unchecked)
			topic.update(v.value, v.ts, now)
			item.setText(self.VALUE_COLUMN, format_value(v.value))
			item.setText(self.TYPE_COLUMN, type_name(v.value))
		self._refresh()

	def _item(self, topic):
		"""The item of ``topic``, added with the items of its levels if not shown yet."""
		item = self._items.get(topic)
		if item is not None:
			return item
		levels = topic.split('/')
		if len(levels) > 1:
			parent = self._item('/'.join(levels[:-1]))
		else:
			parent = self.invisibleRootItem()

		item = QTreeWidgetItem([levels[-1]])
		item.setData(self.TOPIC_COLUMN, self.TOPIC_ROLE, topic)
		item.setToolTip(self.TOPIC_COLUMN, topic)
		for column in (self.SEEN_COLUMN, self.RATE_COLUMN):
			item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
		self._setStale(item, False)
		# Keep the siblings sorted by name.
		row = 0
		while row < parent.childCount() and parent.child(row).text(self.TOPIC_COLUMN) <= levels[-1]:
			row += 1
		parent.insertChild(row, item)
		# A new level is shown expanded, the topics below it are not hidden.
		parent.setExpanded(True)
		self._items[topic] = item
		return item

	def _setStale(self, item, stale):
		if stale:
			for column in range(self.columnCount()):
				item.setForeground(column, QColor(self.STALE_COLOR))
			self._stale.add(item)
		else:
			self.styleName(item)
			for column in range(1, self.columnCount()):
				item.setData(column, Qt.ForegroundRole, None)
			item.setForeground(self.TYPE_COLUMN, QColor(self.TYPE_COLOR))
			self._stale.discard(item)

	@Slot()
	def _refresh(self):
		"""Update the times and rates of the topics and dim the stale ones."""
		now = time.monotonic()
		for name, topic in self._topics.items():
			item = self._items[name]
			item.setText(self.SEEN_COLUMN, f'{now - topic.seen:.1f} s ago')
			rate = topic.rate(now)
			item.setText(self.RATE_COLUMN, '' if rate is None else f'{rate:.2f}/s')
		root = self.invisibleRootItem()
		for row in range(root.childCount()):
			self._refreshStale(root.child(row), now)

	def _refreshStale(self, item, now):
		"""Dim ``item`` if it is stale and return whether it is: a topic which got no value lately, a level
		with only stale topics below it."""
		live = False
		for row in range(item.childCount()):
			if not self._refreshStale(item.child(row), now):
				live = True
		topic = self._topics.get(item.data(self.TOPIC_COLUMN, self.TOPIC_ROLE))
		if topic is not None and not topic.stale(now):
			live = True
		if live == (item in self._stale):
			self._setStale(item, not live)
		return not live

	def retain(self, filter):
		"""Remove the topics not matching the topic ``filter``, with the levels left empty."""
		for name in [name for name in self._topics if not topic_matches(filter, name)]:
			del self._topics[name]
			if name in self._selected:
				self._setSelection(name, False)
			item = self._items[name]
			# The values are gone, the level stays while it has topics below it.
			for column in range(1, self.columnCount()):
				item.setText(column, '')
			item.setData(self.SELECT_COLUMN, Qt.CheckStateRole, None)
		# The deepest items first, a level is left empty once its children are removed.
		for name in sorted(self._items, key=lambda name: -name.count('/')):
			item = self._items[name]
			if item.childCount() == 0 and name not in self._topics:
				(item.parent() or self.invisibleRootItem()).removeChild(item)
				self._stale.discard(item)
				del self._items[name]

	def clearTopics(self):
		for name in list(self._selected):
			self._setSelection(name, False)
		self.clear()
		self._items = {}
		self._topics = {}
		self._stale = set()


class _ValuePanel(QDockWidget):
	"""A dock panel showing the last value of one topic in big monospace letters, scaled to fill the panel.
	Dimmed while the topic is stale. Its top panel sets how the numbers are formatted, see
	:func:`format_number`. ``closed`` tells the user closed it."""

	closed = Signal(str)

	#: Colour of the value, and of a stale one.
	COLOR = '#1d3f7a'
	STALE_COLOR = '#c4c4c4'

	#: Colour of the border of the panel and of the line under its top panel.
	BORDER_COLOR = '#d6d6d6'

	#: Colour of the text of the top panel, subdued not to distract from the value.
	BAR_TEXT_COLOR = '#555'

	#: The largest and smallest font of the value, in pixels.
	MAX_FONT_PX = 96
	MIN_FONT_PX = 12

	def __init__(self, name):
		super().__init__(name)
		self.name = name
		# Identifies the panel in a saved arrangement.
		self.setObjectName(f'mq.{name}')
		self.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable |
		                 QDockWidget.DockWidgetFloatable)
		self._stale = None
		self._value = None
		# The widest integral part shown, kept by the constant integral part style.
		self._integral_width = 0

		# A plain row rather than a toolbar, which would hide the controls not fitting a narrow panel. The row
		# does not keep the panel wide, it is cut off on the right instead.
		self._tb = QWidget()
		self._tb.setObjectName('bar')
		self._tb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
		row = QHBoxLayout(self._tb)
		row.setSizeConstraint(QLayout.SetNoConstraint)
		row.setContentsMargins(8, 4, 8, 4)
		row.setSpacing(4)
		self._precision = QComboBox()
		self._precision.addItems(PRECISIONS)
		self._precision.setCurrentIndex(DEFAULT_PRECISION)
		self._precision.setToolTip(self.tr('The number of decimals, the number of digits after the first one '
		                                   'with a constant number of digits'))
		self._precision.currentIndexChanged.connect(self._formatChanged)
		row.addWidget(self._precision)
		self._style = QComboBox()
		self._style.addItems([self.tr(style) for style in STYLES])
		self._style.setCurrentIndex(STYLES.index(DEFAULT_STYLE))
		self._style.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
		self._style.setToolTip(self.tr('How the number is laid out'))
		self._style.currentIndexChanged.connect(self._formatChanged)
		row.addWidget(self._style)
		row.addSpacing(8)
		self._zero_pad = QCheckBox(self.tr('Zero padding'))
		self._zero_pad.setChecked(True)
		self._zero_pad.setToolTip(self.tr('Pad with leading and trailing zeros instead of spaces'))
		self._zero_pad.toggled.connect(self._formatChanged)
		row.addWidget(self._zero_pad)
		row.addStretch()

		self._label = QLabel()
		self._label.setObjectName('value')
		self._label.setAlignment(Qt.AlignCenter)
		self._label.setTextFormat(Qt.PlainText)
		self._label.setMinimumSize(120, 60)
		self._label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
		font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
		font.setBold(True)
		self._label.setFont(font)
		self._label.installEventFilter(self)

		content = QWidget()
		content.setObjectName('content')
		content.setAttribute(Qt.WA_StyledBackground)
		content.setStyleSheet(f"""
			QWidget#content {{ border: 1px solid {self.BORDER_COLOR}; }}
			QWidget#bar QCheckBox, QWidget#bar QComboBox {{ color: {self.BAR_TEXT_COLOR}; }}
		""")
		layout = QVBoxLayout(content)
		# Inside the border.
		layout.setContentsMargins(1, 1, 1, 1)
		layout.setSpacing(0)
		layout.addWidget(self._tb)
		layout.addWidget(self._label, stretch=1)
		self.setWidget(content)
		self.setStale(False)

	def setValue(self, value):
		self._value = value
		self._show()

	def _show(self):
		value = self._value
		if isinstance(value, (int, float)) and not isinstance(value, bool):
			text, width = format_number(value, self._precision.currentIndex(), STYLES[self._style.currentIndex()],
			                            self._zero_pad.isChecked(), self._integral_width)
			self._integral_width = max(self._integral_width, width)
		else:
			text = format_value(value) or type_name(value)
		if text != self._label.text():
			self._label.setText(text)
			self._fit()

	@Slot()
	def _formatChanged(self):
		# The widest integral part is collected again for the new format.
		self._integral_width = 0
		self._show()

	def setStale(self, stale):
		if stale != self._stale:
			self._stale = stale
			self._label.setStyleSheet(f'QLabel#value {{ color: {self.STALE_COLOR if stale else self.COLOR}; '
			                          f'background-color: white; border-top: 1px solid {self.BORDER_COLOR}; }}')

	def eventFilter(self, obj, event):
		if obj is self._label and event.type() == QEvent.Resize:
			self._fit()
		return False

	def _fit(self):
		"""Size the font so the value fills the label."""
		text = self._label.text() or ' '
		font = self._label.font()
		font.setPixelSize(100)
		metrics = QFontMetrics(font)
		width = max(1, metrics.horizontalAdvance(text))
		height = max(1, metrics.height())
		scale = min((self._label.width() - 16) / width, (self._label.height() - 8) / height)
		size = int(max(self.MIN_FONT_PX, min(self.MAX_FONT_PX, 100 * scale)))
		if self._label.font().pixelSize() != size:
			font.setPixelSize(size)
			self._label.setFont(font)

	def closeEvent(self, event):
		super().closeEvent(event)
		self.closed.emit(self.name)


class MqDisplayArea(QMainWindow):
	"""The dock area of the Display tab, a :class:`_ValuePanel` for every selected topic. The panels can be
	arranged side by side, stacked, grouped into tabs or floated. ``panelClosed`` carries the topic of a panel
	the user closed."""

	panelClosed = Signal(str)

	#: How often the stale panels are updated, in milliseconds.
	REFRESH_MS = 500

	def __init__(self, tree):
		super().__init__()
		self._tree = tree
		self._panels = {}
		self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks |
		                    QMainWindow.GroupedDragging)
		self.setDockNestingEnabled(True)

		# Shown while there are no panels; hidden, it leaves the whole area to the panels.
		self._hint = QLabel(self.tr('Select topics in the Topics tab to show their values here.'))
		self._hint.setAlignment(Qt.AlignCenter)
		self._hint.setStyleSheet('color: #888;')
		self.setCentralWidget(self._hint)

		self._timer = QTimer(self)
		self._timer.setInterval(self.REFRESH_MS)
		self._timer.timeout.connect(self._refresh)
		self._timer.start()

	def add(self, name):
		if name in self._panels:
			return
		panel = _ValuePanel(name)
		panel.closed.connect(self._closed)
		topic = self._tree.topic(name)
		if topic is not None:
			panel.setValue(topic.value)
		# Next to the panels added before, the user may rearrange them.
		self.addDockWidget(Qt.TopDockWidgetArea, panel, Qt.Horizontal)
		self._panels[name] = panel
		self._hint.hide()
		self._refresh()

	def remove(self, name):
		panel = self._panels.pop(name, None)
		if panel is None:
			return
		panel.closed.disconnect(self._closed)
		self.removeDockWidget(panel)
		panel.deleteLater()
		self._hint.setVisible(not self._panels)

	def addValues(self, values):
		for v in values:
			panel = self._panels.get(v.topic)
			if panel is not None:
				panel.setValue(v.value)

	@Slot(str)
	def _closed(self, name):
		self.remove(name)
		self.panelClosed.emit(name)

	@Slot()
	def _refresh(self):
		now = time.monotonic()
		for name, panel in self._panels.items():
			topic = self._tree.topic(name)
			panel.setStale(topic is None or topic.stale(now))


class _Clock:
	"""Maps the timestamps a device publishes the values with to the host :func:`time.monotonic`, so the
	values polled together are graphed when they were published rather than when they arrived.

	The offset of the clocks is the smallest seen, the one of the value polled the soonest after it was
	published. It is found again if the device clock goes back, e.g. when the device restarts. Values without
	a timestamp are graphed when they arrived.
	"""

	def __init__(self):
		self._offset = None
		self._last = None

	def observe(self, ts, now):
		"""Take the timestamp ``ts`` of a value arriving ``now`` into account. Observe all the values polled
		together before mapping them, the oldest ones of a backlog would be mapped to now otherwise."""
		if not ts:
			return
		if self._last is not None and ts < self._last:
			self._offset = None
		self._last = ts
		offset = now - ts
		if self._offset is None or offset < self._offset:
			self._offset = offset

	def time(self, ts, now):
		if not ts or self._offset is None:
			return now
		return ts + self._offset


class _Series:
	"""The graphed history of one topic, the times and the values in arrays grown as needed. The times never
	go back, the graph relies on them being sorted. A gap is a NaN value, the line is not drawn across it."""

	INITIAL_SIZE = 1024

	def __init__(self):
		self._x = np.empty(self.INITIAL_SIZE)
		self._y = np.empty(self.INITIAL_SIZE)
		self._n = 0
		#: Whether there are values not graphed yet.
		self.dirty = False

	def append(self, x, y):
		if self._n == len(self._x):
			self._x = np.resize(self._x, 2 * self._n)
			self._y = np.resize(self._y, 2 * self._n)
		if self._n:
			x = max(x, self._x[self._n - 1])
		self._x[self._n] = x
		self._y[self._n] = y
		self._n += 1
		self.dirty = True

	def gap(self):
		"""Break the line after the last value."""
		if self._n and not math.isnan(self._y[self._n - 1]):
			self.append(self._x[self._n - 1], math.nan)

	@property
	def x(self):
		return self._x[:self._n]

	@property
	def y(self):
		return self._y[:self._n]


class _TimeAxis(pg.AxisItem):
	"""The time axis in seconds with minor ticks every 10 s and major ones every minute, finer ones for the
	short spans. The minor ticks are left out when they would be too dense to tell apart, the labels are
	thinned out to fit."""

	MINOR_S = 10
	MAJOR_S = 60

	#: The steps of the labels, the shortest the labels fit with is used.
	LABEL_STEPS = (1, 5, 10, 60, 120, 300, 600, 900, 1800, 3600)

	#: The space between two labels, in characters.
	LABEL_GAP = 3

	#: The least space between two minor ticks, in pixels.
	MIN_TICK_SPACING = 5

	def __init__(self):
		super().__init__('bottom')
		self._label_step = self.MAJOR_S

	def tickSpacing(self, minVal, maxVal, size):
		span = maxVal - minVal
		per_s = size / span if span > 0 else 0
		levels = [(self.MAJOR_S, 0)]
		if self.MINOR_S * per_s >= self.MIN_TICK_SPACING:
			levels.append((self.MINOR_S, 0))
		if span <= 10:
			levels.append((1, 0))
		elif span <= 30:
			levels.append((5, 0))
		finest = levels[-1][0]

		metrics = QFontMetrics(self.style['tickFont'] or self.font())
		widest = max(len(f'{minVal:.0f}'), len(f'{maxVal:.0f}')) + self.LABEL_GAP
		width = widest * metrics.horizontalAdvance('0')
		self._label_step = next((step for step in self.LABEL_STEPS if step >= finest and step * per_s >= width),
		                        self.LABEL_STEPS[-1])
		return levels

	def tickStrings(self, values, scale, spacing):
		return [f'{v:.0f}' if round(v) % self._label_step == 0 else '' for v in values]


class MqGraph(QWidget):
	"""The Graph tab, a line of every selected topic over time. The time is in seconds since the graph was
	started or cleared. Its top panel sets the time span shown and whether the graph follows the newest values,
	the span ending now. Not following, the graph shows the span the current time falls into, moving on by the
	whole span when the time reaches its end.

	The values of the selected topics are kept until cleared, also of the topics deselected meanwhile, their
	line has a gap while they were not selected. The lines are downsampled to the pixels and clipped to the
	time shown, a long history does not slow the graph down."""

	#: How often the graph is redrawn while it is shown, in milliseconds: when the time moves on by a pixel,
	#: but not more often than the shortest nor less often than the longest interval.
	MIN_REFRESH_MS = 50
	MAX_REFRESH_MS = 1000

	#: Colours of the line under the top panel, of the axes, of their text and of the text of the top panel.
	BORDER_COLOR = '#d6d6d6'
	AXIS_COLOR = '#888'
	TEXT_COLOR = '#333'
	BAR_TEXT_COLOR = '#555'

	#: The width of the lines, in pixels.
	LINE_WIDTH = 1.5

	def __init__(self):
		super().__init__()
		self._t0 = time.monotonic()
		# The series by the topic, of all the topics selected since cleared; the lines and their colours of the
		# selected ones.
		self._series = {}
		self._curves = {}
		self._colors = {}
		# The clocks of the devices publishing the values by the device name.
		self._clocks = {}

		font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
		font.setPointSizeF(QApplication.font().pointSizeF())
		self._font = font

		bar = QWidget()
		bar.setObjectName('bar')
		bar.setAttribute(Qt.WA_StyledBackground)
		row = QHBoxLayout(bar)
		row.setContentsMargins(8, 4, 8, 4)
		row.setSpacing(4)
		label = QLabel(self.tr('Time span:'))
		label.setContentsMargins(0, 0, 4, 0)
		row.addWidget(label)
		self._span = QComboBox()
		for text, _span in GRAPH_SPANS:
			self._span.addItem(text)
		self._span.setCurrentIndex(DEFAULT_GRAPH_SPAN)
		self._span.setToolTip(self.tr('The time shown in the graph'))
		self._span.currentIndexChanged.connect(self._refresh)
		row.addWidget(self._span)
		row.addSpacing(8)
		self._follow = QCheckBox(self.tr('Auto follow'))
		self._follow.setChecked(True)
		self._follow.setToolTip(self.tr('Keep the newest values on the right, else the graph moves on by the '
		                                'whole span'))
		self._follow.toggled.connect(self._refresh)
		row.addWidget(self._follow)
		row.addStretch()

		self._plot = pg.PlotWidget(background='w', axisItems={'bottom': _TimeAxis()})
		item = self._plot.getPlotItem()
		item.setMenuEnabled(False)
		item.hideButtons()
		# Only the time shown is drawn, reduced to the minimum and the maximum of every pixel. Set for the
		# lines as they are added.
		item.setClipToView(True)
		item.setDownsampling(auto=True, mode='peak')
		item.showGrid(x=True, y=True, alpha=0.3)
		for name in ('bottom', 'left'):
			axis = item.getAxis(name)
			axis.setPen(pg.mkPen(self.AXIS_COLOR))
			axis.setTextPen(pg.mkPen(self.TEXT_COLOR))
			axis.setTickFont(font)
			axis.label.setFont(font)
		bottom = item.getAxis('bottom')
		# The time labels are thinned out to fit by the axis, all of them are shown.
		bottom.setStyle(textFillLimits=[(0, 0.95)])
		bottom.enableAutoSIPrefix(False)
		item.setLabel('bottom', self.tr('Time (s)'), color=self.TEXT_COLOR)
		self._legend = item.addLegend(offset=(10, 10), brush=pg.mkBrush(255, 255, 255, 220),
		                              pen=pg.mkPen(self.BORDER_COLOR), labelTextColor=self.TEXT_COLOR,
		                              labelTextSize=f'{font.pointSizeF():g}pt')
		# The time is set by the span and the values fill the height of the time shown.
		view = item.getViewBox()
		view.setMouseEnabled(x=False, y=False)
		view.disableAutoRange(pg.ViewBox.XAxis)
		view.enableAutoRange(pg.ViewBox.YAxis)
		view.setAutoVisible(y=True)

		# Shown while there are no lines.
		self._hint = QLabel(self.tr('Select topics in the Topics tab to graph their values here.'))
		self._hint.setAlignment(Qt.AlignCenter)
		self._hint.setStyleSheet('color: #888;')
		self._stack = QStackedWidget()
		self._stack.addWidget(self._hint)
		self._stack.addWidget(self._plot)

		self.setStyleSheet(f"""
			QWidget#bar {{ border-bottom: 1px solid {self.BORDER_COLOR}; }}
			QWidget#bar QLabel, QWidget#bar QCheckBox, QWidget#bar QComboBox {{ color: {self.BAR_TEXT_COLOR}; }}
		""")
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(bar)
		layout.addWidget(self._stack, stretch=1)

		self._timer = QTimer(self)
		self._timer.setInterval(self.MIN_REFRESH_MS)
		self._timer.timeout.connect(self._refresh)
		self._timer.start()
		self._refresh()

	def add(self, name):
		if name in self._curves:
			return
		series = self._series.get(name)
		if series is None:
			series = self._series[name] = _Series()
		else:
			# Not graphed while it was not selected.
			series.gap()
		used = set(self._colors.values())
		color = next((c for c in GRAPH_COLORS if c not in used), GRAPH_COLORS[len(self._colors) % len(GRAPH_COLORS)])
		self._colors[name] = color
		# The peaks of every half a pixel rather than of a fraction of it keep the lines of a long time span quick
		# to draw.
		curve = pg.PlotDataItem(name=name, pen=pg.mkPen(color, width=self.LINE_WIDTH), antialias=True,
		                        connect='finite', autoDownsampleFactor=2.0)
		curve.setData(series.x, series.y)
		series.dirty = False
		self._plot.addItem(curve)
		label = self._legend.getLabel(curve)
		if label is not None:
			label.item.setFont(self._font)
			label.setText(name)
		self._curves[name] = curve
		self._stack.setCurrentWidget(self._plot)
		self._refresh()

	def remove(self, name):
		curve = self._curves.pop(name, None)
		if curve is None:
			return
		del self._colors[name]
		self._plot.removeItem(curve)
		if not self._curves:
			self._stack.setCurrentWidget(self._hint)

	def addValues(self, values):
		now = time.monotonic()
		values = [v for v in values if v.topic in self._curves]
		for v in values:
			clock = self._clocks.get(v.host)
			if clock is None:
				clock = self._clocks[v.host] = _Clock()
			clock.observe(v.ts, now)
		for v in values:
			y = graph_value(v.value)
			if y is not None:
				self._series[v.topic].append(self._clocks[v.host].time(v.ts, now) - self._t0, y)

	def clear(self):
		"""Forget the history of all the topics and start the time again."""
		self._t0 = time.monotonic()
		self._clocks = {}
		self._series = {name: _Series() for name in self._curves}
		for name, curve in self._curves.items():
			curve.setData(self._series[name].x, self._series[name].y)
		self._refresh()

	def showEvent(self, event):
		super().showEvent(event)
		self._refresh()

	@Slot()
	def _refresh(self):
		"""Graph the new values and move the time shown on. Only while the graph is shown."""
		if not self._plot.isVisible():
			return
		for name, curve in self._curves.items():
			series = self._series[name]
			if series.dirty:
				curve.setData(series.x, series.y)
				series.dirty = False

		span = GRAPH_SPANS[self._span.currentIndex()][1]
		t = time.monotonic() - self._t0
		if self._follow.isChecked():
			start = t - span
		else:
			start = math.floor(t / span) * span
		self._plot.setXRange(start, start + span, padding=0)
		ms = 1000 * span / max(1, self._plot.getPlotItem().getViewBox().width())
		self._timer.setInterval(int(min(self.MAX_REFRESH_MS, max(self.MIN_REFRESH_MS, ms))))


class MqLiveData(QWidget):
	"""The live data with its toolbars: Pause, Clear and the poll rate on the left and the topic filter on the
	right at the top, the polling status and the message statistics at the bottom. In between, the tabs of the
	topic tree, of the display and of the graph of the selected topics."""

	#: The colour of the status, and of an invalid filter.
	STATUS_COLOR = '#888'
	INVALID_BORDER = '#c0392b'

	def __init__(self, side):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._tree = MqLiveDataTreeWidget()
		self._display = MqDisplayArea(self._tree)
		self._graph = MqGraph()
		self._tree.selectionChanged.connect(self._selectionChanged)
		self._display.panelClosed.connect(lambda name: self._tree.setSelected(name, False))
		# The filter the poller sends, and the totals shown in the status.
		self._topic = DEFAULT_TOPIC
		self._messages = 0
		self._missed = 0
		self._paused = False
		self._no_reply = None

		self._tb = QToolBar()
		self._pause = DauiButton(self.tr('Pause'), Icons.pause())
		self._pause.setToolTip(self.tr('Stop polling the device, it keeps buffering the values for a while'))
		self._pause.clicked.connect(self._togglePause)
		self._tb.addWidget(self._pause)
		clear = DauiButton(self.tr('Clear'), Icons.clear())
		clear.setToolTip(self.tr('Remove all the topics shown and the history of the graph'))
		clear.clicked.connect(self._clear)
		self._tb.addWidget(clear)

		label = QLabel(self.tr('Poll rate:'))
		label.setContentsMargins(10, 0, 4, 0)
		self._tb.addWidget(label)
		self._rate = QComboBox()
		for text, _interval in POLL_INTERVALS:
			self._rate.addItem(text)
		self._rate.setCurrentIndex(DEFAULT_POLL_INTERVAL)
		self._rate.setToolTip(self.tr('How often the device is asked for new values, a backlog is fetched right away'))
		self._rate.setFixedHeight(self._pause.sizeHint().height())
		self._rate.currentIndexChanged.connect(self._rateChanged)
		self._tb.addWidget(self._rate)

		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)

		label = QLabel(self.tr('Topic filter:'))
		label.setContentsMargins(6, 0, 4, 0)
		self._tb.addWidget(label)
		self._filter = QLineEdit(DEFAULT_TOPIC)
		self._filter.setPlaceholderText(self.tr('e.g. sensor/#'))
		self._filter.setToolTip(self.tr('The topics the device sends, "+" matches one level, "#" all the levels '
		                                 'below. Press Enter to apply.'))
		self._filter.setFixedWidth(220)
		# As tall as the buttons next to it.
		self._filter.setFixedHeight(self._pause.sizeHint().height())
		self._filter.textChanged.connect(self._validateFilter)
		self._filter.editingFinished.connect(self._applyFilter)
		self._validateFilter(self._filter.text())
		self._tb.addWidget(self._filter)
		cancel = DauiButton(btn_icon=Icons.cancel())
		cancel.setToolTip(self.tr('Reset the filter to all the topics'))
		cancel.clicked.connect(self._resetFilter)
		self._tb.addWidget(cancel)

		self._bottom = QToolBar()
		self._status = QLabel()
		self._status.setContentsMargins(6, 0, 0, 0)
		self._status.setStyleSheet(f'color: {self.STATUS_COLOR};')
		self._bottom.addWidget(self._status)
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._bottom.addWidget(spacer)
		self._stats = QLabel()
		self._stats.setContentsMargins(0, 0, 6, 0)
		self._stats.setStyleSheet(f'color: {self.STATUS_COLOR};')
		self._bottom.addWidget(self._stats)

		self._layout = QVBoxLayout()
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.addWidget(self._tb)
		self._tabs = QTabWidget()
		self._tabs.setTabPosition(QTabWidget.North)
		self._tabs.setDocumentMode(True)
		self._tabs.addTab(self._tree, Icons.liveData(), self.tr('Topics'))
		self._tabs.addTab(self._display, Icons.data(), self.tr('Display'))
		self._tabs.addTab(self._graph, Icons.graph(), self.tr('Graph'))
		self._layout.addWidget(self._tabs)
		self._layout.addWidget(self._bottom)
		self.setLayout(self._layout)

		self._poller = _Poller(side, self._topic, POLL_INTERVALS[DEFAULT_POLL_INTERVAL][1])
		self._poller.received.connect(self._received)
		self._poller.noReply.connect(self._noReply)
		self._poller.start()
		self._updateStatus()

	def stop(self):
		self._poller.stop()

	@Slot(list, int)
	def _received(self, values, missed):
		self._no_reply = None
		# Values buffered by the device before the filter changed may still arrive.
		values = [v for v in values if topic_matches(self._topic, v.topic)]
		self._messages += len(values)
		self._missed += missed
		self._tree.addValues(values)
		self._display.addValues(values)
		self._graph.addValues(values)
		self._updateStatus()

	@Slot(str, bool)
	def _selectionChanged(self, name, selected):
		if selected:
			self._display.add(name)
			self._graph.add(name)
		else:
			self._display.remove(name)
			self._graph.remove(name)

	@Slot(str)
	def _noReply(self, error):
		self._no_reply = error or self.tr('no reply')
		self._updateStatus()

	def _updateStatus(self):
		if self._paused:
			status = self.tr('Paused')
		elif self._no_reply is not None:
			status = self.tr('Waiting for the device (%s)...') % self._no_reply
		elif self._messages == 0:
			status = self.tr('Waiting for data...')
		else:
			status = self.tr('Polling every %s') % self._rate.currentText()
		self._status.setText(status)

		stats = self.tr('%d topics, %d messages') % (self._tree.topicCount, self._messages)
		if self._missed:
			stats += self.tr(', %d batches lost') % self._missed
		self._stats.setText(stats)

	@Slot(int)
	def _rateChanged(self, index):
		text, interval = POLL_INTERVALS[index]
		self._logger.info(f'poll interval {text}')
		self._poller.setInterval(interval)
		self._updateStatus()

	@Slot()
	def _togglePause(self):
		self._paused = not self._paused
		self._poller.setPaused(self._paused)
		self._pause.setText(self.tr('Resume') if self._paused else self.tr('Pause'))
		self._pause.setIcon(Icons.play() if self._paused else Icons.pause())
		self._updateStatus()

	@Slot()
	def _clear(self):
		self._tree.clearTopics()
		self._graph.clear()
		self._messages = 0
		self._missed = 0
		self._updateStatus()

	@Slot(str)
	def _validateFilter(self, text):
		border = '' if is_valid_filter(text.strip()) else f' border: 1px solid {self.INVALID_BORDER};'
		self._filter.setStyleSheet(f'QLineEdit {{ background-color: white;{border} }}')

	@Slot()
	def _applyFilter(self):
		topic = self._filter.text().strip()
		if topic == self._topic or not is_valid_filter(topic):
			return
		self._logger.info(f'topic filter {topic!r}')
		self._topic = topic
		self._poller.setTopic(topic)
		self._tree.retain(topic)
		self._updateStatus()

	@Slot()
	def _resetFilter(self):
		self._filter.setText(DEFAULT_TOPIC)
		self._applyFilter()


class MqLiveDataWindow(AppletWindow):
	"""The values published to the message queue of the connected device, polled over an interface speaking
	the mq protocol. The window closes when the device is disconnected."""

	def __init__(self, connection, name):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)

		self._logger.info(f'opening MQ live data on interface {name!r}')
		# The remote serves a single session and a single cursor.
		self._side = connection.sharedSide(name)
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(f'MQ live data ({name})')
		self._live = MqLiveData(self._side)
		self.setWidget(self._live)
		self.resize(700, 500)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._live.stop()
		super().closeEvent(event)
