import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from starpet.companion import ChatStore, ChatError, valid_role_id
from starpet.storage import atomic_json


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    'tabby': {'name': '团团', 'persona': '小猫的默认设定', 'personality': 'playful', 'formMode': 'chat'},
    'catgirl': {'name': '橘宁', 'persona': '成年猫娘的默认设定', 'personality': 'gentle', 'formMode': 'catgirl'},
    'robot': {'name': '小序', 'persona': '机器人的默认设定', 'personality': 'concise', 'formMode': 'cat'},
}


class RoleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.state = ROOT / 'evidence' / 'tests' / ('role-memory-' + uuid.uuid4().hex[:10])
        self.store = ChatStore(self.state)

    def seed_original(self):
        self.store.save_settings(name='我的橘猫', persona='这是我定制的橘猫设定',
                                 provider='ollama', model='fixture-local', address='星宝',
                                 shareTaskState=True, recentRounds=4, activity='calm', bubbleMode='events')
        self.store.add_fact('TABBY_ONLY：我喜欢橘猫')
        self.store.commit_turn('TABBY_CHAT：你好', 'TABBY_REPLY：你好呀')

    def test_legacy_settings_and_memory_load_without_rewriting_original_bytes(self):
        self.state.mkdir(parents=True)
        legacy = {'name': '老朋友', 'persona': '旧版自定义设定', 'provider': 'codex', 'recentRounds': 3}
        memory = {'schema': 1, 'facts': [{'id': 'legacy-fact', 'text': '原来的记忆', 'createdAt': 123}],
                  'turns': [{'user': '旧问题', 'assistant': '旧回答', 'createdAt': 456, 'source': 'model'}]}
        settings_path = self.state / 'companion-settings.json'
        memory_path = self.state / 'companion-memory.json'
        settings_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=3), encoding='utf-8')
        memory_path.write_text(json.dumps(memory, ensure_ascii=False, indent=3), encoding='utf-8')
        before = (settings_path.read_bytes(), memory_path.read_bytes())
        loaded = ChatStore(self.state)
        self.assertEqual(loaded.settings['roleId'], 'tabby')
        self.assertEqual(loaded.memory_path, memory_path)
        self.assertEqual(loaded.facts, memory['facts'])
        self.assertEqual(loaded.turns, memory['turns'])
        self.assertEqual(loaded.settings['name'], '老朋友')
        self.assertEqual((settings_path.read_bytes(), memory_path.read_bytes()), before)
        self.assertFalse((self.state / 'role-memory').exists())
        self.assertFalse((self.state / 'role-profiles.json').exists())

    def test_roles_have_independent_facts_history_and_persisted_selected_identity(self):
        self.seed_original()
        original = self.store.memory_path.read_bytes()
        self.store.switch_role('catgirl', PROFILES['catgirl'])
        self.assertEqual(self.store.facts, [])
        self.assertEqual(self.store.turns, [])
        self.assertNotIn('TABBY_', json.dumps(self.store.messages('你好')))
        self.store.add_fact('CATGIRL_ONLY：我喜欢喝茶')
        self.store.commit_turn('CATGIRL_CHAT：晚上好', 'CATGIRL_REPLY：晚上好')
        girl_memory = self.store.memory_path.read_bytes()
        self.store.switch_role('robot', PROFILES['robot'])
        self.assertEqual(self.store.facts, [])
        self.assertEqual(self.store.turns, [])
        self.store.add_fact('ROBOT_ONLY：先列步骤')
        self.store.commit_turn('ROBOT_CHAT：开始吧', 'ROBOT_REPLY：好的')
        for identity, own_marker, excluded in (
                ('tabby', 'TABBY_', ('CATGIRL_', 'ROBOT_')),
                ('catgirl', 'CATGIRL_', ('TABBY_', 'ROBOT_')),
                ('robot', 'ROBOT_', ('TABBY_', 'CATGIRL_'))):
            self.store.switch_role(identity, PROFILES[identity])
            reloaded = ChatStore(self.state)
            self.assertEqual(reloaded.settings['roleId'], identity)
            content = json.dumps(reloaded.messages('你记得什么'))
            self.assertIn(own_marker, content)
            for marker in excluded:
                self.assertNotIn(marker, content)
        self.assertEqual((self.state / 'companion-memory.json').read_bytes(), original)
        self.assertEqual((self.state / 'role-memory/catgirl.json').read_bytes(), girl_memory)

    def test_returning_restores_custom_persona_and_shared_connection_preferences(self):
        self.seed_original()
        role_fields = {'name', 'persona', 'personality', 'formMode', 'roleId', 'useRoleAppearance'}
        shared = {key: value for key, value in self.store.settings.items() if key not in role_fields}
        self.store.switch_role('catgirl', PROFILES['catgirl'])
        self.store.save_settings(name='我取的名字', persona='猫娘也要有自己的判断', personality='concise')
        self.store.switch_role('robot', PROFILES['robot'])
        self.store.switch_role('catgirl', PROFILES['catgirl'])
        self.assertEqual(self.store.settings['name'], '我取的名字')
        self.assertEqual(self.store.settings['persona'], '猫娘也要有自己的判断')
        self.assertEqual(self.store.settings['personality'], 'concise')
        self.store.switch_role('tabby', PROFILES['tabby'])
        self.assertEqual(self.store.settings['name'], '我的橘猫')
        self.assertEqual(self.store.settings['persona'], '这是我定制的橘猫设定')
        self.assertEqual({key: value for key, value in self.store.settings.items() if key not in role_fields}, shared)
        self.assertEqual(ChatStore(self.state).settings, self.store.settings)

    def test_custom_identity_uses_its_own_file_and_cannot_escape_state_directory(self):
        self.seed_original()
        custom_id = 'custom-' + 'a' * 32
        self.store.switch_role(custom_id, {'name': '自定义助手', 'persona': '我的图片助手'})
        self.store.add_fact('CUSTOM_ONLY：自定义偏好')
        self.assertEqual(self.store.memory_path, self.state / 'role-memory' / (custom_id + '.json'))
        before_settings = self.store.settings_path.read_bytes()
        before_memory = self.store.memory_path.read_bytes()
        invalid_ids = ('../other', '..\\other', 'C:\\outside', '/tmp/outside',
                       'custom-' + 'A' * 32, 'custom-' + 'a' * 31, '', None, 1)
        for identity in invalid_ids:
            with self.subTest(identity=identity):
                self.assertFalse(valid_role_id(identity))
                with self.assertRaises(ChatError):
                    self.store.switch_role(identity, PROFILES['tabby'])
        self.assertEqual(self.store.settings_path.read_bytes(), before_settings)
        self.assertEqual(self.store.memory_path.read_bytes(), before_memory)

    def test_save_settings_cannot_switch_role_or_partially_apply_other_fields(self):
        self.seed_original()
        before = copy.deepcopy(self.store.settings)
        before_file = self.store.settings_path.read_bytes()
        with self.assertRaises(ChatError):
            self.store.save_settings(roleId='robot', name='不得生效')
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.store.settings_path.read_bytes(), before_file)
        self.assertEqual(self.store.memory_path, self.state / 'companion-memory.json')

    def test_invalid_stored_identity_falls_back_without_reading_or_rewriting_another_file(self):
        self.seed_original()
        self.store.settings_path.write_text(json.dumps({**self.store.settings, 'roleId': '../outside'}), encoding='utf-8')
        outside = self.state / 'outside.json'
        outside.write_text('{"facts":[{"id":"private","text":"DO_NOT_LOAD"}]}', encoding='utf-8')
        before = (self.store.settings_path.read_bytes(), outside.read_bytes())
        loaded = ChatStore(self.state)
        self.assertEqual(loaded.settings['roleId'], 'tabby')
        self.assertNotIn('DO_NOT_LOAD', json.dumps(loaded.facts))
        self.assertEqual((self.store.settings_path.read_bytes(), outside.read_bytes()), before)

    def test_failed_role_switch_keeps_active_memory_target_and_restart_identity(self):
        self.seed_original()
        for fail_at in (self.store.profiles_path, self.store.settings_path):
            with self.subTest(fail_at=fail_at.name):
                before_settings = copy.deepcopy(self.store.settings)
                before_facts = copy.deepcopy(self.store.facts)
                before_turns = copy.deepcopy(self.store.turns)
                before_file = self.store.memory_path.read_bytes()
                def fail_selected(path, value):
                    if Path(path) == fail_at:
                        raise OSError('fixture write failure')
                    return atomic_json(path, value)
                with patch('starpet.companion.atomic_json', side_effect=fail_selected):
                    with self.assertRaises(ChatError):
                        self.store.switch_role('catgirl', PROFILES['catgirl'])
                self.assertEqual(self.store.settings, before_settings)
                self.assertEqual(self.store.facts, before_facts)
                self.assertEqual(self.store.turns, before_turns)
                self.assertEqual(self.store.memory_path, self.state / 'companion-memory.json')
                self.assertEqual(self.store.memory_path.read_bytes(), before_file)
                self.assertEqual(ChatStore(self.state).settings['roleId'], 'tabby')
                self.store.commit_turn('写失败后继续和原角色聊天', '仍是原角色')
                self.assertFalse((self.state / 'role-memory/catgirl.json').exists())

    def test_failed_memory_write_cannot_leak_pending_fact_into_another_role(self):
        self.seed_original()
        before_file = self.store.memory_path.read_bytes()
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture no space')):
            with self.assertRaises(ChatError):
                self.store.add_fact('UNSAVED_TABBY_FACT')
        self.assertEqual(self.store.memory_path.read_bytes(), before_file)
        self.store.switch_role('catgirl', PROFILES['catgirl'])
        self.store.add_fact('只属于猫娘')
        self.assertNotIn('UNSAVED_TABBY_FACT', self.store.memory_path.read_text(encoding='utf-8'))
        self.store.switch_role('tabby', PROFILES['tabby'])
        self.assertNotIn('UNSAVED_TABBY_FACT', json.dumps(self.store.facts))
        self.assertEqual(self.store.memory_path.read_bytes(), before_file)


if __name__ == '__main__':
    unittest.main()
