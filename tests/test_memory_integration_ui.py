"""Memory review integration, using isolated offscreen state and a fake engine."""
import copy
import json
from unittest.mock import patch
import unittest

from test_companion_context_ui import ContextUIFixture, ChatError, ChatStore, QApplication, Qt


USER = '我平时喜欢喝茉莉花茶，回答请先说结论。'
ASSISTANT = '助手回复不应该被送去整理。'
TEXT = '用户喜欢茉莉花茶。'


class MemoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.w, self.store = self.f.window, self.f.store
        self.store.commit_turn(USER, ASSISTANT)
        self.f.chat.reply = json.dumps({'memories': [
            {'text': TEXT, 'sourceId': 'u2', 'quote': '喜欢喝茉莉花茶'}]}, ensure_ascii=False)

    def tearDown(self):
        self.f.close()

    def open(self):
        self.w.open_memory_review()
        self.f.pump()
        return self.w.memory_review

    def generate(self):
        d = self.open()
        d.generate_button.click()
        self.f.wait_for(lambda: self.w.job is None)
        return d

    def select(self, d):
        self.assertEqual(d.candidate_list.count(), 1)
        d.candidate_list.item(0).setCheckState(Qt.CheckState.Checked)

    def test_generation_scoped_and_not_saved_until_selected(self):
        before = self.f.saved_memory()
        turns = copy.deepcopy(self.store.turns)
        self.w.input.setPlainText('还没发送的输入不要整理')
        self.w.fact.setPlainText('手动记忆编辑框里的草稿也保留')
        d = self.generate()
        self.assertEqual(self.f.saved_memory(), before)
        self.assertFalse(d.save_button.isEnabled())
        call = self.f.chat.calls[0]
        data = json.loads(call['messages'][1]['content'])
        self.assertEqual(data['sources'][-1]['text'], USER)
        self.assertNotIn(ASSISTANT, str(call['messages']))
        self.assertNotIn(self.store.facts[0]['text'], str(call['messages']))
        self.assertNotIn('还没发送', str(call['messages']))
        self.select(d)
        d.editor.setPlainText('用户偏好茉莉花茶。')
        d.save_button.click()
        self.assertEqual(ChatStore(self.f.state).facts[-1]['text'], '用户偏好茉莉花茶。')
        self.assertEqual(self.store.turns, turns)
        self.assertEqual(d.candidate_list.count(), 0)
        self.assertIn('已保存 1 条新记忆', d.status.text())
        self.assertEqual(self.w.input.toPlainText(), '还没发送的输入不要整理')
        self.assertEqual(self.w.fact.toPlainText(), '手动记忆编辑框里的草稿也保留')

    def test_saved_provider_used_without_saving_settings_draft(self):
        settings = copy.deepcopy(self.store.settings)
        self.w.provider.setCurrentIndex(self.w.provider.findData('ollama'))
        self.w.model.setCurrentText('尚未保存的模型')
        d = self.generate()
        self.assertEqual(self.f.chat.calls[0]['settings'], settings)
        self.assertEqual(self.store.settings, settings)
        self.assertEqual(self.w.model.currentText(), '尚未保存的模型')
        self.assertIn('Codex', d.scope_note.text())

    def test_cancel_and_late_reply_do_not_persist_or_pollute_chat(self):
        before = self.f.saved_memory()
        self.f.chat.block = True
        d = self.open()
        d.generate_button.click()
        self.assertTrue(self.f.chat.entered.wait(2))
        d.stop_button.click()
        self.f.chat.finish()
        self.f.pump()
        self.assertIsNone(self.w.job)
        self.assertIsNone(self.w.memory_snapshot)
        self.assertEqual(d.candidate_list.count(), 0)
        self.assertEqual(self.f.saved_memory(), before)
        self.assertIsNone(self.w.preview_failed)
        self.assertEqual(self.w.error_text, '')

    def test_hiding_parent_cancels_and_releases_review(self):
        before = self.f.saved_memory()
        self.f.chat.block = True
        d = self.open()
        d.generate_button.click()
        self.assertTrue(self.f.chat.entered.wait(2))
        self.w.hide()
        self.assertIsNone(self.w.memory_review)
        self.assertIsNone(self.w.job)
        self.f.chat.finish()
        self.f.pump()
        self.assertEqual(self.f.saved_memory(), before)

    def test_cancelled_generation_cannot_clear_or_replace_new_job(self):
        before = self.f.saved_memory()
        self.f.chat.block = True
        d = self.open()
        d.generate_button.click()
        self.assertTrue(self.f.chat.entered.wait(2))
        old_sequence = self.w.sequence
        d.stop_button.click()
        d.generate_button.click()
        new_job = self.w.job
        self.w.results.put((old_sequence, 'memory', '{"memories":[]}', None))
        self.w.poll()
        self.assertIs(self.w.job, new_job)
        self.assertEqual(d.candidate_list.count(), 0)
        self.f.chat.finish()
        self.f.wait_for(lambda: self.w.job is None)
        self.assertEqual(d.candidate_list.count(), 1)
        self.assertEqual(self.f.saved_memory(), before)

    def test_role_changed_before_save_rejects_old_candidates(self):
        d = self.generate()
        self.select(d)
        self.f.c.switch_role('robot')  # Explicit test action bypasses modal UI.
        d.save_button.click()
        self.assertEqual(self.store.facts, [])
        self.assertFalse(self.store.memory_path.exists())
        self.assertIn('旧候选不能保存', d.status.text())
        self.assertEqual(d.candidate_list.count(), 0)

    def test_history_changed_while_waiting_invalidates_reply(self):
        self.f.chat.block = True
        d = self.open()
        d.generate_button.click()
        self.assertTrue(self.f.chat.entered.wait(2))
        self.store.clear_history()
        before = self.f.saved_memory()
        self.f.chat.release.set()
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('本次结果已作废', d.status.text())
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(d.candidate_list.count(), 0)

    def test_saved_connection_changed_while_waiting_invalidates_reply(self):
        self.f.chat.block = True
        d = self.open()
        d.generate_button.click()
        self.assertTrue(self.f.chat.entered.wait(2))
        self.store.save_settings(provider='ollama', model='fixture-new-model')
        before = self.f.saved_memory()
        self.f.chat.release.set()
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('本次结果已作废', d.status.text())
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(d.candidate_list.count(), 0)

    def test_saved_settings_changed_after_generation_prevent_confirmation(self):
        d = self.generate()
        self.select(d)
        self.store.save_settings(useMemory=False)
        before = self.f.saved_memory()
        d.save_button.click()
        self.assertIn('旧候选不能保存', d.status.text())
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(d.candidate_list.count(), 0)

    def test_write_failure_retains_selected_edit_for_explicit_retry(self):
        d = self.generate()
        self.select(d)
        d.editor.setPlainText('用户明确喜欢茉莉花茶。')
        before = self.f.saved_memory()
        with patch('starpet.companion.atomic_json', side_effect=OSError('fixture full disk')):
            d.save_button.click()
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(len(self.store.facts), 2)
        self.assertIn('未能写入', d.status.text())
        self.assertEqual(d.editor.toPlainText(), '用户明确喜欢茉莉花茶。')
        self.assertTrue(d.save_button.isEnabled())
        d.save_button.click()
        self.assertEqual(self.store.facts[-1]['text'], '用户明确喜欢茉莉花茶。')

    def test_full_memory_keeps_all_existing_facts_and_candidates(self):
        for index in range(48):
            self.store.add_fact(f'独立容量测试事实 {index}')
        d = self.generate()
        self.select(d)
        before = self.f.saved_memory()
        d.save_button.click()
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(len(self.store.facts), 50)
        self.assertIn('最多保存 50', d.status.text())
        self.assertEqual(d.candidate_list.count(), 1)

    def test_temporary_turn_can_be_confirmed_without_backfilling_history(self):
        self.store.save_settings(saveHistory=False)
        self.store.commit_turn('我希望被称作青团。', '临时回复')
        self.f.chat.reply = json.dumps({'memories': [
            {'text': '用户希望被称作青团。', 'sourceId': 'u3', 'quote': '被称作青团'}]}, ensure_ascii=False)
        d = self.generate()
        self.select(d)
        d.save_button.click()
        reloaded = ChatStore(self.f.state)
        self.assertEqual(reloaded.facts[-1]['text'], '用户希望被称作青团。')
        self.assertEqual(len(reloaded.turns), 2)
        self.assertNotIn('临时回复', str(reloaded.turns))

    def test_invalid_source_result_and_empty_result_never_write(self):
        before = self.f.saved_memory()
        self.f.chat.reply = '{"memories":[{"text":"错误事实","sourceId":"u2","quote":"并不存在的引用"}]}'
        d = self.generate()
        self.assertIn('引用不在', d.status.text())
        self.assertEqual(d.candidate_list.count(), 0)
        self.f.chat.reply = '{"memories":[]}'
        d.generate_button.click()
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('没有需要长期保存', d.status.text())
        self.assertEqual(self.f.saved_memory(), before)

    def test_clear_history_write_error_is_visible_and_keeps_history(self):
        before = self.f.saved_memory()
        with patch.object(self.w, 'confirm_clear', return_value=True), patch(
                'starpet.companion.atomic_json', side_effect=OSError('fixture full disk')):
            self.w.clear_chat()
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(len(self.store.turns), 2)
        self.assertIn('未能写入', self.w.feedback.text())

    def test_local_command_saved_fact_and_failed_history_reported_separately(self):
        original = self.store._write_memory
        calls = []
        def fail_second(facts, turns):
            calls.append(True)
            if len(calls) == 2:
                raise ChatError('隔离测试写入失败')
            return original(facts, turns)
        with patch.object(self.store, '_write_memory', side_effect=fail_second):
            self.f.send('记住：独立保存的用户事实。')
        self.assertEqual(ChatStore(self.f.state).facts[-1]['text'], '独立保存的用户事实。')
        self.assertEqual(len(ChatStore(self.f.state).turns), 2)
        self.assertEqual(len(self.store.turns), 3)
        self.assertIn('已完成本机记忆操作', self.w.feedback.text())
        self.assertIn('聊天记录未保存', self.w.feedback.text())
        self.assertEqual(self.f.chat.calls, [])


if __name__ == '__main__':
    unittest.main()
