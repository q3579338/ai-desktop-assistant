"""Actual request-context counts and private, backwards-compatible history."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid

from starpet.companion import ChatStore, validated_settings


ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'memoryCount', 'historyRounds', 'memoryEnabled', 'taskStateShared'}


def summary(memory=0, history=0, enabled=True, tasks=False):
    return {'memoryCount': memory, 'historyRounds': history,
            'memoryEnabled': enabled, 'taskStateShared': tasks}


class ChatContextTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT/'evidence/tests').resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.state = self.base/('chat-context-'+uuid.uuid4().hex)
        self.state.mkdir()
        self.store = ChatStore(self.state)

    def tearDown(self):
        resolved = self.state.resolve()
        if resolved.parent != self.base or not resolved.name.startswith('chat-context-'):
            raise RuntimeError('Unexpected context fixture path')
        shutil.rmtree(resolved)

    def test_empty_context_is_compatible_and_does_not_write_or_change_provider(self):
        before = copy.deepcopy(self.store.settings)
        messages, info = self.store.context('你好')
        self.assertEqual(messages, self.store.messages('你好'))
        self.assertEqual(info, summary())
        self.assertEqual(messages[-1], {'role': 'user', 'content': '你好'})
        self.assertEqual(len(messages), 2)
        self.assertEqual(self.store.settings, before)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_summary_counts_only_facts_and_selected_history_actually_attached(self):
        self.store.add_fact('FACT_ONE: 喜欢短回答')
        self.store.add_fact('FACT_TWO: 喜欢茶')
        for n in range(4):
            self.store.commit_turn('USER_'+str(n), 'REPLY_'+str(n))
        self.store.save_settings(recentRounds=2)
        before = self.store.memory_path.read_bytes()
        messages, info = self.store.context('新问题')
        self.assertEqual(info, summary(2, 2))
        self.assertIn('FACT_ONE', messages[0]['content'])
        self.assertIn('FACT_TWO', messages[0]['content'])
        self.assertEqual([m['content'] for m in messages[1:-1]], ['USER_2','REPLY_2','USER_3','REPLY_3'])
        self.assertEqual(len(messages), info['historyRounds']*2+2)
        self.assertEqual(self.store.memory_path.read_bytes(), before)

    def test_memory_disabled_keeps_facts_but_neither_sends_nor_counts_them(self):
        self.store.add_fact('PRIVATE_FACT_DISABLED')
        self.store.commit_turn('保留历史', '历史回复')
        self.store.save_settings(useMemory=False)
        messages, info = self.store.context('问题')
        self.assertEqual(info, summary(0, 1, enabled=False))
        self.assertNotIn('PRIVATE_FACT_DISABLED', json.dumps(messages))
        self.assertEqual(self.store.facts[0]['text'], 'PRIVATE_FACT_DISABLED')

    def test_zero_recent_rounds_really_omits_history_and_counts_zero(self):
        self.store.commit_turn('PRIVATE_OLD_USER', 'PRIVATE_OLD_REPLY')
        self.store.save_settings(recentRounds=0)
        messages, info = self.store.context('问题')
        self.assertEqual(info['historyRounds'], 0)
        self.assertEqual(len(messages), 2)
        self.assertNotIn('PRIVATE_OLD', json.dumps(messages))

    def test_24000_character_budget_counts_exact_boundary_and_excludes_dropped_rounds(self):
        self.store.save_settings(recentRounds=24)
        # Two newest complete turns consume exactly 24,000 characters; the
        # older small turn is still excluded rather than counted from settings.
        self.store.commit_turn('OLD', 'older')
        for n in range(2):
            self.store.commit_turn(str(n)*4000, 'a'*8000)
        messages, info = self.store.context('问题')
        self.assertEqual(info['historyRounds'], 2)
        self.assertEqual(sum(len(m['content']) for m in messages[1:-1]), 24000)
        self.assertNotIn('OLD', json.dumps(messages))
        # A single additional character in the latest reply makes the preceding
        # 12,000-character round too large; only one round is actually sent.
        self.store.turns[-1]['assistant'] += 'b'
        messages, info = self.store.context('问题')
        self.assertEqual(info['historyRounds'], 1)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[1]['content'], '1'*4000)

    def test_recent_round_limit_is_honored_even_when_all_rounds_fit_budget(self):
        for n in range(30):
            self.store.commit_turn('u'+str(n), 'a'+str(n))
        self.store.save_settings(recentRounds=100)
        messages, info = self.store.context('问题')
        self.assertEqual(info['historyRounds'], 24)
        self.assertEqual(messages[1]['content'], 'u6')
        self.assertEqual(len(messages), 50)

    def test_task_state_summary_matches_only_the_count_block_actually_attached(self):
        snapshot = {'sessions': [{'clientLabel':'Codex','label':'工作中','prompt':'PRIVATE_TASK_BODY'},
                                 {'clientLabel':'Codex','label':'工作中','token':'PRIVATE_TOKEN'}]}
        messages, info = self.store.context('问题', snapshot)
        self.assertFalse(info['taskStateShared'])
        self.assertNotIn('Codex', messages[0]['content'])
        self.store.save_settings(shareTaskState=True)
        for missing in (None, {}):
            messages, info = self.store.context('问题', missing)
            self.assertFalse(info['taskStateShared'])
            self.assertNotIn('工作状态计数', messages[0]['content'])
        messages, info = self.store.context('问题', snapshot)
        self.assertTrue(info['taskStateShared'])
        self.assertIn('"Codex · 工作中": 2', messages[0]['content'])
        self.assertNotIn('PRIVATE_', json.dumps(messages))
        # An empty sessions aggregate is still a truthful attached count block.
        messages, info = self.store.context('问题', {'sessions': []})
        self.assertTrue(info['taskStateShared'])
        self.assertIn('工作状态计数：{}', messages[0]['content'])

    def test_commit_persists_the_captured_request_summary_not_later_memory_changes(self):
        self.store.add_fact('请求前的一条事实')
        _, info = self.store.context('问题')
        self.store.add_fact('回复到来前才新增的事实')
        self.store.commit_turn('问题', '回答', context=info)
        info['memoryCount'] = 49
        saved = self.store.turns[-1]['context']
        self.assertEqual(saved, summary(1))
        self.assertEqual(set(saved), FIELDS)
        self.assertEqual(ChatStore(self.state).turns[-1]['context'], summary(1))
        self.assertEqual(json.loads(self.store.memory_path.read_text(encoding='utf-8'))['turns'][-1]['context'], summary(1))

    def test_unknown_context_keys_are_removed_without_serializing_private_objects(self):
        extra = {**summary(2, 3), 'memoryText':'PRIVATE_FACT', 'credentials':{'token':'PRIVATE_TOKEN'},
                 'prompt': ['PRIVATE_PROMPT'], 'object': object()}
        self.store.commit_turn('问题', '回答', context=extra)
        self.assertEqual(self.store.turns[-1]['context'], summary(2, 3))
        self.assertNotIn('PRIVATE_', self.store.memory_path.read_text(encoding='utf-8'))
        self.assertEqual(set(ChatStore(self.state).turns[-1]['context']), FIELDS)

    def test_invalid_context_types_missing_fields_and_out_of_range_counts_are_unknown(self):
        invalid = [None, False, 1, 'PRIVATE_CONTEXT', [], object(), {},
                   {**summary(), 'memoryCount': True}, {**summary(), 'memoryCount': -1},
                   {**summary(), 'memoryCount': 51}, {**summary(), 'memoryCount': '1'},
                   {**summary(), 'historyRounds': False}, {**summary(), 'historyRounds': 25},
                   {**summary(), 'historyRounds': 1.0}, {**summary(), 'historyRounds': []},
                   {**summary(), 'memoryEnabled': 1}, {**summary(), 'taskStateShared': 'false'},
                   {**summary(), 'taskStateShared': {}}, summary(1, enabled=False)]
        for index, info in enumerate(invalid):
            with self.subTest(case=index):
                self.store.commit_turn('问题'+str(index), '回答', context=info)
                self.assertNotIn('context', self.store.turns[-1])
        self.assertTrue(all('context' not in row for row in ChatStore(self.state).turns))

    def test_local_remember_commands_do_not_claim_a_model_request(self):
        text = '记住：这个偏好只保存在隔离测试目录'
        reply = self.store.local_command(text)
        self.assertIn('已在本机记住', reply)
        self.store.commit_turn(text, reply, 'local', context=summary(5, 8, tasks=True))
        self.assertNotIn('context', self.store.turns[-1])
        loaded = ChatStore(self.state)
        self.assertNotIn('context', loaded.turns[-1])
        # The future real request does include that fact and local-command turn.
        messages, info = loaded.context('现在发送的问题')
        self.assertEqual(info, summary(1, 1))
        self.assertIn('隔离测试目录', messages[0]['content'])

    def test_old_history_and_new_sanitized_context_load_without_rewriting_original_file(self):
        rows = [{'user':'旧问题','assistant':'旧回答','createdAt':123,'source':'model'},
                {'user':'新问题','assistant':'新回答','source':'model',
                 'context':{**summary(1, 2), 'secret':'PRIVATE_DO_NOT_COPY'}},
                {'user':'坏摘要','assistant':'仍保留回答','context':{'historyRounds':100}},
                {'user':'本机命令','assistant':'本机回复','source':'local','context':summary(1)}]
        self.store.memory_path.write_text(json.dumps({'schema':1,'facts':[],'turns':rows},
                                                     ensure_ascii=False, indent=3), encoding='utf-8')
        original = self.store.memory_path.read_bytes()
        loaded = ChatStore(self.state)
        self.assertEqual(loaded.turns[0], rows[0])
        self.assertEqual(loaded.turns[1]['context'], summary(1, 2))
        self.assertNotIn('context', loaded.turns[2])
        self.assertNotIn('context', loaded.turns[3])
        self.assertNotIn('PRIVATE_', json.dumps(loaded.turns))
        self.assertEqual(loaded.memory_path.read_bytes(), original)

    def test_disabling_history_save_does_not_persist_new_context_or_rewrite_old_history(self):
        self.store.commit_turn('已保存问题', '已保存回复', context=summary())
        original = self.store.memory_path.read_bytes()
        self.store.save_settings(saveHistory=False)
        self.store.commit_turn('只在当前窗口', '不保存', context=summary(0, 1))
        self.assertEqual(self.store.turns[-1]['context'], summary(0, 1))
        self.assertEqual(self.store.memory_path.read_bytes(), original)
        self.assertEqual(len(ChatStore(self.state).turns), 1)

    def test_role_facts_history_and_context_metadata_remain_independent(self):
        self.store.add_fact('TABBY_ONLY_FACT')
        _, info = self.store.context('TABBY_QUESTION')
        self.store.commit_turn('TABBY_QUESTION', 'TABBY_REPLY', context=info)
        tabby_bytes = self.store.memory_path.read_bytes()
        self.store.switch_role('robot', {'name':'Robot','persona':'Robot persona',
                                       'personality':'concise','formMode':'cat'})
        messages, info = self.store.context('机器人问题')
        self.assertEqual(info, summary())
        self.assertNotIn('TABBY_', json.dumps(messages))
        self.store.add_fact('ROBOT_ONLY_FACT_A'); self.store.add_fact('ROBOT_ONLY_FACT_B')
        _, info = self.store.context('ROBOT_QUESTION')
        self.store.commit_turn('ROBOT_QUESTION', 'ROBOT_REPLY', context=info)
        self.assertEqual(ChatStore(self.state).turns[-1]['context'], summary(2))
        self.store.switch_role('tabby', {})
        self.assertEqual(self.store.turns[-1]['context'], summary(1))
        self.assertNotIn('ROBOT_', json.dumps(self.store.context('问题')))
        self.assertEqual(self.store.memory_path.read_bytes(), tabby_bytes)

    def test_proactive_settings_default_off_and_validate_without_changing_provider(self):
        legacy = {'provider':'codebuddy', 'name':'旧角色', 'recentRounds':3}
        path = self.store.settings_path
        path.write_text(json.dumps(legacy, ensure_ascii=False, indent=3), encoding='utf-8')
        original = path.read_bytes()
        loaded = ChatStore(self.state)
        self.assertFalse(loaded.settings['proactiveEnabled'])
        self.assertFalse(loaded.settings['proactiveMemory'])
        self.assertEqual(loaded.settings['proactiveMinutes'], 10)
        self.assertEqual(loaded.settings['provider'], 'codebuddy')
        self.assertEqual(path.read_bytes(), original)
        for value, expected in ((-10,5),(5,5),(10,10),(60,60),(999,60),('20',10),(True,10),(2.5,10)):
            with self.subTest(minutes=value):
                result = validated_settings({**legacy, 'proactiveEnabled':True,
                    'proactiveMemory':True, 'proactiveMinutes':value})
                self.assertEqual(result['proactiveMinutes'], expected)
                self.assertTrue(result['proactiveEnabled']); self.assertTrue(result['proactiveMemory'])
                self.assertEqual(result['provider'], 'codebuddy')
        for value in ('true',1,[],None):
            result = validated_settings({'proactiveEnabled':value,'proactiveMemory':value})
            self.assertFalse(result['proactiveEnabled']); self.assertFalse(result['proactiveMemory'])
        loaded.save_settings(proactiveEnabled=True, proactiveMemory=True, proactiveMinutes=5)
        restored = ChatStore(self.state)
        self.assertEqual([restored.settings[key] for key in
            ('proactiveEnabled','proactiveMemory','proactiveMinutes')], [True, True, 5])
        self.assertEqual(restored.settings['provider'], 'codebuddy')


if __name__ == '__main__':
    unittest.main()
