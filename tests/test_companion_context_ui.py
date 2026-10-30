"""1.5 context and opt-in companionship UI, with isolated offscreen fixtures."""
import copy
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from starpet.chat_gui import CompanionWindow
from starpet.companion import ChatError, ChatJob, ChatStore, DEFAULTS
from starpet.core import Core
from starpet.gui import Controller
from starpet.proactive import ProactiveCompanion
from starpet.storage import Preferences


FACT_A = '隔离测试偏好：先讲结论。'
FACT_B = '隔离测试偏好：用自然中文。'
LEGACY_USER = '没有请求摘要的旧问题'
LEGACY_REPLY = '这是一条保留的旧回复。'
PREVIEW_REPLY = '慢慢来，留一点空闲'


class FixtureEngine:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.error = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.block = False
        self.threads = []

    def __call__(self, settings, messages, state, job, probe=False):
        self.threads.append(threading.current_thread())
        self.finished.clear()
        self.calls.append({'settings': copy.deepcopy(settings), 'messages': copy.deepcopy(messages),
                           'state': str(state), 'probe': probe})
        self.entered.set()
        try:
            if self.block and not self.release.wait(3):
                raise ChatError('隔离测试请求未及时释放。')
            if self.error:
                raise self.error
            return [] if probe else self.reply
        finally:
            self.finished.set()

    def finish(self):
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=2)
            if thread.is_alive():
                raise AssertionError('Fixture model worker did not exit')


class ContextUIFixture:
    """Shared only by the tests and the standalone offscreen screenshot script."""
    def __init__(self, app, folder=None):
        if app.platformName() != 'offscreen':
            raise RuntimeError('This fixture requires Qt offscreen; no foreground fallback')
        self.app = app
        self.base = (ROOT / 'evidence' / 'tests').resolve()
        self.folder = Path(folder).resolve() if folder else self.base / ('companion-context-' + uuid.uuid4().hex)
        self.folder.mkdir(parents=True, exist_ok=bool(folder))
        self.state = self.folder / 'isolated-state'
        self.chat = FixtureEngine('这是一条隔离测试回复，我会先讲结论。')
        self.preview = FixtureEngine(PREVIEW_REPLY)
        proactive = ProactiveCompanion(backend=self.preview)
        self.runtime = SimpleNamespace(close=lambda: None)
        with patch('starpet.local_runtime.LocalRuntime', return_value=self.runtime), patch('starpet.proactive.ProactiveCompanion', return_value=proactive):
            self.c = Controller(app, Core(), Preferences(self.state),
                SimpleNamespace(actions=queue.Queue()), SimpleNamespace(bytes_read=0, errors=0),
                self.state, self.folder / 'empty-profile', tray=False)
        self.c.action_timer.stop()
        self.c.panel.timer.stop()
        self.c.pet.timer.stop()
        self.store = self.c.companion_store
        self.store.save_settings(name='测试伙伴', address='测试用户', persona='这是隔离测试角色。',
                                 provider='codex', model='', recentRounds=8)
        self.store.add_fact(FACT_A)
        self.store.add_fact(FACT_B)
        self.store.commit_turn(LEGACY_USER, LEGACY_REPLY)
        self.window = CompanionWindow(self.c)
        self.c.companion = self.window
        self.window.backend = self.chat
        self.window.poller.stop()
        self.window.show()
        self.c.pet.show()
        self.app.processEvents()

    def pump(self):
        self.app.processEvents()
        self.window.poll()
        self.c.controls()

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            time.sleep(.002)
        raise AssertionError('Offscreen fixture operation did not finish')

    def send(self, text='请用一句话回答这个测试问题。'):
        self.window.input.setPlainText(text)
        self.window.send()

    def use_local(self):
        self.window.provider.setCurrentIndex(self.window.provider.findData('ollama'))
        self.window.local_url.setText('http://127.0.0.1:11536')
        self.window.model.setCurrentText('fixture-local-model')
        if not self.window.save_settings():
            raise AssertionError(self.window.feedback.text())

    def switch_role(self, identity):
        workshop = self.window.workshop
        for index in range(workshop.roles.count()):
            item = workshop.roles.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == identity:
                workshop.roles.setCurrentItem(item)
                workshop.activate()
                if self.store.settings['roleId'] != identity:
                    raise AssertionError(self.window.feedback.text())
                self.pump()
                return
        raise AssertionError('Fixture role missing')

    def saved_memory(self):
        return self.store.memory_path.read_bytes()

    def close(self, remove=True):
        for engine in (self.chat, self.preview):
            engine.finish()
        self.c.stop_chat()
        for widget in (self.window, self.c.pet, self.c.panel, self.c.completion_popup, self.c.focus_popup):
            widget.hide()
        if self.c.pet.clip_movie:
            self.c.pet.clip_movie.stop()
        self.app.aboutToQuit.disconnect(self.c.stop_chat)
        for widget in (self.window, self.c.pet, self.c.panel, self.c.completion_popup, self.c.focus_popup):
            widget.deleteLater()
        self.app.processEvents()
        if remove:
            target = self.folder.resolve()
            if target.parent != self.base or not target.name.startswith('companion-context-'):
                raise RuntimeError('Refuse to remove an unexpected fixture directory')
            shutil.rmtree(target)


class CompanionContextUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Qt offscreen required')

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.w, self.c, self.store = self.f.window, self.f.c, self.f.store

    def tearDown(self):
        self.f.close()

    def test_preview_counts_and_legacy_history_do_not_invent_sent_metadata(self):
        text = self.w.context_info.text()
        self.assertIn('下次请求', text)
        self.assertIn('2 条长期记忆', text)
        self.assertIn('1 轮历史', text)
        self.assertIn(LEGACY_REPLY, self.w.history.toPlainText())
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        self.assertNotIn('context', self.store.turns[0])
        self.assertNotIn('context', ChatStore(self.f.state).turns[0])
        self.assertEqual(self.f.chat.calls, [])

    def test_pending_and_success_footer_describe_exact_submitted_context(self):
        self.f.chat.block = True
        self.f.send()
        self.assertTrue(self.f.chat.entered.wait(2))
        self.assertIn('本次请求：2 条长期记忆 · 1 轮历史', self.w.context_info.text())
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        messages = self.f.chat.calls[0]['messages']
        self.assertIn(FACT_A, messages[0]['content'])
        self.assertIn(FACT_B, messages[0]['content'])
        self.assertEqual(messages[1]['content'], LEGACY_USER)
        self.f.chat.release.set()
        self.f.wait_for(lambda: self.w.job is None)
        expected = {'memoryCount': 2, 'historyRounds': 1, 'memoryEnabled': True, 'taskStateShared': False}
        self.assertEqual(self.store.turns[-1]['context'], expected)
        self.assertEqual(ChatStore(self.f.state).turns[-1]['context'], expected)
        self.assertEqual(self.w.history.toPlainText().count('随请求发送：'), 1)
        self.assertIn('随请求发送：2 条长期记忆 · 1 轮历史', self.w.history.toPlainText())
        self.assertIn('下次请求：2 条长期记忆 · 2 轮历史', self.w.context_info.text())

    def test_disabling_memory_changes_preview_and_request_without_deleting_facts(self):
        original = copy.deepcopy(self.store.facts)
        self.w.use_memory.setChecked(False)
        self.assertTrue(self.w.save_settings())
        self.assertIn('长期记忆未启用', self.w.context_info.text())
        self.f.send()
        self.f.wait_for(lambda: self.w.job is None)
        system = self.f.chat.calls[-1]['messages'][0]['content']
        self.assertNotIn(FACT_A, system)
        self.assertNotIn(FACT_B, system)
        self.assertEqual(self.store.facts, original)
        self.assertEqual(self.store.turns[-1]['context']['memoryCount'], 0)
        self.assertFalse(self.store.turns[-1]['context']['memoryEnabled'])
        self.assertIn('随请求发送：长期记忆未启用', self.w.history.toPlainText())

    def test_task_count_footer_records_the_request_not_later_settings(self):
        self.c.core.ingest('workbuddy', 'context-count-fixture', 'waiting', 'Fixture')
        self.w.share_tasks.setChecked(True)
        self.assertTrue(self.w.save_settings())
        self.assertIn('工作状态计数', self.w.context_info.text())
        self.f.send('按已启用的设置回复。')
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('本机观测到的工作状态计数', self.f.chat.calls[-1]['messages'][0]['content'])
        self.assertTrue(self.store.turns[-1]['context']['taskStateShared'])
        self.w.share_tasks.setChecked(False)
        self.assertTrue(self.w.save_settings())
        self.assertNotIn('工作状态计数', self.w.context_info.text())
        self.assertIn('工作状态计数', self.w.history.toPlainText())
        self.assertTrue(ChatStore(self.f.state).turns[-1]['context']['taskStateShared'])

    def test_local_remember_is_labelled_local_and_never_gets_sent_footer(self):
        self.f.send('记住：这是本机保存的虚构偏好。')
        self.assertIsNone(self.w.job)
        self.assertEqual(self.f.chat.calls, [])
        self.assertEqual(self.store.turns[-1]['source'], 'local')
        self.assertNotIn('context', self.store.turns[-1])
        self.assertIn('本机记忆管理', self.w.history.toPlainText())
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        self.assertIn('没有调用模型', self.w.feedback.text())
        self.assertIn('3 条长期记忆', self.w.context_info.text())

    def test_cancel_then_late_success_never_persists_context_or_reply(self):
        before = self.f.saved_memory()
        self.f.chat.block = True
        self.f.chat.reply = '这条迟到回复必须丢弃。'
        self.f.send('将被取消的测试问题')
        self.assertTrue(self.f.chat.entered.wait(2))
        self.assertIsNotNone(self.w.pending_context)
        self.w.cancel()
        self.assertIsNone(self.w.pending_context)
        self.f.chat.finish()
        self.f.pump()
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(len(self.store.turns), 1)
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        self.assertNotIn(self.f.chat.reply, self.w.history.toPlainText())

    def test_failed_reply_never_persists_context_or_fabricates_success_footer(self):
        before = self.f.saved_memory()
        self.f.chat.error = ChatError('隔离测试：本机引擎不可用。')
        self.f.send('失败测试消息')
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIsNone(self.w.pending_context)
        self.assertEqual(self.f.saved_memory(), before)
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        self.assertIn('未保存为成功对话', self.w.feedback.text())
        self.assertTrue(self.w.retry.isVisible())

    def test_role_switch_displays_only_its_history_memory_counts_and_preview(self):
        self.f.send('小猫专属问题')
        self.f.wait_for(lambda: self.w.job is None)
        self.f.switch_role('robot')
        self.assertIn('0 条长期记忆 · 0 轮历史', self.w.context_info.text())
        self.assertNotIn('小猫专属问题', self.w.history.toPlainText())
        self.assertNotIn('随请求发送', self.w.history.toPlainText())
        self.w.fact.setPlainText('机器人独有的虚构记忆。')
        self.w.add_memory()
        self.f.send('机器人专属问题')
        self.f.wait_for(lambda: self.w.job is None)
        self.assertIn('随请求发送：1 条长期记忆 · 0 轮历史', self.w.history.toPlainText())
        self.f.switch_role('tabby')
        self.assertIn('2 条长期记忆 · 2 轮历史', self.w.context_info.text())
        self.assertIn('小猫专属问题', self.w.history.toPlainText())
        self.assertNotIn('机器人专属问题', self.w.history.toPlainText())
        self.assertEqual(len(self.store.facts), 2)

    def test_proactive_defaults_off_and_controls_require_local_provider(self):
        self.assertFalse(DEFAULTS['proactiveEnabled'])
        self.assertFalse(self.w.proactive_enabled.isChecked())
        for provider in ('codex', 'codebuddy', 'ollama'):
            with self.subTest(provider=provider):
                self.w.provider.setCurrentIndex(self.w.provider.findData(provider))
                for control in (self.w.proactive_enabled, self.w.proactive_minutes,
                                self.w.proactive_memory, self.w.proactive_try):
                    self.assertEqual(control.isEnabled(), provider == 'ollama')
        self.assertEqual(self.f.preview.calls, [])

    def test_saving_proactive_options_preserves_all_other_settings_and_memory(self):
        self.f.use_local()
        before = copy.deepcopy(self.store.settings)
        preferences = copy.deepcopy(self.c.prefs.values)
        memory = self.f.saved_memory()
        self.w.proactive_enabled.setChecked(True)
        self.w.proactive_memory.setChecked(True)
        self.w.proactive_minutes.setValue(17)
        self.assertTrue(self.w.save_settings())
        changed = {'proactiveEnabled': True, 'proactiveMemory': True, 'proactiveMinutes': 17}
        self.assertEqual(self.store.settings, {**before, **changed})
        self.assertEqual(ChatStore(self.f.state).settings, {**before, **changed})
        self.assertEqual(self.c.prefs.values, preferences)
        self.assertEqual(self.f.saved_memory(), memory)
        self.assertEqual(self.f.preview.calls, [])

    def test_try_is_blocked_by_real_task_quiet_hidden_and_busy_without_backend(self):
        self.f.use_local()
        for condition in ('working', 'waiting', 'error', 'completed', 'quiet', 'hidden',
                          'hidden_bubble', 'chat_busy', 'runtime_busy', 'reduced_motion'):
            with self.subTest(condition=condition):
                self.c.core = Core()
                self.c.behavior.resume()
                self.c.completion_popup.hide()
                self.c.chat_state = None
                self.c.pet.show()
                self.c.prefs.save(reducedMotion=False)
                self.store.save_settings(bubbleMode='always')
                self.w.load_settings()
                if condition in ('working', 'waiting', 'error', 'completed'):
                    self.c.core.ingest('workbuddy', 'fixture-task', condition, 'Fixture')
                elif condition == 'quiet':
                    self.c.quiet_companion()
                elif condition == 'hidden':
                    self.c.pet.hide()
                elif condition == 'hidden_bubble':
                    self.store.save_settings(bubbleMode='hidden')
                    self.w.load_settings()
                elif condition == 'chat_busy':
                    self.w.job = ChatJob()
                elif condition == 'runtime_busy':
                    self.w.runtime_busy = True
                else:
                    self.c.prefs.save(reducedMotion=True)
                before = copy.deepcopy(self.store.settings)
                self.w.try_proactive()
                self.assertEqual(self.f.preview.calls, [])
                self.assertEqual(self.store.settings, before)
                self.w.job = None
                self.w.runtime_busy = False

    def test_manual_preview_only_updates_status_not_chat_memory_or_pet_bubble(self):
        self.f.use_local()
        self.w.proactive_memory.setChecked(True)
        self.assertTrue(self.w.save_settings())
        memory = self.f.saved_memory()
        turns = copy.deepcopy(self.store.turns)
        facts = copy.deepcopy(self.store.facts)
        self.w.try_proactive()
        self.f.wait_for(lambda: self.c.proactive.job is None)
        self.assertEqual(self.w.proactive_status.text(), '刚说过：' + PREVIEW_REPLY)
        self.assertEqual(len(self.f.preview.calls), 1)
        messages = self.f.preview.calls[0]['messages']
        self.assertEqual(len(messages), 2)
        self.assertIn(FACT_A, messages[0]['content'])
        self.assertNotIn(LEGACY_USER, str(messages))
        self.assertEqual(self.store.turns, turns)
        self.assertEqual(self.store.facts, facts)
        self.assertEqual(self.f.saved_memory(), memory)
        self.assertNotIn(PREVIEW_REPLY, self.w.history.toPlainText())
        self.assertEqual(self.c.pet.visual_state()['proactive'], '')
        self.f.switch_role('robot')
        self.assertNotIn(PREVIEW_REPLY, self.w.proactive_status.text())

    def test_real_task_arriving_during_preview_discards_late_result_and_keeps_priority(self):
        self.f.use_local()
        before = self.f.saved_memory()
        self.f.preview.block = True
        self.w.try_proactive()
        self.assertTrue(self.f.preview.entered.wait(2))
        self.c.core.ingest('workbuddy', 'fixture-waiting', 'waiting', 'Fixture')
        self.f.pump()
        self.assertIsNone(self.c.proactive.job)
        self.f.preview.finish()
        self.f.pump()
        self.assertNotIn(PREVIEW_REPLY, self.w.proactive_status.text())
        self.assertEqual(self.c.pet.visual_state()['state'], 'waiting')
        self.assertEqual(self.c.pet.visual_state()['proactive'], '')
        self.assertEqual(self.f.saved_memory(), before)


if __name__ == '__main__':
    unittest.main()
