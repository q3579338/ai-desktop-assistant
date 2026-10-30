import os,sys,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.vendor'))
from PySide6.QtWidgets import QApplication
from starpet.companion import ChatStore
from starpet.growth_gui import GrowthPage

class GrowthUITests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):
  root=Path(__file__).resolve().parents[1]/'evidence'/('growth-ui-'+uuid.uuid4().hex);root.mkdir();self.chat=ChatStore(root);self.page=GrowthPage(SimpleNamespace(store=self.chat))
 def tearDown(self):self.page.deleteLater();self.app.processEvents()
 def test_failed_save_retains_input(self):
  self.page.note.setPlainText('不能丢失的草稿')
  with patch('starpet.growth.atomic_json',side_effect=OSError('fixture')):self.page.add()
  self.assertEqual(self.page.note.toPlainText(),'不能丢失的草稿');self.assertEqual(self.page.list.count(),0)
 def test_confirm_delete_and_style_roundtrip(self):
  self.page.enabled.setChecked(True);self.page.style.setCurrentIndex(1);self.page.save();self.page.note.setPlainText('我明确喜欢森林');self.page.add()
  self.assertIn('我明确喜欢森林',self.chat.messages('你好')[0]['content']);self.page.list.setCurrentRow(0);self.page.remove();self.assertNotIn('我明确喜欢森林',self.chat.messages('你好')[0]['content'])
