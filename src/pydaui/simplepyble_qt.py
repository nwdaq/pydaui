# SPDX-License-Identifier: GPL-3.0-or-later
#
# SimplePyBLE stand-in over QtBluetooth for platforms without SimplePyBLE
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The part of the SimplePyBLE API pynbus2's BLE transports use, implemented over QtBluetooth.

SimplePyBLE is not available on Android. :func:`pydaui.bluetooth.install` registers this module as
``simplepyble`` there, so pynbus2 uses it unchanged, with the same blocking semantics. QtBluetooth is
asynchronous and its objects need an event loop, so they all live in a dedicated thread running one; the
blocking calls hand their work over to it and wait for the result. They must not be called from that thread,
the notification callbacks run in it.

The application must have been granted the Bluetooth permission (see :func:`pydaui.bluetooth.withPermission`).

Android does not offer the bonded peripherals to QtBluetooth, :meth:`Adapter.get_paired_peripherals` returns
the peripherals found nearby by scanning instead. The system pairs a peripheral when the firmware first asks
for it.
"""

import atexit
import concurrent.futures
import threading

from PySide6.QtCore import QBluetoothPermission, QCoreApplication, QObject, QThread, QTimer, QUuid, Qt
from PySide6.QtBluetooth import (QBluetoothDeviceDiscoveryAgent, QBluetoothDeviceInfo, QBluetoothLocalDevice,
                                 QBluetoothUuid, QLowEnergyCharacteristic, QLowEnergyController,
                                 QLowEnergyService)

SCAN_TIMEOUT = 5.0
CONNECT_TIMEOUT = 30.0
# Long enough for the user to confirm the pairing the first write or subscription may trigger.
REQUEST_TIMEOUT = 60.0

# The smallest ATT MTU, assumed when the Qt backend does not know the negotiated one (BlueZ).
_MTU_MIN = 23

_CONNECTED_STATES = (
	QLowEnergyController.ControllerState.ConnectedState,
	QLowEnergyController.ControllerState.DiscoveringState,
	QLowEnergyController.ControllerState.DiscoveredState,
)


class BleError(Exception):
	pass


class _Loop:
	"""Runs callables in the thread owning all the QtBluetooth objects.

	The jobs are posted with a single shot timer, connecting a signal from a thread other than the main
	one makes PySide create its receiver as a child of the application, in the wrong thread.
	"""

	def __init__(self):
		self._thread = QThread()
		self._thread.setObjectName('ble')
		self._context = QObject()
		self._context.moveToThread(self._thread)
		self._thread.start()
		# A thread still running when the application is destroyed aborts the process.
		atexit.register(self._stop)

	def _stop(self):
		self._thread.quit()
		self._thread.wait()

	def call(self, start, timeout, what):
		"""Call ``start(future)`` in the loop thread and wait until it resolves ``future``. A ``start``
		raising fails the future, so does running out of ``timeout`` seconds."""
		if QThread.currentThread() is self._thread:
			raise RuntimeError('blocking BLE call from the BLE thread')
		future = concurrent.futures.Future()

		def job():
			try:
				start(future)
			except Exception as e:
				_fail(future, e)

		QTimer.singleShot(0, self._context, job)
		try:
			return future.result(timeout)
		except concurrent.futures.TimeoutError:
			_fail(future, BleError('%s timed out' % what))
			raise BleError('%s timed out after %.0f s' % (what, timeout)) from None


_loop = None
_loop_lock = threading.Lock()


def _get_loop():
	global _loop
	app = QCoreApplication.instance()
	if app is None:
		raise BleError('Bluetooth needs a running Qt application')
	if app.checkPermission(QBluetoothPermission()) != Qt.PermissionStatus.Granted:
		raise BleError('the application is not allowed to use Bluetooth')
	with _loop_lock:
		if _loop is None:
			_loop = _Loop()
		return _loop


# The waiting thread fails a future on a timeout while the loop thread may be resolving it, the first one wins.

def _fail(future, error):
	try:
		future.set_exception(error)
	except concurrent.futures.InvalidStateError:
		pass


def _resolve(future, result=None):
	try:
		future.set_result(result)
	except concurrent.futures.InvalidStateError:
		pass


def _uuid(text):
	return QBluetoothUuid(QUuid(text))


def _uuid_str(uuid):
	return uuid.toString(QUuid.StringFormat.WithoutBraces).lower()


class Adapter:
	"""A local Bluetooth adapter, see the SimplePyBLE ``Adapter`` for the methods."""

	def __init__(self, host):
		self._name = host.name()
		self._address = host.address()

	@staticmethod
	def get_adapters():
		return [Adapter(host) for host in QBluetoothLocalDevice.allDevices()]

	def identifier(self):
		return self._name

	def address(self):
		return self._address.toString()

	def get_paired_peripherals(self):
		"""The named peripherals found nearby by scanning for :data:`SCAN_TIMEOUT`, not only the paired ones."""
		loop = _get_loop()

		def start(future):
			agent = QBluetoothDeviceDiscoveryAgent(self._address)
			agent.setLowEnergyDiscoveryTimeout(int(SCAN_TIMEOUT * 1000))

			def finished():
				found = {}
				for info in agent.discoveredDevices():
					if info.coreConfigurations() & QBluetoothDeviceInfo.CoreConfiguration.LowEnergyCoreConfiguration \
							and info.name():
						found[info.address().toString()] = Peripheral(loop, info, self._address)
				agent.deleteLater()
				_resolve(future, list(found.values()))

			def error(_code):
				message = agent.errorString()
				agent.stop()
				agent.deleteLater()
				_fail(future, BleError('scanning for BLE devices failed: %s' % message))

			agent.finished.connect(finished)
			agent.errorOccurred.connect(error)
			agent.start(QBluetoothDeviceDiscoveryAgent.DiscoveryMethod.LowEnergyMethod)

		return loop.call(start, SCAN_TIMEOUT + 10.0, 'scanning for BLE devices')


class _Service:
	"""A GATT service with its characteristics discovered, and the notification callbacks on them."""

	def __init__(self, service):
		self.service = service
		self.callbacks = {}


class Service:
	"""The part of the SimplePyBLE ``Service`` the transports use."""

	def __init__(self, uuid):
		self._uuid = uuid

	def uuid(self):
		return self._uuid


class Peripheral:
	"""A BLE peripheral, see the SimplePyBLE ``Peripheral`` for the methods."""

	def __init__(self, loop, info, local):
		self._loop = loop
		self._info = info
		self._local = local
		self._name = info.name()
		self._address = info.address().toString()
		self._controller = None
		self._services = {}
		# The request in progress on one of the services, resolved by its completion or error signal.
		self._pending = None
		self._lock = threading.Lock()

	def identifier(self):
		return self._name

	def address(self):
		return self._address

	def connect(self):
		def start(future):
			controller = QLowEnergyController.createCentral(self._info, self._local)
			self._controller = controller
			controller.connected.connect(lambda: controller.discoverServices())
			controller.discoveryFinished.connect(lambda: _resolve(future))
			controller.errorOccurred.connect(lambda _code: self._onError(future, controller.errorString()))
			controller.disconnected.connect(lambda: self._onError(future, 'disconnected'))
			controller.connectToDevice()

		try:
			self._loop.call(start, CONNECT_TIMEOUT, 'connecting to %s' % self._address)
		except Exception:
			self.disconnect()
			raise

	def _onError(self, future, message):
		_fail(future, BleError('BLE connection to %s failed: %s' % (self._address, message)))
		if self._pending is not None:
			_fail(self._pending, BleError('BLE connection to %s: %s' % (self._address, message)))

	def is_connected(self):
		controller = self._controller
		return controller is not None and controller.state() in _CONNECTED_STATES

	def disconnect(self):
		def start(future):
			controller = self._controller
			if controller is None:
				_resolve(future)
				return
			for service in self._services.values():
				service.service.deleteLater()
			self._services = {}
			self._controller = None

			def disconnected():
				controller.deleteLater()
				_resolve(future)

			if controller.state() == QLowEnergyController.ControllerState.UnconnectedState:
				disconnected()
			else:
				controller.disconnected.connect(disconnected)
				controller.disconnectFromDevice()

		self._loop.call(start, CONNECT_TIMEOUT, 'disconnecting from %s' % self._address)

	def mtu(self):
		mtu = self._loop.call(lambda future: _resolve(future, self._controller.mtu()), REQUEST_TIMEOUT, 'reading the MTU')
		return mtu if mtu >= _MTU_MIN else _MTU_MIN

	def services(self):
		return self._loop.call(
			lambda future: _resolve(future, [Service(_uuid_str(u)) for u in self._controller.services()]),
			REQUEST_TIMEOUT, 'listing the services')

	def _service(self, service_uuid):
		"""The service object, its characteristics are discovered by :meth:`_request`. Called in the loop
		thread."""
		known = self._services.get(service_uuid)
		if known is not None:
			return known
		service = self._controller.createServiceObject(_uuid(service_uuid))
		if service is None:
			raise BleError('the device has no service %s' % service_uuid)
		known = _Service(service)

		def changed(characteristic, value):
			callback = known.callbacks.get(_uuid_str(characteristic.uuid()))
			if callback is not None:
				callback(bytes(value.data()))

		def written(_attribute, _value):
			if self._pending is not None:
				_resolve(self._pending)

		def error(code):
			if self._pending is not None:
				_fail(self._pending, BleError('BLE request on service %s failed: %s' % (service_uuid, code.name)))

		service.characteristicChanged.connect(changed)
		service.characteristicWritten.connect(written)
		service.descriptorWritten.connect(written)
		service.errorOccurred.connect(error)
		self._services[service_uuid] = known
		return known

	def _request(self, service_uuid, what, action):
		"""Run ``action(service)`` in the loop thread once the service is discovered and wait until the
		service signals the request is done. One request at a time, like the transports do anyway."""
		def start(future):
			self._pending = future
			known = self._service(service_uuid)
			state = known.service.state()
			if state == QLowEnergyService.ServiceState.RemoteServiceDiscovered:
				action(known)
				return

			def discovered(new_state):
				if new_state == QLowEnergyService.ServiceState.RemoteServiceDiscovered and not future.done():
					try:
						action(known)
					except Exception as e:
						_fail(future, e)

			known.service.stateChanged.connect(discovered)
			if state == QLowEnergyService.ServiceState.RemoteService:
				known.service.discoverDetails()

		with self._lock:
			try:
				return self._loop.call(start, REQUEST_TIMEOUT, what)
			finally:
				self._pending = None

	def _characteristic(self, known, service_uuid, char_uuid):
		characteristic = known.service.characteristic(_uuid(char_uuid))
		if not characteristic.isValid():
			raise BleError('the service %s has no characteristic %s' % (service_uuid, char_uuid))
		return characteristic

	def write_request(self, service_uuid, char_uuid, data):
		service_uuid = service_uuid.lower()

		def action(known):
			characteristic = self._characteristic(known, service_uuid, char_uuid)
			known.service.writeCharacteristic(characteristic, bytes(data), QLowEnergyService.WriteMode.WriteWithResponse)

		self._request(service_uuid, 'writing %s' % char_uuid, action)

	def notify(self, service_uuid, char_uuid, callback):
		self._subscribe(service_uuid.lower(), char_uuid.lower(), callback)

	def unsubscribe(self, service_uuid, char_uuid):
		self._subscribe(service_uuid.lower(), char_uuid.lower(), None)

	def _subscribe(self, service_uuid, char_uuid, callback):
		def action(known):
			characteristic = self._characteristic(known, service_uuid, char_uuid)
			cccd = characteristic.clientCharacteristicConfiguration()
			if not cccd.isValid():
				raise BleError('the characteristic %s cannot notify' % char_uuid)
			if callback is not None:
				known.callbacks[char_uuid] = callback
				value = QLowEnergyCharacteristic.CCCDEnableNotification
			else:
				known.callbacks.pop(char_uuid, None)
				value = QLowEnergyCharacteristic.CCCDDisable
			known.service.writeDescriptor(cccd, value)

		self._request(service_uuid, '%s notifications on %s' % ('enabling' if callback else 'disabling', char_uuid), action)
