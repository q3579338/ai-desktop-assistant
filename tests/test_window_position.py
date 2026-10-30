import unittest
from test_companion_context_ui import ContextUIFixture,QApplication
from PySide6.QtCore import QPoint

class WindowPositionTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):self.f=ContextUIFixture(self.app)
 def tearDown(self):self.f.close()
 def test_model_settings_keeps_existing_position_and_size(self):
  w=self.f.window;c=self.f.c;c.show_companion();w.move(40,40);w.resize(400,500);self.app.processEvents();before=w.geometry()
  c.show_settings_section('connection');self.app.processEvents();self.assertEqual(w.geometry(),before)
 def test_hidden_then_reopened_keeps_user_position(self):
  w=self.f.window;c=self.f.c;c.show_companion();w.move(50,50);w.resize(400,500);self.app.processEvents();before=w.geometry();w.hide();c.show_companion();self.app.processEvents();self.assertEqual(w.geometry(),before)
