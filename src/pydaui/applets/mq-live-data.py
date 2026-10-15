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
message queue. The selected topics are shown in big letters in the Display tab, graphed over time in the
Graph tab, their precision is assessed in the Statistics tab and their distributions are compared in the
Histogram tab.

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

from pydaui.ui.helpers import AppletWindow, DauiButton, StyledTreeWidget
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

	def window(self, start, end):
		"""The times and the values from ``start`` to ``end``."""
		x = self.x
		first, last = int(np.searchsorted(x, start, side='left')), int(np.searchsorted(x, end, side='right'))
		return x[first:last], self.y[first:last]


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


def monospace_font():
	"""The monospace font of the size of the user interface font, of the numbers and the graphs."""
	font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
	font.setPointSizeF(QApplication.font().pointSizeF())
	return font


class _Plot(pg.PlotWidget):
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

	#: Colours of the line under the top panel and of its text.
	BORDER_COLOR = '#d6d6d6'
	BAR_TEXT_COLOR = '#555'

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

		self._plot = _Plot(_TimeAxis())
		item = self._plot.getPlotItem()
		# Only the time shown is drawn, reduced to the minimum and the maximum of every pixel. Set for the
		# lines as they are added.
		item.setClipToView(True)
		item.setDownsampling(auto=True, mode='peak')
		bottom = item.getAxis('bottom')
		# The time labels are thinned out to fit by the axis, all of them are shown.
		bottom.setStyle(textFillLimits=[(0, 0.95)])
		self._plot.setAxisLabel('bottom', self.tr('Time (s)'))
		# The time is set by the span and the values fill the height of the time shown.
		view = item.getViewBox()
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
		curve = pg.PlotDataItem(name=name, pen=pg.mkPen(color, width=_Plot.LINE_WIDTH), antialias=True,
		                        connect='finite', autoDownsampleFactor=2.0)
		curve.setData(series.x, series.y)
		series.dirty = False
		self._plot.addLine(curve)
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

	@property
	def topics(self):
		"""The graphed topics and the colours of their lines, in the order they were selected."""
		return dict(self._colors)

	def window(self, name):
		"""The times and the values of the topic ``name`` in the time shown."""
		return self._series[name].window(*self.timeRange())

	def timeRange(self):
		"""The time shown as ``(start, end)``, in seconds since the graph was started or cleared."""
		span = GRAPH_SPANS[self._span.currentIndex()][1]
		t = time.monotonic() - self._t0
		start = t - span if self._follow.isChecked() else math.floor(t / span) * span
		return start, start + span

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

		start, end = self.timeRange()
		self._plot.setXRange(start, end, padding=0)
		ms = 1000 * (end - start) / max(1, self._plot.getPlotItem().getViewBox().width())
		self._timer.setInterval(int(min(self.MAX_REFRESH_MS, max(self.MIN_REFRESH_MS, ms))))

#: The deviation from the mean, in standard deviations, a value is counted as an outlier beyond.
OUTLIER_S = 4


