# SPDX-License-Identifier: GPL-3.0-or-later
#
# Quantity and unit selector
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui import quantities


class QuantitySelector(QWidget):
	"""A quantity and its unit picked from drop down lists side by side, the quantities of
	:data:`~pydaui.quantities.QUANTITIES` and the units of the quantity picked. Both may be typed too, e.g. a
	quantity not in the list.

	The units are shown as their symbols and given in the D-SI notation by :meth:`unit`, a unit typed which is
	not a known one as it is. Picking a quantity picks its most common unit, unless the unit picked is one of
	its units already. ``changed`` tells the user changed either of them.
	"""

	changed = Signal()

	def __init__(self):
		super().__init__()
		self._setting = False
		self._quantity = QComboBox()
		self._quantity.setEditable(True)
		self._quantity.setInsertPolicy(QComboBox.NoInsert)
		self._quantity.addItem('')
		for quantity in quantities.QUANTITIES:
			self._quantity.addItem(quantity.name)
		self._quantity.lineEdit().setPlaceholderText(self.tr('quantity'))
		self._quantity.setToolTip(self.tr('The quantity, picked or typed'))
		self._quantity.currentTextChanged.connect(self._quantityChanged)
		self._unit = QComboBox()
		self._unit.setEditable(True)
		self._unit.setInsertPolicy(QComboBox.NoInsert)
		self._unit.lineEdit().setPlaceholderText(self.tr('unit'))
		self._unit.setToolTip(self.tr('The unit of the quantity, picked or typed'))
		self._unit.currentTextChanged.connect(self._unitChanged)
		self._fillUnits()

		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)
		layout.addWidget(self._quantity, stretch=2)
		layout.addWidget(self._unit, stretch=1)

	def quantity(self):
		"""The name of the quantity, ``None`` if there is none."""
		return self._quantity.currentText().strip() or None

	def unit(self):
		"""The unit in the D-SI notation if it is a known one, ``None`` if there is none."""
		text = self._unit.currentText().strip()
		return quantities.unit_code(text) if text else None

	def unitText(self):
		"""The unit as shown, e.g. its symbol."""
		return self._unit.currentText().strip()

	def setValue(self, quantity, unit):
		"""Show the ``quantity`` and the ``unit``, either ``None`` if there is none. The unit is in the D-SI
		notation or a symbol."""
		self._setting = True
		self._quantity.setCurrentText(str(quantity or ''))
		self._fillUnits()
		self._unit.setCurrentText(quantities.unit_symbol(str(unit)) if unit is not None else '')
		self._setting = False

	def _fillUnits(self):
		"""Offer the units of the quantity, keeping the unit shown."""
		text = self._unit.currentText()
		self._unit.blockSignals(True)
		self._unit.clear()
		for unit in quantities.units(self.quantity()):
			self._unit.addItem(unit.symbol)
			self._unit.setItemData(self._unit.count() - 1, f'{unit.name} ({unit.dsi})', Qt.ToolTipRole)
		self._unit.setCurrentText(text)
		self._unit.blockSignals(False)

	@Slot(str)
	def _quantityChanged(self, text):
		if self._setting:
			return
		self._fillUnits()
		known = quantities.BY_NAME.get(self.quantity())
		if known is not None and self._unit.currentText() not in [unit.symbol for unit in known.units]:
			self._unit.setCurrentText(known.units[0].symbol)
		elif self.quantity() is None:
			self._unit.setCurrentText('')
		self.changed.emit()

	@Slot(str)
	def _unitChanged(self, text):
		if not self._setting:
			self.changed.emit()
