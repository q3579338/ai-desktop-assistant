"""Independent human sizing at real widget/mask coordinates, offscreen only."""
import hashlib,json,os,sys,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'.vendor'),str(ROOT/'tests')]
os.environ['QT_QPA_PLATFORM']='offscreen'
import numpy as np
from PIL import Image,ImageDraw
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QPointF,QRect,Qt
from PySide6.QtGui import QImage,QPixmap,QRegion
from PySide6.QtWidgets import QApplication,QWidget
from starpet.storage import Preferences
from test_companion_context_ui import ContextUIFixture

class HumanSizeTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.app=QApplication.instance() or QApplication([])
  assert cls.app.platformName()=='offscreen'
  cls.evidence=ROOT/'evidence'/('human-size-'+uuid.uuid4().hex[:10]);cls.evidence.mkdir()
 def setUp(self):
  self.f=ContextUIFixture(self.app);self.c=self.f.c;self.pet=self.c.pet;self.w=self.f.window
  self.w.hide()
  images=[]
  for name,color in (('cat','#DF9456'),('human','#418479')):
   image=Image.new('RGBA',(200,256),(0,0,0,0));ImageDraw.Draw(image).ellipse((45,12,155,245),fill=color)
   path=self.f.folder/(name+'.png');image.save(path);images.append(path)
  identity=self.c.roles.import_pair(*images,'人物缩放测试')
  folder=self.c.roles.spec(identity)['image'].parent
  manifest=json.loads((folder/'role.json').read_text(encoding='utf-8'))
  for key,image in (('cat',images[0]),('human',images[1])):
   first=Image.open(image).convert('RGBA');second=first.copy()
   ImageDraw.Draw(second).ellipse((82,96,118,132),fill='#EFCBA0')
   first.save(folder/(key+'-idle.webp'),save_all=True,append_images=[second],duration=140,loop=0,lossless=True)
   form=manifest if key=='cat' else manifest['human']
   form['clips']={'idle':{'file':key+'-idle.webp','loop':True}}
  (folder/'role.json').write_text(json.dumps(manifest),encoding='utf-8')
  self.c.switch_role(identity)
  self.c.companion_store.add_fact('隔离人物大小测试，记忆须保持。')
  self.c.prefs.save(size=110,humanSize=160,reducedMotion=True,position=[200,200])
  self.pet.place_desktop_anchor(QPoint(200,200));self.pet.show();self.app.processEvents()
 def tearDown(self):
  self.pet.setUpdatesEnabled(False);self.w.hide();self.pet.hide();self.pet.clear_clip()
  QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.f.close()
 def capture(self,name):
  result=QPixmap(self.pet.size());result.fill(Qt.GlobalColor.transparent)
  self.pet.render(result,QPoint(),QRegion(),QWidget.RenderFlag.DrawWindowBackground|QWidget.RenderFlag.IgnoreMask)
  result.save(str(self.evidence/(name+'.png')))
  return result.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
 @staticmethod
 def rgba(image):
  return np.frombuffer(image.constBits(),dtype=np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()
 def assert_mask_covers_art(self,image):
  pixels=self.rgba(image);ys,xs=np.where(pixels[:,:,3]>32)
  self.assertTrue(len(xs))
  mask=self.pet.mask()
  self.assertTrue(all(mask.contains(QPoint(int(x),int(y))) for y,x in zip(ys,xs)))
 def test_human_grows_body_without_growing_bubble_or_cat_and_returns_to_anchor(self):
  old_prefs=self.c.prefs.path.read_bytes();memory=self.f.saved_memory();cat_rect=self.pet.geometry()
  cat=self.capture('cat-100');self.w.show();self.app.processEvents()
  human=self.capture('human-160')
  self.assertTrue(self.pet.human_body_visible());self.assertAlmostEqual(self.pet._body_scale,1.6)
  self.assertGreater(self.pet.height(),cat_rect.height()*1.3)
  self.assertGreater(self.pet.width(),cat_rect.width()*1.5)
  offset=round(self.pet._bubble_offset_x*self.pet._base_scale)
  self.assertEqual(self.pet.x()+offset,cat_rect.x())
  self.assertEqual(self.pet.y(),cat_rect.y())
  self.assertTrue(np.array_equal(self.rgba(cat)[:95],self.rgba(human)[:95,offset:offset+cat.width()]))
  self.assert_mask_covers_art(human)
  self.w.hide();self.app.processEvents();self.assertEqual(self.pet.geometry(),cat_rect)
  self.assertEqual(self.c.prefs.path.read_bytes(),old_prefs);self.assertEqual(self.f.saved_memory(),memory)
 def test_edge_clamp_does_not_accumulate_across_form_changes_or_save_position(self):
  area=QApplication.primaryScreen().availableGeometry();self.pet.place_desktop_anchor(area.bottomRight()-QPoint(10,10))
  cat_rect=self.pet.geometry();before=self.c.prefs.path.read_bytes()
  for _ in range(3):
   self.w.show();self.app.processEvents();self.assertTrue(area.contains(self.pet.geometry()))
   self.w.hide();self.app.processEvents();self.assertEqual(self.pet.geometry(),cat_rect)
  self.assertEqual(self.c.prefs.path.read_bytes(),before)
 def test_exit_position_uses_original_anchor_after_expansion_clamp_and_manual_move(self):
  self.w.show();self.app.processEvents()
  self.assertNotEqual(self.pet.x(),200)
  self.assertEqual(self.pet.saved_desktop_position(),[200,200])
  self.c.prefs.save(position=self.pet.saved_desktop_position())
  self.assertEqual(Preferences(self.f.state).values['position'],[200,200])
  delta=QPoint(23,-11);self.pet.move(self.pet.pos()+delta)
  self.assertEqual(self.pet.saved_desktop_position(),[223,189])
  area=QApplication.primaryScreen().availableGeometry();anchor=area.bottomRight()-QPoint(10,10)
  self.pet.place_desktop_anchor(anchor)
  self.assertTrue(area.contains(self.pet.geometry()))
  self.assertEqual(self.pet.saved_desktop_position(),[anchor.x(),anchor.y()])
  self.c.prefs.save(position=self.pet.saved_desktop_position())
  self.assertEqual(Preferences(self.f.state).values['position'],[anchor.x(),anchor.y()])
 def test_small_screen_fits_without_changing_requested_human_size(self):
  self.c.prefs.values['humanSize']=200
  area=QRect(0,0,320,310);screen=SimpleNamespace(availableGeometry=lambda:area)
  with patch.object(QApplication,'screenAt',return_value=screen),patch.object(QApplication,'primaryScreen',return_value=screen):
   self.w.show();self.pet.apply_size();self.app.processEvents()
   self.assertTrue(area.contains(self.pet.geometry()));self.assertLess(self.pet._body_scale,2)
   self.assertEqual(self.c.prefs.values['humanSize'],200)
   self.assert_mask_covers_art(self.capture('human-small-screen'))
 def test_static_fallback_and_animation_use_same_body_extent_and_voice_coordinates(self):
  self.w.show();self.app.processEvents();animated=self.capture('human-animation')
  self.assertIsNotNone(self.pet.clip_movie)
  self.c.core.ingest('codex','human-size-test','working','TaskStarted')
  still=self.capture('human-working-fallback');self.assertIsNone(self.pet.clip_movie)
  def bounds(image):
   alpha=self.rgba(image)[:,:,3];alpha[:100]=0;ys,xs=np.where(alpha>32)
   return int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())
  self.assertEqual(bounds(animated),bounds(still));self.assert_mask_covers_art(still)
  scale=self.pet._base_scale;offset=self.pet._bubble_offset_x
  point=self.pet.interaction_point(QPointF((207+offset)*scale,76*scale))
  self.assertAlmostEqual(point.x(),207);self.assertAlmostEqual(point.y(),76)
 def test_setting_is_a_draft_until_save_and_reloads_independently(self):
  self.w.load_settings();before=self.c.prefs.path.read_bytes();memory=self.f.saved_memory()
  settings=dict(self.c.companion_store.settings)
  self.w.human_size.setValue(180);self.w.settings_page.select_section('appearance')
  self.assertFalse(hasattr(self.w,'human_3d'))
  self.assertEqual(self.c.prefs.path.read_bytes(),before)
  self.assertTrue(self.w.save_settings());self.assertEqual(Preferences(self.f.state).values['humanSize'],180)
  self.assertNotIn('human3D',Preferences(self.f.state).values)
  self.assertEqual(self.c.prefs.values['size'],110);self.assertEqual(self.f.saved_memory(),memory)
  self.assertEqual(self.c.companion_store.settings,settings)
  self.w.load_settings();self.assertEqual(self.w.human_size.value(),180)
  self.assertFalse(hasattr(self.w,'human_3d'))