def precision(t, v):
	"""The precision metrics of the values ``v`` received at the times ``t`` (sorted, in seconds), a Type A
	evaluation in the terms of the GUM. Returns a dict, a metric is missing when there are too few values to
	compute it. The NaN values (gaps) are left out.

	- ``n``, ``span``, ``rate``: the number of values, the time between the first and the last one and the
	  values per second.
	- ``mean``, ``min``, ``max``, ``p2p``: the mean, the extremes and the peak-to-peak range.
	- ``s``, ``rsd``: the experimental standard deviation and the relative one, in %. The repeatability.
	- ``n_eff``, ``u_mean``: the number of effectively independent values, by the lag-1 autocorrelation of the
	  deviations from the mean (an AR(1) process), and the standard uncertainty of the mean ``s/sqrt(n_eff)``.
	  Correlated values, oversampled or filtered by the sensor, carry less information than their count.
	- ``drift``, ``r2``, ``s_detrended``: the slope of the least squares line per hour, the coefficient of
	  determination of the line and the standard deviation around it, the noise without the drift.
	- ``q``, ``u_q``, ``levels``: the resolution as the smallest step between the distinct values, its
	  standard uncertainty ``q/sqrt(12)`` (Type B) and the number of the distinct values. The resolution only
	  when the values repeat, at most half of them distinct. Else they are not quantized visibly and the
	  smallest step is a matter of chance.
	- ``outliers``: the number of values farther than :data:`OUTLIER_S` standard deviations from the mean.
	"""
	finite = np.isfinite(v)
	t = t[finite]
	v = v[finite]
	n = len(v)
	m = {'n': n}
	if n == 0:
		return m
	span = float(t[-1] - t[0])
	m['span'] = span
	if span > 0:
		m['rate'] = (n - 1) / span
	mean = float(v.mean())
	m['mean'] = mean
	m['min'] = float(v.min())
	m['max'] = float(v.max())
	m['p2p'] = m['max'] - m['min']

	levels = np.unique(v)
	m['levels'] = len(levels)
	if 1 < len(levels) <= n / 2:
		q = float(np.diff(levels).min())
		m['q'] = q
		m['u_q'] = q / math.sqrt(12)

	if n < 2:
		return m
	d = v - mean
	s = float(math.sqrt(np.dot(d, d) / (n - 1)))
	m['s'] = s
	if mean:
		m['rsd'] = 100 * s / abs(mean)
	m['outliers'] = int(np.count_nonzero(np.abs(d) > OUTLIER_S * s))

	ss = float(np.dot(d, d))
	if ss > 0:
		rho = float(np.dot(d[:-1], d[1:])) / ss
		n_eff = n * (1 - rho) / (1 + rho) if rho > 0 else n
		n_eff = min(n, max(1.0, n_eff))
		m['n_eff'] = n_eff
		m['u_mean'] = s / math.sqrt(n_eff)
	else:
		m['n_eff'] = float(n)
		m['u_mean'] = 0.0

	if n >= 3 and span > 0:
		tc = t - t.mean()
		slope = float(np.dot(tc, d) / np.dot(tc, tc))
		residuals = d - slope * tc
		ss_res = float(np.dot(residuals, residuals))
		m['drift'] = 3600 * slope
		if ss > 0:
			m['r2'] = 1 - ss_res / ss
		m['s_detrended'] = math.sqrt(ss_res / (n - 2))
	return m


def _g(value):
	return f'{value:.6g}'


def _u(value):
	"""An uncertainty, rounded to two significant digits by the GUM."""
	return f'{value:.2g}'


#: The URL of a clause of the International vocabulary of metrology (VIM, JCGM 200:2012).
VIM_URL = 'https://jcgm.bipm.org/vim/en/{}.html'

#: The terms of the VIM clauses the statistics refer to.
VIM_TERMS = {
	'2.10': 'measured quantity value',
	'2.15': 'measurement precision',
	'2.19': 'random measurement error',
	'2.20': 'repeatability condition of measurement',
	'2.21': 'measurement repeatability',
	'2.28': 'Type A evaluation of measurement uncertainty',
	'2.29': 'Type B evaluation of measurement uncertainty',
	'2.30': 'standard measurement uncertainty',
	'4.15': 'resolution of a displaying device',
	'4.21': 'instrumental drift',
}

