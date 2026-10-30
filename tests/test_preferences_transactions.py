"""Preferences publish only successfully saved changes, using fixture files."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from starpet.storage import Preferences, atomic_json


ROOT = Path(__file__).resolve().parents[1]


class PreferencesTransactionTests(unittest.TestCase):
    def setUp(self):
        self.state = ROOT / 'evidence/tests' / ('preferences-transactions-' + uuid.uuid4().hex[:12])
        self.preferences = Preferences(self.state)

    def test_replace_failure_keeps_same_mapping_values_and_original_file(self):
        self.preferences.save(size=125, position=[80, 120], character='tabby')
        reference = self.preferences.values
        before = copy.deepcopy(reference)
        disk_before = self.preferences.path.read_bytes()
        with patch('starpet.storage.os.replace', side_effect=PermissionError('fixture locked file')):
            with self.assertRaises(OSError):
                self.preferences.save(size=150, position=[500, 600], reducedMotion=True)
        self.assertIs(self.preferences.values, reference)
        self.assertEqual(reference, before)
        self.assertEqual(self.preferences.path.read_bytes(), disk_before)
        self.assertEqual(list(self.state.glob('preferences.json.*.tmp')), [])
        self.assertEqual(Preferences(self.state).values, before)

    def test_success_updates_existing_mapping_after_write_and_preserves_other_fields(self):
        self.preferences.save(position=[80, 120], character='tabby', customImage='fixture-image.png',
                              workbuddyEnabled=False, completionNotifications=False)
        reference = self.preferences.values
        before = copy.deepcopy(reference)

        def observe_write(path, candidate):
            self.assertIs(self.preferences.values, reference)
            self.assertEqual(reference, before)
            self.assertIsNot(candidate, reference)
            atomic_json(path, candidate)

        with patch('starpet.storage.atomic_json', side_effect=observe_write) as write:
            self.preferences.save(size=130, reducedMotion=True)
        write.assert_called_once()
        self.assertIs(self.preferences.values, reference)
        self.assertEqual(reference, {**before, 'size': 130, 'reducedMotion': True})
        self.assertEqual(Preferences(self.state).values, reference)

    def test_later_success_does_not_persist_changes_from_a_failed_save(self):
        self.preferences.save(size=120, character='tabby', position=[20, 30])
        before = copy.deepcopy(self.preferences.values)
        with patch('starpet.storage.atomic_json', side_effect=OSError('fixture no space')):
            with self.assertRaises(OSError):
                self.preferences.save(size=150, character='robot', position=[900, 900], hooksInstalled=True)
        self.preferences.save(alwaysOnTop=False)
        expected = {**before, 'alwaysOnTop': False}
        self.assertEqual(self.preferences.values, expected)
        self.assertEqual(Preferences(self.state).values, expected)
        self.assertEqual(json.loads(self.preferences.path.read_text(encoding='utf-8')), expected)

    def test_old_preferences_load_without_rewriting_or_fabricating_saved_fields(self):
        self.state.mkdir(parents=True)
        old = {'size': 125, 'position': [-20, 200], 'alwaysOnTop': False, 'character': 'tabby'}
        self.preferences.path.write_text(json.dumps(old, ensure_ascii=False, indent=3), encoding='utf-8')
        disk_before = self.preferences.path.read_bytes()
        with patch('starpet.storage.atomic_json') as write:
            restored = Preferences(self.state)
            write.assert_not_called()
        self.assertEqual({key: restored.values[key] for key in old}, old)
        self.assertTrue(restored.values['completionNotifications'])
        self.assertFalse(restored.values['reducedMotion'])
        self.assertEqual(self.preferences.path.read_bytes(), disk_before)

    def test_first_save_failure_keeps_defaults_and_does_not_leave_a_partial_file(self):
        reference = self.preferences.values
        before = copy.deepcopy(reference)
        with patch('starpet.storage.os.replace', side_effect=PermissionError('fixture first save failure')):
            with self.assertRaises(OSError):
                self.preferences.save(reducedMotion=True, hooksInstalled=True)
        self.assertIs(self.preferences.values, reference)
        self.assertEqual(reference, before)
        self.assertFalse(self.preferences.path.exists())
        self.assertEqual(list(self.state.glob('preferences.json.*.tmp')), [])
        self.preferences.save(size=100)
        self.assertEqual(Preferences(self.state).values, {**before, 'size': 100})


if __name__ == '__main__':
    unittest.main()
