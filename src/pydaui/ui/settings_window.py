# SPDX-License-Identifier: GPL-3.0-or-later
#
# Settings window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import os
from urllib.parse import quote, unquote, urlencode

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pynbus2.uri import parse_uri

from pydaui.ui.helpers import ANDROID, AppletWindow, DauiButton, StyledTreeWidget, WrappedCheckBox, WrappedLabel
from pydaui.ui.icons import Icons


class SettingsGroup(QWidget):
	"""A group box with the ``widgets`` below each other, the ``title`` above it wraps unlike the title of
	a :class:`QGroupBox`."""

	def __init__(self, title, *widgets):
		super().__init__()
		self._title = WrappedLabel()
		layout = QVBoxLayout()
		for widget in widgets:
			layout.addWidget(widget)
		box = QGroupBox()
		box.setLayout(layout)
		outer = QVBoxLayout(self)
		outer.setContentsMargins(0, 0, 0, 0)
		outer.setSpacing(2)
		outer.addWidget(self._title)
		outer.addWidget(box)
		self.setTitle(title)

	def setTitle(self, title):
		self._title.setText(title)
		self._title.setVisible(bool(title))


class SettingsPage(QWidget):
	"""Base class for a page of settings, a section of the settings window or a page of a section.

	It is listed on the left of the settings window with its :attr:`NAME` and :meth:`sectionIcon`. Its texts wrap, the check boxes and labels are :class:`WrappedCheckBox` and :class:`WrappedLabel`, so
	that it is never wider than the window, e.g. maximized on a small screen.
	"""

	NAME = ''

	def __init__(self, settings):
		super().__init__()
		self._settings = settings

		#: The rows of the section, its subclasses add them with ``addRow()``. A field too wide to be next
		#: to its label goes below it.
		self._form = QFormLayout()
		self._form.setRowWrapPolicy(QFormLayout.WrapLongRows)
		# The rows span the whole width on any platform, some keep them at their size hint by default.
		self._form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

		layout = QVBoxLayout()
		layout.addLayout(self._form)
		layout.addStretch(1)
		self.setLayout(layout)

	@staticmethod
	def sectionIcon():
		return QIcon()

	def _checkBox(self, text, key, default=False):
		"""A check box showing the boolean setting ``key``, changing it when toggled."""
		box = WrappedCheckBox(text)
		box.setChecked(bool(self._settings.get(key, default)))
		box.toggled.connect(lambda checked: self._set(key, checked))
		return box

	def _spinBox(self, key, default, minimum, maximum):
		"""A spin box showing the integer setting ``key``, changing it when changed."""
		box = QSpinBox()
		box.setRange(minimum, maximum)
		value = self._settings.get(key, default)
		box.setValue(value if isinstance(value, int) else default)
		box.valueChanged.connect(lambda value: self._set(key, value))
		return box

	def _set(self, key, value):
		"""Change the setting ``key`` and save the settings right away."""
		self._settings.set(key, value)
		self._settings.save()

	def _group(self, title, *widgets):
		"""A :class:`SettingsGroup` titled ``title`` with ``widgets`` below each other, added below the rows."""
		group = SettingsGroup(title, *widgets)
		self._form.addRow(group)
		return group


class SettingsSection(SettingsPage):
	"""Base class for a section of the settings window.

	Each subclass is one section, listed on the left in the order the subclasses are defined. A section with
	:attr:`PAGES` has no content of its own, its pages are listed below it and clicking it shows the first one.
	"""

	#: The :class:`SettingsPage` subclasses listed below the section.
	PAGES = ()


class GeneralSection(SettingsSection):

	NAME = 'General'

	def __init__(self, settings):
		super().__init__(settings)

		self._form.addRow(WrappedLabel(self.tr('Settings file:')), WrappedLabel(str(settings.path)))

	@staticmethod
	def sectionIcon():
		return Icons.conf()