#: The rows of the statistics as groups of ``(key, name, format, VIM clause, description)``, the key of a
#: metric of :func:`precision`, the clause of :data:`VIM_TERMS` defining it, ``None`` for the metrics the VIM
#: does not cover.
STATISTICS = (
	('Samples', (
		('n', 'Count N', str, '2.20', 'The number of values in the time shown in the Graph tab'),
		('span', 'Time span', lambda v: f'{v:.1f} s', '2.20', 'The time between the first and the last value'),
		('rate', 'Sample rate', lambda v: f'{v:.3g}/s', None, 'The values per second'),
	)),
	('Location', (
		('mean', 'Mean', _g, '2.10', 'The arithmetic mean, the best estimate of the value'),
		('min', 'Minimum', _g, '2.10', 'The smallest value'),
		('max', 'Maximum', _g, '2.10', 'The largest value'),
	)),
	('Dispersion', (
		('s', 'Standard deviation s', _g, '2.21', 'The experimental standard deviation, the repeatability'),
		('rsd', 'Relative std. deviation', lambda v: f'{v:.3g} %', '2.15', 'The standard deviation relative to '
		 'the mean, the coefficient of variation'),
		('p2p', 'Peak-to-peak', _g, '2.19', 'The difference of the largest and the smallest value'),
		('u_mean', 'Std. uncertainty of mean', _u, '2.30', 'The standard uncertainty of the mean, s/\u221aN_eff'),
		('n_eff', 'Effective N', lambda v: f'{v:.1f}', '2.28', 'The number of effectively independent values, by '
		 'the lag-1 autocorrelation, lower than N when the values are correlated (oversampled or filtered)'),
		('outliers', f'Outliers (>{OUTLIER_S} s)', str, None, f'The number of values farther than {OUTLIER_S} '
		 'standard deviations from the mean'),
	)),
	('Stability', (
		('drift', 'Drift', lambda v: f'{v:.4g}/h', '4.21', 'The slope of the least squares line, per hour'),
		('r2', 'Drift R\u00b2', lambda v: f'{v:.3f}', '4.21', 'How much of the variance the drift explains, 0 to 1'),
		('s_detrended', 'Detrended std. deviation', _g, '2.19', 'The standard deviation around the drift line, '
		 'the noise without the drift'),
	)),
	('Resolution', (
		('q', 'Resolution q', _g, '4.15', 'The smallest step between the distinct values, only when the values '
		 'repeat. When the standard deviation is not much larger, the quantization dominates the noise'),
		('u_q', 'Resolution uncertainty', _u, '2.29', 'The standard uncertainty of the resolution, q/\u221a12'),
		('levels', 'Distinct values', str, '4.15', 'The number of the distinct values'),
	)),
)


