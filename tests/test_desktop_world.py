import os,sys,unittest,uuid,copy
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.vendor'))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QPushButton
from starpet.desktop_world import DesktopWorld,validate
from starpet.storage import read_json

class DesktopWorldTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):
  self.folder=Path(__file__).resolve().parents[1]/'evidence'/('world-test-'+uuid.uuid4().hex);self.folder.mkdir();self.world=DesktopWorld(self.folder)
 def tearDown(self):
  if self.world.dialog:self.world.dialog.reject()
  for w in self.world.windows.values():w.hide();w.deleteLater()
  self.app.processEvents()
 def click(self,text):
  next(b for b in self.world.dialog.findChildren(QPushButton) if b.text()==text).click();self.app.processEvents()
 def test_default_off_and_locked_no_topmost(self):
  self.assertFalse(self.world.data['enabled']);self.assertFalse(self.world.path.exists())
  for w in self.world.windows.values():
   self.assertFalse(w.isVisible());self.assertTrue(w.windowFlags()&Qt.WindowType.WindowTransparentForInput);self.assertFalse(w.windowFlags()&Qt.WindowType.WindowStaysOnTopHint)
 def test_persist_reload_and_hide(self):
  self.assertTrue(self.world.enable(True));self.assertTrue(all(w.isVisible() for w in self.world.windows.values()))
  self.assertTrue(read_json(self.world.path)['roles']['tabby']['enabled']);self.assertTrue(self.world.enable(False));self.assertFalse(any(w.isVisible() for w in self.world.windows.values()))
 def test_write_failure_preserves_visibility_and_memory(self):
  before=copy.deepcopy(self.world.data)
  with patch('starpet.desktop_world.atomic_json',side_effect=OSError('fixture')):self.assertFalse(self.world.enable(True))
  self.assertEqual(before,self.world.data);self.assertFalse(any(w.isVisible() for w in self.world.windows.values()));self.assertTrue(self.world.error)
 def test_edit_cancel_restores_positions_and_disabled_state(self):
  old={k:w.pos() for k,w in self.world.windows.items()};self.world.settings();self.click('编辑摆放')
  self.world.windows['tree'].move(150,150);self.assertFalse(self.world.windows['tree'].windowFlags()&Qt.WindowType.WindowTransparentForInput)
  self.click('取消');self.assertFalse(self.world.editing);self.assertFalse(any(w.isVisible() for w in self.world.windows.values()));self.assertEqual(old['tree'],self.world.windows['tree'].pos());self.assertFalse(self.world.path.exists())
 def test_saved_position_and_screen_recovery(self):
  self.world.settings();self.click('编辑摆放');self.world.windows['tree'].move(150,150);self.click('保存并锁定');self.assertEqual(self.world.data['items']['tree']['position'],[150,150])
  tree=self.world.windows['tree'];tree.move(-90000,-90000);tree.clamp();self.assertTrue(QApplication.primaryScreen().availableGeometry().contains(tree.geometry()))
 def test_input_validation_rejects_bool_positions_and_wrong_shapes(self):
  data=validate({'scale':99999,'enabled':'yes','items':{'tree':{'visible':False,'position':[True,2]},'house':None}})
  self.assertEqual(data['scale'],160);self.assertFalse(data['enabled']);self.assertIsNone(data['items']['tree']['position']);self.assertFalse(data['items']['tree']['visible'])
 def test_roles_keep_separate_layouts_and_unknown_roles_are_ignored(self):
  self.world.settings();self.click('编辑摆放');self.world.windows['tree'].move(150,150);self.click('保存并锁定')
  self.assertTrue(self.world.use_role('robot'));self.assertFalse(self.world.data['enabled']);self.assertIsNone(self.world.data['items']['tree']['position'])
  self.world.settings();self.click('编辑摆放');self.world.windows['house'].move(210,210);self.click('保存并锁定')
  self.assertFalse(self.world.use_role('not a role'));self.assertEqual(self.world.role,'robot')
  self.assertTrue(self.world.use_role('tabby'));self.assertEqual(self.world.data['items']['tree']['position'],[150,150])
  self.assertNotEqual(self.world.data['items']['house']['position'],[210,210])
  stored=read_json(self.world.path);self.assertEqual(set(stored['roles']),{'tabby','robot'})
  self.assertNotIn('not a role',stored['roles']);self.assertEqual(stored['roles']['robot']['items']['house']['position'],[210,210])
 def test_old_global_layout_belongs_only_to_the_active_role(self):
  from starpet.storage import atomic_json
  atomic_json(self.world.path,{'enabled':True,'scale':120,'items':{'tree':{'visible':False,'position':[40,40]}}})
  other=DesktopWorld(self.folder,'catgirl');self.assertTrue(other.data['enabled']);self.assertEqual(other.data['scale'],120)
  self.assertFalse(other.data['items']['tree']['visible']);self.assertFalse(other.path.read_text(encoding='utf-8').strip().startswith('{"roles"'))
  other.settings();next(b for b in other.dialog.findChildren(QPushButton) if b.text()=='保存并锁定').click();self.app.processEvents()
  self.assertIn('roles',read_json(other.path));self.assertEqual(set(read_json(other.path)['roles']),{'catgirl'})
  for w in other.windows.values():w.hide();w.deleteLater()

if __name__=='__main__':unittest.main()
