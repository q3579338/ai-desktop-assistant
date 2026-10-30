"""Real offscreen QMovie playback hands finite gestures back to Behavior.

Most tests inject a Behavior clock; one long clip also uses real wall time.
GIF decoding, frame events and final timers are real; no reaction is invented.
"""
import copy
import json
from pathlib import Path
import random
import time
import unittest
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture, QApplication
from PIL import Image
from PySide6.QtGui import QMovie
from PySide6.QtTest import QTest

from starpet.behavior import Behavior, DURATIONS
from starpet.characters import character_spec, STATES
from starpet.rolepacks import RoleLibrary


class MotionClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MotionHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Motion tests require Qt offscreen; no foreground fallback')

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.c, self.pet = self.f.c, self.f.c.pet
        self.f.window.hide()
        self.clock = MotionClock()
        self.c.behavior = self.behavior = Behavior(self.clock, random.Random(0))
        self.assets = self.f.folder / 'motion-assets'
        self.colors = {'tabby': ('#bb2222', '#dd6600', '#eeaa11'),
                       'catgirl': ('#229933', '#22bb77', '#66ddaa'),
                       'robot': ('#2244bb', '#5577dd', '#99bbff')}
        for identity, colors in self.colors.items():
            folder = self.assets / 'characters' / identity
            folder.mkdir(parents=True)
            frames = [Image.new('RGB', (24, 24), color) for color in colors]
            frames[0].save(folder / 'image.png')
            frames[0].save(folder / 'idle.gif', save_all=True, append_images=frames[1:],
                           duration=60, loop=0, disposal=2)
            # Infinite source: metadata loop=False must still play exactly once.
            frames[0].save(folder / 'touch.gif', save_all=True, append_images=frames[1:],
                           duration=[40, 40, 160], loop=0, disposal=2)
            frames[0].save(folder / 'poke.gif', save_all=True, append_images=frames[1:],
                           duration=[100, 100, 180], loop=0, disposal=2)
            # Finite source: metadata loop=True must restart it after its end.
            frames[0].save(folder / 'finite.gif', save_all=True, append_images=frames[1:],
                           duration=60, disposal=2)
            clips = {state: {'file': 'idle.gif', 'loop': True} for state in STATES}
            clips['completed'] = {'file': 'touch.gif', 'loop': False}
            clips.update(head_pat={'file': 'touch.gif', 'loop': False},
                         poke={'file': 'poke.gif', 'loop': False},
                         drag_end={'file': 'touch.gif', 'loop': False})
            metadata = {'image': 'image.png', 'columns': 1, 'rows': 1,
                        'states': {state: 0 for state in STATES}, 'clips': clips}
            (folder / 'character.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.c.roles = RoleLibrary(self.f.state, self.assets)
        self.spec_patch = patch('starpet.gui.character_spec',
            side_effect=lambda identity: character_spec(identity, self.assets))
        self.spec_patch.start()
        self.c.companion_store.save_settings(useRoleAppearance=True, formMode='cat')
        self.c.configure_behavior()
        self.pet.load_image()
        self.pet.show()
        # QMovie timers remain real; only automatic painting is suppressed so
        # the test controls when Behavior samples its injected clock.
        self.pet.setUpdatesEnabled(False)
        self.render()
        self.original_memory = self.f.saved_memory()

    def tearDown(self):
        self.pet.clear_clip()
        self.spec_patch.stop()
        self.assertEqual(self.f.chat.calls, [])
        self.f.close()

    def render(self):
        view = self.pet.visual_state()
        image = self.pet.animation_image(view['state'], view['reaction'])
        return view, image

    def wait(self, predicate, timeout=.9):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Real QMovie did not reach the expected state within the bounded wait')

    def start(self, kind='head_pat'):
        self.assertTrue(self.behavior.trigger(kind))
        sample = self.behavior.sample()
        token = sample['action_id']
        self.assertIsNotNone(token)
        view, image = self.render()
        self.assertEqual(view['reaction']['kind'], kind)
        self.assertFalse(image.isNull())
        self.assertIsNotNone(self.pet.clip_movie)
        return token, self.pet.clip_movie, sample['text']

    @staticmethod
    def color(frame):
        image = frame.toImage()
        return image.pixelColor(image.width() // 2, image.height() // 2).name()

    def rewrite_clips(self, transform):
        path = self.assets / 'characters/tabby/character.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        transform(metadata)
        path.write_text(json.dumps(metadata), encoding='utf-8')
        self.pet.load_image()
        self.render()

    def assert_gone(self, old_token):
        # Check cancellation before sending stale callbacks. Finishing an
        # accidentally live old binding here must not mask a regression.
        sample = self.behavior.sample()
        self.assertEqual(sample['kind'], 'idle')
        self.assertIsNone(sample['action_id'])
        self.behavior.motion_frame(old_token, 500)
        self.behavior.finish_motion(old_token)
        view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'idle')
        self.assertIsNone(view['reaction']['action_id'])
        self.assertNotIn(self.pet.clip_key, ('head_pat', 'poke', 'wave', 'drag_end'))

    def test_short_gesture_holds_complete_last_frame_then_keeps_readable_text(self):
        token, movie, line = self.start()
        self.assertTrue(line)
        self.wait(lambda: movie.currentFrameNumber() == 2)
        self.assertEqual(self.color(movie.currentPixmap()), self.colors['tabby'][2])
        # Reaching the last frame is not the end of its display interval.
        QTest.qWait(45)
        self.assertEqual(self.behavior.sample()['action_id'], token)
        self.assertEqual(movie.currentFrameNumber(), 2)
        self.wait(lambda: self.behavior.sample()['kind'] == 'idle')
        sample = self.behavior.sample()
        self.assertIsNone(sample['action_id'])
        self.assertEqual(sample['text'], line)
        view, _ = self.render()
        self.assertEqual(self.pet.clip_key, 'idle')
        self.assertTrue(self.pet.bubble_visible(view))
        self.assertEqual(view['reaction']['text'], line)
        self.clock.advance(DURATIONS['head_pat'] + .1)
        self.assertEqual(self.behavior.sample()['text'], '')
        self.assertEqual(self.f.saved_memory(), self.original_memory)

    def test_wall_clock_clip_outlives_old_duration_and_finishes_at_real_last_frame(self):
        # Actual 2.1-second GIF exceeds poke's old 1.7-second timeout. Both
        # Behavior and QMovie use real time for this independent long-clip case.
        path = self.assets / 'characters/tabby/long.gif'
        frames = [Image.new('RGB', (24, 24), color) for color in self.colors['tabby']]
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       duration=700, loop=0, disposal=2)
        self.rewrite_clips(lambda metadata: metadata['clips'].update(
            poke={'file': 'long.gif', 'loop': False}))
        self.c.behavior = self.behavior = Behavior(time.monotonic, random.Random(0))
        self.c.configure_behavior()
        started = time.monotonic()
        token, movie, _ = self.start('poke')
        self.wait(lambda: time.monotonic() - started > DURATIONS['poke'] + .08,
                  timeout=2.1)
        view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'poke')
        self.assertEqual(view['reaction']['action_id'], token)
        self.assertIs(self.pet.clip_movie, movie)
        self.assertEqual(movie.currentFrameNumber(), 2)
        self.assertEqual(self.color(movie.currentPixmap()), self.colors['tabby'][2])
        self.wait(lambda: self.behavior.sample()['kind'] == 'idle')
        self.assertGreater(time.monotonic() - started, 2.0)
        self.render()
        self.assertEqual(self.pet.clip_key, 'idle')

    def test_same_gesture_does_not_restart_or_consume_other_gesture_cooldown(self):
        token, movie, _ = self.start()
        # Advance in sub-lease increments, letting an actual next frame renew
        # the binding before crossing the normal 1.2-second gesture cooldown.
        self.clock.advance(.7)
        self.wait(lambda: movie.currentFrameNumber() == 1)
        self.clock.advance(.51)
        self.assertFalse(self.behavior.trigger('head_pat'))
        view, _ = self.render()
        self.assertEqual(view['reaction']['action_id'], token)
        self.assertIs(self.pet.clip_movie, movie)
        self.assertGreater(movie.currentFrameNumber(), 0)
        self.assertTrue(self.behavior.trigger('poke'))
        view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'poke')
        self.assertGreater(view['reaction']['action_id'], token)
        self.assertIsNot(self.pet.clip_movie, movie)
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)

    def test_old_final_frame_callback_cannot_finish_replacement_gesture(self):
        old_token, old_movie, _ = self.start()
        self.clock.advance(.65)
        self.wait(lambda: old_movie.currentFrameNumber() == 1)
        self.clock.advance(.56)
        self.wait(lambda: old_movie.currentFrameNumber() == 2)
        self.assertTrue(self.behavior.trigger('poke'))
        view, _ = self.render()
        new_token = view['reaction']['action_id']
        new_movie = self.pet.clip_movie
        self.assertNotEqual(new_token, old_token)
        # Also exercise stale delivery explicitly before deferred deletion.
        old_movie.frameChanged.emit(2)
        self.behavior.motion_frame(old_token, 1000)
        self.behavior.finish_motion(old_token)
        QTest.qWait(185)  # Old final-frame timer has expired; poke is still active.
        view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'poke')
        self.assertEqual(view['reaction']['action_id'], new_token)
        self.assertIs(self.pet.clip_movie, new_movie)
        self.wait(lambda: self.behavior.sample()['kind'] == 'idle')

    def test_switch_role_drops_old_gesture_and_its_pending_callback(self):
        token, movie, _ = self.start()
        self.wait(lambda: movie.currentFrameNumber() == 2)
        self.c.switch_role('robot')
        self.render()
        self.assert_gone(token)
        QTest.qWait(190)
        self.assert_gone(token)
        _, frame = self.render()
        self.assertIn(self.color(frame), self.colors['robot'])

    def test_catgirl_form_switch_does_not_restart_old_gesture_on_new_body(self):
        self.c.companion_store.save_settings(formMode='chat')
        token, movie, _ = self.start()
        self.wait(lambda: movie.currentFrameNumber() == 2)
        self.f.window.show()
        self.render()
        self.assertTrue(self.pet.uses_catgirl_form())
        self.assert_gone(token)
        QTest.qWait(190)
        self.f.window.hide()
        self.render()
        self.assert_gone(token)

    def test_hidden_pet_does_not_resume_old_gesture_when_shown(self):
        token, _, _ = self.start()
        self.pet.hide()
        self.pet.animate()
        self.render()
        QTest.qWait(270)
        self.pet.show()
        self.assert_gone(token)

    def test_reduced_motion_does_not_resume_old_gesture_when_restored(self):
        token, _, _ = self.start()
        self.c.prefs.values['reducedMotion'] = True
        self.render()
        QTest.qWait(270)
        self.c.prefs.values['reducedMotion'] = False
        self.assert_gone(token)

    def test_real_task_priority_cancels_motion_without_changing_task_state(self):
        token, _, _ = self.start()
        self.c.core.ingest('workbuddy', 'motion-handoff-fixture', 'working', 'Fixture')
        before = copy.deepcopy(self.c.core.snapshot())
        before.pop('at')
        view, _ = self.render()
        self.assertEqual(view['state'], 'working')
        self.assertEqual(view['reaction']['pose'], 'working')
        self.assertEqual(view['reaction']['text'], '')
        self.assertEqual(self.pet.clip_key, 'working')
        QTest.qWait(270)
        after = self.c.core.snapshot()
        after.pop('at')
        self.assertEqual(after, before)
        self.c.core.ingest('workbuddy', 'motion-handoff-fixture', 'idle', 'Fixture')
        self.assert_gone(token)

    def test_static_image_without_motion_keeps_original_behavior_duration(self):
        self.rewrite_clips(lambda metadata: metadata.pop('clips'))
        self.assertTrue(self.behavior.trigger('head_pat'))
        view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'head_pat')
        self.assertIsNone(self.pet.clip_movie)
        self.clock.advance(DURATIONS['head_pat'] - .01)
        self.assertEqual(self.behavior.sample()['kind'], 'head_pat')
        self.clock.advance(.02)
        self.assertEqual(self.behavior.sample()['kind'], 'idle')
        self.assertEqual(self.behavior.sample()['text'], '')

    def test_wave_uses_nonloop_completed_fallback_and_finishes(self):
        # No dedicated wave clip exists in this synthetic role.
        _, movie, line = self.start('wave')
        self.assertEqual(self.pet.clip_key, 'completed')
        self.wait(lambda: movie.currentFrameNumber() == 2)
        self.wait(lambda: self.behavior.sample()['kind'] == 'idle')
        self.assertEqual(self.behavior.sample()['text'], line)
        self.render()
        self.assertEqual(self.pet.clip_key, 'idle')

    def test_missing_poke_clip_does_not_bind_nonloop_idle_fallback(self):
        def no_poke(metadata):
            metadata['clips'].pop('poke')
            metadata['clips']['idle']['loop'] = False
        self.rewrite_clips(no_poke)
        with patch.object(self.behavior, 'bind_motion', wraps=self.behavior.bind_motion) as bind:
            token, movie, _ = self.start('poke')
            self.assertEqual(self.pet.clip_key, 'idle')
            self.assertIsNone(self.pet.clip_action_id)
            self.wait(lambda: movie.currentFrameNumber() == 2)
            QTest.qWait(100)
            self.assertEqual(self.behavior.sample()['action_id'], token)
            self.clock.advance(DURATIONS['poke'] - .01)
            view, _ = self.render()
            self.assertEqual(view['reaction']['kind'], 'poke')
            self.clock.advance(.02)
            view, _ = self.render()
            self.assertEqual(view['reaction']['kind'], 'idle')
            self.assertEqual(view['reaction']['text'], '')
            bind.assert_not_called()

    def test_real_completed_task_keeps_nonloop_last_frame_without_finishing_a_gesture(self):
        self.c.core.ingest('workbuddy', 'motion-completed-fixture', 'completed', 'Fixture')
        with patch.object(self.behavior, 'finish_motion', wraps=self.behavior.finish_motion) as finish:
            view, _ = self.render()
            movie = self.pet.clip_movie
            self.assertEqual(view['state'], 'completed')
            self.assertEqual(view['reaction']['kind'], 'idle')
            self.assertIsNone(view['reaction']['action_id'])
            self.wait(lambda: movie.currentFrameNumber() == 2)
            QTest.qWait(200)
            view, _ = self.render()
            self.assertEqual(view['state'], 'completed')
            self.assertEqual(movie.currentFrameNumber(), 2)
            self.assertEqual(movie.state(), QMovie.MovieState.Paused)
            finish.assert_not_called()

    def test_loop_metadata_repeats_finite_source_and_keeps_duration_semantics(self):
        self.rewrite_clips(lambda metadata: metadata['clips'].update(
            head_pat={'file': 'finite.gif', 'loop': True}))
        with patch.object(self.behavior, 'bind_motion', wraps=self.behavior.bind_motion) as bind:
            _, movie, _ = self.start()
            seen = [movie.currentFrameNumber()]
            movie.frameChanged.connect(seen.append)
            self.wait(lambda: seen.count(0) >= 3, timeout=1.1)
            self.assertEqual(self.behavior.sample()['kind'], 'head_pat')
            bind.assert_not_called()
            self.clock.advance(DURATIONS['head_pat'] + .01)
            view, _ = self.render()
        self.assertEqual(view['reaction']['kind'], 'idle')
        self.assertEqual(self.pet.clip_key, 'idle')


if __name__ == '__main__':
    unittest.main()
