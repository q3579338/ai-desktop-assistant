"""Role motion previews are visible, controllable and never activate a role."""
import json,unittest,zipfile
from pathlib import Path

from test_companion_context_ui import ContextUIFixture,QApplication,ROOT
from PySide6.QtCore import Qt
from PySide6.QtGui import QMovie
from PySide6.QtTest import QTest
from PIL import Image


class RolePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        assert cls.app.platformName()=='offscreen'

    def setUp(self):
        self.f=ContextUIFixture(self.app)
        self.w=self.f.window;self.gallery=self.w.workshop
        self.w.tabs.setCurrentIndex(3);self.app.processEvents()

    def tearDown(self):
        self.gallery.stop_preview();self.f.close()

    def select(self,identity):
        for index in range(self.gallery.roles.count()):
            item=self.gallery.roles.item(index)
            if item.data(Qt.ItemDataRole.UserRole)==identity:
                self.gallery.roles.setCurrentItem(item);self.app.processEvents();return
        raise AssertionError('Fixture role missing')

    def state_bytes(self):
        return {str(p.relative_to(self.f.state)):p.read_bytes() for p in self.f.state.rglob('*') if p.is_file()}

    def fixture_pack(self,loop=False,count=3):
        images=[Image.new('RGBA',(48,48),color) for color in ('#AA3355','#33AA55','#3355AA')[:count]]
        png=self.f.folder/'still.png';gif=self.f.folder/'motion.gif'
        images[0].save(png);images[0].save(gif,save_all=True,append_images=images[1:],duration=80,loop=0,disposal=2)
        manifest={'schema':1,'name':'隔离动作','image':'still.png','personality':'gentle','formMode':'cat',
                  'clips':{'idle':{'file':'motion.gif','loop':loop},'poke':{'file':'motion.gif','loop':loop}}}
        path=self.f.folder/'motion.aipet'
        with zipfile.ZipFile(path,'x') as archive:
            archive.writestr('role.json',json.dumps(manifest));archive.write(png,'still.png');archive.write(gif,'motion.gif')
        identity=self.f.c.roles.import_package(path)
        self.gallery.refresh(identity);self.app.processEvents();return identity

    def test_real_builtin_motion_advances_without_changing_role_or_any_saved_data(self):
        before=self.state_bytes();role=self.f.store.settings['roleId']
        self.select('robot')
        movie=self.gallery.preview_movie
        self.assertIsNotNone(movie)
        self.assertEqual(self.gallery.actions.currentData(),'idle')
        self.assertEqual(self.gallery.actions.count(),11)
        frames=[];movie.frameChanged.connect(frames.append)
        QTest.qWait(160)
        self.assertGreaterEqual(len(set(frames)),2)
        self.gallery.actions.setCurrentIndex(self.gallery.actions.findData('working'));QTest.qWait(80)
        self.assertEqual(self.f.store.settings['roleId'],role)
        self.assertEqual(self.state_bytes(),before)
        self.assertEqual(self.f.chat.calls,[]);self.assertEqual(self.f.preview.calls,[])

    def test_pause_resume_hide_and_reduced_motion(self):
        self.select('robot');movie=self.gallery.preview_movie
        self.gallery.play.click();frame=movie.currentFrameNumber();QTest.qWait(100)
        self.assertEqual(movie.state(),QMovie.MovieState.Paused);self.assertEqual(movie.currentFrameNumber(),frame)
        self.gallery.play.click();self.assertEqual(movie.state(),QMovie.MovieState.Running)
        self.w.tabs.setCurrentIndex(0);QTest.qWait(80)
        self.assertIsNone(self.gallery.preview_movie)
        self.f.c.prefs.values['reducedMotion']=True
        self.w.tabs.setCurrentIndex(3);self.select('robot');movie=self.gallery.preview_movie
        self.assertNotEqual(movie.state(),QMovie.MovieState.Running)
        self.gallery.play.click();self.assertEqual(movie.state(),QMovie.MovieState.Running)
        self.assertTrue(self.f.c.prefs.values['reducedMotion'])

    def test_nonloop_stops_at_last_frame_and_replay_starts_first(self):
        self.fixture_pack(loop=False);movie=self.gallery.preview_movie
        QTest.qWait(350)
        self.assertTrue(self.gallery.preview_finished)
        self.assertEqual(movie.currentFrameNumber(),2)
        self.assertEqual(movie.state(),QMovie.MovieState.Paused)
        self.assertEqual(self.gallery.play.text(),'重播')
        self.gallery.play.click()
        self.assertEqual(movie.currentFrameNumber(),0)
        self.assertFalse(self.gallery.preview_finished)

    def test_static_action_clears_player_and_bad_file_falls_back(self):
        self.fixture_pack()
        self.gallery.actions.setCurrentIndex(0)
        self.assertIsNone(self.gallery.preview_movie);self.assertFalse(self.gallery.play.isEnabled())
        self.gallery.preview_spec['clips']['poke']['file']=self.f.folder/'missing.gif'
        self.gallery.actions.setCurrentIndex(self.gallery.actions.findData('poke'))
        self.assertIsNone(self.gallery.preview_movie);self.assertFalse(self.gallery.preview.pixmap().isNull())
        self.assertIn('无法读取',self.gallery.detail.text())
        self.assertFalse(self.gallery.play.isEnabled())

    def test_single_frame_nonloop_label_and_hidden_file_release(self):
        self.fixture_pack(loop=False,count=1)
        movie=self.gallery.preview_movie
        self.assertTrue(self.gallery.preview_finished)
        self.assertEqual(self.gallery.play.text(),'重播')
        self.gallery.play.click()
        self.assertEqual(self.gallery.play.text(),'重播')
        path=Path(movie.fileName())
        self.w.tabs.setCurrentIndex(0)
        self.assertIsNone(self.gallery.preview_movie)
        self.assertEqual(movie.fileName(),'')
        moved=path.with_suffix('.release-check')
        path.rename(moved);moved.rename(path)

    def test_static_custom_role_has_only_static_preview(self):
        source=self.f.folder/'static.png';Image.new('RGBA',(48,48),'#995533').save(source)
        identity=self.f.c.roles.import_image(source,'静态测试','只用于隔离测试')
        before=self.state_bytes();self.gallery.refresh(identity)
        self.assertEqual(self.gallery.actions.count(),1)
        self.assertIsNone(self.gallery.preview_movie);self.assertFalse(self.gallery.play.isEnabled())
        self.assertIn('尚无连续动作',self.gallery.detail.text())
        self.assertEqual(before,self.state_bytes())


if __name__=='__main__':unittest.main()
