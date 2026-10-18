# SPDX-License-Identifier: GPL-3.0-or-later
#
# The compensation model of a calibration result group
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The polynomial compensation model (``comp``) of a result group of a calibration certificate, evaluated as
the mq-compensation service of plumCore evaluates it.

The keys ``recip`` and ``exp`` of the model, next to its ``poly`` scheme, are not specified by the EP yet, they
are supported by the mq-compensation service (its reciprocal and exp parameters): with ``recip``, the measured
value is replaced by its reciprocal before ``x0`` of the poly scheme is subtracted, the value of the polynomial
is raised to the power of ``exp`` preserving its sign, sign(p)·|p|^exp. Together they model hyperbolic and
power-law curves, e.g. a resistance measured by a voltage divider.

The model is handled as a single map here, the poly scheme with the ``recip`` and ``exp`` keys, see
:func:`model_of` and :func:`schemes`.
"""

import numpy as np

#: The keys of the model next to its poly scheme, in the order they are evaluated.
MODEL_KEYS = ('recip', 'exp')

#: The reciprocal of a zero, saturated as by the mq-compensation service (FLT_MAX).
RECIP_OF_ZERO = float(np.finfo(np.float32).max)


def model_of(comp):
	"""The polynomial model of the model ``comp`` of a group as a single map, its poly scheme with the
	``recip`` and ``exp`` keys of the model. ``None`` if it has no poly scheme with coefficients."""
	poly = comp.get('poly') if isinstance(comp, dict) else None
	if not isinstance(poly, dict) or not isinstance(poly.get('c'), list) or not poly['c']:
		return None
	model = {key: comp[key] for key in MODEL_KEYS if key in comp}
	model.update({key: value for key, value in poly.items() if key not in MODEL_KEYS})
	return model


def schemes(model):
	"""The keys of the model (comp) of the single map ``model``: ``recip``, the poly scheme and ``exp``."""
	comp = {'recip': model['recip']} if 'recip' in model else {}
	comp['poly'] = {key: value for key, value in model.items() if key not in MODEL_KEYS}
	if 'exp' in model:
		comp['exp'] = model['exp']
	return comp


def transform(x, recip):
	"""The measured indications ``x`` the polynomial is evaluated at, their reciprocals if ``recip``, the
	reciprocal of a zero saturated to :data:`RECIP_OF_ZERO`."""
	if not recip:
		return x
	with np.errstate(divide='ignore'):
		return np.where(x == 0, RECIP_OF_ZERO, 1.0 / np.where(x == 0, 1, x))


def fittable(x, recip):
	"""The measured indications ``x`` the polynomial can be fitted at: not a zero with ``recip``, its
	reciprocal is only saturated."""
	return np.isfinite(x) & (x != 0) if recip else np.isfinite(x)


def power(values, exp):
	"""The ``values`` raised to the power of ``exp`` preserving their signs, as the mq-compensation service
	does: an infinity to a negative power is a zero, a zero an infinity."""
	if exp == 1:
		return values
	with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
		return np.copysign(np.power(np.abs(values), exp), values)


def predict(model, x):
	"""The reference values the ``model`` predicts at the measured indications ``x``."""
	xr = transform(x, bool(model.get('recip'))) - float(model.get('x0', 0))
	# Horner's method as the service evaluates it, starting from the highest coefficient.
	coefficients = [float(c) for c in model['c']]
	values = np.full(np.shape(xr), coefficients[-1])
	with np.errstate(invalid='ignore', over='ignore'):
		for c in reversed(coefficients[:-1]):
			values = values * xr + c
	return power(values, float(model.get('exp', 1)))


def formula(model, format_number=str):
	"""The right side of the formula of the ``model`` as text, e.g. ``sign(p)·|p(1/x - x0)|^-1``, its exponent
	formatted with ``format_number``."""
	form = '1/x' if model.get('recip') else 'x'
	form = f'p({form})' if 'x0' not in model else f'p({form} - x0)'
	if float(model.get('exp', 1)) != 1:
		form = f'sign(p)·|{form}|^{format_number(model["exp"])}'
	return form
