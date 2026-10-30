from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / ".vendor")]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import Qt, QRectF
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter
from starpet.gui import whale
from PIL import Image

app = QApplication([])
assert app.platformName() == "offscreen"
root = ROOT / "assets"; root.mkdir(exist_ok=True)
image = QPixmap(256, 256); image.fill(Qt.GlobalColor.transparent)
p = QPainter(image); p.setRenderHint(QPainter.RenderHint.Antialiasing)
whale(p, QRectF(6, 8, 244, 238)); p.end()
image.save(str(root / "starpet.png"))
Image.open(root / "starpet.png").save(root / "starpet.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("Original StarPet icon generated offscreen")
