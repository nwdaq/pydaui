# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading and writing the files chosen by the user
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""Read and write the files the user chooses, e.g. in a file dialog or by dropping them.

They are accessed with :class:`QFile`, not the Python file objects. On Android the file dialogs return
``content://`` URIs the storage access framework serves, which only Qt can open. Ordinary paths work the same.
"""

from PySide6.QtCore import QFile, QFileInfo, QIODevice, QSaveFile


def read_file(path):
	"""The whole content of the file at ``path`` as bytes. Raises ``OSError`` if it cannot be read."""
	file = QFile(path)
	if not file.open(QIODevice.ReadOnly):
		raise OSError(file.errorString())
	try:
		data = file.readAll()
		if file.error() != QFile.NoError:
			raise OSError(file.errorString())
		return bytes(data)
	finally:
		file.close()


def write_file(path, data):
	"""Write ``data`` to the file at ``path``, replacing it. Written to a temporary file next to it first,
	which replaces it once complete. Where there cannot be one (a ``content://`` URI), written directly.
	Raises ``OSError`` if it cannot be written."""
	file = QSaveFile(path)
	file.setDirectWriteFallback(True)
	if not file.open(QIODevice.WriteOnly):
		raise OSError(file.errorString())
	if file.write(data) != len(data):
		error = file.errorString()
		file.cancelWriting()
		file.commit()
		raise OSError(error)
	if not file.commit():
		raise OSError(file.errorString())


def display_name(path):
	"""The name of the file at ``path`` to show to the user. Its display name for a ``content://`` URI,
	which ends with a document number."""
	return QFileInfo(path).fileName() or path
