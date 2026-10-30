"""Local, temporary pet reactions; task state remains owned by the caller.

This module only returns presentation values. It never invents a task completion,
persists an interaction, accesses a model, or samples the system wall clock.
"""
import math
import random
import time


INTERVALS = {'calm': (45, 90), 'normal': (25, 45), 'lively': (12, 22)}
PROTECTED = frozenset(('waiting', 'error', 'working', 'thinking', 'unknown', 'interrupted', 'completed'))
MANUAL = frozenset(('head_pat', 'poke', 'tail', 'drag_start', 'drag_end', 'wave'))
MOTION_KINDS = MANUAL - {'drag_start'}
DURATIONS = {'head_pat': 2.6, 'poke': 1.7, 'tail': 2.1, 'drag_end': .9,
             'wave': 2.6, 'look': 3.0, 'stretch': 3.2, 'rest': 4.4}
ACTION_KINDS = ('head_pat', 'poke', 'tail', 'drag_start', 'drag_end', 'wave', 'look', 'stretch', 'rest')
PERSONALITIES = ('gentle', 'playful', 'concise')
ROLE_KINDS = ('tabby', 'catgirl', 'robot', 'custom')

# These are short local reactions, not generated conversation or observations
# about the user's life. Shared lines avoid inventing anatomy for custom art.
_STYLE_LINES = {
    'gentle': {
        'head_pat': ('谢谢，这份温柔收到了', '轻轻的，感觉很放松'),
        'poke': ('我在呢，慢慢说', '嗯，我听着'),
        'tail': ('轻轻碰一下就好', '收到你的招呼了'),
        'drag_start': ('换个位置也好', '慢慢挪，不着急'),
        'drag_end': ('这里挺舒服的', '好，就停在这里'),
        'wave': ('嗨，很高兴见到你', '我在这里，随时聊'),
        'look': ('不着急，慢慢来', '按自己的节奏就好'),
        'stretch': ('稍微放松一下吧', '给自己留一点空隙'),
        'rest': ('我安静陪你一会儿', '需要时再叫我'),
    },
    'playful': {
        'head_pat': ('摸摸收到，心情加一', '嘿，这下精神了'),
        'poke': ('嗨，被你点到了', '到我出场了吗'),
        'tail': ('咦，你碰到我啦', '轻一点，我在呢'),
        'drag_start': ('换个位置看看', '小小搬家，出发'),
        'drag_end': ('好位置，就这里', '新座位，还不错'),
        'wave': ('嗨，来打个招呼', '今天也一起加油'),
        'look': ('我在这边冒个泡', '我在这里，有事招呼'),
        'stretch': ('留个空隙，歇一会儿', '休息一下也挺好'),
        'rest': ('我先安静待一会儿', '不急，留点空闲'),
    },
    'concise': {
        'head_pat': ('收到，谢谢', '轻轻的，正好'),
        'poke': ('我在', '请说'),
        'tail': ('收到触碰', '我注意到了'),
        'drag_start': ('移到哪里', '可以调整位置'),
        'drag_end': ('位置合适', '就放这里'),
        'wave': ('你好，我在', '需要时叫我'),
        'look': ('按你的节奏来', '有需要再叫我'),
        'stretch': ('休息片刻也好', '可以放松一下'),
        'rest': ('安静待一会儿', '有事再聊'),
    },
}

