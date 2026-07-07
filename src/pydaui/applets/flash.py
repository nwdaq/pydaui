import logging
import tempfile
import os
import math

from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *
from superqt.fonticon import icon, spin, pulse

from pydaui.connection.service_proxy import *


class DDRegion(QFrame):

	def __init__(self):
		super().__init__()
		self._logger = logging.getLogger(self.__class__.__name__)

		# It is possible to drop a binary file anywhere onto the widget
		# to start flash programming operation
		self.setAcceptDrops(True)

		self._drag_pixmap = icon('mdi7.folder-outline', color='gray', scale_factor=1).pixmap(64, 64)

		# Apply stylesheet changes to allow highlighting when accepting file drops
		self.setProperty('class', 'dd-region')
		self.setStyleSheet('''
			.dd-region {
				border: 1px solid #ccc;
				border-radius: 5px;
				border-color: #008;
			}
			.dd-region-hl {
				background-color: #fff;
			}
		''')

		self._createUi()

	def _createUi(self):
		self._visualizer = QLabel()
		self._visualizer.setPixmap(icon('mdi7.folder-arrow-up-down-outline', color='#888').pixmap(128, 128))

		self._hint_widget = QLabel(self.tr('Drag and drop here'))
		self._hint_widget.font().setBold(True)
		self._hint_widget.setStyleSheet('color: #888;')

		self._layout = QVBoxLayout()
		self._layout.addWidget(self._visualizer, alignment=Qt.AlignHCenter)
		self._layout.addWidget(self._hint_widget, alignment=Qt.AlignHCenter)
		self.setLayout(self._layout)

	@property
	def drag_pixmap(self):
		return self._drag_pixmap

	@drag_pixmap.setter
	def drag_pixmap(self, drag_pixmap):
		self._drag_pixmap = drag_pixmap

	def dragEnterEvent(self, event):
		if event.mimeData().hasUrls and event.source() != self:
			self._setHighlight(True)
			event.accept()
		else:
			event.ignore()

	def dragLeaveEvent(self, event):
		self._setHighlight(False)


	def dragMoveEvent(self, event):
		if event.mimeData().hasUrls and event.source() != self:
			event.accept()
		else:
			event.ignore()

	def dropEvent(self, event):
		if event.mimeData().hasUrls and event.source() != self:
			urls = event.mimeData().urls()
			if len(urls) > 1:
				raise ValueError('too many files dropped')

			self._setHighlight(False)
			self.receiveFile(urls[0].toLocalFile())
			event.accept()
		else:
			event.ignore()

	def mousePressEvent(self, event):
		if event.button() == Qt.LeftButton:
			self.drag_start_position = event.pos()

	def mouseMoveEvent(self, event):
		if not (event.buttons() and Qt.LeftButton):
			return
		if (event.pos() - self.drag_start_position).manhattanLength() < 5:
			return

		drag = QDrag(self)
		mime_data = QMimeData()
		mime_data.setUrls([QUrl.fromLocalFile(self.sendFile())])
		drag.setMimeData(mime_data)
		drag.setPixmap(self._drag_pixmap)
		drag.setHotSpot(QPoint(0, 0))
		drag.exec()

	def sendFile(self):
		raise NotImplementedError(f'file sending not implemented')

	def receiveFile(self, fname):
		raise NotImplementedError(f'file receiving not implemented')

	def _setHighlight(self, h):
		if h:
			self.setProperty('class', ['dd-region', 'dd-region-hl'])
			self.style().polish(self)
		else:
			self.setProperty('class', 'dd-region')
			self.style().polish(self)


class FlashView(QGroupBox):

	def __init__(self, size, block, used):
		super().__init__('Flash content visualization')

		# Determine the optimal flash map size
		blocks = size // block

		for cols in [8, 16, 32, 64]:
			rows = blocks // cols
			if rows <= (cols / 4):
				break

		if (rows * cols) < blocks:
			rows += 1

		# Generate widget with a flash view
		fwl = QGridLayout()
		for y in range(rows):
			for x in range(cols):
				if (y * cols + x + 1) * block < used:
					t = '■'
					c = '#c44'
				elif (y * cols + x) * block >= used:
					t = '□'
					c = '#888'
				else:
					t = '◩'
					c = '#c44'
				label = QLabel(t)
				label.setStyleSheet(f'color: {c};')
				fwl.addWidget(label, y, x)
		self.setLayout(fwl)



class FlashDDRegion(DDRegion):

	def __init__(self):
		super().__init__()
		self._flash_size = 65536
		self._block_size = 2048


class DauiButton(QPushButton):

	def __init__(self, text=None, btn_icon=None, color='#000'):
		super().__init__()

		if type(btn_icon) == str:
			print('tu som')
			btn_icon = icon(f'mdi7.{btn_icon}', color=color, scale_factor=1)
		self.setIcon(btn_icon)

		if text:
			self.setText(text)

		self.setIconSize(QSize(24, 24))


class FlashVolume(QWidget):

	def __init__(self):
		super().__init__()

		self._flash_view = FlashView(65536*3, 2048, 37000)

		self._tb = QToolBar()
		self._tb.addWidget(DauiButton(self.tr('Refresh'), 'refresh'))
		self._tb.addSeparator()
		self._tb.addWidget(DauiButton(self.tr('Upload'), 'cloud-upload-outline'))
		self._tb.addWidget(DauiButton(self.tr('Download'), 'cloud-download-outline'))
		self._tb.addSeparator()

		self._flash_info = QGroupBox('Volume information:')
		self._flash_info_layout = QVBoxLayout()
		self._flash_info_layout.addWidget(QLabel('Flash size: 65536\nBlock size: 2048'))
		self._flash_info.setLayout(self._flash_info_layout)

		self._layout = QVBoxLayout()
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.addWidget(self._tb)
		self._layout.addWidget(self._flash_info)
		self._layout.addWidget(self._flash_view)
		self._layout.addWidget(QLabel('\nYou can drag and drop files to/from your file manager to initiate volume content download/upload.'))
		self._layout.addWidget(FlashDDRegion())
		self.setLayout(self._layout)


class AppletWindow(QMdiSubWindow):

	def __init__(self, parent=None):
		super().__init__(parent=parent)

	def moveEvent(self, event):
		super().moveEvent(event)

		if self.x() < 20:
			self.move(0, self.y())
		elif (self.x() + self.width()) >= (self.parent().width() - 20):
			self.move(self.parent().width() - self.width(), self.y())

		if self.y() < 20:
			self.move(self.x(), 0)
		elif (self.y() + self.height()) >= (self.parent().height() - 20):
			self.move(self.x(), self.parent().height() - self.height())


class FlashVolumeList(AppletWindow):

	def __init__(self):
		super().__init__()
		self._service_proxy = ServiceProxy()

		self.setWidget(FlashVolume())