class UiSection(SettingsSection):

	NAME = 'UI'

	#: Show the main window maximized on startup.
	MAIN_START_MAXIMIZED = 'ui.main_window.start_maximized'
	#: Show the newly opened windows in the MDI area maximized.
	APPLET_START_MAXIMIZED = 'ui.applets.start_maximized'
	#: Show the minimize button in the title bars of the newly opened windows in the MDI area.
	APPLET_ALLOW_MINIMIZING = 'ui.applets.allow_minimizing'

	#: On Android the screen is too small for windows side by side, they are maximized and not minimized.
	ANDROID = ANDROID

	def __init__(self, settings):
		super().__init__(settings)

		self._group(self.tr('Main window'),
			self._checkBox(self.tr('Start maximized'), self.MAIN_START_MAXIMIZED))
		self._group(self.tr('Applet and application windows'),
			self._checkBox(self.tr('Open new windows maximized'), self.APPLET_START_MAXIMIZED,
				default=self.ANDROID),
			self._checkBox(self.tr('Allow minimizing'), self.APPLET_ALLOW_MINIMIZING, default=not self.ANDROID))

	@classmethod
	def startMaximized(cls, settings):
		"""Whether the windows in the MDI area are opened maximized in the ``settings``."""
		return bool(settings.get(cls.APPLET_START_MAXIMIZED, cls.ANDROID))

	@classmethod
	def allowMinimizing(cls, settings):
		"""Whether the windows in the MDI area can be minimized in the ``settings``."""
		return bool(settings.get(cls.APPLET_ALLOW_MINIMIZING, not cls.ANDROID))

	@staticmethod
	def sectionIcon():
		return Icons.window()


class DeviceSection(SettingsSection):

	NAME = 'Device'

	#: Connect the last connected device on startup.
	AUTO_CONNECT = 'device.auto_connect'
	#: The URI of the last connected device, cleared when it is disconnected explicitly. Not shown in the
	#: section.
	LAST_URI = 'device.last_uri'

	def __init__(self, settings):
		super().__init__(settings)

		self._group(self.tr('Device connection'),
			self._checkBox(self.tr('Connect the last device automatically (unless disconnected explicitly)'),
				self.AUTO_CONNECT))

	@staticmethod
	def sectionIcon():
		return Icons.device()


class SshSessionList(StyledTreeWidget):

	COLUMN_RATIOS = (2, 5)

	def __init__(self):
		super().__init__()
		self.setHeaderLabels([self.tr('Name'), self.tr('URI')])
		self.setRootIsDecorated(False)


