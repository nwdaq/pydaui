import sys
import math

from PySide6.QtWidgets import QWidget, QApplication, QVBoxLayout
from PySide6.QtGui import QPainter, QPen, QBrush, QColor, QFont
from PySide6.QtCore import Qt, QPoint

class SatelliteViewWidget(QWidget):
	C_BG = QColor('#fff')
	C_GRID = QColor('#ccc')
	C_CONST = {
		'gps': QColor('#73d216'),
		'gal': QColor('#729fcf'),
		'glo': QColor('#ef2929'),
	}
	C_SAT_STROKE = QColor('#444')

	def __init__(self):
		super().__init__()

		self.satellites = [
			{'e': 0, 'a': 0, 'g': 'gps', 't': '23'},
			{'e': 10, 'a': 30, 'g': 'gps', 't': '23'},
			{'e': 20, 'a': 60, 'g': 'gal', 't': '23'},
			{'e': 30, 'a': 90, 'g': 'gps', 't': '23'},
			{'e': 40, 'a': 120, 'g': 'glo', 't': '23'},
			{'e': 50, 'a': 150, 'g': 'gps', 't': '23'},
			{'e': 60, 'a': 180, 'g': 'gal', 't': '23'},
			{'e': 70, 'a': 210, 'g': 'gps', 't': '23'},
			{'e': 80, 'a': 240, 'g': 'gal', 't': '23'},
			{'e': 65, 'a': 270, 'g': 'glo', 't': '23'},
		]

		self.initUI()

	def initUI(self):
		self.setGeometry(200, 200, 600, 600)
		self.setWindowTitle('Satellite View')
		self.show()

	def paintEvent(self, event):
		qp = QPainter()
		qp.begin(self)
		qp.setRenderHint(QPainter.Antialiasing)
		self.drawBackground(qp)
		self.drawGrid(qp)
		self.drawSatellites(qp)
		qp.end()

	def drawBackground(self, qp):
		qp.setBrush(Qt.NoBrush)
		qp.setPen(Qt.NoPen)
		qp.drawRect(0, 0, self.width() - 1, self.height() - 1)

	def drawLabel(self, qp, x, y, text, inverted=False):
		font = QFont()
		font.setPixelSize(12)
		qp.setFont(font)
		if inverted:
			qp.setPen(QPen(Qt.black, 1, Qt.SolidLine))
		else:
			qp.setPen(QPen(self.C_BG, 1, Qt.SolidLine))

		br = qp.boundingRect(0, 0, 0, 0, Qt.AlignCenter, text)

		# Apparently there is no way to draw a text stroke, so simulate it
		for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
			qp.drawText(x - br.width() / 2 + dx, y + br.height() / 3 + dy, text)

		if inverted:
			qp.setPen(QPen(self.C_BG, 1, Qt.SolidLine))
		else:
			qp.setPen(QPen(Qt.black, 1, Qt.SolidLine))

		qp.drawText(x - br.width() / 2, y + br.height() / 3, text)

	def drawGrid(self, qp):
		center_x = self.width() / 2
		center_y = self.height() / 2
		max_radius = self.width() / 20 * 9

		for i in range(1, 7):
			radius = max_radius / 6 * i

			qp.setPen(QPen(self.C_GRID, 1, Qt.SolidLine))
			qp.setBrush(Qt.NoBrush)
			qp.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)

		angle_labels = ['N', '30°', '60°', 'E', '120°', '150°', 'S', '210°', '240°', 'W', '300°', '330°']
		for i in range(12):
			angle = i * 30

			qp.setPen(QPen(self.C_GRID, 1, Qt.SolidLine))
			qp.setBrush(Qt.NoBrush)

			radian_angle = math.radians(angle - 90)
			x = center_x + max_radius * math.cos(radian_angle)
			y = center_y + max_radius * math.sin(radian_angle)
			qp.drawLine(center_x, center_y, x, y)

			# Angle labels
			text_radius = max_radius * 1.05
			x = center_x + text_radius * math.cos(radian_angle)
			y = center_y + text_radius * math.sin(radian_angle)
			self.drawLabel(qp, x, y, angle_labels[i])

		for i in range(1, 7):
			radius = max_radius / 6 * i

			x = center_x + radius * math.cos(math.radians(-75))
			y = center_y + radius * math.sin(math.radians(-75))
			self.drawLabel(qp, x, y, f'{90 - i * 15}°')

		self.drawLabel(qp, center_x, center_y, 'Zenith')


	def drawSatellites(self, qp):
		center_x = self.width() / 2
		center_y = self.height() / 2
		max_radius = self.width() / 20 * 9
		sat_size = 24

		for sat in self.satellites:
			qp.setBrush(QBrush(self.C_CONST[sat['g']]))

			r = max_radius - (sat['e'] / 90 * max_radius)
			a = math.radians(sat['a'] - 90)
			x = center_x + r * math.cos(a)
			y = center_y + r * math.sin(a)

			qp.setPen(QPen(self.C_SAT_STROKE, 4, Qt.SolidLine))
			qp.drawEllipse(x - sat_size / 2, y - sat_size / 2, sat_size, sat_size)
			qp.setPen(QPen(Qt.white, 2, Qt.SolidLine))
			qp.drawEllipse(x - sat_size / 2, y - sat_size / 2, sat_size, sat_size)
			qp.setPen(Qt.NoPen)
			qp.drawEllipse(x - sat_size / 2, y - sat_size / 2, sat_size, sat_size)

			self.drawLabel(qp, x, y, sat['t'], inverted=True)

	def resizeEvent(self, event):
		super().resizeEvent(event)
		width = self.width()
		height = self.height()
		size = min(width, height)
		self.resize(size, size)

def main():
	app = QApplication(sys.argv)
	main_widget = QWidget()
	layout = QVBoxLayout()
	main_widget.setLayout(layout)

	ex = SatelliteViewWidget()
	layout.addWidget(ex)

	main_widget.resize(600, 600)
	main_widget.show()
	sys.exit(app.exec())

if __name__ == '__main__':
	main()