class HumanPreferenceCompatibilityTests(unittest.TestCase):
 def test_retired_renderer_preferences_cannot_return_after_upgrade_or_save(self):
  folder=ROOT/'evidence'/('two-dimensional-pref-'+uuid.uuid4().hex[:10]);folder.mkdir()
  legacy={'human3D':True,'humanModel':'sample','cat3D':True,'humanSize':180,'position':[321,123]}
  original=json.dumps(legacy).encode();(folder/'preferences.json').write_bytes(original)
  prefs=Preferences(folder)
  self.assertEqual((folder/'preferences.json').read_bytes(),original)
  self.assertFalse({'human3D','humanModel','cat3D'} & prefs.values.keys())
  prefs.save(human3D=True,cat3D=True,humanModel='original')
  saved=json.loads((folder/'preferences.json').read_text())
  self.assertFalse({'human3D','humanModel','cat3D'} & saved.keys())
  self.assertEqual(saved['humanSize'],180);self.assertEqual(saved['position'],[321,123])
 def test_missing_or_invalid_human_size_never_rewrites_on_read(self):
  folder=ROOT/'evidence'/('human-pref-'+uuid.uuid4().hex[:10]);folder.mkdir()
  for row,expected in [({'size':105},160),({'humanSize':35},100),({'humanSize':999},200),({'humanSize':'180'},160),({'humanSize':True},160)]:
   original=json.dumps(row).encode();(folder/'preferences.json').write_bytes(original)
   self.assertEqual(Preferences(folder).values['humanSize'],expected)
   self.assertEqual((folder/'preferences.json').read_bytes(),original)

if __name__=='__main__':unittest.main()
