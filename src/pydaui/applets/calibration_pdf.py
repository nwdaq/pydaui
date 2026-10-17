# SPDX-License-Identifier: GPL-3.0-or-later
#
# Calibration certificate PDF export
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Export a calibration certificate (see :mod:`pydaui.applets.calibration`) as a printable PDF document.

The certificate is rendered to HTML with a Jinja2 template, by default :data:`TEMPLATE` in the assets, and
the HTML to PDF with WeasyPrint. The document shows the header and the administrative data of the
certificate, its calibration targets and its measurement results with their compensation models and their
graphs, followed by its encoded bytes as a Base64 dump and a QR code, their checksum and the signatures. The
template gets the variables made by :func:`context`.

WeasyPrint needs the Pango library of the system, it is imported only when a document is rendered.
"""

import base64
import datetime
import hashlib
import math
from pathlib import Path
from xml.sax.saxutils import escape

import jinja2
import numpy as np
import segno

from pydaui import compensation, quantities
from pydaui.ui.plot import GRAPH_COLORS

#: The default template of the document.
TEMPLATE = Path(__file__).resolve().parent.parent / 'assets' / 'certificate.html.j2'

#: The checksum of the certificate bytes shown in the document.
CHECKSUM_ALGORITHM = 'BLAKE2s-256'

#: The size of the graph of a compensation model in SVG user units, its margins around the plot area (left,
#: top, right, bottom) and the font size of its text.
GRAPH_SIZE = (640, 280)
GRAPH_MARGINS = (78, 12, 14, 42)
GRAPH_FONT_SIZE = 10

#: The number of the points the model curve is drawn with.
GRAPH_CURVE_POINTS = 400

#: The colours of the graph, as in the applet (see :class:`pydaui.ui.plot.Plot`).
GRAPH_AXIS_COLOR = '#888'
GRAPH_TEXT_COLOR = '#333'
GRAPH_GRID_COLOR = '#e4e4e4'


def checksum(data):
	"""The :data:`CHECKSUM_ALGORITHM` checksum of ``data`` in hex."""
	return hashlib.blake2s(data).hexdigest()


def group_hex(text, size=8):
	"""The hex ``text`` in groups of ``size`` characters separated by spaces, to be read aloud or typed."""
	return ' '.join(text[i:i + size] for i in range(0, len(text), size))


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


def _ticks(low, high, count=6):
	"""About ``count`` round values spanning ``low`` to ``high`` for the ticks of an axis."""
	span = high - low
	raw = span / max(count - 1, 1)
	magnitude = 10 ** math.floor(math.log10(raw))
	step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
	first = math.ceil(low / step - 1e-9)
	ticks = []
	tick = first * step
	while tick <= high + step * 1e-9:
		ticks.append(0.0 if abs(tick) < step * 1e-9 else tick)
		first += 1
		tick = first * step
	return ticks


def _range(values):
	"""The range of the finite ``values`` with a margin, widened if it is empty or a single value."""
	values = values[np.isfinite(values)]
	if not len(values):
		return 0.0, 1.0
	low, high = float(values.min()), float(values.max())
	if high == low:
		margin = abs(low) * 0.1 or 1.0
	else:
		margin = (high - low) * 0.05
	return low - margin, high + margin


def model_graph(x, y, model, x_label, y_label):
	"""The graph of the ``model`` with the points ``x``, ``y`` as an SVG data URI, the axes labelled
	``x_label`` and ``y_label``. The curve spans the measured indications, its values far outside of the
	reference values are clipped. ``None`` if there are no points."""
	if not len(x):
		return None
	width, height = GRAPH_SIZE
	left, top, right, bottom = GRAPH_MARGINS
	plot_w, plot_h = width - left - right, height - top - bottom
	low, high = float(x.min()), float(x.max())
	curve_x = np.linspace(low, high, GRAPH_CURVE_POINTS) if high > low else np.array([low])
	curve_y = compensation.predict(model, curve_x)

	x_low, x_high = _range(x)
	y_low, y_high = _range(np.concatenate([y, curve_y]))
	# The curve may run off to an infinity (e.g. a reciprocal near zero), the points stay in view.
	p_low, p_high = _range(y)
	span = p_high - p_low
	y_low, y_high = max(y_low, p_low - span), min(y_high, p_high + span)

	def px(value):
		return left + (value - x_low) / (x_high - x_low) * plot_w

	def py(value):
		return top + (y_high - value) / (y_high - y_low) * plot_h

	font = f'font-family="DejaVu Sans Mono, Liberation Mono, monospace" font-size="{GRAPH_FONT_SIZE}"'
	parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
	         f'height="{height}">',
	         f'<defs><clipPath id="area"><rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}"/>'
	         f'</clipPath></defs>',
	         f'<g {font} fill="{GRAPH_TEXT_COLOR}">']
	for tick in _ticks(x_low, x_high):
		parts.append(f'<line x1="{px(tick):.2f}" y1="{top}" x2="{px(tick):.2f}" y2="{top + plot_h}" '
		             f'stroke="{GRAPH_GRID_COLOR}" stroke-width="0.75"/>')
		parts.append(f'<text x="{px(tick):.2f}" y="{top + plot_h + GRAPH_FONT_SIZE + 4}" '
		             f'text-anchor="middle">{escape(f"{tick:.7g}")}</text>')
	for tick in _ticks(y_low, y_high):
		parts.append(f'<line x1="{left}" y1="{py(tick):.2f}" x2="{left + plot_w}" y2="{py(tick):.2f}" '
		             f'stroke="{GRAPH_GRID_COLOR}" stroke-width="0.75"/>')
		parts.append(f'<text x="{left - 5}" y="{py(tick) + GRAPH_FONT_SIZE * 0.35:.2f}" '
		             f'text-anchor="end">{escape(f"{tick:.7g}")}</text>')
	parts.append(f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="none" '
	             f'stroke="{GRAPH_AXIS_COLOR}" stroke-width="1"/>')
	parts.append(f'<text x="{left + plot_w / 2:.2f}" y="{height - 6}" text-anchor="middle">'
	             f'{escape(x_label)}</text>')
	parts.append(f'<text transform="translate(12 {top + plot_h / 2:.2f}) rotate(-90)" text-anchor="middle">'
	             f'{escape(y_label)}</text>')
	parts.append('</g>')

	# The curve, broken where its values are not finite.
	path, pen_down = [], False
	for cx, cy in zip(curve_x, curve_y):
		if not np.isfinite(cy):
			pen_down = False
			continue
		path.append(f'{"L" if pen_down else "M"}{px(cx):.2f} {py(cy):.2f}')
		pen_down = True
	parts.append(f'<g clip-path="url(#area)">')
	if path:
		parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{GRAPH_COLORS[1]}" stroke-width="1.5" '
		             f'stroke-linejoin="round"/>')
	for point_x, point_y in zip(x, y):
		parts.append(f'<circle cx="{px(point_x):.2f}" cy="{py(point_y):.2f}" r="3.5" fill="{GRAPH_COLORS[0]}"/>')
	parts.append('</g>')

	# The legend in the top left corner.
	lx, ly = left + 10, top + 10
	parts.append(f'<g {font} fill="{GRAPH_TEXT_COLOR}">'
	             f'<rect x="{lx}" y="{ly}" width="86" height="38" fill="#fff" fill-opacity="0.85" '
	             f'stroke="#d6d6d6"/>'
	             f'<circle cx="{lx + 14}" cy="{ly + 12}" r="3.5" fill="{GRAPH_COLORS[0]}"/>'
	             f'<text x="{lx + 28}" y="{ly + 15.5}">Points</text>'
	             f'<line x1="{lx + 6}" y1="{ly + 27}" x2="{lx + 22}" y2="{ly + 27}" stroke="{GRAPH_COLORS[1]}" '
	             f'stroke-width="1.5"/>'
	             f'<text x="{lx + 28}" y="{ly + 30.5}">Model</text></g>')
	parts.append('</svg>')
	svg = ''.join(parts)
	return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode('utf-8')).decode('ascii')


def _first_column(columns):
	"""The name, the values as an array and the unit symbol of the first quantity of the reference or the
	measured ``columns`` of a group, as the applet fits the model to, ``None`` if there is none."""
	if not isinstance(columns, dict) or not columns:
		return None
	name, quantity = next(iter(columns.items()))
	quantity = quantity if isinstance(quantity, dict) else {'v': quantity}
	value = quantity.get('v')
	value = value if isinstance(value, list) else [] if value is None else [value]
	try:
		values = np.array([float(v) for v in value])
	except (TypeError, ValueError):
		return None
	unit = quantities.unit_symbol(str(quantity['u'])) if 'u' in quantity else ''
	return str(name), values, unit


def _model(group):
	"""The compensation model of the result ``group`` for the template, see :func:`context`, ``None`` if it
	has no polynomial model."""
	model = compensation.model_of(group.get('comp'))
	if model is None:
		return None
	try:
		coefficients = [float(c) for c in model['c']]
	except (TypeError, ValueError):
		return None
	result = {
		'formula': 'y = ' + compensation.formula(model, _number),
		'coefficients': [(f'c{index}', _number(c)) for index, c in enumerate(coefficients)],
		'x0': _number(model['x0']) if 'x0' in model else None,
		'residuals': None,
		'graph': None,
		# The residuals of the points in the order of the group and their unit, see _result().
		'points': None,
		'unit': '',
	}
	ref, meas = _first_column(group.get('ref')), _first_column(group.get('meas'))
	if ref is None or meas is None:
		return result
	(y_name, y, y_unit), (x_name, x, x_unit) = ref, meas
	count = min(len(x), len(y))
	x, y = x[:count], y[:count]
	result['unit'] = y_unit
	try:
		result['points'] = y - compensation.predict(model, x)
	except (TypeError, ValueError, KeyError, ZeroDivisionError):
		pass
	finite = np.isfinite(x) & np.isfinite(y)
	x, y = x[finite], y[finite]
	try:
		residuals = y - compensation.predict(model, x)
	except (TypeError, ValueError, KeyError, ZeroDivisionError):
		return result
	residuals = residuals[np.isfinite(residuals)]
	if len(residuals):
		unit = f' {y_unit}' if y_unit else ''
		result['residuals'] = (f'RMS {float(np.sqrt(np.mean(residuals ** 2))):.3g}{unit}, '
		                       f'max {float(np.abs(residuals).max()):.3g}{unit}')

	def label(name, unit):
		return f'{name} [{unit}]' if unit else name

	result['graph'] = model_graph(x, y, model, label(x_name, x_unit), label(y_name, y_unit))
	return result


#: The decimal places of an error in %, a smaller one is shown as below the smallest one.
ERROR_DECIMALS = 3


def _percent(value):
	"""The error ``value`` in % for display, to :data:`ERROR_DECIMALS` places, e.g. ``-0.125``, ``< 0.001`` if
	its magnitude is below that, empty if it is not finite."""
	if not np.isfinite(value):
		return ''
	smallest = 10 ** -ERROR_DECIMALS
	if abs(value) < smallest:
		return f'< {smallest:.{ERROR_DECIMALS}f}'
	return f'{value:.{ERROR_DECIMALS}f}'


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
	# The residual of each point from the model, and it as a percentage of the reference value.
	model = _model(group)
	residuals = model.pop('points') if model is not None else None
	if residuals is not None:
		reference = _first_column(group.get('ref'))[1]
		unit = model.pop('unit')
		columns.append({'kind': 'Model', 'name': 'Residual', 'unit': unit, 'note': 'reference − model'})
		columns.append({'kind': 'Model', 'name': 'Error', 'unit': '%', 'note': 'of the reference'})
		values.append([f'{r:.4g}' if np.isfinite(r) else '' for r in residuals])
		with np.errstate(divide='ignore', invalid='ignore'):
			errors = residuals / np.abs(reference[:len(residuals)]) * 100
		values.append([_percent(e) for e in errors])
	elif model is not None:
		model.pop('unit')
	count = max((len(column) for column in values), default=0)
	result['columns'] = columns
	result['rows'] = [[column[i] if i < len(column) else '' for column in values] for i in range(count)]
	result['model'] = model
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
	  ``name``, ``unit`` and ``note`` (the uncertainty of the whole column), with a model the residual and
	  the error in % of the points, and the ``rows`` of the points, a list of the texts of the cells each,
	  and its compensation ``model``, ``None`` if it has none, a map of its ``formula``, its ``coefficients``
	  as ``(name, value)``, its centering offset ``x0`` (``None`` if absent), the ``residuals`` at the points
	  and the ``graph`` of it and the points as an SVG data URI (``None`` if they cannot be computed)
	- ``version``, ``size``: the format version and the size of the encoded certificate in bytes
	- ``base64``: the certificate in Base64, ``qr``: the QR code as a data URI, ``None`` if too large
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
		'base64': base64.b64encode(data).decode('ascii'),
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
