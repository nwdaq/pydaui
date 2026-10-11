# SPDX-License-Identifier: GPL-3.0-or-later
#
# Version and build information
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The application version and build date.

Builds stamp them into ``build.json`` next to this module: the pdm-backend hook in ``pdm_build.py`` for the
sdist and wheels, the PyInstaller spec for the binaries. Running from the source tree there is no build,
the version comes from the installed package metadata and the date of the current git commit stands in
for the build date.
"""

import importlib.metadata
import json
import subprocess
from pathlib import Path

BUILD_FILE = Path(__file__).resolve().parent / 'build.json'


def _load():
	try:
		return json.loads(BUILD_FILE.read_text())
	except (OSError, ValueError):
		return {}


_build = _load()


def version():
	"""The application version, ``None`` if unknown."""
	if 'version' in _build:
		return _build['version']
	try:
		return importlib.metadata.version('pydaui')
	except importlib.metadata.PackageNotFoundError:
		return None


def build_date():
	"""The build date as ``YYYY-MM-DD``, ``None`` when not running a build."""
	return _build.get('date')


def commit_date():
	"""The date of the current git commit as ``YYYY-MM-DD`` when running from a git checkout, else ``None``."""
	try:
		date = subprocess.check_output(['git', 'log', '-1', '--format=%cs'], cwd=BUILD_FILE.parent,
			text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
	except (OSError, subprocess.SubprocessError):
		return None
	return date or None
