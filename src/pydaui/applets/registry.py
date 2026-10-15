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

Some applets need more than the protocol to tell whether the device supports them, e.g. a flash partition
of a given name. Their ``probe`` asks the device once connected, see :func:`candidates`.

To add an applet, add its :class:`Applet` to :data:`APPLETS`. Its ``panel`` names the ribbon panel its
buttons go to, grouping the applets by function. The panels are listed in :data:`PANELS`.
"""

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtGui import QIcon

from pydaui.applets import calibration, conf, flash, update
from pydaui.ui.icons import Icons

# The module name is not a Python identifier.
mq_live_data = importlib.import_module('pydaui.applets.mq-live-data')


@dataclass(frozen=True)
class Applet:
	#: The protocol of the interfaces the applet talks to.
	protocol: str
	title: str
	#: Called without arguments, returns the icon of the applet's buttons.
	icon: Callable[[], QIcon]
	#: The name of the ribbon panel the applet's buttons go to, one of :data:`PANELS`.
	panel: str
	#: Called as ``window(connection, interface_name)``, returns the applet's ``QMdiSubWindow``. A window
	#: of an applet with ``settings`` gets the application settings and one with a ``probe`` its result as
	#: the further arguments, ``window(connection, interface_name, settings, context)``.
	window: type
	#: Called as ``probe(connection, interface_name)`` off the GUI thread once connected, asks the device
	#: whether the interface supports the applet. Returns ``None`` if it does not, else the context the
	#: window gets.
	probe: Optional[Callable] = None
	settings: bool = False


@dataclass(frozen=True)
class AppletInstance:
	"""An applet bound to one advertised interface."""
	applet: Applet
	#: The name of the interface, e.g. ``'ff14 conf'``.
	interface: str
	#: The result of the applet's probe, if it has one.
	context: Any = None

	@property
	def unit(self):
		"""The unit the interface belongs to, e.g. ``'ff14'``, empty for the main device."""
		return ' '.join(self.interface.split()[:-1])

	def open(self, connection, settings):
		args = (settings,) if self.applet.settings else ()
		if self.applet.probe is not None:
			args += (self.context,)
		return self.applet.window(connection, self.interface, *args)


#: The ribbon panels as (name, icon) tuples, in the order they are shown. The icon is called like
#: :attr:`Applet.icon`.
PANELS = (
	('Configuration', Icons.conf),
	('Storage', Icons.storage),
	('System', Icons.system),
	('Data', Icons.data),
)

#: The applets in the order they are offered within their panels.
APPLETS = (
	Applet(conf.PROTOCOL, 'Configuration tree', Icons.conf, 'Configuration', conf.ConfTreeWindow),
	Applet(calibration.PROTOCOL, 'Calibration', Icons.certificate, 'Configuration', calibration.CalibrationWindow,
	       settings=True),
	Applet(flash.PROTOCOL, 'Flash', Icons.storage, 'Storage', flash.FlashWindow),
	Applet(update.PROTOCOL, 'Update', Icons.update, 'System', update.UpdateWindow, probe=update.probe,
	       settings=True),
	Applet(mq_live_data.PROTOCOL, 'MQ live data', Icons.liveData, 'Data', mq_live_data.MqLiveDataWindow),
)


def _instances(applet, interfaces):
	names = [name for name, desc in interfaces if desc.get('p') == applet.protocol]
	names.sort(key=lambda name: len(name.split()))
	return [AppletInstance(applet, name) for name in names]


def available(interfaces):
	"""The applets offered by the (name, desc) ``interfaces`` a device advertises, grouped by applet in
	the :data:`APPLETS` order. Within an applet the main device comes first, then the units in the order
	they were discovered. The applets with a ``probe`` are left out, see :func:`candidates`."""
	result = [_instances(applet, interfaces) for applet in APPLETS if applet.probe is None]
	return [group for group in result if group]


def candidates(interfaces):
	"""The instances of the applets with a ``probe`` the ``interfaces`` may offer, grouped like in
	:func:`available`. Each is offered only if its probe returns a context, :func:`probed` makes the
	instance offered with it."""
	result = [_instances(applet, interfaces) for applet in APPLETS if applet.probe is not None]
	return [group for group in result if group]


def probed(instance, context):
	"""The ``instance`` of :func:`candidates` with the ``context`` its probe returned."""
	return AppletInstance(instance.applet, instance.interface, context)


def order(groups):
	"""The ``groups`` of applet instances in the :data:`APPLETS` order."""
	return sorted(groups, key=lambda group: APPLETS.index(group[0].applet))
