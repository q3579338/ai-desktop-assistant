"""Provider model names stay scoped without migrating or rewriting private data."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from starpet.companion import ChatError, ChatStore, DEFAULTS, PROVIDERS, validated_settings


ROOT = Path(__file__).resolve().parents[1]


class ModelScopeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT / 'evidence/tests').resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.state = self.base / ('model-scope-' + uuid.uuid4().hex)
        self.state.mkdir()
        self.store = ChatStore(self.state)

    def tearDown(self):
        target = self.state.resolve()
        if target.parent != self.base or not target.name.startswith('model-scope-'):
            raise RuntimeError('Unexpected model scope fixture path')
        shutil.rmtree(target)

    def assert_models(self, expected):
        self.assertEqual(self.store.settings['modelsByProvider'], expected)
        self.assertEqual(self.store.settings['model'], expected[self.store.settings['provider']])

    def test_legacy_load_assigns_model_only_to_its_provider_without_writes(self):
        memory = b'{"schema":1,"facts":[],"turns":[]}\n'
        (self.state / 'companion-memory.json').write_bytes(memory)
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                legacy = {'provider': provider, 'model': '  legacy-model  ', 'name': 'Existing name'}
                payload = json.dumps(legacy).encode('utf-8')
                self.store.settings_path.write_bytes(payload)
                before = self.store.settings_path.stat()
                self.store = ChatStore(self.state)
                self.assert_models({key: 'legacy-model' if key == provider else '' for key in PROVIDERS})
                self.assertEqual(self.store.settings_path.read_bytes(), payload)
                self.assertEqual(self.store.settings_path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual((self.state / 'companion-memory.json').read_bytes(), memory)
        self.assertEqual({p.name for p in self.state.iterdir()}, {'companion-settings.json', 'companion-memory.json'})

    def test_three_providers_restore_their_own_models_when_switch_has_no_model(self):
        expected = {'codex': 'official-codex', 'ollama': 'qwen3-4b-local', 'codebuddy': 'official-codebuddy'}
        for provider, model in expected.items():
            self.store.save_settings(provider=provider, model=model)
        for provider in ('ollama', 'codex', 'codebuddy', 'ollama'):
            self.store.save_settings(provider=provider)
            self.assertEqual(self.store.settings['provider'], provider)
            self.assert_models(expected)
            restored = ChatStore(self.state)
            self.assertEqual(restored.settings, self.store.settings)

    def test_first_switch_to_other_provider_uses_empty_default(self):
        self.store.save_settings(provider='ollama', model='qwen3-4b-local')
        self.store.save_settings(provider='codebuddy')
        self.assertEqual(self.store.settings['model'], '')
        self.store.save_settings(provider='codex')
        self.assertEqual(self.store.settings['model'], '')
        self.store.save_settings(provider='ollama')
        self.assertEqual(self.store.settings['model'], 'qwen3-4b-local')

    def test_explicit_model_updates_only_target_and_empty_resets_only_target(self):
        self.store.save_settings(provider='codex', model='codex-choice')
        self.store.save_settings(provider='ollama', model='local-choice')
        self.store.save_settings(provider='codebuddy', model='  buddy-choice  ')
        self.assert_models({'codex': 'codex-choice', 'ollama': 'local-choice', 'codebuddy': 'buddy-choice'})
        self.store.save_settings(provider='ollama', model='')
        self.assert_models({'codex': 'codex-choice', 'ollama': '', 'codebuddy': 'buddy-choice'})
        self.store.save_settings(name='Changed name')
        self.assert_models({'codex': 'codex-choice', 'ollama': '', 'codebuddy': 'buddy-choice'})

    def test_valid_mapping_wins_over_stale_compatibility_model(self):
        result = validated_settings({'provider': 'codebuddy', 'model': 'stale-local',
                                     'modelsByProvider': {'codex': 'cx', 'codebuddy': 'cb'}})
        self.assertEqual(result['model'], 'cb')
        self.assertEqual(result['modelsByProvider'], {'codex': 'cx', 'ollama': '', 'codebuddy': 'cb'})
        missing = validated_settings({'provider': 'ollama', 'model': 'legacy-local',
                                      'modelsByProvider': {'codex': 'cx'}})
        self.assertEqual(missing['modelsByProvider'], {'codex': 'cx', 'ollama': 'legacy-local', 'codebuddy': ''})

    def test_validation_drops_unknown_keys_nested_values_and_limits_names(self):
        raw = {'provider': 'ollama', 'model': ['invalid'], 'apiKey': 'DO_NOT_SAVE',
               'modelsByProvider': {'codex': '  ' + 'x' * 180 + ' ', 'ollama': {'token': 'DO_NOT_SAVE'},
                                    'codebuddy': True, 'apiKey': 'DO_NOT_SAVE', 'unknown': 'DO_NOT_SAVE'}}
        before = copy.deepcopy(raw)
        result = validated_settings(raw)
        self.assertEqual(result['modelsByProvider'], {'codex': 'x' * 160, 'ollama': '', 'codebuddy': ''})
        self.assertEqual(result['model'], '')
        self.assertNotIn('DO_NOT_SAVE', json.dumps(result))
        self.assertEqual(raw, before)
        for malformed in (None, True, 4, 'DO_NOT_SAVE', ['DO_NOT_SAVE']):
            with self.subTest(malformed=malformed):
                result = validated_settings({'provider': 'ollama', 'model': 'legacy', 'modelsByProvider': malformed})
                self.assertEqual(result['modelsByProvider'], {'codex': '', 'ollama': 'legacy', 'codebuddy': ''})

    def test_save_ignores_invalid_updates_and_never_persists_unknown_fields(self):
        self.store.save_settings(provider='ollama', model='local-choice')
        self.store.save_settings(provider='codebuddy', model='buddy-choice')
        self.store.save_settings(provider='ollama', model={'apiKey': 'DO_NOT_SAVE'}, apiKey='DO_NOT_SAVE',
            modelsByProvider={'ollama': ['DO_NOT_SAVE'], 'codebuddy': False, 'credentials': 'DO_NOT_SAVE'})
        self.assert_models({'codex': '', 'ollama': 'local-choice', 'codebuddy': 'buddy-choice'})
        saved = self.store.settings_path.read_text(encoding='utf-8')
        self.assertNotIn('DO_NOT_SAVE', saved)
        self.assertNotIn('credentials', saved)

    def test_default_model_maps_are_not_shared(self):
        one = validated_settings({})
        two = validated_settings(None)
        one['modelsByProvider']['codex'] = 'changed'
        self.assertEqual(two['modelsByProvider'], {key: '' for key in PROVIDERS})
        self.assertEqual(DEFAULTS['modelsByProvider'], {key: '' for key in PROVIDERS})
        self.assertEqual(list(self.state.iterdir()), [])

    def test_role_switch_preserves_shared_connection_and_separate_memory(self):
        self.store.add_fact('Tabby fixture fact')
        self.store.commit_turn('Tabby fixture question', 'Tabby fixture reply')
        tabby_path = self.store.memory_path
        tabby_bytes = tabby_path.read_bytes()
        self.store.save_settings(provider='codex', model='codex-choice')
        self.store.save_settings(provider='ollama', model='local-choice')
        models = copy.deepcopy(self.store.settings['modelsByProvider'])
        self.store.switch_role('robot', {'name': 'Robot', 'persona': 'Fixture', 'personality': 'concise',
                                        'formMode': 'cat', 'model': 'MUST_IGNORE',
                                        'modelsByProvider': {'ollama': 'MUST_IGNORE'}, 'provider': 'codebuddy'})
        self.assertEqual(self.store.settings['provider'], 'ollama')
        self.assert_models(models)
        self.assertEqual(self.store.facts, [])
        self.assertEqual(self.store.turns, [])
        self.store.add_fact('Robot fixture fact')
        robot_path = self.store.memory_path
        robot_bytes = robot_path.read_bytes()
        self.store.save_settings(provider='codebuddy', model='buddy-choice')
        models['codebuddy'] = 'buddy-choice'
        self.store.switch_role('tabby', {})
        self.assertEqual(self.store.settings['provider'], 'codebuddy')
        self.assert_models(models)
        self.assertEqual(self.store.facts[0]['text'], 'Tabby fixture fact')
        self.assertEqual(tabby_path.read_bytes(), tabby_bytes)
        self.assertEqual(robot_path.read_bytes(), robot_bytes)
        profiles = json.loads(self.store.profiles_path.read_text(encoding='utf-8'))
        self.assertTrue(all(set(profile) == {'name', 'persona', 'personality', 'formMode'} for profile in profiles.values()))

    def test_failed_settings_write_does_not_change_active_or_cached_models(self):
        self.store.save_settings(provider='ollama', model='local-choice')
        original = copy.deepcopy(self.store.settings)
        original_bytes = self.store.settings_path.read_bytes()
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture write failure')):
            with self.assertRaises(ChatError):
                self.store.save_settings(provider='codebuddy', model='new-choice')
        self.assertEqual(self.store.settings, original)
        self.assertEqual(self.store.settings_path.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()