class SshSessionsPage(SettingsPage):
	"""The SSH sessions offered in the SSH tab of the connect window in a list, added and edited in a group
	shown below it."""

	NAME = 'SSH'

	#: The SSH sessions, a list of ``{"name": ..., "uri": "ssh://[<user>@]<host>[:<port>][?identity=<key>]"}``.
	SESSIONS = 'ssh.sessions'

	DEFAULT_PORT = 22

	def __init__(self, settings):
		super().__init__(settings)

		self._list = SshSessionList()
		self._list.itemSelectionChanged.connect(self._updateButtons)
		self._list.itemDoubleClicked.connect(lambda item, column: self._edit())

		self._add = DauiButton(self.tr('Add'), Icons.add())
		self._add.clicked.connect(self._startAdd)
		self._edit_btn = DauiButton(self.tr('Edit'), Icons.edit())
		self._edit_btn.clicked.connect(self._edit)
		self._remove = DauiButton(self.tr('Remove'), Icons.remove())
		self._remove.clicked.connect(self._removeSelected)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addWidget(self._add)
		buttons.addWidget(self._edit_btn)
		buttons.addWidget(self._remove)
		buttons.addStretch(1)
		button_row = QWidget()
		button_row.setLayout(buttons)

		self._group(self.tr('SSH sessions'),
			WrappedLabel(self.tr('Devices attached to a DAQ controller are reached through the pynbus2 SSH proxy '
			                     'running on it. Passwords are not saved, they are asked for when connecting.')),
			self._list, button_row)
		self._form_group = self._editGroup()

		# The index of the session being edited, None when adding one.
		self._editing = None
		self._load()
		self._closeEdit()

	@classmethod
	def sessions(cls, settings):
		"""The SSH sessions in the ``settings``, the dictionaries with an URI."""
		sessions = settings.get(cls.SESSIONS, [])
		if not isinstance(sessions, list):
			return []
		return [s for s in sessions if isinstance(s, dict) and isinstance(s.get('uri'), str)]

	def _editGroup(self):
		"""The group adding or editing a session, hidden until Add or Edit is clicked."""
		self._name = QLineEdit()
		self._name.textChanged.connect(self._updateConfirm)
		self._host = QLineEdit()
		self._host.setPlaceholderText(self.tr('host name, address or ~/.ssh/config alias'))
		self._host.textChanged.connect(self._updateConfirm)
		self._user = QLineEdit()
		self._user.setPlaceholderText(self.tr('from ~/.ssh/config or the local user'))
		self._port = QSpinBox()
		self._port.setRange(1, 65535)
		self._key = QLineEdit()
		self._key.setPlaceholderText(self.tr('the SSH agent and the keys in ~/.ssh'))
		browse = QToolButton()
		browse.setIcon(Icons.folder())
		browse.setToolTip(self.tr('Select the private key file'))
		browse.clicked.connect(self._browseKey)
		key_row = QHBoxLayout()
		key_row.setContentsMargins(0, 0, 0, 0)
		key_row.addWidget(self._key, stretch=1)
		key_row.addWidget(browse)
		key = QWidget()
		key.setLayout(key_row)

		fields = QFormLayout()
		fields.setContentsMargins(0, 0, 0, 0)
		fields.setRowWrapPolicy(QFormLayout.WrapLongRows)
		fields.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		fields.addRow(WrappedLabel(self.tr('Name:')), self._name)
		fields.addRow(WrappedLabel(self.tr('Host:')), self._host)
		fields.addRow(WrappedLabel(self.tr('Port:')), self._port)
		fields.addRow(WrappedLabel(self.tr('User:')), self._user)
		fields.addRow(WrappedLabel(self.tr('Private key:')), key)
		field_rows = QWidget()
		field_rows.setLayout(fields)

		self._confirm = DauiButton(self.tr('Add'), Icons.confirm())
		self._confirm.setDefault(True)
		self._confirm.clicked.connect(self._confirmEdit)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self._closeEdit)
		# Enter confirms and Escape cancels from the edit boxes.
		for edit in (self._name, self._host, self._user, self._key):
			edit.returnPressed.connect(self._confirm.click)
		QShortcut(QKeySequence(Qt.Key_Escape), field_rows, cancel.click, context=Qt.WidgetWithChildrenShortcut)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addStretch(1)
		buttons.addWidget(self._confirm)
		buttons.addWidget(cancel)
		button_row = QWidget()
		button_row.setLayout(buttons)

		return self._group('', field_rows, button_row)

	def _uri(self):
		"""The session URI of the edited fields."""
		host = self._host.text().strip()
		uri = 'ssh://%s%s' % (quote(self._user.text().strip(), safe='') + '@' if self._user.text().strip() else '',
		                      '[%s]' % host if ':' in host else host)
		if self._port.value() != self.DEFAULT_PORT:
			uri += ':%d' % self._port.value()
		if self._key.text().strip():
			uri += '?' + urlencode({'identity': self._key.text().strip()})
		return uri

	def _load(self):
		self._list.clear()
		for session in self.sessions(self._settings):
			# Decoded for reading, e.g. the path of the private key.
			item = QTreeWidgetItem([session.get('name') or session['uri'], unquote(session['uri'])])
			self._list.styleName(item)
			self._list.addTopLevelItem(item)

	@Slot()
	def _startAdd(self):
		self._openEdit(None, self.tr('Add SSH session'), self.tr('Add'), {})

	@Slot()
	def _edit(self):
		index = self._list.indexOfTopLevelItem(self._list.currentItem())
		if index < 0 or self._form_group.isVisible():
			return
		self._openEdit(index, self.tr('Edit SSH session'), self.tr('Confirm'), self.sessions(self._settings)[index])

	def _openEdit(self, index, title, confirm, session):
		self._editing = index
		self._form_group.setTitle(title)
		self._confirm.setText(confirm)
		self._name.setText(session.get('name', ''))
		host, port, user, key = '', self.DEFAULT_PORT, '', ''
		if session.get('uri'):
			try:
				uri = parse_uri(session['uri'])
				host, port, user, key = uri.host, uri.port or port, unquote(uri.username or ''), uri.get('identity', '')
			except Exception:
				host = session['uri']
		self._host.setText(host)
		self._port.setValue(port)
		self._user.setText(user)
		self._key.setText(key)
		self._updateConfirm()
		self._form_group.show()
		self._name.setFocus()
		self._updateButtons()

	@Slot()
	def _browseKey(self):
		path, _ = QFileDialog.getOpenFileName(self, self.tr('Private key'),
		                                      self._key.text() or os.path.expanduser('~/.ssh'))
		if path:
			self._key.setText(path)

	@Slot()
	def _confirmEdit(self):
		if not self._confirm.isEnabled():
			return
		sessions = self.sessions(self._settings)
		session = {'name': self._name.text().strip(), 'uri': self._uri()}
		if self._editing is None:
			sessions.append(session)
		else:
			sessions[self._editing] = dict(sessions[self._editing], **session)
		row = len(sessions) - 1 if self._editing is None else self._editing
		self._set(self.SESSIONS, sessions)
		self._load()
		self._list.setCurrentItem(self._list.topLevelItem(row))
		self._closeEdit()

	@Slot()
	def _removeSelected(self):
		index = self._list.indexOfTopLevelItem(self._list.currentItem())
		if index < 0 or self._form_group.isVisible():
			return
		sessions = self.sessions(self._settings)
		answer = QMessageBox.question(self, self.tr('Remove SSH session'),
			self.tr('Remove the SSH session %s?') % (sessions[index].get('name') or sessions[index]['uri']))
		if answer != QMessageBox.Yes:
			return
		del sessions[index]
		self._set(self.SESSIONS, sessions)
		self._load()
		self._updateButtons()

	@Slot()
	def _closeEdit(self):
		self._editing = None
		self._form_group.hide()
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		editing = self._form_group.isVisibleTo(self)
		self._add.setEnabled(not editing)
		selected = self._list.currentItem() is not None
		self._edit_btn.setEnabled(not editing and selected)
		self._remove.setEnabled(not editing and selected)

	@Slot()
	def _updateConfirm(self):
		self._confirm.setEnabled(bool(self._name.text().strip()) and bool(self._host.text().strip()))

	@staticmethod
	def sectionIcon():
		return Icons.terminal()


