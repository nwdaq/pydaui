# SPDX-License-Identifier: GPL-3.0-or-later
#
# SWD flash partitions of the known products
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Split the raw flash regions the pynbus2 ``swd`` transport exports into the partitions of a product.

pynbus2 knows nothing about the products, it exports each flash region whole (``flash0``, ...). pydaui
registers :class:`PartitionedSwdTransport` for the ``swd`` scheme instead. It reads the product from the
URI query (``product=<pn>&version=<version>``, see :mod:`pydaui.products`) and serves the partitions of
the product's partition table as further volumes of the virtual flash service, next to the whole
regions.
"""

import logging

from pynbus2.services import flash as flash_service
from pynbus2.transport import register
from pynbus2.transport.swd import SwdTransport

from pydaui import products

logger = logging.getLogger(__name__)


class PartitionVolume(flash_service.FlashVolume):
	"""The ``partition`` of the flash ``volume``, addresses relative to the partition."""

	def __init__(self, volume, partition):
		self.name = partition.name
		self._volume = volume
		self._offset = partition.offset
		self._size = partition.size

	def sizes(self):
		(_size, ops), *blocks = self._volume.sizes()
		return [(self._size, ops)] + blocks

	def _check(self, addr, length):
		if addr + length > self._size:
			raise flash_service.FlashError(f'{length} B at 0x{addr:x} exceed partition {self.name!r}')

	def read(self, addr, length):
		self._check(addr, length)
		return self._volume.read(self._offset + addr, length)

	def erase(self, addr, length):
		self._check(addr, length)
		self._volume.erase(self._offset + addr, length)

	def write(self, addr, data):
		self._check(addr, len(data))
		self._volume.write(self._offset + addr, data)

	def close(self):
		self._volume.close()


def partition_volumes(volumes, partitions):
	"""The :class:`PartitionVolume` of each of the ``partitions`` of the flash ``volumes``. A partition
	outside its volume or not aligned to its erase blocks is left out with a warning."""
	by_name = {volume.name: volume for volume in volumes}
	result = []
	for partition in partitions:
		volume = by_name.get(partition.volume)
		if volume is None:
			logger.warning(f'partition {partition.name!r}: no flash volume {partition.volume!r}, skipped')
			continue
		size, *blocks = volume.sizes()
		erase = [block for block, ops in blocks if ops & flash_service.OPS_ERASE]
		if partition.offset + partition.size > size[0]:
			logger.warning(f'partition {partition.name!r} exceeds flash volume {volume.name!r} ({size[0]} B), skipped')
			continue
		if erase and (partition.offset % erase[0] or partition.size % erase[0]):
			logger.warning(f'partition {partition.name!r} is not aligned to the {erase[0]} B erase blocks of flash '
			               f'volume {volume.name!r}, skipped')
			continue
		logger.info(f'partition {partition.name!r}: flash volume {volume.name!r} at 0x{partition.offset:x}, '
		            f'{partition.size} B')
		result.append(PartitionVolume(volume, partition))
	return result


class PartitionedSwdTransport(SwdTransport):
	"""The pynbus2 ``swd`` transport, its flash service extended with the partitions of the product the
	URI names."""

	@classmethod
	def from_uri(cls, uri):
		transport = super().from_uri(uri)
		pn, version = uri.get('product'), uri.get('version')
		if not pn:
			return transport
		product = products.find(pn, version)
		if product is None:
			logger.warning(f'unknown product {pn} {version}, exporting the whole flash regions only')
			return transport
		volumes = transport._volumes + partition_volumes(transport._volumes, product.partitions)
		# Takes the blocks as large as the service it replaces, which the interface MTU advertises.
		block_len = transport._interfaces['flash'].block_len
		transport._interfaces['flash'] = flash_service.FlashService(volumes, reset=transport._reset,
		                                                            block_len=block_len)
		return transport


register('swd', PartitionedSwdTransport)
