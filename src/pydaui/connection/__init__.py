# SPDX-License-Identifier: GPL-3.0-or-later
#
# Connection to a plumCore DAQ target
#
# Copyright (c) 2025, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""connection - connect to a plumCore DAQ target over a URI and call its services.

The transports (UDP/IPv6, serial, BLE, SWD), addressing and discovery are provided by pynbus2. pydaui's
UI uses :class:`PyDauiConnection`, a Qt-facing wrapper that emits ``connectionChanged`` on
connect/disconnect and supports opening tracked side connections (:meth:`PyDauiConnection.connectSide`)
to other services on the same target.
"""

from pynbus2 import NbusError, UriError, NotConnectedError

from .pydaui_connection import PyDauiConnection

__all__ = [
	'NbusError',
	'UriError',
	'NotConnectedError',
	'PyDauiConnection',
]
