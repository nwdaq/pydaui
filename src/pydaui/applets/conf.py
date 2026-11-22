# SPDX-License-Identifier: GPL-3.0-or-later
#
# Configuration tree applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Configuration tree applet: browse the configuration tree of a plumCore device.

It talks to an interface speaking the ``conf`` protocol (plumCore services/proto-conf), the same tree
services expose via ``*_get_conf()``. The client and the tree widget are ported from plumCore
``tools/proto-conf.py`` and ``tools/proto-conf-gui.py``. Subtrees are loaded lazily when expanded,
off the GUI thread, as every request is a round trip to the device.
"""

import logging
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

import cbor2
import json5
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton, blend_colors
from pydaui.ui.icons import Icons
from pydaui.ui.progress_window import ProgressWindow
from pydaui.ui.tree import DetailTreeWidget, RowDelegate


# enum conf_type (see services/interfaces/conf.h)
CONF_NONE = 0
CONF_SUBTREE = 1
CONF_F = 2
CONF_U32 = 3
CONF_S32 = 4
CONF_U16 = 5
CONF_S16 = 6
CONF_U8 = 7
CONF_S8 = 8
CONF_B = 9
CONF_BSTR = 10
CONF_STR = 11
CONF_ENUM = 12

TYPE_NAMES = {
	CONF_NONE: 'none',
	CONF_SUBTREE: 'subtree',
	CONF_F: 'float',
	CONF_U32: 'u32',
	CONF_S32: 's32',
	CONF_U16: 'u16',
	CONF_S16: 's16',
	CONF_U8: 'u8',
	CONF_S8: 's8',
	CONF_B: 'bool',
	CONF_BSTR: 'bstr',
	CONF_STR: 'str',
	CONF_ENUM: 'enum',
}

# enum conf_dir
CONF_DIR_NEXT = 0
CONF_DIR_PREV = 1
CONF_DIR_CHILD = 2
CONF_DIR_UP = 3

# enum conf_flag
CONF_READ = (1 << 0)
CONF_WRITE = (1 << 1)
CONF_CONST = (1 << 2)
CONF_STATUS = (1 << 3)
CONF_DETAIL = (1 << 4)
CONF_CREATE = (1 << 5)

#: The flag names shown in the tree, in the order of the bits.
FLAG_NAMES = (
	(CONF_READ, 'READ'),
	(CONF_WRITE, 'WRITE'),
	(CONF_CONST, 'CONST'),
	(CONF_STATUS, 'STATUS'),
	(CONF_DETAIL, 'DETAIL'),
	(CONF_CREATE, 'CREATE'),
)


def format_flags(flags):
	"""The flags set in ``flags`` as a list of bracketed names, e.g. ``[READ] [WRITE]``."""
	return ' '.join('[%s]' % name for bit, name in FLAG_NAMES if flags & bit)

#: The protocol name of the interfaces this applet talks to.
PROTOCOL = 'conf'


class ConfClient:
	"""conf protocol client over a :class:`~pydaui.connection.PyDauiConnection` connected to a conf interface.

	Requests are serialized, so the client may be used from several threads. Changes to the configuration
	(writes, the load and save jobs) are queued with :meth:`submit` and run one after another in the order
	they were submitted, so a save always follows the writes before it.
	"""

	#: Number of attempts of a request before giving up.
	RETRIES = 10

	#: How long the load and save jobs may take, in seconds.
	JOB_TIMEOUT = 10.0

	def __init__(self, connection):
		self._connection = connection
		# Shared by the clients on the same connection, see PyDauiConnection.sharedSide().
		self._lock = connection.lock
		self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conf-change')
		# The value names of the enum nodes loaded, by their path as a tuple.
		self._enum_names = {}

	def submit(self, fn):
		"""Queue ``fn`` to run off the GUI thread after the changes queued before it."""
		self._queue.submit(fn)

	def close(self):
		"""Drop the queued changes not started yet."""
		self._queue.shutdown(wait=False, cancel_futures=True)

	def call(self, req: dict, timeout=None, retries=None):
		"""Send a request and return the decoded response map, or ``None`` if there is no valid one.

		Without ``timeout`` the retransmit interval suits the transport, e.g. a BLE link is much slower
		than UDP.
		"""
		with self._lock:
			sock = self._connection.socket
			if sock is None:
				return None
			reply = sock.request(cbor2.dumps(req), timeout=timeout or sock.request_timeout,
			                     retries=retries or self.RETRIES)
		if reply is None:
			return None
		try:
			return cbor2.loads(reply)
		except Exception:
			return None

	def _walk(self, path, dir, limit=None):
		req = {'c': 'walk', 'p': path, 'dir': dir}
		if limit is not None:
			req['limit'] = limit
		r = self.call(req)
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err', 'unknown error'))
		return r.get('nodes', [])

	def _read(self, path):
		r = self.call({'c': 'read', 'p': path})
		if r is None or r.get('err'):
			return None
		return r.get('val')

	def _next_siblings(self, path):
		"""Collect all siblings following the node identified by path. The responder
		caps a single walk to MAX_SIBLINGS, so keep walking from the last returned node."""
		result = []
		cur = path
		while True:
			batch = self._walk(cur, CONF_DIR_NEXT)
			if not batch:
				break
			result += batch
			cur = cur[:-1] + [batch[-1]['n']]
		return result

	def _children(self, path):
		"""Return the list of {n,t,f} child nodes of the node identified by path."""
		first = self._walk(path, CONF_DIR_CHILD)
		if not first:
			return []
		return first + self._next_siblings(path + [first[0]['n']])

	def description(self, path):
		"""The (brief, detail) help strings of the node at path, ``None`` for the ones it has not."""
		r = self.call({'c': 'get_desc', 'p': path})
		if r is None or r.get('err'):
			return None, None
		return r.get('brief'), r.get('detail')

	def constraints(self, path):
		"""The constraints of the value of the node at path as the response map, ``None`` if it has none."""
		r = self.call({'c': 'get_constraints', 'p': path})
		if r is None or r.get('err'):
			return None
		return r

	def _load_enum_names(self, path):
		"""Get the value names of the enum node at path, the values are their indices. Without the names
		in the constraints the values are named by their numbers."""
		c = self.constraints(path)
		if c is None:
			return
		names = c.get('values')
		if not isinstance(names, list):
			names = [str(i) for i in range(c.get('count', 0))]
		if names:
			self._enum_names[tuple(path)] = [str(name) for name in names]

	def enum_names(self, path):
		"""The value names of the enum node at path, indexed by the value, ``None`` if they are not known
		(e.g. the node was not loaded with :meth:`children`)."""
		return self._enum_names.get(tuple(path))

	def _fmt_value(self, val, path=None):
		if isinstance(val, bytes):
			return val.hex()
		names = self.enum_names(path) if path is not None and isinstance(val, int) else None
		if names is not None and 0 <= val < len(names):
			return names[val]
		return str(val)

	def children(self, path=None):
		"""Return a single level of children of the node at path as a list of dicts:
		{'name', 'type', 'flags', 'value', 'is_subtree', 'path', 'brief', 'detail'}. Leaf values and the help
		strings of all the nodes are read here;
		subtrees are not descended into (lazy loading is up to the caller)."""
		path = path or []
		nodes = []
		for ch in self._children(path):
			name = ch.get('n', '')
			type = ch.get('t', CONF_NONE)
			child_path = path + [name]

			value = ''
			is_subtree = (type == CONF_SUBTREE)
			if type == CONF_ENUM:
				self._load_enum_names(child_path)
			if not is_subtree and type != CONF_NONE:
				val = self._read(child_path)
				if val is not None:
					value = self._fmt_value(val, child_path)
			brief, detail = self.description(child_path)

			nodes.append({
				'name': name,
				'type': type,
				'flags': ch.get('f', 0),
				'value': value,
				'is_subtree': is_subtree,
				'path': child_path,
				'brief': brief or '',
				'detail': detail or '',
			})
		return nodes

	def read(self, path):
		"""Read the value of the node at path formatted for display, or ``None`` if it cannot be read."""
		val = self._read(path)
		return None if val is None else self._fmt_value(val, path)

	@staticmethod
	def _parse_value(type_int, s, names=None):
		try:
			if type_int == CONF_ENUM and names is not None:
				# A value name, or a value number with a name.
				if s in names:
					return names.index(s)
				val = int(s, 0)
				return val if 0 <= val < len(names) else None
			if type_int == CONF_F:
				return float(s)
			elif type_int in (CONF_U32, CONF_U16, CONF_U8, CONF_ENUM):
				return int(s, 0)
			elif type_int in (CONF_S32, CONF_S16, CONF_S8):
				return int(s, 0)
			elif type_int == CONF_B:
				if s.lower() in ('true', '1', 'yes', 'on'):
					return True
				if s.lower() in ('false', '0', 'no', 'off'):
					return False
				return None
			elif type_int == CONF_STR:
				return s
			elif type_int == CONF_BSTR:
				return bytes.fromhex(s)
		except (ValueError, TypeError):
			return None
		return None

	def parse(self, type_int, s, path=None):
		"""Parse the text ``s`` as a value of the given conf type, ``None`` if it is not valid. An enum
		value of the node at ``path`` may be given by its name."""
		names = self.enum_names(path) if path is not None else None
		return self._parse_value(type_int, s.strip() if type_int != CONF_STR else s, names)

	def write(self, path, type_int, val):
		"""Write the parsed value ``val`` of the given conf type to the node at path. Raises ``ValueError``
		with the reason if the write fails."""
		r = self.call({'c': 'write', 'p': path, 't': type_int, 'val': val})
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err'))

	def run_job(self, cmd):
		"""Run the load or save job on the device. The device replies once the job ends, so the request is
		sent only once with a long timeout, a retransmit would start the job again. Raises ``ValueError``
		with the reason if the job fails."""
		r = self.call({'c': cmd}, timeout=self.JOB_TIMEOUT, retries=1)
		if r is None:
			raise ValueError('no response')
		if r.get('err'):
			raise ValueError(r.get('err'))

	def _to_json(self, val, path):
		"""The value read from the node at path as a JSON value: an enum by its name, a byte string in hex."""
		if isinstance(val, bytes):
			return val.hex()
		names = self.enum_names(path)
		if names is not None and isinstance(val, int) and not isinstance(val, bool) and 0 <= val < len(names):
			return names[val]
		return val

	def _from_json(self, type_int, val, path):
		"""The JSON value ``val`` as a value of the given conf type for the node at path. Raises
		``ValueError`` if it does not fit the type."""
		is_int = isinstance(val, int) and not isinstance(val, bool)
		if type_int == CONF_ENUM:
			names = self.enum_names(path) or []
			if isinstance(val, str) and val in names:
				return names.index(val)
			if is_int and 0 <= val < len(names):
				return val
			raise ValueError('not one of %s' % ', '.join(names) if names else 'not a valid enum value')
		if type_int == CONF_F and (is_int or isinstance(val, float)):
			return float(val)
		if type_int in (CONF_U32, CONF_S32, CONF_U16, CONF_S16, CONF_U8, CONF_S8) and is_int:
			return val
		if type_int == CONF_B and isinstance(val, bool):
			return val
		if type_int == CONF_STR and isinstance(val, str):
			return val
		if type_int == CONF_BSTR and isinstance(val, str):
			try:
				return bytes.fromhex(val)
			except ValueError:
				raise ValueError('not a hex byte string')
		raise ValueError('%s expected, got %s' % (TYPE_NAMES.get(type_int, str(type_int)), json5.dumps(val)))

	def export_tree(self, progress, aborted, path=None):
		"""Read the whole tree below path recursively, keeping the values saved on the device (see
		:func:`is_saved`).

		Returns a list of entries ``{'name', 'brief', 'detail'}`` with a ``value`` (leaves) or the
		``children`` entries (subtrees with something to export). ``progress(path, False)`` is called before
		each node is read and ``progress(path, True)`` once its value is exported, ``aborted()`` is checked between the nodes and stops the export with :class:`Aborted`.
		"""
		path = path or []
		entries = []
		for ch in self._children(path):
			if aborted():
				raise Aborted()
			name = ch.get('n', '')
			type = ch.get('t', CONF_NONE)
			child_path = path + [name]
			progress(child_path, False)
			entry = {'name': name}
			if type == CONF_SUBTREE:
				children = self.export_tree(progress, aborted, child_path)
				if not children:
					continue
				entry['children'] = children
			elif is_saved(type, ch.get('f', 0)):
				if type == CONF_ENUM:
					self._load_enum_names(child_path)
				val = self._read(child_path)
				if val is None:
					raise ValueError('reading %s failed' % '/'.join(child_path))
				entry['value'] = self._to_json(val, child_path)
			else:
				continue
			entry['brief'], entry['detail'] = self.description(child_path)
			entries.append(entry)
			if 'value' in entry:
				progress(child_path, True)
		return entries

	def import_tree(self, data, progress, aborted, path=None, failures=None):
		"""Write the values of the nested dict ``data`` to the nodes of the tree below path, matched by
		their names. Values which cannot be written or are not saved on the device (see :func:`is_saved`)
		are skipped.

		Returns the list of ``(path, reason)`` of the values skipped. ``progress(written)`` is called after
		each value with whether it was written, ``aborted()`` is checked between them and stops the import with :class:`Aborted`.
		"""
		path = path or []
		failures = [] if failures is None else failures
		try:
			nodes = {ch.get('n', ''): ch for ch in self._children(path)}
		except ValueError as e:
			# None of the values below can be written.
			for leaf_path in leaf_paths(data, path):
				failures.append((leaf_path, 'reading the tree failed: %s' % e))
				progress(False)
			return failures
		for name, val in data.items():
			if aborted():
				raise Aborted()
			child_path = path + [name]
			node = nodes.get(name)
			type = node.get('t', CONF_NONE) if node is not None else None
			if isinstance(val, dict):
				if type == CONF_SUBTREE:
					self.import_tree(val, progress, aborted, child_path, failures)
				else:
					reason = 'no such subtree' if node is None else 'not a subtree'
					for leaf_path in leaf_paths(val, child_path):
						failures.append((leaf_path, reason))
						progress(False)
				continue
			try:
				if node is None:
					raise ValueError('no such item')
				if type == CONF_SUBTREE:
					raise ValueError('a subtree, not a value')
				if not is_editable(type, node.get('f', 0)):
					raise ValueError('not writable')
				if not is_saved(type, node.get('f', 0)):
					raise ValueError('a status value, not a part of the configuration')
				if type == CONF_ENUM and self.enum_names(child_path) is None:
					self._load_enum_names(child_path)
				self.write(child_path, type, self._from_json(type, val, child_path))
			except ValueError as e:
				failures.append((child_path, str(e)))
				progress(False)
				continue
			progress(True)
		return failures


class Aborted(Exception):
	"""The export or import was aborted."""


def leaf_paths(data, path=None):
	"""The paths of the values (not the nested dicts) in the nested dict ``data``, below path."""
	path = path or []
	paths = []
	for name, val in data.items():
		if isinstance(val, dict):
			paths += leaf_paths(val, path + [name])
		else:
			paths.append(path + [name])
	return paths


#: The width the help strings in the comments of the exported file are wrapped to, the indentation left out.
JSON5_COMMENT_WIDTH = 100


def json5_comment(brief, detail, indent):
	"""The lines of a ``/* */`` comment block in the style of plumCore C code: the brief help string, an empty
	line and the detailed one, each line starting with `` * ``. No lines without any help string."""
	paragraphs = [brief] if brief else []
	if detail and detail != brief:
		paragraphs += [''] + detail.split('\n')
	if not paragraphs:
		return []
	lines = [indent + '/*']
	for paragraph in paragraphs:
		# A comment in the help string would end the block.
		paragraph = paragraph.replace('*/', '* /')
		wrapped = textwrap.wrap(paragraph, JSON5_COMMENT_WIDTH) or ['']
		lines += [(indent + ' * ' + line).rstrip() for line in wrapped]
	lines.append(indent + ' */')
	return lines


def format_json5(entries, depth=0):
	"""The entries of :meth:`ConfClient.export_tree` as a JSON5 document, each item preceded by a comment
	with its help strings."""
	indent = '\t' * (depth + 1)
	lines = ['{'] if depth == 0 else []
	previous_comment = False
	for i, entry in enumerate(entries):
		comment = json5_comment(entry.get('brief'), entry.get('detail'), indent)
		if i and (comment or previous_comment):
			# A blank line above and below each item with a comment, the items without any are kept together.
			lines.append('')
		previous_comment = bool(comment)
		lines += comment
		comma = ',' if i < len(entries) - 1 else ''
		key = json5.dumps(entry['name'])
		if 'children' in entry:
			lines.append('%s%s: {' % (indent, key))
			lines += format_json5(entry['children'], depth + 1)
			lines.append('%s}%s' % (indent, comma))
		else:
			lines.append('%s%s: %s%s' % (indent, key, json5.dumps(entry['value']), comma))
	if depth == 0:
		lines.append('}')
		return '\n'.join(lines) + '\n'
	return lines


def read_values(connection, interface, paths):
	"""The values of the conf nodes at ``paths`` (lists of names) on the conf ``interface`` of the
	``connection``, formatted for display, ``None`` for a node which cannot be read. It asks the device over
	the shared side connection to the interface, run it off the GUI thread."""
	client = ConfClient(connection.sharedSide(interface))
	return [client.read(path) for path in paths]


#: The conf types which can be edited as text.
EDITABLE_TYPES = (CONF_F, CONF_U32, CONF_S32, CONF_U16, CONF_S16, CONF_U8, CONF_S8, CONF_B, CONF_BSTR, CONF_STR,
                  CONF_ENUM)


def is_editable(type_int, flags):
	"""Whether a node of the given type and flags holds a value the user can change."""
	return bool(type_int in EDITABLE_TYPES and flags & CONF_WRITE and not flags & CONF_CONST)


def is_saved(type_int, flags):
	"""Whether a node of the given type and flags is a part of the configuration saved on the device, like
	``conf_cbor_saveable()`` in plumCore services/conf-cbor decides. Status values (e.g. the RTC time) can
	be written, but are not saved."""
	return bool(is_editable(type_int, flags) and flags & CONF_READ and not flags & CONF_STATUS)


class _ValueWriter(QObject):
	"""Writes one value off the GUI thread and reads it back, as the device may adjust what it stores.

	``done`` carries the value read back (``None`` if it cannot be read), ``failed`` the reason and the
	value read back. The tree item is carried through untouched.
	"""

	done = Signal(object, object)          # (tree item, value read back)
	failed = Signal(object, str, object)   # (tree item, error message, value read back)

	def __init__(self, conf, path, type_int, val, item):
		super().__init__()
		self._conf = conf
		self._path = path
		self._type = type_int
		self._val = val
		self._item = item

	def start(self):
		self._conf.submit(self._run)

	def _run(self):
		try:
			self._conf.write(self._path, self._type, self._val)
		except Exception as e:
			self.failed.emit(self._item, str(e), self._conf.read(self._path))
			return
		self.done.emit(self._item, self._conf.read(self._path))


class _JobRunner(QObject):
	"""Runs the load or save job (:meth:`ConfClient.run_job`) after the changes queued before it."""

	done = Signal()
	failed = Signal(str)

	def __init__(self, conf, cmd):
		super().__init__()
		self._conf = conf
		self._cmd = cmd

	def start(self):
		self._conf.submit(self._run)

	def _run(self):
		try:
			self._conf.run_job(self._cmd)
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.done.emit()


class _TreeTask(QObject):
	"""Runs the export or import ``fn(progress, aborted)`` after the changes queued before it, see
	:meth:`ConfClient.export_tree` and :meth:`ConfClient.import_tree`. ``fn`` calls ``progress`` with the
	arguments to emit :attr:`progress` with."""

	progress = Signal(object)
	done = Signal(object)        # the result of fn
	failed = Signal(str)
	aborted = Signal()

	def __init__(self, conf, fn):
		super().__init__()
		self._conf = conf
		self._fn = fn
		self._abort = threading.Event()

	def start(self):
		self._conf.submit(self._run)

	def abort(self):
		self._abort.set()

	def _run(self):
		try:
			result = self._fn(lambda *args: self.progress.emit(args), self._abort.is_set)
		except Aborted:
			self.aborted.emit()
			return
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.done.emit(result)


class TextEditor(QLineEdit):
	"""The line edit of a value in the tree, the editor of the types without their own (see
	:func:`create_editor`). It reports when its text should be written: after :attr:`IDLE_MS` without typing
	(``idle``), on Enter (``enter``, also moving to the next row) or when it loses the focus (``focusLost``,
	with whether the editor should close).
	"""

	#: Typing pause after which the value is written, in milliseconds.
	IDLE_MS = 2000

	idle = Signal()
	commit = Signal()
	enter = Signal()           # write the value and move to the next row
	cancel = Signal()
	focusLost = Signal(bool)   # close the editor
	navigate = Signal(int)     # -1 previous row, +1 next row

	# It covers the whole cell; the left padding (with the border) matches the cell text inset, so the
	# value does not move when the editor opens.
	STYLE = """
		QLineEdit {
			background-color: %s;
			border: 1px solid %s;
			border-radius: 0;
			padding: 0 7px;
		}
	"""
	BORDER = '#7aa7e3'
	INVALID_BORDER = '#c0392b'
	#: How much of :attr:`INVALID_BORDER` the edit field background of a value which is not valid is tinted with.
	INVALID_TINT = 0.25

	def __init__(self, text):
		super().__init__(text)
		self._timer = QTimer(self)
		self._timer.setSingleShot(True)
		self._timer.setInterval(self.IDLE_MS)
		self._timer.timeout.connect(self.idle)
		self.textEdited.connect(lambda _text: self._timer.start())
		self.setValid(True)

	def setValid(self, valid):
		"""Show whether the text is a valid value, with a red border and the edit field background of the
		palette tinted red if not."""
		if valid:
			self.setStyleSheet(self.STYLE % ('palette(base)', self.BORDER))
			return
		base, red, tint = QApplication.palette().color(QPalette.Base), QColor(self.INVALID_BORDER), self.INVALID_TINT
		background = QColor.fromRgbF(*(a * (1 - tint) + b * tint for a, b in
		                               zip(base.getRgbF()[:3], red.getRgbF()[:3])))
		self.setStyleSheet(self.STYLE % (background.name(), self.INVALID_BORDER))

	def stopIdleTimer(self):
		self._timer.stop()

	def begin(self):
		"""Take the keyboard focus to edit the value."""
		self.setFocus(Qt.OtherFocusReason)
		self.selectAll()

	def keyPressEvent(self, event):
		key = event.key()
		if key in (Qt.Key_Return, Qt.Key_Enter):
			self._timer.stop()
			self.enter.emit()
		elif key == Qt.Key_Escape:
			self._timer.stop()
			self.cancel.emit()
		elif key in (Qt.Key_Up, Qt.Key_Down):
			self.navigate.emit(-1 if key == Qt.Key_Up else 1)
		else:
			super().keyPressEvent(event)

	def focusOutEvent(self, event):
		super().focusOutEvent(event)
		self._timer.stop()
		# Switching to another window or opening a popup keeps the editor, the value is written anyway.
		self.focusLost.emit(event.reason() not in (Qt.ActiveWindowFocusReason, Qt.PopupFocusReason))


def parse_bool(text):
	"""The bool value ``text`` shows, ``None`` if it shows none."""
	return ConfClient._parse_value(CONF_B, text.strip())


class BoolEditor(QFrame):
	"""The check box of a bool value in the tree, labelled with the value like the cell, a change is written
	right away (``commit``). It has the signals and methods of :class:`TextEditor`, its text is the value as
	the device formats it."""

	idle = Signal()
	commit = Signal()
	enter = Signal()
	cancel = Signal()
	focusLost = Signal(bool)
	navigate = Signal(int)
	textChanged = Signal(str)

	# It covers the whole cell like the line edit, the check box is where the cell draws its indicator.
	STYLE = """
		BoolEditor {
			background-color: palette(base);
			border: 1px solid %s;
		}
	""" % TextEditor.BORDER
	CHECK_LEFT = 3

	def __init__(self, text):
		super().__init__()
		self.setStyleSheet(self.STYLE)
		self._check = QCheckBox()
		self._check.setChecked(bool(parse_bool(text)))
		self._check.setText(self.text())
		self._check.toggled.connect(self._toggled)
		self._check.installEventFilter(self)
		self.setFocusProxy(self._check)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(self.CHECK_LEFT, 0, 0, 0)
		layout.addWidget(self._check)

	def text(self):
		return str(self._check.isChecked())

	def setText(self, text):
		value = parse_bool(text)
		if value is not None:
			# Not written back.
			self._check.blockSignals(True)
			self._check.setChecked(value)
			self._check.blockSignals(False)
			self._check.setText(self.text())

	def setValid(self, valid):
		# Either value is valid.
		pass

	def stopIdleTimer(self):
		pass

	def begin(self):
		self._check.setFocus(Qt.OtherFocusReason)

	@Slot(bool)
	def _toggled(self, checked):
		self._check.setText(self.text())
		self.textChanged.emit(self.text())
		self.commit.emit()

	def eventFilter(self, obj, event):
		if event.type() == QEvent.KeyPress:
			key = event.key()
			if key == Qt.Key_Escape:
				self.cancel.emit()
				return True
			if key in (Qt.Key_Return, Qt.Key_Enter):
				self.enter.emit()
				return True
			if key in (Qt.Key_Up, Qt.Key_Down):
				self.navigate.emit(-1 if key == Qt.Key_Up else 1)
				return True
		elif event.type() == QEvent.FocusOut:
			self.focusLost.emit(event.reason() not in (Qt.ActiveWindowFocusReason, Qt.PopupFocusReason))
		return False


class EnumEditor(QComboBox):
	"""The drop-down list of the value names of an enum value in the tree, a choice is written right away
	(``commit``). It has the signals and methods of :class:`TextEditor`, its text is the name of the value."""

	idle = Signal()
	commit = Signal()
	enter = Signal()
	cancel = Signal()
	focusLost = Signal(bool)
	navigate = Signal(int)
	textChanged = Signal(str)

	def __init__(self, text, names):
		super().__init__()
		self.addItems(names)
		self.setText(text)
		self.activated.connect(self._activated)

	def text(self):
		return self.currentText()

	def setText(self, text):
		# A value without a name selects none, its empty text is not a valid value to write back.
		self.blockSignals(True)
		self.setCurrentIndex(self.findText(text))
		self.blockSignals(False)

	def setValid(self, valid):
		# Only the valid values can be chosen.
		pass

	def stopIdleTimer(self):
		pass

	def begin(self):
		self.setFocus(Qt.OtherFocusReason)

	@Slot(int)
	def _activated(self, index):
		self.textChanged.emit(self.text())
		self.commit.emit()

	def keyPressEvent(self, event):
		key = event.key()
		if key == Qt.Key_Escape:
			self.cancel.emit()
		elif key in (Qt.Key_Return, Qt.Key_Enter):
			# The open list takes Enter itself, choosing the value.
			self.enter.emit()
		elif key in (Qt.Key_Up, Qt.Key_Down) and not event.modifiers() & Qt.AltModifier:
			# Alt+Down opens the list, as Space does.
			self.navigate.emit(-1 if key == Qt.Key_Up else 1)
		else:
			super().keyPressEvent(event)

	def wheelEvent(self, event):
		# Scrolling the tree over the editor does not change the value.
		event.ignore()

	def focusOutEvent(self, event):
		super().focusOutEvent(event)
		# The list takes the focus when opened, not as a popup focus change.
		popup = self.view().isVisible()
		self.focusLost.emit(not popup and event.reason() not in (Qt.ActiveWindowFocusReason, Qt.PopupFocusReason))


#: The editors of the conf types with their own, the others are edited as text.
EDITORS = {
	CONF_B: BoolEditor,
}


def create_editor(type_int, text, names=None):
	"""The editor of a value of the conf type ``type_int`` showing ``text``, ``names`` are the value names of
	an enum, without them it is edited as text. Every editor has the signals and methods of :class:`TextEditor`
	the tree uses: ``idle``, ``commit``, ``cancel``, ``focusLost``, ``navigate``, ``textChanged``, ``text()``,
	``setText()``, ``setValid()``, ``stopIdleTimer()`` and ``begin()``."""
	if type_int == CONF_ENUM:
		return EnumEditor(text, names) if names else TextEditor(text)
	return EDITORS.get(type_int, TextEditor)(text)


class _ValueDelegate(RowDelegate):
	"""Delegate of the Value column: places the value editor over the whole cell, hides the text of the
	cell being edited, which would show through around the editor, and shows the bool values as check
	boxes."""

	def updateEditorGeometry(self, editor, option, index):
		# The default places the editor in the text area, which the item padding squeezes. The editor
		# covers the whole cell instead, without the detail line below it.
		rect = QRect(option.rect)
		rect.setHeight(rect.height() - self._tree.detailHeight(index))
		editor.setGeometry(rect)

	def initStyleOption(self, option, index):
		super().initStyleOption(option, index)
		if self._tree.isEditing(index):
			option.text = ''
			return
		if index.siblingAtColumn(0).data(ConfTreeWidget.TYPE_ROLE) == CONF_B:
			value = parse_bool(option.text)
			if value is not None:
				option.features |= QStyleOptionViewItem.HasCheckIndicator
				option.checkState = Qt.Checked if value else Qt.Unchecked


class _ChildrenLoader(QObject):
	"""Loads one level of the tree (:meth:`ConfClient.children`) off the GUI thread.

	The ``done``/``failed`` signals are delivered on the GUI thread, carrying the tree item the children
	belong to through untouched.
	"""

	done = Signal(object, list)    # (tree item, list of node dicts)
	failed = Signal(object, str)   # (tree item, error message)

	def __init__(self, conf, path, item):
		super().__init__()
		self._conf = conf
		self._path = path
		self._item = item

	def start(self):
		threading.Thread(target=self._run, name='conf-load', daemon=True).start()

	def _run(self):
		try:
			nodes = self._conf.children(self._path)
		except Exception as e:
			self.failed.emit(self._item, str(e))
			return
		self.done.emit(self._item, nodes)


class ConfTreeWidget(DetailTreeWidget):

	#: The number of nodes loaded so far changed, collapsed subtrees are counted once loaded.
	loadedCountChanged = Signal(int)

	#: A value was written to the device.
	valueWritten = Signal()

	PATH_ROLE = Qt.UserRole

	#: Set on the placeholder child of an item whose children are being loaded.
	LOADING_ROLE = Qt.UserRole + 1

	#: Set on the placeholder child of an item whose children failed to load.
	FAILED_ROLE = Qt.UserRole + 2

	#: The conf type and flags of a node.
	TYPE_ROLE = Qt.UserRole + 3
	FLAGS_ROLE = Qt.UserRole + 4

	#: The full description of a node, empty if it has none.
	DETAIL_ROLE = Qt.UserRole + 5

	#: The column of the flags, of the values and their editor, and of the brief descriptions.
	FLAGS_COLUMN = 2
	VALUE_COLUMN = 3
	BRIEF_COLUMN = 4

	#: Colour of the flags.
	FLAGS_COLOR = '#999'

	#: Colour of a value whose last write failed.
	WRITE_FAILED_COLOR = '#c0392b'

	#: Interval of the spinner animation frames shown while loading, in milliseconds.
	SPINNER_INTERVAL_MS = 100

	#: Relative widths of the Name, Type, Flags, Value and Brief description columns, which always fill the
	#: whole width.
	COLUMN_RATIOS = (4, 2, 3, 4, 6)

	def __init__(self, conf: ConfClient):
		super().__init__()
		self._conf = conf
		# Keep loader references alive until their signal has been delivered.
		self._loaders = set()
		# Keep writer references alive until their signal has been delivered.
		self._writers = set()
		# The item whose value is being edited, its editor and the value text the edit started from (or
		# was last written).
		self._edit_item = None
		self._editor = None
		self._edit_base = None
		# The lowercase filter text, empty when not filtering.
		self._filter = ''
		# Set by expandAll(): subtrees loaded meanwhile are expanded too, until collapseAll() or refresh().
		self._expand_all = False
		# The subtree entered on Enter whose children are being loaded, see _advance().
		self._enter_pending = None
		# The number of nodes loaded, subtrees included.
		self._loaded_count = 0
		# The placeholders of the subtrees being loaded by each loader, showing an animated spinner.
		self._spinning = {}
		self._spinner_frames = [Icons.spinner(i) for i in range(Icons.SPINNER_FRAMES)]
		self._spinner_frame = 0
		self._spinner_timer = QTimer(self)
		self._spinner_timer.setInterval(self.SPINNER_INTERVAL_MS)
		self._spinner_timer.timeout.connect(self._spin)

		self.setColumnCount(5)
		self.setHeaderLabels(['Name', 'Type', 'Flags', 'Value', 'Brief description'])
		self.itemExpanded.connect(self._on_expanded)
		# The value of the current row is edited in place; a click on the current row reopens a closed editor.
		self.setEditTriggers(QAbstractItemView.NoEditTriggers)
		self._row_delegate = RowDelegate(self)
		self.setItemDelegate(self._row_delegate)
		self._value_delegate = _ValueDelegate(self)
		self.setItemDelegateForColumn(self.VALUE_COLUMN, self._value_delegate)
		self.currentItemChanged.connect(lambda current, previous: self._openEditor(current))
		self.itemClicked.connect(self._itemClicked)

		self.refresh()

	def refresh(self):
		"""Reload the tree from the root, collapsing everything."""
		self._closeEditor(write=True)
		self._expand_all = False
		self._enter_pending = None
		# The placeholders are deleted with the items.
		self._spinning.clear()
		self.clear()
		self._loaded_count = 0
		self.loadedCountChanged.emit(0)
		root = self.invisibleRootItem()
		self._load(root, [], self._placeholder(root))

	@staticmethod
	def _placeholder(parent):
		return QTreeWidgetItem(parent, ['...'])

	def _load(self, item, path, placeholder):
		placeholder.setText(0, 'loading...')
		placeholder.setIcon(0, self._spinner_frames[self._spinner_frame])
		placeholder.setData(0, self.LOADING_ROLE, True)
		loader = _ChildrenLoader(self._conf, path, item)
		loader.done.connect(self._on_loaded)
		loader.failed.connect(self._on_failed)
		self._loaders.add(loader)
		self._spinning[loader] = placeholder
		self._spinner_timer.start()
		loader.start()

	def _spin(self):
		"""Show the next spinner frame on the placeholders being loaded."""
		self._spinner_frame = (self._spinner_frame + 1) % len(self._spinner_frames)
		for placeholder in self._spinning.values():
			placeholder.setIcon(0, self._spinner_frames[self._spinner_frame])

	def _loaded(self, loader):
		"""Forget a loader that finished, stopping the spinner once there is nothing loading."""
		self._loaders.discard(loader)
		self._spinning.pop(loader, None)
		if not self._spinning:
			self._spinner_timer.stop()

	def _add_nodes(self, parent, nodes):
		for node in nodes:
			item = QTreeWidgetItem(parent, [
				node['name'],
				# A subtree is apparent from its expand/collapse indicator, it does not need a type.
				'' if node['is_subtree'] else TYPE_NAMES.get(node['type'], str(node['type'])),
				'' if node['is_subtree'] else format_flags(node['flags']),
				node['value'],
				node['brief'],
			])
			item.setForeground(self.FLAGS_COLUMN, QColor(self.FLAGS_COLOR))
			item.setForeground(self.BRIEF_COLUMN, self.descriptionColor())
			# The full description, the brief one is cut to the column width.
			item.setToolTip(self.BRIEF_COLUMN, node['detail'] or node['brief'])
			item.setData(0, self.PATH_ROLE, node['path'])
			item.setData(0, self.TYPE_ROLE, node['type'])
			item.setData(0, self.FLAGS_ROLE, node['flags'])
			item.setData(0, self.DETAIL_ROLE, node['detail'])
			self.styleName(item)
			if node['is_subtree']:
				# Add a placeholder child so the expand arrow appears; the real
				# children are loaded lazily when the item is first expanded.
				self._placeholder(item)

	def _on_expanded(self, item):
		# A single child without a stored path is the placeholder, not loaded yet or failed to load.
		if item.childCount() == 1 and item.child(0).data(0, self.PATH_ROLE) is None:
			placeholder = item.child(0)
			if not placeholder.data(0, self.LOADING_ROLE):
				self._load(item, item.data(0, self.PATH_ROLE), placeholder)

	@Slot(object, list)
	def _on_loaded(self, item, nodes):
		self._loaded(self.sender())
		try:
			item.takeChildren()
			self._add_nodes(item, nodes)
		except RuntimeError:
			# The item was removed (e.g. the tree was refreshed) while loading.
			return
		self._loaded_count += len(nodes)
		self.loadedCountChanged.emit(self._loaded_count)
		if item is self._enter_pending:
			self._enter_pending = None
			if item is self.currentItem() and item.childCount():
				self._navigate(1)
		if self._expand_all:
			self._expand_subtrees(item)
		if self._filter:
			self._apply_filter()

	@Slot(object, str)
	def _on_failed(self, item, message):
		self._loaded(self.sender())
		try:
			# Leave a placeholder in place, collapsing and expanding the item again retries.
			item.takeChildren()
			placeholder = self._placeholder(item)
			placeholder.setText(0, 'loading failed: %s' % message)
			# Not loaded again by the filter, only when expanded.
			placeholder.setData(0, self.FAILED_ROLE, True)
		except RuntimeError:
			pass

	def expandAll(self):
		"""Expand every subtree. The subtrees not loaded yet are expanded as their children arrive."""
		self._expand_all = True
		self._expand_subtrees(self.invisibleRootItem())

	def collapseAll(self):
		self._expand_all = False
		super().collapseAll()

	def _expand_subtrees(self, parent):
		for i in range(parent.childCount()):
			item = parent.child(i)
			# Only subtrees have children, a placeholder one if not loaded yet.
			if item.childCount():
				item.setExpanded(True)
				self._expand_subtrees(item)

	def mousePressEvent(self, event):
		# A subtree row clicked anywhere, its indicator included, is expanded or collapsed here, not by the tree.
		item = self.itemAt(event.position().toPoint())
		if event.button() != Qt.LeftButton or item is None or item.data(0, self.TYPE_ROLE) != CONF_SUBTREE:
			super().mousePressEvent(event)
			return
		self.setCurrentItem(item)
		item.setExpanded(not item.isExpanded())

	def keyPressEvent(self, event):
		# Enter on a row without an editor, see _advance().
		if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.currentItem() is not None:
			self._advance()
			return
		super().keyPressEvent(event)

	def _advance(self):
		"""Go on from the current row on Enter: enter a subtree at its first item, expanding it if collapsed,
		otherwise move to the next row. An editable value gets its editor there."""
		item = self.currentItem()
		if item.data(0, self.TYPE_ROLE) == CONF_SUBTREE:
			item.setExpanded(True)
			if item.childCount() and item.child(0).data(0, self.PATH_ROLE) is None:
				# The children are still loading, the first one becomes current once they arrive, unless
				# another row is current by then.
				self._enter_pending = item
				return
		self._navigate(1)

	def mouseDoubleClickEvent(self, event):
		# Double clicks do nothing, their second click is pressed as another click.
		pass

	def isEditing(self, index):
		"""Whether the value at ``index`` is being edited."""
		return (self._edit_item is not None and index.column() == self.VALUE_COLUMN and
		        index.siblingAtColumn(0) == self.indexFromItem(self._edit_item, 0))

	def _isEditable(self, item):
		return (item is not None and item.data(0, self.PATH_ROLE) is not None and
		        is_editable(item.data(0, self.TYPE_ROLE), item.data(0, self.FLAGS_ROLE) or 0))

	@Slot(QTreeWidgetItem, int)
	def _itemClicked(self, item, column):
		if item is self.currentItem() and self._edit_item is None:
			self._openEditor(item)

	def _openEditor(self, item):
		"""Show an editor of the value in the Value cell of ``item``, if the value can be changed."""
		if item is self._edit_item:
			return
		self._closeEditor(write=True)
		if not self._isEditable(item):
			return

		text = item.text(self.VALUE_COLUMN)
		editor = create_editor(item.data(0, self.TYPE_ROLE), text, self._conf.enum_names(item.data(0, self.PATH_ROLE)))
		editor.setFont(item.font(self.VALUE_COLUMN))
		editor.idle.connect(lambda: self._writeEdited(close=False))
		editor.commit.connect(lambda: self._writeEdited(close=False))
		editor.enter.connect(self._enterEdited)
		editor.cancel.connect(self._cancelEdit)
		editor.focusLost.connect(lambda close: self._writeEdited(close=close))
		editor.navigate.connect(self._navigate)
		editor.textChanged.connect(self._validateEdited)

		# The editor covers the whole cell, see _ValueDelegate.updateEditorGeometry().
		self._edit_item, self._editor, self._edit_base = item, editor, text
		self.setItemWidget(item, self.VALUE_COLUMN, editor)
		editor.begin()

	def _closeEditor(self, write):
		"""Remove the editor, writing its value first if ``write`` and it changed."""
		if self._edit_item is None:
			return
		if write:
			self._writeEdited(close=False)
		item, editor = self._edit_item, self._editor
		self._edit_item = self._editor = self._edit_base = None
		editor.stopIdleTimer()
		# Removing the editor takes its focus away; its signals must not act on the next editor.
		editor.blockSignals(True)
		try:
			# The widget is deleted later, it is safe to remove it from within its own event handlers.
			# Hide it right away, it would stay in place until deleted.
			widget = self.itemWidget(item, self.VALUE_COLUMN)
			if widget is not None:
				widget.hide()
			self.removeItemWidget(item, self.VALUE_COLUMN)
			# Repaint the cell text hidden while editing.
			self.viewport().update(self.visualItemRect(item))
		except RuntimeError:
			# The item is gone already (e.g. the tree was cleared).
			pass

	def finishEditing(self, write):
		"""Close the editor, writing its value first if ``write`` and it changed."""
		self._closeEditor(write=write)

	@Slot()
	def _cancelEdit(self):
		"""Close the editor without writing, the keyboard focus returns to the tree."""
		self._closeEditor(write=False)
		self.setFocus()

	@Slot(int)
	def _navigate(self, step):
		current = self.currentItem()
		target = self.itemAbove(current) if step < 0 else self.itemBelow(current)
		if target is not None:
			self.setCurrentItem(target)
		# Keep the keyboard focus in the tree when there is no editor on the new row.
		if self._editor is None:
			self.setFocus()

	@Slot()
	def _enterEdited(self):
		"""Write the value edited and move on to the next row, unless the text is not a valid value."""
		item, editor = self._edit_item, self._editor
		if item is None:
			return
		text = editor.text()
		if text != self._edit_base and self._conf.parse(item.data(0, self.TYPE_ROLE), text,
		                                                item.data(0, self.PATH_ROLE)) is None:
			return
		self._writeEdited(close=False)
		self._advance()

	@Slot(str)
	def _validateEdited(self, text):
		if self._editor is not None:
			item = self._edit_item
			self._editor.setValid(self._conf.parse(item.data(0, self.TYPE_ROLE), text, item.data(0, self.PATH_ROLE))
			                      is not None)

	def _writeEdited(self, close):
		"""Write the text of the editor if it changed since the edit started or was last written, then
		close the editor if ``close``. Text which is not a valid value is not written; closing the editor
		discards it."""
		item, editor = self._edit_item, self._editor
		if item is None:
			return
		text = editor.text()
		if text != self._edit_base:
			type_int = item.data(0, self.TYPE_ROLE)
			val = self._conf.parse(type_int, text, item.data(0, self.PATH_ROLE))
			if val is None:
				if close:
					logging.getLogger(self.__class__.__name__).warning(
						f'not writing {"/".join(item.data(0, self.PATH_ROLE))}: {text!r} is not a valid '
						f'{TYPE_NAMES.get(type_int, type_int)}')
			else:
				self._edit_base = text
				item.setText(self.VALUE_COLUMN, text)
				writer = _ValueWriter(self._conf, item.data(0, self.PATH_ROLE), type_int, val, item)
				writer.done.connect(self._on_written)
				writer.failed.connect(self._on_write_failed)
				self._writers.add(writer)
				writer.start()
		if close:
			self._closeEditor(write=False)

	def _showWritten(self, item, value):
		"""Show the value read back after a write, in the cell and in the editor if it was not edited since."""
		if value is None:
			return
		if item is self._edit_item and self._editor.text() == self._edit_base:
			self._editor.setText(value)
			self._edit_base = value
		item.setText(self.VALUE_COLUMN, value)

	@Slot(object, object)
	def _on_written(self, item, value):
		self._writers.discard(self.sender())
		try:
			item.setData(self.VALUE_COLUMN, Qt.ForegroundRole, None)
			item.setToolTip(self.VALUE_COLUMN, '')
			self._showWritten(item, value)
		except RuntimeError:
			pass
		self.valueWritten.emit()

	@Slot(object, str, object)
	def _on_write_failed(self, item, message, value):
		self._writers.discard(self.sender())
		try:
			path = '/'.join(item.data(0, self.PATH_ROLE))
			logging.getLogger(self.__class__.__name__).warning(f'writing {path} failed: {message}')
			item.setForeground(self.VALUE_COLUMN, QColor(self.WRITE_FAILED_COLOR))
			item.setToolTip(self.VALUE_COLUMN, f'writing the value failed: {message}')
			self._showWritten(item, value)
		except RuntimeError:
			pass

	def setFilter(self, text):
		"""Show only the items with a path component containing ``text`` (ignoring case), together with
		their subtrees and ancestors. Empty text shows all items.

		The tree is loaded lazily, so while filtering the subtrees not loaded yet are loaded in the
		background to be searched too, and the filter is applied again as they arrive.
		"""
		self._filter = text.strip().lower()
		self._apply_filter()

	def _apply_filter(self):
		root = self.invisibleRootItem()
		for i in range(root.childCount()):
			self._filter_item(root, root.child(i), False)

	def _filter_item(self, parent, item, ancestor_matches):
		"""Show or hide ``item`` and its subtree. Returns whether the item or any of its descendants
		matches the filter by itself."""
		path = item.data(0, self.PATH_ROLE)
		if path is None:
			# The placeholder of children not loaded yet, shown with its parent. Load them to search them.
			item.setHidden(False)
			if self._filter and not item.data(0, self.LOADING_ROLE) and not item.data(0, self.FAILED_ROLE):
				self._load(parent, parent.data(0, self.PATH_ROLE) or [], item)
			return False

		matches = bool(self._filter) and any(self._filter in component.lower() for component in path)
		descendant_matches = False
		for i in range(item.childCount()):
			if self._filter_item(item, item.child(i), ancestor_matches or matches):
				descendant_matches = True

		item.setHidden(bool(self._filter) and not (ancestor_matches or matches or descendant_matches))
		if self._filter and descendant_matches:
			# Reveal the matching descendants.
			item.setExpanded(True)
		return matches or descendant_matches


class ConfTree(QWidget):
	"""The configuration tree with its toolbar: Reload and Save on the left, Expand all, Collapse all and
	the filter on the right."""

	def __init__(self, conf: ConfClient):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._conf = conf
		self._tree = ConfTreeWidget(conf)
		# Keep the job runner alive until its signal has been delivered.
		self._job = None
		# The export or import running and its progress window.
		self._task = None
		self._progress = None

		self._tb = QToolBar()
		self._reload = DauiButton(self.tr('Reload'), Icons.refresh())
		self._reload.setToolTip(self.tr('Load the saved configuration on the device and show it'))
		self._reload.clicked.connect(lambda: self._runJob('load'))
		self._tb.addWidget(self._reload)
		self._save = DauiButton(self.tr('Save'), Icons.save())
		self._save.setToolTip(self.tr('Save the configuration on the device'))
		self._save.clicked.connect(lambda: self._runJob('save'))
		self._tb.addWidget(self._save)
		self._tb.addSeparator()
		self._import = DauiButton(self.tr('Import'), Icons.importFile())
		self._import.setToolTip(self.tr('Write the configuration values from a JSON5 file to the device'))
		self._import.clicked.connect(self._importFile)
		self._tb.addWidget(self._import)
		self._export = DauiButton(self.tr('Export'), Icons.exportFile())
		self._export.setToolTip(self.tr('Save the configuration values of the device to a JSON5 file'))
		self._export.clicked.connect(self._exportFile)
		self._tb.addWidget(self._export)
		self._tb.addSeparator()

		self._details = QCheckBox(self.tr('Show details'))
		self._details.setToolTip(self.tr('Show the full description of each configuration item below it'))
		# Spaced from the separator like the filter label.
		self._details.setStyleSheet('QCheckBox { margin-left: 6px; }')
		self._details.toggled.connect(self._tree.setShowDetails)
		self._tb.addWidget(self._details)

		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)

		expand = DauiButton(self.tr('Expand all'), Icons.expandAll())
		expand.setToolTip(self.tr('Expand all configuration subtrees, loading them from the device'))
		expand.clicked.connect(self._tree.expandAll)
		self._tb.addWidget(expand)
		collapse = DauiButton(self.tr('Collapse all'), Icons.collapseAll())
		collapse.setToolTip(self.tr('Collapse all configuration subtrees'))
		collapse.clicked.connect(self._tree.collapseAll)
		self._tb.addWidget(collapse)

		label = QLabel(self.tr('Filter:'))
		label.setContentsMargins(6, 0, 4, 0)
		self._tb.addWidget(label)
		self._filter = QLineEdit()
		self._filter.setPlaceholderText(self.tr('configuration item name'))
		self._filter.setFixedWidth(220)
		# As tall as the buttons next to it.
		self._filter.setFixedHeight(self._reload.sizeHint().height())
		self._filter.textChanged.connect(self._tree.setFilter)
		self._tb.addWidget(self._filter)
		cancel = DauiButton(btn_icon=Icons.cancel())
		cancel.setToolTip(self.tr('Cancel the filter'))
		cancel.clicked.connect(self._filter.clear)
		self._tb.addWidget(cancel)

		self._layout = QVBoxLayout()
		self._layout.setContentsMargins(0, 0, 0, 0)
		# Spaced below the toolbar only, the status bar is right below the tree, its padding around the text.
		spacing = self._layout.spacing()
		self._layout.setSpacing(0)
		self._layout.addWidget(self._tb)
		self._layout.addSpacing(spacing)
		self._layout.addWidget(self._tree)

		self._status = QStatusBar()
		# The window is resized by its own frame.
		self._status.setSizeGripEnabled(False)
		self._loaded = QLabel()
		self._status.addWidget(self._loaded)
		self._unsaved = QLabel(self.tr('Changed values are not saved on the device yet'))
		color = QColor(ConfTreeWidget.WRITE_FAILED_COLOR)
		if self.palette().color(QPalette.Window).lightness() < 128:
			# Lightened towards the text colour, the dark red is hard to read in a dark theme.
			color = blend_colors(color, self.palette().color(QPalette.WindowText), self.UNSAVED_DARK_LIGHTEN)
		self._unsaved.setStyleSheet('color: %s;' % color.name())
		self._unsaved.setToolTip(self.tr('Save the configuration to keep the changes after the device restarts'))
		self._unsaved.hide()
		self._status.addPermanentWidget(self._unsaved)
		# A bit higher than the text, centered in it.
		for label in (self._loaded, self._unsaved):
			label.setContentsMargins(4, 4, 4, 4)
			label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
		self._layout.addWidget(self._status)
		self.setLayout(self._layout)

		self._tree.loadedCountChanged.connect(self._showLoadedCount)
		self._tree.valueWritten.connect(lambda: self._setUnsaved(True))
		self._showLoadedCount(0)

	#: The blue mixed into the background of the Save button while there are changes not saved, and how
	#: much of it. Mixed with the button colour of the palette, the button text stays readable in a dark
	#: theme too.
	UNSAVED_SAVE_TINT = Icons.BLUE
	UNSAVED_SAVE_TINT_RATIO = 0.6

	#: How much the colour of the note about the changes not saved is mixed with the text colour in a dark
	#: theme.
	UNSAVED_DARK_LIGHTEN = 0.4

	def _setUnsaved(self, unsaved):
		"""Show the note about the changes not saved and tint the Save button while there are some."""
		self._unsaved.setVisible(unsaved)
		# An empty palette sets no colour, the button inherits them again.
		palette = QPalette()
		if unsaved:
			palette.setColor(QPalette.Button, blend_colors(self.palette().color(QPalette.Button),
				self.UNSAVED_SAVE_TINT, self.UNSAVED_SAVE_TINT_RATIO))
		self._save.setPalette(palette)

	def _showLoadedCount(self, count):
		if count == 1:
			self._loaded.setText(self.tr('1 node loaded'))
		else:
			self._loaded.setText(self.tr('%d nodes loaded') % count)

	_JOB_TEXT = {
		'load': ('Reload', 'Loading...', 'Loading the saved configuration failed'),
		'save': ('Save', 'Saving...', 'Saving the configuration failed'),
	}

	def _runJob(self, cmd):
		"""Run the load or save job on the device, the buttons are disabled meanwhile."""
		if self._job is not None or self._task is not None:
			return
		# A value being edited is written before saving; loading replaces it with the saved one.
		self._tree.finishEditing(write=(cmd == 'save'))

		button = self._reload if cmd == 'load' else self._save
		button.setText(self.tr(self._JOB_TEXT[cmd][1]))
		self._setBusy(True)

		self._job = _JobRunner(self._conf, cmd)
		self._job.done.connect(lambda: self._jobFinished(cmd, None))
		self._job.failed.connect(lambda message: self._jobFinished(cmd, message))
		self._job.start()

	def _jobFinished(self, cmd, error):
		self._job = None
		self._reload.setText(self.tr(self._JOB_TEXT['load'][0]))
		self._save.setText(self.tr(self._JOB_TEXT['save'][0]))
		self._setBusy(False)

		if error is not None:
			self._logger.warning(f'{cmd} job failed: {error}')
			QMessageBox.warning(self, self.tr('Configuration'), f'{self.tr(self._JOB_TEXT[cmd][2])}: {error}')
			return
		self._logger.info(f'{cmd} job finished')
		# Saved, or replaced by the saved configuration.
		self._setUnsaved(False)
		if cmd == 'load':
			# Show the configuration just loaded.
			self._tree.refresh()

	def _setBusy(self, busy):
		"""Disable the buttons starting a job, an export or an import while one runs."""
		for button in (self._reload, self._save, self._import, self._export):
			button.setEnabled(not busy)

	#: The file types of the exported configuration.
	FILE_FILTER = 'JSON5 files (*.json5 *.json);;All files (*)'

	def _exportFile(self):
		"""Read the whole configuration tree and save its values with their help strings to a JSON5 file."""
		if self._job is not None or self._task is not None:
			return
		path, _filter = QFileDialog.getSaveFileName(self, self.tr('Export configuration'), 'configuration.json5',
		                                            self.tr(self.FILE_FILTER))
		if not path:
			return
		# The value being edited is exported too.
		self._tree.finishEditing(write=True)
		self._logger.info(f'exporting the configuration to {path}')
		exported = 0

		def progress(node_path, value_exported):
			nonlocal exported
			exported += value_exported
			self._progress.setText(self.tr('Reading the configuration items...\n%s\n\n%d values exported') % (
				'/'.join(node_path), exported))

		def done(entries):
			try:
				with open(path, 'w', encoding='utf-8') as f:
					f.write(format_json5(entries))
			except OSError as e:
				failed(str(e))
				return
			self._logger.info(f'configuration exported to {path}')

		def failed(message):
			self._logger.warning(f'exporting the configuration failed: {message}')
			QMessageBox.warning(self, self.tr('Export configuration'),
			                    self.tr('Exporting the configuration failed: %s') % message)

		self._runTask(self.tr('Export configuration'), self.tr('Reading the configuration items...'), 0,
		              lambda report, aborted: self._conf.export_tree(report, aborted),
		              lambda args: progress(*args), done, failed)

	def _importFile(self):
		"""Write the values of a JSON5 file to the configuration tree, skipping the ones which cannot be
		written, and warn about them at the end."""
		if self._job is not None or self._task is not None:
			return
		path, _filter = QFileDialog.getOpenFileName(self, self.tr('Import configuration'), '',
		                                            self.tr(self.FILE_FILTER))
		if not path:
			return
		try:
			with open(path, encoding='utf-8') as f:
				data = json5.load(f)
			if not isinstance(data, dict):
				raise ValueError('the file does not contain an object of the configuration items')
		except Exception as e:
			QMessageBox.warning(self, self.tr('Import configuration'),
			                    self.tr('Reading %s failed: %s') % (path, e))
			return
		# The values imported replace the one being edited.
		self._tree.finishEditing(write=False)
		self._logger.info(f'importing the configuration from {path}')
		total = len(leaf_paths(data))
		count = written = 0

		def progress(args):
			nonlocal count, written
			count += 1
			written += bool(args[0])
			self._progress.setProgress(count)

		def failed(message):
			self._logger.warning(f'importing the configuration failed: {message}')
			QMessageBox.warning(self, self.tr('Import configuration'),
			                    self.tr('Importing the configuration failed: %s') % message)
			self._importFinished(written, total, None)

		self._runTask(self.tr('Import configuration'), self.tr('Writing the configuration values...'), total,
		              lambda report, aborted: self._conf.import_tree(data, report, aborted),
		              progress, lambda failures: self._importFinished(written, total, failures), failed,
		              aborted=lambda: self._importFinished(written, total, None))

	#: The most values skipped listed in the warning after an import.
	IMPORT_FAILURES_SHOWN = 15

	def _importFinished(self, written, total, failures):
		"""Show the ``written`` values imported, ``failures`` lists the values skipped, ``None`` if the import
		did not finish."""
		if written:
			# Show the values written, they are not saved on the device yet.
			self._tree.refresh()
			self._setUnsaved(True)
		if failures is None:
			return
		self._logger.info(f'configuration imported, {written} of {total} values written')
		if not failures:
			return
		for path, reason in failures:
			self._logger.warning(f'importing {"/".join(path)} failed: {reason}')
		lines = [f'{"/".join(path)}: {reason}' for path, reason in failures[:self.IMPORT_FAILURES_SHOWN]]
		if len(failures) > self.IMPORT_FAILURES_SHOWN:
			lines.append(self.tr('... and %d more') % (len(failures) - self.IMPORT_FAILURES_SHOWN))
		QMessageBox.warning(self, self.tr('Import configuration'),
		                    self.tr('Not all values were imported, %d of %d were skipped:\n\n%s') % (
		                    len(failures), total, '\n'.join(lines)))

	def _runTask(self, title, text, total, fn, progress, done, failed, aborted=None):
		"""Run the export or import ``fn`` (see :class:`_TreeTask`) showing its progress, ``total`` steps or
		a busy indicator if 0. ``progress``, ``done`` and ``failed`` are called with the arguments of the
		signals of the task, ``aborted`` without any when the progress window aborts it."""
		self._setBusy(True)
		self._progress = ProgressWindow(title, text)
		self._progress.setProgress(0, total)
		self._progress.aborted.connect(self._abortTask)
		self._progress.showOver(self)

		def ended(callback, *args):
			self._task = None
			self._progress.finish()
			self._progress = None
			self._setBusy(False)
			if callback is not None:
				callback(*args)

		self._task = _TreeTask(self._conf, fn)
		self._task.progress.connect(lambda args: progress(args) if self._progress is not None else None)
		self._task.done.connect(lambda result: ended(done, result))
		self._task.failed.connect(lambda message: ended(failed, message))
		self._task.aborted.connect(lambda: ended(aborted))
		self._task.start()

	@Slot()
	def _abortTask(self):
		if self._task is not None:
			self._task.abort()
			self._progress.setText(self.tr('Aborting...'))
			self._progress.setAbortEnabled(False)

	def abort(self):
		"""Abort the export or import running, if any, and close its progress window, e.g. when the window
		closes. Nothing is reported of it any more."""
		if self._task is not None:
			self._task.abort()
			for signal in (self._task.progress, self._task.done, self._task.failed, self._task.aborted):
				signal.disconnect()
			self._task = None
			self._progress.finish()
			self._progress = None


class ConfTreeWindow(AppletWindow):
	"""The configuration tree served by an interface speaking the conf protocol on the connected device.

	It talks over the side connection to the interface shared with the other users of the interface, the
	conf service serves a single session. The window closes when the device is disconnected.
	"""

	def __init__(self, connection, name):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)

		self._logger.info(f'opening the configuration tree on interface {name!r}')
		self._side = connection.sharedSide(name)
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(f'Configuration tree ({name})')
		self._conf = ConfClient(self._side)
		self._tree = ConfTree(self._conf)
		self.setWidget(self._tree)
		self.resize(950, 500)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._tree.abort()
			self._conf.close()
		super().closeEvent(event)
