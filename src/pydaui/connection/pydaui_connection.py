# SPDX-License-Identifier: GPL-3.0-or-later
#
# Qt-facing connection to a plumCore target
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The Qt-facing connection pydaui's UI uses, with a signal for reacting to connection state changes.

The transports, addressing and discovery are provided by pynbus2 (plumCore ``tools/pynbus2``); this is
a thin layer over a :class:`pynbus2.Nbus` and one of its sockets.

Not a singleton: pydaui may talk to several services on the same target at once (e.g. a config
session plus a firmware upload side by side), so each part of the app that needs its own logical
connection gets its own :class:`PyDauiConnection`. The app creates one root connection and threads it
through the widgets that need it; widgets that need an additional service on the *same* target call
:meth:`connectSide` on it rather than connecting a whole new instance from scratch.
"""

import logging

import pynbus2
from PySide6.QtCore import QObject, Signal


class PyDauiConnection(QObject):

	connectionChanged = Signal(object)

	def __init__(self):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		# The root connection owns the pynbus2 connection, side connections only own their socket.
		self._nbus = None
		self._owns_nbus = False
		self._socket = None
		self._uri = None
		self._interfaces = []
		self._children = []
		# The connection a side connection was opened from.
		self._parent = None

	def connectUri(self, uri):
		if self.connected:
			self.disconnectUri()

		self._logger.info(f'connecting to {uri}')
		nbus = pynbus2.connect(uri)
		try:
			# A socket connected to the destination the URI carries, if it carries one.
			self._socket = nbus.socket()
		except Exception:
			nbus.close()
			raise
		self._nbus = nbus
		self._owns_nbus = True
		self._uri = uri
		self._interfaces = self._discoverInterfaces()
		self.connectionChanged.emit(uri)

	def _discoverInterfaces(self):
		"""Walk the interfaces the target exports (RemoteInterfaces over rible, the virtual ones over swd)
		and log them. Transports without named interfaces have nothing to discover."""
		try:
			interfaces = self._nbus.walk()
		except pynbus2.NbusError as e:
			# Also raised by transports which do not support discovery.
			self._logger.info(f'no interfaces discovered: {e}')
			return []
		except Exception as e:
			# Stay connected, the connection itself is fine.
			self._logger.warning(f'interface discovery failed: {e}')
			return []
		self._logger.info(f'{len(interfaces)} interface(s) discovered')
		for name, desc in interfaces:
			self._logger.info(f'interface {name!r}: protocol {desc.get("p")}, version {desc.get("pv", "(none)")}, '
			                  f'mtu {desc.get("mtu")}, max sessions {desc.get("ms")}')
		return interfaces

	def disconnectUri(self):
		if not self.connected:
			return

		# Tear down any side connections opened from this one first, they share its transport.
		for child in list(self._children):
			child.disconnectUri()

		if self._parent is not None:
			self._parent._children.remove(self)
			self._parent = None

		self._logger.info(f'disconnecting from {self._uri}')
		if self._owns_nbus:
			# Closes all its sockets too.
			self._nbus.close()
		else:
			self._socket.close()
		self._nbus = None
		self._socket = None
		self._uri = None
		self._interfaces = []
		self.connectionChanged.emit(None)

	def connectSide(self, id, ep):
		"""Open a side connection to a different service (id, ep) on the same target this connection
		is already talking to, as another socket sharing the underlying transport.

		The side connection is tracked as a child: disconnecting this connection also disconnects it.
		Disconnecting the side connection closes only its own socket.
		"""
		if not self.connected:
			raise pynbus2.NotConnectedError('connect before opening a side connection')
		side = PyDauiConnection()
		side._nbus = self._nbus
		side._socket = self._nbus.socket(id, ep)
		side._uri = self._uri
		side._parent = self
		self._children.append(side)
		return side

	def send(self, data):
		self._checkConnected()
		self._socket.send(data)

	def recv(self, timeout=0.05):
		self._checkConnected()
		return self._socket.recv(timeout)

	def request(self, data, timeout=0.05, retries=50):
		self._checkConnected()
		return self._socket.request(data, timeout, retries)

	def probe(self, uri=''):
		"""Discover connection URIs one refinement step at a time (see :func:`pynbus2.probe`).

		``uri`` is any prefix of a full connection URI; the returned list of (possibly still incomplete)
		URI strings can be probed again to refine, or passed to :meth:`connectUri` once complete.
		Probing may block, run it off the GUI thread.
		"""
		uris = pynbus2.probe(uri)
		for u in uris:
			self._logger.info(f'discovered {u}')
		return uris

	def _checkConnected(self):
		if not self.connected:
			raise pynbus2.NotConnectedError('not connected')

	@property
	def uri(self):
		return self._uri

	@property
	def interfaces(self):
		"""The (name, desc) tuples of the interfaces discovered when connecting, empty if there are none."""
		return self._interfaces

	@property
	def nbus(self):
		"""The underlying :class:`pynbus2.Nbus`, or ``None`` when not connected."""
		return self._nbus

	@property
	def socket(self):
		"""The :class:`pynbus2.NbusSocket` this connection talks over, or ``None`` when not connected."""
		return self._socket

	@property
	def connected(self):
		return self._socket is not None
