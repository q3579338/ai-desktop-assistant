"""Offscreen local-engine controls with isolated state and no model processes."""
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
sys.path[:0] = [str(ROOT), str(ROOT/'.vendor')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from starpet.chat_gui import CompanionWindow
from starpet.companion import ChatError
from starpet.core import Core
from starpet.gui import Controller
from starpet.local_runtime import LocalRuntime, LocalRuntimeError, URL, MODEL
from starpet.storage import Preferences


def runtime_result(status='available', owned=True, running=True, message='本机服务可用。', **updates):
    return {'status': status, 'installed': True, 'owned': owned, 'running': running,
            'url': URL, 'model': MODEL, 'loaded': False, 'busy': False,
            'message': message, **updates}


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.closed = False

    def perform(self, action):
        self.calls.append(action)
        handler = self.handlers.get(action)
        if handler is not None:
            return handler()
        if action == 'stop':
            return runtime_result('stopped', owned=False, running=False, message='本次启动的引擎已停止。')
        return runtime_result()

    def start(self):
        return self.perform('start')

    def status(self):
        return self.perform('status')

    def stop(self):
        return self.perform('stop')

    def close(self):
        self.closed = True
        self.calls.append('close')


class LocalRuntimeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Local runtime UI tests require Qt offscreen')

    def setUp(self):
        self.base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT/'evidence/tests').resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.folder = self.base/('local-runtime-ui-'+uuid.uuid4().hex)
        self.folder.mkdir()
        self.state = self.folder/'isolated-state'
        self.runtime = FakeRuntime()
        with patch('starpet.local_runtime.LocalRuntime', return_value=self.runtime) as constructor:
            self.c = Controller(self.app, Core(), Preferences(self.state),
                SimpleNamespace(actions=queue.Queue()), SimpleNamespace(bytes_read=0, errors=0),
                self.state, self.folder/'profile', tray=False)
        constructor.assert_called_once_with(self.state)
        self.c.action_timer.stop()
        self.c.panel.timer.stop()
        self.c.pet.timer.stop()
        self.store = self.c.companion_store
        self.store.save_settings(provider='codex', model='saved-official-model', name='已保存的名字')
        self.store.add_fact('这条虚构偏好只存在于隔离测试目录。')
        self.store.commit_turn('旧问题', '旧回复')
        self.window = CompanionWindow(self.c)
        self.c.companion = self.window
        self.window.poller.stop()
        self.scheduled = []
        # Capture this window's one-shot status refresh: no callback survives a
        # test or launches work against a disposed QWidget.
        self.timer_patch = patch('starpet.chat_gui.QTimer.singleShot',
                                 side_effect=lambda ms, callback: self.scheduled.append((ms, callback)))
        self.timer_patch.start()
        self.gates = []
        self.baseline = self.snapshot()

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        if self.window.job:
            self.window.cancel()
        self.wait_for(lambda: not self.window.runtime_busy, required=False)
        self.c.stop_chat()
        self.timer_patch.stop()
        self.window.poller.stop()
        self.window.hide()
        self.c.pet.hide(); self.c.panel.hide(); self.c.completion_popup.hide()
        if self.c.pet.clip_movie:
            self.c.pet.clip_movie.stop()
        self.app.aboutToQuit.disconnect(self.c.stop_chat)
        for widget in (self.window, self.c.pet, self.c.panel, self.c.completion_popup):
            widget.deleteLater()
        self.app.processEvents()
        resolved = self.folder.resolve()
        if resolved.parent != self.base or not resolved.name.startswith('local-runtime-ui-'):
            raise RuntimeError('Unexpected UI fixture path')
        shutil.rmtree(resolved)

    def snapshot(self):
        return {str(path.relative_to(self.state)): path.read_bytes()
                for path in self.state.rglob('*') if path.is_file()}

    def wait_for(self, predicate, timeout=2, required=True):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            self.app.processEvents()
            self.window.poll()
            if predicate():
                return True
            time.sleep(.002)
        if required:
            self.fail('Offscreen asynchronous operation did not finish')
        return False

    def select_local_draft(self):
        w = self.window
        w.provider.setCurrentIndex(w.provider.findData('ollama'))
        w.local_url.setText('http://127.0.0.1:11434')
        w.model.setCurrentText('draft-model')

    def hold(self, action='start', result=None):
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        def work():
            entered.set()
            if not release.wait(3):
                raise LocalRuntimeError('隔离测试没有释放操作')
            return result if result is not None else runtime_result()
        self.runtime.handlers[action] = work
        return entered, release

    def finish_runtime(self):
        self.wait_for(lambda: not self.window.runtime_busy)

    def test_construction_does_not_start_or_probe_runtime_and_preserves_saved_provider(self):
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.store.settings['provider'], 'codex')
        self.assertFalse(self.window.local_stop.isEnabled())
        self.assertEqual(self.snapshot(), self.baseline)

    def test_start_fills_only_local_connection_draft_without_saving_provider_or_memory(self):
        self.select_local_draft()
        self.window.local_start.click()
        self.finish_runtime()
        self.assertEqual(self.runtime.calls, ['start'])
        self.assertEqual(self.window.local_url.text(), URL)
        self.assertEqual(self.window.model.currentText(), MODEL)
        self.assertEqual(self.store.settings['provider'], 'codex')
        self.assertEqual(self.store.settings['model'], 'saved-official-model')
        self.assertEqual(self.snapshot(), self.baseline)
        self.assertIn('保存设置后', self.window.feedback.text())
        self.assertTrue(self.window.local_stop.isEnabled())
        self.assertEqual([ms for ms, _ in self.scheduled], [700])

    def test_connection_edits_during_start_are_not_overwritten(self):
        for field in ('provider', 'url', 'model'):
            with self.subTest(field=field):
                self.select_local_draft()
                entered, release = self.hold()
                self.window.local_runtime_action('start')
                self.assertTrue(entered.wait(2))
                if field == 'provider':
                    self.window.provider.setCurrentIndex(self.window.provider.findData('codebuddy'))
                elif field == 'url':
                    self.window.local_url.setText('http://127.0.0.1:12345')
                else:
                    self.window.model.setCurrentText('user-changed-model')
                expected = (self.window.provider.currentData(), self.window.local_url.text(), self.window.model.currentText())
                release.set(); self.finish_runtime()
                self.assertEqual((self.window.provider.currentData(), self.window.local_url.text(), self.window.model.currentText()), expected)
                self.assertEqual(self.snapshot(), self.baseline)

    def test_unrelated_profile_and_chat_drafts_survive_async_start_result(self):
        self.select_local_draft()
        entered, release = self.hold()
        self.window.local_runtime_action('start')
        self.assertTrue(entered.wait(2))
        self.window.name.setText('没有保存的新名字')
        self.window.persona.setPlainText('没有保存的新角色设定')
        self.window.rounds.setValue(2)
        self.window.use_memory.setChecked(False)
        self.window.input.setPlainText('我还没有发送的消息')
        release.set(); self.finish_runtime()
        self.assertEqual(self.window.name.text(), '没有保存的新名字')
        self.assertEqual(self.window.persona.toPlainText(), '没有保存的新角色设定')
        self.assertEqual(self.window.rounds.value(), 2)
        self.assertFalse(self.window.use_memory.isChecked())
        self.assertEqual(self.window.input.toPlainText(), '我还没有发送的消息')
        self.assertEqual((self.window.local_url.text(), self.window.model.currentText()), (URL, MODEL))
        self.assertEqual(self.snapshot(), self.baseline)

    def test_chat_job_rejects_start_stop_and_status_even_when_called_directly(self):
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        def backend(*args, **kwargs):
            entered.set(); release.wait(3)
            return '隔离测试回复'
        self.window.backend = backend
        self.window.runtime_owned = True
        self.window.input.setPlainText('正在处理的问题')
        self.window.send()
        self.assertTrue(entered.wait(2))
        for action in ('start', 'stop', 'status'):
            self.window.local_runtime_action(action)
        self.assertEqual(self.runtime.calls, [])
        self.assertFalse(self.window.local_start.isEnabled())
        self.assertFalse(self.window.local_stop.isEnabled())
        self.assertFalse(self.window.local_status_button.isEnabled())
        self.assertIn('当前操作', self.window.feedback.text())
        self.window.cancel(); release.set()
        self.assertEqual(self.snapshot(), self.baseline)

    def test_runtime_operation_blocks_chat_probe_and_save_without_losing_input(self):
        self.select_local_draft()
        entered, release = self.hold()
        self.window.local_runtime_action('start')
        self.assertTrue(entered.wait(2))
        calls = []
        self.window.backend = lambda *args, **kwargs: calls.append(args)
        self.window.input.setPlainText('记住：不应在启动中保存这句')
        self.window.send(); self.window.probe()
        self.assertFalse(self.window.save_settings())
        self.assertEqual(calls, [])
        self.assertIsNone(self.window.job)
        self.assertEqual(self.window.input.toPlainText(), '记住：不应在启动中保存这句')
        self.assertEqual(self.snapshot(), self.baseline)
        release.set(); self.finish_runtime()

    def test_failed_start_does_not_fill_connection_or_claim_success(self):
        for error in (LocalRuntimeError('没有安装本机引擎'), RuntimeError('private details')):
            with self.subTest(error=type(error).__name__):
                self.select_local_draft()
                def fail():
                    raise error
                self.runtime.handlers['start'] = fail
                self.window.local_runtime_action('start'); self.finish_runtime()
                self.assertEqual(self.window.local_url.text(), 'http://127.0.0.1:11434')
                self.assertEqual(self.window.model.currentText(), 'draft-model')
                self.assertIn('没有安装' if isinstance(error, LocalRuntimeError) else '未完成', self.window.local_status.text())
                self.assertNotIn('private details', self.window.local_status.text())
                self.assertTrue(self.window.local_start.isEnabled())
                self.assertFalse(self.window.local_stop.isEnabled())
                self.assertEqual(self.scheduled, [])
                self.assertEqual(self.snapshot(), self.baseline)

    def test_owned_stop_updates_controls_without_changing_saved_state(self):
        self.select_local_draft()
        self.window.local_start.click(); self.finish_runtime()
        self.window.local_stop.click(); self.finish_runtime()
        self.assertEqual(self.runtime.calls, ['start', 'stop'])
        self.assertIn('已停止', self.window.local_status.text())
        self.assertFalse(self.window.local_stop.isEnabled())
        self.assertEqual(self.snapshot(), self.baseline)

    def test_real_runtime_unowned_service_status_cannot_enable_or_execute_process_stop(self):
        # Only the status result is simulated; this uses the real stop() return
        # type through the UI to catch controller/runtime contract mismatches.
        actual = LocalRuntime(self.state)
        self.c.local_runtime = actual
        health = {'ok': True, 'engine': 'transformers', 'model': MODEL, 'loaded': True, 'busy': False}
        with patch('starpet.local_runtime._configuration', return_value={}), \
             patch('starpet.local_runtime._health', return_value=health), \
             patch('starpet.local_runtime.subprocess.Popen') as process:
            self.window.local_runtime_action('status'); self.finish_runtime()
            self.assertFalse(self.window.local_stop.isEnabled())
            self.window.local_stop.click()
            self.window.local_runtime_action('stop'); self.finish_runtime()
            process.assert_not_called()
        self.assertFalse(self.window.runtime_owned)
        self.assertEqual(self.snapshot(), self.baseline)

    def test_start_followup_reports_service_failure_without_saving_settings(self):
        self.select_local_draft()
        self.runtime.handlers['start'] = lambda: runtime_result('loading', message='正在后台启动。')
        self.runtime.handlers['status'] = lambda: runtime_result('unavailable', owned=False, running=False,
                                                                message='本机服务未运行或健康检查未通过。')
        self.window.local_start.click(); self.finish_runtime()
        self.scheduled[0][1](); self.finish_runtime()
        self.assertIn('未运行', self.window.local_status.text())
        self.assertFalse(self.window.local_stop.isEnabled())
        self.assertEqual(self.runtime.calls, ['start', 'status'])
        self.assertEqual(self.snapshot(), self.baseline)

    def test_existing_chat_uses_saved_provider_until_user_saves_runtime_draft(self):
        self.select_local_draft()
        self.window.local_start.click(); self.finish_runtime()
        captured = []
        def backend(settings, messages, state, job, probe=False):
            captured.append((copy.deepcopy(settings), copy.deepcopy(messages), state, probe))
            return '聊天流程仍可正常收到回复。'
        self.window.backend = backend
        self.window.input.setPlainText('请回答这个虚构测试问题')
        self.window.send(); self.wait_for(lambda: self.window.job is None)
        self.assertEqual(captured[0][0]['provider'], 'codex')
        self.assertEqual(captured[0][0]['model'], 'saved-official-model')
        self.assertIn('隔离测试目录', captured[0][1][0]['content'])
        self.assertEqual(self.store.turns[-1]['assistant'], '聊天流程仍可正常收到回复。')
        self.assertEqual(self.store.settings_path.read_bytes(), self.baseline['companion-settings.json'])
        self.assertTrue(self.window.send_button.isEnabled())

    def test_explicit_save_selects_local_provider_and_updates_privacy_without_rewriting_memory(self):
        self.select_local_draft()
        self.window.local_start.click(); self.finish_runtime()
        self.assertIn('联网', self.window.chat_privacy.text())
        memory_before = self.store.memory_path.read_bytes()
        self.assertTrue(self.window.save_settings())
        self.assertEqual(self.store.settings['provider'], 'ollama')
        self.assertEqual(self.store.settings['localUrl'], URL)
        self.assertEqual(self.store.settings['model'], MODEL)
        self.assertEqual(self.store.memory_path.read_bytes(), memory_before)
        self.assertIn('本机模式', self.window.chat_privacy.text())
        self.assertNotIn('联网聊天', self.window.chat_privacy.text())
        captured = []
        def backend(settings, *args, **kwargs):
            captured.append(settings['provider'])
            return '已按本机方式发送的替身回复'
        self.window.backend = backend
        self.window.input.setPlainText('保存后的虚构测试')
        self.window.send(); self.wait_for(lambda: self.window.job is None)
        self.assertEqual(captured, ['ollama'])

    def test_cancelled_chat_late_result_is_not_committed_and_next_chat_still_works(self):
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        def late(*args, **kwargs):
            entered.set(); release.wait(3)
            return '不应该保存的迟到回复'
        self.window.backend = late
        self.window.input.setPlainText('将取消的虚构测试')
        self.window.send(); self.assertTrue(entered.wait(2))
        self.window.cancel(); release.set()
        # Wait for the cancelled worker to actually enqueue its late result;
        # an empty queue immediately after release would not prove rejection.
        late_result = self.window.results.get(timeout=2)
        self.window.results.put(late_result)
        self.window.poll()
        self.assertEqual(self.snapshot(), self.baseline)
        self.window.backend = lambda *args, **kwargs: '新回复'
        self.window.input.setPlainText('新的虚构测试')
        self.window.send(); self.wait_for(lambda: self.window.job is None)
        self.assertEqual(self.store.turns[-1]['assistant'], '新回复')
        self.assertNotIn('不应该保存', str(self.store.turns))

    def test_controller_exit_closes_runtime_while_start_result_is_pending(self):
        self.select_local_draft()
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        def late_start():
            entered.set(); release.wait(3)
            if self.runtime.closed:
                raise LocalRuntimeError('助手已经退出，不再启动服务。')
            return runtime_result()
        self.runtime.handlers['start'] = late_start
        self.window.local_start.click(); self.assertTrue(entered.wait(2))
        self.c.stop_chat()
        self.assertTrue(self.runtime.closed)
        release.set(); self.finish_runtime()
        self.assertIn('已经退出', self.window.local_status.text())
        self.assertEqual(self.snapshot(), self.baseline)


if __name__ == '__main__':
    unittest.main()
