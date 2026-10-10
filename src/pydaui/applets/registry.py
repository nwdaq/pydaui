# SPDX-License-Identifier: GPL-3.0-or-later
#
# Applet registry
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Decide which applets a connected device offers, from the interfaces it advertises.

Each applet talks to interfaces speaking one protocol. Every advertised interface speaking it yields one
:class:`AppletInstance`, so a device advertising ``conf`` for itself and ``ff14 conf`` for its measurement
card offers two configuration trees. Interface names are space-delimited paths: the last component names
the interface, the components before it name the unit (daughter card, module, ...) it belongs to. An
interface without a unit belongs to the main device.

To add an applet, add its :class:`Applet` to :data:`APPLETS`. Its ``panel`` names the ribbon panel its
buttons go to, grouping the applets by function. The panels are listed in :data:`PANELS`.
"""

from dataclasses import dataclass
from typing import Callable

from PySide6.QtGui import QIcon

from pydaui.applets import conf, flash
from pydaui.ui.icons import Icons


@dataclass(frozen=True)
class Applet:
	#: The protocol of the interfaces the applet talks to.
	protocol: str
	title: str
	#: Called without arguments, returns the icon of the applet's buttons.
	icon: Callable[[], QIcon]
	#: The name of the ribbon panel the applet's buttons go to, one of :data:`PANELS`.
	panel: str
	#: Called as ``window(connection, interface_name)``, returns the applet's ``QMdiSubWindow``.
	window: type


@dataclass(frozen=True)
class AppletInstance:
	"""An applet bound to one advertised interface."""
	applet: Applet
	#: The name of the interface, e.g. ``'ff14 conf'``.
	interface: str

	@property
	def unit(self):
		"""The unit the interface belongs to, e.g. ``'ff14'``, empty for the main device."""
		return ' '.join(self.interface.split()[:-1])

	def open(self, connection):
		return self.applet.window(connection, self.interface)


#: The ribbon panels as (name, icon) tuples, in the order they are shown. The icon is called like
#: :attr:`Applet.icon`.
PANELS = (
	('Configuration', Icons.conf),
	('Storage', Icons.storage),
)

#: The applets in the order they are offered within their panels.
APPLETS = (
	Applet(conf.PROTOCOL, 'Configuration tree', Icons.conf, 'Configuration', conf.ConfTreeWindow),
	Applet(flash.PROTOCOL, 'Flash', Icons.storage, 'Storage', flash.FlashVolumeList),
)


def available(interfaces):
	"""The applets offered by the (name, desc) ``interfaces`` a device advertises, grouped by applet in
	the :data:`APPLETS` order. Within an applet the main device comes first, then the units in the order
	they were discovered."""
	result = []
	for applet in APPLETS:
		names = [name for name, desc in interfaces if desc.get('p') == applet.protocol]
		names.sort(key=lambda name: len(name.split()))
		result.append([AppletInstance(applet, name) for name in names])
	return [group for group in result if group]
