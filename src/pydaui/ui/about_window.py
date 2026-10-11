# SPDX-License-Identifier: GPL-3.0-or-later
#
# About window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import base64
import platform
import sys

import PySide6
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui import build_info
from pydaui.ui.helpers import Admonition
from pydaui.ui.icons import Icons
from pydaui.ui.info_window import InfoWindow


class AboutWindow(InfoWindow):
	"""Information about the application."""

	TEXT = 'text'
	LICENSE = 'license'

	#: The content of the window from the top, centred rich text and licensing notes shown as admonitions.
	#: The fields in braces are filled in by :meth:`_fields`.
	SECTIONS = [
		# Every project in the same order: the name, a short description, where to report bugs and read more,
		# the versions and the licence with the copyrights at its end.
		(TEXT, """
			<h2>da_UI</h2>
			<p>UI configurator for data acquisition devices running the plumCore framework</p>
			<p>
				Found a bug or want to contribute? Report issues and send pull requests at
				<a href="https://github.com/nwdaq/pydaui">https://github.com/nwdaq/pydaui</a>.
				More information is at
				<a href="https://nwdaq.com/software/pydaui">https://nwdaq.com/software/pydaui</a>.
			</p>
			<p>
				Version: <b>{version}</b><br/>
				Build date: <b>{build_date}</b><br/>
				Python: <b>{python}</b><br/>
				Interpreter: <b>{interpreter}</b>
			</p>
		"""),
		(LICENSE, """
			<p>
				This program is free software: you can redistribute it and/or modify it under the terms of the
				GNU General Public License as published by the Free Software Foundation, either version 3 of the
				License, or (at your option) any later version.
			</p>
			<p>
				This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
				even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
				General Public License for more details. You should have received a copy of the GNU General Public
				License along with this program. If not, see
				<a href="https://www.gnu.org/licenses/">https://www.gnu.org/licenses/</a>.
			</p>
			<p>
				Copyright &copy; 2015-2026 Marek Koza &lt;qyx@krtko.org&gt;<br/>
				Copyright &copy; 2021-2026 Marek Koza &lt;marek.koza@nwdaq.com&gt;
			</p>
		"""),
		(TEXT, """
			<h3>Qt</h3>
			<p>The cross-platform application toolkit, used through the PySide6 bindings</p>
			<p>
				More information is at <a href="https://www.qt.io/">https://www.qt.io/</a>.
			</p>
			<p>
				Qt: <b>{qt}</b><br/>
				PySide6: <b>{pyside}</b>
			</p>
		"""),
		(LICENSE, """
			<p>
				This program uses Qt through PySide6 under the terms of the GNU Lesser General Public License
				version 3. See
				<a href="https://www.qt.io/licensing/open-source-lgpl-obligations">https://www.qt.io/licensing/</a>
				for more details.
			</p>
			<p>
				Copyright &copy; The Qt Company Ltd. and other contributors. Qt is a trademark of The Qt Company Ltd.
			</p>
		"""),
		(TEXT, """
			<h3><img src="{plum}" width="{plum_size}" height="{plum_size}"/>&nbsp;plumCore</h3>
			<p>
				A modular framework for data logging and remote data acquisition, running on the devices
				configured by da_UI.
			</p>
			<p>
				Report bugs and send pull requests at
				<a href="https://github.com/iqyx/plumcore">https://github.com/iqyx/plumcore</a>.
			</p>
		"""),
		(LICENSE, """
			<p>
				plumCore is released under the GNU General Public License v3.0 or later. Some of its parts are
				still licensed under the 2-clause BSD license.
			</p>
			<p>
				Copyright &copy; 2015-2026 Marek Koza &lt;qyx@krtko.org&gt;
			</p>
		"""),
	]

	def __init__(self, parent=None):
		super().__init__(self.tr('About'), Icons.about(), parent=parent)

		fields = self._fields()
		for kind, text in self.SECTIONS:
			text = text.format(**fields)
			if kind == self.LICENSE:
				self.addWidget(Admonition(text, Admonition.NONE, center=True))
			else:
				self.addText(text)

	def _fields(self):
		"""The values of the fields in :attr:`SECTIONS`."""
		# The plum logo is as tall as the heading text, rendered for the screen pixel density.
		plum_size = QFontMetrics(self.font()).height() * 3 // 2
		scale = self.devicePixelRatioF()
		pixmap = Icons.plum().pixmap(QSize(plum_size, plum_size) * scale)
		buffer = QBuffer()
		buffer.open(QIODevice.WriteOnly)
		pixmap.save(buffer, 'PNG')
		plum = 'data:image/png;base64,' + base64.b64encode(bytes(buffer.data())).decode()

		return {
			'version': build_info.version() or self.tr('unknown'),
			'build_date': self._buildDate(),
			'python': platform.python_version(),
			'interpreter': f'{platform.python_implementation()} {sys.version}',
			'qt': qVersion(),
			'pyside': PySide6.__version__,
			'plum': plum,
			'plum_size': plum_size,
		}

	def _buildDate(self):
		"""The build date, or the date of the current git commit when running from the source tree."""
		if date := build_info.build_date():
			return date
		if date := build_info.commit_date():
			return self.tr('%s (git commit)') % date
		return self.tr('unknown')
