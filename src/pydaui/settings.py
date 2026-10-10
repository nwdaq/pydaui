# SPDX-License-Identifier: GPL-3.0-or-later
#
# Persistent application settings
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import base64
import json
import logging
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QStandardPaths


class Settings:
	"""Application settings kept as a nested dictionary in a JSON file.

	Values are addressed by dotted keys, ``ui.geometry`` refers to
	``{"ui": {"geometry": ...}}``. Changes are kept in memory until
	:meth:`save` is called.
	"""

	FILE_NAME = 'settings.json'

	def __init__(self, path=None):
		self.logger = logging.getLogger(self.__class__.__name__)
		if path is None:
			path = Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)) / self.FILE_NAME
		self.path = Path(path)
		self._data = {}
		self.load()

	def load(self):
		try:
			with open(self.path, encoding='utf-8') as f:
				data = json.load(f)
		except FileNotFoundError:
			return
		except (OSError, ValueError) as e:
			self.logger.warning(f'cannot load settings from {self.path}: {e}')
			return
		if not isinstance(data, dict):
			self.logger.warning(f'ignoring settings in {self.path}, not a JSON object')
			return
		self._data = data
		self.logger.debug(f'settings loaded from {self.path}')

	def save(self):
		"""Write the settings to a temporary file and move it over the old one."""
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			tmp = self.path.with_name(self.path.name + '.tmp')
			with open(tmp, 'w', encoding='utf-8') as f:
				json.dump(self._data, f, indent='\t')
				f.write('\n')
			os.replace(tmp, self.path)
		except OSError as e:
			self.logger.warning(f'cannot save settings to {self.path}: {e}')
			return
		self.logger.debug(f'settings saved to {self.path}')

	def get(self, key, default=None):
		node = self._data
		for part in key.split('.'):
			if not isinstance(node, dict) or part not in node:
				return default
			node = node[part]
		return node

	def set(self, key, value):
		*parents, name = key.split('.')
		node = self._data
		for part in parents:
			if not isinstance(node.get(part), dict):
				node[part] = {}
			node = node[part]
		node[name] = value

	def getBytes(self, key):
		"""Return a base64 encoded value as a QByteArray, None if missing or invalid."""
		value = self.get(key)
		if not isinstance(value, str):
			return None
		try:
			return QByteArray(base64.b64decode(value, validate=True))
		except ValueError:
			self.logger.warning(f'ignoring invalid base64 value of {key}')
			return None

	def setBytes(self, key, value):
		"""Store binary data (bytes or a QByteArray) base64 encoded."""
		self.set(key, base64.b64encode(bytes(value)).decode('ascii'))
