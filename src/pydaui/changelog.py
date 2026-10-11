# SPDX-License-Identifier: GPL-3.0-or-later
#
# Change log from the git history
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The change log: the commits of every release tagged in git, newest first.

Builds generate it into ``changelog.json`` next to this module: the pdm-backend hook in ``pdm_build.py`` for
the sdist and wheels, the PyInstaller spec for the binaries. Running from a git checkout, it is read from
git directly. The build hook imports this module, it must not depend on anything outside the standard
library.

The format is ``{"releases": [{"tag": ..., "date": ..., "commits": [{"hash": ..., "date": ...,
"subject": ...}, ...]}, ...]}``. The commits after the last tag are in a release with the tag ``null``.
"""

import json
import subprocess
from pathlib import Path

CHANGELOG_FILE = Path(__file__).resolve().parent / 'changelog.json'


def _git(repo, *args):
	return subprocess.check_output(['git', *args], cwd=repo, text=True, stderr=subprocess.DEVNULL, timeout=30)


def _commits(repo, revisions):
	out = _git(repo, 'log', '--no-merges', '--format=%h%x1f%cs%x1f%s', revisions)
	return [dict(zip(('hash', 'date', 'subject'), line.split('\x1f'))) for line in out.splitlines() if line]


def from_git(repo):
	"""The change log of the commit checked out in ``repo``. Raises ``OSError`` or
	``subprocess.SubprocessError`` if it cannot be read from git."""
	out = _git(repo, 'for-each-ref', '--merged', 'HEAD', '--sort=creatordate',
		'--format=%(refname:short)%1f%(creatordate:short)', 'refs/tags')
	releases = []
	previous = None
	for line in out.splitlines():
		tag, date = line.split('\x1f')
		revisions = f'{previous}..{tag}' if previous else tag
		releases.append({'tag': tag, 'date': date, 'commits': _commits(repo, revisions)})
		previous = tag
	unreleased = _commits(repo, f'{previous}..HEAD' if previous else 'HEAD')
	if unreleased:
		releases.append({'tag': None, 'date': unreleased[0]['date'], 'commits': unreleased})
	releases.reverse()
	return {'releases': releases}


def write(path, repo):
	"""Write the change log of ``repo`` to ``path``."""
	Path(path).write_text(json.dumps(from_git(repo), indent=1))


def load():
	"""The change log of the build, or from git when running from a checkout, ``None`` if unavailable."""
	try:
		return json.loads(CHANGELOG_FILE.read_text())
	except (OSError, ValueError):
		pass
	try:
		return from_git(CHANGELOG_FILE.parent)
	except (OSError, subprocess.SubprocessError):
		return None
