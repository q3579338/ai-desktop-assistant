"""Memory commits use isolated fixture files; no real chats or models are read."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from starpet.companion import ChatError, ChatStore
from starpet.storage import atomic_json


ROOT = Path(__file__).resolve().parents[1]


class MemoryTransactionTests(unittest.TestCase):
    def setUp(self):
        self.state = ROOT / 'evidence/tests' / ('memory-transactions-' + uuid.uuid4().hex[:12])
        self.store = ChatStore(self.state)

    def seed(self):
        self.store.add_facts(['原来的第一条', '原来的第二条'])
        self.store.commit_turn('旧问题', '旧回答')

    def snapshot(self):
        return (copy.deepcopy(self.store.facts), copy.deepcopy(self.store.turns),
                copy.deepcopy(self.store.saved_turns),
                self.store.memory_path.read_bytes() if self.store.memory_path.exists() else None)

    def fill(self, count):
        texts = ['已有条目 %02d' % n for n in range(count)]
        for start in range(0, count, 8):
            self.store.add_facts(texts[start:start + 8])
        return texts

    def test_failed_memory_changes_keep_all_state_and_cannot_leak_into_later_chat(self):
        self.seed()
        identity = self.store.facts[0]['id']
        operations = {
            'add': lambda: self.store.add_fact('未保存的新条目'),
            'batch': lambda: self.store.add_facts(['未保存一', '未保存二']),
            'edit': lambda: self.store.edit_fact(identity, '未保存的修改'),
            'remove': lambda: self.store.remove_fact(identity),
            'clear_facts': self.store.remove_fact,
            'clear_history': self.store.clear_history,
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                before = self.snapshot()
                with patch('starpet.companion.atomic_json', side_effect=OSError('fixture disk full')):
                    with self.assertRaises(ChatError):
                        operation()
                self.assertEqual(self.snapshot(), before)
                self.store.commit_turn('失败后可以继续 ' + name, '本轮成功回复')
                restored = ChatStore(self.state)
                self.assertEqual(restored.facts, before[0])
                self.assertEqual(restored.turns[:-1], before[2])
                self.assertEqual(restored.turns[-1]['user'], '失败后可以继续 ' + name)

    def test_atomic_replace_failure_preserves_old_bytes_and_cleans_temporary_file(self):
        self.seed()
        before = self.snapshot()
        with patch('starpet.storage.os.replace', side_effect=PermissionError('fixture locked')):
            with self.assertRaises(ChatError):
                self.store.add_facts(['新条目一', '新条目二'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.state.glob('companion-memory.json.*.tmp')), [])
        self.assertEqual(self.store.add_facts(['新条目一', '新条目二']), ['新条目一', '新条目二'])
        self.assertEqual(ChatStore(self.state).facts, self.store.facts)

    def test_first_save_failure_does_not_create_memory_or_in_memory_candidates(self):
        with patch('starpet.storage.os.replace', side_effect=PermissionError('fixture locked')):
            with self.assertRaises(ChatError):
                self.store.add_facts(['第一条也需保存成功'])
        self.assertEqual(self.snapshot(), ([], [], [], None))
        self.assertEqual(list(self.state.glob('*.tmp')), [])
        self.assertEqual(self.store.add_fact('后续明确确认'), '后续明确确认')
        self.assertEqual([row['text'] for row in ChatStore(self.state).facts], ['后续明确确认'])

    def test_fiftieth_fact_is_preserved_and_fifty_first_is_rejected_explicitly(self):
        texts = self.fill(50)
        before = self.snapshot()
        with patch('starpet.companion.atomic_json', wraps=atomic_json) as write:
            with self.assertRaisesRegex(ChatError, '50'):
                self.store.add_fact('不能挤掉最早条目')
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([row['text'] for row in ChatStore(self.state).facts], texts)
        with patch('starpet.companion.atomic_json') as write:
            self.assertEqual(self.store.add_fact('  ' + texts[0] + '  '), texts[0])
            self.assertEqual(self.store.add_facts([texts[-1], texts[0]]), [])
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_capacity_failure_rolls_back_entire_batch_after_exact_deduplication(self):
        texts = self.fill(49)
        before = self.snapshot()
        with self.assertRaisesRegex(ChatError, '本次没有新增'):
            self.store.add_facts([texts[0], '第50条候选', '第51条候选', '第50条候选'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.add_facts([texts[0], '第50条候选', '第50条候选']), ['第50条候选'])
        self.assertEqual(len(self.store.facts), 50)
        self.assertEqual(self.store.facts[:49], before[0])

    def test_batch_returns_only_new_trimmed_texts_and_writes_exactly_once(self):
        self.seed()
        before = copy.deepcopy(self.store.facts)
        with patch('starpet.companion.atomic_json', wraps=atomic_json) as write:
            added = self.store.add_facts([' 原来的第一条 ', '喜欢茶', ' 喜欢茶 ', 'TEA', 'tea'])
        self.assertEqual(added, ['喜欢茶', 'TEA', 'tea'])
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.store.facts[:2], before)
        self.assertEqual([row['text'] for row in self.store.facts[2:]], added)
        self.assertEqual(len({row['id'] for row in self.store.facts}), 5)
        self.assertEqual(ChatStore(self.state).facts, self.store.facts)
        added.append('修改返回值不修改存储')
        self.assertEqual(len(self.store.facts), 5)

    def test_empty_and_duplicate_only_batches_are_noops(self):
        self.seed()
        before = self.snapshot()
        with patch('starpet.companion.atomic_json') as write:
            self.assertEqual(self.store.add_facts([]), [])
            self.assertEqual(self.store.add_facts(['原来的第一条'] * 8), [])
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_batch_validates_every_item_before_any_mutation_or_write(self):
        self.seed()
        before = self.snapshot()
        invalid = (None, '不接受整串代替列表', ('元组',), {'字典': '值'}, ['条目'] * 9,
                   ['可用的第一条', ''], ['可用的第一条', ' \n\t '],
                   ['可用的第一条', 42], ['可用的第一条', None],
                   ['原来的第一条', '长' * 301], ['可用的第一条', '\ud800'])
        with patch('starpet.companion.atomic_json') as write:
            for batch in invalid:
                with self.subTest(batch=repr(batch)):
                    with self.assertRaises(ChatError):
                        self.store.add_facts(batch)
                    self.assertEqual(self.snapshot(), before)
            write.assert_not_called()
        texts = ['字' * 300] + ['第%d项' % n for n in range(7)]
        self.assertEqual(self.store.add_facts(texts), texts)

    def test_edit_missing_or_invalid_fact_cannot_change_old_value(self):
        self.seed()
        before = self.snapshot()
        with patch('starpet.companion.atomic_json') as write:
            with self.assertRaisesRegex(ChatError, '不存在'):
                self.store.edit_fact('missing-id', '新的文字')
            with self.assertRaises(ChatError):
                self.store.edit_fact(self.store.facts[0]['id'], ' ')
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        identity = self.store.facts[0]['id']
        self.assertIsNone(self.store.edit_fact(identity, ' 修改后的记忆 '))
        self.assertEqual(self.store.facts[0]['id'], identity)
        self.assertEqual(self.store.facts[0]['createdAt'], before[0][0]['createdAt'])
        self.assertEqual(ChatStore(self.state).facts[0]['text'], '修改后的记忆')

    def test_failed_reply_stays_visible_but_fact_saves_and_later_chat_do_not_persist_it(self):
        self.seed()
        before = self.snapshot()
        summary = {'memoryCount': 2, 'historyRounds': 1, 'memoryEnabled': True, 'taskStateShared': False}
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture unavailable')):
            with self.assertRaisesRegex(ChatError, '不会自动补存'):
                self.store.commit_turn('未保存问题', '已收到但未保存的回答', context=summary)
        self.assertEqual(self.store.facts, before[0])
        self.assertEqual(self.store.saved_turns, before[2])
        self.assertEqual(self.store.memory_path.read_bytes(), before[3])
        self.assertEqual(self.store.turns[-1]['assistant'], '已收到但未保存的回答')
        self.assertEqual(self.store.turns[-1]['context'], summary)
        self.store.add_fact('保存新事实不会补存失败历史')
        self.store.edit_fact(self.store.facts[0]['id'], '只修改这条事实')
        self.store.remove_fact(self.store.facts[1]['id'])
        self.store.persist()
        self.assertEqual(ChatStore(self.state).turns, before[2])
        self.store.commit_turn('后来成功问题', '后来成功回答')
        self.assertEqual([row['user'] for row in self.store.turns], ['旧问题', '未保存问题', '后来成功问题'])
        self.assertEqual([row['user'] for row in ChatStore(self.state).turns], ['旧问题', '后来成功问题'])

    def test_reenabling_history_does_not_backfill_replies_from_disabled_period(self):
        self.seed()
        self.store.save_settings(saveHistory=False)
        self.store.commit_turn('关闭保存期间的问题', '只在本次窗口')
        self.store.add_facts(['与临时聊天无关的明确记忆'])
        self.store.save_settings(saveHistory=True)
        self.store.commit_turn('重新开启后问题', '这一轮可以保存')
        self.assertEqual(len(self.store.turns), 3)
        self.assertEqual([row['user'] for row in ChatStore(self.state).turns], ['旧问题', '重新开启后问题'])
        self.assertEqual([row['user'] for row in self.store.saved_turns], ['旧问题', '重新开启后问题'])

    def test_many_failed_replies_do_not_evict_durable_history(self):
        self.seed()
        before = self.snapshot()
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture unavailable')):
            for n in range(65):
                with self.assertRaises(ChatError):
                    self.store.commit_turn('暂存问题 %d' % n, '暂存回答')
        self.assertEqual(len(self.store.turns), 60)
        self.assertEqual(self.store.saved_turns, before[2])
        self.assertEqual(self.store.memory_path.read_bytes(), before[3])
        self.store.commit_turn('恢复后成功', '成功回答')
        self.assertEqual([row['user'] for row in ChatStore(self.state).turns], ['旧问题', '恢复后成功'])

    def test_clear_history_is_transactional_even_when_window_has_unsaved_replies(self):
        self.seed()
        self.store.save_settings(saveHistory=False)
        self.store.commit_turn('仅在窗口的问题', '仅在窗口的回答')
        before = self.snapshot()
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture unavailable')):
            with self.assertRaises(ChatError):
                self.store.clear_history()
        self.assertEqual(self.snapshot(), before)
        self.store.clear_history()
        self.assertEqual(self.store.turns, [])
        self.assertEqual(self.store.saved_turns, [])
        self.assertEqual(ChatStore(self.state).turns, [])
        self.assertEqual(self.store.facts, before[0])
        self.store.save_settings(saveHistory=True)
        self.store.commit_turn('清空后的新问题', '新回答')
        self.assertEqual([row['user'] for row in ChatStore(self.state).turns], ['清空后的新问题'])

    def test_capacity_and_failed_batches_are_isolated_per_role(self):
        self.fill(50)
        tabby_path = self.store.memory_path
        tabby_bytes = tabby_path.read_bytes()
        for identity in ('robot', 'catgirl', 'custom-' + 'a' * 32):
            with self.subTest(role=identity):
                self.store.switch_role(identity, {'name': '测试角色'})
                self.assertEqual(self.store.add_facts([identity + ' 专属一', identity + ' 专属二']),
                                 [identity + ' 专属一', identity + ' 专属二'])
                before = self.snapshot()
                with patch('starpet.companion.atomic_json', side_effect=OSError('fixture unavailable')):
                    with self.assertRaises(ChatError):
                        self.store.add_facts(['失败候选不得属于任何角色'])
                self.assertEqual(self.snapshot(), before)
                self.store.commit_turn(identity + ' 问题', identity + ' 回答')
                self.assertEqual(ChatStore(self.state).facts, before[0])
                self.assertEqual(tabby_path.read_bytes(), tabby_bytes)
        self.store.switch_role('tabby', {'name': '测试小猫'})
        self.assertEqual(len(self.store.facts), 50)
        with self.assertRaisesRegex(ChatError, '50'):
            self.store.add_facts(['仍然需要先整理旧记忆'])
        self.assertEqual(tabby_path.read_bytes(), tabby_bytes)

    def test_legacy_memory_load_is_read_only_and_new_batch_preserves_metadata(self):
        self.state.mkdir(parents=True)
        legacy = {'facts': [{'id': 'legacy-id', 'text': '旧版事实', 'createdAt': 123}],
                  'turns': [{'user': '旧版问题', 'assistant': '旧版回答', 'createdAt': 456}]}
        self.store.memory_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=3), encoding='utf-8')
        before = self.store.memory_path.read_bytes()
        self.store = ChatStore(self.state)
        self.assertEqual(self.store.memory_path.read_bytes(), before)
        self.assertEqual(self.store.facts, legacy['facts'])
        self.assertEqual(self.store.turns, legacy['turns'])
        self.assertNotIn('context', self.store.turns[0])
        self.assertEqual(self.store.add_facts(['旧版事实']), [])
        self.assertEqual(self.store.memory_path.read_bytes(), before)
        self.assertEqual(self.store.add_facts(['明确新增']), ['明确新增'])
        restored = ChatStore(self.state)
        self.assertEqual(restored.facts[0], legacy['facts'][0])
        self.assertEqual(restored.turns, legacy['turns'])
        self.assertEqual(json.loads(self.store.memory_path.read_text(encoding='utf-8'))['schema'], 1)

    def test_legacy_missing_fact_id_remains_usable_without_rewrite_on_load(self):
        self.state.mkdir(parents=True)
        self.store.memory_path.write_text('{"facts":[{"text":"旧版没有ID"}],"turns":[]}', encoding='utf-8')
        before = self.store.memory_path.read_bytes()
        self.store = ChatStore(self.state)
        self.assertTrue(self.store.facts[0]['id'])
        self.assertEqual(self.store.facts[0]['createdAt'], 0)
        self.assertEqual(self.store.memory_path.read_bytes(), before)
        self.store.add_facts(['新确认事实'])
        self.assertEqual(ChatStore(self.state).facts, self.store.facts)


if __name__ == '__main__':
    unittest.main()
