# SPDX-License-Identifier: GPL-3.0-or-later
#
# Physical quantities and their units
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The physical quantities offered e.g. as the measurand and the indication of a calibration target, with the
units each is commonly expressed in.

A quantity is named in lower case English, as in a calibration certificate (plumCore EP-7d3), e.g.
``temperature``. A unit is kept in the D-SI notation the certificate recommends, e.g. ``\\degreecelsius``, and
shown as its symbol, e.g. ``°C``. See :func:`unit_symbol` and :func:`unit_code` to convert between them.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
	#: The unit in the D-SI notation, e.g. ``\\kilo\\pascal``.
	dsi: str
	#: The symbol shown, e.g. ``kPa``.
	symbol: str
	#: The name of the unit, e.g. ``kilopascal``.
	name: str


@dataclass(frozen=True)
class Quantity:
	#: The name of the quantity, e.g. ``pressure``.
	name: str
	#: The units the quantity is expressed in, the most common one first.
	units: tuple


def _units(*units):
	return tuple(Unit(*unit) for unit in units)


#: The quantities offered, sorted by name.
QUANTITIES = (
	Quantity('acceleration', _units(
		('\\metre\\second\\tothe{-2}', 'm/s²', 'metre per second squared'),
	)),
	Quantity('angle', _units(
		('\\degree', '°', 'degree'),
		('\\radian', 'rad', 'radian'),
	)),
	Quantity('angular velocity', _units(
		('\\radian\\second\\tothe{-1}', 'rad/s', 'radian per second'),
		('\\degree\\second\\tothe{-1}', '°/s', 'degree per second'),
	)),
	Quantity('capacitance', _units(
		('\\farad', 'F', 'farad'),
		('\\micro\\farad', 'µF', 'microfarad'),
		('\\nano\\farad', 'nF', 'nanofarad'),
		('\\pico\\farad', 'pF', 'picofarad'),
	)),
	Quantity('charge', _units(
		('\\coulomb', 'C', 'coulomb'),
		('\\pico\\coulomb', 'pC', 'picocoulomb'),
	)),
	Quantity('code', _units(
		('\\one', '1', 'one, a dimensionless count, e.g. of an ADC'),
	)),
	Quantity('current', _units(
		('\\ampere', 'A', 'ampere'),
		('\\milli\\ampere', 'mA', 'milliampere'),
		('\\micro\\ampere', 'µA', 'microampere'),
		('\\nano\\ampere', 'nA', 'nanoampere'),
	)),
	Quantity('energy', _units(
		('\\joule', 'J', 'joule'),
		('\\kilo\\joule', 'kJ', 'kilojoule'),
		('\\watt\\hour', 'Wh', 'watt hour'),
		('\\kilo\\watt\\hour', 'kWh', 'kilowatt hour'),
	)),
	Quantity('force', _units(
		('\\newton', 'N', 'newton'),
		('\\kilo\\newton', 'kN', 'kilonewton'),
	)),
	Quantity('frequency', _units(
		('\\hertz', 'Hz', 'hertz'),
		('\\kilo\\hertz', 'kHz', 'kilohertz'),
		('\\mega\\hertz', 'MHz', 'megahertz'),
	)),
	Quantity('illuminance', _units(
		('\\lux', 'lx', 'lux'),
	)),
	Quantity('inductance', _units(
		('\\henry', 'H', 'henry'),
		('\\milli\\henry', 'mH', 'millihenry'),
		('\\micro\\henry', 'µH', 'microhenry'),
	)),
	Quantity('length', _units(
		('\\metre', 'm', 'metre'),
		('\\milli\\metre', 'mm', 'millimetre'),
		('\\micro\\metre', 'µm', 'micrometre'),
	)),
	Quantity('magnetic flux density', _units(
		('\\tesla', 'T', 'tesla'),
		('\\milli\\tesla', 'mT', 'millitesla'),
		('\\micro\\tesla', 'µT', 'microtesla'),
	)),
	Quantity('mass', _units(
		('\\kilogram', 'kg', 'kilogram'),
		('\\gram', 'g', 'gram'),
		('\\milli\\gram', 'mg', 'milligram'),
	)),
	Quantity('power', _units(
		('\\watt', 'W', 'watt'),
		('\\milli\\watt', 'mW', 'milliwatt'),
		('\\kilo\\watt', 'kW', 'kilowatt'),
	)),
	Quantity('pressure', _units(
		('\\pascal', 'Pa', 'pascal'),
		('\\hecto\\pascal', 'hPa', 'hectopascal'),
		('\\kilo\\pascal', 'kPa', 'kilopascal'),
		('\\mega\\pascal', 'MPa', 'megapascal'),
		('\\bar', 'bar', 'bar'),
		('\\milli\\bar', 'mbar', 'millibar'),
	)),
	Quantity('ratio', _units(
		('\\one', '1', 'one'),
		('\\percent', '%', 'percent'),
		('\\ppm', 'ppm', 'parts per million'),
	)),
	Quantity('relative humidity', _units(
		('\\percent', '%', 'percent'),
	)),
	Quantity('resistance', _units(
		('\\ohm', 'Ω', 'ohm'),
		('\\milli\\ohm', 'mΩ', 'milliohm'),
		('\\kilo\\ohm', 'kΩ', 'kiloohm'),
		('\\mega\\ohm', 'MΩ', 'megaohm'),
	)),
	Quantity('temperature', _units(
		('\\degreecelsius', '°C', 'degree Celsius'),
		('\\kelvin', 'K', 'kelvin'),
	)),
	Quantity('time', _units(
		('\\second', 's', 'second'),
		('\\milli\\second', 'ms', 'millisecond'),
		('\\micro\\second', 'µs', 'microsecond'),
	)),
	Quantity('velocity', _units(
		('\\metre\\second\\tothe{-1}', 'm/s', 'metre per second'),
	)),
	Quantity('voltage', _units(
		('\\volt', 'V', 'volt'),
		('\\milli\\volt', 'mV', 'millivolt'),
		('\\micro\\volt', 'µV', 'microvolt'),
		('\\kilo\\volt', 'kV', 'kilovolt'),
	)),
	Quantity('volume flow', _units(
		('\\metre\\tothe{3}\\second\\tothe{-1}', 'm³/s', 'cubic metre per second'),
		('\\litre\\minute\\tothe{-1}', 'l/min', 'litre per minute'),
	)),
)

#: The quantities by name.
BY_NAME = {quantity.name: quantity for quantity in QUANTITIES}

#: All the units once each, in the order of the quantities.
UNITS = tuple({unit.dsi: unit for quantity in QUANTITIES for unit in quantity.units}.values())

_BY_DSI = {unit.dsi: unit for unit in UNITS}
_BY_SYMBOL = {unit.symbol: unit for unit in UNITS}


def units(quantity):
	"""The units of the ``quantity`` named, all the units if it is not one of :data:`QUANTITIES`."""
	known = BY_NAME.get(quantity)
	return known.units if known is not None else UNITS


def find_unit(text):
	"""The :class:`Unit` of the D-SI notation or the symbol ``text``, ``None`` if it is not a known one."""
	return _BY_DSI.get(text) or _BY_SYMBOL.get(text)


def unit_symbol(text):
	"""The symbol of the unit ``text`` in the D-SI notation or as a symbol, ``text`` as it is if it is not a
	known unit."""
	unit = find_unit(text)
	return unit.symbol if unit is not None else text


def unit_code(text):
	"""The D-SI notation of the unit ``text`` given as a symbol or in the D-SI notation, ``text`` as it is if
	it is not a known unit."""
	unit = find_unit(text)
	return unit.dsi if unit is not None else text