class ConnectionSection(SettingsSection):
	"""The ways of connecting the devices, a page for each one which has settings."""

	NAME = 'Device connection'
	PAGES = (SshSessionsPage,)

	@staticmethod
	def sectionIcon():
		return Icons.connection()


class RepositoryList(StyledTreeWidget):

	COLUMN_RATIOS = (2, 5, 2)

	def __init__(self):
		super().__init__()
		self.setHeaderLabels([self.tr('Name'), self.tr('URL'), self.tr('Authentication')])
		self.setRootIsDecorated(False)


class RepositoriesSection(SettingsSection):
	"""The plumCore repositories in a list, added and edited in a group shown below it."""

	NAME = 'plumCore repositories'

	#: The repositories, a list of ``{"name": ..., "url": ..., "auth": ...}``. ``auth`` is None when the
	#: repository needs no authentication or its credentials are not saved, else
	#: ``{"type": "basic", "username": ..., "password": ...}`` for HTTP basic authentication.
	REPOSITORIES = 'plumcore.repositories'
	#: The number of the latest firmware files downloaded when a repository is updated.
	LATEST = 'plumcore.latest'
	LATEST_DEFAULT = 5

	def __init__(self, settings):
		super().__init__(settings)

		self._list = RepositoryList()
		self._list.itemSelectionChanged.connect(self._updateButtons)
		self._list.itemDoubleClicked.connect(lambda item, column: self._edit())

		self._add = DauiButton(self.tr('Add'), Icons.add())
		self._add.clicked.connect(self._startAdd)
		self._edit_btn = DauiButton(self.tr('Edit'), Icons.edit())
		self._edit_btn.clicked.connect(self._edit)
		self._remove = DauiButton(self.tr('Remove'), Icons.remove())
		self._remove.clicked.connect(self._removeSelected)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addWidget(self._add)
		buttons.addWidget(self._edit_btn)
		buttons.addWidget(self._remove)
		buttons.addStretch(1)
		button_row = QWidget()
		button_row.setLayout(buttons)

		self._group(self.tr('Repositories'), self._list, button_row)
		self._form_group = self._editGroup()

		latest = QHBoxLayout()
		latest.setContentsMargins(0, 0, 0, 0)
		latest.addWidget(WrappedLabel(self.tr('Number of the latest firmware files to download:')), stretch=1)
		latest.addWidget(self._spinBox(self.LATEST, self.LATEST_DEFAULT, 1, 100), alignment=Qt.AlignTop)
		latest_row = QWidget()
		latest_row.setLayout(latest)
		self._group(self.tr('Repository updates'), latest_row)

		# The index of the repository being edited, None when adding one.
		self._editing = None
		self._load()
		self._closeEdit()

	def _editGroup(self):
		"""The group adding or editing a repository, hidden until Add or Edit is clicked."""
		self._name = QLineEdit()
		self._name.textChanged.connect(self._updateConfirm)
		self._url = QLineEdit()
		self._url.setPlaceholderText('https://')
		self._url.textChanged.connect(self._updateConfirm)
		fields = QFormLayout()
		fields.setContentsMargins(0, 0, 0, 0)
		fields.setRowWrapPolicy(QFormLayout.WrapLongRows)
		fields.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		fields.addRow(WrappedLabel(self.tr('Name:')), self._name)
		fields.addRow(WrappedLabel(self.tr('URL:')), self._url)
		field_rows = QWidget()
		field_rows.setLayout(fields)

		self._confirm = DauiButton(self.tr('Add'), Icons.confirm())
		self._confirm.setDefault(True)
		self._confirm.clicked.connect(self._confirmEdit)
		cancel = DauiButton(self.tr('Cancel'), Icons.cancel())
		cancel.clicked.connect(self._closeEdit)
		# Enter confirms and Escape cancels from the edit boxes.
		self._name.returnPressed.connect(self._confirm.click)
		self._url.returnPressed.connect(self._confirm.click)
		QShortcut(QKeySequence(Qt.Key_Escape), field_rows, cancel.click, context=Qt.WidgetWithChildrenShortcut)
		buttons = QHBoxLayout()
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.addStretch(1)
		buttons.addWidget(self._confirm)
		buttons.addWidget(cancel)
		button_row = QWidget()
		button_row.setLayout(buttons)

		return self._group('', field_rows, button_row)

	@classmethod
	def repositories(cls, settings):
		"""The repositories in the ``settings``."""
		repositories = settings.get(cls.REPOSITORIES, [])
		return [r for r in repositories if isinstance(r, dict)] if isinstance(repositories, list) else []

	@classmethod
	def auth(cls, settings, url):
		"""The saved ``(username, password)`` of the repository at ``url``, ``None`` if there are none."""
		for repository in cls.repositories(settings):
			auth = repository.get('auth')
			if repository.get('url') == url and isinstance(auth, dict) and auth.get('type') == 'basic':
				return auth.get('username', ''), auth.get('password', '')
		return None

	@classmethod
	def saveAuth(cls, settings, url, username, password):
		"""Save the HTTP basic authentication credentials of the repository at ``url``."""
		repositories = cls.repositories(settings)
		for repository in repositories:
			if repository.get('url') == url:
				repository['auth'] = {'type': 'basic', 'username': username, 'password': password}
		settings.set(cls.REPOSITORIES, repositories)
		settings.save()

	@classmethod
	def latest(cls, settings):
		"""The number of the latest firmware files to download in the ``settings``."""
		latest = settings.get(cls.LATEST, cls.LATEST_DEFAULT)
		return latest if isinstance(latest, int) and latest > 0 else cls.LATEST_DEFAULT

	def _repositories(self):
		return self.repositories(self._settings)

	def _load(self):
		self._list.clear()
		for repository in self._repositories():
			auth = self.auth(self._settings, repository.get('url'))
			item = QTreeWidgetItem([repository.get('name', ''), repository.get('url', ''),
				self.tr('basic (%s)') % auth[0] if auth else self.tr('none')])
			self._list.styleName(item)
			self._list.addTopLevelItem(item)

	@Slot()
	def _startAdd(self):
		self._openEdit(None, self.tr('Add repository'), self.tr('Add'), {})

	@Slot()
	def _edit(self):
		index = self._list.indexOfTopLevelItem(self._list.currentItem())
		if index < 0 or self._form_group.isVisible():
			return
		self._openEdit(index, self.tr('Edit repository'), self.tr('Confirm'), self._repositories()[index])

	def _openEdit(self, index, title, confirm, repository):
		self._editing = index
		self._form_group.setTitle(title)
		self._confirm.setText(confirm)
		self._name.setText(repository.get('name', ''))
		self._url.setText(repository.get('url', ''))
		self._updateConfirm()
		self._form_group.show()
		self._name.setFocus()
		self._updateButtons()

	@Slot()
	def _confirmEdit(self):
		if not self._confirm.isEnabled():
			return
		repositories = self._repositories()
		if self._editing is None:
			repositories.append({'name': self._name.text().strip(), 'url': self._url.text().strip(), 'auth': None})
		else:
			repository = dict(repositories[self._editing])
			repository.update(name=self._name.text().strip(), url=self._url.text().strip())
			repositories[self._editing] = repository
		row = len(repositories) - 1 if self._editing is None else self._editing
		self._set(self.REPOSITORIES, repositories)
		self._load()
		self._list.setCurrentItem(self._list.topLevelItem(row))
		self._closeEdit()

	@Slot()
	def _removeSelected(self):
		index = self._list.indexOfTopLevelItem(self._list.currentItem())
		if index < 0 or self._form_group.isVisible():
			return
		repositories = self._repositories()
		answer = QMessageBox.question(self, self.tr('Remove repository'),
			self.tr('Remove the repository %s?') % repositories[index].get('name', ''))
		if answer != QMessageBox.Yes:
			return
		del repositories[index]
		self._set(self.REPOSITORIES, repositories)
		self._load()
		self._updateButtons()

	@Slot()
	def _closeEdit(self):
		self._editing = None
		self._form_group.hide()
		self._updateButtons()

	@Slot()
	def _updateButtons(self):
		editing = self._form_group.isVisibleTo(self)
		self._add.setEnabled(not editing)
		selected = self._list.currentItem() is not None
		self._edit_btn.setEnabled(not editing and selected)
		self._remove.setEnabled(not editing and selected)

	@Slot()
	def _updateConfirm(self):
		self._confirm.setEnabled(bool(self._name.text().strip()) and bool(self._url.text().strip()))

	@staticmethod
	def sectionIcon():
		return Icons.plum()