# Only moments with meaningful character differences need dedicated wording.
# No names are embedded: a renamed character must never speak an old name.
_ROLE_LINES = {
    'tabby': {
        'gentle': {
            'head_pat': ('喵，轻轻摸就好', '呼噜，舒服多了'),
            'tail': ('尾巴有一点怕痒', '轻轻碰，别拉尾巴哦'),
            'stretch': ('小猫伸个懒腰', '爪爪伸一伸，放松啦'),
        },
        'playful': {
            'head_pat': ('喵，摸摸收到啦', '呼噜呼噜，刚刚好'),
            'tail': ('哎呀，尾巴被发现啦', '尾巴怕痒，轻一点嘛'),
            'stretch': ('爪爪伸长，懒腰拉满', '小猫伸展一下，嘿'),
        },
        'concise': {
            'head_pat': ('喵，很舒服', '摸摸收到'),
            'tail': ('尾巴怕痒', '轻碰尾巴就好'),
            'stretch': ('伸伸爪', '小猫伸个腰'),
        },
    },
    'catgirl': {
        'gentle': {
            'head_pat': ('这样轻轻摸就好', '嗯，放松了不少'),
            'tail': ('尾巴有点敏感，轻些', '轻轻碰尾巴就好'),
            'stretch': ('伸个懒腰，舒展一下', '肩膀也放松一点吧'),
        },
        'playful': {
            'head_pat': ('摸摸收到，精神回来了', '嘿，手法还不错'),
            'tail': ('哎呀，碰到尾巴了', '尾巴怕痒，轻点碰'),
            'stretch': ('懒腰伸完，又精神了', '换个姿势，松快一点'),
        },
        'concise': {
            'head_pat': ('嗯，舒服一些了', '这样轻轻摸就好'),
            'tail': ('尾巴请轻碰', '别拉尾巴就好'),
            'stretch': ('伸个懒腰', '肩膀放松一下'),
        },
    },
    'robot': {
        'gentle': {
            'head_pat': ('轻轻碰，招呼收到了', '温柔的触碰，收到啦'),
            'tail': ('我在，不用着急', '收到，轻轻碰就好'),
            'look': ('需要理清思路时叫我', '我在这边，随时可以聊'),
            'stretch': ('换个姿势，放松一下', '留点空隙，缓一缓'),
        },
        'playful': {
            'head_pat': ('机壳收到一份摸摸', '摸摸已签收，谢谢啦'),
            'tail': ('咦，机壳被点中了', '轻轻一点，我就收到啦'),
            'look': ('小助手在这儿候场', '有点子时，来碰个头'),
            'stretch': ('小小伸展，缓一缓', '换个姿势再待会儿'),
        },
        'concise': {
            'head_pat': ('触碰已收到', '轻触即可'),
            'tail': ('触碰收到', '我在，请说'),
            'look': ('需要梳理时叫我', '随时可以讨论'),
            'stretch': ('调整姿势', '放松片刻'),
        },
    },
}

# Complete and deterministic 4-role x 3-style x 9-action table. Each action has
# two alternatives so repeated gestures need not repeat the previous line.
LINE_TABLE = {
    role: {style: {kind: _ROLE_LINES.get(role, {}).get(style, {}).get(kind, _STYLE_LINES[style][kind])
                   for kind in ACTION_KINDS} for style in PERSONALITIES}
    for role in ROLE_KINDS
}
LINES = LINE_TABLE['tabby']['playful']  # Compatibility for the original default.


def _identity(role_id):
    if isinstance(role_id, str) and (role_id in ROLE_KINDS or role_id.startswith('custom-')):
        return role_id
    return 'custom'


def _role_kind(role_id):
    return role_id if role_id in ROLE_KINDS else 'custom'


