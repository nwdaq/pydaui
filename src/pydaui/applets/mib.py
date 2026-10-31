# SPDX-License-Identifier: GPL-3.0-or-later
#
# MIB editor applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""MIB editor applet: edit the manufacturer information block (MIB, plumCore EP-1cf) in the ``mib`` flash
partition of a plumCore device, laid out by a schema kept in a local file.

The MIB is read by the plumCore flash-cbor-mib service and built by plumCore ``tools/make-mib.py``. Its
root is an indefinite-length CBOR map (``0xbf`` ... ``0xff``) whose mandatory first entry maps the magic
key ``CBORMIB`` to the structure version. Every further entry maps a node name (a text string) to a nested
map (a subtree) or to a scalar, whose configuration type the device infers from its CBOR encoding. With a
top-level ``check-blake2s`` marker entry, a 32 B blake2s digest of the whole map follows the break.

The schema is a JSON file (``*.mibschema.json``) describing the nodes of the MIB in their order, with the
names of the types and constraints of the conf protocol::

    {"mibschema": 1, "nodes": [
        {"key": "sn", "type": "str", "brief": "Serial number", "max_len": 16, "regexp": "^[A-Za-z0-9.-]+$"},
        {"key": "hw", "type": "subtree", "children": [
            {"key": "rev", "type": "u32", "min": 1, "max": 99, "default": 1}]}]}