class _SectionTree(QTreeWidget):
	"""The tree of sections, as wide as its widest item but at least the minimal width."""

	def sizeHint(self):
		hint = super().sizeHint()
		hint.setWidth(max(self.minimumWidth(), self.sizeHintForColumn(0) + 2 * self.frameWidth()))
		return hint

	def moveCursor(self, action, modifiers):
		# Moving up from a page skips the sections it is in, selecting one selects its first page again.
		index = super().moveCursor(action, modifiers)
		current = self.currentIndex()
		while action == QAbstractItemView.MoveUp and index.isValid() and self._contains(index, current):
			above = self.indexAbove(index)
			if not above.isValid():
				return current
			index = above
		return index

	@staticmethod
	def _contains(index, other):
		"""Whether the item at ``other`` is below the one at ``index``."""
		parent = other.parent()
		while parent.isValid():
			if parent == index:
				return True
			parent = parent.parent()
		return False


class SectionPages(QWidget):
	"""A tree of sections on the left on a dark background, the page of the selected one on the right with
	the ``footer`` widget, if any, below it. A section with pages lists them below it, see :meth:`addGroup`.
	Only the sections the selected page is in are expanded."""

	#: Minimal width of the section tree.
	LIST_MIN_WIDTH = 200
	LIST_ICON_SIZE = 24
	#: How far the pages of a section are indented.
	LIST_INDENT = 20

	#: The index of the page an item shows, in its data.
	PAGE_ROLE = Qt.UserRole

	LIST_STYLE = """
		QTreeWidget {
			background-color: #444;
			border: none;
			outline: 0;
			padding: 6px 0;
			/* The indentation of the selected and hovered pages too. */
			show-decoration-selected: 1;
		}
		QTreeWidget::item, QTreeWidget::branch {
			/* Without a background the padding applies to the selected and hovered items only. */
			background-color: transparent;
		}
		QTreeWidget::item {
			color: #fff;
			padding: 8px 10px;
		}
		QTreeWidget::item:hover, QTreeWidget::branch:hover {
			background-color: #555;
		}
		QTreeWidget::item:selected, QTreeWidget::branch:selected {
			background-color: #666;
		}
	"""

	def __init__(self, footer=None):
		super().__init__()

		self._tree = _SectionTree()
		self._tree.setHeaderHidden(True)
		self._tree.setRootIsDecorated(False)
		# Expanded and collapsed by selecting the pages.
		self._tree.setExpandsOnDoubleClick(False)
		self._tree.setIndentation(self.LIST_INDENT)
		self._tree.setMinimumWidth(self.LIST_MIN_WIDTH)
		self._tree.setIconSize(QSize(self.LIST_ICON_SIZE, self.LIST_ICON_SIZE))
		self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self._tree.setStyleSheet(self.LIST_STYLE)

		self._pages = QStackedWidget()
		self._tree.currentItemChanged.connect(self._showItem)

		right = QVBoxLayout()
		right.setContentsMargins(0, 0, 0, 0)
		right.setSpacing(0)
		right.addWidget(self._pages, stretch=1)
		if footer is not None:
			right.addWidget(footer)

		layout = QHBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(self._tree)
		layout.addLayout(right, stretch=1)
		self.setLayout(layout)

	def addGroup(self, name, group_icon=QIcon(), group=None):
		"""Add a section without a page of its own listed as ``name``, below the ``group`` if given. Its pages
		(or sections) are added below it with :meth:`addPage` (or this method). Selecting it selects its first
		page."""
		item = QTreeWidgetItem([name])
		item.setIcon(0, group_icon)
		if group is None:
			self._tree.addTopLevelItem(item)
		else:
			group.addChild(item)
		return item

	def addPage(self, widget, name, page_icon=QIcon(), group=None):
		"""Add the page ``widget`` listed as ``name``, below the ``group`` if given. The first one added is
		selected."""
		item = QTreeWidgetItem([name])
		item.setIcon(0, page_icon)
		item.setData(0, self.PAGE_ROLE, self._pages.count())
		if group is None:
			self._tree.addTopLevelItem(item)
		else:
			group.addChild(item)
		# Scrolled when the window is lower than the page, e.g. maximized on a small screen. Never
		# horizontally, the page wraps to the width of the window.
		scroll = QScrollArea()
		scroll.setWidget(widget)
		scroll.setWidgetResizable(True)
		scroll.setFrameShape(QFrame.NoFrame)
		scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self._pages.addWidget(scroll)
		if self._tree.currentItem() is None:
			self._tree.setCurrentItem(item)

	@Slot(QTreeWidgetItem, QTreeWidgetItem)
	def _showItem(self, item, previous):
		# Once the click selecting the item is handled: the view selects the item under the mouse after
		# making it the current one, another one when expanding or collapsing moved the items meanwhile.
		if item is not None:
			QTimer.singleShot(0, lambda: self._select(item))

	def _select(self, item):
		"""Show the page of the current ``item``. A section shows no page, its first one is selected instead."""
		if item is not self._tree.currentItem():
			return
		if item.childCount():
			leaf = item
			while leaf.childCount():
				leaf = leaf.child(0)
			self._tree.setCurrentItem(leaf)
			return
		self._expandOnly(item)
		if item.data(0, self.PAGE_ROLE) is not None:
			self._pages.setCurrentIndex(item.data(0, self.PAGE_ROLE))

	def _expandOnly(self, item):
		"""Expand the sections ``item`` is in, collapse all the others."""
		path = set()
		parent = item.parent()
		while parent is not None:
			path.add(id(parent))
			parent = parent.parent()
		iterator = QTreeWidgetItemIterator(self._tree)
		while iterator.value():
			section = iterator.value()
			if section.childCount():
				section.setExpanded(id(section) in path)
			iterator += 1