class MqStatistics(QWidget):
	"""The Statistics tab, the precision metrics of :func:`precision` of every graphed topic over the time shown
	in the Graph tab. A column for every topic, in the colour of its line. The VIM column refers to the clause of
	the vocabulary defining the metric, a click opens it."""

	METRIC_COLUMN = 0
	VIM_COLUMN = 1
	#: The first column of the topics.
	TOPIC_COLUMN = 2

	#: Colour of the VIM references.
	VIM_COLOR = '#1d3f7a'

	#: How often the statistics are computed while they are shown, in milliseconds.
	REFRESH_MS = 500

	BORDER_COLOR = '#d6d6d6'
	BAR_TEXT_COLOR = '#555'

	#: The width of the topic columns, in characters of the monospace font.
	COLUMN_CHARS = 16

	def __init__(self, graph):
		super().__init__()
		self._graph = graph
		self._topics = {}
		# The items of the metrics by the key.
		self._items = {}
		self._font = font = monospace_font()

		bar = QWidget()
		bar.setObjectName('bar')
		bar.setAttribute(Qt.WA_StyledBackground)
		row = QHBoxLayout(bar)
		row.setContentsMargins(8, 4, 8, 4)
		self._window = QLabel()
		self._window.setToolTip(self.tr('The statistics cover the time shown in the Graph tab, set its time span '
		                                'there'))
		row.addWidget(self._window)
		row.addStretch()

		self._tree = StyledTreeWidget()
		self._tree.setRootIsDecorated(False)
		self._tree.setIndentation(12)
		self._tree.setSelectionMode(QAbstractItemView.NoSelection)
		self._tree.setFocusPolicy(Qt.NoFocus)
		header = self._tree.header()
		header.setStretchLastSection(False)
		header.setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
		header.setMinimumSectionSize(QFontMetrics(font).horizontalAdvance('0') * self.COLUMN_CHARS)
		for group, metrics in STATISTICS:
			parent = QTreeWidgetItem([self.tr(group)])
			self._tree.addTopLevelItem(parent)
			self._tree.styleName(parent)
			for key, name, _format, clause, description in metrics:
				item = QTreeWidgetItem(parent, [self.tr(name)])
				item.setToolTip(self.METRIC_COLUMN, self.tr(description))
				if clause is not None:
					item.setText(self.VIM_COLUMN, clause)
					item.setData(self.VIM_COLUMN, Qt.UserRole, VIM_URL.format(clause))
					item.setToolTip(self.VIM_COLUMN, self.tr('VIM %s %s, click to open') % (clause, VIM_TERMS[clause]))
					item.setForeground(self.VIM_COLUMN, QColor(self.VIM_COLOR))
					font = item.font(self.VIM_COLUMN)
					font.setUnderline(True)
					item.setFont(self.VIM_COLUMN, font)
				else:
					item.setText(self.VIM_COLUMN, '\u2013')
					item.setToolTip(self.VIM_COLUMN, self.tr('Not defined by the VIM'))
				item.setTextAlignment(self.VIM_COLUMN, Qt.AlignCenter)
				self._items[key] = item
			parent.setExpanded(True)
		self._tree.itemClicked.connect(self._clicked)

		# Shown while there are no topics.
		self._hint = QLabel(self.tr('Select topics in the Topics tab to assess their precision here.'))
		self._hint.setAlignment(Qt.AlignCenter)
		self._hint.setStyleSheet('color: #888;')
		self._stack = QStackedWidget()
		self._stack.addWidget(self._hint)
		self._stack.addWidget(self._tree)
		self._setColumns({})

		self.setStyleSheet(f"""
			QWidget#bar {{ border-bottom: 1px solid {self.BORDER_COLOR}; }}
			QWidget#bar QLabel {{ color: {self.BAR_TEXT_COLOR}; }}
		""")
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(bar)
		layout.addWidget(self._stack, stretch=1)

		self._timer = QTimer(self)
		self._timer.setInterval(self.REFRESH_MS)
		self._timer.timeout.connect(self._refresh)
		self._timer.start()

	def _setColumns(self, topics):
		"""A column for every topic of ``topics``, the colours by the names."""
		self._topics = topics
		self._tree.setColumnCount(self.TOPIC_COLUMN + len(topics))
		header = self._tree.headerItem()
		header.setText(self.METRIC_COLUMN, self.tr('Metric'))
		header.setTextAlignment(self.METRIC_COLUMN, Qt.AlignLeft | Qt.AlignVCenter)
		header.setText(self.VIM_COLUMN, self.tr('VIM'))
		header.setTextAlignment(self.VIM_COLUMN, Qt.AlignCenter)
		header.setToolTip(self.VIM_COLUMN, self.tr('The clause of the International vocabulary of metrology '
		                                           '(JCGM 200:2012) defining the metric'))
		for column, (name, color) in enumerate(topics.items(), self.TOPIC_COLUMN):
			header.setText(column, name)
			header.setToolTip(column, name)
			header.setForeground(column, QColor(color))
			for item in self._items.values():
				item.setText(column, '')
				item.setFont(column, self._font)
				item.setForeground(column, QColor(color))
				item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
		self._tree.header().setSectionResizeMode(self.METRIC_COLUMN, QHeaderView.ResizeToContents)
		self._tree.header().setSectionResizeMode(self.VIM_COLUMN, QHeaderView.ResizeToContents)
		for column in range(self.TOPIC_COLUMN, self.TOPIC_COLUMN + len(topics)):
			self._tree.header().setSectionResizeMode(column, QHeaderView.Stretch)
		self._stack.setCurrentWidget(self._tree if topics else self._hint)

	@Slot(QTreeWidgetItem, int)
	def _clicked(self, item, column):
		url = item.data(self.VIM_COLUMN, Qt.UserRole) if column == self.VIM_COLUMN else None
		if url:
			QDesktopServices.openUrl(QUrl(url))

	def showEvent(self, event):
		super().showEvent(event)
		self._refresh()

	@Slot()
	def _refresh(self):
		if not self.isVisible():
			return
		topics = self._graph.topics
		if topics != self._topics:
			self._setColumns(topics)
		start, end = self._graph.timeRange()
		self._window.setText(self.tr('Over the time shown in the Graph tab, %.1f s to %.1f s') % (start, end))
		for column, name in enumerate(topics, self.TOPIC_COLUMN):
			metrics = precision(*self._graph.window(name))
			for _group, rows in STATISTICS:
				for key, _name, format, _clause, _description in rows:
					value = metrics.get(key)
					self._items[key].setText(column, '–' if value is None else format(value))


