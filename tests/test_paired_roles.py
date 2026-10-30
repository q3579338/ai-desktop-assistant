"""One paired identity: validation, export roundtrip, form selection and memory."""
import copy,hashlib,json,unittest,zipfile
from pathlib import Path
from PIL import Image
from test_companion_context_ui import ContextUIFixture,QApplication
from starpet.rolepacks import RoleLibrary,RolePackError,_manifest,_json_bytes
from starpet.pair_gui import PairDialog

class PairedRoles(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):
  self.f=ContextUIFixture(self.app);self.c=self.f.c;self.c.pet.setUpdatesEnabled(False);self.f.window.hide()
  self.cat=self.f.folder/'cat.png';self.human=self.f.folder/'human.jpg'
  Image.new('RGBA',(40,40),'red').save(self.cat);Image.new('RGB',(40,40),'blue').save(self.human)
 def tearDown(self):self.f.close()
 def pair(self,name='配对测试'):return self.c.roles.import_pair(self.cat,self.human,name)
 def color(self):
  view=self.c.pet.visual_state();frame=self.c.pet.animation_image(view['state'],view['reaction']);return frame.toImage().pixelColor(20,20)
 def test_roundtrip_contains_both_bodies_and_no_personal_state(self):
  role=self.pair();target=self.f.folder/'pair.aipet';self.c.roles.export_package(role,target)
  with zipfile.ZipFile(target) as z:
   self.assertEqual(set(z.namelist()),{'role.json','cat.png','human.png'})
   manifest=json.loads(z.read('role.json'));self.assertEqual(manifest['human']['image'],'human.png')
  other=RoleLibrary(self.f.folder/'other');new=other.import_package(target)
  self.assertEqual(other.spec(new)['human']['image'].read_bytes(),self.c.roles.spec(role)['human']['image'].read_bytes())
 def test_chat_switches_only_body_and_keeps_identity_and_memory(self):
  role=self.pair();self.c.switch_role(role);self.c.companion_store.add_fact('这是一条配对测试记忆。')
  memory=self.c.companion_store.memory_path.read_bytes();settings=copy.deepcopy(self.c.companion_store.settings)
  self.assertEqual(self.color().name(),'#ff0000')
  self.f.window.show();self.assertTrue(self.c.pet.uses_catgirl_form());self.assertGreater(self.color().blue(),200)
  self.assertEqual(self.c.companion_store.settings,settings);self.assertEqual(self.c.companion_store.memory_path.read_bytes(),memory)
  self.f.window.hide();self.assertEqual(self.color().name(),'#ff0000')
 def test_two_pairs_have_different_human_and_separate_memory(self):
  first=self.pair();Image.new('RGB',(40,40),'green').save(self.human);second=self.pair('另一个角色')
  self.c.switch_role(first);self.c.companion_store.add_fact('第一只猫的记忆');self.f.window.show();self.assertGreater(self.color().blue(),200)
  self.c.switch_role(second);self.assertFalse(self.c.companion_store.facts);self.assertGreater(self.color().green(),100);self.assertLess(self.color().blue(),30)
  self.c.switch_role(first);self.assertEqual(len(self.c.companion_store.facts),1)
 def test_explicit_cat_or_human_mode(self):
  self.c.switch_role(self.pair());self.c.companion_store.settings['formMode']='cat';self.f.window.show();self.assertEqual(self.color().name(),'#ff0000')
  self.c.companion_store.settings['formMode']='catgirl';self.f.window.hide();self.assertGreater(self.color().blue(),200)
 def test_rejects_recursive_personal_and_aliased_human(self):
  base={'schema':1,'name':'测试','image':'cat.png'}
  for human in [{'image':'../secret.png'},{'image':'human.png','human':{}},{'image':'human.png','memory':[]},{'image':'cat.png'}]:
   with self.subTest(human=human),self.assertRaises(RolePackError):_manifest(_json_bytes({**base,'human':human}))
 def test_dialog_cancelling_never_imports_or_changes_settings(self):
  before=copy.deepcopy(self.c.companion_store.settings);ids=[r['id'] for r in self.c.roles.list_roles()]
  d=PairDialog(self.f.window.workshop);d.name.setText('预览测试');self.assertTrue(d.select_image('cat',self.cat));self.assertTrue(d.select_image('human',self.human));self.assertTrue(d.create.isEnabled());d.reject()
  self.assertEqual(ids,[r['id'] for r in self.c.roles.list_roles()]);self.assertEqual(before,self.c.companion_store.settings)
 def test_dialog_explicit_create_does_not_activate(self):
  before=copy.deepcopy(self.c.companion_store.settings);d=PairDialog(self.f.window.workshop);d.name.setText('确认测试');d.select_image('cat',self.cat);d.select_image('human',self.human);d.create.click()
  self.assertIsNotNone(d.identity);self.assertIn('human',self.c.roles.spec(d.identity));self.assertEqual(before,self.c.companion_store.settings)
 def test_role_preview_both_forms_does_not_activate(self):
  role=self.pair();workshop=self.f.window.workshop;before=copy.deepcopy(self.c.companion_store.settings)
  workshop.refresh(role);self.assertTrue(workshop.form_preview.isHidden() is False)
  self.assertEqual(workshop.preview_spec['image'].name,'cat.png')
  workshop.form_preview.setCurrentIndex(1);self.assertEqual(workshop.preview_spec['image'].name,'human.png')
  self.assertEqual(self.c.companion_store.settings,before)
 def test_human_has_its_own_animation_after_import(self):
  role=self.pair();folder=self.c.roles.root/role;manifest=json.loads((folder/'role.json').read_text(encoding='utf-8'))
  frames=[Image.new('RGB',(32,32),color) for color in ('blue','yellow','green')]
  frames[0].save(folder/'human.gif',save_all=True,append_images=frames[1:],duration=50,loop=0)
  manifest['human']['clips']={'idle':{'file':'human.gif','loop':True}}
  (folder/'role.json').write_text(json.dumps(manifest),encoding='utf-8')
  self.c.switch_role(role);self.f.window.show();self.color()
  self.assertEqual(Path(self.c.pet.clip_movie.fileName()).name,'human.gif')
  self.f.window.hide();self.color();self.assertIsNone(self.c.pet.clip_movie)
 def test_original_images_unchanged(self):
  before=[p.read_bytes() for p in (self.cat,self.human)];self.pair();self.assertEqual(before,[p.read_bytes() for p in (self.cat,self.human)])

if __name__=='__main__':unittest.main()
