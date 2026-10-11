# SPDX-License-Identifier: GPL-3.0-or-later
#
# Change log window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import html
import re

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui import build_info, changelog
from pydaui.ui.icons import Icons
from pydaui.ui.info_window import InfoWindow


class ChangelogWindow(InfoWindow):
	"""The commits of every release of the running version, newest first."""

	#: The type prefix of a conventional commit subject, e.g. ``feat:`` or ``fix(ui)!:``, shown in bold.
	COMMIT_TYPE = re.compile(r'^(\w+(\([^)]*\))?!?:)\s*(.*)$')

	#: Colour of the release dates and the commit hashes.
	DIM_COLOR = Icons.GRAY

	def __init__(self, parent=None):
		super().__init__(self.tr('Change log'), Icons.changelog(), parent=parent)

		log = changelog.load()
		if not log or not log['releases']:
			self.addText(self.tr('<p>No change log is available.</p>'))
			return
		for release in log['releases']:
			self.addText(self._release(release), Qt.AlignLeft)

	def _release(self, release):
		"""The rich text of ``release``: its tag and date as the heading and the list of its commits."""
		if release['tag'] is None:
			title = self.tr('Unreleased (%s)') % html.escape(build_info.version() or self.tr('development'))
		else:
			title = html.escape(release['tag'])
		text = f'<h3>{title} <span style="color: {self.DIM_COLOR}; font-weight: normal;">{release["date"]}</span></h3>'
		if not release['commits']:
			return text + '<p>' + self.tr('No changes.') + '</p>'
		items = ''.join(f'<li>{self._subject(c["subject"])} '
			f'<span style="color: {self.DIM_COLOR};">{html.escape(c["hash"])}</span></li>'
			for c in release['commits'])
		return f'{text}<ul>{items}</ul>'

	def _subject(self, subject):
		if m := self.COMMIT_TYPE.match(subject):
			return f'<b>{html.escape(m.group(1))}</b> {html.escape(m.group(3))}'
		return html.escape(subject)
