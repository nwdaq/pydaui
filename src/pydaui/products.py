# SPDX-License-Identifier: GPL-3.0-or-later
#
# Known products
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The products pydaui knows, loaded from :data:`PRODUCTS_FILE`.

A target reached over a debug probe (SWD) does not identify itself, so the user selects the product. Its
pyocd target type and CMSIS pack let pyocd program the flash, its partition table splits the raw flash
regions into the partitions the firmware uses, see :mod:`pydaui.connection.swd_partitions`.

The file holds ``{"products": [...]}``, each product as::

    {"pn": "nwdaq-main-hh1", "version": "1.0.0", "mcu": "STM32U575VITx", "pack": "Keil.STM32U5xx_DFP.3.2.0.pack",
     "partitions": [{"name": "app", "volume": "flash0", "offset": "0x20000", "size": "0x80000"}, ...]}

``volume`` names the flash region the partition is in, as the pynbus2 ``swd`` transport exports it
(``flash0``, ``flash1``, ... in address order). ``offset`` and ``size`` are in bytes, numbers or strings
in any Python integer notation.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

#: The product list.
PRODUCTS_FILE = Path(__file__).resolve().parent / 'assets' / 'products.json'

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Partition:
	name: str
	#: The flash region (volume) the partition is in, e.g. ``flash0``.
	volume: str
	#: The start of the partition in the region, in bytes.
	offset: int
	size: int


@dataclass(frozen=True)
class Product:
	pn: str
	version: str
	#: pyocd target type.
	mcu: str
	#: CMSIS pack with the target and its flash algorithm, relative to the pydaui package.
	pack: str
	partitions: tuple = field(default=())


def _int(value):
	return int(value, 0) if isinstance(value, str) else int(value)


def load(path=PRODUCTS_FILE):
	"""The products in the file at ``path``, empty if it cannot be loaded."""
	try:
		data = json.loads(Path(path).read_text())
		return [Product(p['pn'], p['version'], p['mcu'], p['pack'], tuple(
			Partition(part['name'], part['volume'], _int(part['offset']), _int(part['size']))
			for part in p.get('partitions', ())))
			for p in data['products']]
	except (OSError, ValueError, KeyError, TypeError) as e:
		logger.warning(f'cannot load the products from {path}: {e}')
		return []


def find(pn, version):
	"""The product ``pn`` of ``version``, ``None`` if it is not known."""
	return next((p for p in load() if p.pn == pn and p.version == version), None)
