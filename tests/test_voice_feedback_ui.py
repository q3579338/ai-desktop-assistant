"""Input removal and retained speech output; no microphone or system input."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture,QApplication
from test_voice import FakeProcess
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QPointF,QRect,Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel,QPushButton


class VoiceFeedbackUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if cls.app.platformName()!='offscreen':raise RuntimeError('Offscreen only; no foreground fallback')

    def setUp(self):
        self.f=ContextUIFixture(self.app)
        self.c=self.f.c;self.pet=self.c.pet;self.voice=self.c.voice;self.popup=self.c.voice_feedback

    def tearDown(self):self.f.close()

    def publish(self,message,process=None,state='idle',mode='speak'):
        self.voice.process=process;self.voice.state=state;self.voice.mode=mode;self.voice.message=message
        self.voice.changed.emit();self.app.processEvents()

    def test_input_controls_and_routes_are_removed_but_output_is_available(self):
        w=self.f.window
        for name in ('voice_button','voice_language','voice_level'):self.assertFalse(hasattr(w,name))
        for name in ('toggle_voice','receive_voice'):self.assertFalse(hasattr(self.c,name))
        self.assertFalse(hasattr(self.popup,'level'))
        self.assertEqual(w.read_button.text(),'朗读回复')
        visible_text=' '.join(widget.text() for widget in w.findChildren(QPushButton)+w.findChildren(QLabel))
        for forbidden in ('麦克风','录音','识别语言','语音输入'):self.assertNotIn(forbidden,visible_text)

    def test_output_failure_remains_readable_without_changing_task_or_draft(self):
        self.c.core.ingest('codex','speech-ui','working','TaskStarted')
        before=self.c.core.snapshot()['active'].copy()
        self.f.window.input.setPlainText('保留草稿')
        message='朗读失败：没有找到所选声音，请在朗读设置中选择可用声音后重试。'
        self.publish(message)
        self.assertTrue(self.popup.isVisible());self.assertEqual(self.popup.title.text(),'语音朗读')
        self.assertEqual(self.popup.detail.toPlainText(),message);self.assertEqual(self.popup.action.text(),'知道了')
        self.assertEqual(self.c.core.snapshot()['active'],before)
        self.assertEqual(self.f.window.input.toPlainText(),'保留草稿');self.assertEqual(self.f.chat.calls,[])
        self.assertTrue(self.popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(self.popup.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)

    def test_output_stop_and_dismiss(self):
        process=FakeProcess();self.publish('正在朗读',process,state='playing')
        self.assertEqual(self.popup.action.text(),'停止')
        self.popup.action.click();self.assertEqual(process.written,[b'stop\n'])
        self.publish('朗读已结束。');self.popup.action.click();self.assertFalse(self.popup.isVisible())
        self.voice.changed.emit();self.assertFalse(self.popup.isVisible())

    def test_output_can_retry_same_early_failure_after_dismissal(self):
        message='找不到 Windows 本机朗读程序。'
        self.publish(message);self.popup.action.click()
        self.f.window.last_voice_reply='用户收到的回复'
        def fail(*_):self.voice.message=message;self.voice.changed.emit();return False
        with patch.object(self.voice,'start',side_effect=fail) as start:
            self.f.window.read_reply();start.assert_called_once_with('speak','用户收到的回复')
        self.assertTrue(self.popup.isVisible());self.assertEqual(self.popup.detail.toPlainText(),message)

    def test_output_long_error_fits_small_screen_and_preserves_task_bubble(self):
        area=QRect(0,0,320,310);screen=SimpleNamespace(availableGeometry=lambda:area)
        message='朗读失败：'+('请检查所选声音与输出设备设置。'*28)
        with patch.object(QApplication,'screenAt',return_value=screen),patch.object(QApplication,'primaryScreen',return_value=screen):
            self.pet.place_desktop_anchor(QPoint(5,5));self.pet.apply_size();self.publish(message)
            self.assertEqual(self.popup.detail.toPlainText(),message);self.assertTrue(area.contains(self.popup.geometry()))
            self.assertGreater(self.popup.detail.verticalScrollBar().maximum(),0)
            bubble=QRect(self.pet.x()+round((6+self.pet._bubble_offset_x)*self.pet._base_scale),
                         self.pet.y()+round(5*self.pet._base_scale),round(238*self.pet._base_scale),round(85*self.pet._base_scale))
            self.assertFalse(self.popup.geometry().intersects(bubble))

    def test_former_voice_button_area_uses_normal_task_bubble_click_at_both_sizes(self):
        for human,size,human_size in ((False,80,100),(False,150,200),(True,80,160),(True,110,200),(True,150,160)):
            with self.subTest(human=human,size=size,human_size=human_size),patch.object(self.pet,'human_body_visible',return_value=human):
                self.c.prefs.values.update(size=size,humanSize=human_size);self.pet.apply_size()
                point=QPointF((207+self.pet._bubble_offset_x)*self.pet._base_scale,76*self.pet._base_scale)
                self.assertTrue(self.pet.mask().contains(point.toPoint()))
                global_point=QPointF(self.pet.mapToGlobal(point.toPoint()))
                with patch.object(self.c,'show_panel') as panel,patch.object(self.voice,'start') as start:
                    press=QMouseEvent(QEvent.Type.MouseButtonPress,point,global_point,Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
                    release=QMouseEvent(QEvent.Type.MouseButtonRelease,point,global_point,Qt.MouseButton.LeftButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
                    QCoreApplication.sendEvent(self.pet,press);QCoreApplication.sendEvent(self.pet,release)
                    panel.assert_called_once();start.assert_not_called()
                self.assertFalse(self.pet.click_timer.isActive())


if __name__=='__main__':unittest.main()
