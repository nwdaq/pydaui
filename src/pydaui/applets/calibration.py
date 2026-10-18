# SPDX-License-Identifier: GPL-3.0-or-later
#
# Calibration certificate applet
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Calibration certificate applet: load, inspect and save the digital calibration certificate of a plumCore
device (plumCore EP-7d3).

A certificate is a CBOR indefinite-length map starting with the ``"CBCAL"`` format version, see
:func:`decode` and :func:`encode`. It is read from and written to a flash partition of the device over an
interface speaking the ``flash`` protocol, or opened from and saved to a local file. Its top-level sections
(administrative data, targets, configurations, measuring equipment and results) are shown in the tabs.
"""

import base64
import copy
import datetime
import io
import json
import logging
import re
import secrets
import threading
import uuid

import cbor2
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from pynbus2.services import flash as flash_service

from pydaui import build_info, compensation, quantities
from pydaui.applets import calibration_pdf, conf
from pydaui.applets.flash import Aborted, FlashClient, _Operation, can_read, can_write, format_size, interface_mtu
from pydaui.files import display_name, read_file, write_file
from pydaui.ui.helpers import Admonition, AppletWindow, DauiButton, StyledTreeWidget, TabScrollArea, WrappedLabel
from pydaui.ui.icons import Icons
from pydaui.ui.plot import GRAPH_COLORS, Plot, monospace_font
from pydaui.ui.progress_window import ByteFormat, ProgressWindow
from pydaui.ui.quantity_selector import QuantitySelector
from pydaui.ui.settings_window import (CalibrationCompaniesPage, CalibrationPersonsPage, SettingsGroup, form_layout,
                                       polish_forms)


#: The protocol name of the interfaces this applet talks to.
PROTOCOL = flash_service.PROTOCOL

#: The key of the format version, the first pair of the certificate map.
VERSION_KEY = 'CBCAL'

#: The format version written, and the versions read.
VERSION = 1
VERSIONS = (1,)

#: The leading bytes of every certificate: an indefinite-length map, the "CBCAL" text and the version 1.
MAGIC = b'\xbf\x65CBCAL\x01'

#: The partition preselected if the device has one, else the first one with this in its name.
DEFAULT_PARTITION = 'cal'

#: The file types offered in the file dialogs.
FILE_FILTER = 'Calibration certificates (*.cbor);;All files (*)'
PDF_FILTER = 'PDF documents (*.pdf);;All files (*)'

#: The top-level sections of a certificate as (key, title, icon, description).
SECTIONS = (
	('adm', 'Administrative', Icons.company, 'The certificate, the issuing laboratory and the calibration event'),
	('tgt', 'Targets', Icons.chip, 'The calibrated entities, e.g. a sensor or an ADC'),
	('cfg', 'Configurations', Icons.conf, 'The device configurations the results were taken under'),
	('meq', 'Equipment', Icons.probe, 'The measuring equipment and reference standards used'),
	('res', 'Results', Icons.data, 'The measurement results grouped by the influence conditions'),
)


#: The names of the software components the certificate may list in the software of the administrative data.
FIRMWARE_NAME = 'plumCore'
SOFTWARE_NAME = 'pydaui'

#: The conf path of the firmware version of the device.
FIRMWARE_VERSION_PATH = ['system', 'firmware', 'version']


#: The kinds of targets offered, others may be typed.
TARGET_TYPES = ('device', 'module', 'sensor', 'adc', 'dac', 'channel')


#: The ways a certificate identifier is made, see :func:`generate_id`.
ID_CUSTOM = 'Custom'
ID_YEAR_RANDOM = 'Year + random'
ID_UUID = 'UUID'
ID_KINDS = (ID_CUSTOM, ID_YEAR_RANDOM, ID_UUID)
DEFAULT_ID_KIND = ID_YEAR_RANDOM


def generate_id(kind):
	"""A new certificate identifier of the ``kind`` of :data:`ID_KINDS`: ``<year>.<random 32 bits in hex>``,
	e.g. ``2026.3f9a0c7e``, or a random UUID. ``None`` for a custom one, which the user types."""
	if kind == ID_YEAR_RANDOM:
		return f'{datetime.date.today().year}.{secrets.randbits(32):08x}'
	if kind == ID_UUID:
		return str(uuid.uuid4())
	return None


def parse_date(text):
	"""The ISO 8601 date or date and time ``text`` of a certificate as a local :class:`QDateTime`, ``None`` if
	it is not one. A time without a time zone is a local time."""
	try:
		dt = datetime.datetime.fromisoformat(str(text))
	except ValueError:
		return None
	if dt.tzinfo is not None:
		return QDateTime.fromSecsSinceEpoch(int(dt.timestamp()))
	return QDateTime(QDate(dt.year, dt.month, dt.day), QTime(dt.hour, dt.minute, dt.second))


def format_datetime(value):
	"""The :class:`QDateTime` ``value`` as an ISO 8601 UTC date and time, e.g. ``2026-06-25T09:00:00Z``."""
	dt = datetime.datetime.fromtimestamp(value.toSecsSinceEpoch(), datetime.timezone.utc)
	return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def format_date(value):
	"""The date of the :class:`QDateTime` ``value`` as an ISO 8601 date, e.g. ``2026-06-27``."""
	return value.date().toString('yyyy-MM-dd')


class Certificate:
	"""A calibration certificate: the ``content`` map without the format version, and the bytes following the
	map (the signatures, see :func:`decode`).

	``signed`` tells whether the certificate is signed: the ``sign`` keys of the signing entities (see
	:func:`signing_entities`) are encoded and the signatures follow the map. Unsigned, :func:`encode` leaves
	them out, they are kept in the ``content`` meanwhile.
	"""

	def __init__(self, content=None, signatures=b'', signed=False):
		self.content = content if content is not None else {}
		self.signatures = signatures
		self.signed = signed

	def section(self, key):
		"""The top-level section ``key``, ``None`` if it is absent."""
		return self.content.get(key)

	def software(self):
		"""The software components listed in the administrative data, the entries that are not maps left
		out."""
		sw = (self.section('adm') or {}).get('sw')
		return [entry for entry in sw if isinstance(entry, dict)] if isinstance(sw, list) else []

	def setSoftware(self, name, version):
		"""List the software component ``name`` in the version ``version``, replacing it if it is listed,
		or remove it if ``version`` is ``None``."""
		software = [entry for entry in self.software() if entry.get('n') != name]
		if version is not None:
			software.append({'n': name, 'ver': version})
		self.setField('adm', 'sw', software or None)

	def setSection(self, key, value):
		"""Set the top-level section ``key`` to ``value``, an empty ``value`` removes it."""
		if value in (None, '', {}, []):
			self.content.pop(key, None)
		else:
			self.content[key] = value

	def setField(self, section, key, value):
		"""Set the ``key`` of the map ``section`` to ``value``, adding the section if it is absent. An empty
		``value`` removes the key, and the section once it is empty."""
		fields = self.content.setdefault(section, {})
		if value in (None, ''):
			fields.pop(key, None)
			if not fields:
				del self.content[section]
		else:
			fields[key] = value


def signing_entities(content):
	"""The signing entities in the certificate ``content``, the maps which may carry a ``sign`` key: the
	laboratory, the customer and the responsible persons, in the order their signatures follow."""
	adm = content.get('adm')
	if not isinstance(adm, dict):
		return []
	entities = []
	for key, value in adm.items():
		if key in ('lab', 'cst') and isinstance(value, dict):
			entities.append(value)
		elif key == 'rsp' and isinstance(value, list):
			entities.extend(person for person in value if isinstance(person, dict))
	return entities


def target_parent(targets, key):
	"""The identifier of the parent of the target ``key`` in the ``targets`` (the ``tgt`` section), ``None``
	if it has none, or if its parent is not a target or is its own descendant."""
	parent = targets[key].get('p')
	ancestor, seen = parent, {key}
	while isinstance(targets.get(ancestor), dict):
		if ancestor in seen:
			return None
		seen.add(ancestor)
		ancestor = targets[ancestor].get('p')
	return parent if isinstance(targets.get(parent), dict) else None


def target_tree(targets):
	"""The targets which are maps in the ``targets`` (the ``tgt`` section) as ``(key, target, depth)`` in the
	order of their tree, each after its parent (see :func:`target_parent`), the siblings in the order of the
	targets. The depth of a target without a parent is 0."""
	targets = {key: target for key, target in targets.items() if isinstance(target, dict)}
	children = {}
	for key in targets:
		children.setdefault(target_parent(targets, key), []).append(key)
	tree = []

	def walk(parent, depth):
		for key in children.get(parent, []):
			tree.append((key, targets[key], depth))
			walk(key, depth + 1)

	walk(None, 0)
	return tree


def natural_key(key):
	"""A sort key ordering the identifiers ``key`` by their text with the numbers in it by value, e.g. ``g2``
	before ``g10``."""
	return [(0, int(part), '') if part.isdigit() else (1, 0, part) for part in re.split(r'(\d+)', str(key))]


def parse_setting(text):
	"""The value of a configuration setting typed as ``text``: ``true`` and ``false`` a bool, a number an int or
	a float, anything else the text."""
	text = text.strip()
	if text in ('true', 'false'):
		return text == 'true'
	for kind in (int, float):
		try:
			return kind(text)
		except ValueError:
			pass
	return text


def parse_number(text):
	"""The number ``text`` as an int, or a float if it is not an integer. Raises ``ValueError`` if it is not a
	number."""
	text = text.strip()
	try:
		return int(text)
	except ValueError:
		return float(text)


def parse_numbers(text):
	"""The number ``text``, or a list of the numbers in it separated by commas, e.g. per-point uncertainties.
	Raises ``ValueError`` if a part is not a number."""
	if ',' in text:
		return [parse_number(part) for part in text.split(',')]
	return parse_number(text)


def format_value(value):
	"""A value of a quantity for display, a list of values separated by commas."""
	if isinstance(value, (list, tuple)):
		return ', '.join(format_scalar(item) for item in value)
	return format_scalar(value)


def decode(data, padded=False):
	"""The :class:`Certificate` encoded in ``data``. ``padded`` data is the content of a flash partition, the
	erased flash (0xff) following the certificate is not a part of it. Raises ``ValueError`` if ``data`` is not
	a certificate of a supported version.

	The signature methods are not specified yet, the bytes following the map are kept as they are.
	"""
	if not data.startswith(MAGIC[:-1]):
		raise ValueError('not a calibration certificate')
	fp = io.BytesIO(data)
	try:
		content = cbor2.CBORDecoder(fp).decode()
	except Exception as e:
		raise ValueError(f'invalid certificate: {e}')
	if not isinstance(content, dict):
		raise ValueError('invalid certificate')
	version = content.pop(VERSION_KEY, None)
	if version not in VERSIONS:
		raise ValueError(f'unsupported certificate version {version}')
	signatures = data[fp.tell():]
	if padded:
		signatures = signatures.rstrip(b'\xff')
	signed = any('sign' in entity for entity in signing_entities(content))
	return Certificate(content, signatures, signed)


def _unsigned(content):
	"""A copy of the certificate ``content`` without the ``sign`` keys of the signing entities."""
	content = copy.deepcopy(content)
	for entity in signing_entities(content):
		entity.pop('sign', None)
	return content


def encode(certificate):
	"""The bytes of the ``certificate``: an indefinite-length map with the format version first, the
	signatures following it. Unsigned, without the ``sign`` keys and the signatures."""
	content = certificate.content if certificate.signed else _unsigned(certificate.content)
	parts = [b'\xbf', cbor2.dumps(VERSION_KEY), cbor2.dumps(VERSION)]
	for key, value in content.items():
		parts.append(cbor2.dumps(key))
		parts.append(cbor2.dumps(value))
	parts.append(b'\xff')
	if certificate.signed:
		parts.append(certificate.signatures)
	return b''.join(parts)


def split_remarks(text):
	"""The paragraphs of the ``text`` of the remarks, separated by blank lines, as the items of the ``rem``
	array of the administrative data. The lines of a paragraph are kept, the blank ones left out."""
	paragraphs, lines = [], []
	for line in text.splitlines() + ['']:
		if line.strip():
			lines.append(line.rstrip())
		elif lines:
			paragraphs.append('\n'.join(lines))
			lines = []
	return paragraphs


def join_remarks(remarks):
	"""The text of the ``rem`` array of the administrative data, the reverse of :func:`split_remarks`."""
	return '\n\n'.join(str(remark) for remark in remarks) if isinstance(remarks, list) else ''


def format_preview(data, count):
	"""The first ``count`` bytes of ``data`` as ``0xbf 0x65 ...``, followed by an ellipsis if there are
	more."""
	text = ' '.join(f'0x{byte:02x}' for byte in data[:count])
	return text + ' ...' if len(data) > count else text


def hex_dump(data, width=16):
	"""``data`` as lines of ``width`` bytes: the offset, the bytes in hex in two halves and the printable
	ASCII characters, other bytes as dots."""
	lines = []
	half = width // 2
	for offset in range(0, len(data), width):
		chunk = data[offset:offset + width]
		hex_bytes = [f'{byte:02x}' for byte in chunk] + ['  '] * (width - len(chunk))
		text = ''.join(chr(byte) if 0x20 <= byte < 0x7f else '.' for byte in chunk)
		lines.append(f'{offset:08x}  {" ".join(hex_bytes[:half])}  {" ".join(hex_bytes[half:])}  |{text}|')
	return '\n'.join(lines)


def _jsonable(value):
	"""``value`` decoded from CBOR as a value JSON encodes: the map keys as text, the byte strings in hex and
	anything else JSON has no type of (e.g. a tagged value) as text."""
	if isinstance(value, dict):
		return {key if isinstance(key, str) else format_scalar(key): _jsonable(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_jsonable(item) for item in value]
	if isinstance(value, bytes):
		return value.hex()
	if value is None or isinstance(value, (bool, int, float, str)):
		return value
	return str(value)


def to_json(data):
	"""The certificate encoded in ``data`` as JSON, the format version first. Without the signatures, they
	are not a part of the map."""
	content = {VERSION_KEY: VERSION, **decode(data).content}
	return json.dumps(_jsonable(content), indent=2, ensure_ascii=False)


def c_array(data, width=16):
	"""``data`` as the items of a C array, ``0xbf, 0x65, ...`` in lines of ``width`` bytes."""
	return ',\n'.join(', '.join(f'0x{byte:02x}' for byte in data[offset:offset + width])
	                  for offset in range(0, len(data), width))


def format_scalar(value):
	"""A scalar ``value`` of a certificate formatted for display."""
	if value is None:
		return 'null'
	if isinstance(value, bool):
		return 'true' if value else 'false'
	if isinstance(value, float):
		return f'{value:.9g}'
	if isinstance(value, bytes):
		return value.hex()
	return str(value)


class _PartitionLister(QObject):
	"""Lists the partitions of the device with their geometry off the GUI thread. ``listed`` carries a list of
	``(name, detail)``, the detail like in :func:`~pydaui.applets.flash.can_read`, ``None`` if it cannot be
	loaded. ``failed`` tells the partitions cannot be listed."""

	listed = Signal(list)
	failed = Signal(str)

	def __init__(self, client):
		super().__init__()
		self._client = client

	def start(self):
		threading.Thread(target=self._run, name='cal-list', daemon=True).start()

	def _run(self):
		try:
			partitions = []
			for name in self._client.list():
				try:
					size, erase, page = self._client.info(name)
					partitions.append((name, {'size': size, 'erase': erase, 'page': page}))
				except ValueError:
					partitions.append((name, None))
		except Exception as e:
			self.failed.emit(str(e))
			return
		self.listed.emit(partitions)


class _FirmwareVersionReader(QObject):
	"""Reads the firmware version of the device off the GUI thread with ``read``, a function returning it or
	``None``. ``read`` carries it, ``None`` if it cannot be read."""

	done = Signal(object)

	def __init__(self, read):
		super().__init__()
		self._read = read

	def start(self):
		threading.Thread(target=self._run, name='cal-firmware', daemon=True).start()

	def _run(self):
		try:
			version = self._read()
		except Exception as e:
			logging.getLogger(self.__class__.__name__).info(f'cannot read the firmware version: {e}')
			version = None
		self.done.emit(version)


class SectionView(QWidget):
	"""A tab showing one top-level section of the certificate as a tree of its keys and values. The maps and
	the arrays are branches, the scalars leaves. A hint is shown while the section is absent."""

	KEY_COLUMN = 0
	VALUE_COLUMN = 1

	#: Colour of the hint.
	HINT_COLOR = '#888'

	def __init__(self, key, title):
		super().__init__()
		self._key = key
		self._tree = StyledTreeWidget()
		self._tree.setColumnCount(2)
		self._tree.setHeaderLabels([self.tr('Key'), self.tr('Value')])
		self._tree.header().setDefaultSectionSize(200)

		self._hint = QLabel(self.tr('The certificate has no %s section.') % title.lower())
		self._hint.setAlignment(Qt.AlignCenter)
		self._hint.setStyleSheet(f'color: {self.HINT_COLOR};')
		self._stack = QStackedWidget()
		self._stack.addWidget(self._hint)
		self._stack.addWidget(self._tree)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.addWidget(self._stack)

	def setCertificate(self, certificate):
		"""Show the section of the ``certificate``."""
		self.setValue(certificate.section(self._key))

	def setValue(self, value):
		"""Show the section ``value``, ``None`` if the section is absent."""
		self._tree.clear()
		if value is None:
			self._stack.setCurrentWidget(self._hint)
			return
		self._addChildren(self._tree.invisibleRootItem(), value)
		self._tree.expandAll()
		self._stack.setCurrentWidget(self._tree)

	def _addChildren(self, parent, value):
		if isinstance(value, dict):
			children = ((str(key), child) for key, child in value.items())
		else:
			children = ((f'[{i}]', child) for i, child in enumerate(value))
		for key, child in children:
			item = QTreeWidgetItem(parent, [key])
			self._tree.styleName(item)
			if isinstance(child, (dict, list, tuple)):
				self._addChildren(item, child)
			else:
				item.setText(self.VALUE_COLUMN, format_scalar(child))


class _PreviewEdit(QLineEdit):
	"""Shows the first bytes of the data set with :meth:`setData`, as many as fit its width."""

	#: Room left for the frame and the margins of the text.
	MARGIN = 16

	def __init__(self):
		super().__init__()
		self._bytes = b''

	def setData(self, data):
		self._bytes = data
		self._update()

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self._update()

	def _update(self):
		width = self.width() - self.MARGIN
		metrics = self.fontMetrics()
		# Guessed from the width of a byte, fewer while the text does not fit, e.g. in a proportional font.
		count = max(0, width // max(1, metrics.horizontalAdvance('0x00 ')))
		while count > 0 and metrics.horizontalAdvance(format_preview(self._bytes, count)) > width:
			count -= 1
		self.setText(format_preview(self._bytes, count))


class CertificateView(QWidget):
	"""The Certificate tab, the certificate as a whole: its format and its data, opening and saving it, the
	software listed in it and its integrity control. The buttons ask the :class:`CalibrationCertificate` with their signals, ``changed``
	tells the user changed the certificate."""

	changed = Signal()
	openRequested = Signal()
	saveRequested = Signal()
	pdfRequested = Signal()
	viewDataRequested = Signal()

	def __init__(self):
		super().__init__()
		self._certificate = None

		form = QFormLayout()
		form.setRowWrapPolicy(QFormLayout.WrapLongRows)
		form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		form.addRow(self.tr('Certificate format:'),
		            QLabel(self.tr('CBOR calibration certificate, version %d') % VERSION))

		view = DauiButton(self.tr('View'), Icons.storage())
		view.setToolTip(self.tr('Show all the bytes of the certificate in a new window'))
		view.clicked.connect(self.viewDataRequested)
		self._data = _PreviewEdit()
		self._data.setEnabled(False)
		self._data.setToolTip(self.tr('The first bytes of the certificate, as written to a file or the flash'))
		self._data.setFixedHeight(view.sizeHint().height())
		data_row = QHBoxLayout()
		data_row.setSpacing(4)
		data_row.addWidget(self._data, stretch=1)
		data_row.addWidget(view)
		form.addRow(self.tr('Certificate data:'), data_row)

		open_file = DauiButton(self.tr('Open'), Icons.folder())
		open_file.setToolTip(self.tr('Open a certificate from a local file'))
		open_file.clicked.connect(self.openRequested)
		save = DauiButton(self.tr('Save'), Icons.save())
		save.setToolTip(self.tr('Save the certificate to a local file'))
		save.clicked.connect(self.saveRequested)
		pdf = DauiButton(self.tr('Generate PDF'), Icons.certificate())
		pdf.setToolTip(self.tr('Export the certificate as a printable PDF document'))
		pdf.clicked.connect(self.pdfRequested)
		files = QGroupBox(self.tr('Import/Export'))
		files_row = QHBoxLayout(files)
		files_row.addWidget(open_file)
		files_row.addWidget(save)
		files_row.addWidget(pdf)
		files_row.addStretch(1)

		# The versions of the software components the check boxes list, the firmware one once read.
		self._versions = {FIRMWARE_NAME: None, SOFTWARE_NAME: build_info.version() or 'unknown'}
		self._software = {}
		for name, tool_tip in (
				(FIRMWARE_NAME, self.tr('List the firmware of the device with its version in the software of the '
				                        'administrative data')),
				(SOFTWARE_NAME, self.tr('List this application with its version in the software of the '
				                        'administrative data'))):
			box = QCheckBox()
			box.setToolTip(tool_tip)
			box.toggled.connect(lambda checked, name=name: self._softwareToggled(name, checked))
			self._software[name] = box
		metadata = QGroupBox(self.tr('Software metadata'))
		metadata_rows = QVBoxLayout(metadata)
		metadata_rows.addWidget(self._software[FIRMWARE_NAME])
		metadata_rows.addWidget(self._software[SOFTWARE_NAME])
		self._updateSoftware()

		self._checksum = QCheckBox(self.tr('Enable certificate checksum (BLAKE2s)'))
		self._checksum.setEnabled(False)
		self._checksum.setToolTip(self.tr('Not supported by the certificate format yet'))
		self._signed = QCheckBox(self.tr('Enable certificate signing'))
		self._signed.setToolTip(self.tr('Sign the certificate by the laboratory, the customer and the responsible '
		                                'persons with a signature method. Disabled, their signature methods are '
		                                'left out of the certificate.'))
		self._signed.toggled.connect(self._signedToggled)
		integrity = QGroupBox(self.tr('Integrity control'))
		integrity_rows = QVBoxLayout(integrity)
		integrity_rows.addWidget(self._checksum)
		integrity_rows.addWidget(self._signed)

		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(files)
		layout.addWidget(metadata)
		layout.addWidget(integrity)
		layout.addStretch(1)

	def setCertificate(self, certificate):
		self._certificate = certificate
		self._signed.blockSignals(True)
		self._signed.setChecked(certificate.signed)
		self._signed.blockSignals(False)
		self._updateSoftware()

	def setFirmwareVersion(self, version):
		"""Offer to list the firmware of the device in the ``version`` read, ``None`` if it cannot be."""
		self._versions[FIRMWARE_NAME] = version
		self._updateSoftware()

	def _updateSoftware(self):
		"""Show the versions in the check boxes, checked if the certificate lists the component."""
		listed = {entry.get('n') for entry in self._certificate.software()} if self._certificate is not None else set()
		for name, label in ((FIRMWARE_NAME, self.tr('Append device firmware version (%s)')),
		                    (SOFTWARE_NAME, self.tr('Append this software version (%s)'))):
			box = self._software[name]
			version = self._versions[name]
			box.blockSignals(True)
			box.setText(label % (version or self.tr('unknown')))
			box.setChecked(name in listed)
			# Listed already, it can still be removed without knowing the version.
			box.setEnabled(self._certificate is not None and (version is not None or name in listed))
			box.blockSignals(False)

	def _softwareToggled(self, name, checked):
		if self._certificate is not None:
			self._certificate.setSoftware(name, self._versions[name] if checked else None)
			self.changed.emit()

	def setData(self, data):
		"""Show the ``data`` of the certificate encoded."""
		self._data.setData(data)

	@Slot(bool)
	def _signedToggled(self, signed):
		if self._certificate is not None:
			self._certificate.signed = signed
			self.changed.emit()


class CertificateDataWindow(AppletWindow):
	"""The bytes of a certificate in a tab for each format: a hex dump, a C array, Base64 and the certificate
	encoded as JSON instead, updated with :meth:`setData`."""

	MAXIMIZE = False

	#: The formats as (title, the function formatting the bytes).
	FORMATS = (
		('Hex dump', hex_dump),
		('C array', c_array),
		('Base64', lambda data: base64.b64encode(data).decode('ascii')),
		('JSON', to_json),
	)

	def __init__(self, title):
		super().__init__()
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(title)
		self.setWindowIcon(Icons.storage())
		tabs = QTabWidget()
		tabs.setDocumentMode(True)
		# The text of each format with the function formatting it.
		self._dumps = []
		for name, fn in self.FORMATS:
			dump = QPlainTextEdit()
			dump.setReadOnly(True)
			dump.setLineWrapMode(QPlainTextEdit.NoWrap)
			dump.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
			tabs.addTab(dump, self.tr(name))
			self._dumps.append((dump, fn))
		self.setWidget(tabs)
		metrics = self._dumps[0][0].fontMetrics()
		# A whole line of the hex dump and the C array wide, and a few dozens of them high.
		width = max(metrics.horizontalAdvance(fn(bytes(16))) for fn in (hex_dump, c_array))
		self.resize(width + 60, metrics.lineSpacing() * 30 + 90)

	def setData(self, data):
		"""Show ``data``, keeping each text scrolled where it is."""
		for dump, fn in self._dumps:
			scroll = dump.verticalScrollBar().value(), dump.horizontalScrollBar().value()
			dump.setPlainText(fn(data))
			dump.verticalScrollBar().setValue(scroll[0])
			dump.horizontalScrollBar().setValue(scroll[1])


class _DateField(QWidget):
	"""A date (and time) of the certificate: an edit opening a calendar to pick it, and an Assign
	automatically check box on the right. Assigned automatically, the date cannot be edited, it is set by
	:meth:`assignNow` when the event it records happens. ``edited`` tells the date was changed.

	The edit shows the local time. An unset date is shown as the minimum of the edit with a special text."""

	edited = Signal()

	#: The minimum of the edit, standing for an unset date.
	UNSET = QDateTime(QDate(2000, 1, 1), QTime(0, 0))

	def __init__(self, with_time, tool_tip):
		super().__init__()
		self._with_time = with_time
		self._setting = False
		self._edit = QDateTimeEdit() if with_time else QDateEdit()
		self._edit.setCalendarPopup(True)
		self._edit.setDisplayFormat('yyyy-MM-dd HH:mm:ss' if with_time else 'yyyy-MM-dd')
		self._edit.setMinimumDateTime(self.UNSET)
		self._edit.setDateTime(self.UNSET)
		self._edit.setToolTip(tool_tip)
		self._edit.dateTimeChanged.connect(self._changed)
		self._edit.calendarWidget().installEventFilter(self)
		self._auto = QCheckBox(self.tr('Assign automatically'))
		self._auto.setChecked(True)
		self._auto.toggled.connect(self._autoToggled)

		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(8)
		layout.addWidget(self._edit, stretch=1)
		layout.addWidget(self._auto)
		self._autoToggled(True)

	def setAutoToolTip(self, text):
		self._auto.setToolTip(text)

	@property
	def automatic(self):
		return self._auto.isChecked()

	def value(self):
		"""The date as an ISO 8601 text for the certificate, ``None`` if it is not set."""
		value = self._edit.dateTime()
		if value == self.UNSET:
			return None
		return format_datetime(value) if self._with_time else format_date(value)

	def setValue(self, text):
		"""Show the ISO 8601 date ``text``, unset if it is ``None`` or not a date."""
		self._setting = True
		self._edit.setDateTime(parse_date(text) if text is not None and parse_date(text) else self.UNSET)
		self._setting = False

	def setOmitted(self, omitted):
		"""Disable the field while the dates are not included in the certificate."""
		self._auto.setEnabled(not omitted)
		self._edit.setEnabled(not omitted and not self.automatic)

	def assignNow(self):
		"""Set the date to now."""
		self._edit.setDateTime(QDateTime.currentDateTime())

	def eventFilter(self, obj, event):
		# The calendar of an unset date opens at the current month rather than at the minimum.
		if event.type() == QEvent.Show and self._edit.dateTime() == self.UNSET:
			today = QDate.currentDate()
			obj.setCurrentPage(today.year(), today.month())
		return False

	@Slot(bool)
	def _autoToggled(self, automatic):
		self._edit.setEnabled(not automatic)
		self._edit.setSpecialValueText(self.tr('Assigned automatically') if automatic else self.tr('Not set'))
		# A date to start picking from, the calendar opens at the minimum otherwise.
		if not automatic and self._edit.dateTime() == self.UNSET:
			self.assignNow()

	@Slot()
	def _changed(self):
		if not self._setting:
			self.edited.emit()


class _PopupComboBox(QComboBox):
	"""A combo box telling when its popup is about to be shown, e.g. to update the items."""

	aboutToShowPopup = Signal()

	def showPopup(self):
		self.aboutToShowPopup.emit()
		super().showPopup()


class AdministrativeView(QWidget):
	"""The Administrative tab, the fields of the administrative data (``adm``) of the certificate.
	``changed`` tells the user changed the certificate.

	The identifier is typed or generated, of the kind chosen next to it (see :func:`generate_id`). The dates
	are picked, or assigned automatically: the begin when the first calibration value is written, the end
	when the last one is (see :meth:`valueWritten`) and the issue when the certificate is saved (see
	:meth:`issued`). The laboratory and the customer are chosen from the companies in the ``settings`` (see
	:class:`~pydaui.ui.settings_window.CalibrationCompaniesPage`), the chosen one is copied into the
	certificate. The copy in the certificate is offered first, marked as the certificate's own unless it is
	one of the companies. The responsible persons are a list, added from the persons in the ``settings`` (see
	:class:`~pydaui.ui.settings_window.CalibrationPersonsPage`) and removed one by one. The companies and
	the persons are read again whenever a chooser opens, the settings may have changed meanwhile. The dates may be left out of the certificate altogether, they are kept in the fields
	meanwhile and put back when included again.
	"""

	changed = Signal()

	KEY = 'adm'

	def __init__(self, settings):
		super().__init__()
		self._settings = settings
		self._certificate = None

		self._id = QLineEdit()
		self._id.setPlaceholderText(self.tr('e.g. 2026.3f9a0c7e'))
		self._id.setToolTip(self.tr('The unique identifier of the certificate within the namespace of the '
		                            'issuing laboratory'))
		self._id.textEdited.connect(self._idEdited)
		self._id_kind = QComboBox()
		self._id_kind.addItems([self.tr(kind) for kind in ID_KINDS])
		self._id_kind.setCurrentIndex(ID_KINDS.index(DEFAULT_ID_KIND))
		self._id_kind.setToolTip(self.tr('How the identifier is made: typed, the year and a random number, or a '
		                                 'random UUID'))
		self._id_kind.currentIndexChanged.connect(self._updateGenerate)
		self._generate = DauiButton(self.tr('Generate'), Icons.refresh())
		self._generate.setToolTip(self.tr('Replace the identifier with a new one of the chosen kind'))
		self._generate.clicked.connect(self._generateId)
		# As tall as the button next to them.
		for widget in (self._id, self._id_kind):
			widget.setFixedHeight(self._generate.sizeHint().height())
		id_row = QHBoxLayout()
		id_row.setSpacing(4)
		id_row.addWidget(self._id, stretch=1)
		id_row.addWidget(self._id_kind)
		id_row.addWidget(self._generate)

		form = QFormLayout()
		form.setRowWrapPolicy(QFormLayout.WrapLongRows)
		form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		form.addRow(self.tr('Certificate ID:'), id_row)

		self._location = QLineEdit()
		self._location.setPlaceholderText(self.tr('e.g. Metrology Lab 2'))
		self._location.setToolTip(self.tr('Where the calibration was performed'))
		self._location.setFixedHeight(self._generate.sizeHint().height())
		self._location.textEdited.connect(self._locationEdited)
		form.addRow(self.tr('Location:'), self._location)

		# The party choosers by their keys in the administrative data, and the parties offered in each, None
		# for nobody.
		self._parties = []
		self._persons = []
		self._party_combos = {}
		self._party_items = {}
		for key, label, tool_tip in (
				('lab', self.tr('Laboratory:'), self.tr('The calibration laboratory issuing the certificate')),
				('cst', self.tr('Customer:'), self.tr('The customer the calibration was performed for'))):
			combo = _PopupComboBox()
			combo.aboutToShowPopup.connect(self._loadKnown)
			combo.setToolTip(tool_tip)
			combo.setFixedHeight(self._generate.sizeHint().height())
			combo.activated.connect(lambda index, key=key: self._partyChosen(key, index))
			self._party_combos[key] = combo
			form.addRow(label, combo)

		# The responsible persons of the certificate, a row with a remove button each, and the known persons to
		# add below them.
		self._persons_rows = QVBoxLayout()
		self._persons_rows.setContentsMargins(0, 0, 0, 0)
		self._persons_rows.setSpacing(4)
		self._person_combo = _PopupComboBox()
		self._person_combo.aboutToShowPopup.connect(self._loadKnown)
		self._person_combo.setToolTip(self.tr('A person to add to the responsible persons'))
		self._person_combo.setFixedHeight(self._generate.sizeHint().height())
		self._person_combo.currentIndexChanged.connect(self._updateAddPerson)
		self._add_person = DauiButton(self.tr('Add'), Icons.add())
		self._add_person.setToolTip(self.tr('Add the chosen person to the responsible persons'))
		self._add_person.clicked.connect(self._addPerson)
		add_row = QHBoxLayout()
		add_row.setSpacing(4)
		add_row.addWidget(self._person_combo, stretch=1)
		add_row.addWidget(self._add_person)
		persons = QVBoxLayout()
		persons.setSpacing(4)
		persons.addLayout(self._persons_rows)
		persons.addLayout(add_row)
		form.addRow(self.tr('Responsible persons:'), persons)
		form.addRow(Admonition(self.tr('The laboratory, the customer and the responsible persons are chosen from the '
		                               'parties and persons in the application settings, add them there first.'),
		                       Admonition.NOTE))

		# The date fields by their keys in the administrative data.
		self._dates = {
			'db': _DateField(True, self.tr('The date and time the calibration was started, in local time')),
			'de': _DateField(True, self.tr('The date and time the calibration was completed, in local time')),
			'di': _DateField(False, self.tr('The date the certificate was issued, i.e. signed and released')),
		}
		self._dates['db'].setAutoToolTip(self.tr('Set when the first calibration value is written'))
		self._dates['de'].setAutoToolTip(self.tr('Set whenever a calibration value is written'))
		self._dates['di'].setAutoToolTip(self.tr('Set when the certificate is saved to a file or written to the '
		                                         'device'))
		dates = QGroupBox(self.tr('Dates'))
		dates_form = QFormLayout(dates)
		dates_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		self._omit_dates = QCheckBox(self.tr('Do not include time/date info in the certificate'))
		self._omit_dates.setToolTip(self.tr('Leave the begin, end and issue dates out of the certificate, e.g. '
		                                    'for a reproducible one'))
		self._omit_dates.toggled.connect(self._omitDatesToggled)
		dates_form.addRow(self._omit_dates)
		for key, label in (('db', self.tr('Begin:')), ('de', self.tr('End:')), ('di', self.tr('Issue:'))):
			field = self._dates[key]
			field.edited.connect(lambda key=key: self._dateEdited(key))
			dates_form.addRow(label, field)

		self._remarks = QPlainTextEdit()
		self._remarks.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
		self._remarks.setPlaceholderText(self.tr('e.g. Calibration performed against references traceable to '
		                                         'national standards.'))
		self._remarks.setToolTip(self.tr('Free-form statements, remarks or declarations, e.g. the scope or the '
		                                 'conformance. Paragraphs separated by a blank line are separate '
		                                 'remarks.'))
		self._remarks.setMinimumHeight(self._remarks.fontMetrics().lineSpacing() * 6)
		self._remarks.textChanged.connect(self._remarksEdited)
		remarks = QGroupBox(self.tr('Remarks'))
		remarks_layout = QVBoxLayout(remarks)
		remarks_layout.addWidget(self._remarks)

		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(dates)
		layout.addWidget(remarks)
		layout.addStretch(1)
		self._updateGenerate()
		self._loadKnown()

	def setCertificate(self, certificate):
		self._certificate = certificate
		adm = certificate.section(self.KEY) or {}
		self._id.setText(format_scalar(adm['id']) if 'id' in adm else '')
		self._location.setText(format_scalar(adm['loc']) if 'loc' in adm else '')
		self._remarks.blockSignals(True)
		self._remarks.setPlainText(join_remarks(adm.get('rem')))
		self._remarks.blockSignals(False)
		self._showParties()
		self._showPersons()
		for key, field in self._dates.items():
			field.setValue(adm.get(key))
		# Unless the certificate has some, the dates stay left out if they were.
		if any(key in adm for key in self._dates):
			self._omit_dates.setChecked(False)
		elif self._omit_dates.isChecked():
			self._omitDatesToggled(True)

	@Slot()
	def _loadKnown(self):
		"""Offer the companies and the persons in the settings, keeping the person chosen to add."""
		index = self._person_combo.currentIndex()
		chosen = self._persons[index] if 0 <= index < len(self._persons) else None
		self._parties = CalibrationCompaniesPage.entries(self._settings)
		self._persons = CalibrationPersonsPage.entries(self._settings)
		self._showParties()
		self._showPersons()
		if chosen in self._persons:
			self._person_combo.setCurrentIndex(self._persons.index(chosen))

	def _adm(self):
		return (self._certificate.section(self.KEY) if self._certificate is not None else None) or {}

	def _name(self, entry):
		return str(entry.get('n') or self.tr('Unnamed'))

	def _showParties(self):
		"""Fill the party choosers with the copy in the certificate first if it is not one of the known
		parties, nobody and the known parties, and select the one of the certificate."""
		for key, combo in self._party_combos.items():
			current = self._adm().get(key)
			if not isinstance(current, dict):
				current = None
			items = [None] + self._parties
			if current is not None and current not in self._parties:
				items.insert(0, current)
			self._party_items[key] = items
			combo.clear()
			for party in items:
				if party is None:
					combo.addItem(self.tr('None'))
					continue
				name = self._name(party)
				if party is current and current not in self._parties:
					name = self.tr('%s (certificate custom)') % name
				combo.addItem(name)
				combo.setItemData(combo.count() - 1, str(party.get('addr', '')), Qt.ToolTipRole)
			combo.setCurrentIndex(items.index(current))

	def _partyChosen(self, key, index):
		if self._certificate is not None:
			self._certificate.setField(self.KEY, key, copy.deepcopy(self._party_items[key][index]))
			self._showParties()
			self.changed.emit()

	def _responsible(self):
		"""The responsible persons of the certificate, the entries that are not maps left out."""
		rsp = self._adm().get('rsp')
		return [person for person in rsp if isinstance(person, dict)] if isinstance(rsp, list) else []

	def _showPersons(self):
		"""Show a row for each responsible person of the certificate, and offer the known persons to add."""
		while self._persons_rows.count():
			row = self._persons_rows.takeAt(0).widget()
			# Not in the layout any more, it would show over the others until deleted.
			row.hide()
			row.deleteLater()
		responsible = self._responsible()
		if not responsible:
			none = QLabel(self.tr('None'))
			none.setStyleSheet(f'color: {SectionView.HINT_COLOR};')
			# As tall as a row of a person, in line with the label of the field.
			none.setFixedHeight(self._generate.sizeHint().height())
			self._persons_rows.addWidget(none)
		for index, person in enumerate(responsible):
			row = QWidget()
			layout = QHBoxLayout(row)
			layout.setContentsMargins(0, 0, 0, 0)
			layout.setSpacing(4)
			name = QLabel(self._name(person))
			name.setToolTip(str(person.get('role', '')))
			remove = DauiButton(btn_icon=Icons.remove())
			remove.setToolTip(self.tr('Remove the person from the responsible persons'))
			remove.clicked.connect(lambda checked=False, index=index: self._removePerson(index))
			layout.addWidget(name, stretch=1)
			layout.addWidget(remove)
			self._persons_rows.addWidget(row)

		self._person_combo.clear()
		for person in self._persons:
			self._person_combo.addItem(self._name(person))
			self._person_combo.setItemData(self._person_combo.count() - 1, str(person.get('role', '')),
			                               Qt.ToolTipRole)
		self._updateAddPerson()
		# The rows change the size of the view, the scroll area it is in does not learn it otherwise. Once the
		# view is laid out again and the rows removed are deleted, the scroll area asks for its height then.
		QTimer.singleShot(0, self.updateGeometry)

	@Slot()
	def _updateAddPerson(self):
		index = self._person_combo.currentIndex()
		self._add_person.setEnabled(self._certificate is not None and 0 <= index < len(self._persons)
		                            and self._persons[index] not in self._responsible())

	def _setResponsible(self, responsible):
		self._certificate.setField(self.KEY, 'rsp', responsible or None)
		self._showPersons()
		self.changed.emit()

	@Slot()
	def _addPerson(self):
		index = self._person_combo.currentIndex()
		if self._certificate is not None and 0 <= index < len(self._persons):
			self._setResponsible(self._responsible() + [copy.deepcopy(self._persons[index])])

	def _removePerson(self, index):
		if self._certificate is not None:
			responsible = self._responsible()
			del responsible[index]
			self._setResponsible(responsible)

	def valueWritten(self):
		"""Assign the automatic dates when a calibration value is written: the begin if not set yet, the
		end."""
		if self._omit_dates.isChecked():
			return
		adm = self._certificate.section(self.KEY) or {}
		if self._dates['db'].automatic and 'db' not in adm:
			self._dates['db'].assignNow()
		if self._dates['de'].automatic:
			self._dates['de'].assignNow()

	def issued(self):
		"""Assign the automatic issue date when the certificate is saved."""
		if self._dates['di'].automatic and not self._omit_dates.isChecked():
			self._dates['di'].assignNow()

	@Slot(str)
	def _locationEdited(self, text):
		if self._certificate is not None:
			self._certificate.setField(self.KEY, 'loc', text.strip())
			self.changed.emit()

	@Slot()
	def _remarksEdited(self):
		if self._certificate is not None:
			self._certificate.setField(self.KEY, 'rem', split_remarks(self._remarks.toPlainText()) or None)
			self.changed.emit()

	@Slot(bool)
	def _omitDatesToggled(self, omitted):
		for field in self._dates.values():
			field.setOmitted(omitted)
		if self._certificate is None:
			return
		before = dict(self._certificate.section(self.KEY) or {})
		for key, field in self._dates.items():
			self._certificate.setField(self.KEY, key, None if omitted else field.value())
		if (self._certificate.section(self.KEY) or {}) != before:
			self.changed.emit()

	def _dateEdited(self, key):
		if self._certificate is not None and not self._omit_dates.isChecked():
			self._certificate.setField(self.KEY, key, self._dates[key].value())
			self.changed.emit()

	@Slot()
	def _updateGenerate(self):
		self._generate.setEnabled(ID_KINDS[self._id_kind.currentIndex()] != ID_CUSTOM)

	@Slot()
	def _generateId(self):
		value = generate_id(ID_KINDS[self._id_kind.currentIndex()])
		if value is not None:
			self._id.setText(value)
			self._idEdited(value)

	@Slot(str)
	def _idEdited(self, text):
		if self._certificate is not None:
			self._certificate.setField(self.KEY, 'id', text.strip())
			self.changed.emit()


class _TargetTree(StyledTreeWidget):
	"""The tree of the targets, rearranged by dragging them. ``moved`` tells a target was dropped."""

	moved = Signal()

	COLUMN_RATIOS = (3, 2, 1, 2)

	def __init__(self):
		super().__init__()
		self.setSelectionMode(QAbstractItemView.SingleSelection)
		self.setDragDropMode(QAbstractItemView.InternalMove)
		self.setDefaultDropAction(Qt.MoveAction)

	def dropEvent(self, event):
		super().dropEvent(event)
		self.moved.emit()


class TargetsView(QWidget):
	"""The Targets tab, the calibration targets (``tgt``) of the certificate in a tree, a target below its
	parent (its ``p``). ``changed`` tells the user changed the certificate.

	The targets are added and edited in a group below the tree, shown while adding or editing one. The
	identifier of a target is generated from its type if left empty, renaming it renames it in its children and
	in the results too. A target is moved among its siblings, indented below the sibling before it and unindented
	after its parent with the buttons, or dragged anywhere. The certificate keeps the targets in the order of the
	tree, a parent before its children.
	"""

	changed = Signal()

	KEY = 'tgt'

	#: The columns of the tree as (key, title).
	COLUMNS = (('n', 'Name'), ('pn', 'Part number'), ('t', 'Type'), ('sn', 'Serial number'))

	#: The text fields of the form as (key, label, placeholder), the type follows the name.
	TEXT_FIELDS = (
		('n', 'Name:', 'e.g. Channel 0 ADC'),
		('mfg', 'Manufacturer:', 'manufacturer of the target'),
		('pn', 'Part number:', 'model designation, part number or product name'),
		('sn', 'Serial number:', 'serial number of the target'),
	)

	#: The keys of a target edited in the form, in the order the EP lists them.
	EDITED = ('n', 't', 'mfg', 'pn', 'sn', 'mq', 'mu', 'iq', 'iu')

	def __init__(self):
		super().__init__()
		self._certificate = None
		# The identifier of the target being edited, None when adding one.
		self._editing = None

		self._list = _TargetTree()
		self._list.setHeaderLabels([self.tr(title) for key, title in self.COLUMNS])
		self._list.currentItemChanged.connect(lambda current, previous: self._updateButtons())
		self._list.itemDoubleClicked.connect(lambda item, column: self._edit())
		self._list.moved.connect(self._dropped)

		self._add = DauiButton(self.tr('Add'), Icons.add())
		self._add.setToolTip(self.tr('Add a target after the selected one'))
		self._add.clicked.connect(self._startAdd)
		self._edit_btn = DauiButton(self.tr('Edit'), Icons.edit())
		self._edit_btn.setToolTip(self.tr('Edit the selected target'))
		self._edit_btn.clicked.connect(self._edit)
		self._remove = DauiButton(self.tr('Remove'), Icons.remove())
		self._remove.setToolTip(self.tr('Remove the selected target with its parts'))
		self._remove.clicked.connect(self._removeSelected)
		self._indent = DauiButton(btn_icon=Icons.indent())
		self._indent.setToolTip(self.tr('Make the selected target a part of the target above it'))
		self._indent.clicked.connect(self._indentSelected)
		self._unindent = DauiButton(btn_icon=Icons.unindent())
		self._unindent.setToolTip(self.tr('Move the selected target out of its parent, after it'))
		self._unindent.clicked.connect(self._unindentSelected)
		self._up = DauiButton(btn_icon=Icons.up())
		self._up.setToolTip(self.tr('Move the selected target up among its siblings'))
		self._up.clicked.connect(lambda: self._moveSelected(-1))
		self._down = DauiButton(btn_icon=Icons.down())
		self._down.setToolTip(self.tr('Move the selected target down among its siblings'))
		self._down.clicked.connect(lambda: self._moveSelected(1))
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		for button in (self._add, self._edit_btn, self._remove):
			buttons.addWidget(button)
		buttons.addStretch(1)
		for button in (self._indent, self._unindent, self._up, self._down):
			buttons.addWidget(button)
		button_row = QWidget()
		button_row.setLayout(buttons)

		self._form_group = self._editGroup()
		polish_forms(self._form_group)

		layout = QVBoxLayout(self)
		layout.addWidget(SettingsGroup(self.tr('Targets'), self._list, button_row))
		layout.addWidget(self._form_group)
		layout.addStretch(1)
		self._closeEdit()

	def _editGroup(self):
		"""The group adding or editing a target, hidden until Add or Edit is clicked."""
		fields = form_layout()
		fields.setContentsMargins(0, 0, 0, 0)
		field_rows = QWidget()
		field_rows.setLayout(fields)

		self._confirm = DauiButton(self.tr('Add'), Icons.confirm())
		self._confirm.setDefault(True)
		self._confirm.clicked.connect(self._confirmEdit)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self._closeEdit)

		self._id = QLineEdit()
		self._id.setPlaceholderText(self.tr('generated from the type if left empty, e.g. adc0'))
		self._id.setToolTip(self.tr('The identifier of the target, unique within the certificate, the results '
		                            'refer to the target by it'))
		self._id.textChanged.connect(self._updateConfirm)
		self._id.returnPressed.connect(self._confirm.click)
		fields.addRow(WrappedLabel(self.tr('Identifier:')), self._id)

		# The edit widgets of the text fields and the type by their keys.
		self._fields = {}
		for key, label, placeholder in self.TEXT_FIELDS:
			widget = QLineEdit()
			widget.setPlaceholderText(self.tr(placeholder))
			# Enter confirms from the edit boxes.
			widget.returnPressed.connect(self._confirm.click)
			self._fields[key] = widget
			fields.addRow(WrappedLabel(self.tr(label)), widget)
			if key == 'n':
				kind = QComboBox()
				kind.setEditable(True)
				kind.setInsertPolicy(QComboBox.NoInsert)
				kind.addItems([''] + list(TARGET_TYPES))
				kind.lineEdit().setPlaceholderText(self.tr('kind of target, picked or typed'))
				self._fields['t'] = kind
				fields.addRow(WrappedLabel(self.tr('Type:')), kind)

		self._measurand = QuantitySelector()
		self._measurand.setToolTip(self.tr('The physical quantity the target measures and its unit, e.g. '
		                                   'pressure in kPa'))
		fields.addRow(WrappedLabel(self.tr('Measurand:')), self._measurand)
		self._indication = QuantitySelector()
		self._indication.setToolTip(self.tr('The quantity the target outputs and its unit, e.g. resistance in Ω, '
		                                    'or the code of an ADC'))
		fields.addRow(WrappedLabel(self.tr('Indication:')), self._indication)

		# Escape cancels.
		QShortcut(QKeySequence(Qt.Key_Escape), field_rows, cancel.click, context=Qt.WidgetWithChildrenShortcut)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addStretch(1)
		buttons.addWidget(self._confirm)
		buttons.addWidget(cancel)
		button_row = QWidget()
		button_row.setLayout(buttons)

		return SettingsGroup('', field_rows, button_row)

	def setCertificate(self, certificate):
		self._certificate = certificate
		self._closeEdit()
		self._load()

	def _targets(self):
		"""The targets of the certificate by their identifiers."""
		tgt = self._certificate.section(self.KEY) if self._certificate is not None else None
		return dict(tgt) if isinstance(tgt, dict) else {}

	def _load(self, select=None):
		"""Show the targets in the tree, the target ``select`` selected."""
		# Telling the current item changed while it is deleted would ask the deleted item.
		self._list.blockSignals(True)
		self._list.clear()
		targets = {key: target for key, target in self._targets().items() if isinstance(target, dict)}
		items = {}
		for key, target in targets.items():
			values = [format_scalar(target[column]) if column in target else '' for column, title in self.COLUMNS]
			# Without a name, its identifier stands for it.
			values[0] = values[0] or format_scalar(key)
			item = QTreeWidgetItem(values)
			item.setData(0, Qt.UserRole, key)
			item.setToolTip(0, self.tr('Identifier: %s') % format_scalar(key))
			self._list.styleName(item)
			items[key] = item
		for key, item in items.items():
			parent = target_parent(targets, key)
			if parent is None:
				self._list.addTopLevelItem(item)
			else:
				items[parent].addChild(item)
		self._list.expandAll()
		if select in items:
			self._list.setCurrentItem(items[select])
		self._list.blockSignals(False)
		self._updateButtons()

	def _store(self, targets=None, select=None):
		"""Put the targets in the certificate in the order of the tree, each with its parent in the tree.
		``targets`` are the targets by their identifiers if they changed, the ones not shown are kept, the items
		of the targets not in them are left out with their children."""
		old = self._targets() if targets is None else targets
		new = {}

		def walk(item, parent):
			key = item.data(0, Qt.UserRole)
			if key not in old:
				return
			target = dict(old[key])
			if parent is not None:
				target['p'] = parent
			elif target_parent(old, key) is not None:
				target.pop('p', None)
			new[key] = target
			for index in range(item.childCount()):
				walk(item.child(index), key)

		for index in range(self._list.topLevelItemCount()):
			walk(self._list.topLevelItem(index), None)
		# The entries which are not maps.
		for key, target in old.items():
			new.setdefault(key, target)
		self._certificate.setSection(self.KEY, new)
		self._load(select)
		self.changed.emit()

	def _selected(self):
		"""The selected item with its parent (``None`` at the top level), its index among its siblings and the
		number of them. ``None`` if there is none."""
		item = self._list.currentItem()
		if item is None:
			return None
		parent = item.parent()
		if parent is None:
			return item, None, self._list.indexOfTopLevelItem(item), self._list.topLevelItemCount()
		return item, parent, parent.indexOfChild(item), parent.childCount()

	def _take(self, item):
		parent = item.parent()
		self._list.blockSignals(True)
		if parent is None:
			self._list.takeTopLevelItem(self._list.indexOfTopLevelItem(item))
		else:
			parent.takeChild(parent.indexOfChild(item))
		self._list.blockSignals(False)

	def _insert(self, parent, index, item):
		if parent is None:
			self._list.insertTopLevelItem(index, item)
		else:
			parent.insertChild(index, item)

	def _item(self, key):
		"""The item of the target ``key``, ``None`` if there is none."""
		iterator = QTreeWidgetItemIterator(self._list)
		while iterator.value() is not None:
			if iterator.value().data(0, Qt.UserRole) == key:
				return iterator.value()
			iterator += 1
		return None

	def _currentKey(self):
		"""The identifier of the selected target, ``None`` if there is none."""
		item = self._list.currentItem()
		return item.data(0, Qt.UserRole) if item is not None else None

	def _rearrange(self, fn):
		"""Rearrange the tree with ``fn()``, returning the identifier of the target to select once stored or
		``None`` if it did not change anything, and store it.

		The items rearranged are let go before the tree is shown again: a Python object kept of a deleted item
		would stand for the new item made at its address, deleting it twice.
		"""
		if self._form_group.isVisible():
			return
		key = fn()
		if key is not None:
			self._store(select=key)

	@Slot()
	def _dropped(self):
		self._store(select=self._currentKey())

	def _moveSelected(self, step):
		def move():
			item, parent, index, count = self._selected()
			if 0 <= index + step < count:
				self._take(item)
				self._insert(parent, index + step, item)
				return item.data(0, Qt.UserRole)

		if self._selected() is not None:
			self._rearrange(move)

	@Slot()
	def _indentSelected(self):
		def indent():
			item, parent, index, count = self._selected()
			if index > 0:
				above = parent.child(index - 1) if parent is not None else self._list.topLevelItem(index - 1)
				self._take(item)
				above.addChild(item)
				return item.data(0, Qt.UserRole)

		if self._selected() is not None:
			self._rearrange(indent)

	@Slot()
	def _unindentSelected(self):
		def unindent():
			item, parent, index, count = self._selected()
			if parent is not None:
				grandparent = parent.parent()
				after = (grandparent.indexOfChild(parent) if grandparent is not None
				         else self._list.indexOfTopLevelItem(parent)) + 1
				self._take(item)
				self._insert(grandparent, after, item)
				return item.data(0, Qt.UserRole)

		if self._selected() is not None:
			self._rearrange(unindent)

	def _generateId(self, kind):
		"""A new identifier of a target of the ``kind``, e.g. ``adc0``, not one of the targets."""
		base = ''.join(c for c in (kind or '').lower() if c.isalnum()) or 'tgt'
		targets = self._targets()
		index = 0
		while f'{base}{index}' in targets:
			index += 1
		return f'{base}{index}'

	def _typedId(self):
		"""The identifier typed, the identifier of the target being edited if it was not changed, ``None`` if
		it is empty."""
		text = self._id.text().strip()
		if self._editing is not None and text == format_scalar(self._editing):
			return self._editing
		return text or None

	@Slot()
	def _startAdd(self):
		self._openEdit(None, self.tr('Add target'), self.tr('Add'), {})

	@Slot()
	def _edit(self):
		item = self._list.currentItem()
		if item is None or self._form_group.isVisible():
			return
		key = item.data(0, Qt.UserRole)
		self._openEdit(key, self.tr('Edit target'), self.tr('Confirm'), self._targets()[key])

	def _openEdit(self, key, title, confirm, target):
		self._editing = key
		self._form_group.setTitle(title)
		self._confirm.setText(confirm)
		self._id.setText(format_scalar(key) if key is not None else '')
		for field, widget in self._fields.items():
			value = format_scalar(target[field]) if field in target else ''
			if isinstance(widget, QComboBox):
				widget.setCurrentText(value)
			else:
				widget.setText(value)
		self._measurand.setValue(target.get('mq'), target.get('mu'))
		self._indication.setValue(target.get('iq'), target.get('iu'))
		self._updateConfirm()
		self._form_group.show()
		self._fields['n'].setFocus()
		self._updateButtons()

	def _entry(self):
		"""The edited fields of the target, without the empty ones."""
		values = {key: (widget.currentText() if isinstance(widget, QComboBox) else widget.text()).strip()
		          for key, widget in self._fields.items()}
		values.update(mq=self._measurand.quantity(), mu=self._measurand.unit(),
		              iq=self._indication.quantity(), iu=self._indication.unit())
		return {key: values[key] for key in self.EDITED if values[key]}

	@Slot()
	def _confirmEdit(self):
		if not self._confirm.isEnabled() or self._certificate is None:
			return
		targets = self._targets()
		entry = self._entry()
		key = self._typedId() or self._generateId(entry.get('t'))
		if self._editing is None:
			# A sibling right after the selected target, the siblings are shown in the order of the targets.
			after = self._currentKey()
			if after is not None and target_parent(targets, after) is not None:
				entry = {'p': targets[after]['p'], **entry}
			added = {}
			for k, v in targets.items():
				added[k] = v
				if k == after:
					added[key] = entry
			added.setdefault(key, entry)
			self._closeEdit()
			self._certificate.setSection(self.KEY, added)
			# Shown, then stored in the order of the tree.
			self._load()
			self._store(select=key)
			return
		else:
			# The keys not edited here, e.g. the parent or added by a newer version, are kept.
			target = {k: v for k, v in targets[self._editing].items() if k not in self.EDITED}
			target.update(entry)
			# Renamed in place, the children follow the item, the results refer to it by the new name.
			targets = {(key if k == self._editing else k): (target if k == self._editing else v)
			           for k, v in targets.items()}
			if key != self._editing:
				self._item(self._editing).setData(0, Qt.UserRole, key)
				results = self._certificate.section('res')
				for group in results if isinstance(results, list) else []:
					if isinstance(group, dict) and group.get('tgt') == self._editing:
						group['tgt'] = key
		self._closeEdit()
		self._store(targets, select=key)

	@Slot()
	def _removeSelected(self):
		if self._currentKey() is None or self._form_group.isVisible():
			return
		# No Python object of the item is kept while the tree is shown again, see _rearrange().
		keys = self._subtree(self._list.currentItem())
		name = self._list.currentItem().text(0)
		question = self.tr('Remove the target %s?') % name
		if len(keys) > 1:
			question = self.tr('Remove the target %s with its %d parts?') % (name, len(keys) - 1)
		if QMessageBox.question(self, self.tr('Remove target'), question) != QMessageBox.Yes:
			return
		self._store({key: target for key, target in self._targets().items() if key not in keys})

	@classmethod
	def _subtree(cls, item):
		"""The identifiers of the target of ``item`` and of all its parts."""
		keys = [item.data(0, Qt.UserRole)]
		for index in range(item.childCount()):
			keys += cls._subtree(item.child(index))
		return keys

	@Slot()
	def _closeEdit(self):
		self._editing = None
		self._form_group.hide()
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		editing = self._form_group.isVisibleTo(self)
		selected = self._selected() if not editing else None
		self._add.setEnabled(not editing and self._certificate is not None)
		self._edit_btn.setEnabled(selected is not None)
		self._remove.setEnabled(selected is not None)
		self._up.setEnabled(selected is not None and selected[2] > 0)
		self._down.setEnabled(selected is not None and selected[2] < selected[3] - 1)
		self._indent.setEnabled(selected is not None and selected[2] > 0)
		self._unindent.setEnabled(selected is not None and selected[1] is not None)
		# The target being edited is found by its identifier, the tree stays as it is meanwhile.
		self._list.setDragEnabled(not editing)

	@Slot()
	def _updateConfirm(self):
		key = self._typedId()
		taken = key is not None and key != self._editing and key in self._targets()
		self._confirm.setEnabled(not taken)
		self._confirm.setToolTip(self.tr('Another target has the identifier %s') % key if taken else '')


class _IdentifiedView(QWidget):
	"""Base class of a tab of a top-level section of the certificate which is a map of maps by identifiers, e.g.
	the configurations, in a list. ``changed`` tells the user changed the certificate.

	The entries are added and edited in a group below the list, shown while adding or editing one. They are
	kept sorted by their identifiers (see :func:`natural_key`), an added or renamed one goes where its
	identifier belongs. The identifier is generated if left empty, renaming an entry renames it in the results
	too (see :meth:`_renamed`). A subclass adds its fields to the group and shows its entries in the list.
	"""

	changed = Signal()

	#: The key of the section.
	KEY = ''
	#: The title of the list.
	TITLE = ''
	#: The text of an entry, used in the titles and the questions, e.g. ``'configuration'``.
	ENTRY = ''
	#: The generated identifiers are this followed by a number, e.g. ``cfg0``.
	ID_PREFIX = ''
	#: The columns of the list and their relative widths.
	HEADERS = ()
	COLUMN_RATIOS = None

	def __init__(self):
		super().__init__()
		self._certificate = None
		# The identifier of the entry being edited, None when adding one.
		self._editing = None

		self._list = StyledTreeWidget()
		self._list.COLUMN_RATIOS = self.COLUMN_RATIOS
		self._list.setHeaderLabels([self.tr(title) for title in self.HEADERS])
		self._list.currentItemChanged.connect(lambda current, previous: self._updateButtons())
		self._list.itemDoubleClicked.connect(lambda item, column: self._edit())

		self._add = DauiButton(self.tr('Add'), Icons.add())
		self._add.setToolTip(self.tr('Add a %s') % self.tr(self.ENTRY))
		self._add.clicked.connect(self._startAdd)
		self._edit_btn = DauiButton(self.tr('Edit'), Icons.edit())
		self._edit_btn.setToolTip(self.tr('Edit the selected %s') % self.tr(self.ENTRY))
		self._edit_btn.clicked.connect(self._edit)
		self._remove = DauiButton(self.tr('Remove'), Icons.remove())
		self._remove.setToolTip(self.tr('Remove the selected %s') % self.tr(self.ENTRY))
		self._remove.clicked.connect(self._removeSelected)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		for button in (self._add, self._edit_btn, self._remove):
			buttons.addWidget(button)
		buttons.addStretch(1)
		button_row = QWidget()
		button_row.setLayout(buttons)

		self._form_group = self._editGroup()
		polish_forms(self._form_group)

		layout = QVBoxLayout(self)
		layout.addWidget(SettingsGroup(self.tr(self.TITLE), self._list, button_row))
		layout.addWidget(self._form_group)
		layout.addStretch(1)
		self._closeEdit()

	def _editGroup(self):
		"""The group adding or editing an entry, hidden until Add or Edit is clicked."""
		fields = form_layout()
		fields.setContentsMargins(0, 0, 0, 0)
		field_rows = QWidget()
		field_rows.setLayout(fields)

		self._confirm = DauiButton(self.tr('Add'), Icons.confirm())
		self._confirm.setDefault(True)
		self._confirm.clicked.connect(self._confirmEdit)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self._closeEdit)

		self._id = QLineEdit()
		self._id.setPlaceholderText(self.tr('generated if left empty, e.g. %s0') % self.ID_PREFIX)
		self._id.setToolTip(self.tr('The identifier of the %s, unique within the certificate, the results refer '
		                            'to the %s by it') % (self.tr(self.ENTRY), self.tr(self.ENTRY)))
		self._id.textChanged.connect(self._updateConfirm)
		self._id.returnPressed.connect(self._confirm.click)
		fields.addRow(WrappedLabel(self.tr('Identifier:')), self._id)
		self._addFields(fields)

		# Escape cancels.
		QShortcut(QKeySequence(Qt.Key_Escape), field_rows, cancel.click, context=Qt.WidgetWithChildrenShortcut)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addStretch(1)
		buttons.addWidget(self._confirm)
		buttons.addWidget(cancel)
		button_row = QWidget()
		button_row.setLayout(buttons)

		return SettingsGroup('', field_rows, button_row)

	def _addFields(self, fields):
		"""Add the rows of the fields of an entry below the identifier to the form layout ``fields``."""

	def _showEntry(self, entry):
		"""Show the ``entry`` in the fields, an empty map when adding one."""

	def _entry(self):
		"""The edited entry, ``None`` if it is not valid."""
		return {}

	def _invalidReason(self):
		"""Why the edited entry is not valid, shown on the confirm button."""
		return ''

	def _merge(self, old, entry):
		"""The edited ``entry`` of the entry ``old`` being edited."""
		return entry

	def _item(self, key, entry):
		"""The top-level item of the list showing the ``entry`` of the identifier ``key``."""
		return QTreeWidgetItem([format_scalar(key)])

	def _renamed(self, results, old, new):
		"""Refer to the entry renamed from ``old`` to ``new`` in the result group map ``results``."""

	def setCertificate(self, certificate):
		self._certificate = certificate
		self._closeEdit()
		self._load()

	def _entries(self):
		"""The entries of the certificate by their identifiers."""
		section = self._certificate.section(self.KEY) if self._certificate is not None else None
		return dict(section) if isinstance(section, dict) else {}

	def _load(self, select=None):
		"""Show the entries which are maps in the list, the entry ``select`` selected. Each item carries the
		identifier of its entry, its children too."""
		# Telling the current item changed while it is deleted would ask the deleted item.
		self._list.blockSignals(True)
		self._list.clear()
		for key, entry in self._entries().items():
			if not isinstance(entry, dict):
				continue
			item = self._item(key, entry)
			self._list.styleName(item)
			self._setKey(item, key)
			self._list.addTopLevelItem(item)
			if key == select:
				self._list.setCurrentItem(item)
		self._list.expandAll()
		self._list.blockSignals(False)
		self._updateButtons()

	@classmethod
	def _setKey(cls, item, key):
		"""Let the ``item`` and its children carry the identifier ``key``."""
		item.setData(0, Qt.UserRole, key)
		for index in range(item.childCount()):
			cls._setKey(item.child(index), key)

	def _store(self, entries, select=None):
		"""Put the ``entries`` in the certificate sorted by their identifiers and show them."""
		ordered = dict(sorted(entries.items(), key=lambda pair: natural_key(pair[0])))
		self._certificate.setSection(self.KEY, ordered)
		self._load(select)
		self.changed.emit()

	def _currentKey(self):
		"""The identifier of the entry of the selected item, ``None`` if there is none."""
		item = self._list.currentItem()
		return item.data(0, Qt.UserRole) if item is not None else None

	def _generateId(self):
		"""A new identifier, e.g. ``cfg0``, not one of the entries."""
		entries = self._entries()
		index = 0
		while f'{self.ID_PREFIX}{index}' in entries:
			index += 1
		return f'{self.ID_PREFIX}{index}'

	def _typedId(self):
		"""The identifier typed, the identifier of the entry being edited if it was not changed, ``None`` if
		it is empty."""
		text = self._id.text().strip()
		if self._editing is not None and text == format_scalar(self._editing):
			return self._editing
		return text or None

	@Slot()
	def _startAdd(self):
		self._openEdit(None, self.tr('Add %s') % self.tr(self.ENTRY), self.tr('Add'), {})

	@Slot()
	def _edit(self):
		key = self._currentKey()
		if key is None or self._form_group.isVisible():
			return
		self._openEdit(key, self.tr('Edit %s') % self.tr(self.ENTRY), self.tr('Confirm'), self._entries()[key])

	def _openEdit(self, key, title, confirm, entry):
		self._editing = key
		self._form_group.setTitle(title)
		self._confirm.setText(confirm)
		self._id.setText(format_scalar(key) if key is not None else '')
		self._showEntry(entry)
		self._updateConfirm()
		self._form_group.show()
		self._id.setFocus()
		self._updateButtons()

	@Slot()
	def _confirmEdit(self):
		if not self._confirm.isEnabled() or self._certificate is None:
			return
		entries = self._entries()
		entry = self._entry()
		key = self._typedId() or self._generateId()
		if self._editing is not None:
			entry = self._merge(entries.pop(self._editing), entry)
			if key != self._editing:
				results = self._certificate.section('res')
				for group in results if isinstance(results, list) else []:
					if isinstance(group, dict):
						self._renamed(group, self._editing, key)
		entries[key] = entry
		self._closeEdit()
		self._store(entries, select=key)

	@Slot()
	def _removeSelected(self):
		key = self._currentKey()
		if key is None or self._form_group.isVisible():
			return
		question = self.tr('Remove the %s %s?') % (self.tr(self.ENTRY), format_scalar(key))
		if QMessageBox.question(self, self.tr('Remove %s') % self.tr(self.ENTRY), question) != QMessageBox.Yes:
			return
		entries = self._entries()
		del entries[key]
		self._store(entries)

	@Slot()
	def _closeEdit(self):
		self._editing = None
		self._form_group.hide()
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		editing = self._form_group.isVisibleTo(self)
		selected = not editing and self._currentKey() is not None
		self._add.setEnabled(not editing and self._certificate is not None)
		self._edit_btn.setEnabled(selected)
		self._remove.setEnabled(selected)

	@Slot()
	def _updateConfirm(self):
		key = self._typedId()
		taken = key is not None and key != self._editing and key in self._entries()
		invalid = self._entry() is None
		self._confirm.setEnabled(not taken and not invalid)
		if taken:
			self._confirm.setToolTip(self.tr('Another %s has the identifier %s') % (self.tr(self.ENTRY), key))
		else:
			self._confirm.setToolTip(self._invalidReason() if invalid else '')


class ConfigurationsView(_IdentifiedView):
	"""The Configurations tab, the device configurations (``cfg``) of the certificate in a two-level tree: the
	configurations by their identifiers, their settings with the values below them. The settings are edited in
	a table, see :class:`_IdentifiedView`.
	"""

	KEY = 'cfg'
	TITLE = 'Configurations'
	ENTRY = 'configuration'
	ID_PREFIX = 'cfg'
	HEADERS = ('Configuration', 'Value')
	COLUMN_RATIOS = (1, 1)

	#: The role of a cell keeping what it was set to, and of whether it was set to anything.
	ORIGINAL_ROLE = Qt.UserRole
	HAS_ORIGINAL_ROLE = Qt.UserRole + 1

	def _addFields(self, fields):
		self._settings = QTableWidget(0, 2)
		self._settings.setHorizontalHeaderLabels([self.tr('Setting'), self.tr('Value')])
		self._settings.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
		self._settings.verticalHeader().hide()
		self._settings.setSelectionBehavior(QAbstractItemView.SelectRows)
		self._settings.setSelectionMode(QAbstractItemView.SingleSelection)
		self._settings.setToolTip(self.tr('The device settings of the configuration, e.g. pga = 1. A value is a '
		                                  'number, true, false or a text.'))
		self._settings.setFixedHeight(self._settings.horizontalHeader().sizeHint().height()
		                              + self._settings.verticalHeader().defaultSectionSize() * 5 + 4)
		self._settings.itemChanged.connect(self._updateConfirm)
		self._settings.itemSelectionChanged.connect(self._updateSettingButtons)
		self._add_setting = DauiButton(self.tr('Add setting'), Icons.add())
		self._add_setting.clicked.connect(self._addSetting)
		self._remove_setting = DauiButton(self.tr('Remove setting'), Icons.remove())
		self._remove_setting.clicked.connect(self._removeSetting)
		setting_buttons = QHBoxLayout()
		setting_buttons.setContentsMargins(0, 0, 0, 0)
		setting_buttons.addWidget(self._add_setting)
		setting_buttons.addWidget(self._remove_setting)
		setting_buttons.addStretch(1)
		settings = QVBoxLayout()
		settings.setSpacing(4)
		settings.addWidget(self._settings)
		settings.addLayout(setting_buttons)
		fields.addRow(WrappedLabel(self.tr('Settings:')), settings)

	def _item(self, key, entry):
		item = QTreeWidgetItem([format_scalar(key)])
		for name, value in entry.items():
			QTreeWidgetItem(item, [format_scalar(name), format_scalar(value)])
		return item

	def _renamed(self, results, old, new):
		if results.get('cfg') == old:
			results['cfg'] = new

	def _showEntry(self, entry):
		self._settings.blockSignals(True)
		self._settings.setRowCount(0)
		for name, value in entry.items():
			self._appendSetting(name, value)
		self._settings.blockSignals(False)
		if not entry:
			self._addSetting()
		self._updateSettingButtons()

	def _appendSetting(self, *setting):
		"""Add a row of the ``setting`` given as its name and value, an empty one if not given. Each cell keeps
		what it was set to, a setting left as it was keeps its name and value, e.g. a value of a type which
		cannot be typed."""
		row = self._settings.rowCount()
		self._settings.insertRow(row)
		for column in range(2):
			cell = QTableWidgetItem(format_scalar(setting[column]) if setting else '')
			if setting:
				cell.setData(self.ORIGINAL_ROLE, setting[column])
				cell.setData(self.HAS_ORIGINAL_ROLE, True)
			self._settings.setItem(row, column, cell)

	@Slot()
	def _addSetting(self):
		self._settings.blockSignals(True)
		self._appendSetting()
		self._settings.blockSignals(False)
		row = self._settings.rowCount() - 1
		self._settings.setCurrentCell(row, 0)
		self._settings.editItem(self._settings.item(row, 0))
		self._updateConfirm()

	@Slot()
	def _removeSetting(self):
		row = self._settings.currentRow()
		if row >= 0:
			self._settings.removeRow(row)
			self._updateConfirm()
			self._updateSettingButtons()

	@Slot()
	def _updateSettingButtons(self):
		self._remove_setting.setEnabled(self._settings.currentRow() >= 0)

	def _cell(self, row, column, parse):
		"""The value of the cell, what it was set to if it was not edited, else the text, parsed with ``parse``
		if given."""
		cell = self._settings.item(row, column)
		original = cell.data(self.ORIGINAL_ROLE)
		if cell.data(self.HAS_ORIGINAL_ROLE) and cell.text() == format_scalar(original):
			return original
		return parse(cell.text()) if parse is not None else cell.text().strip()

	def _entry(self):
		"""The settings of the table as a map, the empty rows left out. ``None`` if a setting has no name or
		two have the same one."""
		settings = {}
		for row in range(self._settings.rowCount()):
			name = self._cell(row, 0, None)
			value_text = self._settings.item(row, 1).text().strip()
			if name in (None, '') and not value_text:
				continue
			if name in (None, '') or name in settings:
				return None
			settings[name] = self._cell(row, 1, parse_setting)
		return settings

	def _invalidReason(self):
		return self.tr('Each setting needs a name of its own')


class EquipmentView(_IdentifiedView):
	"""The Equipment tab, the measuring equipment and reference standards (``meq``) of the certificate in a
	list, see :class:`_IdentifiedView`."""

	KEY = 'meq'
	TITLE = 'Measuring equipment'
	ENTRY = 'equipment'
	ID_PREFIX = 'meq'
	HEADERS = ('Identifier', 'Name', 'Traceability')
	COLUMN_RATIOS = (1, 2, 2)

	#: The fields of the form as (key, label, placeholder, tool tip).
	FIELDS = (
		('n', 'Name:', 'e.g. Reference DMM', 'The name of the standard or instrument'),
		('trace', 'Traceability:', 'e.g. CAL-2025-009921',
		 'The traceability reference, e.g. the identifier of the calibration certificate of the standard'),
	)

	def __init__(self):
		super().__init__()
		self._list.setRootIsDecorated(False)

	def _addFields(self, fields):
		# The edit boxes by the field keys.
		self._fields = {}
		for key, label, placeholder, tool_tip in self.FIELDS:
			edit = QLineEdit()
			edit.setPlaceholderText(self.tr(placeholder))
			edit.setToolTip(self.tr(tool_tip))
			# Enter confirms from the edit boxes.
			edit.returnPressed.connect(self._confirm.click)
			self._fields[key] = edit
			fields.addRow(WrappedLabel(self.tr(label)), edit)

	def _item(self, key, entry):
		return QTreeWidgetItem([format_scalar(key)] + [format_scalar(entry[field]) if field in entry else ''
		                                                for field, label, placeholder, tool_tip in self.FIELDS])

	def _renamed(self, results, old, new):
		equipment = results.get('meq')
		if isinstance(equipment, list):
			results['meq'] = [new if key == old else key for key in equipment]

	def _showEntry(self, entry):
		for key, edit in self._fields.items():
			edit.setText(format_scalar(entry[key]) if key in entry else '')

	def _entry(self):
		return {key: edit.text().strip() for key, edit in self._fields.items() if edit.text().strip()}

	def _merge(self, old, entry):
		# The keys not edited here, e.g. added by a newer version, are kept.
		return {**entry, **{key: value for key, value in old.items() if key not in self._fields}}


class _ValueEdit(QLineEdit):
	"""An edit box of a value of a quantity, e.g. a number or a list of them. A value set with
	:meth:`setValue` and left as it was is given back as it was set, e.g. a float which is shown as an
	integer."""

	def __init__(self, placeholder, tool_tip):
		super().__init__()
		self.setPlaceholderText(placeholder)
		self.setToolTip(tool_tip)
		self._original = None
		self._has_original = False

	def setValue(self, value, present=True):
		"""Show the ``value``, nothing if it is not ``present``."""
		self._original = value
		self._has_original = present
		self.setText(format_value(value) if present else '')

	def value(self, parse):
		"""The value, the one set if it was not edited, else the text parsed with ``parse``, ``None`` if it is
		empty. Raises ``ValueError`` if the text cannot be parsed."""
		if self._has_original and self.text() == format_value(self._original):
			return self._original
		if not self.text().strip():
			return None
		return parse(self.text())


class _QuantityRow(QWidget):
	"""A quantity of a result group (see the Quantity representation of the EP) in a row: its kind if
	``kinds`` are given, its name and unit (see :class:`~pydaui.ui.quantity_selector.QuantitySelector`), its
	value if ``with_value``, its uncertainty, its coverage factor and a remove button if ``removable``.
	``changed`` tells the user changed it, ``removeRequested`` that the remove button was clicked.

	The keys of the quantity not edited here are kept, and so is its unit if it was not changed."""

	changed = Signal()
	removeRequested = Signal()

	def __init__(self, kinds=None, with_value=False, removable=True):
		super().__init__()
		self._original = {}
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)

		self._kind = None
		if kinds:
			self._kind = QComboBox()
			for key, title in kinds:
				self._kind.addItem(title, key)
			self._kind.setToolTip(self.tr('Whether the column holds the reference values of the measurand or the '
			                              'measured indications'))
			self._kind.currentIndexChanged.connect(self.changed)
			layout.addWidget(self._kind)
		self._quantity = QuantitySelector()
		self._quantity.setToolTip(self.tr('The name of the quantity and its unit'))
		self._quantity.changed.connect(self.changed)
		layout.addWidget(self._quantity, stretch=3)
		self._value = None
		if with_value:
			self._value = _ValueEdit(self.tr('value'), self.tr('The value held during the result group'))
			self._value.textChanged.connect(self.changed)
			layout.addWidget(self._value, stretch=1)
		self._unc = _ValueEdit(self.tr('uncertainty'), self.tr('The uncertainty in the unit of the quantity, one '
		                                                       'for every point or a list of them separated by '
		                                                       'commas'))
		self._unc.textChanged.connect(self.changed)
		layout.addWidget(self._unc, stretch=1)
		self._k = _ValueEdit(self.tr('k'), self.tr('The coverage factor of the uncertainty, e.g. 2'))
		self._k.setFixedWidth(self._k.fontMetrics().horizontalAdvance('0000') + 16)
		self._k.textChanged.connect(self.changed)
		layout.addWidget(self._k)
		if removable:
			remove = DauiButton(btn_icon=Icons.remove())
			remove.setToolTip(self.tr('Remove the quantity'))
			remove.clicked.connect(self.removeRequested)
			layout.addWidget(remove)
		polish_forms(self)

	def setQuantity(self, name, quantity, kind=None):
		"""Show the ``quantity`` map named ``name`` of the ``kind``."""
		self._original = quantity if isinstance(quantity, dict) else {'v': quantity}
		if self._kind is not None and kind is not None:
			self._kind.setCurrentIndex(max(0, self._kind.findData(kind)))
		self._quantity.setValue(name, self._original.get('u'))
		if self._value is not None:
			self._value.setValue(self._original.get('v'), 'v' in self._original)
		self._unc.setValue(self._original.get('unc'), 'unc' in self._original)
		self._k.setValue(self._original.get('k'), 'k' in self._original)

	def kind(self):
		return self._kind.currentData() if self._kind is not None else None

	def name(self):
		"""The name of the quantity, ``None`` if there is none."""
		return self._quantity.quantity()

	def unit(self):
		"""The unit, the one set if it was not changed, ``None`` if there is none."""
		original = self._original.get('u')
		if original is not None and self._quantity.unitText() == quantities.unit_symbol(str(original)):
			return original
		return self._quantity.unit()

	def original(self):
		"""The quantity map shown."""
		return self._original

	def quantity(self, values=None):
		"""The quantity map with the ``values`` if given, else the value edited. Raises ``ValueError`` telling
		why if a number cannot be parsed."""
		quantity = {}
		value = values if values is not None else (self._value.value(parse_number) if self._value else None)
		if value is not None:
			quantity['v'] = value
		unit = self.unit()
		if unit is not None:
			quantity['u'] = unit
		for key, edit, parse in (('unc', self._unc, parse_numbers), ('k', self._k, parse_number)):
			try:
				value = edit.value(parse)
			except ValueError:
				raise ValueError(self.tr('The %s of %s is not a number') % (edit.placeholderText(),
				                                                          self.name() or self.tr('a quantity')))
			if value is not None:
				quantity[key] = value
		# The keys not edited here are kept.
		for key, value in self._original.items():
			if key not in ('v', 'u', 'unc', 'k'):
				quantity[key] = value
		return quantity


class _PointsTable(QTableWidget):
	"""The table of the measurement points, a row per point and a column per quantity. Pasting text (e.g.
	from a spreadsheet) fills the cells from the current one on, its lines as rows and its cells separated by
	tabs, adding the rows needed. Delete clears the selected cells. ``edited`` tells the user changed it."""

	edited = Signal()

	def keyPressEvent(self, event):
		if event.matches(QKeySequence.Paste):
			self._paste()
		elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.state() != QAbstractItemView.EditingState:
			for cell in self.selectedItems():
				cell.setText('')
			self.edited.emit()
		else:
			super().keyPressEvent(event)

	def _paste(self):
		lines = [line for line in QGuiApplication.clipboard().text().splitlines() if line.strip()]
		if not lines or self.columnCount() == 0:
			return
		row, column = max(0, self.currentRow()), max(0, self.currentColumn())
		for offset, line in enumerate(lines):
			if row + offset >= self.rowCount():
				self.insertRow(self.rowCount())
			for index, text in enumerate(line.split('\t')):
				if column + index < self.columnCount():
					self.setItem(row + offset, column + index, QTableWidgetItem(text.strip()))
		self.edited.emit()


class _FitPlot(Plot):
	"""The graph of the compensation model, preferring its minimum height: a graph prefers to be large, the
	tabs it is in would be as tall."""

	def sizeHint(self):
		return QSize(super().sizeHint().width(), self.minimumHeight())


class ResultsView(QWidget):
	"""The Results tab, the result groups (``res``) of the certificate in a list, in the order of the
	certificate. ``changed`` tells the user changed the certificate.

	The groups are added and edited in a group below the list, shown while adding or editing one, its fields
	in tabs: the description, the conditions, the quantities, the points and the compensation model. A group
	refers to a target, a configuration and equipment of the certificate, picked from the ones in the other
	tabs. Its influence conditions are quantities in rows. It has a reference and a measured quantity, the
	columns of the table of the points, a new group gets the measurand and the indication of its target. Further
	reference or measured quantities of a group are not shown, they are kept.

	The compensation model is a polynomial fitted to the points, mapping the measured indication, or its
	reciprocal, to the reference measurand (the ``poly`` scheme of the EP), its value raised to a power,
	included in the group if chosen. Its order, whether the reciprocal is used and the exponent may be chosen
	automatically, see :meth:`_autoFit`. The model of a group
	is kept as it is until the points or the fit are changed, its other schemes (e.g. ``tc``) are kept.
	"""

	changed = Signal()

	KEY = 'res'

	#: The columns of the list as (title, ratio).
	COLUMNS = (('Target', 2), ('Configuration', 2), ('Description', 3), ('Conditions', 2), ('Points', 1))

	#: The keys of a group in the order of the EP, and the ones edited here.
	ORDER = ('tgt', 'cfg', 'desc', 'cond', 'ref', 'meas', 'comp', 'meq', 'method')
	EDITED = ('tgt', 'cfg', 'desc', 'cond', 'ref', 'meas', 'comp', 'meq', 'method')

	#: The kinds of the columns of the points as (key, title), in the order of the columns.
	KINDS = (('ref', 'Reference'), ('meas', 'Measured'))
	REF_COLUMN = 0
	MEAS_COLUMN = 1

	#: The orders of the polynomial of the compensation model offered, and the default one. The mq-compensation
	#: service evaluates up to 7 (its coefficient table), as many as its build configuration allows.
	MAX_ORDER = 7
	DEFAULT_ORDER = 1
	#: The highest order chosen automatically, the default build configuration of the mq-compensation service.
	AUTO_MAX_ORDER = 3
	#: Choosing automatically, a model whose cross-validation error is within this fraction of the best one is
	#: preferred if it is simpler (a lower order, no reciprocal, no exponent). Errors below this fraction of
	#: the largest reference value are rounding errors, as good as none.
	AUTO_TOLERANCE = 0.1
	AUTO_NEGLIGIBLE = 1e-9

	#: The number of the points the fitted polynomial is drawn with.
	CURVE_POINTS = 200

	#: The role of a cell of the points keeping its value, and of whether it has one.
	ORIGINAL_ROLE = Qt.UserRole
	HAS_ORIGINAL_ROLE = Qt.UserRole + 1

	def __init__(self):
		super().__init__()
		self._certificate = None
		# The index of the group being edited, None when adding one.
		self._editing = None
		# Whether the form is being filled, the changes are not the user's then.
		self._filling = False

		self._list = StyledTreeWidget()
		self._list.COLUMN_RATIOS = tuple(ratio for title, ratio in self.COLUMNS)
		self._list.setHeaderLabels([self.tr(title) for title, ratio in self.COLUMNS])
		self._list.setRootIsDecorated(False)
		# The columns fill the whole width.
		self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self._list.currentItemChanged.connect(lambda current, previous: self._updateButtons())
		self._list.itemDoubleClicked.connect(lambda item, column: self._edit())

		self._add = DauiButton(self.tr('Add'), Icons.add())
		self._add.setToolTip(self.tr('Add a result group after the selected one'))
		self._add.clicked.connect(self._startAdd)
		self._edit_btn = DauiButton(self.tr('Edit'), Icons.edit())
		self._edit_btn.setToolTip(self.tr('Edit the selected result group'))
		self._edit_btn.clicked.connect(self._edit)
		self._remove = DauiButton(self.tr('Remove'), Icons.remove())
		self._remove.setToolTip(self.tr('Remove the selected result group'))
		self._remove.clicked.connect(self._removeSelected)
		self._up = DauiButton(btn_icon=Icons.up())
		self._up.setToolTip(self.tr('Move the selected result group up'))
		self._up.clicked.connect(lambda: self._moveSelected(-1))
		self._down = DauiButton(btn_icon=Icons.down())
		self._down.setToolTip(self.tr('Move the selected result group down'))
		self._down.clicked.connect(lambda: self._moveSelected(1))
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		for button in (self._add, self._edit_btn, self._remove):
			buttons.addWidget(button)
		buttons.addStretch(1)
		buttons.addWidget(self._up)
		buttons.addWidget(self._down)
		button_row = QWidget()
		button_row.setLayout(buttons)

		self._form_group = self._editGroup()
		polish_forms(self._form_group)

		layout = QVBoxLayout(self)
		layout.addWidget(SettingsGroup(self.tr('Result groups'), self._list, button_row))
		layout.addWidget(self._form_group)
		layout.addStretch(1)
		self._closeEdit()

	def _editGroup(self):
		"""The group adding or editing a result group in tabs, hidden until Add or Edit is clicked."""
		self._tabs = QTabWidget()
		field_rows = QWidget()
		field_rows_layout = QVBoxLayout(field_rows)
		field_rows_layout.setContentsMargins(0, 0, 0, 0)
		field_rows_layout.addWidget(self._tabs)

		def page(title, icon, layout, stretch=True):
			"""Add a tab of the ``layout``, its widgets stacked at the top. Unless ``stretch``, the layout gets
			the height the tab has more than it needs, e.g. as tall as the tallest tab."""
			widget = QWidget()
			outer = QVBoxLayout(widget)
			outer.addLayout(layout, stretch=0 if stretch else 1)
			if stretch:
				outer.addStretch(1)
			self._tabs.addTab(widget, icon, self.tr(title))
			return layout

		fields = page('Group description', Icons.group(), form_layout())

		self._confirm = DauiButton(self.tr('Add'), Icons.confirm())
		self._confirm.setDefault(True)
		self._confirm.clicked.connect(self._confirmEdit)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self._closeEdit)

		self._target = QComboBox()
		self._target.setToolTip(self.tr('The calibration target the result group applies to'))
		self._target.currentIndexChanged.connect(self._targetChanged)
		fields.addRow(WrappedLabel(self.tr('Target:')), self._target)
		self._configuration = QComboBox()
		self._configuration.setToolTip(self.tr('The configuration the target was in during the result group'))
		self._configuration.currentIndexChanged.connect(self._updateConfirm)
		fields.addRow(WrappedLabel(self.tr('Configuration:')), self._configuration)
		self._desc = QLineEdit()
		self._desc.setPlaceholderText(self.tr('e.g. Voltage transfer characteristic, PGA = 1'))
		self._desc.setToolTip(self.tr('A human-readable description of the result group'))
		fields.addRow(WrappedLabel(self.tr('Description:')), self._desc)
		self._method = QLineEdit()
		self._method.setPlaceholderText(self.tr('name of or reference to the calibration procedure'))
		self._method.setToolTip(self.tr('The calibration method or procedure applied'))
		fields.addRow(WrappedLabel(self.tr('Method:')), self._method)
		self._equipment = QListWidget()
		self._equipment.setToolTip(self.tr('The measuring equipment the result group was obtained with'))
		self._equipment.setFixedHeight(self._equipment.fontMetrics().lineSpacing() * 4 + 16)
		fields.addRow(WrappedLabel(self.tr('Equipment:')), self._equipment)

		# The influence conditions, a row each, and the button adding one below them.
		self._conditions = []
		self._condition_rows = QVBoxLayout()
		self._condition_rows.setContentsMargins(0, 0, 0, 0)
		self._condition_rows.setSpacing(4)
		add_condition = DauiButton(self.tr('Add condition'), Icons.add())
		add_condition.setToolTip(self.tr('Add an influence condition held during the result group, e.g. the '
		                                 'temperature'))
		add_condition.clicked.connect(lambda: self._addCondition())
		conditions = QVBoxLayout()
		conditions.setSpacing(4)
		conditions.addLayout(self._condition_rows)
		conditions.addLayout(self._buttonRow(add_condition))
		page('Conditions', Icons.thermometer(), conditions)

		# The reference and the measured quantity, in the order of the columns of the points. The further
		# quantities of the group by their kind, not shown.
		self._columns = []
		self._other_columns = {}
		columns = QVBoxLayout()
		columns.setSpacing(8)
		for kind, title, tool_tip in (
				('ref', 'Reference quantity', 'The reference values of the measurand, provided by a standard'),
				('meas', 'Measured quantity', 'The indications measured from the target')):
			row = _QuantityRow(removable=False)
			row.setToolTip(self.tr(tool_tip))
			row.changed.connect(self._columnChanged)
			self._columns.append(row)
			box = QGroupBox(self.tr(title))
			QVBoxLayout(box).addWidget(row)
			columns.addWidget(box)
		page('Quantities', Icons.quantity(), columns)

		self._points = _PointsTable(0, len(self.KINDS))
		self._points.verticalHeader().setDefaultAlignment(Qt.AlignCenter)
		self._points.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
		self._points.setToolTip(self.tr('The measurement points, a row each. Paste the values from a spreadsheet '
		                                'with Ctrl+V.'))
		self._points.setFixedHeight(self._points.horizontalHeader().sizeHint().height()
		                            + self._points.verticalHeader().defaultSectionSize() * 8 + 4)
		self._points.itemChanged.connect(self._fitInputChanged)
		self._points.edited.connect(self._fitInputChanged)
		self._points.itemSelectionChanged.connect(self._updatePointButtons)
		self._add_point = DauiButton(self.tr('Add point'), Icons.add())
		self._add_point.clicked.connect(self._addPoint)
		self._remove_point = DauiButton(self.tr('Remove point'), Icons.remove())
		self._remove_point.clicked.connect(self._removePoints)
		points = QVBoxLayout()
		points.setSpacing(4)
		points.addWidget(self._points)
		points.addLayout(self._buttonRow(self._add_point, self._remove_point))
		page('Points', Icons.graph(), points)
		page('Compensation model', Icons.fit(), self._compensationPage(), stretch=False)

		# Escape cancels.
		QShortcut(QKeySequence(Qt.Key_Escape), field_rows, cancel.click, context=Qt.WidgetWithChildrenShortcut)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addStretch(1)
		buttons.addWidget(self._confirm)
		buttons.addWidget(cancel)
		button_row = QWidget()
		button_row.setLayout(buttons)

		return SettingsGroup('', field_rows, button_row)

	def _compensationPage(self):
		"""The layout of the compensation model tab: whether the model is included, the polynomial and its
		fit, and the graph of the fit."""
		self._include_model = QCheckBox(self.tr('Include model in the certificate'))
		self._include_model.setToolTip(self.tr('Put the polynomial fitted to the points into the result group as '
		                                       'its compensation model'))
		self._include_model.toggled.connect(self._updateConfirm)
		self._auto = QCheckBox(self.tr('Choose the order, the reciprocal and the exponent automatically'))
		self._auto.setToolTip(self.tr('Pick the polynomial order (up to %d), whether to use the reciprocal of the '
		                              'measured indication and the exponent (1 or -1) which predict the points '
		                              'best, each point predicted from a fit to the others') % self.AUTO_MAX_ORDER)
		self._auto.toggled.connect(self._fitInputChanged)
		self._order = QSpinBox()
		self._order.setRange(1, self.MAX_ORDER)
		self._order.setToolTip(self.tr('The order of the polynomial, 1 for a straight line (offset and gain)'))
		self._order.valueChanged.connect(self._fitInputChanged)
		self._reciprocal = QCheckBox(self.tr('Reciprocal input (1/x)'))
		self._reciprocal.setToolTip(self.tr('Fit the polynomial to the reciprocal of the measured indication, e.g. '
		                                    'a conductance instead of a resistance. Points with a zero measured '
		                                    'value are left out.'))
		self._reciprocal.toggled.connect(self._fitInputChanged)
		self._exp = _ValueEdit('1', self.tr('The exponent the value of the polynomial is raised to, preserving its '
		                                    'sign, e.g. -1 for a resistance measured by a voltage divider. 1 or '
		                                    'empty for none.'))
		self._exp.textChanged.connect(self._fitInputChanged)
		self._x0 = _ValueEdit('0', self.tr('The reference point subtracted from the measured indication (or its '
		                                   'reciprocal) before evaluating the polynomial, improving the numerical '
		                                   'conditioning'))
		self._x0.textChanged.connect(self._fitInputChanged)
		self._center = DauiButton(self.tr('Center'), Icons.refresh())
		self._center.setToolTip(self.tr('Set the centering offset to the middle of the measured indications'))
		self._center.clicked.connect(self._centerX0)
		x0_row = QHBoxLayout()
		x0_row.setSpacing(4)
		x0_row.addWidget(self._x0, stretch=1)
		x0_row.addWidget(self._center)
		self._coefficients = QLabel()
		self._coefficients.setFont(monospace_font())
		self._coefficients.setTextInteractionFlags(Qt.TextSelectableByMouse)
		# Why there is no fit, or the schemes of the model which are kept.
		self._fit_note = QLabel()
		self._fit_note.setWordWrap(True)
		self._fit_note.setStyleSheet(f'color: {SectionView.HINT_COLOR};')

		fields = form_layout()
		fields.addRow(self._include_model)
		fields.addRow(self._auto)
		fields.addRow(WrappedLabel(self.tr('Polynomial order:')), self._order)
		fields.addRow('', self._reciprocal)
		fields.addRow(WrappedLabel(self.tr('Output exponent:')), self._exp)
		fields.addRow(WrappedLabel(self.tr('Centering offset (x0):')), x0_row)
		fields.addRow(WrappedLabel(self.tr('Coefficients:')), self._coefficients)
		fields.addRow(self._fit_note)

		self._fit_plot = _FitPlot()
		self._fit_plot.setMinimumHeight(170)
		self._fit_points = pg.PlotDataItem(name=self.tr('Points'), pen=None, symbol='o', symbolSize=7,
		                                   symbolBrush=GRAPH_COLORS[0], symbolPen=pg.mkPen(GRAPH_COLORS[0]))
		self._fit_curve = pg.PlotDataItem(name=self.tr('Model'), antialias=True,
		                                  pen=pg.mkPen(GRAPH_COLORS[1], width=Plot.LINE_WIDTH))
		self._fit_plot.addLine(self._fit_curve)
		self._fit_plot.addLine(self._fit_points)

		layout = QVBoxLayout()
		layout.addLayout(fields)
		layout.addWidget(self._fit_plot, stretch=1)
		# The original model of the group, its poly scheme used until the points or the fit are changed.
		self._comp = {}
		self._fit_edited = False
		# The poly scheme of the model shown, None if there is none.
		self._poly = None
		return layout

	@staticmethod
	def _buttonRow(*buttons):
		row = QHBoxLayout()
		row.setContentsMargins(0, 0, 0, 0)
		for button in buttons:
			row.addWidget(button)
		row.addStretch(1)
		return row

	def setCertificate(self, certificate):
		self._certificate = certificate
		self._closeEdit()
		self._load()

	def _section(self, key):
		"""The top-level map ``key`` of the certificate, the entries which are maps."""
		section = self._certificate.section(key) if self._certificate is not None else None
		return {k: v for k, v in section.items() if isinstance(v, dict)} if isinstance(section, dict) else {}

	def _groups(self):
		"""The result groups of the certificate, a copy of the list."""
		res = self._certificate.section(self.KEY) if self._certificate is not None else None
		return list(res) if isinstance(res, list) else []

	@staticmethod
	def _points_count(group):
		"""The number of points of the result ``group``, the length of its longest reference or measured
		column."""
		count = 0
		for key in ('ref', 'meas'):
			columns = group.get(key)
			for quantity in columns.values() if isinstance(columns, dict) else []:
				value = quantity.get('v') if isinstance(quantity, dict) else None
				count = max(count, len(value) if isinstance(value, list) else int(value is not None))
		return count

	def _summary(self, group):
		"""The texts of the columns of the list of the result ``group``."""
		targets = self._section('tgt')
		target = group.get('tgt')
		name = targets[target].get('n') if target in targets else None
		conditions = []
		cond = group.get('cond')
		for key, quantity in cond.items() if isinstance(cond, dict) else []:
			quantity = quantity if isinstance(quantity, dict) else {'v': quantity}
			unit = quantities.unit_symbol(str(quantity['u'])) if 'u' in quantity else ''
			conditions.append(f'{key} {format_value(quantity.get("v", ""))} {unit}'.strip())
		return [str(name or format_scalar(target) if target is not None else ''),
		        format_scalar(group['cfg']) if 'cfg' in group else '',
		        str(group.get('desc', '')), ', '.join(conditions), str(self._points_count(group))]

	def _load(self, select=None):
		"""Show the result groups in the list, the one at the index ``select`` selected."""
		# Telling the current item changed while it is deleted would ask the deleted item.
		self._list.blockSignals(True)
		self._list.clear()
		for index, group in enumerate(self._groups()):
			item = QTreeWidgetItem(self._summary(group) if isinstance(group, dict) else [format_scalar(group)])
			item.setData(0, Qt.UserRole, index)
			self._list.styleName(item)
			self._list.addTopLevelItem(item)
			if index == select:
				self._list.setCurrentItem(item)
		self._list.blockSignals(False)
		self._updateButtons()

	def _store(self, groups, select=None):
		self._certificate.setSection(self.KEY, groups)
		self._load(select)
		self.changed.emit()

	def _currentIndex(self):
		"""The index of the selected result group, ``None`` if there is none."""
		item = self._list.currentItem()
		return item.data(0, Qt.UserRole) if item is not None else None

	def _moveSelected(self, step):
		index = self._currentIndex()
		groups = self._groups()
		if index is None or self._form_group.isVisible() or not 0 <= index + step < len(groups):
			return
		groups[index], groups[index + step] = groups[index + step], groups[index]
		self._store(groups, select=index + step)

	@Slot()
	def _removeSelected(self):
		index = self._currentIndex()
		if index is None or self._form_group.isVisible():
			return
		question = self.tr('Remove the result group %d (%s)?') % (index + 1, self._list.currentItem().text(2)
		                                                         or self._list.currentItem().text(0))
		if QMessageBox.question(self, self.tr('Remove result group'), question) != QMessageBox.Yes:
			return
		groups = self._groups()
		del groups[index]
		self._store(groups, select=min(index, len(groups) - 1))

	@Slot()
	def _startAdd(self):
		self._openEdit(None, self.tr('Add result group'), self.tr('Add'), {})

	@Slot()
	def _edit(self):
		index = self._currentIndex()
		if index is None or self._form_group.isVisible() or not isinstance(self._groups()[index], dict):
			return
		self._openEdit(index, self.tr('Edit result group %d') % (index + 1), self.tr('Confirm'),
		               self._groups()[index])

	def _fillChoices(self, combo, choices, current, none):
		"""Offer the ``choices`` as (key, title) in the ``combo``, ``none`` first if given (``None``), the
		``current`` key selected, offered as missing if it is not one of them."""
		combo.clear()
		if none is not None:
			combo.addItem(none, None)
		for key, title in choices:
			combo.addItem(title, key)
		if current is not None and combo.findData(current) < 0:
			combo.addItem(self.tr('%s (missing)') % format_scalar(current), current)
		combo.setCurrentIndex(max(0, combo.findData(current)))

	def _offerChoices(self, target, configuration, used):
		"""Offer the targets, the configurations and the equipment of the certificate, the ``target``, the
		``configuration`` and the equipment ``used`` picked."""
		targets = self._section('tgt')
		self._fillChoices(self._target, [(key, f'{target.get("n") or format_scalar(key)} ({format_scalar(key)})')
		                                 for key, target in targets.items()], target, None)
		self._fillChoices(self._configuration, [(key, format_scalar(key)) for key in self._section('cfg')],
		                  configuration, self.tr('None'))
		self._equipment.clear()
		equipment = self._section('meq')
		for key in list(equipment) + [key for key in used if key not in equipment]:
			title = (f'{equipment[key].get("n") or format_scalar(key)} ({format_scalar(key)})' if key in equipment
			         else self.tr('%s (missing)') % format_scalar(key))
			item = QListWidgetItem(title)
			item.setData(Qt.UserRole, key)
			item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
			item.setCheckState(Qt.Checked if key in used else Qt.Unchecked)
			self._equipment.addItem(item)

	def showEvent(self, event):
		super().showEvent(event)
		# The other tabs may have changed the targets, the configurations and the equipment meanwhile.
		if self._certificate is None:
			return
		self._load(self._currentIndex())
		if self._form_group.isVisibleTo(self):
			self._filling = True
			used = [self._equipment.item(index).data(Qt.UserRole) for index in range(self._equipment.count())
			        if self._equipment.item(index).checkState() == Qt.Checked]
			self._offerChoices(self._target.currentData(), self._configuration.currentData(), used)
			self._filling = False
			self._updateConfirm()

	def _openEdit(self, index, title, confirm, group):
		self._filling = True
		self._editing = index
		self._form_group.setTitle(title)
		self._confirm.setText(confirm)
		self._tabs.setCurrentIndex(0)
		self._offerChoices(group.get('tgt'), group.get('cfg'),
		                   group.get('meq') if isinstance(group.get('meq'), list) else [])
		self._desc.setText(str(group.get('desc', '')))
		self._method.setText(str(group.get('method', '')))

		for row in list(self._conditions):
			self._removeRow(row)
		cond = group.get('cond')
		for name, quantity in cond.items() if isinstance(cond, dict) else []:
			self._addCondition(name, quantity)
		self._points.setRowCount(0)
		self._points.setRowCount(self._points_count(group))
		self._other_columns = {}
		for column, (kind, title) in enumerate(self.KINDS):
			columns = group.get(kind)
			columns = list(columns.items()) if isinstance(columns, dict) else []
			name, quantity = columns[0] if columns else (None, {})
			self._setColumn(column, name, quantity)
			self._other_columns[kind] = dict(columns[1:])
		self._columnChanged()

		comp = group.get('comp')
		self._comp = comp if isinstance(comp, dict) else {}
		poly = self._model()
		self._include_model.setChecked(poly is not None)
		self._order.setValue(min(max(len(poly['c']) - 1, 1), self.MAX_ORDER) if poly else self.DEFAULT_ORDER)
		self._x0.setValue(poly.get('x0') if poly else None, poly is not None and 'x0' in poly)
		self._reciprocal.setChecked(bool(poly.get('recip')) if poly else False)
		self._exp.setValue(poly.get('exp') if poly else None, poly is not None and 'exp' in poly)
		# A model of the group is shown as it is, a new one is chosen automatically.
		self._auto.setChecked(poly is None)
		self._fit_edited = poly is None
		self._filling = False
		self._updateFit()
		if index is None:
			self._targetChanged()
		self._updateConfirm()
		self._updatePointButtons()
		self._form_group.show()
		self._target.setFocus()
		self._updateButtons()

	def _addCondition(self, name=None, quantity=None):
		row = _QuantityRow(with_value=True)
		if name is not None:
			row.setQuantity(name, quantity)
		row.changed.connect(self._updateConfirm)
		row.removeRequested.connect(lambda: self._removeRow(row))
		self._conditions.append(row)
		self._condition_rows.addWidget(row)
		self._updateConfirm()

	def _setColumn(self, column, name, quantity):
		"""Show the quantity map ``quantity`` named ``name`` (``None`` for none) in the ``column`` of the
		points, its values in the rows there are."""
		row = self._columns[column]
		row.setQuantity(name, quantity if isinstance(quantity, dict) else {'v': quantity})
		value = row.original().get('v')
		values = value if isinstance(value, list) else [value] if value is not None else []
		self._points.blockSignals(True)
		for point in range(self._points.rowCount()):
			cell = QTableWidgetItem(format_scalar(values[point]) if point < len(values) else '')
			if point < len(values):
				cell.setData(self.ORIGINAL_ROLE, values[point])
				cell.setData(self.HAS_ORIGINAL_ROLE, True)
			self._points.setItem(point, column, cell)
		self._points.blockSignals(False)

	def _removeRow(self, row):
		"""Remove the condition ``row``."""
		self._conditions.remove(row)
		# Not in the layout any more, it would show over the others until deleted.
		row.hide()
		row.deleteLater()
		self._updateConfirm()

	@Slot()
	def _columnChanged(self):
		"""Show the kinds, names and units of the columns in the header of the points."""
		self._points.setHorizontalHeaderLabels([f'{self.tr(title)}\n{self._columnLabel(column)}'
		                                        for column, (kind, title) in enumerate(self.KINDS)])
		self._updatePointButtons()
		if not self._filling:
			self._updateFit()
		self._updateConfirm()

	def _columnLabel(self, column):
		"""The name of the quantity of the ``column`` with its unit, e.g. ``voltage [V]``."""
		row = self._columns[column]
		unit = row.unit()
		label = row.name() or ''
		return f'{label} [{quantities.unit_symbol(str(unit))}]' if unit else label

	@Slot()
	def _targetChanged(self):
		"""Offer the columns of the measurand and the indication of the target picked for a new group with no
		columns yet."""
		if self._filling or self._editing is not None or any(row.name() for row in self._columns):
			self._updateConfirm()
			return
		target = self._section('tgt').get(self._target.currentData(), {})
		for column, (name, unit) in enumerate((('mq', 'mu'), ('iq', 'iu'))):
			if target.get(name):
				self._columns[column].setQuantity(target[name], {'u': target[unit]} if target.get(unit) else {})
		self._columnChanged()

	@Slot()
	def _addPoint(self):
		self._points.blockSignals(True)
		row = self._points.rowCount()
		self._points.insertRow(row)
		for column in range(self._points.columnCount()):
			self._points.setItem(row, column, QTableWidgetItem(''))
		self._points.blockSignals(False)
		self._points.setCurrentCell(row, 0)
		self._points.editItem(self._points.item(row, 0))
		self._fitInputChanged()

	@Slot()
	def _removePoints(self):
		rows = sorted({index.row() for index in self._points.selectedIndexes()} or
		              ({self._points.currentRow()} - {-1}), reverse=True)
		for row in rows:
			self._points.removeRow(row)
		self._updatePointButtons()
		self._fitInputChanged()

	@Slot()
	def _updatePointButtons(self):
		self._remove_point.setEnabled(self._points.currentRow() >= 0 or bool(self._points.selectedIndexes()))

	@Slot()
	def _fitInputChanged(self):
		"""The points or the fit were changed, the model is fitted again."""
		if self._filling:
			return
		self._fit_edited = True
		self._updateFit()
		self._updateConfirm()

	def _fitPoints(self):
		"""The measured indications and the reference values of the points as arrays, the points with a value
		which is not finite (e.g. NaN or an infinity) left out, and the number of them. Raises ``ValueError``
		telling why there are none."""
		x, y = [], []
		for point in range(self._points.rowCount()):
			try:
				x.append(float(self._cellValue(point, self.MEAS_COLUMN)))
				y.append(float(self._cellValue(point, self.REF_COLUMN)))
			except (TypeError, ValueError):
				raise ValueError(self.tr('Each point needs a number in every column to fit the model.'))
		x, y = np.array(x), np.array(y)
		finite = np.isfinite(x) & np.isfinite(y)
		return x[finite], y[finite], int(np.count_nonzero(~finite))

	# The model is evaluated by the compensation module.
	MODEL_KEYS = compensation.MODEL_KEYS
	_transform = staticmethod(compensation.transform)
	_fittable = staticmethod(compensation.fittable)
	_power = staticmethod(compensation.power)
	_predict = staticmethod(compensation.predict)
	_schemes = staticmethod(compensation.schemes)

	def _model(self):
		"""The polynomial model of the group as a single map, see :func:`pydaui.compensation.model_of`."""
		return compensation.model_of(self._comp)

	@classmethod
	def _coefficients_of(cls, x, y, order, recip, exp, x0):
		"""The coefficients of the polynomial of the ``order`` fitted to the points ``x``, ``y`` with the
		reciprocal of ``x`` if ``recip`` and the exponent ``exp``, centered at ``x0``: fitted to the reference
		values raised to ``1/exp``, the points which cannot be (e.g. a zero to a negative power) left out.
		``None`` if there are not enough points left.

		Each point is weighted by the slope of raising back to ``exp`` there, the fit minimises the errors of
		the reference values rather than of their powers (to a first order)."""
		yt = cls._power(y, 1 / exp)
		usable = cls._fittable(x, recip) & np.isfinite(yt)
		xt, yt = cls._transform(x[usable], recip), yt[usable]
		if len(set(xt)) < order + 1:
			return None
		weights = None
		if exp != 1:
			with np.errstate(divide='ignore', over='ignore'):
				weights = abs(exp) * np.power(np.abs(yt), exp - 1)
			if not np.all(np.isfinite(weights)) or not np.any(weights > 0):
				return None
			weights = weights / weights.max()
		coefficients = np.polynomial.polynomial.polyfit(xt - x0, yt, order, w=weights)
		return coefficients if np.all(np.isfinite(coefficients)) else None

	@classmethod
	def _modelError(cls, x, y, order, recip, exp, cross_validate):
		"""The RMS error of the model of the ``order``, ``recip`` and ``exp`` at the points ``x``, ``y``:
		predicting each point by a fit to the others (leave-one-out cross-validation) if ``cross_validate``,
		else the residuals of a fit to all of them. ``None`` if the model cannot be fitted or a prediction is
		not finite."""
		folds = [np.arange(len(x)) != index for index in range(len(x))] if cross_validate else [None]
		errors = []
		for index, others in enumerate(folds):
			fit_x, fit_y = (x[others], y[others]) if others is not None else (x, y)
			# Centered for the conditioning, the predictions do not depend on it.
			usable = cls._transform(fit_x[cls._fittable(fit_x, recip)], recip)
			x0 = float(np.mean(usable)) if len(usable) else 0.0
			coefficients = cls._coefficients_of(fit_x, fit_y, order, recip, exp, x0)
			if coefficients is None:
				return None
			poly = {'recip': recip, 'x0': x0, 'c': list(coefficients), 'exp': exp}
			if others is not None:
				errors.append(float(cls._predict(poly, x[index:index + 1])[0]) - y[index])
			else:
				errors.extend(cls._predict(poly, x) - y)
		error = float(np.sqrt(np.mean(np.square(errors))))
		return error if np.isfinite(error) else None

	def _autoFit(self, x, y):
		"""The order, whether to use the reciprocal and the exponent (1 or -1) of the model predicting the
		points ``x``, ``y`` best: the smallest leave-one-out cross-validation error, a simpler model (a lower
		order, then no reciprocal, then no exponent) preferred within :data:`AUTO_TOLERANCE` of it. The order is
		up to :data:`AUTO_MAX_ORDER`. Without enough points to cross-validate each model of order 1 which can
		be fitted (e.g. leaving out the zeros with the reciprocal), the models of order 1 (of as many
		coefficients) are compared by their residuals instead. Failing that, a straight line."""
		combinations = [(recip, exp) for recip in (False, True) for exp in (1, -1)]
		validated = all(self._modelError(x, y, 1, recip, exp, True) is not None for recip, exp in combinations
		                if self._modelError(x, y, 1, recip, exp, False) is not None)
		stages = [(False, (1,))]
		if validated:
			stages.insert(0, (True, range(1, self.AUTO_MAX_ORDER + 1)))
		for cross_validate, orders in stages:
			candidates = []
			for order in orders:
				for recip, exp in combinations:
					error = self._modelError(x, y, order, recip, exp, cross_validate)
					if error is not None:
						candidates.append((error, order, recip, exp))
			if candidates:
				best = min(candidate[0] for candidate in candidates)
				limit = best * (1 + self.AUTO_TOLERANCE) + self.AUTO_NEGLIGIBLE * float(np.max(np.abs(y)))
				error, order, recip, exp = min(
					(candidate for candidate in candidates if candidate[0] <= limit),
					key=lambda candidate: (candidate[1], candidate[2], candidate[3] != 1))
				return order, recip, exp
		return self.DEFAULT_ORDER, False, 1

	def _fitPoly(self, x, y, order, recip, exp):
		"""The poly scheme of the ``order`` fitted to the points ``x``, ``y``, of the reciprocals of ``x`` if
		``recip``, raised to the power of ``exp``. Raises ``ValueError`` telling why it cannot be fitted."""
		try:
			x0 = self._x0.value(parse_number)
		except ValueError:
			raise ValueError(self.tr('The centering offset is not a number.'))
		coefficients = self._coefficients_of(x, y, order, recip, exp, x0 or 0)
		if coefficients is None:
			raise ValueError(self.tr('A polynomial of order %d needs at least %d points with different measured '
			                         'values it can be fitted to.') % (order, order + 1))
		poly = {'recip': True} if recip else {}
		if x0 not in (None, 0):
			poly['x0'] = x0
		poly['c'] = [float(c) for c in coefficients]
		if exp != 1:
			poly['exp'] = exp
		# The keys not edited here are kept.
		old = self._comp.get('poly')
		for key, value in old.items() if isinstance(old, dict) else []:
			if key not in ('recip', 'x0', 'c', 'exp'):
				poly.setdefault(key, value)
		return poly

	def _exponent(self):
		"""The exponent typed, 1 if it is empty. Raises ``ValueError`` if it is not a number other than 0."""
		value = self._exp.value(parse_number)
		if value is None:
			return 1
		if value == 0 or not np.isfinite(value):
			raise ValueError(self.tr('The exponent is not a number other than 0.'))
		return value

	def _updateFit(self):
		"""Fit the model to the points, or keep the model of the group if they were not changed, and show it
		with the graphs."""
		self._poly, note = None, ''
		x = y = np.array([])
		left_out = 0
		try:
			x, y, left_out = self._fitPoints()
		except ValueError as e:
			note = str(e)
		original = self._model()
		if not self._fit_edited and original is not None:
			self._poly = original
		elif not note:
			if self._auto.isChecked():
				order, recip, exp = self._autoFit(x, y)
				for widget, setter, value in ((self._order, self._order.setValue, order),
				                              (self._reciprocal, self._reciprocal.setChecked, recip),
				                              (self._exp, self._exp.setText, '' if exp == 1 else format_scalar(exp))):
					widget.blockSignals(True)
					setter(value)
					widget.blockSignals(False)
			try:
				self._poly = self._fitPoly(x, y, self._order.value(), self._reciprocal.isChecked(), self._exponent())
			except ValueError as e:
				note = str(e)
		for widget in (self._order, self._reciprocal, self._exp):
			widget.setEnabled(not self._auto.isChecked())
		notes = [note] if note else []
		if left_out:
			notes.append(self.tr('%d point(s) with a value which is not finite (NaN or an infinity) are left out '
			                     'of the fit.') % left_out)
		if self._poly is not None and len(x):
			try:
				recip, exp = bool(self._poly.get('recip')), float(self._poly.get('exp', 1))
				unusable = ~(self._fittable(x, recip) & np.isfinite(self._power(y, 1 / exp)))
				if np.any(unusable):
					notes.append(self.tr('%d point(s) cannot be fitted with the reciprocal or the exponent (e.g. a '
					                     'zero) and are left out of the fit, they are predicted still.')
					             % int(np.count_nonzero(unusable)))
			except (TypeError, ValueError, ZeroDivisionError):
				pass
		others = [key for key in self._comp if key != 'poly' and key not in self.MODEL_KEYS]
		if self._poly is not None and others:
			notes.append(self.tr('The other schemes of the model are kept: %s.') % ', '.join(map(str, others)))
		note = ' '.join(notes)
		self._fit_note.setText(note)
		self._fit_note.setVisible(bool(note))

		measured = self._columnLabel(self.MEAS_COLUMN)
		reference = self._columnLabel(self.REF_COLUMN)
		self._fit_plot.setAxisLabel('bottom', measured)
		self._fit_plot.setAxisLabel('left', reference)
		self._fit_points.setData(x, y)
		if self._poly is None:
			self._coefficients.setText('—')
			self._fit_curve.setData([], [])
			return
		try:
			lines = [f'c{index} = {float(c):.9g}' for index, c in enumerate(self._poly['c'])]
			lines.insert(0, self.tr('y = %s') % compensation.formula(self._poly, format_scalar))
			if len(x):
				low, high = float(x.min()), float(x.max())
				curve = np.linspace(low, high, self.CURVE_POINTS) if high > low else np.array([low])
				values = self._predict(self._poly, curve)
				shown = np.isfinite(values)
				self._fit_curve.setData(curve[shown], values[shown])
				residuals = y - self._predict(self._poly, x)
				residuals = residuals[np.isfinite(residuals)]
				residuals = residuals if len(residuals) else np.array([0.0])
				unit = self._columns[self.REF_COLUMN].unit()
				unit = f' {quantities.unit_symbol(str(unit))}' if unit else ''
				lines.append(self.tr('residuals: RMS %s%s, max %s%s') % (
					f'{float(np.sqrt(np.mean(residuals ** 2))):.3g}', unit, f'{float(np.abs(residuals).max()):.3g}',
					unit))
			else:
				self._fit_curve.setData([], [])
		except (TypeError, ValueError, KeyError, ZeroDivisionError):
			lines = [self.tr('The model of the result group is not a valid polynomial.')]
			self._poly = None
		self._coefficients.setText('\n'.join(lines))

	@Slot()
	def _centerX0(self):
		"""Set the centering offset to the middle of the range of the measured indications, or of their
		reciprocals."""
		try:
			x, y, left_out = self._fitPoints()
		except ValueError:
			return
		recip = self._reciprocal.isChecked()
		x = self._transform(x[self._fittable(x, recip)], recip)
		if len(x):
			middle = (float(x.min()) + float(x.max())) / 2
			self._x0.setText(format_scalar(int(middle) if middle.is_integer() else middle))

	def _cellValue(self, row, column):
		"""The value of the cell of the points, the one set if it was not edited. Raises ``ValueError`` if it
		is empty or not a number."""
		cell = self._points.item(row, column)
		text = cell.text() if cell is not None else ''
		if cell is not None and cell.data(self.HAS_ORIGINAL_ROLE) and text == format_scalar(cell.data(self.ORIGINAL_ROLE)):
			return cell.data(self.ORIGINAL_ROLE)
		return parse_number(text)

	def _entry(self):
		"""The edited result group without its empty keys. Raises ``ValueError`` telling why it is not
		valid."""
		if self._target.currentData() is None:
			raise ValueError(self.tr('Pick the target of the result group'))
		group = {'tgt': self._target.currentData()}
		if self._configuration.currentData() is not None:
			group['cfg'] = self._configuration.currentData()
		if self._desc.text().strip():
			group['desc'] = self._desc.text().strip()

		cond = {}
		for row in self._conditions:
			if not row.name() or row.name() in cond:
				raise ValueError(self.tr('Each condition needs a name of its own'))
			quantity = row.quantity()
			if 'v' not in quantity:
				raise ValueError(self.tr('The condition %s needs a value') % row.name())
			cond[row.name()] = quantity
		if cond:
			group['cond'] = cond

		points = self._points.rowCount()
		for column, (kind, title) in enumerate(self.KINDS):
			row = self._columns[column]
			columns = {}
			if row.name() is None:
				if any(self._points.item(point, column) is not None and self._points.item(point, column).text().strip()
				       for point in range(points)):
					raise ValueError(self.tr('Name the %s quantity of the points') % self.tr(title).lower())
			elif points:
				try:
					values = [self._cellValue(point, column) for point in range(points)]
				except ValueError:
					raise ValueError(self.tr('Each point needs a number in every column'))
				# A single value given as a number stays one.
				if points == 1 and not isinstance(row.original().get('v'), list) and 'v' in row.original():
					values = values[0]
				quantity = row.quantity(values)
				unc = quantity.get('unc')
				if isinstance(unc, list) and len(unc) != points:
					raise ValueError(self.tr('The uncertainties of %s are not one per point') % row.name())
				columns[row.name()] = quantity
			for name, quantity in self._other_columns.get(kind, {}).items():
				columns.setdefault(name, quantity)
			if columns:
				group[kind] = columns

		# The model, recip, the poly scheme and exp first, the other schemes kept.
		comp = {key: value for key, value in self._comp.items() if key != 'poly' and key not in self.MODEL_KEYS}
		if self._include_model.isChecked():
			if self._poly is None:
				raise ValueError(self._fit_note.text() or self.tr('There is no compensation model'))
			comp = {**self._schemes(self._poly), **comp}
		if comp:
			group['comp'] = comp

		meq = [self._equipment.item(index).data(Qt.UserRole) for index in range(self._equipment.count())
		       if self._equipment.item(index).checkState() == Qt.Checked]
		if meq:
			group['meq'] = meq
		if self._method.text().strip():
			group['method'] = self._method.text().strip()
		return group

	@Slot()
	def _confirmEdit(self):
		if not self._confirm.isEnabled() or self._certificate is None:
			return
		try:
			entry = self._entry()
		except ValueError:
			return
		groups = self._groups()
		if self._editing is None:
			index = self._currentIndex()
			index = len(groups) if index is None else index + 1
			groups.insert(index, entry)
		else:
			index = self._editing
			old = groups[index]
			# The keys not edited here, e.g. the compensation model, are kept, in the order of the EP.
			kept = {key: value for key, value in old.items() if key not in self.EDITED}
			merged = {**entry, **kept}
			groups[index] = {key: merged[key] for key in self.ORDER if key in merged}
			groups[index].update({key: value for key, value in merged.items() if key not in self.ORDER})
		self._closeEdit()
		self._store(groups, select=index)

	@Slot()
	def _closeEdit(self):
		self._editing = None
		self._form_group.hide()
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		editing = self._form_group.isVisibleTo(self)
		index = self._currentIndex() if not editing else None
		count = self._list.topLevelItemCount()
		self._add.setEnabled(not editing and self._certificate is not None)
		self._edit_btn.setEnabled(index is not None)
		self._remove.setEnabled(index is not None)
		self._up.setEnabled(index is not None and index > 0)
		self._down.setEnabled(index is not None and index < count - 1)

	@Slot()
	def _updateConfirm(self):
		if self._filling:
			return
		try:
			self._entry()
		except ValueError as e:
			self._confirm.setEnabled(False)
			self._confirm.setToolTip(str(e))
			return
		self._confirm.setEnabled(True)
		self._confirm.setToolTip('')


class CalibrationCertificate(QWidget):
	"""The calibration certificate with its toolbars: New and the flash partition with Read and Write at the
	top, where the certificate comes from and its summary at the bottom. In between,
	the Certificate tab (see :class:`CertificateView`) and a tab for each top-level section of the
	certificate."""

	#: The colour of the status.
	STATUS_COLOR = '#888'

	def __init__(self, client, settings, read_firmware_version=None):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._client = client
		self._certificate = Certificate()
		# Where the certificate comes from, shown in the status.
		self._source = self.tr('New certificate')
		# Whether the certificate was changed since it was opened, read, saved or written.
		self._modified = False
		# The partitions by name with their details, None while they are being listed.
		self._partitions = None
		self._lister = None
		# The partition operation running (read, write) and its progress window, None when there is none.
		self._operation = None
		self._progress = None
		# The window showing the data of the certificate, None when there is none.
		self._data_window = None

		self._tb = QToolBar()
		new = DauiButton(self.tr('New'), Icons.add())
		new.setToolTip(self.tr('Start a new empty certificate'))
		new.clicked.connect(self.new)
		self._tb.addWidget(new)
		self._tb.addSeparator()

		label = QLabel(self.tr('Partition:'))
		label.setContentsMargins(6, 0, 4, 0)
		self._tb.addWidget(label)
		self._partition = QComboBox()
		self._partition.setToolTip(self.tr('The flash partition of the device holding the certificate'))
		self._partition.setMinimumWidth(140)
		self._partition.setFixedHeight(new.sizeHint().height())
		self._partition.currentIndexChanged.connect(self._updateButtons)
		self._tb.addWidget(self._partition)
		self._read = DauiButton(self.tr('Read'), Icons.read())
		self._read.setToolTip(self.tr('Read the certificate from the flash partition'))
		self._read.clicked.connect(self.readPartition)
		self._tb.addWidget(self._read)
		self._write = DauiButton(self.tr('Write'), Icons.write())
		self._write.setToolTip(self.tr('Write the certificate to the flash partition'))
		self._write.clicked.connect(self.writePartition)
		self._tb.addWidget(self._write)

		self._bottom = QToolBar()
		self._status = QLabel()
		self._status.setContentsMargins(6, 0, 0, 0)
		self._status.setStyleSheet(f'color: {self.STATUS_COLOR};')
		self._bottom.addWidget(self._status)
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._bottom.addWidget(spacer)
		self._summary = QLabel()
		self._summary.setContentsMargins(0, 0, 6, 0)
		self._summary.setStyleSheet(f'color: {self.STATUS_COLOR};')
		self._bottom.addWidget(self._summary)

		self._tabs = QTabWidget()
		self._tabs.setTabPosition(QTabWidget.North)
		self._tabs.setDocumentMode(True)
		self._certificate_view = CertificateView()
		self._certificate_view.changed.connect(self._changed)
		self._certificate_view.openRequested.connect(self.openFile)
		self._certificate_view.saveRequested.connect(self.saveFile)
		self._certificate_view.pdfRequested.connect(self.generatePdf)
		self._certificate_view.viewDataRequested.connect(self.viewData)
		index = self._tabs.addTab(TabScrollArea(self._certificate_view), Icons.certificate(), self.tr('Certificate'))
		self._tabs.setTabToolTip(index, self.tr('The certificate as a whole, its format, data and integrity'))
		self._sections = {}
		for key, title, icon, description in SECTIONS:
			if key == AdministrativeView.KEY:
				view = AdministrativeView(settings)
				view.changed.connect(self._changed)
				# Scrolled when the window is lower, the responsible persons may be many.
				tab = TabScrollArea(view)
			elif key in (TargetsView.KEY, ConfigurationsView.KEY, EquipmentView.KEY, ResultsView.KEY):
				view = {TargetsView.KEY: TargetsView, ConfigurationsView.KEY: ConfigurationsView,
				        EquipmentView.KEY: EquipmentView, ResultsView.KEY: ResultsView}[key]()
				view.changed.connect(self._changed)
				# Scrolled when the window is lower, the form is below the tree.
				tab = TabScrollArea(view)
			else:
				view = tab = SectionView(key, self.tr(title))
			index = self._tabs.addTab(tab, icon(), self.tr(title))
			self._tabs.setTabToolTip(index, self.tr(description))
			self._sections[key] = view

		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.addWidget(self._tb)
		layout.addWidget(self._tabs)
		layout.addWidget(self._bottom)

		self._setCertificate(self._certificate, self._source)
		self._listPartitions()
		self._firmware_reader = None
		if read_firmware_version is not None:
			self._firmware_reader = _FirmwareVersionReader(read_firmware_version)
			self._firmware_reader.done.connect(self._certificate_view.setFirmwareVersion)
			self._firmware_reader.start()

	def _listPartitions(self):
		self._partitions = None
		self._partition.clear()
		self._partition.addItem(self.tr('Loading...'))
		self._lister = _PartitionLister(self._client)
		self._lister.listed.connect(self._listed)
		self._lister.failed.connect(self._listFailed)
		self._lister.start()
		self._updateButtons()

	@Slot(list)
	def _listed(self, partitions):
		self._partitions = dict(partitions)
		self._partition.clear()
		for name, detail in partitions:
			self._partition.addItem(name if detail is None else f'{name} ({format_size(detail["size"])})', name)
		names = [name for name, _detail in partitions]
		default = DEFAULT_PARTITION if DEFAULT_PARTITION in names else next(
			(name for name in names if DEFAULT_PARTITION in name), None)
		if default is not None:
			self._partition.setCurrentIndex(names.index(default))
		self._updateButtons()

	@Slot(str)
	def _listFailed(self, message):
		self._logger.warning(f'listing the flash partitions failed: {message}')
		self._partitions = {}
		self._partition.clear()
		self._partition.addItem(self.tr('No partitions'))
		self._updateButtons()

	def _selectedPartition(self):
		"""The name and the details of the selected partition, ``(None, None)`` if there is none."""
		if not self._partitions:
			return None, None
		name = self._partition.currentData()
		return name, self._partitions.get(name)

	@Slot()
	def _updateButtons(self):
		# The flash service supports a single session, so one operation at a time.
		idle = self._operation is None
		_name, detail = self._selectedPartition()
		self._partition.setEnabled(idle and bool(self._partitions))
		self._read.setEnabled(idle and can_read(detail))
		self._write.setEnabled(idle and can_write(detail))

	def _setCertificate(self, certificate, source):
		self._certificate = certificate
		self._certificate_view.setCertificate(certificate)
		for view in self._sections.values():
			view.setCertificate(certificate)
		self._setSource(source)

	def _setSource(self, source):
		"""Tell the certificate is unmodified, coming from or stored in ``source``."""
		self._source = source
		self._modified = False
		self._show()

	@Slot()
	def _changed(self):
		self._modified = True
		self._show()

	def _show(self):
		"""Show where the certificate comes from and its summary."""
		status = self._source
		if self._modified:
			status += self.tr(' (modified)')
		self._status.setText(status)
		summary = []
		for key, name in (('tgt', self.tr('%d targets')), ('res', self.tr('%d results'))):
			section = self._certificate.section(key)
			summary.append(name % (len(section) if isinstance(section, (dict, list)) else 0))
		data = encode(self._certificate)
		summary.append(format_size(len(data)))
		self._summary.setText(', '.join(summary))
		self._certificate_view.setData(data)
		if self._data_window is not None:
			self._data_window.setData(data)

	@Slot()
	def viewData(self):
		"""Show the data of the certificate in a window, updated while the certificate changes."""
		if self._data_window is None:
			self._data_window = CertificateDataWindow(self.tr('Certificate data'))
			self._data_window.destroyed.connect(self._dataWindowClosed)
			self._data_window.setData(encode(self._certificate))
			self._showWindow(self._data_window)
		else:
			self._data_window.showNormal()
		if self._data_window.mdiArea() is not None:
			self._data_window.mdiArea().setActiveSubWindow(self._data_window)

	@Slot()
	def _dataWindowClosed(self):
		self._data_window = None

	def closeWindows(self):
		"""Close the windows opened, e.g. when the applet window closes."""
		if self._data_window is not None:
			self._data_window.close()

	def valueWritten(self):
		"""Tell a calibration value was written to the certificate, assigning the automatic dates."""
		self._sections[AdministrativeView.KEY].valueWritten()

	@Slot()
	def new(self):
		"""Start a new empty certificate after asking."""
		question = self.tr('Start a new empty certificate? The current one is discarded.')
		if self._modified:
			question = self.tr('Start a new empty certificate? The current one is discarded, its changes are not '
			                   'saved.')
		answer = QMessageBox.question(self, self.tr('New certificate'), question)
		if answer == QMessageBox.Yes:
			self._setCertificate(Certificate(), self.tr('New certificate'))

	@Slot()
	def openFile(self):
		path, _filter = QFileDialog.getOpenFileName(self, self.tr('Open certificate'), '', self.tr(FILE_FILTER))
		if not path:
			return
		name = display_name(path)
		try:
			certificate = decode(read_file(path))
		except (OSError, ValueError) as e:
			QMessageBox.warning(self, self.tr('Open certificate'), self.tr('Cannot open %s: %s') % (name, e))
			return
		self._logger.info(f'certificate opened from {path}')
		self._setCertificate(certificate, self.tr('Opened from %s') % name)

	@Slot()
	def saveFile(self):
		cert_id = (self._certificate.section('adm') or {}).get('id')
		default = f'{cert_id or "certificate"}.cbor'
		path, _filter = QFileDialog.getSaveFileName(self, self.tr('Save certificate'), default, self.tr(FILE_FILTER))
		if not path:
			return
		name = display_name(path)
		self._sections[AdministrativeView.KEY].issued()
		try:
			write_file(path, encode(self._certificate))
		except OSError as e:
			QMessageBox.warning(self, self.tr('Save certificate'), self.tr('Cannot write %s: %s') % (name, e))
			return
		self._logger.info(f'certificate saved to {path}')
		self._setSource(self.tr('Saved to %s') % name)

	@Slot()
	def generatePdf(self):
		"""Export the certificate as a PDF document, rendered off the GUI thread showing a busy window."""
		if self._operation is not None:
			return
		cert_id = (self._certificate.section('adm') or {}).get('id')
		default = f'{cert_id or "certificate"}.pdf'
		path, _filter = QFileDialog.getSaveFileName(self, self.tr('Generate PDF'), default, self.tr(PDF_FILTER))
		if not path:
			return
		name = display_name(path)
		# A copy, the certificate may be edited while the document is rendered.
		certificate = copy.deepcopy(self._certificate)
		data = encode(certificate)

		def render(progress, abort, stage):
			pdf = calibration_pdf.render_pdf(certificate, data, VERSION)
			if abort.is_set():
				raise Aborted()
			write_file(path, pdf)

		def done(ok):
			if ok:
				self._logger.info(f'certificate exported to {path}')

		self._runOperation(render, self.tr('Generate PDF'), self.tr('Generating the PDF document %s...') % name, 0,
		                   finished=done)

	@Slot()
	def readPartition(self):
		"""Read the certificate from the selected partition, showing the progress in a window."""
		name, detail = self._selectedPartition()
		if self._operation is not None or not can_read(detail):
			return
		size = detail['size']
		buffer = io.BytesIO()

		def done(ok):
			if not ok:
				return
			try:
				certificate = decode(buffer.getvalue(), padded=True)
			except ValueError as e:
				QMessageBox.warning(self, self.tr('Read certificate'),
				                    self.tr('The flash partition %s does not hold a certificate: %s') % (name, e))
				return
			self._setCertificate(certificate, self.tr('Read from partition %s') % name)

		# The certificate is at the start of the partition, the erased flash following it is not read.
		self._runOperation(lambda progress, abort, stage: self._client.read(name, size, buffer, progress, abort,
		                                                                    stop_sector=detail.get('erase')),
		                   self.tr('Read certificate'), self.tr('Reading the certificate from flash partition %s...') % name,
		                   size, finished=done)

	@Slot()
	def writePartition(self):
		"""Write the certificate to the selected partition after asking, showing the progress in a window."""
		name, detail = self._selectedPartition()
		if self._operation is not None or not can_write(detail):
			return
		data = encode(self._certificate)
		size, erase_size, page_size = detail['size'], detail['erase'], detail['page']
		if len(data) > size:
			QMessageBox.warning(self, self.tr('Write certificate'),
			                    self.tr('The certificate (%s) does not fit the flash partition %s (%s).') % (
				                    format_size(len(data)), name, format_size(size)))
			return
		answer = QMessageBox.warning(
			self, self.tr('Write certificate'),
			self.tr('Write the certificate (%s) to the flash partition %s (%s)? The whole partition is erased '
			        'first, all its content will be lost.') % (format_size(len(data)), name, format_size(size)),
			QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
		if answer != QMessageBox.Yes:
			return
		self._sections[AdministrativeView.KEY].issued()
		data = encode(self._certificate)
		if len(data) > size:
			QMessageBox.warning(self, self.tr('Write certificate'),
			                    self.tr('The certificate (%s) does not fit the flash partition %s (%s).') % (
				                    format_size(len(data)), name, format_size(size)))
			return

		stages = {
			'erase': self.tr('Erasing flash partition %s before writing...') % name,
			'write': self.tr('Writing the certificate to flash partition %s...') % name,
			'verify': self.tr('Verifying the certificate written to flash partition %s...') % name,
		}

		def done(ok):
			if ok:
				self._setSource(self.tr('Written to partition %s') % name)

		self._runOperation(
			lambda progress, abort, stage: self._client.write(name, size, erase_size, page_size, data,
			                                                  progress, abort, stage),
			self.tr('Write certificate'), stages['erase'], size, stages=stages, finished=done)

	def _runOperation(self, fn, title, text, size, stages=None, finished=None):
		"""Run the partition operation ``fn(progress, abort, stage)`` (see
		:class:`~pydaui.applets.flash._Operation`), showing its progress in a window titled ``title``.
		``stages`` maps the stages the operation reports to the texts describing them. ``finished`` is called
		with whether the operation succeeded once it ends."""
		self._progress = ProgressWindow(title, text, format=ByteFormat(size))
		self._progress.setProgress(0, size)
		self._progress.aborted.connect(self._abortOperation)
		self._showWindow(self._progress)

		def ended(error, aborted=False):
			self._operation = None
			self._progress.finish()
			self._progress = None
			if aborted:
				self._logger.info(f'{title.lower()} aborted')
			elif error is not None:
				self._logger.warning(f'{title.lower()} failed: {error}')
				QMessageBox.warning(self, title, self.tr('%s failed: %s') % (title, error))
			if finished is not None:
				finished(error is None and not aborted)
			self._updateButtons()

		def progress(done):
			if self._progress is not None:
				self._progress.setProgress(done)

		def stage(what, total):
			if self._progress is not None and stages and what in stages:
				self._progress.setText(stages[what])
				self._progress.setStep(list(stages).index(what) + 1, len(stages))
				self._progress.setFormat(ByteFormat(total))
				self._progress.setProgress(0, total)

		self._logger.info(title.lower())
		self._operation = _Operation(fn)
		self._operation.stage.connect(stage)
		self._operation.progress.connect(progress)
		self._operation.done.connect(lambda: ended(None))
		self._operation.aborted.connect(lambda: ended(None, aborted=True))
		self._operation.failed.connect(lambda message: ended(message))
		self._operation.start()
		self._updateButtons()

	def _showWindow(self, window):
		"""Show the ``window``, e.g. a progress, in the MDI area the applet window is in, centered over it."""
		subwindow = self.parent()
		while subwindow is not None and not isinstance(subwindow, QMdiSubWindow):
			subwindow = subwindow.parent()
		if subwindow is not None and subwindow.mdiArea() is not None:
			subwindow.mdiArea().addSubWindow(window)
			window.move(subwindow.geometry().center() - window.rect().center())
		window.show()

	@Slot()
	def _abortOperation(self):
		if self._operation is not None:
			self._operation.abort()
			self._progress.setText(self.tr('Aborting...'))
			self._progress.setAbortEnabled(False)

	def abort(self):
		"""Abort the operation running, if any, and close its progress window, e.g. when the applet window
		closes. Nothing is reported of the operation any more."""
		if self._lister is not None:
			self._lister.blockSignals(True)
		if self._firmware_reader is not None:
			self._firmware_reader.blockSignals(True)
		if self._operation is not None:
			self._operation.blockSignals(True)
			self._operation.abort()
			self._operation = None
			self._progress.finish()
			self._progress = None


class CalibrationWindow(AppletWindow):
	"""The calibration certificate of the connected device, kept in a flash partition served by an interface
	speaking the flash protocol.

	It opens its own side connection to the interface, closed with the window. The window closes when the
	device is disconnected.
	"""

	#: The size of the window, unless its content needs more.
	DEFAULT_SIZE = QSize(800, 500)

	def __init__(self, connection, name, settings):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self.setAttribute(Qt.WA_DeleteOnClose)

		self._logger.info(f'opening the calibration certificate on interface {name!r}')
		self._side = connection.connectSide(name, flash_service.MAIN_EP)
		self._connection = connection
		connection.connectionChanged.connect(self._connectionChanged)

		self.setWindowTitle(f'Calibration certificate ({name})')
		self._certificate = CalibrationCertificate(FlashClient(self._side, interface_mtu(connection, name)), settings,
		                                           self._firmwareVersionReader(connection))
		self.setWidget(self._certificate)
		# Whether the window was sized to its content, when first shown.
		self._sized = False

	@staticmethod
	def _firmwareVersionReader(connection):
		"""A function reading the firmware version from the conf tree of the device, ``None`` if it has
		none."""
		interface = connection.findInterface(conf.PROTOCOL)
		if interface is None:
			return None
		return lambda: conf.read_values(connection, interface, [FIRMWARE_VERSION_PATH])[0]

	@Slot(object)
	def _connectionChanged(self, uri):
		if uri is None:
			self.close()

	def showEvent(self, event):
		super().showEvent(event)
		# Large enough for the tabs, e.g. the administrative one, the tabs scroll when the window is resized
		# lower. Only once shown, the style and the MDI area change the sizes of the widgets until then.
		if not self._sized:
			self._sized = True
			self.resize(self.DEFAULT_SIZE.expandedTo(self.sizeHint()))

	def closeEvent(self, event):
		if self._connection is not None:
			self._connection.connectionChanged.disconnect(self._connectionChanged)
			self._connection = None
			self._certificate.abort()
			self._certificate.closeWindows()
			# Already disconnected together with the device connection, if that is why it closes.
			self._side.disconnectUri()
		super().closeEvent(event)
