import unittest,sqlite3,uuid,hashlib
from pathlib import Path
from contextlib import closing
from starpet.workbuddy_readonly import WorkBuddyWatcher
from starpet.core import Core

class WorkBuddyReadOnlyTests(unittest.TestCase):
 def setUp(self):
  self.root=Path(__file__).resolve().parents[1]/'evidence'/('wb-db-'+uuid.uuid4().hex);(self.root/'.workbuddy').mkdir(parents=True)
  self.path=self.root/'.workbuddy/workbuddy.db'
  with closing(sqlite3.connect(self.path)) as c:
   c.execute('CREATE TABLE sessions(id TEXT,status TEXT,updated_at INTEGER,deleted_at INTEGER,is_background_automation INTEGER,title TEXT)')
   c.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',('fixture','working',1,None,0,'PRIVATE'))
   c.commit()
  self.core=Core();self.w=WorkBuddyWatcher(self.root,self.core)
 def update(self,status,stamp):
  with closing(sqlite3.connect(self.path)) as c:c.execute('UPDATE sessions SET status=?,updated_at=?',(status,stamp));c.commit()
 def test_work_completion_dedup_and_no_startup_replay(self):
  self.w.poll();self.assertEqual(self.core.received['workbuddy'],0)
  self.update('working',2);self.w.poll();self.assertEqual(self.core.snapshot()['active']['state'],'working')
  self.update('completed',3);self.w.poll();self.assertEqual(len(self.core.notices),1);self.w.poll();self.assertEqual(len(self.core.notices),1)
  w=WorkBuddyWatcher(self.root,self.core);w.poll();self.assertEqual(len(self.core.notices),1)
 def test_database_unchanged_and_no_private_content(self):
  before=self.path.read_bytes();self.w.poll();self.assertEqual(self.path.read_bytes(),before);self.assertNotIn('PRIVATE',str(self.core.snapshot()));self.assertFalse((self.root/'.workbuddy/settings.json').exists())
 def test_pending_is_not_approval_or_completion(self):
  self.w.poll();self.update('pending',2);self.w.poll();self.assertFalse(self.core.notices);self.assertNotEqual(self.core.snapshot()['active']['state'],'waiting')
 def test_missing_or_incompatible_database_does_not_write(self):
  self.path.unlink();self.w.poll();self.assertFalse(self.path.exists());self.assertFalse(self.core.notices)
 def test_error_transition_and_disabled(self):
  self.w.poll();self.update('error',2);self.w.poll();self.assertEqual(self.core.snapshot()['active']['state'],'error')
  self.core.set_enabled('workbuddy',False);self.update('working',3);self.w.poll();self.assertEqual(self.core.received['workbuddy'],1)
 def test_reenable_does_not_replay_completions_from_disabled_period(self):
  self.w.poll();self.update('working',2);self.w.poll()
  self.assertEqual(self.core.snapshot()['active']['state'],'working')
  self.core.set_enabled('workbuddy',False);self.w.poll();self.assertFalse(self.w.seen)
  self.update('completed',3);self.core.set_enabled('workbuddy',True);self.w.poll()
  self.assertFalse(self.core.notices);self.assertIsNone(self.core.snapshot()['active'])
  self.update('working',4);self.w.poll();self.update('completed',5);self.w.poll()
  self.assertEqual(len(self.core.notices),1)