A node has a ``key`` and a ``type`` (one of :data:`TYPES`), optionally a ``brief`` and a ``detail``
description, the range of its value (``min``/``max`` of a number, ``min_len``/``max_len`` of a string,
``regexp`` of a text string) and a ``default`` value, a byte string in hex. A subtree has ``children``.
"""

import hashlib
import io
import json
import logging
import math
import re
import struct

import cbor2
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from pynbus2.flash import FlashClient, FlashError
from pynbus2.services import flash as flash_service

from pydaui.applets import conf
from pydaui.applets.flash import _Operation, format_size
from pydaui.files import display_name, read_file, write_file
from pydaui.ui.helpers import AppletWindow, DauiButton, WrappedLabel
from pydaui.ui.icons import Icons
from pydaui.ui.progress_window import ByteFormat, ProgressWindow
from pydaui.ui.settings_window import form_layout, polish_forms
from pydaui.ui.tree import DetailTreeWidget, RowDelegate

#: The protocol name of the interfaces this applet talks to.
PROTOCOL = flash_service.PROTOCOL

#: The name of the flash partition holding the MIB.
MIB_PARTITION = 'mib'

#: The magic first key of the MIB map, its value is the structure version.
MIB_MAGIC = 'CBORMIB'
MIB_VERSION = 1

#: The top-level marker key selecting the blake2s content check, the digest follows the map.
CHECK_BLAKE2S = 'check-blake2s'
DIGEST_SIZE = 32

#: The top-level keys of the MIB which are not nodes.
RESERVED_KEYS = (MIB_MAGIC, CHECK_BLAKE2S)

#: The longest node name the device takes, in UTF-8 bytes (FLASH_CBOR_MIB_MAX_NAME_LEN with the NUL).
MAX_KEY_LEN = 63

#: The version of the schema files written.
SCHEMA_VERSION = 1
SCHEMA_FILTER = 'MIB schema (*.mibschema.json);;JSON files (*.json);;All files (*)'

#: The node types, the ones besides the subtree as the device infers them from the CBOR encoding.
TYPES = ('subtree', 'u32', 's32', 'float', 'bool', 'str', 'bstr')
NUMBER_TYPES = ('u32', 's32', 'float')
INT_RANGES = {'u32': (0, 2 ** 32 - 1), 's32': (-2 ** 31, 2 ** 31 - 1)}
#: The largest magnitude of a 32 bit float, the device keeps the floats in.
FLOAT_MAX = 3.4028234663852886e38

#: The conf types of the node types, choosing their value editors.
CONF_TYPES = {
	'u32': conf.CONF_U32,
	's32': conf.CONF_S32,
	'float': conf.CONF_F,
	'bool': conf.CONF_B,
	'str': conf.CONF_STR,
	'bstr': conf.CONF_BSTR,
}

#: The fields of a node describing it besides its key and type, in the order they are written.
SCHEMA_FIELDS = ('brief', 'detail', 'min', 'max', 'min_len', 'max_len', 'regexp', 'default')

#: The range fields each type takes.
RANGE_FIELDS = {
	'u32': ('min', 'max'),
	's32': ('min', 'max'),
	'float': ('min', 'max'),
	'str': ('min_len', 'max_len', 'regexp'),
	'bstr': ('min_len', 'max_len'),
}


def format_value(type, value):
	"""The ``value`` of a node of the ``type`` as text, empty if it has none."""
	if value is None:
		return ''
	if type == 'bstr':
		return value.hex()
	return str(value)


def parse_value(type, text):
	"""The value of the ``type`` the ``text`` shows. Raises ``ValueError`` if it is not one."""
	value = conf.ConfClient._parse_value(CONF_TYPES[type], text if type == 'str' else text.strip())
	if value is None:
		raise ValueError(f'{text!r} is not a valid {type}')
	return value


def check_value(node, value):
	"""Why ``value`` is not a valid value of the ``node``, ``None`` if it is."""
	type = node['type']
	if type in INT_RANGES:
		if not isinstance(value, int) or isinstance(value, bool):
			return f'not a {type}'
		low, high = INT_RANGES[type]
		if not low <= value <= high:
			return f'out of the {type} range'
	elif type == 'float':
		if not isinstance(value, (int, float)) or isinstance(value, bool):
			return 'not a number'
		if math.isfinite(value) and abs(value) > FLOAT_MAX:
			return 'too large for a 32 bit float'
	elif type == 'bool':
		if not isinstance(value, bool):
			return 'not a bool'
	elif type == 'str':
		if not isinstance(value, str):
			return 'not a text string'
	elif type == 'bstr':
		if not isinstance(value, bytes):
			return 'not a byte string'
	if type in NUMBER_TYPES:
		if node.get('min') is not None and value < node['min']:
			return f'less than the minimum {node["min"]}'
		if node.get('max') is not None and value > node['max']:
			return f'more than the maximum {node["max"]}'
	if type in ('str', 'bstr'):
		unit = 'characters' if type == 'str' else 'bytes'
		if node.get('min_len') is not None and len(value) < node['min_len']:
			return f'shorter than {node["min_len"]} {unit}'
		if node.get('max_len') is not None and len(value) > node['max_len']:
			return f'longer than {node["max_len"]} {unit}'
	if type == 'str' and node.get('regexp'):
		if re.fullmatch(node['regexp'], value) is None:
			return f'does not match {node["regexp"]}'
	return None


def convert_value(type, value):
	"""The decoded CBOR ``value`` as a value of the ``type``, ``None`` if it is not one. An integer is a
	float too."""
	if type in INT_RANGES:
		return value if isinstance(value, int) and not isinstance(value, bool) else None
	if type == 'float':
		return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
	expected = {'bool': bool, 'str': str, 'bstr': bytes}[type]
	return value if isinstance(value, expected) else None


def infer_type(value):
	"""The node type of the decoded CBOR ``value`` as the device infers it, ``None`` if it has none."""
	if isinstance(value, dict):
		return 'subtree'
	if isinstance(value, bool):
		return 'bool'
	if isinstance(value, int):
		return 'u32' if value >= 0 else 's32'
	if isinstance(value, float):
		return 'float'
	if isinstance(value, str):
		return 'str'
	if isinstance(value, bytes):
		return 'bstr'
	return None


def key_error(key, taken):
	"""Why ``key`` cannot name a node among the siblings with the ``taken`` keys, ``None`` if it can."""
	if not key:
		return 'the key is empty'
	if len(key.encode()) > MAX_KEY_LEN:
		return f'the key is longer than {MAX_KEY_LEN} bytes'
	if key in taken:
		return f'a sibling or a reserved key is named {key}'
	return None


def _head(major, length):
	"""The CBOR head of the ``major`` type with the ``length`` argument."""
	if length < 24:
		return bytes([major << 5 | length])
	for info, fmt in ((24, '>B'), (25, '>H'), (26, '>I'), (27, '>Q')):
		if length < 1 << (8 * struct.calcsize(fmt)):
			return bytes([major << 5 | info]) + struct.pack(fmt, length)
	raise ValueError('too long')


def encode_entries(entries):
	"""The CBOR of the ``(key, type, value)`` ``entries`` concatenated, a subtree's value is the list of its
	entries, encoded as a definite-length map. The floats are 32 bit, the device takes no half floats."""
	data = b''
	for key, type, value in entries:
		data += cbor2.dumps(key)
		if type == 'subtree':
			data += _head(5, len(value)) + encode_entries(value)
		elif type == 'float':
			data += b'\xfa' + struct.pack('>f', value)
		else:
			data += cbor2.dumps(value)
	return data


def make_image(entries, check):
	"""The MIB image of the ``entries`` (see :func:`encode_entries`), with the blake2s content check if
	``check``."""
	image = b'\xbf' + cbor2.dumps(MIB_MAGIC) + cbor2.dumps(MIB_VERSION) + encode_entries(entries)
	if check:
		image += cbor2.dumps(CHECK_BLAKE2S) + cbor2.dumps(True)
	image += b'\xff'
	if check:
		image += hashlib.blake2s(image, digest_size=DIGEST_SIZE).digest()
	return image


def parse_image(data):
	"""The content of the MIB image at the start of ``data`` as ``(content, version, checked)``: the nodes as
	a map without the reserved keys, the structure version and whether its blake2s digest matches, ``None``
	if it has none. Raises ``ValueError`` if ``data`` holds no MIB."""
	if not data or data[0] != 0xbf:
		raise ValueError('the partition holds no MIB' + (', it is erased' if data[:1] == b'\xff' else ''))
	fp = io.BytesIO(data)
	try:
		root = cbor2.CBORDecoder(fp).decode()
	except Exception as e:
		raise ValueError(f'the MIB is not valid CBOR: {e}')
	length = fp.tell()
	if not isinstance(root, dict) or not root:
		raise ValueError('the MIB is not a CBOR map')
	magic, version = next(iter(root.items()))
	if magic != MIB_MAGIC or not isinstance(version, int) or isinstance(version, bool) or version < 1:
		raise ValueError(f'the MIB does not start with the {MIB_MAGIC} magic and version')
	checked = None
	if CHECK_BLAKE2S in root:
		digest = data[length:length + DIGEST_SIZE]
		checked = digest == hashlib.blake2s(data[:length], digest_size=DIGEST_SIZE).digest()

	def check_keys(mapping):
		for key, value in mapping.items():
			if not isinstance(key, str):
				raise ValueError(f'the MIB has a key which is not a text string: {key!r}')
			if isinstance(value, dict):
				check_keys(value)

	check_keys(root)
	return {key: value for key, value in root.items() if key not in RESERVED_KEYS}, version, checked


def schema_node(data, path, taken):
	"""The node of the schema ``data`` (a node map of the JSON file) at ``path`` with its children as a
	``(node, children)`` tuple, ``taken`` the keys of its siblings before it. Raises ``ValueError`` if it is
	not valid."""
	if not isinstance(data, dict):
		raise ValueError(f'{path}: a node is not a map')
	key, type = data.get('key'), data.get('type')
	if not isinstance(key, str):
		raise ValueError(f'{path}: the node has no key')
	path = f'{path}/{key}' if path else key
	error = key_error(key, taken)
	if error is not None:
		raise ValueError(f'{path}: {error}')
	if type not in TYPES:
		raise ValueError(f'{path}: unknown type {type!r}')
	node = {'key': key, 'type': type}
	for field in ('brief', 'detail', 'regexp'):
		if data.get(field) is not None:
			if not isinstance(data[field], str):
				raise ValueError(f'{path}: {field} is not a text string')
			node[field] = data[field]
	for field in ('min', 'max'):
		if data.get(field) is not None:
			node[field] = data[field]
			if type in NUMBER_TYPES and check_value({'type': type}, data[field]) is not None:
				raise ValueError(f'{path}: {field} is not a valid {type}')
	for field in ('min_len', 'max_len'):
		if data.get(field) is not None:
			if not isinstance(data[field], int) or isinstance(data[field], bool) or data[field] < 0:
				raise ValueError(f'{path}: {field} is not a length')
			node[field] = data[field]
	if node.get('regexp'):
		try:
			re.compile(node['regexp'])
		except re.error as e:
			raise ValueError(f'{path}: invalid regexp: {e}')
	if data.get('default') is not None and type != 'subtree':
		default = data['default']
		if type == 'bstr':
			try:
				default = bytes.fromhex(default)
			except (TypeError, ValueError):
				raise ValueError(f'{path}: the default is not a byte string in hex')
		error = check_value(node, default)
		if error is not None:
			raise ValueError(f'{path}: the default is {error}')
		node['default'] = default
	children = []
	if type == 'subtree':
		keys = set()
		for child in data.get('children') or []:
			children.append(schema_node(child, path, keys))
			keys.add(children[-1][0]['key'])
	return node, children


def parse_schema(data):
	"""The nodes of the schema JSON ``data`` as a list of ``(node, children)`` tuples (see
	:func:`schema_node`). Raises ``ValueError`` if it is not a valid schema."""
	try:
		schema = json.loads(data)
	except ValueError as e:
		raise ValueError(f'not a JSON file: {e}')
	if not isinstance(schema, dict) or not isinstance(schema.get('nodes'), list):
		raise ValueError('not a MIB schema, it has no nodes')
	if schema.get('mibschema') != SCHEMA_VERSION:
		raise ValueError(f'unknown MIB schema version {schema.get("mibschema")!r}')
	nodes = []
	keys = set(RESERVED_KEYS)
	for data in schema['nodes']:
		nodes.append(schema_node(data, '', keys))
		keys.add(nodes[-1][0]['key'])
	return nodes


def probe(connection, interface):
	"""The name of the MIB partition on the flash ``interface`` of the device, ``None`` if it has none. It
	asks the device, run it off the GUI thread."""
	try:
		connection.flashClient(interface).info(MIB_PARTITION)
	except FlashError:
		return None
	return MIB_PARTITION


class _NodeItem(QTreeWidgetItem):
	"""A node of the MIB in the tree: its schema ``node`` (a map of the key, the type and the
	:data:`SCHEMA_FIELDS` it has), its ``value`` (``None`` if it has none) and whether it is ``extra``, read
	from the MIB but not in the schema."""

	def __init__(self, node, value=None, extra=False):
		super().__init__()
		self.node = node
		self.value = value
		self.extra = extra

	@property
	def key(self):
		return self.node['key']

	@property
	def type(self):
		return self.node['type']

	@property
	def isSubtree(self):
		return self.node['type'] == 'subtree'

	def children(self):
		return [self.child(index) for index in range(self.childCount())]

	def problem(self):
		"""Why the value is not valid, ``None`` if it is or there is none."""
		if self.isSubtree or self.value is None:
			return None
		return check_value(self.node, self.value)


class _ValueDelegate(RowDelegate):
	"""Delegate of the Value column: places the value editor over the whole cell, hides the text of the
	cell being edited, which would show through around the editor, and shows the bool values as check
	boxes."""

	def updateEditorGeometry(self, editor, option, index):
		# The whole cell, without the detail line below it.
		rect = QRect(option.rect)
		rect.setHeight(rect.height() - self._tree.detailHeight(index))
		editor.setGeometry(rect)

	def initStyleOption(self, option, index):
		super().initStyleOption(option, index)
		if self._tree.isEditing(index):
			option.text = ''
			return
		item = self._tree.itemFromIndex(index)
		if isinstance(item, _NodeItem) and item.type == 'bool' and isinstance(item.value, bool):
			option.features |= QStyleOptionViewItem.HasCheckIndicator
			option.checkState = Qt.Checked if item.value else Qt.Unchecked


class MibTreeWidget(DetailTreeWidget):
	"""The nodes of the MIB with their values, edited in place like in the configuration tree: the value of
	the current row has an editor. The detailed descriptions may be shown below the nodes. ``edited`` tells a
	value was changed."""

	edited = Signal()

	KEY_COLUMN = 0
	TYPE_COLUMN = 1
	VALUE_COLUMN = 2
	BRIEF_COLUMN = 3

	TYPE_COLOR = '#999'
	#: Colour of a value which is not valid.
	INVALID_COLOR = '#c0392b'
	#: Colour of the key of a node not in the schema.
	EXTRA_COLOR = '#b9770e'

	COLUMN_RATIOS = (4, 2, 4, 6)

	def __init__(self):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		# The item whose value is being edited, its editor and the value text the edit started from (or
		# was last stored).
		self._edit_item = None
		self._editor = None
		self._edit_base = None

		self.setColumnCount(4)
		self.setHeaderLabels([self.tr('Key'), self.tr('Type'), self.tr('Value'), self.tr('Brief description')])
		self.setSelectionMode(QAbstractItemView.SingleSelection)
		self.setEditTriggers(QAbstractItemView.NoEditTriggers)
		self._row_delegate = RowDelegate(self)
		self.setItemDelegate(self._row_delegate)
		self._value_delegate = _ValueDelegate(self)
		self.setItemDelegateForColumn(self.VALUE_COLUMN, self._value_delegate)
		self.currentItemChanged.connect(lambda current, previous: self._openEditor(current))
		self.itemClicked.connect(self._itemClicked)

	def showItem(self, item):
		"""Show the node, the value and the problems of ``item`` in its cells."""
		item.setText(self.KEY_COLUMN, item.key)
		# A subtree is apparent from its expand/collapse indicator, it does not need a type.
		item.setText(self.TYPE_COLUMN, '' if item.isSubtree else item.type)
		item.setText(self.VALUE_COLUMN, '' if item.isSubtree else format_value(item.type, item.value))
		item.setText(self.BRIEF_COLUMN, item.node.get('brief', ''))
		self.styleName(item)
		item.setForeground(self.TYPE_COLUMN, QColor(self.TYPE_COLOR))
		item.setForeground(self.BRIEF_COLUMN, self.descriptionColor())
		item.setData(self.KEY_COLUMN, self.DETAIL_ROLE, item.node.get('detail', ''))
		detail = item.node.get('detail') or item.node.get('brief', '')
		item.setToolTip(self.BRIEF_COLUMN, detail)
		if item.extra:
			item.setForeground(self.KEY_COLUMN, QColor(self.EXTRA_COLOR))
			item.setToolTip(self.KEY_COLUMN, self.tr('Read from the MIB, not in the schema'))
		else:
			item.setToolTip(self.KEY_COLUMN, detail)
		problem = item.problem()
		if problem is not None:
			item.setForeground(self.VALUE_COLUMN, QColor(self.INVALID_COLOR))
			item.setToolTip(self.VALUE_COLUMN, self.tr('Not a valid value: %s') % problem)
		else:
			item.setData(self.VALUE_COLUMN, Qt.ForegroundRole, None)
			item.setToolTip(self.VALUE_COLUMN, '')

	def topItems(self):
		return [self.topLevelItem(index) for index in range(self.topLevelItemCount())]

	def siblings(self, parent):
		"""The children of ``parent``, the top level items if it is ``None``."""
		return parent.children() if parent is not None else self.topItems()

	def takenKeys(self, parent, exclude=None):
		"""The keys a child of ``parent`` cannot have, the ones of its children but ``exclude``."""
		keys = {item.key for item in self.siblings(parent) if item is not exclude}
		return keys | set(RESERVED_KEYS) if parent is None else keys

	def allItems(self):
		iterator = QTreeWidgetItemIterator(self)
		items = []
		while iterator.value() is not None:
			items.append(iterator.value())
			iterator += 1
		return items

	def take(self, item):
		"""Take ``item`` out of the tree, returning its parent and its index among its siblings."""
		self.finishEditing(write=True)
		parent = item.parent()
		self.blockSignals(True)
		if parent is None:
			index = self.indexOfTopLevelItem(item)
			self.takeTopLevelItem(index)
		else:
			index = parent.indexOfChild(item)
			parent.takeChild(index)
		self.blockSignals(False)
		return parent, index

	def insert(self, parent, index, item, select=True):
		"""Put ``item`` among the children of ``parent`` at ``index``, selected if ``select``."""
		if parent is None:
			self.insertTopLevelItem(index, item)
		else:
			parent.insertChild(index, item)
			parent.setExpanded(True)
		if select:
			self.setCurrentItem(item)

	def build(self, parent, nodes):
		"""Add the items of the ``(node, children)`` ``nodes`` (see :func:`parse_schema`) to ``parent``, with
		their default values."""
		for node, children in nodes:
			item = _NodeItem(node, node.get('default'))
			self.insert(parent, len(self.siblings(parent)), item, select=False)
			self.showItem(item)
			self.build(item, children)

	def setSchema(self, nodes):
		"""Show the ``(node, children)`` ``nodes`` (see :func:`parse_schema`) with their default values."""
		self.finishEditing(write=False)
		self.clear()
		self.build(None, nodes)
		self.expandAll()

	def schema(self, parent=None):
		"""The nodes under ``parent`` as the JSON data of a schema file."""
		nodes = []
		for item in self.siblings(parent):
			node = {'key': item.key, 'type': item.type}
			for field in SCHEMA_FIELDS:
				if item.node.get(field) is not None:
					value = item.node[field]
					node[field] = value.hex() if isinstance(value, bytes) else value
			if item.isSubtree:
				node['children'] = self.schema(item)
			nodes.append(node)
		return nodes

	def clearExtra(self):
		"""Take the nodes not in the schema in, e.g. once the schema is saved with them."""
		for item in self.allItems():
			if item.extra:
				item.extra = False
				self.showItem(item)

	def entries(self, parent=None):
		"""The nodes under ``parent`` as the ``(key, type, value)`` entries of the MIB (see
		:func:`encode_entries`), without the ones without a value."""
		entries = []
		for item in self.siblings(parent):
			if item.isSubtree:
				entries.append((item.key, item.type, self.entries(item)))
			elif item.value is not None:
				entries.append((item.key, item.type, item.value))
		return entries

	def content(self, parent=None):
		"""The values under ``parent`` as a map, like :func:`parse_image` gives."""
		content = {}
		for item in self.siblings(parent):
			if item.isSubtree:
				content[item.key] = self.content(item)
			elif item.value is not None:
				content[item.key] = item.value
		return content

	def setContent(self, content):
		"""Show the values of the MIB ``content`` (see :func:`parse_image`) in the nodes of the schema, the
		nodes not in the schema are added. The nodes added by the content shown before are removed. Returns
		the problems found as a list of texts."""
		self.finishEditing(write=False)
		for item in self.allItems():
			if item.extra and (item.parent() is None or not item.parent().extra):
				self.take(item)
		for item in self.allItems():
			item.value = None
		problems = []
		self._setContent(None, content, '', problems)
		for item in self.allItems():
			self.showItem(item)
		return problems

	def _setContent(self, parent, content, path, problems):
		items = {item.key: item for item in self.siblings(parent)}
		for key, value in content.items():
			key_path = f'{path}/{key}' if path else key
			item = items.get(key)
			if item is None:
				self._addExtra(parent, key, value, key_path, problems)
			elif item.isSubtree:
				if isinstance(value, dict):
					self._setContent(item, value, key_path, problems)
				else:
					problems.append(self.tr('%s: a value instead of a subtree, left out') % key_path)
			elif isinstance(value, dict):
				problems.append(self.tr('%s: a subtree instead of a %s value, left out') % (key_path, item.type))
			else:
				converted = convert_value(item.type, value)
				if converted is None:
					problems.append(self.tr('%s: %r is not a %s, left out') % (key_path, value, item.type))
				else:
					item.value = converted

	def _addExtra(self, parent, key, value, path, problems):
		"""Add the node ``key`` not in the schema with the ``value`` (a map for a subtree) to ``parent``."""
		type = infer_type(value)
		if type is None:
			problems.append(self.tr('%s: %r has no node type, left out') % (path, value))
			return
		item = _NodeItem({'key': key, 'type': type}, None if type == 'subtree' else value, extra=True)
		self.insert(parent, len(self.siblings(parent)), item, select=False)
		if type == 'subtree':
			for child_key, child_value in value.items():
				self._addExtra(item, child_key, child_value, f'{path}/{child_key}', problems)

	def problems(self):
		"""The paths of the nodes with a value which is not valid, with the reason, and the paths of the
		values missing."""
		invalid, missing = [], []
		for item in self.allItems():
			if item.isSubtree:
				continue
			if item.value is None:
				missing.append(self.path(item))
			elif item.problem() is not None:
				invalid.append(f'{self.path(item)}: {item.problem()}')
		return invalid, missing

	@staticmethod
	def path(item):
		keys = []
		while item is not None:
			keys.insert(0, item.key)
			item = item.parent()
		return '/'.join(keys)

	def isEditing(self, index):
		"""Whether the value at ``index`` is being edited."""
		return (self._edit_item is not None and index.column() == self.VALUE_COLUMN and
		        index.siblingAtColumn(0) == self.indexFromItem(self._edit_item, 0))

	@Slot(QTreeWidgetItem, int)
	def _itemClicked(self, item, column):
		if item is self.currentItem() and self._edit_item is None:
			self._openEditor(item)

	def _openEditor(self, item):
		"""Show an editor of the value in the Value cell of ``item``, unless it is a subtree."""
		if item is self._edit_item:
			return
		self._closeEditor(write=True)
		if item is None or item.isSubtree:
			return

		editor = conf.create_editor(CONF_TYPES[item.type], item.text(self.VALUE_COLUMN))
		editor.setFont(item.font(self.VALUE_COLUMN))
		editor.idle.connect(lambda: self._storeEdited(close=False))
		editor.commit.connect(lambda: self._storeEdited(close=False))
		editor.cancel.connect(self._cancelEdit)
		editor.focusLost.connect(lambda close: self._storeEdited(close=close))
		editor.navigate.connect(self._navigate)
		editor.textChanged.connect(self._validateEdited)

		# The text as the editor shows it, e.g. a bool without a value as False, is not a change.
		self._edit_item, self._editor, self._edit_base = item, editor, editor.text()
		self.setItemWidget(item, self.VALUE_COLUMN, editor)
		editor.setValid(self._parse(item, editor.text())[1] is None)
		editor.begin()

	def _closeEditor(self, write):
		"""Remove the editor, storing its value first if ``write`` and it changed."""
		if self._edit_item is None:
			return
		if write:
			self._storeEdited(close=False)
		item, editor = self._edit_item, self._editor
		self._edit_item = self._editor = self._edit_base = None
		editor.stopIdleTimer()
		# Removing the editor takes its focus away; its signals must not act on the next editor.
		editor.blockSignals(True)
		try:
			widget = self.itemWidget(item, self.VALUE_COLUMN)
			if widget is not None:
				widget.hide()
			self.removeItemWidget(item, self.VALUE_COLUMN)
			self.viewport().update(self.visualItemRect(item))
		except RuntimeError:
			# The item is gone already (e.g. the tree was cleared).
			pass

	def finishEditing(self, write):
		"""Close the editor, storing its value first if ``write`` and it changed."""
		self._closeEditor(write=write)

	@Slot()
	def _cancelEdit(self):
		self._closeEditor(write=False)
		self.setFocus()

	@Slot(int)
	def _navigate(self, step):
		current = self.currentItem()
		target = self.itemAbove(current) if step < 0 else self.itemBelow(current)
		if target is not None:
			self.setCurrentItem(target)
		if self._editor is None:
			self.setFocus()

	@staticmethod
	def _parse(item, text):
		"""The value of ``item`` the ``text`` shows and why it is not valid (``None`` if it is). Empty text
		of a type other than a text string removes the value."""
		if item.type != 'str' and not text.strip():
			return None, None
		try:
			value = parse_value(item.type, text)
		except ValueError as e:
			return None, str(e)
		return value, check_value(item.node, value)

	@Slot(str)
	def _validateEdited(self, text):
		if self._editor is not None:
			self._editor.setValid(self._parse(self._edit_item, text)[1] is None)

	def _storeEdited(self, close):
		"""Store the text of the editor as the value if it changed since the edit started or was last stored,
		then close the editor if ``close``. Text which is not a valid value is not stored; closing the editor
		discards it."""
		item, editor = self._edit_item, self._editor
		if item is None:
			return
		text = editor.text()
		if text != self._edit_base:
			value, error = self._parse(item, text)
			if error is not None:
				if close:
					self._logger.warning(f'not storing {self.path(item)}: {error}')
			else:
				self._edit_base = text
				item.value = value
				self.showItem(item)
				self.edited.emit()
		if close:
			self._closeEditor(write=False)


class NodeWindow(AppletWindow):
	"""A window adding or editing a node: its key, type, brief and detailed description, the range of its
	value and its default value. OK emits :attr:`accepted` with the node map and closes the window, the window
	emits :attr:`finished` once it closes, either way."""

	MAXIMIZE = False
	ON_TOP = True

	accepted = Signal(dict)
	finished = Signal()

	#: The range fields as (field, label, placeholder).
	RANGE_ROWS = (
		('min', 'Minimum:', 'no minimum'),
		('max', 'Maximum:', 'no maximum'),
		('min_len', 'Minimum length:', 'no minimum'),
		('max_len', 'Maximum length:', 'no maximum'),
		('regexp', 'Regular expression:', 'the whole value matches it, e.g. ^[A-Z0-9-]+$'),
	)

	def __init__(self, title, node, taken, subtree_only=False, parent=None):
		"""``node`` is the node edited, an empty map when adding one, ``taken`` the keys the node cannot
		have. A node with children stays a subtree if ``subtree_only``."""
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(title)
		self._taken = taken

		self._ok = DauiButton(self.tr('OK'), Icons.confirm())
		self._ok.clicked.connect(self._accept)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self.close)

		self._key = QLineEdit(node.get('key', ''))
		self._key.setPlaceholderText(self.tr('the name of the node in the MIB, e.g. sn'))
		self._type = QComboBox()
		self._type.addItems(['subtree'] if subtree_only else list(TYPES))
		self._type.setCurrentText(node.get('type', 'str'))
		self._type.setEnabled(not subtree_only)
		self._brief = QLineEdit(node.get('brief', ''))
		self._brief.setPlaceholderText(self.tr('a short description, e.g. Serial number'))
		self._detail = QPlainTextEdit(node.get('detail', ''))
		self._detail.setPlaceholderText(self.tr('the full description of the node'))
		self._detail.setTabChangesFocus(True)
		self._detail.setFixedHeight(self._detail.fontMetrics().lineSpacing() * 5)

		self._form = form_layout()
		self._form.addRow(WrappedLabel(self.tr('Key:')), self._key)
		self._form.addRow(WrappedLabel(self.tr('Type:')), self._type)
		self._form.addRow(WrappedLabel(self.tr('Brief description:')), self._brief)
		self._form.addRow(WrappedLabel(self.tr('Detailed description:')), self._detail)
		self._range = {}
		for field, label, placeholder in self.RANGE_ROWS:
			edit = QLineEdit(self._fieldText(node, field))
			edit.setPlaceholderText(self.tr(placeholder))
			self._range[field] = edit
			self._form.addRow(WrappedLabel(self.tr(label)), edit)
		self._default = QLineEdit(self._fieldText(node, 'default'))
		self._default.setPlaceholderText(self.tr('the value of a new node, none if empty'))
		self._form.addRow(WrappedLabel(self.tr('Default value:')), self._default)

		self._error = QLabel()
		self._error.setWordWrap(True)
		self._error.setStyleSheet(f'color: {MibTreeWidget.INVALID_COLOR};')

		for edit in [self._key, self._brief, self._default] + list(self._range.values()):
			edit.textChanged.connect(self._validate)
			# Enter confirms from the edit boxes.
			edit.returnPressed.connect(self._ok.click)
		self._detail.textChanged.connect(self._validate)
		self._type.currentTextChanged.connect(self._typeChanged)
		QShortcut(QKeySequence(Qt.Key_Escape), self, cancel.click, context=Qt.WidgetWithChildrenShortcut)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._ok)
		self._tb.addWidget(cancel)

		content = QVBoxLayout()
		content.setContentsMargins(9, 9, 9, 9)
		content.addLayout(self._form)
		content.addWidget(self._error)
		content.addStretch(1)
		layout = QVBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addLayout(content, stretch=1)
		layout.addWidget(self._tb)
		widget = QWidget()
		widget.setLayout(layout)
		polish_forms(widget)
		self.setWidget(widget)
		self.resize(520, 560)
		self._typeChanged(self._type.currentText())

	@staticmethod
	def _fieldText(node, field):
		value = node.get(field)
		if value is None:
			return ''
		return value.hex() if isinstance(value, bytes) else str(value)

	def showEvent(self, event):
		super().showEvent(event)
		self._key.setFocus()

	@Slot(str)
	def _typeChanged(self, type):
		fields = RANGE_FIELDS.get(type, ())
		for field, edit in self._range.items():
			self._form.setRowVisible(edit, field in fields)
		self._form.setRowVisible(self._default, type != 'subtree')
		self._default.setPlaceholderText(
			self.tr('true or false, none if empty') if type == 'bool' else
			self.tr('hex bytes, none if empty') if type == 'bstr' else
			self.tr('the value of a new node, none if empty'))
		self._validate()

	def node(self):
		"""The node as edited. Raises ``ValueError`` with the reason if it is not valid."""
		key, type = self._key.text().strip(), self._type.currentText()
		error = key_error(key, self._taken)
		if error is not None:
			raise ValueError(error)
		node = {'key': key, 'type': type}
		for field, edit in (('brief', self._brief), ('detail', self._detail)):
			text = edit.toPlainText().strip() if edit is self._detail else edit.text().strip()
			if text:
				node[field] = text
		fields = RANGE_FIELDS.get(type, ())
		texts = {field: self._range[field].text().strip() for field in fields}
		for field in ('min', 'max'):
			if texts.get(field):
				try:
					node[field] = parse_value(type, texts[field])
				except ValueError:
					raise ValueError(f'the {field}imum is not a valid {type}')
				if check_value({'type': type}, node[field]) is not None:
					raise ValueError(f'the {field}imum is out of the {type} range')
		for field in ('min_len', 'max_len'):
			if texts.get(field):
				try:
					node[field] = int(texts[field], 0)
				except ValueError:
					node[field] = -1
				if node[field] < 0:
					raise ValueError(f'the {field[:3]}imum length is not a length')
		for low, high in (('min', 'max'), ('min_len', 'max_len')):
			if node.get(low) is not None and node.get(high) is not None and node[low] > node[high]:
				raise ValueError('the minimum is more than the maximum')
		if texts.get('regexp'):
			try:
				re.compile(texts['regexp'])
			except re.error as e:
				raise ValueError(f'the regular expression is not valid: {e}')
			node['regexp'] = texts['regexp']
		text = self._default.text()
		if type != 'subtree' and (text if type == 'str' else text.strip()):
			try:
				node['default'] = parse_value(type, text)
			except ValueError:
				raise ValueError(f'the default value is not a valid {type}')
			error = check_value(node, node['default'])
			if error is not None:
				raise ValueError(f'the default value is {error}')
		return node

	@Slot()
	def _validate(self):
		try:
			self.node()
			error = ''
		except ValueError as e:
			error = str(e)
		self._error.setText(error[:1].upper() + error[1:] + ('.' if error else ''))
		self._ok.setEnabled(not error)

	@Slot()
	def _accept(self):
		if not self._ok.isEnabled():
			return
		self.accepted.emit(self.node())
		self.close()

	def closeEvent(self, event):
		self.finished.emit()
		super().closeEvent(event)


class MibEditor(QWidget):
	"""The MIB tree with its toolbar: loading and saving the schema, reading and writing the MIB, the schema
	edit mode and the schema operations, enabled only in the schema edit mode."""

	def __init__(self, client: FlashClient, partition):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._client = client
		self._partition = partition
		# The flash operation running and its progress window, the window adding or editing a node.
		self._operation = None
		self._progress = None
		self._node_window = None

		self._tree = MibTreeWidget()
		self._tree.currentItemChanged.connect(lambda current, previous: self._updateButtons())
		self._tree.itemDoubleClicked.connect(self._doubleClicked)

		def button(text, icon, tip, slot):
			b = DauiButton(self.tr(text) if text else None, icon)
			b.setToolTip(self.tr(tip))
			b.clicked.connect(slot)
			return b

		self._load_schema = button('Load schema', Icons.folder(), 'Load the schema from a file', self.loadSchema)
		self._save_schema = button('Save schema', Icons.save(), 'Save the schema to a file', self.saveSchema)
		self._read = button('Read MIB', Icons.read(), f'Read the MIB from the {partition} flash partition',
		                    self.readMib)
		self._write = button('Write MIB', Icons.write(), f'Write the MIB to the {partition} flash partition',
		                     self.writeMib)
		self._check = QCheckBox(self.tr('Integrity check'))
		self._check.setToolTip(self.tr('Append a blake2s digest of the content to the MIB written, the device '
		                               'checks it'))
		self._check.setChecked(True)
		self._schema_mode = QCheckBox(self.tr('Schema edit mode'))
		self._schema_mode.setToolTip(self.tr('Allow changing the nodes, otherwise only their values can be '
		                                     'changed'))
		self._schema_mode.toggled.connect(self._schemaModeToggled)
		self._details = QCheckBox(self.tr('Show details'))
		self._details.setToolTip(self.tr('Show the detailed description of each node below it'))
		self._details.toggled.connect(self._tree.setShowDetails)
		for box in (self._check, self._details, self._schema_mode):
			box.setContentsMargins(6, 0, 6, 0)

		self._add_sibling = button('Add sibling', Icons.add(), 'Add a node after the selected one',
		                           self.addSibling)
		self._add_child = button('Add child', Icons.add(), 'Add a node at the end of the selected subtree',
		                         self.addChild)
		self._remove = button('Remove', Icons.remove(), 'Remove the selected node with its children',
		                      self.removeNode)
		self._duplicate = button('Duplicate', Icons.duplicate(), 'Add a copy of the selected node after it',
		                         self.duplicateNode)
		self._edit = button('Edit', Icons.edit(), 'Edit the selected node', self.editNode)
		self._indent = button(None, Icons.indent(), 'Move the selected node to the end of the subtree above it',
		                      self.indentNode)
		self._unindent = button(None, Icons.unindent(), 'Move the selected node out of its subtree, after it',
		                        self.unindentNode)
		self._up = button(None, Icons.up(), 'Move the selected node up among its siblings',
		                  lambda: self.moveNode(-1))
		self._down = button(None, Icons.down(), 'Move the selected node down among its siblings',
		                    lambda: self.moveNode(1))

		# The files and the partition on the first row, the schema operations on the second one.
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb = QToolBar()
		self._schema_tb = QToolBar()
		for toolbar, widgets in (
				(self._tb, (self._load_schema, self._save_schema, None, self._read, self._write, self._check, spacer,
				            self._details, self._schema_mode)),
				(self._schema_tb, (self._add_sibling, self._add_child, self._remove, self._duplicate, self._edit, None,
				                   self._indent, self._unindent, self._up, self._down))):
			for widget in widgets:
				if widget is None:
					toolbar.addSeparator()
				else:
					toolbar.addWidget(widget)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(self._tb)
		layout.addWidget(self._schema_tb)
		layout.addWidget(self._tree)
		self._updateButtons()

	def _selected(self):
		"""The selected item with its parent (``None`` at the top level), its index among its siblings and the
		number of them. ``None`` if there is none."""
		item = self._tree.currentItem()
		if item is None:
			return None
		siblings = self._tree.siblings(item.parent())
		return item, item.parent(), siblings.index(item), len(siblings)

	@Slot()
	def _updateButtons(self):
		idle = self._operation is None
		free = idle and self._node_window is None
		for b in (self._load_schema, self._read, self._write):
			b.setEnabled(free)
		self._save_schema.setEnabled(free and self._tree.topLevelItemCount() > 0)
		self._tree.setEnabled(idle)

		editing = self._schema_mode.isChecked() and free
		selected = self._selected() if editing else None
		item, parent, index, count = selected if selected is not None else (None, None, 0, 0)
		self._add_sibling.setEnabled(editing)
		self._add_child.setEnabled(item is not None and item.isSubtree)
		for b in (self._remove, self._duplicate, self._edit):
			b.setEnabled(item is not None)
		self._up.setEnabled(item is not None and index > 0)
		self._down.setEnabled(item is not None and index < count - 1)
		self._indent.setEnabled(self._indentTarget(selected) is not None)
		self._unindent.setEnabled(parent is not None and item.key not in self._tree.takenKeys(parent.parent()))

	def _indentTarget(self, selected):
		"""The subtree the selected node is indented to, ``None`` if it cannot be."""
		if selected is None or selected[2] == 0:
			return None
		item, parent, index, count = selected
		above = self._tree.siblings(parent)[index - 1]
		if not above.isSubtree or item.key in self._tree.takenKeys(above):
			return None
		return above

	@Slot(bool)
	def _schemaModeToggled(self, on):
		if not on and self._node_window is not None:
			self._node_window.close()
		self._updateButtons()

	@Slot(QTreeWidgetItem, int)
	def _doubleClicked(self, item, column):
		if column != MibTreeWidget.VALUE_COLUMN and self._edit.isEnabled():
			self.editNode()

	def _warn(self, title, text):
		QMessageBox.warning(self, title, text)

	@staticmethod
	def _list(lines, limit=15):
		"""The ``lines`` as a bulleted list, the ones over ``limit`` counted."""
		text = '\n'.join(f'• {line}' for line in lines[:limit])
		if len(lines) > limit:
			text += f'\n… and {len(lines) - limit} more'
		return text

	# Schema files

	@Slot()
	def loadSchema(self):
		"""Load the schema from a file chosen by the user. The values shown are kept in the nodes of the new
		schema, the ones not in it are added as nodes not in the schema."""
		path, _filter = QFileDialog.getOpenFileName(self, self.tr('Load MIB schema'), '', self.tr(SCHEMA_FILTER))
		if not path:
			return
		title = self.tr('Load MIB schema')
		try:
			nodes = parse_schema(read_file(path))
		except (OSError, ValueError) as e:
			self._logger.warning(f'cannot load the schema {path}: {e}')
			self._warn(title, self.tr('Cannot load the schema %s: %s') % (display_name(path), e))
			return
		self._logger.info(f'loaded the schema {path}')
		self._tree.finishEditing(write=True)
		content = self._tree.content()
		self._tree.setSchema(nodes)
		problems = self._tree.setContent(content) if content else []
		self._tree.expandAll()
		self._updateButtons()
		if problems:
			self._warn(title, self.tr('The values shown do not fit the schema:') + '\n\n' + self._list(problems))

	@Slot()
	def saveSchema(self):
		"""Save the schema of the nodes shown to a file chosen by the user."""
		path, _filter = QFileDialog.getSaveFileName(self, self.tr('Save MIB schema'), 'mib.mibschema.json',
		                                            self.tr(SCHEMA_FILTER))
		if not path:
			return
		self._tree.finishEditing(write=True)
		data = json.dumps({'mibschema': SCHEMA_VERSION, 'nodes': self._tree.schema()}, indent='\t',
		                  ensure_ascii=False) + '\n'
		try:
			write_file(path, data.encode())
		except OSError as e:
			self._logger.warning(f'cannot save the schema {path}: {e}')
			self._warn(self.tr('Save MIB schema'), self.tr('Cannot save the schema %s: %s') % (display_name(path), e))
			return
		self._logger.info(f'saved the schema {path}')
		# The nodes read from the MIB are in the schema now.
		self._tree.clearExtra()

	# The MIB partition

	@Slot()
	def readMib(self):
		"""Read the MIB from the partition and show its values in the nodes of the schema."""
		if self._operation is not None:
			return
		self._tree.finishEditing(write=False)
		name = self._partition
		buffer = io.BytesIO()

		def run(progress, abort, stage):
			size, erase, page = self._client.info(name)
			stage('read', size)
			self._client.read(name, size, buffer, progress, abort)

		def done(ok):
			if ok:
				self._showImage(buffer.getvalue())

		stages = {'read': self.tr('Reading the MIB from flash partition %s...') % name}
		self._runOperation(run, self.tr('Read MIB'), self.tr('Reading'), stages, done)

	def _showImage(self, data):
		title = self.tr('Read MIB')
		try:
			content, version, checked = parse_image(data)
		except ValueError as e:
			self._logger.warning(f'cannot read the MIB: {e}')
			self._warn(title, self.tr('Cannot read the MIB: %s') % e)
			return
		self._logger.info(f'read the MIB, structure version {version}, check {checked}')
		problems = self._tree.setContent(content)
		self._tree.expandAll()
		self._check.setChecked(checked is not None)
		if version != MIB_VERSION:
			problems.insert(0, self.tr('the MIB structure version is %d, not %d') % (version, MIB_VERSION))
		if checked is False:
			problems.insert(0, self.tr('the blake2s digest of the content does not match, the device rejects it'))
		self._updateButtons()
		if problems:
			self._warn(title, self.tr('The MIB was read with problems:') + '\n\n' + self._list(problems))

	@Slot()
	def writeMib(self):
		"""Write the values shown to the partition as the MIB, after asking."""
		if self._operation is not None:
			return
		self._tree.finishEditing(write=True)
		title = self.tr('Write MIB')
		invalid, missing = self._tree.problems()
		if invalid:
			self._warn(title, self.tr('Some values are not valid, fix them first:') + '\n\n' + self._list(invalid))
			return
		image = make_image(self._tree.entries(), self._check.isChecked())
		name = self._partition
		question = self.tr('Write the MIB (%s) to the flash partition %s? The whole partition is erased first, '
		                   'its content will be lost.') % (format_size(len(image)), name)
		if missing:
			question += '\n\n' + self.tr('These nodes have no value, they are left out:') + '\n' + \
			            self._list(missing)
		answer = QMessageBox.warning(self, title, question, QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
		if answer != QMessageBox.Yes:
			return

		def run(progress, abort, stage):
			size, erase, page = self._client.info(name)
			if erase is None or page is None:
				raise ValueError('the partition cannot be written')
			self._client.write(name, size, erase, page, image, progress, abort, stage)

		stages = {
			'erase': self.tr('Erasing flash partition %s before writing...') % name,
			'write': self.tr('Writing the MIB (%s) to flash partition %s...') % (format_size(len(image)), name),
			'verify': self.tr('Verifying the MIB written to flash partition %s...') % name,
		}
		self._runOperation(run, title, self.tr('Writing'), stages)

	def _runOperation(self, fn, title, verb, stages, finished=None):
		"""Run the flash operation ``fn(progress, abort, stage)`` (see :class:`~pydaui.applets.flash._Operation`)
		on the partition, showing its progress in a window titled ``title``. ``stages`` maps the stages the
		operation reports to the texts describing them. ``finished`` is called with whether it succeeded."""
		name = self._partition
		self._logger.info(f'{verb.lower()} the MIB partition {name!r}')
		self._progress = ProgressWindow(title, next(iter(stages.values())))
		self._progress.aborted.connect(self._abortOperation)
		self._showProgress(self._progress)

		def ended(error, aborted=False):
			self._operation = None
			self._progress.finish()
			self._progress = None
			if aborted:
				self._logger.info(f'{verb.lower()} the MIB partition {name!r} aborted')
			elif error is not None:
				self._logger.warning(f'{verb.lower()} the MIB partition {name!r} failed: {error}')
				self._warn(title, self.tr('%s flash partition %s failed: %s') % (verb, name, error))
			self._updateButtons()
			if finished is not None:
				finished(error is None and not aborted)

		def progress(done):
			if self._progress is not None:
				self._progress.setProgress(done)

		def stage(what, total):
			if self._progress is not None:
				if what in stages:
					self._progress.setText(stages[what])
					if len(stages) > 1:
						self._progress.setStep(list(stages).index(what) + 1, len(stages))
				self._progress.setFormat(ByteFormat(total))
				self._progress.setProgress(0, total)

		self._operation = _Operation(fn)
		self._operation.stage.connect(stage)
		self._operation.progress.connect(progress)
		self._operation.done.connect(lambda: ended(None))
		self._operation.aborted.connect(lambda: ended(None, aborted=True))
		self._operation.failed.connect(lambda message: ended(message))
		self._operation.start()
		self._updateButtons()

	def _showProgress(self, window):
		"""Show the window in the MDI area the editor is in, centered over it."""
		subwindow = self._subwindow()
		if subwindow is not None and subwindow.mdiArea() is not None:
			subwindow.mdiArea().addSubWindow(window)
			window.move(subwindow.geometry().center() - window.rect().center())
		window.show()

	def _subwindow(self):
		subwindow = self.parent()
		while subwindow is not None and not isinstance(subwindow, QMdiSubWindow):
			subwindow = subwindow.parent()
		return subwindow

	@Slot()
	def _abortOperation(self):
		if self._operation is not None:
			self._operation.abort()
			self._progress.setText(self.tr('Aborting...'))
			self._progress.setAbortEnabled(False)

	def abort(self):
		"""Abort the operation running and close the windows it opened, e.g. when the applet window closes."""
		if self._operation is not None:
			self._operation.blockSignals(True)
			self._operation.abort()
			self._operation = None
			self._progress.finish()
			self._progress = None
		if self._node_window is not None:
			self._node_window.close()

	# Schema operations

	def _openNodeWindow(self, title, node, taken, subtree_only, accepted):
		"""Open the window adding or editing a node, ``accepted`` is called with the node map on OK."""
		self._tree.finishEditing(write=True)
		window = NodeWindow(title, node, taken, subtree_only)
		window.accepted.connect(accepted)
		window.finished.connect(self._nodeWindowClosed)
		self._node_window = window
		self._showProgress(window)
		self._updateButtons()

	@Slot()
	def _nodeWindowClosed(self):
		self._node_window = None
		self._updateButtons()

	def _addNode(self, parent, index, node):
		item = _NodeItem(node, node.get('default'))
		self._tree.insert(parent, index, item)
		self._tree.showItem(item)
		self._updateButtons()

	@Slot()
	def addSibling(self):
		selected = self._selected()
		parent, index = (selected[1], selected[2] + 1) if selected is not None else (None, self._tree.topLevelItemCount())
		self._openNodeWindow(self.tr('Add node'), {}, self._tree.takenKeys(parent), False,
		                     lambda node: self._addNode(parent, index, node))

	@Slot()
	def addChild(self):
		item = self._tree.currentItem()
		if item is None or not item.isSubtree:
			return
		self._openNodeWindow(self.tr('Add node to %s') % item.key, {}, self._tree.takenKeys(item), False,
		                     lambda node: self._addNode(item, item.childCount(), node))

	@Slot()
	def editNode(self):
		item = self._tree.currentItem()
		if item is None:
			return
		self._openNodeWindow(self.tr('Edit node %s') % item.key, item.node,
		                     self._tree.takenKeys(item.parent(), exclude=item), item.childCount() > 0,
		                     lambda node: self._editedNode(item, node))

	def _editedNode(self, item, node):
		old_type = item.type
		item.node = node
		if item.isSubtree:
			item.value = None
		elif item.value is not None and item.type != old_type:
			# Kept if it is a value of the new type too, e.g. an u32 one of s32.
			try:
				item.value = parse_value(item.type, format_value(old_type, item.value))
			except ValueError:
				item.value = None
		self._tree.showItem(item)
		# A new value editor for the type.
		self._tree.setCurrentItem(None)
		self._tree.setCurrentItem(item)
		self._updateButtons()

	@Slot()
	def removeNode(self):
		item = self._tree.currentItem()
		if item is None:
			return
		count = sum(1 for _ in self._descendants(item))
		question = self.tr('Remove the node %s?') % item.key
		if count:
			question = self.tr('Remove the node %s with its %d descendants?') % (item.key, count)
		if QMessageBox.question(self, self.tr('Remove node'), question) != QMessageBox.Yes:
			return
		self._tree.take(item)
		self._updateButtons()

	@classmethod
	def _descendants(cls, item):
		for child in item.children():
			yield child
			yield from cls._descendants(child)

	def _clone(self, item):
		copy = _NodeItem(dict(item.node), item.value, item.extra)
		for child in item.children():
			copy.addChild(self._clone(child))
		return copy

	@Slot()
	def duplicateNode(self):
		selected = self._selected()
		if selected is None:
			return
		item, parent, index, count = selected
		copy = self._clone(item)
		taken = self._tree.takenKeys(parent)
		base = re.sub(r'_\d+$', '', item.key)
		number = 2
		while f'{base}_{number}' in taken:
			number += 1
		copy.node['key'] = f'{base}_{number}'
		self._tree.insert(parent, index + 1, copy)
		for each in [copy] + list(self._descendants(copy)):
			self._tree.showItem(each)
		copy.setExpanded(item.isExpanded())
		self._updateButtons()

	def _move(self, item, parent, index):
		expanded = item.isExpanded()
		self._tree.take(item)
		self._tree.insert(parent, index, item)
		item.setExpanded(expanded)
		self._updateButtons()

	@Slot()
	def moveNode(self, step):
		selected = self._selected()
		if selected is not None and 0 <= selected[2] + step < selected[3]:
			self._move(selected[0], selected[1], selected[2] + step)

	@Slot()
	def indentNode(self):
		selected = self._selected()
		above = self._indentTarget(selected)
		if above is not None:
			self._move(selected[0], above, above.childCount())

	@Slot()
	def unindentNode(self):
		selected = self._selected()
		if selected is None or selected[1] is None:
			return
		item, parent = selected[0], selected[1]
		if item.key in self._tree.takenKeys(parent.parent()):
			return
		grandparent = parent.parent()
		self._move(item, grandparent, self._tree.siblings(grandparent).index(parent) + 1)


class MibWindow(AppletWindow):
	"""The MIB editor of the MIB partition on an interface speaking the flash protocol on the connected
	device. It talks over the side connection to the interface shared with the other applets (see
	:meth:`PyDauiConnection.sharedSide`), the device serves a single session per interface. The window closes
	when the device is disconnected."""

	def __init__(self, connection, interface, context):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)
		# The context is the name of the MIB partition, see probe().
		self._logger.info(f'opening the MIB editor, partition {context!r} on interface {interface!r}')
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(self.tr('MIB editor (%s)') % interface)
		self.setWindowIcon(Icons.chip())
		self._editor = MibEditor(connection.flashClient(interface), context)
		self.setWidget(self._editor)
		self.resize(1000, 550)

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._editor.abort()
		super().closeEvent(event)
