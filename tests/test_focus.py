import copy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from starpet.focus import CHECKPOINT_SECONDS, FocusError, FocusTimer, MAX_FILE_BYTES
from starpet.storage import atomic_json


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


class FocusTimerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[1] / 'evidence' / 'tests'
        self.folder = self.base / ('focus-' + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.time = FakeTime()
        self.timer = self.new_timer()

    def tearDown(self):
        target = self.folder.resolve()
        self.assertEqual(target.parent, self.base.resolve())
        self.assertTrue(target.name.startswith('focus-'))
        shutil.rmtree(target)

    def new_timer(self, directory=None):
        return FocusTimer(directory or self.folder, self.time.clock, self.time.monotonic)

    def expire(self, timer=None):
        timer = timer or self.timer
        self.time.advance(timer.snapshot()['remainingSeconds'] + 1)
        result = timer.tick()
        self.assertEqual(result['status'], 'awaiting')
        return timer.pending_notice()

    def test_new_timer_is_idle_read_only_and_does_not_touch_other_data(self):
        sibling = self.folder / 'companion-memory.json'
        sibling.write_bytes(b'unchanged unrelated fixture')
        for _ in range(50):
            self.assertEqual(self.timer.snapshot()['status'], 'idle')
            self.assertEqual(self.timer.tick()['remainingSeconds'], 0)
            self.assertIsNone(self.timer.pending_notice())
        self.assertFalse(self.timer.path.exists())
        self.assertTrue(self.timer.snapshot()['notifyEnabled'])
        self.assertEqual(sibling.read_bytes(), b'unchanged unrelated fixture')

    def test_start_default_and_duration_label_boundaries(self):
        result = self.timer.start()
        self.assertEqual((result['status'], result['phase'], result['remainingSeconds']), ('running', 'work', 1500))
        self.assertEqual((result['workMinutes'], result['breakMinutes']), (25, 5))
        self.assertIsNone(self.timer.pending_notice())
        self.timer.cancel()
        result = self.timer.start('  检查设计稿  ', 180, 60)
        self.assertEqual(result['label'], '检查设计稿')
        self.assertEqual(result['remainingSeconds'], 10800)
        self.timer.cancel()
        result = self.timer.start('x' * 80, 5, 1)
        self.assertEqual(result['remainingSeconds'], 300)
        self.assertLess(self.timer.path.stat().st_size, MAX_FILE_BYTES)

    def test_invalid_inputs_never_create_or_change_a_record(self):
        for label, work, rest in (('', 25, 5), ('x' * 81, 25, 5), ('line\nbreak', 25, 5),
                                   ('\ud800', 25, 5), ('name\x7f', 25, 5),
                                   ('任务', 4, 5), ('任务', 181, 5), ('任务', 25, 0),
                                   ('任务', 25, 61), ('任务', True, 5), ('任务', 5.5, 1)):
            with self.subTest(label=label, work=work, rest=rest), self.assertRaises(FocusError):
                self.timer.start(label, work, rest)
            self.assertFalse(self.timer.path.exists())
        with self.assertRaises(FocusError):
            self.timer.set_notify_enabled(1)
        self.assertFalse(self.timer.path.exists())

    def test_pause_resume_cancel_and_restart_preserve_exact_phase(self):
        self.timer.start('写作', 5, 1)
        self.time.advance(70.4)
        self.assertEqual(self.timer.pause()['remainingSeconds'], 230)
        self.time.advance(10000)
        self.time.mono = 5
        restored = self.new_timer()
        self.assertEqual(restored.snapshot()['status'], 'paused')
        self.assertEqual(restored.snapshot()['remainingSeconds'], 230)
        restored.resume()
        self.time.advance(29.6)
        self.assertEqual(restored.snapshot()['remainingSeconds'], 200)
        restored.cancel()
        self.assertEqual(self.new_timer().snapshot()['status'], 'idle')
        before = restored.path.read_bytes()
        restored.cancel()
        self.assertEqual(restored.path.read_bytes(), before)

    def test_running_restart_uses_wall_elapsed_with_new_monotonic_origin(self):
        self.timer.start('读书', 5, 1)
        self.time.advance(42)
        self.time.mono = 1
        restored = self.new_timer()
        self.assertEqual(restored.snapshot()['remainingSeconds'], 258)
        self.time.advance(8)
        self.assertEqual(restored.snapshot()['remainingSeconds'], 250)
        restored.tick()
        self.assertEqual(restored.snapshot()['phase'], 'work')

    def test_overdue_restart_yields_one_notice_without_offline_cycles(self):
        self.timer.start('专注阅读', 5, 1)
        self.time.advance(3 * 86400)
        restored = self.new_timer()
        self.assertEqual(restored.snapshot()['remainingSeconds'], 0)
        self.assertIsNone(restored.pending_notice())
        result = restored.tick()
        notice = restored.pending_notice()
        self.assertEqual(result['status'], 'awaiting')
        self.assertEqual(notice['kind'], 'focus_timer')
        self.assertEqual(notice['title'], '专注时间到了')
        self.assertIn('不代表工作任务已完成', notice['message'])
        before = restored.path.read_bytes()
        self.time.advance(7 * 86400)
        for _ in range(20):
            self.assertEqual(restored.tick()['phase'], 'work')
            self.assertEqual(restored.pending_notice()['id'], notice['id'])
        self.assertEqual(restored.path.read_bytes(), before)
        self.assertEqual(self.new_timer().pending_notice()['id'], notice['id'])

    def test_each_explicit_next_phase_gets_full_duration_and_a_new_notice_id(self):
        self.timer.start('测试专注', 5, 1)
        first = self.expire()
        self.time.advance(6000)
        result = self.timer.begin_next()
        self.assertEqual((result['phase'], result['remainingSeconds']), ('break', 60))
        self.assertIsNone(self.timer.pending_notice())
        second = self.expire()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(second['title'], '休息时间到了')
        result = self.timer.begin_next()
        self.assertEqual((result['phase'], result['remainingSeconds']), ('work', 300))
        self.assertEqual(result['label'], '测试专注')

    def test_notice_display_and_acknowledgement_are_persistent_and_idempotent(self):
        self.timer.start('专注', 5, 1)
        notice = self.expire()
        self.assertFalse(self.timer.mark_notice_shown('wrong-id'))
        self.assertTrue(self.timer.mark_notice_shown(notice['id']))
        self.assertIsNone(self.timer.pending_notice())
        restored = self.new_timer()
        self.assertTrue(restored.snapshot()['noticeShown'])
        self.assertFalse(restored.snapshot()['noticePending'])
        before = restored.path.read_bytes()
        self.assertFalse(restored.mark_notice_shown(notice['id']))
        self.assertEqual(restored.path.read_bytes(), before)
        self.assertTrue(restored.acknowledge(notice['id']))
        before = restored.path.read_bytes()
        self.assertFalse(restored.acknowledge(notice['id']))
        self.assertEqual(restored.path.read_bytes(), before)
        self.assertEqual(self.new_timer().snapshot()['status'], 'awaiting')
        self.assertTrue(self.new_timer().snapshot()['noticeAcknowledged'])

    def test_notification_switch_defers_same_id_in_its_own_file(self):
        self.timer.set_notify_enabled(False)
        self.timer.start('专注', 5, 1)
        self.assertFalse(self.timer.snapshot()['notifyEnabled'])
        self.expire()
        result = self.timer.snapshot()
        self.assertTrue(result['noticePending'])
        self.assertIsNone(self.timer.pending_notice())
        self.assertFalse(self.timer.mark_notice_shown(result['noticeId']))
        restored = self.new_timer()
        restored.set_notify_enabled(True)
        self.assertEqual(restored.pending_notice()['id'], result['noticeId'])
        self.assertEqual({path.name for path in self.folder.iterdir()}, {'focus-timer.json'})

    def test_acknowledge_before_display_suppresses_popup_but_does_not_start_next(self):
        self.timer.start('专注', 5, 1)
        notice = self.expire()
        self.assertTrue(self.timer.acknowledge(notice['id']))
        self.assertIsNone(self.timer.pending_notice())
        self.assertFalse(self.timer.mark_notice_shown(notice['id']))
        self.assertEqual(self.new_timer().snapshot()['status'], 'awaiting')

    def test_checkpoints_are_bounded_and_snapshot_never_writes(self):
        with patch('starpet.focus.atomic_json', wraps=atomic_json) as writer:
            self.timer.start('专注', 5, 1)
            for second in range(1, 121):
                self.time.advance(1)
                for _ in range(5):
                    self.timer.snapshot()
                    self.timer.tick()
            self.assertEqual(writer.call_count, 1 + 120 // CHECKPOINT_SECONDS)
        data = json.loads(self.timer.path.read_text(encoding='utf-8'))
        self.assertEqual(data['remainingSeconds'], 180)

    def test_wall_and_monotonic_rollback_do_not_increase_remaining(self):
        self.timer.start('专注', 5, 1)
        self.time.advance(40)
        self.timer.tick()
        self.assertEqual(self.timer.snapshot()['remainingSeconds'], 260)
        self.time.wall -= 3600
        self.time.mono += 10
        self.assertEqual(self.timer.snapshot()['remainingSeconds'], 250)
        self.time.mono -= 50
        self.assertEqual(self.timer.snapshot()['remainingSeconds'], 250)
        self.time.mono = 180
        self.assertEqual(self.timer.tick()['remainingSeconds'], 220)
        # A backward wall time on restart cannot restore more than the last
        # durable remaining-time checkpoint (30-second recovery granularity).
        self.time.wall -= 86400
        self.time.mono = 1
        restored = self.new_timer()
        self.assertEqual(restored.snapshot()['remainingSeconds'], 220)

    def test_wall_clock_jump_forward_does_not_finish_a_live_monotonic_timer(self):
        self.timer.start('专注', 5, 1)
        self.time.wall += 86400
        self.assertEqual(self.timer.tick()['remainingSeconds'], 300)
        self.assertEqual(self.timer.snapshot()['status'], 'running')
        self.assertIsNone(self.timer.pending_notice())

    def test_pause_after_expiry_records_awaiting_instead_of_zero_time_pause(self):
        self.timer.start('专注', 5, 1)
        self.time.advance(301)
        self.assertEqual(self.timer.pause()['status'], 'awaiting')
        self.assertIsNotNone(self.timer.pending_notice())

    def test_bad_legacy_missing_fields_and_oversized_files_are_read_only_fallbacks(self):
        for content in ('not json', '{"schema":0,"remaining":10}', '{"schema":1,"status":"running"}',
                        '{"schema":1,"schema":1}', 'x' * (MAX_FILE_BYTES + 1)):
            with self.subTest(content=content[:45]):
                self.timer.path.write_text(content, encoding='utf-8')
                before = self.timer.path.read_bytes()
                restored = self.new_timer()
                self.assertEqual(restored.snapshot()['status'], 'idle')
                self.assertTrue(restored.snapshot()['warning'])
                restored.tick()
                self.assertEqual(restored.path.read_bytes(), before)

    def test_inconsistent_deadline_and_malformed_notice_are_not_resumed(self):
        self.timer.start('专注', 5, 1)
        valid = json.loads(self.timer.path.read_text(encoding='utf-8'))
        candidates = [{**valid, 'deadline': valid['deadline'] + 100},
                      {**valid, 'schema': True}, {**valid, 'remainingSeconds': float('nan')},
                      {**valid, 'savedAt': 10 ** 400}, {**valid, 'label': '\ud800'},
                      {**valid, 'notice': {'id': valid['timerId'], 'shown': False, 'acknowledged': False}}]
        for value in candidates:
            with self.subTest(value=value):
                self.timer.path.write_text(json.dumps(value), encoding='utf-8')
                restored = self.new_timer()
                self.assertEqual(restored.snapshot()['status'], 'idle')
                self.assertTrue(restored.load_warning)

    def test_failed_atomic_replacement_preserves_old_file_and_removes_temporary_file(self):
        self.timer.start('可恢复', 5, 1)
        self.time.advance(20)
        before = self.timer.path.read_bytes()
        with patch('starpet.storage.os.replace', side_effect=PermissionError('fixture file locked')):
            with self.assertRaises(FocusError):
                self.timer.pause()
        self.assertEqual(self.timer.snapshot()['status'], 'running')
        self.assertEqual(self.timer.path.read_bytes(), before)
        self.assertEqual({path.name for path in self.folder.iterdir()}, {'focus-timer.json'})
        self.assertEqual(self.new_timer().snapshot()['remainingSeconds'], 280)
        self.assertEqual(self.timer.pause()['status'], 'paused')

    def test_start_write_failure_never_claims_active_or_creates_a_file(self):
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(FocusError):
                self.timer.start('专注', 5, 1)
        self.assertEqual(self.timer.snapshot()['status'], 'idle')
        self.assertFalse(self.timer.path.exists())

    def test_failed_actions_keep_old_memory_and_recoverable_file(self):
        for action in ('pause', 'resume', 'cancel', 'begin_next', 'notify', 'shown', 'acknowledge', 'expiry'):
            with self.subTest(action=action):
                timer = self.new_timer(self.folder / action)
                timer.start('可恢复', 5, 1)
                if action == 'resume':
                    timer.pause()
                elif action in ('begin_next', 'shown', 'acknowledge'):
                    self.expire(timer)
                elif action == 'expiry':
                    self.time.advance(301)
                before_memory = copy.deepcopy(timer._data)
                before_file = timer.path.read_bytes()
                operation = {'pause': timer.pause, 'resume': timer.resume, 'cancel': timer.cancel,
                             'begin_next': timer.begin_next, 'notify': lambda: timer.set_notify_enabled(False),
                             'shown': lambda: timer.mark_notice_shown(timer.pending_notice()['id']),
                             'acknowledge': lambda: timer.acknowledge(timer.pending_notice()['id']),
                             'expiry': timer.tick}[action]
                with patch('starpet.focus.atomic_json', side_effect=OSError('fixture disk failure')):
                    with self.assertRaises(FocusError):
                        operation()
                self.assertEqual(timer._data, before_memory)
                self.assertEqual(timer.path.read_bytes(), before_file)
                self.assertTrue(timer.snapshot()['warning'])
                restored = self.new_timer(timer.path.parent)
                self.assertEqual(restored.snapshot()['status'], before_memory['status'])

    def test_expiry_write_retry_is_bounded_and_keeps_notice_identity(self):
        self.timer.start('专注', 5, 1)
        identity = self.timer._data['timerId']
        self.time.advance(301)
        with patch('starpet.focus.atomic_json', side_effect=OSError('fixture')) as writer:
            with self.assertRaises(FocusError):
                self.timer.tick()
            for _ in range(20):
                self.timer.tick()
            self.assertEqual(writer.call_count, 1)
            self.assertIsNone(self.timer.pending_notice())
        self.time.advance(5)
        self.timer.tick()
        self.assertEqual(self.timer.pending_notice()['id'], identity)
        self.assertFalse(self.timer.snapshot()['warning'])

    def test_invalid_state_actions_never_skip_a_phase(self):
        for operation in (self.timer.pause, self.timer.resume, self.timer.begin_next):
            with self.assertRaises(FocusError):
                operation()
        self.timer.start('专注', 5, 1)
        before = self.timer.path.read_bytes()
        for operation in (self.timer.resume, self.timer.begin_next, lambda: self.timer.start('替换', 10, 2)):
            with self.assertRaises(FocusError):
                operation()
            self.assertEqual(self.timer.path.read_bytes(), before)

    def test_slow_successful_write_does_not_extend_running_timer(self):
        def slow(path, value):
            atomic_json(path, value)
            self.time.advance(4)
        with patch('starpet.focus.atomic_json', side_effect=slow):
            result = self.timer.start('专注', 5, 1)
        self.assertEqual(result['remainingSeconds'], 296)
        self.assertEqual(self.new_timer().snapshot()['remainingSeconds'], 296)


if __name__ == '__main__':
    unittest.main()
