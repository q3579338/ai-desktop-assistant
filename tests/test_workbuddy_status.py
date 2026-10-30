import json,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from starpet.workbuddy_status import inspect,describe

class ReadOnlyStatusTests(unittest.TestCase):
 def setUp(self):
  self.root=Path(__file__).resolve().parents[1]/'evidence'/('wb-readonly-'+uuid.uuid4().hex);self.root.mkdir();self.wb=self.root/'.workbuddy';self.wb.mkdir();self.file=self.wb/'settings.json'
 def test_detection_never_writes_or_leaks_commands(self):
  raw=json.dumps({'hooks':{'Stop':[{'hooks':[{'command':'PRIVATE --starpet-workbuddy-hook Stop','secret':'PRIVATE'}]}]},'token':'PRIVATE'}).encode();self.file.write_bytes(raw)
  with patch('starpet.storage.atomic_json',side_effect=AssertionError('no write')):result=inspect(self.root)
  self.assertEqual(self.file.read_bytes(),raw);self.assertEqual(result['legacy_hook_count'],1);self.assertNotIn('PRIVATE',json.dumps(result));self.assertFalse(result['automatic_notifications_supported']);self.assertIn('未改动',describe(result))
 def test_missing_corrupt_and_oversize_are_not_connected(self):
  self.assertEqual(inspect(self.root)['status'],'settings_not_found')
  self.file.write_bytes(b'broken');self.assertEqual(inspect(self.root)['status'],'settings_unreadable')
  self.file.write_bytes(b' '* (1024*1024+1));self.assertEqual(inspect(self.root)['status'],'settings_too_large')
 def test_no_legacy_record_is_never_called_connected(self):
  self.file.write_text('{}');self.assertFalse(inspect(self.root)['automatic_notifications_supported']);self.assertEqual(inspect(self.root)['legacy_hook_count'],0)
 def test_live_connected_database_is_not_reported_unsupported_when_config_missing(self):
  result=inspect(self.root,'只读状态库已连接 · 仅新变化提醒，审批等待未验证',True)
  self.assertEqual(result['listener_status'],'connected');self.assertTrue(result['automatic_notifications_supported'])
  self.assertFalse(result['settings_present']);self.assertIn('已连接国内版任务状态库',describe(result))
  self.assertNotIn('不支持',describe(result));self.assertIn('审批等待和海外版尚未验证',describe(result))
 def test_disabled_listener_is_not_connected_from_stale_health(self):
  result=inspect(self.root,'只读状态库已连接 · 仅新变化提醒，审批等待未验证',False)
  self.assertEqual(result['listener_status'],'disabled');self.assertFalse(result['automatic_notifications_supported'])
  self.assertIn('已关闭',describe(result))
 def test_missing_or_incompatible_database_is_reported_separately(self):
  for health,status in [('只读监听：未找到国内版状态库','not_found'),('只读状态库暂不可用或结构不兼容 · 未改动客户端','unavailable')]:
   with self.subTest(status=status):
    result=inspect(self.root,health);self.assertEqual(result['listener_status'],status)
    self.assertFalse(result['automatic_notifications_supported']);self.assertNotIn('已连接',describe(result))
 def test_unexpected_health_text_is_not_echoed(self):
  result=inspect(self.root,'PRIVATE_UNEXPECTED_HEALTH');self.assertEqual(result['listener_status'],'unverified')
  self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('PRIVATE',describe(result))
