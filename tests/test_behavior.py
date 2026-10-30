import random
import unittest

from starpet.behavior import (ACTION_KINDS, Behavior, DURATIONS, INTERVALS, LINES, LINE_TABLE,
                             MANUAL, MOTION_KINDS, PERSONALITIES, PROTECTED, ROLE_KINDS)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FirstChoice:
    """Select a requested idle action without depending on random's algorithm."""
    action = None

    def uniform(self, low, high):
        return low

    def choice(self, choices):
        return self.action if self.action in choices else choices[0]


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.behavior = Behavior(self.clock, random.Random(0))

    def assert_still(self, sample):
        self.assertEqual([sample[k] for k in ('dx', 'dy', 'rotation', 'sx', 'sy', 'effect')],
                         [0.0, 0.0, 0.0, 1.0, 1.0, 'none'])

    def test_initial_identity_and_short_local_lines(self):
        self.assert_still(self.behavior.sample())
        self.assertEqual(self.behavior.sample()['text'], '')
        self.assertTrue(all(len(line) <= 14 for lines in LINES.values() for line in lines))

    def test_manual_reaction_expires_into_latest_real_task(self):
        self.assertTrue(self.behavior.trigger('head_pat'))
        self.clock.advance(.6)
        reaction = self.behavior.sample()
        self.assertEqual(reaction['kind'], 'head_pat')
        self.assertEqual(reaction['pose'], 'completed')
        self.assertTrue(reaction['text'])
        self.clock.advance(3)
        restored = self.behavior.sample('thinking')
        self.assertEqual(restored['kind'], 'idle')
        self.assertEqual(restored['pose'], 'thinking')
        self.assertEqual(restored['text'], '')

    def test_critical_states_never_lose_task_pose_or_text(self):
        self.behavior.trigger('head_pat')
        self.clock.advance(.5)
        for state, pose in [('waiting', 'waiting'), ('error', 'error'), ('working', 'working'),
                            ('thinking', 'thinking'), ('unknown', 'error'), ('interrupted', 'error'), ('completed', 'completed')]:
            sample = self.behavior.sample(state)
            self.assertEqual(sample['pose'], pose)
            self.assertEqual(sample['text'], '')
            self.assertEqual(sample['rotation'], 0)
            self.assertEqual(sample['effect'], 'heart')
        self.clock.advance(120)
        self.assertEqual(self.behavior.sample('working')['kind'], 'idle')

    def test_gesture_cooldown_and_drag_bypass(self):
        self.assertTrue(self.behavior.trigger('poke'))
        self.assertFalse(self.behavior.trigger('tail'))
        self.clock.advance(1.21)
        self.assertTrue(self.behavior.trigger('tail'))
        self.assertTrue(self.behavior.trigger('drag_start'))
        self.clock.advance(.3)
        self.assertNotEqual(self.behavior.sample('working')['rotation'], 0)
        self.assertFalse(self.behavior.trigger('head_pat'))
        self.assertTrue(self.behavior.trigger('drag_end'))
        self.clock.advance(.5)
        self.assertEqual(self.behavior.sample()['kind'], 'drag_end')
        self.clock.advance(.41)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertFalse(self.behavior.trigger('invented_action'))

    def test_touch_toggle_keeps_dragging_available(self):
        self.behavior.trigger('head_pat')
        self.behavior.configure(touch_enabled=False)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertFalse(self.behavior.trigger('poke'))
        self.assertTrue(self.behavior.trigger('drag_start'))
        self.assertTrue(self.behavior.trigger('drag_end'))

    def test_idle_activity_intervals_and_nonrepeating_actions(self):
        for activity, bounds in INTERVALS.items():
            self.behavior.configure(activity=activity)
            previous_action = previous_text = None
            for _ in range(8):
                delay = self.behavior._next_idle - self.clock()
                self.assertGreaterEqual(delay, bounds[0])
                self.assertLessEqual(delay, bounds[1])
                self.clock.advance(delay)
                sample = self.behavior.sample()
                self.assertIn(sample['kind'], ('look', 'stretch', 'rest'))
                self.assertNotEqual(sample['kind'], previous_action)
                self.assertNotEqual(sample['text'], previous_text)
                previous_action, previous_text = sample['kind'], sample['text']
                self.clock.advance(5)
                self.assertEqual(self.behavior.sample()['kind'], 'idle')

    def test_quiet_suppresses_idle_but_allows_gestures_and_work(self):
        self.behavior.quiet(minutes=30)
        self.clock.advance(100)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertTrue(self.behavior.trigger('wave'))
        self.assertTrue(self.behavior.sample()['text'])
        self.assertEqual(self.behavior.sample('waiting')['pose'], 'waiting')
        self.clock.advance(4)
        self.behavior.sample()
        self.behavior.resume()
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.clock.advance(50)
        self.assertIn(self.behavior.sample()['kind'], ('look', 'stretch', 'rest'))

    def test_hidden_chat_busy_and_quiet_expiry_do_not_catch_up(self):
        for kwargs in ({'visible': False}, {'chatting': True}, {'base_state': 'working'},
                       {'reduced_motion': True}):
            self.clock.advance(100)
            self.assertEqual(self.behavior.sample(**kwargs)['kind'], 'idle')
            self.assertEqual(self.behavior.sample()['kind'], 'idle')
            self.clock.advance(50)
            self.assertIn(self.behavior.sample()['kind'], ('look', 'stretch', 'rest'))
        self.behavior.quiet(minutes=1)
        self.behavior.sample()
        self.clock.advance(61)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')

    def test_reduced_motion_is_still_but_direct_text_remains(self):
        self.behavior.trigger('head_pat')
        self.clock.advance(.5)
        sample = self.behavior.sample(reduced_motion=True)
        self.assert_still(sample)
        self.assertTrue(sample['text'])
        self.assertEqual(self.behavior.sample(visible=False)['text'], '')

    def test_idle_messages_toggle_does_not_disable_motion(self):
        self.behavior.configure(idle_messages=False)
        self.clock.advance(50)
        sample = self.behavior.sample()
        self.assertEqual(sample['text'], '')
        self.clock.advance(.8)
        sample = self.behavior.sample()
        self.assertTrue(any(sample[k] != value for k, value in
                            (('dx', 0), ('dy', 0), ('rotation', 0), ('sx', 1), ('sy', 1))))
        self.assertTrue(self.behavior.trigger('poke'))
        self.assertTrue(self.behavior.sample()['text'])

    def test_explicit_now_and_animation_bounds(self):
        for kind in ('head_pat', 'poke', 'tail', 'drag_start', 'drag_end', 'wave'):
            start = 200 + 10 * list(('head_pat', 'poke', 'tail', 'drag_start', 'drag_end', 'wave')).index(kind)
            self.assertTrue(self.behavior.trigger(kind, now=start))
            for tick in range(30):
                sample = self.behavior.sample(now=start+tick*.1)
                self.assertLessEqual(abs(sample['dx']), 5)
                self.assertLessEqual(abs(sample['dy']), 5)
                self.assertLessEqual(abs(sample['rotation']), 4)
                self.assertTrue(.95 <= sample['sx'] <= 1.05)
                self.assertTrue(.95 <= sample['sy'] <= 1.05)
                self.assertTrue(0 <= sample['progress'] <= 1)

    def test_complete_table_is_short_name_free_and_body_safe(self):
        self.assertEqual(set(ROLE_KINDS), {'tabby', 'catgirl', 'robot', 'custom'})
        self.assertEqual(set(PERSONALITIES), {'gentle', 'playful', 'concise'})
        self.assertEqual(set(ACTION_KINDS), MANUAL | {'look', 'stretch', 'rest'})
        self.assertEqual(set(LINE_TABLE), set(ROLE_KINDS))
        for role, styles in LINE_TABLE.items():
            self.assertEqual(set(styles), set(PERSONALITIES))
            for style, actions in styles.items():
                self.assertEqual(set(actions), set(ACTION_KINDS))
                for kind, lines in actions.items():
                    with self.subTest(role=role, style=style, action=kind):
                        self.assertIsInstance(lines, tuple)
                        self.assertEqual(len(lines), 2)
                        self.assertEqual(len(set(lines)), 2)
                        for line in lines:
                            self.assertTrue(1 <= len(line) <= 14)
                            self.assertEqual(line, line.strip())
                            self.assertNotIn('\n', line)
                            self.assertFalse(any(name in line for name in ('团团', '橘宁', '小序')))
                            if role in ('robot', 'custom'):
                                self.assertFalse(any(part in line for part in ('喵', '尾巴', '猫耳', '爪', '呼噜')))
                            if role == 'custom':
                                self.assertFalse(any(part in line for part in ('机壳', '肩膀', '耳朵')))

    def test_all_108_role_style_action_cells_are_used_by_public_interactions(self):
        seen = set()
        for role in ROLE_KINDS:
            for style in PERSONALITIES:
                for kind in ACTION_KINDS:
                    with self.subTest(role=role, style=style, action=kind):
                        clock, rng = FakeClock(), FirstChoice()
                        behavior = Behavior(clock, rng)
                        identity = 'custom-own-art' if role == 'custom' else role
                        behavior.configure(role_id=identity, personality=style)
                        if kind in MANUAL:
                            self.assertTrue(behavior.trigger(kind))
                        else:
                            rng.action = kind
                            clock.advance(INTERVALS['normal'][0])
                        sample = behavior.sample()
                        self.assertEqual(sample['kind'], kind)
                        self.assertIn(sample['text'], LINE_TABLE[role][style][kind])
                        seen.add((role, style, kind))
        self.assertEqual(len(seen), 108)

    def test_role_and_style_have_distinct_head_pat_responses(self):
        first_lines = {}
        for role in ROLE_KINDS:
            for style in PERSONALITIES:
                behavior = Behavior(FakeClock(), FirstChoice())
                behavior.configure(role_id=role, personality=style)
                behavior.trigger('head_pat')
                first_lines[role, style] = behavior.sample()['text']
        for role in ROLE_KINDS:
            self.assertEqual(len({first_lines[role, style] for style in PERSONALITIES}), 3)
        for style in PERSONALITIES:
            self.assertEqual(len({first_lines[role, style] for role in ROLE_KINDS}), 4)

    def test_switch_discards_old_line_without_resetting_quiet_or_cooldown(self):
        self.behavior.quiet(30)
        quiet_until = self.behavior.quiet_until
        self.behavior.trigger('head_pat')
        self.assertTrue(self.behavior.sample()['text'])
        self.clock.advance(.5)
        self.behavior.configure(role_id='catgirl', personality='gentle')
        self.assertEqual(self.behavior.sample()['text'], '')
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertEqual(self.behavior.quiet_until, quiet_until)
        self.assertGreaterEqual(self.behavior._next_idle, quiet_until + INTERVALS['normal'][0])
        self.assertFalse(self.behavior.trigger('poke'))
        self.clock.advance(.71)
        self.assertTrue(self.behavior.trigger('head_pat'))
        self.assertIn(self.behavior.sample()['text'], LINE_TABLE['catgirl']['gentle']['head_pat'])
        self.assertEqual(self.behavior.sample('working')['text'], '')
        self.assertEqual(self.behavior.sample('working')['pose'], 'working')
        self.clock.advance(60)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')

    def test_style_and_custom_identity_switches_restart_idle_wait_but_same_settings_do_not(self):
        self.behavior.configure(role_id='custom-first', personality='gentle')
        self.clock.advance(50)
        self.assertIn(self.behavior.sample()['kind'], ('look', 'stretch', 'rest'))
        self.behavior.configure(role_id='custom-first', personality='concise')
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        due = self.behavior._next_idle
        self.assertTrue(self.clock() + 25 <= due <= self.clock() + 45)
        self.behavior.configure(role_id='custom-first', personality='concise')
        self.assertEqual(self.behavior._next_idle, due)
        self.behavior.trigger('head_pat')
        self.behavior.configure(role_id='custom-second', personality='concise')
        self.assertEqual(self.behavior.sample()['text'], '')
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.clock.advance(46)
        self.assertIn(self.behavior.sample()['text'], tuple(line for lines in LINE_TABLE['custom']['concise'].values() for line in lines))

    def test_unknown_role_falls_back_to_neutral_and_invalid_style_to_playful(self):
        for identity in ('unknown-imported-art', '', None, {'role': 'tabby'}):
            with self.subTest(identity=identity):
                behavior = Behavior(self.clock, FirstChoice())
                behavior.configure(role_id=identity, personality='unsupported')
                behavior.trigger('tail')
                self.assertIn(behavior.sample()['text'], LINE_TABLE['custom']['playful']['tail'])

    def test_each_character_style_obeys_task_priority_and_silent_idle_rules(self):
        for role in ROLE_KINDS:
            for style in PERSONALITIES:
                with self.subTest(role=role, style=style):
                    clock = FakeClock()
                    behavior = Behavior(clock, random.Random(0))
                    behavior.configure(role_id=role, personality=style)
                    behavior.trigger('wave')
                    for state in PROTECTED:
                        sample = behavior.sample(state)
                        self.assertEqual(sample['text'], '')
                        self.assertEqual(sample['pose'], 'error' if state in ('unknown', 'interrupted') else state)
                    clock.advance(5)
                    behavior.sample()
                    for kwargs in ({'visible': False}, {'chatting': True}, {'reduced_motion': True}):
                        clock.advance(100)
                        self.assertEqual(behavior.sample(**kwargs)['kind'], 'idle')
                        self.assertEqual(behavior.sample()['kind'], 'idle')
                    behavior.quiet(30)
                    clock.advance(100)
                    self.assertEqual(behavior.sample()['kind'], 'idle')
                    self.assertTrue(behavior.trigger('wave'))
                    self.assertIn(behavior.sample(chatting=True)['text'], LINE_TABLE[role][style]['wave'])
                    self.assert_still(behavior.sample(reduced_motion=True))
                    self.assertEqual(behavior.sample(visible=False)['text'], '')

    def test_role_tables_do_not_repeat_consecutive_lines_or_idle_actions(self):
        for role in ROLE_KINDS:
            for style in PERSONALITIES:
                with self.subTest(role=role, style=style):
                    clock = FakeClock()
                    behavior = Behavior(clock, random.Random(0))
                    behavior.configure(role_id=role, personality=style)
                    last = None
                    for _ in range(6):
                        self.assertTrue(behavior.trigger('head_pat'))
                        line = behavior.sample()['text']
                        self.assertNotEqual(line, last)
                        last = line
                        clock.advance(3)
                        behavior.sample()
                    previous_kind = previous_line = None
                    for _ in range(12):
                        clock.advance(behavior._next_idle - clock())
                        sample = behavior.sample()
                        self.assertNotEqual(sample['kind'], previous_kind)
                        self.assertNotEqual(sample['text'], previous_line)
                        previous_kind, previous_line = sample['kind'], sample['text']
                        clock.advance(5)
                        behavior.sample()

    def start_motion(self, kind='head_pat', delay=1000):
        self.assertTrue(self.behavior.trigger(kind))
        sample = self.behavior.sample()
        self.assertTrue(self.behavior.bind_motion(sample['action_id']))
        self.assertTrue(self.behavior.motion_frame(sample['action_id'], delay))
        return sample['action_id'], sample['text']

    def test_short_clip_finishes_visual_immediately_but_text_keeps_reading_time(self):
        action, line = self.start_motion()
        self.clock.advance(.24)
        self.assertTrue(self.behavior.finish_motion(action))
        ended = self.behavior.sample()
        self.assertEqual((ended['kind'], ended['pose'], ended['action_id']), ('idle', 'idle', None))
        self.assert_still(ended)
        self.assertEqual(ended['text'], line)
        self.assertFalse(self.behavior.finish_motion(action))
        self.clock.advance(2.35)
        self.assertEqual(self.behavior.sample()['text'], line)
        self.clock.advance(.02)
        self.assertEqual(self.behavior.sample()['text'], '')

    def test_long_clip_outlives_rule_duration_without_extending_text(self):
        action, _ = self.start_motion('poke')
        for _ in range(12):
            self.clock.advance(.5)
            self.assertTrue(self.behavior.motion_frame(action, 500))
            sample = self.behavior.sample()
            self.assertEqual((sample['kind'], sample['action_id']), ('poke', action))
            self.assertTrue(0 <= sample['progress'] <= 1)
        self.assertEqual(sample['text'], '')
        self.assertTrue(self.behavior.finish_motion(action))
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertEqual(self.behavior.sample()['text'], '')

    def test_bound_same_gesture_does_not_restart_or_consume_other_gesture_cooldown(self):
        action, _ = self.start_motion()
        self.clock.advance(1.21)
        self.assertFalse(self.behavior.trigger('head_pat'))
        self.assertEqual(self.behavior.sample()['action_id'], action)
        self.assertTrue(self.behavior.trigger('poke'))
        replacement = self.behavior.sample()['action_id']
        self.assertGreater(replacement, action)
        self.assertTrue(self.behavior.bind_motion(replacement))
        self.assertFalse(self.behavior.finish_motion(action))
        self.assertFalse(self.behavior.motion_frame(action, 64000))
        self.assertFalse(self.behavior.bind_motion(action))
        self.assertEqual(self.behavior.sample()['action_id'], replacement)

    def test_drag_interrupts_bound_gesture_and_stale_callbacks_cannot_end_release(self):
        action, _ = self.start_motion()
        self.assertTrue(self.behavior.trigger('drag_start'))
        drag = self.behavior.sample()['action_id']
        self.assertGreater(drag, action)
        self.assertFalse(self.behavior.bind_motion(drag))
        self.assertTrue(self.behavior.trigger('drag_end'))
        released = self.behavior.sample()['action_id']
        self.assertGreater(released, drag)
        self.assertTrue(self.behavior.bind_motion(released))
        self.assertFalse(self.behavior.finish_motion(action))
        self.assertFalse(self.behavior.motion_frame(drag, 1000))
        self.assertEqual(self.behavior.sample()['kind'], 'drag_end')

    def test_initial_bind_expires_and_repeated_bind_cannot_keep_it_alive(self):
        self.behavior.trigger('head_pat')
        action = self.behavior.sample()['action_id']
        self.assertTrue(self.behavior.bind_motion(action))
        self.clock.advance(.8)
        self.assertTrue(self.behavior.bind_motion(action))
        self.clock.advance(.21)
        self.assertFalse(self.behavior.bind_motion(action))
        self.assertFalse(self.behavior.motion_frame(action, 2000))
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertTrue(self.behavior.sample()['text'])

    def test_frame_delay_lease_has_lower_and_upper_bounds(self):
        for delay, lease in ((-2000, 1), (0, 1), (200, 1.2), (10000000, 65)):
            with self.subTest(delay=delay):
                self.setUp()
                action, _ = self.start_motion(delay=delay)
                self.clock.advance(lease - .01)
                self.assertEqual(self.behavior.sample()['action_id'], action)
                self.clock.advance(.02)
                self.assertEqual(self.behavior.sample()['kind'], 'idle')
                self.assertFalse(self.behavior.motion_frame(action, 10000000))

    def test_invalid_frames_and_ids_do_not_create_or_extend_a_binding(self):
        self.behavior.trigger('head_pat')
        action = self.behavior.sample()['action_id']
        for invalid in (None, True, str(action), action + 99):
            self.assertFalse(self.behavior.bind_motion(invalid))
            self.assertFalse(self.behavior.finish_motion(invalid))
        self.assertFalse(self.behavior.motion_frame(action, 1000))
        self.assertFalse(self.behavior.finish_motion(action))
        self.assertTrue(self.behavior.bind_motion(action))
        self.clock.advance(.8)
        for delay in (None, 'broken', float('nan'), float('inf'), float('-inf')):
            self.assertFalse(self.behavior.motion_frame(action, delay))
        self.clock.advance(.21)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')

    def test_protected_states_cancel_motion_without_overriding_task_or_reviving(self):
        for state in PROTECTED:
            with self.subTest(state=state):
                self.setUp()
                action, _ = self.start_motion()
                self.clock.advance(.2)
                sample = self.behavior.sample(state)
                self.assertEqual(sample['pose'], 'error' if state in ('unknown', 'interrupted') else state)
                self.assertEqual((sample['kind'], sample['action_id'], sample['text']), ('idle', None, ''))
                self.assert_still(sample)
                self.assertFalse(self.behavior.motion_frame(action, 5000))
                self.assertFalse(self.behavior.finish_motion(action))
                self.assertEqual(self.behavior.sample()['kind'], 'idle')
                self.assertFalse(self.behavior.bind_motion(action))

    def test_hidden_and_reduced_motion_end_binding_without_reviving_old_clip(self):
        for kwargs in ({'visible': False}, {'reduced_motion': True}):
            with self.subTest(kwargs=kwargs):
                self.setUp()
                action, line = self.start_motion()
                sample = self.behavior.sample(**kwargs)
                self.assertEqual((sample['kind'], sample['action_id']), ('idle', None))
                self.assert_still(sample)
                self.assertEqual(sample['text'], '' if 'visible' in kwargs else line)
                self.assertFalse(self.behavior.motion_frame(action, 5000))
                self.assertEqual(self.behavior.sample()['kind'], 'idle')
                self.assertFalse(self.behavior.bind_motion(action))

    def test_no_new_binding_when_last_sample_is_protected_or_hidden(self):
        for kwargs in ({'base_state': 'working'}, {'visible': False}, {'reduced_motion': True}):
            with self.subTest(kwargs=kwargs):
                self.setUp()
                self.behavior.trigger('head_pat')
                action = self.behavior._action_id
                self.behavior.sample(**kwargs)
                self.assertFalse(self.behavior.bind_motion(action))

    def test_quiet_and_chatting_do_not_cancel_direct_motion_or_lingering_text(self):
        action, line = self.start_motion()
        self.behavior.quiet(30)
        self.assertEqual(self.behavior.sample(chatting=True)['action_id'], action)
        self.clock.advance(.3)
        self.assertTrue(self.behavior.finish_motion(action))
        self.behavior.quiet(30)
        self.assertEqual(self.behavior.sample(chatting=True)['text'], line)
        self.clock.advance(3)
        self.assertEqual(self.behavior.sample()['text'], '')

    def test_role_style_or_touch_change_discards_binding_and_lingering_line(self):
        for settings in ({'role_id': 'catgirl'}, {'personality': 'gentle'}, {'touch_enabled': False}):
            for finished in (False, True):
                with self.subTest(settings=settings, finished=finished):
                    self.setUp()
                    action, _ = self.start_motion()
                    self.behavior.quiet(30)
                    quiet_until = self.behavior.quiet_until
                    if finished:
                        self.assertTrue(self.behavior.finish_motion(action))
                    self.behavior.configure(**settings)
                    sample = self.behavior.sample()
                    self.assertEqual((sample['kind'], sample['text'], sample['action_id']), ('idle', '', None))
                    self.assertEqual(self.behavior.quiet_until, quiet_until)
                    self.assertFalse(self.behavior.motion_frame(action, 5000))
                    self.assertFalse(self.behavior.finish_motion(action))

    def test_ids_survive_role_changes_and_rule_only_actions_keep_original_timing(self):
        previous = 0
        for kind in ACTION_KINDS:
            self.clock.advance(50)
            if kind in MANUAL:
                self.assertTrue(self.behavior.trigger(kind))
            else:
                self.behavior = Behavior(self.clock, FirstChoice())
                self.behavior.rng.action = kind
                self.clock.advance(25)
                previous = 0
            sample = self.behavior.sample()
            action = sample['action_id']
            self.assertGreater(action, previous)
            previous = action
            if kind not in MOTION_KINDS:
                self.assertFalse(self.behavior.bind_motion(action))
            if kind == 'drag_start':
                self.clock.advance(500)
                self.assertEqual(self.behavior.sample()['action_id'], action)
            else:
                self.clock.advance(DURATIONS[kind] + .01)
                self.assertEqual(self.behavior.sample()['action_id'], None)
            self.behavior.configure(role_id='custom-' + kind)
            self.assertFalse(self.behavior.bind_motion(action))

    def test_explicit_motion_timestamps_do_not_depend_on_clock_advancing(self):
        self.behavior.trigger('wave', now=200)
        action = self.behavior.sample(now=200)['action_id']
        self.assertTrue(self.behavior.bind_motion(action, now=200))
        self.assertTrue(self.behavior.motion_frame(action, 5000, now=200))
        self.assertEqual(self.behavior.sample(now=204)['kind'], 'wave')
        self.assertEqual(self.behavior.sample(now=204)['text'], '')
        self.assertTrue(self.behavior.finish_motion(action, now=204))
        self.assertEqual(self.behavior.sample(now=204)['action_id'], None)


if __name__ == '__main__':
    unittest.main()
