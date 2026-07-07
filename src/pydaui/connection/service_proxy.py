from dataclasses import dataclass
import logging
from uritools import urisplit, uricompose

from PySide6.QtCore import *

from pyocd.core.helpers import ConnectHelper
from pyocd.flash.loader import MemoryLoader
from pyocd.core.exceptions import ProbeError



class ServiceProxyNotConnected(Exception):
	pass


class ServiceProxyConnection:

	def __init__(self):
		self._uri = ''

	def connectUri(self, uri):
		self._uri = uri

	def disconnectUri(self):
		self._uri = ''

	@property
	def uri(self):
		return self._uri


@dataclass
class ServiceProxyTarget:
	uri: str
	vendor: str
	product: str


class ServiceProxyConnectionError(Exception):
	pass

class SwdServiceProxyConnection(ServiceProxyConnection):

	@dataclass
	class Product:
		pn: str
		version: str
		mcu: str
		pack: str

	products = [
		Product('v35v-ctrl', '1.0.0', 'STM32G491CCUx', 'Keil.STM32G4xx_DFP.2.0.0.pack'),
	]


	def __init__(self):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._session = None

	def connectUri(self, uri):
		super().connectUri(uri)
		uris = urisplit(uri)
		self._session = ConnectHelper.session_with_chosen_probe(
			unique_id=uris.host,
			options={
				"target_override": "STM32G491CCUx",
				"pack": "Keil.STM32G4xx_DFP.2.0.0.pack",
			}
		)
		try:
			self._session.open()
		except ProbeError as e:
			self._session.close()
			self._logger.error(f'cannot connect target on {uri}: {str(e)}')
			self._session = None
			raise ServiceProxyConnectionError()

		self._loader = MemoryLoader(self._session)
		print(self._session.board.target.memory_map)

	def disconnectUri(self):
		super().disconnectUri()
		self._session.close()
		self._session = None

	@property
	def connected(self):
		return self._session != None

	@staticmethod
	def probe():
		targets = []
		for dp in ConnectHelper.get_all_connected_probes():
			targets.append(ServiceProxyTarget(
				vendor=dp.vendor_name,
				product=dp.product_name,
				uri=uricompose(scheme='swd', host=dp.unique_id),
			))

		return targets


class Singleton(type(QObject)):
	def __init__(cls, name, bases, dict):
		super().__init__(name, bases, dict)
		cls.instance = None

	def __call__(cls, *args, **kw):
		if cls.instance is None:
			cls.instance = super().__call__(*args, **kw)
		return cls.instance


class ServiceProxy(QObject, metaclass=Singleton):

	connectionChanged = Signal(str)

	def __init__(self):
		super().__init__()
		self._connection = None
		self._logger = logging.getLogger(self.__class__.__name__)


	def connectUri(self, uri):
		uris = urisplit(uri)
		if self._connection:
			self._connection.disconnectUri()
			self._connection = None

		self._logger.info(f'connecting to {uri}')

		try:
			if uris.scheme == 'swd':
				self._connection = SwdServiceProxyConnection()
			self._connection.connectUri(uri)
		except ServiceProxyConnectionError as e:
			self._connection = None
			raise e
		self.connectionChanged.emit(uri)

	def disconnectUri(self):
		if self.connected:
			self._logger.info(f'disconnecting from {self._connection.uri}')
			self._connection.disconnectUri()
			self._connection = None
			self.connectionChanged.emit(None)

	def probe(self):
		targets = SwdServiceProxyConnection.probe()
		for t in targets:
			self._logger.info(f'discovered target at {t.uri} ({t.vendor} {t.product})')
		return targets

	@property
	def connection(self):
		return self._connection

	@property
	def connected(self):
		return self._connection and self._connection.connected


class ServiceProxySocket:
	pass
