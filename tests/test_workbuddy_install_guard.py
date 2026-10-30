import unittest
from pathlib import Path
from unittest.mock import patch
from starpet.hooks import configure

class WorkBuddyInstallGuard(unittest.TestCase):
 def test_blocked_before_reading_or_writing_user_profile(self):
  with patch.object(Path,'read_bytes',side_effect=AssertionError('must not read')),patch('starpet.hooks.atomic_json',side_effect=AssertionError('must not write')):
   with self.assertRaisesRegex(ValueError,'暂时停用'):configure(Path('unused-profile'),Path('unused-state'))
 def test_uninstall_remains_available(self):
  with patch.object(Path,'is_dir',return_value=False):
   with self.assertRaisesRegex(ValueError,'尚未找到'):configure(Path('unused-profile'),Path('unused-state'),uninstall=True)
