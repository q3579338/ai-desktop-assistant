"""Authenticated local appearance metadata contains no companion/account state."""
import http.client,json,sys,unittest,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from starpet.core import Core
from starpet.storage import read_json
from starpet.transport import IPC

class AppearanceIPCTests(unittest.TestCase):
 def setUp(self):
  self.state=ROOT/'evidence'/('appearance-ipc-'+uuid.uuid4().hex[:10]);self.state.mkdir()
  self.private='ISOLATED_PRIVATE_CONVERSATION_NOT_FOR_APPEARANCE'
  (self.state/'companion-memory.json').write_text(json.dumps({'turns':[{'user':self.private}]}),encoding='utf-8')
  self.ipc=IPC(Core(),self.state)
  self.expected={'renderer':'raster','form':'human','ready':False,'error':'','humanSize':160,'windowSize':[400,348],'model':None}
  self.ipc.appearance=dict(self.expected);self.ipc.start()
  self.runtime=read_json(self.state/'runtime.json')
 def tearDown(self):self.ipc.stop()
 def call(self,path,method='GET',payload=None,extra=None):
  conn=http.client.HTTPConnection('127.0.0.1',self.runtime['port'],timeout=2)
  headers={'Authorization':'Bearer '+self.runtime['token'],'Content-Type':'application/json'}
  headers.update(extra or {})
  try:
   conn.request(method,path,body=json.dumps(payload).encode() if payload is not None else None,headers=headers)
   response=conn.getresponse();return response.status,response.read().decode()
  finally:conn.close()
 def test_appearance_metadata_excludes_memory_runtime_auth_and_selection(self):
  status,body=self.call('/appearance');self.assertEqual(status,200);self.assertEqual(json.loads(body),self.expected)
  self.assertNotIn(self.private,body);self.assertNotIn(self.runtime['token'],body)
  for key in ('roleId','persona','provider','messages','memory','token','port','sessionId'):
   self.assertNotIn(key,json.loads(body))
 def test_appearance_rejects_wrong_auth_host_and_browser_origin(self):
  for headers in ({'Authorization':''},{'Authorization':'Bearer invalid'},
                  {'Host':'example.invalid'},{'Origin':'https://example.invalid'},{'Referer':'https://example.invalid/'}):
   with self.subTest(headers=list(headers)):
    status,body=self.call('/appearance',extra=headers);self.assertEqual(status,403);self.assertNotIn('humanSize',body)
 def test_chat_control_uses_authenticated_queue_and_does_not_call_a_model(self):
  status,_=self.call('/control','POST',{'action':'chat'},{'Authorization':'Bearer invalid'})
  self.assertEqual(status,403);self.assertTrue(self.ipc.actions.empty())
  status,body=self.call('/control','POST',{'action':'chat'})
  self.assertEqual(status,200);self.assertTrue(json.loads(body)['ok'])
  self.assertEqual(self.ipc.actions.get_nowait(),'chat');self.assertTrue(self.ipc.actions.empty())

if __name__=='__main__':unittest.main()
