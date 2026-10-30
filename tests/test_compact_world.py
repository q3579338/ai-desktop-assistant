import os,sys,uuid,unittest
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.vendor'))
from PySide6.QtWidgets import QApplication,QPushButton,QCheckBox
from PySide6.QtGui import QPixmap,QPainter
from PySide6.QtCore import Qt
from starpet.desktop_world import DesktopWorld

class CompactWorldTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):
  root=Path(__file__).resolve().parents[1]/'evidence'/('compact-'+uuid.uuid4().hex);root.mkdir();self.world=DesktopWorld(root,compact=True)
 def tearDown(self):
  if self.world.dialog:self.world.dialog.reject()
  for w in self.world.windows.values():w.deleteLater()
  self.app.processEvents()
 def test_enabled_scene_has_no_independent_desktop_windows(self):
  self.world.enable(True);self.assertFalse(any(w.isVisible() for w in self.world.windows.values()))
  pix=QPixmap(250,252);pix.fill(Qt.GlobalColor.transparent);p=QPainter(pix);self.world.paint_compact(p);p.end()
  self.assertTrue(any(pix.toImage().pixelColor(x,180).alpha()>0 for x in range(250)))
 def test_cancel_does_not_enable_scene_or_write_state(self):
  self.world.settings();d=self.world.dialog;d.findChildren(QCheckBox)[0].setChecked(True);d.reject();self.assertFalse(self.world.data['enabled']);self.assertFalse(self.world.path.exists())
 def test_compact_dialog_has_no_drag_mode(self):
  self.world.settings();texts=[b.text() for b in self.world.dialog.findChildren(QPushButton)];self.assertNotIn('编辑摆放',texts);self.assertIn('保存',texts)
 def test_switching_role_closes_old_scene_draft_and_cannot_save_it_to_new_role(self):
  self.world.enable(False);before=self.world.path.read_bytes()
  self.world.settings();old_dialog=self.world.dialog
  old_dialog.findChildren(QCheckBox)[0].setChecked(True)
  save=next(button for button in old_dialog.findChildren(QPushButton) if button.text()=='保存')
  self.assertTrue(self.world.use_role('catgirl'))
  self.assertIsNone(self.world.dialog);self.assertFalse(old_dialog.isVisible())
  save.click()  # A queued action from the old dialog is harmless, too.
  self.assertEqual(self.world.path.read_bytes(),before)
  self.assertFalse(self.world.data['enabled'])
  self.world.settings();new_dialog=self.world.dialog
  new_dialog.findChildren(QCheckBox)[0].setChecked(True)
  next(button for button in new_dialog.findChildren(QPushButton) if button.text()=='保存').click()
  self.world.use_role('tabby');self.assertFalse(self.world.data['enabled'])
  self.world.use_role('catgirl');self.assertTrue(self.world.data['enabled'])
