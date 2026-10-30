"""Isolated synthetic animation playback and form-switch regressions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor'), str(ROOT / 'tests')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PIL import Image
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, Qt
from PySide6.QtGui import QMovie, QPainter, QPixmap, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_companion_context_ui import ContextUIFixture
from starpet.characters import character_spec, STATES
from starpet.rolepacks import RoleLibrary


class AnimationFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Animation tests require Qt offscreen')
        cls.evidence = ROOT / 'evidence' / ('animation-forms-' + uuid.uuid4().hex[:10])
        cls.evidence.mkdir(parents=True)

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.c, self.pet = self.f.c, self.f.c.pet
        self.f.window.hide()
        self.assets = self.f.folder / 'synthetic-assets'
        self.colors = {'tabby': ('#e02020', '#b01010', '#ff8040'),
                       'catgirl': ('#20d050', '#108030', '#70ef90'),
                       'robot': ('#2070e0', '#104090', '#70a0ff')}
        for identity, colors in self.colors.items():
            folder = self.assets / 'characters' / identity
            (folder / 'clips').mkdir(parents=True)
            frames = []
            for color in colors:
                image = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
                image.paste(Image.new('RGBA', (28, 28), color), (2, 2))
                frames.append(image)
            frames[0].save(folder / 'image.png')
            frames[0].save(folder / 'clips/loop.gif', save_all=True, append_images=frames[1:],
                           duration=80, loop=0, disposal=2)
            frames[0].save(folder / 'clips/finite.gif', save_all=True, append_images=frames[1:],
                           duration=80, loop=1, disposal=2)
            clips = {state: {'file': 'clips/loop.gif', 'loop': state != 'completed'} for state in STATES}
            clips['head_pat'] = {'file': 'clips/loop.gif', 'loop': False}
            clips['wave'] = {'file': 'clips/finite.gif', 'loop': True}
            metadata = {'image': 'image.png', 'columns': 1, 'rows': 1,
                        'states': {state: 0 for state in STATES}, 'clips': clips,
                        'source': 'synthetic-' + identity, 'generation_prompt': 'test fixture only'}
            (folder / 'character.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.c.roles = RoleLibrary(self.f.state, self.assets)
        self.patcher = patch('starpet.gui.character_spec', side_effect=lambda name: character_spec(name, self.assets))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.c.companion_store.save_settings(roleId='tabby', useRoleAppearance=True, formMode='chat')
        self.pet.load_image()
        self.pet.show()
        self.pet.setUpdatesEnabled(False)

    def tearDown(self):
        self.pet.hide()
        if self.pet.clip_movie:
            self.pet.clip_movie.stop()
            self.pet.clip_movie.setFileName('')
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.f.close()

    @staticmethod
    def reaction(pose='idle', kind='idle', progress=0):
        return {'pose': pose, 'kind': kind, 'progress': progress}

    def frame(self, state='idle', kind='idle', progress=0):
        return self.pet.animation_image(state, self.reaction(state, kind, progress))

    def color(self, frame):
        image = frame.toImage()
        return image.pixelColor(image.width() // 2, image.height() // 2).name()

    def test_chat_form_uses_its_own_clips_and_returns_to_pet_frame_zero(self):
        self.frame()
        self.assertIsNotNone(self.pet.clip_movie)
        self.assertTrue(self.pet.clip_movie.jumpToFrame(1))
        old_movie = self.pet.clip_movie
        self.f.window.show()
        girl = self.frame()
        self.assertTrue(self.pet.uses_catgirl_form())
        self.assertIsNotNone(self.pet.clip_movie)
        self.assertIsNot(self.pet.clip_movie, old_movie)
        self.assertIn('catgirl', self.pet.clip_movie.fileName())
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)
        self.assertEqual(self.color(girl), self.colors['catgirl'][0])
        self.pet.clip_movie.jumpToFrame(1)
        girl_movie = self.pet.clip_movie
        self.f.window.hide()
        cat = self.frame()
        self.assertIsNot(self.pet.clip_movie, girl_movie)
        self.assertIn('tabby', self.pet.clip_movie.fileName())
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)
        self.assertEqual(self.color(cat), self.colors['tabby'][0])

    def test_direct_catgirl_role_plays_own_clips_without_form_switch(self):
        self.c.switch_role('catgirl')
        self.c.companion_store.save_settings(formMode='chat')
        self.f.window.show()
        frame = self.frame()
        self.assertFalse(self.pet.uses_catgirl_form())
        self.assertEqual(self.color(frame), self.colors['catgirl'][0])
        self.assertIn('catgirl', self.pet.clip_movie.fileName())

    def test_static_form_first_paint_does_not_flash_previous_animation_frame(self):
        metadata = self.assets / 'characters/catgirl/character.json'
        value = json.loads(metadata.read_text(encoding='utf-8'))
        value.pop('clips')
        metadata.write_text(json.dumps(value), encoding='utf-8')
        self.pet.load_image()
        self.pet.setUpdatesEnabled(True)
        with patch('starpet.gui.time.monotonic', return_value=100):
            self.pet.grab().save(str(self.evidence / 'before-static-form.png'))
            self.f.window.show()
            first = self.pet.grab()
            first.save(str(self.evidence / 'first-static-form.png'))
            self.assertIsNone(self.pet.clip_movie)
            self.assertEqual(first.toImage().pixelColor(125, 176).name(), self.colors['catgirl'][0])
            self.assertIsNone(self.pet.previous_frame)

    def test_role_change_and_state_change_start_new_movie_from_zero(self):
        self.frame('idle')
        old = self.pet.clip_movie
        old.jumpToFrame(1)
        self.frame('working')
        self.assertIsNot(self.pet.clip_movie, old)
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)
        self.pet.clip_movie.jumpToFrame(1)
        self.c.switch_role('robot')
        self.assertIsNone(self.pet.previous_frame)
        self.assertIsNone(self.pet.current_frame)
        frame = self.frame('working')
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)
        self.assertEqual(self.color(frame), self.colors['robot'][0])

    def test_nonloop_stops_at_last_frame_until_state_changes(self):
        self.frame('completed')
        movie = self.pet.clip_movie
        self.assertTrue(movie.jumpToFrame(1))
        self.assertTrue(movie.jumpToFrame(2))
        self.assertEqual(movie.state(), QMovie.MovieState.Paused)
        QTest.qWait(300)
        self.assertEqual(movie.currentFrameNumber(), 2)
        self.c.prefs.values['reducedMotion'] = True
        self.frame('completed')
        self.c.prefs.values['reducedMotion'] = False
        self.frame('completed')
        self.assertEqual(movie.currentFrameNumber(), 2)
        self.frame('idle')
        self.frame('completed')
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)

    def test_reduced_motion_and_hidden_resume_current_frame_without_advance(self):
        self.frame()
        movie = self.pet.clip_movie
        self.assertTrue(movie.jumpToFrame(1))
        self.c.prefs.values['reducedMotion'] = True
        self.frame()
        self.assertEqual(movie.state(), QMovie.MovieState.Paused)
        QTest.qWait(200)
        self.assertEqual(movie.currentFrameNumber(), 1)
        self.c.prefs.values['reducedMotion'] = False
        self.frame()
        self.assertIs(self.pet.clip_movie, movie)
        self.assertEqual(movie.state(), QMovie.MovieState.Running)
        self.pet.hide()
        self.pet.animate()
        QTest.qWait(200)
        self.assertEqual(movie.currentFrameNumber(), 1)
        self.pet.show()
        self.frame()
        self.assertIs(self.pet.clip_movie, movie)
        self.assertEqual(movie.currentFrameNumber(), 1)
        self.assertEqual(movie.state(), QMovie.MovieState.Running)

    def test_repeated_manual_clip_restarts_and_loop_metadata_overrides_file_end(self):
        self.frame('idle', 'head_pat', .7)
        movie = self.pet.clip_movie
        movie.jumpToFrame(1)
        movie.jumpToFrame(2)
        self.frame('idle', 'head_pat', 0)
        self.assertIsNot(self.pet.clip_movie, movie)
        self.assertEqual(self.pet.clip_movie.currentFrameNumber(), 0)
        self.frame('idle', 'wave')
        movie = self.pet.clip_movie
        seen = []
        movie.frameChanged.connect(seen.append)
        elapsed=QElapsedTimer();elapsed.start()
        while seen.count(0)<2 and elapsed.elapsed()<1600:
            QTest.qWait(20)
        self.assertIs(self.pet.clip_movie, movie)
        self.assertEqual(movie.state(), QMovie.MovieState.Running)
        self.assertGreaterEqual(seen.count(0), 2)

    def test_protected_state_uses_task_clip_and_never_changes_core(self):
        self.c.core.ingest('workbuddy', 'animation-fixture', 'working', 'Fixture')
        before = copy.deepcopy(self.c.core.snapshot())
        before.pop('at')
        self.c.behavior.trigger('head_pat')
        view = self.pet.visual_state()
        self.assertEqual(view['reaction']['pose'], 'working')
        self.assertEqual(view['reaction']['text'], '')
        self.pet.animation_image(view['state'], view['reaction'])
        self.assertEqual(self.pet.clip_key, 'working')
        after = self.c.core.snapshot()
        after.pop('at')
        self.assertEqual(after, before)

    def test_export_uses_selected_role_metadata_and_preserves_clip_bytes(self):
        self.f.window.show()  # Displaying catgirl must not change tabby's export identity.
        for identity in ('tabby', 'catgirl'):
            with self.subTest(identity=identity):
                path = self.c.roles.export_package(identity, self.f.folder / (identity + '.aipet'))
                folder = self.assets / 'characters' / identity
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read('role.json'))
                    self.assertEqual(manifest['source']['kind'], 'synthetic-' + identity)
                    self.assertEqual(manifest['source']['sha256'], hashlib.sha256((folder / 'image.png').read_bytes()).hexdigest())
                    self.assertFalse(manifest['clips']['completed']['loop'])
                    self.assertTrue(manifest['clips']['idle']['loop'])
                    self.assertEqual(archive.read('clips/loop.gif'), (folder / 'clips/loop.gif').read_bytes())
                imported = self.c.roles.import_package(path)
                restored = self.c.roles.spec(imported)
                self.assertEqual(restored['clips']['idle']['file'].read_bytes(), (folder / 'clips/loop.gif').read_bytes())

    def test_opaque_atlas_is_real_artwork_and_static_robot_keeps_its_own_body(self):
        folder=self.assets/'characters/robot'
        Image.new('RGB',(32,32),self.colors['robot'][0]).save(folder/'image.png')
        value=json.loads((folder/'character.json').read_text(encoding='utf-8'))
        value.pop('clips')
        (folder/'character.json').write_text(json.dumps(value),encoding='utf-8')
        self.c.switch_role('robot')
        self.c.companion_store.save_settings(formMode='chat')
        self.f.window.show()
        self.assertFalse(self.pet.uses_catgirl_form())
        frame=self.frame()
        self.assertFalse(frame.isNull())
        self.assertEqual(self.color(frame),self.colors['robot'][0])
        self.assertIsNone(self.pet.clip_movie)

    def test_transparent_atlas_retains_one_shared_crop_for_different_poses(self):
        source=self.f.folder/'two-pose-atlas.png'
        image=Image.new('RGBA',(64,32),(0,0,0,0))
        image.paste(Image.new('RGBA',(8,8),'#e02020'),(2,2))
        image.paste(Image.new('RGBA',(8,8),'#20d050'),(54,20))
        image.save(source)
        _,frames=self.pet.atlas_images({'image':source,'columns':2,'rows':1})
        self.assertEqual(len(frames),2)
        self.assertEqual(frames[0].size(),frames[1].size())
        self.assertEqual((frames[0].width(),frames[0].height()),(32,31))
        self.assertEqual(frames[0].toImage().pixelColor(3,3).name(),'#e02020')
        self.assertEqual(frames[1].toImage().pixelColor(3,3).alpha(),0)
        self.assertEqual(frames[1].toImage().pixelColor(23,21).name(),'#20d050')

    def test_imported_partial_clips_keep_static_fallback_canvas_scale_and_anchor(self):
        source=self.f.folder/'partial-clips';source.mkdir()
        frames=[]
        for color in ('#e02020','#20d050'):
            image=Image.new('RGBA',(32,32),(0,0,0,0))
            image.paste(Image.new('RGBA',(12,12),color),(10,10))
            frames.append(image)
        atlas=Image.new('RGBA',(64,32),(0,0,0,0))
        atlas.paste(frames[0],(0,0));atlas.paste(frames[1],(32,0))
        atlas.save(source/'atlas.png')
        frames[0].save(source/'idle.gif',save_all=True,append_images=[frames[1]],duration=80,loop=0,disposal=2)
        manifest={'schema':1,'name':'Partial clip canvas fixture','image':'atlas.png',
                  'atlas':{'columns':2,'rows':1,'states':{state:0 if state=='idle' else 1 for state in STATES}},
                  'clips':{'idle':{'file':'idle.gif','loop':True}}}
        package=source/'partial.aipet'
        with zipfile.ZipFile(package,'x') as archive:
            archive.writestr('role.json',json.dumps(manifest))
            archive.write(source/'atlas.png','atlas.png');archive.write(source/'idle.gif','idle.gif')
        identity=self.c.roles.import_package(package)
        self.c.switch_role(identity)
        animated=self.frame('idle')
        self.assertIsNotNone(self.pet.clip_movie)
        fallback=self.frame('working')
        self.assertIsNone(self.pet.clip_movie)
        self.assertEqual((animated.width(),animated.height()),(32,32))
        self.assertEqual(fallback.size(),animated.size())
        self.assertEqual(QRegion(fallback.mask()).boundingRect(),QRegion(animated.mask()).boundingRect())

        def painted_bounds(frame):
            target=QPixmap(250,160);target.fill(Qt.GlobalColor.transparent)
            painter=QPainter(target);painter.translate(125,80)
            self.pet.draw_frame(painter,frame,1);painter.end()
            return QRegion(target.mask()).boundingRect()
        self.assertEqual(painted_bounds(animated),painted_bounds(fallback))
        self.assertEqual(self.color(animated),'#e02020')
        self.assertEqual(self.color(fallback),'#20d050')

    def test_custom_role_and_legacy_custom_image_keep_priority_over_form_setting(self):
        image=self.assets/'characters/robot/image.png'
        identity=self.c.roles.import_image(image,'合成自定义角色')
        self.c.switch_role(identity)
        self.c.companion_store.save_settings(formMode='catgirl')
        self.f.window.show()
        self.assertFalse(self.pet.uses_catgirl_form())
        self.assertEqual(self.color(self.frame()),self.colors['robot'][0])
        self.assertIsNone(self.pet.clip_movie)
        self.c.companion_store.save_settings(useRoleAppearance=False)
        self.c.prefs.save(customImage=str(image),character='tabby')
        self.pet.load_image()
        self.assertFalse(self.pet.uses_catgirl_form())
        self.assertEqual(self.color(self.frame()),self.colors['robot'][0])

    def test_finite_loop_finished_while_hidden_can_resume_and_old_file_is_released(self):
        self.frame('idle','wave')
        movie=self.pet.clip_movie
        movie.setSpeed(1000)
        self.pet.hide()
        # The renderer's timer is disabled in this fixture: reproduce the
        # narrow finish-before-hidden-pause window with a real QMovie finish.
        elapsed=QElapsedTimer();elapsed.start()
        while movie.state()!=QMovie.MovieState.NotRunning and elapsed.elapsed()<1000:
            QTest.qWait(10)
        self.assertEqual(movie.state(),QMovie.MovieState.NotRunning)
        self.pet.show()
        self.frame('idle','wave')
        self.assertEqual(movie.state(),QMovie.MovieState.Running)
        self.assertEqual(movie.currentFrameNumber(),0)
        old_path=Path(movie.fileName())
        self.frame('working')
        self.assertEqual(movie.fileName(),'')
        renamed=old_path.with_name('released.gif')
        old_path.rename(renamed)  # Must work on Windows before deferred deletion.
        renamed.rename(old_path)


if __name__ == '__main__':
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AnimationFormTests))
    report = {'passed': outcome.wasSuccessful(), 'testsRun': outcome.testsRun, 'failures': len(outcome.failures),
              'errors': len(outcome.errors), 'platform': 'offscreen', 'assets': 'isolated synthetic GIFs/PNG only',
              'productionAssetsModified': False, 'realModelCalled': False,
              'guiSha256': hashlib.sha256((ROOT / 'starpet/gui.py').read_bytes()).hexdigest(),
              'failureDetails': [detail for _, detail in outcome.failures + outcome.errors]}
    (AnimationFormTests.evidence / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence': str(AnimationFormTests.evidence), **report}, ensure_ascii=False))
    sys.exit(0 if outcome.wasSuccessful() else 1)
