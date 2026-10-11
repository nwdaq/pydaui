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
import threading

import pynbus2
from PySide6.QtCore import QObject, Signal, Slot


class PyDauiConnection(QObject):

	connectionChanged = Signal(object)
	#: (uri, error message) when :meth:`connectUriAsync` fails.
	connectFailed = Signal(str, str)
	#: Delivers the (attempt, uri, result) of the thread of :meth:`connectUriAsync` to the GUI thread.
	_opened = Signal(object, str, object)

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
		# The URI :meth:`connectUriAsync` is connecting to, and the token of that attempt telling its result
		# from the result of an attempt cancelled before.
		self._connecting = None
		self._attempt = None
		self._opened.connect(self._finishAsync)

	def connectUri(self, uri):
		"""Connect to ``uri``, blocking until connected. Raises when connecting fails."""
		if self.connected:
			self.disconnectUri()
		self._finish(uri, self._open(uri))

	def connectUriAsync(self, uri):
		"""Connect to ``uri`` in a thread without blocking the GUI. Emits :attr:`connectionChanged` when
		connected or :attr:`connectFailed` when connecting fails, nothing when cancelled with
		:meth:`cancelConnect`."""
		if self.connected:
			self.disconnectUri()
		self._connecting = uri
		self._attempt = object()
		threading.Thread(target=self._openAsync, args=(self._attempt, uri), name='connect', daemon=True).start()

	def cancelConnect(self):
		"""Abandon the connection :meth:`connectUriAsync` is making, it is closed when it is made."""
		if self._connecting is not None:
			self._logger.info(f'connecting to {self._connecting} cancelled')
			self._connecting = None
			self._attempt = None

	def _openAsync(self, attempt, uri):
		try:
			opened = self._open(uri)
		except Exception as e:
			opened = e
		self._opened.emit(attempt, uri, opened)

	@Slot(object, str, object)
	def _finishAsync(self, attempt, uri, opened):
		if attempt is not self._attempt:
			# Cancelled meanwhile.
			if not isinstance(opened, Exception):
				opened[0].close()
			return
		self._connecting = None
		self._attempt = None
		if isinstance(opened, Exception):
			self._logger.warning(f'connecting to {uri} failed: {opened}')
			self.connectFailed.emit(uri, str(opened))
			return
		self._finish(uri, opened)

	def _open(self, uri):
		"""Connect to ``uri`` and discover its interfaces without changing the state of the connection, it
		may run in a thread. Returns the (nbus, socket, interfaces) for :meth:`_finish`."""
		self._logger.info(f'connecting to {uri}')
		nbus = pynbus2.connect(uri)
		try:
			# A socket connected to the destination the URI carries, if it carries one.
			socket = nbus.socket()
		except Exception:
			nbus.close()
			raise
		return nbus, socket, self._discoverInterfaces(nbus)

	def _finish(self, uri, opened):
		self._nbus, self._socket, self._interfaces = opened
		self._owns_nbus = True
		self._uri = uri
		self.connectionChanged.emit(uri)

	def _discoverInterfaces(self, nbus):
		"""Walk the interfaces the target exports (RemoteInterfaces over rible, the virtual ones over swd)
		and log them. Transports without named interfaces have nothing to discover."""
		try:
			interfaces = nbus.walk()
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

	def findInterface(self, protocol):
		"""The name of the topmost discovered interface speaking ``protocol``: the one with the fewest path
		components, the first discovered among equals. ``None`` if there is none."""
		names = [name for name, desc in self._interfaces if desc.get('p') == protocol]
		return min(names, key=lambda name: len(name.split()), default=None)

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
	def connecting(self):
		"""Whether :meth:`connectUriAsync` is connecting."""
		return self._connecting is not None

	@property
	def connected(self):
		return self._socket is not None