#: The most bins of a histogram.
MAX_BINS = 200


def histogram(v, q=None):
	"""The probability density histogram of the finite values ``v``, the bin edges and the densities of the
	bins. The densities do not depend on the number of the values nor on the width of the bins, the histograms
	of different topics compare.

	The width of the bins is by the Freedman-Diaconis rule, 2 IQR/N^(1/3), robust to outliers (by Scott's rule
	if the interquartile range is 0), at most :data:`MAX_BINS` bins. The bins of values quantized to the
	resolution ``q`` are a multiple of it wide and centred on the levels. Else some bins would get more levels
	than the others and the histogram would look like a comb.
	"""
	n = len(v)
	lo, hi = float(v.min()), float(v.max())
	q25, q75 = np.percentile(v, (25, 75))
	spread = 2 * float(q75 - q25) if q75 > q25 else 3.49 * float(v.std())
	width = max(spread / n ** (1 / 3), (hi - lo) / MAX_BINS)
	if q:
		width = max(1, math.ceil(width / q - 1e-9)) * q
		lo -= q / 2
	elif width <= 0:
		# All the values are the same.
		width = 1.0
		lo -= width / 2
	bins = int((hi - lo) // width) + 1
	edges = lo + width * np.arange(bins + 1)
	counts, _ = np.histogram(v, edges)
	return edges, counts / (n * width)


class MqHistogram(QWidget):
	"""The Histogram tab, the distribution of the values of every graphed topic over the time shown in the Graph
	tab, all in one graph to compare them. The bars are translucent in the colour of the line of the topic, the
	dashed lines mark one and the dotted ones two standard deviations from the mean.

	Relative, the histograms show the deviations from the mean, the topics around different values overlap. The
	mean rather than the median, the standard deviations are around the mean too and they stay symmetric about
	zero.
	"""

	#: How often the histograms are computed while they are shown, in milliseconds.
	REFRESH_MS = 500

	BORDER_COLOR = '#d6d6d6'
	BAR_TEXT_COLOR = '#555'

	#: The opacity of the bars, they overlap.
	FILL_ALPHA = 60

	def __init__(self, graph):
		super().__init__()
		self._graph = graph
		self._topics = {}
		# The bars and the standard deviation lines (-2, -1, +1, +2) of the topics by the name.
		self._bars = {}
		self._sigmas = {}

		bar = QWidget()
		bar.setObjectName('bar')
		bar.setAttribute(Qt.WA_StyledBackground)
		row = QHBoxLayout(bar)
		row.setContentsMargins(8, 4, 8, 4)
		row.setSpacing(4)
		self._relative = QCheckBox(self.tr('Relative'))
		self._relative.setToolTip(self.tr('Show the deviations from the mean, the topics around different values '
		                                  'overlap'))
		self._relative.toggled.connect(self._relativeChanged)
		row.addWidget(self._relative)
		row.addStretch()
		self._window = QLabel()
		self._window.setToolTip(self.tr('The histograms cover the time shown in the Graph tab, set its time span '
		                                'there'))
		row.addWidget(self._window)

		self._plot = _Plot()
		self._plot.setAxisLabel('left', self.tr('Probability density'))
		self._relativeChanged(False)
		self._plot.getPlotItem().getViewBox().enableAutoRange()

		# Shown while there are no topics.
		self._hint = QLabel(self.tr('Select topics in the Topics tab to show their histograms here.'))
		self._hint.setAlignment(Qt.AlignCenter)
		self._hint.setStyleSheet('color: #888;')
		self._stack = QStackedWidget()
		self._stack.addWidget(self._hint)
		self._stack.addWidget(self._plot)

		self.setStyleSheet(f"""
			QWidget#bar {{ border-bottom: 1px solid {self.BORDER_COLOR}; }}
			QWidget#bar QLabel, QWidget#bar QCheckBox {{ color: {self.BAR_TEXT_COLOR}; }}
		""")
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(bar)
		layout.addWidget(self._stack, stretch=1)

		self._timer = QTimer(self)
		self._timer.setInterval(self.REFRESH_MS)
		self._timer.timeout.connect(self._refresh)
		self._timer.start()

	def _setTopics(self, topics):
		"""Histograms of the topics of ``topics``, the colours by the names."""
		for name in self._bars:
			self._plot.removeItem(self._bars[name])
			for line in self._sigmas[name]:
				self._plot.removeItem(line)
		self._topics = topics
		self._bars = {}
		self._sigmas = {}
		for name, color in topics.items():
			fill = QColor(color)
			fill.setAlpha(self.FILL_ALPHA)
			bars = pg.PlotDataItem(name=name, stepMode='center', fillLevel=0, brush=fill,
			                       pen=pg.mkPen(color, width=_Plot.LINE_WIDTH), antialias=True)
			self._plot.addLine(bars)
			self._bars[name] = bars
			lines = []
			for k in (-2, -1, 1, 2):
				style = Qt.DashLine if abs(k) == 1 else Qt.DotLine
				line = pg.InfiniteLine(angle=90, pen=pg.mkPen(color, width=_Plot.LINE_WIDTH, style=style))
				line.setToolTip(f'{name}: {k:+d}σ')
				self._plot.addItem(line)
				lines.append(line)
			self._sigmas[name] = lines
		self._stack.setCurrentWidget(self._plot if topics else self._hint)

	@Slot(bool)
	def _relativeChanged(self, relative):
		self._plot.setAxisLabel('bottom', self.tr('Deviation from the mean') if relative else self.tr('Value'))
		self._refresh()

	def showEvent(self, event):
		super().showEvent(event)
		self._refresh()

	@Slot()
	def _refresh(self):
		if not self.isVisible():
			return
		topics = self._graph.topics
		if topics != self._topics:
			self._setTopics(topics)
		start, end = self._graph.timeRange()
		self._window.setText(self.tr('Over the time shown in the Graph tab, %.1f s to %.1f s') % (start, end))
		relative = self._relative.isChecked()
		for name in topics:
			t, v = self._graph.window(name)
			metrics = precision(t, v)
			v = v[np.isfinite(v)]
			bars = self._bars[name]
			lines = self._sigmas[name]
			if not len(v):
				bars.hide()
				for line in lines:
					line.hide()
				continue
			mean = metrics['mean']
			if relative:
				v = v - mean
				mean = 0.0
			edges, density = histogram(v, metrics.get('q'))
			bars.setData(edges, density)
			bars.show()
			s = metrics.get('s')
			for k, line in zip((-2, -1, 1, 2), lines):
				line.setVisible(bool(s))
				if s:
					line.setValue(mean + k * s)


class MqLiveData(QWidget):
	"""The live data with its toolbars: Pause, Clear and the poll rate on the left and the topic filter on the
	right at the top, the polling status and the message statistics at the bottom. In between, the tabs of the
	topic tree and of the display, the graph, the statistics and the histograms of the selected topics."""

	#: The colour of the status, and of an invalid filter.
	STATUS_COLOR = '#888'
	INVALID_BORDER = '#c0392b'

	def __init__(self, side):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._tree = MqLiveDataTreeWidget()
		self._display = MqDisplayArea(self._tree)
		self._graph = MqGraph()
		self._statistics = MqStatistics(self._graph)
		self._histogram = MqHistogram(self._graph)
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
		self._tabs.addTab(self._statistics, Icons.statistics(), self.tr('Statistics'))
		self._tabs.addTab(self._histogram, Icons.histogram(), self.tr('Histogram'))
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
		valid = is_valid_filter(text.strip())
		self._filter.setStyleSheet('' if valid else f'QLineEdit {{ border: 1px solid {self.INVALID_BORDER}; }}')

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
