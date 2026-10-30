"""Connection checks use isolated drafts; all account/model calls are substitutes."""
import copy
from contextlib import ExitStack
import threading
import unittest
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture, QApplication
from starpet.companion import ChatError


class ConnectionDraftUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Qt offscreen required; no foreground fallback')

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.w, self.c, self.store = self.f.window, self.f.c, self.f.store
        self.store.save_settings(
            provider='codex', model='saved-codex',
            modelsByProvider={'codex': 'saved-codex', 'ollama': 'saved-local',
                              'codebuddy': 'saved-buddy'},
            codexPath=str(self.f.folder / 'saved-codex.exe'),
            codebuddyPath=str(self.f.folder / 'saved-codebuddy.exe'),
            localUrl='http://127.0.0.1:19101', timeoutSeconds=87)
        self.w.load_settings()

    def tearDown(self):
        self.f.close()

    def snapshot(self):
        return {
            'files': {p.relative_to(self.f.state).as_posix(): p.read_bytes()
                      for p in self.f.state.rglob('*') if p.is_file()},
            'settings': copy.deepcopy(self.store.settings),
            'prefs': copy.deepcopy(self.c.prefs.values),
            'facts': copy.deepcopy(self.store.facts),
            'turns': copy.deepcopy(self.store.turns),
            'saved_turns': copy.deepcopy(self.store.saved_turns),
        }

    def forbid_saves(self):
        guards = ExitStack()
        for owner in (self.w, self.store):
            guards.enter_context(patch.object(owner, 'save_settings',
                side_effect=AssertionError('Connection action tried to save settings')))
        guards.enter_context(patch.object(self.c.prefs, 'save',
            side_effect=AssertionError('Connection action tried to save preferences')))
        return guards

    def draft(self, provider='ollama', model='draft-local'):
        self.w.provider.setCurrentIndex(self.w.provider.findData(provider))
        self.w.model.setCurrentText(model)
        self.w.local_url.setText(' http://127.0.0.1:19102 ')
        self.w.codex_path.setText(' ' + str(self.f.folder / 'draft-codex.exe') + ' ')
        self.w.codebuddy_path.setText(' ' + str(self.f.folder / 'draft-codebuddy.exe') + ' ')

    def unrelated_drafts(self):
        self.w.name.setText('未保存的名字')
        self.w.address.setText('未保存的称呼')
        self.w.persona.setPlainText('未' * 2101)
        self.w.timeout.setValue(300)
        self.w.rounds.setValue(0)
        self.w.use_memory.setChecked(False)
        self.w.save_history.setChecked(False)
        self.w.size.setValue(149)
        self.w.motion.setChecked(True)
        self.w.notifications.setChecked(False)
        self.w.workbuddy_tasks.setChecked(False)

    def expected_connection(self):
        expected = copy.deepcopy(self.store.settings)
        provider = self.w.provider.currentData()
        model = self.w.model.currentText().strip()
        expected.update(provider=provider, model=model,
            localUrl=self.w.local_url.text().strip(),
            codexPath=self.w.codex_path.text().strip(),
            codebuddyPath=self.w.codebuddy_path.text().strip())
        expected['modelsByProvider'][provider] = model
        return expected

    def begin_probe(self, models=None, error=None):
        self.f.chat.block = True
        self.f.chat.error = error
        models = ['discovered-a', 'discovered-b'] if models is None else models

        def backend(settings, messages, state, job, probe=False):
            self.f.chat(settings, messages, state, job, probe=probe)
            return models

        self.w.backend = backend
        self.w.probe()
        self.assertTrue(self.f.chat.entered.wait(1), self.w.feedback.text())
        self.assertEqual(self.w.job_kind, 'probe')

    def finish_probe(self):
        self.f.chat.release.set()
        self.f.wait_for(lambda: self.w.job is None)

    def test_probe_uses_only_connection_draft_and_never_saves_unrelated_edits(self):
        self.draft()
        self.unrelated_drafts()
        expected = self.expected_connection()
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            self.assertEqual(self.snapshot(), before)
            call = self.f.chat.calls[-1]
            self.assertEqual(call['settings'], expected)
            self.assertEqual(call['settings']['timeoutSeconds'], 87)
            self.assertEqual(call['messages'], [])
            self.assertTrue(call['probe'])
            self.assertEqual(call['state'], str(self.f.state))
            self.finish_probe()
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.w.model.currentText(), 'draft-local')
        self.assertEqual([self.w.model.itemText(i) for i in range(self.w.model.count())],
                         ['discovered-a', 'discovered-b'])
        self.assertEqual(self.w.persona.toPlainText(), '未' * 2101)
        self.assertEqual(self.w.name.text(), '未保存的名字')
        self.assertIn('保存', self.w.feedback.text())
        self.assertIn('检测没有保存', self.w.feedback.text())

    def test_successful_probe_autofills_empty_model_only_in_draft(self):
        self.draft(model='')
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            self.finish_probe()
        self.assertEqual(self.w.model.currentText(), 'discovered-a')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.settings['modelsByProvider']['ollama'], 'saved-local')

    def test_failed_probe_keeps_drafts_and_all_persistent_state(self):
        self.draft()
        self.unrelated_drafts()
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe(error=ChatError('隔离测试连接不可用'))
            self.assertEqual(self.snapshot(), before)
            self.finish_probe()
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.w.feedback.text(), '隔离测试连接不可用')
        self.assertEqual(self.w.model.currentText(), 'draft-local')
        self.assertEqual(self.w.model.count(), 0)
        self.assertEqual(self.w.persona.toPlainText(), '未' * 2101)

    def check_late_probe(self, change):
        self.draft(model='')
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            change()
            draft_after_change = self.w.connection_draft()
            self.finish_probe()
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.w.connection_draft(), draft_after_change)
        self.assertEqual(self.w.model.count(), 0)
        self.assertIn('保留当前草稿', self.w.feedback.text())

    def test_late_probe_does_not_fill_another_provider(self):
        self.check_late_probe(lambda: self.w.provider.setCurrentIndex(
            self.w.provider.findData('codebuddy')))

    def test_late_probe_does_not_replace_edited_model(self):
        self.check_late_probe(lambda: self.w.model.setCurrentText('new-choice'))

    def test_late_probe_does_not_fill_changed_endpoint(self):
        self.check_late_probe(lambda: self.w.local_url.setText('http://127.0.0.1:19103'))

    def test_late_probe_does_not_fill_changed_cli_path(self):
        self.check_late_probe(lambda: self.w.codebuddy_path.setText(
            str(self.f.folder / 'another-codebuddy.exe')))

    def connect_with_status(self, status, login_calls):
        def account(settings, state, job):
            return status

        def login(settings, state, job):
            login_calls.append(settings)
            return []

        with patch('starpet.codebuddy_backend.account_status', account), \
             patch('starpet.codebuddy_backend.launch_official_login', login):
            self.w.connect_button.click()
            self.f.wait_for(lambda: self.w.job is None)

    def test_signed_in_account_skips_the_login_window(self):
        calls = []
        self.connect_with_status('signed_in', calls)
        self.assertEqual(calls, [])
        self.assertIn('已登录，无需再次登录', self.w.feedback.text())
        self.assertIn('Codex 任务提醒', self.w.feedback.text())
        self.assertEqual(self.c.prefs.values['workbuddyEnabled'], True)
        self.assertEqual(self.c.prefs.values['codexEnabled'], True)

    def test_signed_out_account_opens_the_login_window(self):
        calls = []
        self.connect_with_status('signed_out', calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['provider'], 'codebuddy')
        self.assertIn('尚未登录', self.w.feedback.text())
        self.assertIn('官方窗口已打开', self.w.feedback.text())

    def test_missing_cli_keeps_task_reminders_without_login(self):
        calls = []
        self.connect_with_status('missing', calls)
        self.assertEqual(calls, [])
        self.assertIn('没有找到独立的官方 CodeBuddy CLI', self.w.feedback.text())
        self.assertIn('任务提醒不受影响', self.w.feedback.text())
        self.assertEqual(self.c.prefs.values['codexEnabled'], True)

    def test_one_button_enables_both_task_reminders_and_opens_login(self):
        self.c.core.health['codex'] = '只读监听已就绪 · 等待新事件'
        seen = {}

        def account(settings, state, job):
            return 'signed_out'

        def login(settings, state, job):
            seen['settings'] = copy.deepcopy(settings)
            seen['state'] = state
            return []

        before_files = self.snapshot()['files']
        with patch('starpet.codebuddy_backend.account_status', account), \
             patch('starpet.codebuddy_backend.launch_official_login', login):
            self.w.connect_button.click()
            self.f.wait_for(lambda: self.w.job is None)
        self.assertEqual(self.c.prefs.values['workbuddyEnabled'], True)
        self.assertEqual(self.c.prefs.values['codexEnabled'], True)
        self.assertTrue(self.c.core.snapshot()['enabled']['workbuddy'])
        self.assertTrue(self.c.core.snapshot()['enabled']['codex'])
        self.assertEqual(seen['settings']['provider'], 'codebuddy')
        self.assertEqual(seen['state'], self.f.state)
        self.assertIn('Codex 任务提醒', self.w.feedback.text())
        self.assertIn('官方窗口已打开', self.w.feedback.text())
        self.assertIn('保存连接设置', self.w.feedback.text())
        self.assertFalse((self.f.state / 'codebuddy-profile').exists())
        self.assertEqual(before_files.keys() - self.snapshot()['files'].keys(), set())

    def test_connect_reports_login_failure_without_dropping_task_reminders(self):
        def account(settings, state, job):
            return 'signed_out'

        def login(settings, state, job):
            raise ChatError('没有找到独立的官方 CodeBuddy CLI。')

        with patch('starpet.codebuddy_backend.account_status', account), \
             patch('starpet.codebuddy_backend.launch_official_login', login):
            self.w.connect_button.click()
            self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('没有找到独立的官方 CodeBuddy CLI', self.w.feedback.text())
        self.assertIn('Codex 任务提醒', self.w.feedback.text())
        self.assertEqual(self.c.prefs.values['codexEnabled'], True)
        self.assertEqual(self.c.prefs.values['workbuddyEnabled'], True)
        self.assertIsNone(self.w.job)

    def test_role_change_alone_discards_probe_result(self):
        self.draft(model='')
        self.begin_probe()
        # The normal role UI blocks while busy. Exercise an external store
        # change to prove the request is also guarded at result delivery.
        old_connection = self.w.connection_draft()[:-1]
        self.store.switch_role('robot', {'name': '隔离机器人', 'persona': '测试角色',
            'personality': 'concise', 'formMode': 'cat'})
        self.assertEqual(self.w.connection_draft()[:-1], old_connection)
        after_role_change = self.snapshot()
        with self.forbid_saves():
            self.finish_probe()
        self.assertEqual(self.snapshot(), after_role_change)
        self.assertEqual(self.w.model.currentText(), '')
        self.assertEqual(self.w.model.count(), 0)
        self.assertIn('保留当前草稿', self.w.feedback.text())

    def test_unrelated_edits_during_probe_do_not_discard_or_get_overwritten(self):
        self.draft(model='')
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            self.unrelated_drafts()
            self.finish_probe()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.w.model.currentText(), 'discovered-a')
        self.assertEqual(self.w.name.text(), '未保存的名字')
        self.assertEqual(self.w.timeout.value(), 300)
        self.assertEqual(self.w.persona.toPlainText(), '未' * 2101)

    def run_login(self, error=None):
        self.draft('codebuddy', 'draft-buddy')
        self.unrelated_drafts()
        expected = self.expected_connection()
        before = self.snapshot()
        entered, release = threading.Event(), threading.Event()
        calls, workers = [], []

        def fake_login(settings, state, job):
            workers.append(threading.current_thread())
            calls.append((copy.deepcopy(settings), state, job))
            entered.set()
            if not release.wait(3):
                raise ChatError('隔离登录替身未及时释放')
            if error:
                raise error
            return None

        try:
            with self.forbid_saves(), patch('starpet.codebuddy_backend.launch_official_login',
                                            side_effect=fake_login):
                self.w.login_codebuddy()
                self.assertTrue(entered.wait(1), self.w.feedback.text())
                self.assertEqual(self.w.job_kind, 'login')
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(calls[0][0], expected)
                self.assertEqual(calls[0][1], self.f.state)
                self.assertIs(calls[0][2], self.w.job)
                self.assertEqual(calls[0][0]['timeoutSeconds'], 87)
                release.set()
                self.f.wait_for(lambda: self.w.job is None)
                self.assertEqual(self.snapshot(), before)
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.f.chat.calls, [])
        self.assertEqual(self.w.model.currentText(), 'draft-buddy')
        self.assertEqual(self.w.persona.toPlainText(), '未' * 2101)
        self.assertEqual(self.store.settings['provider'], 'codex')
        self.assertEqual(self.store.settings['model'], 'saved-codex')
        self.assertEqual(self.store.settings['modelsByProvider']['codebuddy'], 'saved-buddy')

    def test_official_login_only_uses_connection_draft_without_saving(self):
        self.run_login()
        self.assertIn('保存连接设置', self.w.feedback.text())
        self.assertNotIn('登录成功', self.w.feedback.text())

    def test_failed_login_does_not_save_or_report_success(self):
        self.run_login(ChatError('隔离测试官方程序检查失败'))
        self.assertEqual(self.w.feedback.text(), '隔离测试官方程序检查失败')

    def test_official_login_rejects_non_codebuddy_selection(self):
        before = self.snapshot()
        with self.forbid_saves(), patch('starpet.codebuddy_backend.launch_official_login') as login:
            for provider in ('codex', 'ollama'):
                with self.subTest(provider=provider):
                    self.draft(provider)
                    self.w.login_codebuddy()
                    self.assertIsNone(self.w.job)
                    self.assertIn('选择', self.w.feedback.text())
                    self.assertIn('WorkBuddy / CodeBuddy', self.w.feedback.text())
                    self.assertEqual(self.snapshot(), before)
            login.assert_not_called()

    def test_cancelled_probe_cannot_later_fill_model_or_write_state(self):
        self.draft(model='')
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            self.w.cancel()
            self.f.chat.release.set()
            self.f.chat.finish()
            self.f.pump()
        self.assertIsNone(self.w.job)
        self.assertEqual(self.w.model.currentText(), '')
        self.assertEqual(self.w.model.count(), 0)
        self.assertEqual(self.snapshot(), before)

    def test_chat_after_probe_uses_saved_connection_and_context(self):
        self.draft(model='')
        self.unrelated_drafts()
        saved = copy.deepcopy(self.store.settings)
        before = self.snapshot()
        with self.forbid_saves():
            self.begin_probe()
            self.finish_probe()
            self.assertEqual(self.snapshot(), before)
            self.w.backend = self.f.chat
            self.f.send('请回答这个隔离测试问题。')
            self.f.wait_for(lambda: self.w.job is None)
        call = self.f.chat.calls[-1]
        self.assertFalse(call['probe'])
        self.assertEqual(call['settings'], saved)
        self.assertIn(saved['persona'], call['messages'][0]['content'])
        self.assertNotIn('未' * 2101, str(call['messages']))
        self.assertEqual(self.store.settings, saved)
        self.assertEqual(self.c.prefs.values, before['prefs'])
        self.assertEqual(self.store.facts, before['facts'])
        self.assertEqual(len(self.store.turns), len(before['turns']) + 1)
        self.assertEqual(self.store.turns[-1]['assistant'], self.f.chat.reply)
        self.assertEqual(self.store.saved_turns, self.store.turns)
        after_files = self.snapshot()['files']
        memory_file = self.store.memory_path.relative_to(self.f.state).as_posix()
        self.assertEqual({k: v for k, v in after_files.items() if k != memory_file},
                         {k: v for k, v in before['files'].items() if k != memory_file})


if __name__ == '__main__':
    unittest.main()
