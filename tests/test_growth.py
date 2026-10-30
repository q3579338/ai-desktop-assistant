import unittest,uuid,copy
from pathlib import Path
from unittest.mock import patch
from starpet.growth import GrowthStore
from starpet.companion import ChatStore

class GrowthTests(unittest.TestCase):
 def setUp(self):
  self.root=Path(__file__).resolve().parents[1]/'evidence'/('growth-'+uuid.uuid4().hex);self.root.mkdir()
 def test_disabled_by_default_without_creating_files(self):
  store=GrowthStore(self.root,'tabby');self.assertEqual(store.context(),'');self.assertFalse(store.path.exists())
 def test_user_and_role_isolation_and_restart(self):
  a=GrowthStore(self.root,'tabby','alice');a.add('喜欢森林');a.preferences(True,'brief')
  self.assertIn('喜欢森林',GrowthStore(self.root,'tabby','alice').context())
  for role,user in [('tabby','bob'),('robot','alice')]:self.assertNotIn('喜欢森林',GrowthStore(self.root,role,user).context())
 def test_delete_pause_and_reset_remove_future_injection(self):
  a=GrowthStore(self.root,'tabby');a.add('明确偏好');a.preferences(True,'warm');identity=a.data['notes'][0]['id'];a.remove(identity);self.assertNotIn('明确偏好',a.context());a.preferences(False,'warm');self.assertEqual(a.context(),'');a.reset();self.assertEqual(a.data['notes'],[])
 def test_failed_write_does_not_change_memory_or_disk(self):
  a=GrowthStore(self.root,'tabby');a.add('原有事实');before=a.path.read_bytes();data=copy.deepcopy(a.data)
  with patch('starpet.growth.atomic_json',side_effect=OSError('fixture')):
   with self.assertRaises(OSError):a.add('不能保存')
  self.assertEqual(a.data,data);self.assertEqual(a.path.read_bytes(),before)
 def test_chat_memory_toggle_controls_growth(self):
  a=GrowthStore(self.root,'tabby');a.add('喜欢森林');a.preferences(True,'brief');chat=ChatStore(self.root)
  self.assertIn('喜欢森林',chat.messages('你好')[0]['content']);chat.settings['useMemory']=False;self.assertNotIn('喜欢森林',chat.messages('你好')[0]['content'])
 def test_invalid_identity_cannot_escape_namespace(self):
  for role,user in [('../else','local'),('tabby','../alice')]:
   with self.assertRaises(ValueError):GrowthStore(self.root,role,user)
 def test_user_input_limit_and_namespace_files_only(self):
  a=GrowthStore(self.root,'tabby')
  with self.assertRaises(ValueError):a.add('x'*301)
  self.assertFalse(a.path.exists());a.preferences(True,'detailed');self.assertEqual([p.name for p in self.root.iterdir()],['growth-profiles'])
