"""Recoverable local focus timer; never reports or changes client task state."""
import copy
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .storage import atomic_json


MAX_FILE_BYTES = 16384
CHECKPOINT_SECONDS = 30
STATUSES = ('idle', 'running', 'paused', 'awaiting')


class FocusError(Exception):
    """A timer action could not be committed; the old state remains usable."""


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or value != value.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _defaults(now):
    return {'schema': 1, 'status': 'idle', 'phase': 'work', 'label': '专注时间',
            'workMinutes': 25, 'breakMinutes': 5, 'notifyEnabled': True,
            'remainingSeconds': 0.0, 'savedAt': now, 'deadline': None,
            'timerId': None, 'notice': None}


def _validate(data):
    if not isinstance(data, dict) or set(data) != set(_defaults(0)):
        raise ValueError('incomplete timer schema')
    if type(data['schema']) is not int or data['schema'] != 1:
        raise ValueError('unsupported timer schema')
    if data['status'] not in STATUSES or data['phase'] not in ('work', 'break'):
        raise ValueError('invalid state')
    if not _label(data['label']):
        raise ValueError('invalid label')
    for key, low, high in (('workMinutes', 5, 180), ('breakMinutes', 1, 60)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError('invalid duration')
    remaining = data['remainingSeconds']
    maximum = data['workMinutes' if data['phase'] == 'work' else 'breakMinutes'] * 60
    if (type(data['notifyEnabled']) is not bool or not _number(data['savedAt'])
            or not _number(remaining) or not 0 <= remaining <= maximum):
        raise ValueError('invalid timing')
    if data['status'] == 'idle':
        if remaining != 0 or data['timerId'] is not None or data['notice'] is not None:
            raise ValueError('idle has an active timer')
    elif not isinstance(data['timerId'], str) or not re.fullmatch(r'[a-f0-9]{32}', data['timerId']):
        raise ValueError('invalid phase identity')
    if data['status'] == 'running':
        if (not _number(data['deadline'])
                or abs(data['deadline'] - (data['savedAt'] + remaining)) > .01):
            raise ValueError('inconsistent deadline')
    elif data['deadline'] is not None:
        raise ValueError('nonrunning deadline')
    if data['status'] == 'paused' and remaining <= 0:
        raise ValueError('expired pause')
    notice = data['notice']
    if data['status'] == 'awaiting':
        if (remaining != 0 or not isinstance(notice, dict)
                or set(notice) != {'id', 'shown', 'acknowledged'} or notice['id'] != data['timerId']
                or type(notice['shown']) is not bool or type(notice['acknowledged']) is not bool):
            raise ValueError('invalid completion notice')
    elif notice is not None:
        raise ValueError('unexpected notice')
    return data


class FocusTimer:
    """One user-controlled work/break sequence stored only in focus-timer.json.

    In-process elapsed time is monotonic. A restart uses the saved wall-clock
    deadline, capped by the saved remaining time so a backward wall-clock jump
    cannot add time. Normal checkpoints are limited to once per 30 seconds.
    Call mark_notice_shown(id) successfully BEFORE displaying a popup; the
    persisted ID then prevents a second popup after restart. Acknowledge and
    begin_next are separate actions; no offline cycles are auto-completed.
    """
    def __init__(self, state_dir, clock=time.time, monotonic=time.monotonic):
        self.path = Path(state_dir).absolute() / 'focus-timer.json'
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self.load_warning = ''
        self._write_warning = ''
        wall, mono = self._moment()
        data = _defaults(wall)
        try:
            self._safe_path()
            if self.path.exists():
                if self.path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError('oversized')
                with self.path.open('rb') as source:
                    raw = source.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError('oversized')
                data = _validate(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object))
        except (OSError, ValueError, TypeError, RecursionError, FocusError):
            self.load_warning = '专注记录损坏、缺少字段或版本不支持，未恢复计时；原文件未改动。'
            data = _defaults(wall)
        if data['status'] == 'running':
            data = copy.deepcopy(data)
            data['remainingSeconds'] = max(0.0, min(data['remainingSeconds'], data['deadline'] - wall))
        self._adopt(data, mono)
        self._retry_after = float('-inf')

    def _moment(self):
        wall, mono = self.clock(), self.monotonic()
        if not _number(wall) or not _number(mono):
            raise FocusError('本机时间不可用，专注计时未改变。')
        return float(wall), float(mono)

    def _safe_path(self):
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise FocusError('专注记录路径包含链接或联接目录，未写入。')

    def _adopt(self, data, mono):
        self._data = copy.deepcopy(data)
        self._anchor = mono
        self._elapsed = 0.0

    def _remaining(self, mono):
        if self._data['status'] != 'running':
            return self._data['remainingSeconds']
        # A malformed/injected monotonic clock cannot grow the remaining time.
        self._elapsed = max(self._elapsed, mono - self._anchor, 0.0)
        return max(0.0, self._data['remainingSeconds'] - self._elapsed)

    def _materialize(self, wall, mono):
        value = copy.deepcopy(self._data)
        value['remainingSeconds'] = self._remaining(mono)
        if value['status'] == 'running' and value['remainingSeconds'] <= 0:
            value.update(status='awaiting', deadline=None,
                         notice={'id': value['timerId'], 'shown': False, 'acknowledged': False})
        value['savedAt'] = wall
        value['deadline'] = wall + value['remainingSeconds'] if value['status'] == 'running' else None
        return value

    def _commit(self, value, mono):
        _validate(value)
        try:
            self._safe_path()
            atomic_json(self.path, value)
        except (OSError, FocusError) as error:
            self._write_warning = '专注记录未能保存，本次操作未生效；原有可恢复记录保留。'
            raise FocusError(self._write_warning) from error
        # State changes only after the atomic replacement succeeds. The anchor
        # uses the pre-write moment so slow disk I/O does not lengthen a timer.
        self._adopt(value, mono)
        self.load_warning = self._write_warning = ''
        self._retry_after = float('-inf')

    def _snapshot(self, mono):
        data, notice = self._data, self._data['notice']
        pending = bool(notice and not notice['shown'] and not notice['acknowledged'])
        return {'status': data['status'], 'phase': data['phase'], 'label': data['label'],
                'workMinutes': data['workMinutes'], 'breakMinutes': data['breakMinutes'],
                'remainingSeconds': math.ceil(self._remaining(mono)),
                'nextPhase': ('break' if data['phase'] == 'work' else 'work') if data['status'] != 'idle' else None,
                'notifyEnabled': data['notifyEnabled'], 'noticePending': pending,
                'noticeId': notice['id'] if notice else None,
                'noticeShown': bool(notice and notice['shown']),
                'noticeAcknowledged': bool(notice and notice['acknowledged']),
                'warning': self._write_warning or self.load_warning}

    def snapshot(self):
        with self._lock:
            return self._snapshot(self._moment()[1])

    def start(self, label='专注时间', work_minutes=25, break_minutes=5):
        with self._lock:
            if self._data['status'] != 'idle':
                raise FocusError('已有专注计时，请先取消，或在到时后开始下一阶段。')
            if (not isinstance(label, str) or not _label(label.strip())
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in label)):
                raise FocusError('专注名称须为 1～80 字，不能包含换行或控制字符。')
            if (type(work_minutes) is not int or not 5 <= work_minutes <= 180
                    or type(break_minutes) is not int or not 1 <= break_minutes <= 60):
                raise FocusError('专注时间须为 5～180 分钟，休息须为 1～60 分钟。')
            wall, mono = self._moment()
            value = _defaults(wall)
            value.update(status='running', label=label.strip(), workMinutes=work_minutes,
                         breakMinutes=break_minutes, remainingSeconds=float(work_minutes * 60),
                         deadline=wall + work_minutes * 60, timerId=uuid.uuid4().hex,
                         notifyEnabled=self._data['notifyEnabled'])
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def tick(self):
        with self._lock:
            wall, mono = self._moment()
            if self._data['status'] == 'running':
                value = self._materialize(wall, mono)
                due = value['status'] == 'awaiting'
                if mono >= self._retry_after and (due or self._elapsed >= CHECKPOINT_SECONDS):
                    try:
                        self._commit(value, mono)
                    except FocusError:
                        self._retry_after = mono + (5 if due else CHECKPOINT_SECONDS)
                        raise
            return self._snapshot(self._moment()[1])

    def pause(self):
        with self._lock:
            if self._data['status'] != 'running':
                raise FocusError('只有正在计时的阶段可以暂停。')
            wall, mono = self._moment()
            value = self._materialize(wall, mono)
            if value['status'] == 'running':
                value.update(status='paused', deadline=None)
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def resume(self):
        with self._lock:
            if self._data['status'] != 'paused':
                raise FocusError('只有已暂停的阶段可以继续。')
            wall, mono = self._moment()
            value = self._materialize(wall, mono)
            value.update(status='running', deadline=wall + value['remainingSeconds'])
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def cancel(self):
        with self._lock:
            if self._data['status'] == 'idle' and not self.load_warning and not self._write_warning:
                return self.snapshot()
            wall, mono = self._moment()
            value = self._materialize(wall, mono)
            value.update(status='idle', phase='work', remainingSeconds=0.0,
                         deadline=None, timerId=None, notice=None)
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def begin_next(self):
        with self._lock:
            if self._data['status'] != 'awaiting':
                raise FocusError('当前阶段尚未到时，请先完成或取消本次计时。')
            wall, mono = self._moment()
            value = self._materialize(wall, mono)
            phase = 'break' if value['phase'] == 'work' else 'work'
            duration = value['breakMinutes' if phase == 'break' else 'workMinutes'] * 60
            value.update(status='running', phase=phase, remainingSeconds=float(duration),
                         deadline=wall + duration, timerId=uuid.uuid4().hex, notice=None)
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def set_notify_enabled(self, enabled):
        if type(enabled) is not bool:
            raise FocusError('提醒开关必须为开启或关闭。')
        with self._lock:
            if self._data['notifyEnabled'] == enabled:
                return self.snapshot()
            wall, mono = self._moment()
            value = self._materialize(wall, mono)
            value['notifyEnabled'] = enabled
            self._commit(value, mono)
            return self._snapshot(self._moment()[1])

    def pending_notice(self):
        with self._lock:
            data, notice = self._data, self._data['notice']
            if not data['notifyEnabled'] or not notice or notice['shown'] or notice['acknowledged']:
                return None
            phase = '专注' if data['phase'] == 'work' else '休息'
            return {'id': notice['id'], 'kind': 'focus_timer', 'phase': data['phase'], 'label': data['label'],
                    'title': phase + '时间到了',
                    'message': phase + '计时结束。由你决定何时开始下一阶段；这不代表工作任务已完成。'}

    def _notice_action(self, identity, field):
        notice = self._data['notice']
        if (not isinstance(identity, str) or not notice or notice['id'] != identity
                or notice[field] or (field == 'shown' and
                                    (notice['acknowledged'] or not self._data['notifyEnabled']))):
            return False
        wall, mono = self._moment()
        value = self._materialize(wall, mono)
        value['notice'][field] = True
        self._commit(value, mono)
        return True

    def mark_notice_shown(self, identity):
        with self._lock:
            return self._notice_action(identity, 'shown')

    def acknowledge(self, identity):
        with self._lock:
            return self._notice_action(identity, 'acknowledged')
