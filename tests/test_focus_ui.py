"""Focus 1.6 UI checks; synthetic state, injected timer, Qt offscreen only.

Run this file with --scale 1 or --scale 2 to retain screenshots and a result
manifest under evidence/focus-160-*. No real model or system input is used.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor'), str(ROOT / 'tests')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
if __name__ == '__main__':
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument('--scale', choices=('1', '2'), default='1')
    _args = _parser.parse_args()
    os.environ['QT_SCALE_FACTOR'] = _args.scale

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel
from test_companion_context_ui import ContextUIFixture
from starpet import VERSION
from starpet.behavior import Behavior
from starpet.companion import ChatJob
from starpet.core import Core
from starpet.focus import FocusTimer


class FakeTime:
    def __init__(self):
        self.wall = 1700000000.0
        self.mono = 100.0

    def clock(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FocusUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Qt offscreen is mandatory; no foreground fallback')
        cls.evidence = ROOT / 'evidence' / ('focus-160-' + os.environ.get('QT_SCALE_FACTOR', '1') + 'x-' + uuid.uuid4().hex[:10])
        cls.evidence.mkdir(parents=True)
        cls.captures = []

    def setUp(self):
        self.time = FakeTime()
        self.make_fixture()

    def make_fixture(self, folder=None):
        factory = lambda state: FocusTimer(state, self.time.clock, self.time.monotonic)
        with patch('starpet.focus.FocusTimer', new=factory):
            self.f = ContextUIFixture(self.app, folder)
        self.c, self.w, self.store = self.f.c, self.f.window, self.f.store
        self.c.core.clock = self.time.clock
        self.page = self.w.focus_page
        self.w.tabs.setCurrentIndex(4)
        self.page.work.setValue(5)
        self.page.rest.setValue(1)
        self.app.processEvents()

    def close_fixture(self, remove=True):
        popup = self.c.focus_popup
        popup.expiry.stop()
        popup.hide()
        self.c.completion_popup.expiry.stop()
        self.f.close(remove=remove)

    def tearDown(self):
        self.close_fixture()

    def pump(self, seconds=0):
        self.time.advance(seconds)
        self.c.focus_next_tick = 0
        self.c.controls()
        self.app.processEvents()

    def start(self, label='隔离测试专注'):
        self.page.task.setText(label)
        self.page.start.click()
        self.app.processEvents()
        self.assertEqual(self.c.focus.snapshot()['status'], 'running')

    def core_state(self):
        value = copy.deepcopy(self.c.core.snapshot())
        value.pop('at')
        return value

    def test_page_lifecycle_empty_name_and_explicit_next_phase(self):
        before = self.f.saved_memory()
        self.start('   ')
        self.assertEqual(self.c.focus.snapshot()['label'], '专注时间')
        self.assertEqual(self.page.stage.text(), '专注中')
        self.assertFalse(self.page.task.isEnabled())
        self.assertTrue(self.page.pause.isVisible())
        self.pump(50)
        self.assertEqual(self.page.clock.text(), '04:10')
        self.page.pause.click()
        self.assertEqual(self.page.stage.text(), '专注已暂停')
        self.pump(1000)
        self.assertEqual(self.page.clock.text(), '04:10')
        self.page.resume.click()
        self.c.quiet_companion()
        self.pump(251)
        self.assertEqual(self.page.stage.text(), '专注到时间了')
        self.assertEqual(self.page.next.text(), '开始休息')
        self.page.next.click()
        self.assertEqual(self.c.focus.snapshot()['phase'], 'break')
        self.assertEqual(self.page.clock.text(), '01:00')
        self.pump(61)
        self.assertEqual(self.page.next.text(), '开始下一段专注')
        self.page.next.click()
        self.assertEqual(self.c.focus.snapshot()['phase'], 'work')
        self.assertEqual(self.page.clock.text(), '05:00')
        self.page.cancel.click()
        self.assertEqual(self.c.focus.snapshot()['status'], 'idle')
        self.assertIn('不计作任务完成', self.page.feedback.text())
        self.assertTrue(self.page.task.isEnabled())
        self.assertEqual(self.f.saved_memory(), before)
        self.assertEqual(self.f.chat.calls, [])
        self.assertEqual(self.f.preview.calls, [])

    def test_idle_duration_preview_updates_without_erasing_label_draft(self):
        self.page.task.setText('尚未开始的测试草稿')
        for minutes in (12, 180, 5):
            with self.subTest(minutes=minutes):
                self.page.work.setValue(minutes)
                self.app.processEvents()
                self.assertEqual(self.page.clock.text(), f'{minutes:02d}:00')
                self.assertEqual(self.page.task.text(), '尚未开始的测试草稿')
                self.assertEqual(self.c.focus.snapshot()['status'], 'idle')
                self.assertFalse(self.c.focus.path.exists())

    def test_real_saved_running_and_paused_timers_restore_in_new_controller(self):
        self.start('恢复这个真实测试计时')
        self.pump(42)
        folder = self.f.folder
        self.close_fixture(remove=False)
        self.time.advance(18)
        self.time.mono = 1
        self.make_fixture(folder)
        self.assertEqual(self.page.task.text(), '恢复这个真实测试计时')
        self.assertEqual(self.page.stage.text(), '专注中')
        self.assertEqual(self.page.clock.text(), '04:00')
        self.page.pause.click()
        self.close_fixture(remove=False)
        self.time.advance(10000)
        self.make_fixture(folder)
        self.assertEqual(self.page.stage.text(), '专注已暂停')
        self.assertEqual(self.page.clock.text(), '04:00')
        self.assertTrue(self.page.resume.isVisible())

    def test_countdown_and_expiry_never_create_or_change_core_task_state(self):
        self.c.core.ingest('workbuddy', 'only-observed-event', 'working', 'Fixture')
        before = self.core_state()
        self.start()
        self.pump(301)
        self.assertEqual(self.core_state(), before)
        view = self.c.pet.visual_state()
        self.assertEqual(view['state'], 'working')
        self.assertEqual(view['reaction']['pose'], 'working')
        self.assertEqual(view['reaction']['text'], '')
        self.assertIsNone(view['focus'])
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertTrue(self.c.focus.snapshot()['noticePending'])
        self.assertEqual(self.c.core.snapshot()['notices'], [])

    def test_work_confirmation_error_and_real_completion_have_popup_priority(self):
        self.start()
        self.time.advance(301)
        for state in ('working', 'thinking', 'waiting', 'error', 'unknown', 'interrupted'):
            with self.subTest(state=state):
                self.c.core = Core(clock=self.time.clock)
                self.c.core.ingest('codex', 'real-state-fixture', state, 'Fixture')
                self.pump()
                self.assertFalse(self.c.focus_popup.isVisible())
                self.assertFalse(self.c.focus.snapshot()['noticeShown'])
                self.assertIsNone(self.c.pet.visual_state()['focus'])
        self.c.core = Core(clock=self.time.clock)
        self.c.core.ingest('codex', 'real-completed-fixture', 'completed', 'Stop')
        self.pump()
        self.assertTrue(self.c.completion_popup.isVisible())
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertFalse(self.c.focus.snapshot()['noticeShown'])
        self.c.completion_popup.done.click()
        self.c.core.clear_finished()
        self.pump()
        self.assertTrue(self.c.focus_popup.isVisible())
        self.assertIn('专注', self.c.focus_popup.title.text())
        self.assertIn('不代表工作任务已完成', self.c.focus_popup.detail.text())

    def test_quiet_hidden_chat_busy_and_notifications_defer_the_same_notice(self):
        self.start()
        self.time.advance(301)
        identity = None
        for condition in ('quiet', 'hidden', 'chat_busy', 'notify_off'):
            with self.subTest(condition=condition):
                self.c.behavior.resume()
                self.c.pet.show()
                self.w.job = None
                self.page.notify.setChecked(True)
                if condition == 'quiet':
                    self.c.quiet_companion()
                elif condition == 'hidden':
                    self.c.pet.hide()
                elif condition == 'chat_busy':
                    self.w.job = ChatJob()
                else:
                    self.page.notify.setChecked(False)
                self.pump()
                state = self.c.focus.snapshot()
                identity = identity or state['noticeId']
                self.assertEqual(state['noticeId'], identity)
                self.assertTrue(state['noticePending'])
                self.assertFalse(state['noticeShown'])
                self.assertFalse(self.c.focus_popup.isVisible())
        self.w.job = None
        self.c.pet.show()
        self.c.resume_companion()
        self.page.notify.setChecked(True)
        self.pump()
        self.assertEqual(self.c.focus_popup.current['id'], identity)
        self.assertTrue(self.c.focus_popup.isVisible())

    def test_notice_is_silent_once_and_acknowledgement_survives_restart(self):
        self.start()
        self.pump(301)
        popup = self.c.focus_popup
        identity = popup.current['id']
        self.assertTrue(popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(popup.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        popup.expiry.stop()
        popup.hide()
        for _ in range(3):
            self.pump()
        self.assertFalse(popup.isVisible())
        self.assertTrue(self.c.focus.snapshot()['noticeShown'])
        folder = self.f.folder
        self.close_fixture(remove=False)
        self.make_fixture(folder)
        self.pump()
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertEqual(self.c.focus.snapshot()['noticeId'], identity)
        self.assertTrue(self.c.focus.acknowledge(identity))
        self.close_fixture(remove=False)
        self.make_fixture(folder)
        self.pump()
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertTrue(self.c.focus.snapshot()['noticeAcknowledged'])
        self.assertEqual(self.c.focus.snapshot()['status'], 'awaiting')

    def test_popup_confirm_button_does_not_start_the_next_phase(self):
        self.start('隔离测试任务名称保持完整可读' * 5)
        self.pump(301)
        self.capture('focus-popup-long-label', self.c.focus_popup)
        for item in (self.c.focus_popup.task, self.c.focus_popup.detail):
            self.assertGreaterEqual(item.height(), item.heightForWidth(item.contentsRect().width()))
        self.c.focus_popup.done.click()
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertTrue(self.c.focus.snapshot()['noticeAcknowledged'])
        self.assertEqual(self.c.focus.snapshot()['status'], 'awaiting')
        self.pump()
        self.assertFalse(self.c.focus_popup.isVisible())

    def test_focus_keeps_touch_interactions_but_suppresses_idle_and_automatic_model(self):
        self.f.use_local()
        self.w.proactive_enabled.setChecked(True)
        self.assertTrue(self.w.save_settings())
        self.w.hide()
        self.c.behavior = Behavior(clock=self.time.monotonic, rng=random.Random(0))
        self.c.proactive.clock = self.time.monotonic
        self.start()
        self.c.proactive._suppressed = False
        self.c.proactive.next_due = 0
        self.c.behavior._next_idle = 0
        self.pump(40)
        self.assertEqual(self.f.preview.calls, [])
        self.assertIsNone(self.c.proactive.job)
        self.assertEqual(self.c.pet.visual_state()['reaction']['kind'], 'idle')
        self.c.pet.pending_touch = 'head_pat'
        self.c.pet.finish_click()
        view = self.c.pet.visual_state()
        self.assertEqual(view['reaction']['kind'], 'head_pat')
        self.assertTrue(view['reaction']['text'])
        self.assertEqual(view['focus']['status'], 'running')
        self.assertEqual(self.c.core.snapshot()['notices'], [])
        self.c.focus.cancel()
        self.c.controls()
        self.assertEqual(self.f.preview.calls, [])  # No catch-up request after focus.

    def test_preview_button_during_work_does_not_start_then_cancel_model_request(self):
        self.f.use_local()
        self.start()
        with patch.object(self.c.proactive,'request') as request:
            self.w.try_proactive()
            request.assert_not_called()
            self.assertIn('暂停或结束专注',self.w.feedback.text())
            self.assertEqual(self.f.preview.calls,[])
            self.page.pause.click()
            self.w.try_proactive()
            request.assert_called_once()

    def test_save_failures_are_visible_and_never_claim_start_pause_or_popup_success(self):
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture storage unavailable')):
            self.page.start.click()
        self.assertEqual(self.c.focus.snapshot()['status'], 'idle')
        self.assertEqual(self.page.stage.text(), '准备开始')
        self.assertIn('未能保存', self.page.feedback.text())
        self.assertNotIn('已开始', self.page.feedback.text())
        self.start()
        before = self.c.focus.path.read_bytes()
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture storage unavailable')):
            self.page.pause.click()
        self.assertEqual(self.c.focus.snapshot()['status'], 'running')
        self.assertEqual(self.c.focus.path.read_bytes(), before)
        self.assertNotIn('已暂停', self.page.feedback.text())
        self.c.quiet_companion()
        self.pump(301)
        self.c.resume_companion()
        with patch('starpet.gui.time.monotonic', self.time.monotonic):
            with patch('starpet.focus.atomic_json', side_effect=OSError('fixture storage unavailable')):
                self.pump()
            self.assertFalse(self.c.focus_popup.isVisible())
            self.assertFalse(self.c.focus.snapshot()['noticeShown'])
            self.assertIn('未能保存', self.c.focus_error)
            self.pump(4)
            self.assertFalse(self.c.focus_popup.isVisible())
            self.pump(1)
        self.assertTrue(self.c.focus_popup.isVisible())
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture storage unavailable')):
            self.c.focus_popup.done.click()
        self.assertTrue(self.c.focus_popup.isVisible())
        self.assertFalse(self.c.focus.snapshot()['noticeAcknowledged'])
        self.c.focus_popup.done.click()
        self.assertTrue(self.c.focus.snapshot()['noticeAcknowledged'])

    def test_notification_setting_is_separate_from_chat_settings_and_memory(self):
        settings = copy.deepcopy(self.store.settings)
        preferences = copy.deepcopy(self.c.prefs.values)
        memory = self.f.saved_memory()
        self.page.notify.setChecked(False)
        self.assertEqual(self.store.settings, settings)
        self.assertEqual(self.c.prefs.values, preferences)
        self.assertEqual(self.f.saved_memory(), memory)
        saved = json.loads(self.c.focus.path.read_text(encoding='utf-8'))
        self.assertFalse(saved['notifyEnabled'])
        self.assertEqual(saved['status'], 'idle')

    def test_expiry_save_failure_stays_pending_without_a_false_completion_popup(self):
        self.start()
        self.time.advance(301)
        before = self.c.focus.path.read_bytes()
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture disk unavailable')):
            self.pump()
        self.assertEqual(self.c.focus.snapshot()['status'], 'running')
        self.assertEqual(self.page.stage.text(), '专注中')
        self.assertIn('未能保存', self.page.feedback.text())
        self.assertEqual(self.c.focus.path.read_bytes(), before)
        self.assertFalse(self.c.focus_popup.isVisible())
        self.assertEqual(self.c.core.snapshot()['notices'], [])
        self.pump(5)
        self.assertEqual(self.c.focus.snapshot()['status'], 'awaiting')
        self.assertTrue(self.c.focus_popup.isVisible())

    def capture(self, name, widget=None):
        self.app.processEvents()
        widget = widget or self.w
        shot = widget.grab()
        self.assertAlmostEqual(shot.devicePixelRatio(), float(os.environ.get('QT_SCALE_FACTOR', '1')))
        path = self.evidence / (name + '.png')
        self.assertFalse(path.exists())
        self.assertTrue(shot.save(str(path)))
        self.captures.append({'file': path.name, 'logicalSize': [widget.width(), widget.height()],
                              'pixelSize': [shot.width(), shot.height()], 'dpr': shot.devicePixelRatio()})

    def assert_small_layout(self, width, height):
        self.assertEqual((self.w.width(), self.w.height()), (width, height))
        self.assertEqual(self.page.horizontalScrollBar().maximum(), 0)
        bar = self.w.tabs.tabBar()
        for index in range(bar.count()):
            rect = bar.tabRect(index)
            self.assertGreaterEqual(rect.left(), 0)
            self.assertLessEqual(rect.right(), bar.width())
            self.assertGreaterEqual(rect.width(), bar.fontMetrics().horizontalAdvance(bar.tabText(index)) + 16)
        for widget in (self.page.task, self.page.work, self.page.rest, self.page.notify,
                       self.page.start, self.page.pause, self.page.resume, self.page.next, self.page.cancel):
            if widget.isHidden():
                continue
            left = widget.mapTo(self.page.widget(), QPoint(0, 0)).x()
            self.assertGreaterEqual(left, 0)
            self.assertLessEqual(left + widget.width(), self.page.widget().width())
            self.assertGreaterEqual(widget.width(), widget.minimumSizeHint().width())
        for widget in self.page.widget().findChildren(QLabel):
            if widget.isHidden():
                continue
            needed = widget.heightForWidth(widget.contentsRect().width()) if widget.hasHeightForWidth() else widget.sizeHint().height()
            self.assertGreaterEqual(widget.height(), needed, widget.text())

    def test_small_and_regular_window_tabs_controls_and_labels_fit(self):
        expected_tabs = ['聊天', '记忆', '详细设置', '角色', '专注']
        self.assertEqual([self.w.tabs.tabText(i) for i in range(5)], expected_tabs)
        for width, height in ((360, 540), (540, 760)):
            self.w.resize(width, height)
            self.page.verticalScrollBar().setValue(0)
            self.app.processEvents()
            self.assert_small_layout(width, height)
            self.capture(f'idle-top-{width}x{height}')
            self.page.ensureWidgetVisible(self.page.start, 0, 12)
            self.capture(f'idle-controls-{width}x{height}')
            self.start('整理本次隔离测试资料，保持说明完整可读')
            self.page.verticalScrollBar().setValue(0)
            self.app.processEvents()
            self.assert_small_layout(width, height)
            self.capture(f'running-{width}x{height}')
            self.page.pause.click()
            self.page.ensureWidgetVisible(self.page.resume, 0, 12)
            self.app.processEvents()
            self.assert_small_layout(width, height)
            self.capture(f'paused-{width}x{height}')
            self.page.resume.click()
            self.c.quiet_companion()
            self.pump(301)
            self.page.ensureWidgetVisible(self.page.next, 0, 12)
            self.assert_small_layout(width, height)
            self.capture(f'awaiting-{width}x{height}')
            self.page.cancel.click()
        self.assertEqual(self.f.chat.calls, [])
        self.assertEqual(self.f.preview.calls, [])


if __name__ == '__main__':
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FocusUITests))
    report = {'passed': outcome.wasSuccessful(), 'testsRun': outcome.testsRun,
              'failures': len(outcome.failures), 'errors': len(outcome.errors),
              'platform': 'offscreen', 'scale': int(_args.scale), 'realModelCalled': False,
              'systemInputSent': False, 'productionStateModified': False,
              'fixtureClock': True, 'captures': FocusUITests.captures,
              'targetRelease': '1.6.0', 'actualVersion': VERSION,
              'sourceSha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                               for name in ('starpet/focus.py', 'starpet/focus_gui.py', 'starpet/gui.py',
                                            'starpet/chat_gui.py', 'tests/test_focus_ui.py')},
              'failureDetails': [detail for _, detail in outcome.failures + outcome.errors]}
    target = FocusUITests.evidence / 'result.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence': str(FocusUITests.evidence), **report}, ensure_ascii=False))
    sys.exit(0 if outcome.wasSuccessful() else 1)