class Behavior:
    """A clock-injectable overlay with bounded, non-repeating idle actions.

    ``sample()['pose']`` is a drawing pose, never a new task state. An empty
    ``text`` means the UI should keep its normal, truthful task/idle message.
    Hidden and chat windows suppress automatic companionship; direct gestures
    are still usable while a chat is open.
    """

    def __init__(self, clock=time.monotonic, rng=None):
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        self.activity = 'normal'
        self.idle_messages = True
        self.touch_enabled = True
        self.role_id = 'tabby'
        self.personality = 'playful'
        self.quiet_until = 0.0
        self._kind = None
        self._started = 0.0
        self._line = ''
        self._manual = False
        self._action_serial = 0
        self._action_id = None
        self._motion_id = None
        self._motion_deadline = 0.0
        self._motion_allowed = True
        self._linger_line = ''
        self._linger_until = 0.0
        self._last_manual = float('-inf')
        self._last_idle = None
        self._last_line = ''
        self._suppressed = False
        self._next_idle = self.clock() + self._interval()

    def _interval(self):
        return self.rng.uniform(*INTERVALS[self.activity])

    def _schedule(self, now):
        self._next_idle = now + self._interval()

    def configure(self, activity='normal', idle_messages=True, touch_enabled=True,
                  role_id='tabby', personality='playful'):
        """Apply presentation settings; identity changes never reset quiet/cooldown.

        Pass the current role_id/personality on each settings refresh. Custom
        identities share neutral wording but still clear overlays when switched.
        """
        activity = activity if activity in INTERVALS else 'normal'
        role_id = _identity(role_id)
        personality = personality if personality in PERSONALITIES else 'playful'
        changed_identity = (role_id, personality) != (self.role_id, self.personality)
        reschedule = changed_identity or activity != self.activity
        self.activity = activity
        self.role_id, self.personality = role_id, personality
        if changed_identity:
            self._clear()
            self._last_line = ''
            self._last_idle = None
        if reschedule:
            self._schedule(max(self.clock(), self.quiet_until))
        self.idle_messages = bool(idle_messages)
        self.touch_enabled = bool(touch_enabled)
        if not self.touch_enabled and (self._motion_id is not None or self._linger_line or
                (self._manual and self._kind not in ('drag_start', 'drag_end'))):
            self._clear()

    def _clear(self):
        self._kind = None
        self._manual = False
        self._line = ''
        self._action_id = None
        self._motion_id = None
        self._motion_deadline = 0.0
        self._linger_line = ''
        self._linger_until = 0.0

    def _begin(self, kind, now, manual):
        self._clear()
        self._action_serial += 1
        self._action_id = self._action_serial
        self._kind, self._started, self._manual = kind, now, manual
        lines = LINE_TABLE[_role_kind(self.role_id)][self.personality][kind]
        choices = [line for line in lines if line != self._last_line]
        self._line = self.rng.choice(choices)
        self._last_line = self._line

    def _finish_visual(self, now):
        """End motion, retaining only the remaining manual-text reading time."""
        line = self._line if self._manual else ''
        until = self._started + DURATIONS.get(self._kind, 0)
        self._clear()
        if line and now < until:
            self._linger_line, self._linger_until = line, until
        self._schedule(max(now, self.quiet_until))

    def _expire(self, now):
        if self._linger_line and now >= self._linger_until:
            self._linger_line, self._linger_until = '', 0.0
        if self._motion_id is not None:
            if now >= self._motion_deadline:
                self._finish_visual(now)
        elif self._kind and self._kind != 'drag_start' and now - self._started >= DURATIONS[self._kind]:
            self._clear()
            self._schedule(max(now, self.quiet_until))

    def _matches_motion_action(self, action_id):
        return (type(action_id) is int and action_id == self._action_id and
                self._manual and self._kind in MOTION_KINDS)

    def bind_motion(self, action_id, now=None):
        """Bind one current gesture to frame callbacks; repeated binds do not renew.

        The caller should bind finite clips only. A looping clip has no finish
        callback and otherwise keeps renewing until interrupted. The first frame
        should synchronously renew the initial one-second lease.
        """
        if not self._matches_motion_action(action_id) or not self._motion_allowed:
            return False
        now = self.clock() if now is None else now
        self._expire(now)
        if not self._matches_motion_action(action_id):
            return False
        if self._motion_id is None:
            self._motion_id = action_id
            self._motion_deadline = now + 1.0
        return True

    def motion_frame(self, action_id, delay_ms, now=None):
        """Renew only the live binding, with a bounded decoder-stall fallback."""
        if not self._matches_motion_action(action_id) or self._motion_id != action_id:
            return False
        try:
            delay = float(delay_ms)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(delay):
            return False
        now = self.clock() if now is None else now
        self._expire(now)
        if not self._matches_motion_action(action_id) or self._motion_id != action_id:
            return False
        self._motion_deadline = now + min(65.0, max(1.0, delay / 1000.0 + 1.0))
        return True

    def finish_motion(self, action_id, now=None):
        """Finish one live binding; late/duplicate callbacks cannot touch a new one."""
        if not self._matches_motion_action(action_id) or self._motion_id != action_id:
            return False
        now = self.clock() if now is None else now
        self._expire(now)
        if not self._matches_motion_action(action_id) or self._motion_id != action_id:
            return False
        self._finish_visual(now)
        return True

    def trigger(self, kind, now=None):
        """Accept a user gesture, or return False for disabled/cooling gestures."""
        if kind not in MANUAL:
            return False
        now = self.clock() if now is None else now
        self._expire(now)
        if kind not in ('drag_start', 'drag_end'):
            # A second identical touch should not restart an unfinished clip or
            # consume the cooldown of a different gesture requested next.
            if self._motion_id is not None and kind == self._kind:
                return False
            if not self.touch_enabled or self._kind == 'drag_start' or now - self._last_manual < 1.2:
                return False
            self._last_manual = now
        self._begin(kind, now, True)
        self._schedule(now)
        return True

    def quiet(self, minutes=30, now=None):
        """Pause automatic idle behavior; real work and direct gestures continue."""
        now = self.clock() if now is None else now
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 30.0
        if not math.isfinite(minutes):
            minutes = 30.0
        self.quiet_until = now + max(0.0, minutes) * 60.0
        if not self._manual and not self._linger_line:
            self._clear()
        self._schedule(self.quiet_until)

    def resume(self):
        self.quiet_until = 0.0
        self._schedule(self.clock())

    @staticmethod
    def _base_pose(base_state):
        if base_state in ('idle', 'thinking', 'working', 'waiting', 'completed', 'error'):
            return base_state
        return 'error' if base_state in ('unknown', 'interrupted') else 'idle'

    def sample(self, base_state='idle', now=None, reduced_motion=False, visible=True, chatting=False):
        now = self.clock() if now is None else now
        base_pose = self._base_pose(base_state)
        protected = base_state in PROTECTED
        self._motion_allowed = not (protected or not visible or reduced_motion)
        if self._motion_id is not None and not self._motion_allowed:
            self._finish_visual(now)
        self._expire(now)
        suppressed = protected or not visible or chatting or now < self.quiet_until or reduced_motion
        if suppressed:
            if not self._manual and not self._linger_line:
                self._clear()
            self._suppressed = True
        elif self._suppressed:
            # Never catch up on missed actions after a busy/hidden/quiet period.
            self._schedule(now)
            self._suppressed = False

        kind = self._kind
        if not kind and not suppressed and now >= self._next_idle:
            choices = [action for action in ('look', 'stretch', 'rest') if action != self._last_idle]
            kind = self.rng.choice(choices)
            self._last_idle = kind
            self._begin(kind, now, False)

        result = {'kind': 'idle', 'action_id': None, 'pose': base_pose, 'text': '', 'dx': 0.0, 'dy': 0.0,
                  'rotation': 0.0, 'sx': 1.0, 'sy': 1.0, 'effect': 'none', 'progress': 0.0}
        if self._linger_line and not protected and visible:
            result['text'] = self._linger_line
        if not kind or not visible:
            return result
        elapsed = max(0.0, now - self._started)
        progress = 0.0 if kind == 'drag_start' else min(1.0, elapsed / DURATIONS[kind])
        result.update(kind=kind, action_id=self._action_id, progress=progress)
        if not protected:
            if (self._manual or self.idle_messages) and (self._motion_id is None or elapsed < DURATIONS[kind]):
                result['text'] = self._line
            if kind in ('head_pat', 'wave'):
                result['pose'] = 'completed'

        if reduced_motion:
            return result

        # Direct feedback can decorate work without replacing its task pose.
        if kind == 'head_pat':
            result['effect'] = 'heart'
        elif kind in ('poke', 'tail', 'wave'):
            result['effect'] = 'spark'
        elif kind == 'rest' and not protected:
            result['effect'] = 'zzz'
        if protected and kind not in ('drag_start', 'drag_end'):
            return result

        envelope = math.sin(math.pi * progress)
        if kind == 'head_pat':
            result.update(dy=2.0 * envelope, rotation=2.2 * math.sin(progress * math.tau) * envelope,
                          sx=1.0 + .025 * envelope, sy=1.0 - .025 * envelope)
        elif kind == 'poke':
            result.update(dx=4.0 * math.sin(progress * math.tau * 2) * (1.0-progress),
                          sy=1.0 - .025 * envelope)
        elif kind == 'tail':
            result.update(rotation=3.5 * math.sin(progress * math.tau * 2) * envelope)
        elif kind == 'wave':
            result.update(rotation=3.0 * math.sin(progress * math.tau * 2) * envelope,
                          dy=-3.0 * envelope)
        elif kind == 'drag_start':
            result.update(rotation=math.sin(elapsed * 5.0) * 4.0, dy=-2.0)
        elif kind == 'drag_end':
            wave = math.sin(progress * math.pi * 3) * (1.0-progress)
            result.update(dy=-5.0 * abs(wave), sx=1.0 + .035 * wave, sy=1.0 - .035 * wave)
        elif kind == 'look':
            result.update(dx=2.5 * math.sin(progress * math.tau),
                          rotation=2.0 * math.sin(progress * math.tau))
        elif kind == 'stretch':
            result.update(sx=1.0 - .025 * envelope, sy=1.0 + .04 * envelope, dy=-2.0 * envelope)
        elif kind == 'rest':
            result.update(dy=1.5 * envelope, rotation=-2.0 * envelope)
        return result