class SettingsWindow(AppletWindow):
	"""The settings, one page per :class:`SettingsSection` subclass or each of its pages, the Close button at
	the bottom right."""

	LAYER = AppletWindow.TOOL

	def __init__(self, settings, parent=None):
		super().__init__(parent=parent)
		self.setAttribute(Qt.WA_DeleteOnClose)
		self.setWindowTitle(self.tr('Settings'))
		self.setWindowIcon(Icons.conf())

		self._close = DauiButton(self.tr('Close'), Icons.cancel())
		self._close.clicked.connect(self.close)

		self._tb = QToolBar()
		spacer = QWidget()
		spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		self._tb.addWidget(spacer)
		self._tb.addWidget(self._close)

		self._pages = SectionPages(footer=self._tb)
		for section_cls in SettingsSection.__subclasses__():
			if section_cls.PAGES:
				group = self._pages.addGroup(self.tr(section_cls.NAME), section_cls.sectionIcon())
				for page_cls in section_cls.PAGES:
					self._pages.addPage(page_cls(settings), self.tr(page_cls.NAME), page_cls.sectionIcon(), group)
			else:
				self._pages.addPage(section_cls(settings), self.tr(section_cls.NAME), section_cls.sectionIcon())
		self.setWidget(self._pages)
		self.resize(720, 520)
