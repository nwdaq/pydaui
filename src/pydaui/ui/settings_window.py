# SPDX-License-Identifier: GPL-3.0-or-later
#
# Settings window
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *

from pydaui.ui.helpers import AppletWindow, DauiButton, StyledTreeWidget
from pydaui.ui.icons import Icons


class SettingsSection(QWidget):
	"""Base class for a section of the settings window.

	Each subclass is one section, listed on the left with its :attr:`NAME` and :meth:`sectionIcon`, in the
	order the subclasses are defined.
	"""

	NAME = ''

	def __init__(self, settings):
		super().__init__()
		self._settings = settings

		#: The rows of the section, its subclasses add them with ``addRow()``.
		self._form = QFormLayout()

		layout = QVBoxLayout()
		layout.addLayout(self._form)
		layout.addStretch(1)
		self.setLayout(layout)

	@staticmethod
	def sectionIcon():
		return QIcon()

	def _checkBox(self, text, key, default=False):
		"""A check box showing the boolean setting ``key``, changing it when toggled."""
		box = QCheckBox(text)
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
		"""A group box titled ``title`` with ``widgets`` below each other, added below the rows."""
		layout = QVBoxLayout()
		for widget in widgets:
			layout.addWidget(widget)
		group = QGroupBox(title)
		group.setLayout(layout)
		self._form.addRow(group)
		return group


class GeneralSection(SettingsSection):

	NAME = 'General'

	def __init__(self, settings):
		super().__init__(settings)

		path = QLabel(str(settings.path))
		path.setTextInteractionFlags(Qt.TextSelectableByMouse)
		self._form.addRow(self.tr('Settings file:'), path)

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
	ANDROID = QSysInfo.productType() == 'android'

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
		latest.addWidget(QLabel(self.tr('Number of the latest firmware files to download:')))
		latest.addWidget(self._spinBox(self.LATEST, self.LATEST_DEFAULT, 1, 100))
		latest.addStretch(1)
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
		fields.addRow(self.tr('Name:'), self._name)
		fields.addRow(self.tr('URL:'), self._url)
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


class _SectionList(QListWidget):
	"""The list of sections, as wide as its widest item but at least the minimal width."""

	def sizeHint(self):
		hint = super().sizeHint()
		hint.setWidth(max(self.minimumWidth(), self.sizeHintForColumn(0) + 2 * self.frameWidth()))
		return hint


class SectionPages(QWidget):
	"""A list of sections on the left on a dark background, the page of the selected one on the right with
	the ``footer`` widget, if any, below it."""

	#: Minimal width of the section list.
	LIST_MIN_WIDTH = 200
	LIST_ICON_SIZE = 24

	LIST_STYLE = """
		QListWidget {
			background-color: #444;
			border: none;
			outline: 0;
			padding: 6px 0;
		}
		QListWidget::item {
			/* Without a background the padding applies to the selected and hovered items only. */
			background-color: transparent;
			color: #fff;
			padding: 8px 10px;
		}
		QListWidget::item:hover {
			background-color: #555;
		}
		QListWidget::item:selected {
			background-color: #666;
		}
	"""

	def __init__(self, footer=None):
		super().__init__()

		self._list = _SectionList()
		self._list.setMinimumWidth(self.LIST_MIN_WIDTH)
		self._list.setIconSize(QSize(self.LIST_ICON_SIZE, self.LIST_ICON_SIZE))
		self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self._list.setStyleSheet(self.LIST_STYLE)

		self._pages = QStackedWidget()
		self._list.currentRowChanged.connect(self._pages.setCurrentIndex)

		right = QVBoxLayout()
		right.setContentsMargins(0, 0, 0, 0)
		right.setSpacing(0)
		right.addWidget(self._pages, stretch=1)
		if footer is not None:
			right.addWidget(footer)

		layout = QHBoxLayout()
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(self._list)
		layout.addLayout(right, stretch=1)
		self.setLayout(layout)

	def addPage(self, widget, name, page_icon=QIcon()):
		"""Add the page ``widget`` listed as ``name``, the first one added is selected."""
		self._list.addItem(QListWidgetItem(page_icon, name))
		self._pages.addWidget(widget)
		if self._list.currentRow() < 0:
			self._list.setCurrentRow(0)


class SettingsWindow(AppletWindow):
	"""The settings, one page per :class:`SettingsSection` subclass, the Close button at the bottom right."""

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
			self._pages.addPage(section_cls(settings), self.tr(section_cls.NAME), section_cls.sectionIcon())
		self.setWidget(self._pages)
		self.resize(720, 520)
