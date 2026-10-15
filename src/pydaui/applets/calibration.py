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
import secrets
import threading
import uuid

import cbor2
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from pynbus2.services import flash as flash_service

from pydaui.applets.flash import FlashClient, _Operation, can_read, can_write, format_size, interface_mtu
from pydaui.files import display_name, read_file, write_file
from pydaui.ui.helpers import Admonition, AppletWindow, DauiButton, StyledTreeWidget, TabScrollArea
from pydaui.ui.icons import Icons
from pydaui.ui.progress_window import ByteFormat, ProgressWindow
from pydaui.ui.settings_window import CalibrationCompaniesPage, CalibrationPersonsPage


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

#: The top-level sections of a certificate as (key, title, icon, description).
SECTIONS = (
	('adm', 'Administrative', Icons.company, 'The certificate, the issuing laboratory and the calibration event'),
	('tgt', 'Targets', Icons.chip, 'The calibrated entities, e.g. a sensor or an ADC'),
	('cfg', 'Configurations', Icons.conf, 'The device configurations the results were taken under'),
	('meq', 'Equipment', Icons.probe, 'The measuring equipment and reference standards used'),
	('res', 'Results', Icons.data, 'The measurement results grouped by the influence conditions'),
)


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
	"""The Certificate tab, the certificate as a whole: its format and its data, opening and saving it, and
	its integrity control. The buttons ask the :class:`CalibrationCertificate` with their signals, ``changed``
	tells the user changed the certificate."""

	changed = Signal()
	openRequested = Signal()
	saveRequested = Signal()
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
		files = QGroupBox(self.tr('Import/Export'))
		files_row = QHBoxLayout(files)
		files_row.addWidget(open_file)
		files_row.addWidget(save)
		files_row.addStretch(1)

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
		layout.addWidget(integrity)
		layout.addStretch(1)

	def setCertificate(self, certificate):
		self._certificate = certificate
		self._signed.blockSignals(True)
		self._signed.setChecked(certificate.signed)
		self._signed.blockSignals(False)

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

		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(dates)
		layout.addStretch(1)
		self._updateGenerate()
		self._loadKnown()

	def setCertificate(self, certificate):
		self._certificate = certificate
		adm = certificate.section(self.KEY) or {}
		self._id.setText(format_scalar(adm['id']) if 'id' in adm else '')
		self._location.setText(format_scalar(adm['loc']) if 'loc' in adm else '')
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


class CalibrationCertificate(QWidget):
	"""The calibration certificate with its toolbars: New and the flash partition with Read and Write at the
	top, where the certificate comes from and its summary at the bottom. In between,
	the Certificate tab (see :class:`CertificateView`) and a tab for each top-level section of the
	certificate."""

	#: The colour of the status.
	STATUS_COLOR = '#888'

	def __init__(self, client, settings):
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

		self._runOperation(lambda progress, abort, stage: self._client.read(name, size, buffer, progress, abort),
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
		self._certificate = CalibrationCertificate(FlashClient(self._side, interface_mtu(connection, name)), settings)
		self.setWidget(self._certificate)
		# Whether the window was sized to its content, when first shown.
		self._sized = False

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
