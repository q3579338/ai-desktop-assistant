"""Role identity stays aligned with appearance across partial writes/restarts."""
import hashlib
import os
from pathlib import Path
import queue
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'.vendor')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from starpet.core import Core
from starpet.gui import Controller
from starpet.storage import Preferences,atomic_json,read_json
from starpet.characters import character_spec


class RoleAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        assert cls.app.platformName()=='offscreen'

    def setUp(self):
        self.folder=ROOT/'evidence/role-appearance-tests'/uuid.uuid4().hex
        self.controllers=[]

    def controller(self,state=None):
        state=state or self.folder/'state'
        c=Controller(self.app,Core(),Preferences(state),SimpleNamespace(actions=queue.Queue()),
                     SimpleNamespace(bytes_read=0,errors=0),state,self.folder/'profile',tray=False)
        c.action_timer.stop();c.panel.timer.stop();c.pet.timer.stop()
        self.controllers.append(c)
        return c

    def tearDown(self):
        for c in self.controllers:
            if c.pet.clip_movie:c.pet.clip_movie.stop()
            c.pet.hide();c.panel.hide();c.completion_popup.hide()
            if c.companion:c.companion.poller.stop();c.companion.hide()

    @staticmethod
    def digest(c):
        frame=c.pet.character_image('idle')
        return hashlib.sha256(bytes(frame.toImage().constBits())).hexdigest() if frame else None

    def test_partial_compatibility_write_cannot_restore_wrong_role_on_restart(self):
        c=self.controller();c.switch_role('robot');robot=self.digest(c)
        def fail_preferences(path,value):
            if Path(path)==c.state/'preferences.json':raise OSError('fixture write failure')
            return atomic_json(path,value)
        with patch('starpet.storage.atomic_json',side_effect=fail_preferences):
            self.assertTrue(c.switch_role('tabby'))
        expected=self.digest(c)
        self.assertNotEqual(expected,robot)
        restarted=self.controller(c.state)
        self.assertEqual(self.digest(restarted),expected)
        self.assertEqual(restarted.companion_store.settings['roleId'],'tabby')
        self.assertEqual(restarted.companion_store.memory_path.name,'companion-memory.json')
        self.assertEqual(read_json(c.state/'preferences.json')['character'],'robot')

    def test_legacy_appearance_buttons_work_without_changing_role_memory(self):
        c=self.controller();c.switch_role('robot');robot=self.digest(c)
        c.companion_store.add_fact('ROBOT_FIXTURE_ONLY')
        c.panel.use_cat()
        self.assertNotEqual(self.digest(c),robot)
        self.assertFalse(c.companion_store.settings['useRoleAppearance'])
        self.assertEqual(c.companion_store.settings['roleId'],'robot')
        self.assertIn('保留',c.panel.feedback.text())
        c.panel.reset_image()
        self.assertIsNone(self.digest(c))  # Original whale is drawn by code.
        self.assertEqual(c.companion_store.facts[0]['text'],'ROBOT_FIXTURE_ONLY')
        c.switch_role('robot')
        self.assertTrue(c.companion_store.settings['useRoleAppearance'])
        self.assertEqual(self.digest(c),robot)

    def test_existing_custom_appearance_survives_upgrade_until_role_is_selected(self):
        state=self.folder/'state'
        Preferences(state).save(customImage=str(ROOT/'assets/starpet.png'),character='whale')
        c=self.controller(state)
        self.assertFalse(c.companion_store.settings['useRoleAppearance'])
        self.assertIsNotNone(c.pet.image)
        original=self.digest(c)
        c.switch_role('tabby')
        self.assertTrue(c.companion_store.settings['useRoleAppearance'])
        self.assertNotEqual(self.digest(c),original)

    def check_chat_form(self,girl_spec):
        c=self.controller();c.switch_role('tabby')
        c.companion_store.save_settings(formMode='chat')
        c.pet.show();c.pet.setUpdatesEnabled(False)
        self.assertFalse(c.pet.uses_catgirl_form())
        c.pet.animation_image('idle',{'kind':'idle','pose':'idle','progress':0})
        self.assertIsNotNone(c.pet.clip_movie)
        c.companion=SimpleNamespace(isVisible=lambda:True,poller=SimpleNamespace(stop=lambda:None),hide=lambda:None)
        self.assertTrue(c.pet.uses_catgirl_form())
        clips=girl_spec.get('clips',{})
        if clips:
            selected='idle' if 'idle' in clips else next(iter(clips))
            pose=selected if selected in girl_spec['states'] else 'idle'
            frame=c.pet.animation_image(pose,{'kind':selected,'pose':pose,'progress':0})
            movie=c.pet.clip_movie
            self.assertIsNotNone(movie)
            self.assertEqual(Path(movie.fileName()).resolve(),Path(clips[selected]['file']).resolve())
            self.assertEqual(c.pet.clip_key,selected)
            changed=[movie.currentFrameNumber()];movie.frameChanged.connect(changed.append)
            elapsed=QElapsedTimer();elapsed.start()
            try:
                while len(set(changed))<2 and elapsed.elapsed()<1500:QTest.qWait(20)
            finally:movie.frameChanged.disconnect(changed.append)
            self.assertGreaterEqual(len(set(changed)),2,'Selected catgirl clip did not advance')
        else:
            frame=c.pet.animation_image('idle',{'kind':'idle','pose':'idle','progress':0})
            self.assertIsNone(c.pet.clip_movie)
            index=girl_spec['states']['idle']
            self.assertEqual(frame.cacheKey(),c.pet.extra_atlases['catgirl'][0][index].cacheKey())
        self.assertFalse(frame.isNull())
        self.assertEqual(c.companion_store.settings['roleId'],'tabby')
        c.companion.isVisible=lambda:False
        self.assertFalse(c.pet.uses_catgirl_form())
        c.pet.animation_image('idle',{'kind':'idle','pose':'idle','progress':0})
        self.assertIsNotNone(c.pet.clip_movie)
        self.assertEqual(Path(c.pet.clip_movie.fileName()).resolve(),Path(character_spec('tabby')['clips']['idle']['file']).resolve())

    def test_animated_tabby_uses_available_bundled_catgirl_form(self):
        self.check_chat_form(character_spec('catgirl'))

    def test_static_catgirl_fallback_remains_covered_when_bundled_role_gains_clips(self):
        girl={key:value for key,value in character_spec('catgirl').items() if key!='clips'}
        with patch('starpet.gui.character_spec',side_effect=lambda identity:girl if identity=='catgirl' else character_spec(identity)):
            self.check_chat_form(girl)

    def test_chat_form_animated_fixture_selects_and_advances_its_own_clip(self):
        from PIL import Image
        self.folder.mkdir(parents=True)
        path=self.folder/'catgirl-fixture.gif'
        frames=[Image.new('RGBA',(32,32),color) for color in ('#aa3355','#33aa55','#3355aa')]
        frames[0].save(path,save_all=True,append_images=frames[1:],duration=80,loop=0,disposal=2)
        girl={**character_spec('catgirl'),'clips':{'idle':{'file':path,'loop':True}}}
        with patch('starpet.gui.character_spec',side_effect=lambda identity:girl if identity=='catgirl' else character_spec(identity)):
            self.check_chat_form(girl)

    def test_robot_does_not_turn_into_catgirl_when_chat_opens(self):
        c=self.controller();c.switch_role('robot');c.companion_store.save_settings(formMode='chat')
        c.companion=SimpleNamespace(isVisible=lambda:True,poller=SimpleNamespace(stop=lambda:None),hide=lambda:None)
        self.assertFalse(c.pet.uses_catgirl_form())


if __name__=='__main__':unittest.main()
