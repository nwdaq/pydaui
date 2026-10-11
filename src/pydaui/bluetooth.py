# SPDX-License-Identifier: GPL-3.0-or-later
#
# Bluetooth support for the BLE connections
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Bluetooth for pynbus2's BLE transports where SimplePyBLE is not available (Android).

:func:`install` registers :mod:`pydaui.simplepyble_qt`, a SimplePyBLE stand-in over QtBluetooth, as the
``simplepyble`` module there. QtBluetooth refuses to work until the application has been granted the Bluetooth
permission, it is requested with :func:`withPermission` from the GUI thread before the BLE devices are listed
or connected.
"""

import importlib.util
import sys

from PySide6.QtCore import QBluetoothPermission, QCoreApplication, Qt
from pynbus2.uri import parse_uri

#: The URI schemes of the pynbus2 BLE transports.
SCHEMES = ('rible', 'dgble')

DENIED = 'pydaui is not allowed to use Bluetooth, allow it in the system settings'

_installed = False


def install():
	"""Use the QtBluetooth SimplePyBLE stand-in when SimplePyBLE is not installed."""
	global _installed
	if importlib.util.find_spec('simplepyble') is not None:
		return
	from pydaui import simplepyble_qt
	sys.modules['simplepyble'] = simplepyble_qt
	_installed = True


def scans():
	"""Whether the BLE devices are found by scanning (the QtBluetooth stand-in), not listed from the bonded
	ones."""
	return _installed


def usesBluetooth(uri):
	return parse_uri(uri).scheme in SCHEMES


def withPermission(context, granted, denied):
	"""Call ``granted()`` once the application may use Bluetooth, or ``denied()`` when it may not. The
	user is asked when they have not decided yet, the callbacks are called later then, unless ``context``
	is destroyed meanwhile."""
	app = QCoreApplication.instance()
	permission = QBluetoothPermission()

	def decided(result):
		if result.status() == Qt.PermissionStatus.Granted:
			granted()
		else:
			denied()

	status = app.checkPermission(permission)
	if status == Qt.PermissionStatus.Undetermined:
		app.requestPermission(permission, context, decided)
	elif status == Qt.PermissionStatus.Granted:
		granted()
	else:
		denied()
