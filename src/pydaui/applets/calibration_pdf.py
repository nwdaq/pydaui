# SPDX-License-Identifier: GPL-3.0-or-later
#
# Calibration certificate PDF export
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Export a calibration certificate (see :mod:`pydaui.applets.calibration`) as a printable PDF document.

The certificate is rendered to HTML with a Jinja2 template, by default :data:`TEMPLATE` in the assets, and
the HTML to PDF with WeasyPrint. The document shows the header and the administrative data of the
certificate, its calibration targets and its measurement results, followed by its encoded bytes as a Base64 dump and a QR code, their checksum and the
signatures. The template gets the variables made by :func:`context`.

WeasyPrint needs the Pango library of the system, it is imported only when a document is rendered.
"""

import base64
import datetime
import hashlib
from pathlib import Path

import jinja2
import segno

from pydaui import quantities

#: The default template of the document.
TEMPLATE = Path(__file__).resolve().parent.parent / 'assets' / 'certificate.html.j2'

#: The characters per line of the Base64 dump, as in MIME.
BASE64_LINE = 76

#: The checksum of the certificate bytes shown in the document.
CHECKSUM_ALGORITHM = 'BLAKE2s-256'


def checksum(data):
	"""The :data:`CHECKSUM_ALGORITHM` checksum of ``data`` in hex."""
	return hashlib.blake2s(data).hexdigest()


def group_hex(text, size=8):
	"""The hex ``text`` in groups of ``size`` characters separated by spaces, to be read aloud or typed."""
	return ' '.join(text[i:i + size] for i in range(0, len(text), size))


def base64_lines(data):
	"""``data`` in Base64 as lines of :data:`BASE64_LINE` characters."""
	text = base64.b64encode(data).decode('ascii')
	return [text[i:i + BASE64_LINE] for i in range(0, len(text), BASE64_LINE)]


def qr_code(data):
	"""The QR code of the bytes ``data`` as an SVG data URI, ``None`` if they do not fit the largest one."""
	try:
		qr = segno.make(data, micro=False)
	except segno.DataOverflowError:
		return None
	return qr.svg_data_uri(scale=4, border=4)


def format_date(text):
	"""The ISO 8601 date or date and time ``text`` of a certificate for display, e.g.
	``2026-06-25 09:00:00 UTC``, as it is if it is not one, ``None`` if it is absent."""
	if text is None:
		return None
	try:
		dt = datetime.datetime.fromisoformat(str(text))
	except ValueError:
		return str(text)
	if len(str(text)) <= len('yyyy-mm-dd'):
		return dt.strftime('%Y-%m-%d')
	if dt.utcoffset() == datetime.timedelta(0):
		return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
	return dt.strftime('%Y-%m-%d %H:%M:%S %z').rstrip()


def _party(value):
	"""The company ``value`` of the administrative data with the lines of its address, ``None`` if it is not
	one."""
	if not isinstance(value, dict):
		return None
	addr = str(value.get('addr', ''))
	return {**value, 'addr_lines': [line.strip() for line in addr.splitlines() if line.strip()]}


def _quantity(quantity, unit):
	"""The ``quantity`` of a target with its ``unit`` as a symbol for display, e.g. ``temperature [°C]``,
	``None`` if there is neither."""
	unit = quantities.unit_symbol(str(unit)) if unit is not None else None
	if quantity is None:
		return f'[{unit}]' if unit else None
	return f'{quantity} [{unit}]' if unit else str(quantity)


def _targets(certificate):
	"""The calibration targets of the ``certificate`` in the order of their tree, see :func:`context`."""
	# Imported here, the applet imports this module.
	from pydaui.applets.calibration import target_tree

	tgt = certificate.section('tgt')
	targets = []
	for key, target, depth in target_tree(tgt if isinstance(tgt, dict) else {}):
		fields = {k: str(v) for k, v in target.items() if k in ('n', 't', 'mfg', 'pn', 'sn') and v is not None}
		targets.append({**fields, 'id': str(key), 'depth': depth,
		                'measurand': _quantity(target.get('mq'), target.get('mu')),
		                'indication': _quantity(target.get('iq'), target.get('iu'))})
	return targets


def _number(value):
	"""A number of a quantity for display, ``value`` as text if it is not one."""
	if isinstance(value, float):
		return f'{value:.9g}'
	return str(value)


def _numbers(value):
	"""A value of a quantity for display, a list of values separated by commas."""
	if isinstance(value, (list, tuple)):
		return ', '.join(_number(item) for item in value)
	return _number(value)


def _uncertainty(quantity, unit):
	"""The uncertainty of the ``quantity`` map with its coverage factor for display, e.g. ``U = 0.3 °C (k =
	2)``, ``None`` if it has none."""
	if 'unc' not in quantity:
		return None
	text = f'U = {_numbers(quantity["unc"])}{" " + unit if unit else ""}'
	return f'{text} (k = {_number(quantity["k"])})' if 'k' in quantity else text


def _result(index, group, certificate):
	"""The result ``group`` at the ``index`` for the template, see :func:`context`."""

	def section(key):
		value = certificate.section(key)
		return value if isinstance(value, dict) else {}

	def unit_of(quantity):
		return quantities.unit_symbol(str(quantity['u'])) if 'u' in quantity else ''

	targets, configurations, equipment = section('tgt'), section('cfg'), section('meq')
	target = group.get('tgt')
	name = targets[target].get('n') if isinstance(targets.get(target), dict) else None
	result = {
		'index': index + 1,
		'desc': str(group['desc']) if 'desc' in group else None,
		'target': f'{name} ({target})' if name else str(target) if target is not None else None,
		'method': str(group['method']) if 'method' in group else None,
	}
	if 'cfg' in group:
		settings = configurations.get(group['cfg'])
		settings = ', '.join(f'{key} = {_numbers(value)}' for key, value in settings.items()) \
			if isinstance(settings, dict) else ''
		result['cfg'] = f'{group["cfg"]} ({settings})' if settings else str(group['cfg'])

	conditions = []
	cond = group.get('cond')
	for key, quantity in cond.items() if isinstance(cond, dict) else []:
		quantity = quantity if isinstance(quantity, dict) else {'v': quantity}
		unit = unit_of(quantity)
		text = f'{key}: {_numbers(quantity.get("v", ""))}{" " + unit if unit else ""}'
		uncertainty = _uncertainty(quantity, unit)
		conditions.append(f'{text}, {uncertainty}' if uncertainty else text)
	result['conditions'] = conditions

	meq = group.get('meq')
	result['equipment'] = [
		f'{equipment[key].get("n")} ({key})' if isinstance(equipment.get(key), dict) and equipment[key].get('n')
		else str(key) for key in (meq if isinstance(meq, list) else [])]

	# The reference and measured columns with their values, a per-point uncertainty with each value.
	columns, values = [], []
	for kind, title in (('ref', 'Reference'), ('meas', 'Measured')):
		group_columns = group.get(kind)
		for key, quantity in group_columns.items() if isinstance(group_columns, dict) else []:
			quantity = quantity if isinstance(quantity, dict) else {'v': quantity}
			unit = unit_of(quantity)
			value = quantity.get('v')
			value = value if isinstance(value, list) else [] if value is None else [value]
			unc = quantity.get('unc')
			if isinstance(unc, list):
				value = [f'{_number(v)} ± {_number(u)}' for v, u in zip(value, unc)] + \
				        [_number(v) for v in value[len(unc):]]
				note = f'(k = {_number(quantity["k"])})' if 'k' in quantity else None
			else:
				value = [_number(v) for v in value]
				note = _uncertainty(quantity, unit)
			columns.append({'kind': title, 'name': str(key), 'unit': unit, 'note': note})
			values.append(value)
	count = max((len(column) for column in values), default=0)
	result['columns'] = columns
	result['rows'] = [[column[i] if i < len(column) else '' for column in values] for i in range(count)]
	return result


def _results(certificate):
	"""The result groups of the ``certificate`` which are maps, see :func:`context`."""
	res = certificate.section('res')
	return [_result(index, group, certificate)
	        for index, group in enumerate(res if isinstance(res, list) else []) if isinstance(group, dict)]


def context(certificate, data, version):
	"""The variables of the template for the ``certificate`` encoded as ``data`` in the format ``version``:

	- ``adm``: the administrative data, the dates formatted with :func:`format_date`, ``lab`` and ``cst``
	  with the ``addr_lines`` of the address, ``rsp`` the list of the responsible persons, ``sw`` the list of
  the software, ``rem`` the list of the remarks
	- ``targets``: the calibration targets in the order of their tree, each a map of its text fields (``n``,
	  ``t``, ``mfg``, ``pn``, ``sn``), its ``id``, its ``depth`` in the tree (0 without a parent) and its
	  ``measurand`` and ``indication`` with their units, e.g. ``temperature [°C]``
	- ``results``: the result groups, each a map of its ``index`` (from 1), ``desc``, ``target`` and ``cfg``
	  (with its settings) and ``method`` as texts, ``None`` if absent, the lists of the texts of its
	  ``conditions`` and its ``equipment``, its reference and measured ``columns`` as maps of their ``kind``,
	  ``name``, ``unit`` and ``note`` (the uncertainty of the whole column), and the ``rows`` of the points,
	  a list of the texts of the cells each
	- ``version``, ``size``: the format version and the size of the encoded certificate in bytes
	- ``base64``: the lines of the Base64 dump, ``qr``: the QR code as a data URI, ``None`` if too large
	- ``checksum``, ``checksum_algorithm``: the checksum of the encoded certificate in groups of hex digits
	- ``signed``, ``signing``: whether the certificate is signed and the signing entities as ``(role, name,
	  method)``, ``signatures``: the bytes of the signatures in groups of hex digits
	- ``generated``: when the document was made
	"""
	adm = certificate.section('adm')
	adm = dict(adm) if isinstance(adm, dict) else {}
	for key in ('db', 'de', 'di'):
		adm[key] = format_date(adm.get(key))
	adm['lab'] = _party(adm.get('lab'))
	adm['cst'] = _party(adm.get('cst'))
	rsp = adm.get('rsp')
	adm['rsp'] = [person for person in rsp if isinstance(person, dict)] if isinstance(rsp, list) else []
	sw = adm.get('sw')
	adm['sw'] = [entry for entry in sw if isinstance(entry, dict)] if isinstance(sw, list) else []
	rem = adm.get('rem')
	adm['rem'] = [str(remark) for remark in rem] if isinstance(rem, list) else []

	signing = []
	for role, entity in (('Laboratory', adm['lab']), ('Customer', adm['cst'])):
		if entity is not None:
			signing.append((role, entity.get('n', ''), entity.get('sign')))
	for person in adm['rsp']:
		signing.append((person.get('role') or 'Responsible person', person.get('n', ''), person.get('sign')))

	return {
		'adm': adm,
		'targets': _targets(certificate),
		'results': _results(certificate),
		'version': version,
		'size': len(data),
		'base64': base64_lines(data),
		'qr': qr_code(data),
		'checksum': group_hex(checksum(data)),
		'checksum_algorithm': CHECKSUM_ALGORITHM,
		'signed': certificate.signed,
		'signing': signing,
		'signatures': group_hex(certificate.signatures.hex()) if certificate.signed else '',
		'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
	}


def render_html(certificate, data, version, template=TEMPLATE):
	"""The ``certificate`` encoded as ``data`` rendered with the Jinja2 ``template`` file."""
	template = Path(template)
	env = jinja2.Environment(loader=jinja2.FileSystemLoader(template.parent), autoescape=True,
	                         trim_blocks=True, lstrip_blocks=True)
	return env.get_template(template.name).render(context(certificate, data, version))


def render_pdf(certificate, data, version, template=TEMPLATE):
	"""The PDF document of the ``certificate`` encoded as ``data``, see :func:`render_html`."""
	import weasyprint

	html = render_html(certificate, data, version, template)
	return weasyprint.HTML(string=html, base_url=str(Path(template).parent)).write_pdf()
