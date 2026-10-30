"""Speech output and removed-input regressions, without actual audio devices."""
import base64
import unittest
from unittest.mock import patch
from test_companion_context_ui import ContextUIFixture,QApplication
from starpet.voice import VoiceSession


class FakeProcess:
    def __init__(self,data=b''):self.data=data;self.written=[];self.killed=False
    def readAllStandardOutput(self):data=self.data;self.data=b'';return data
    def readAllStandardError(self):return b''
    def write(self,value):self.written.append(value)
    def kill(self):self.killed=True
    def waitForFinished(self,_):return True
    def deleteLater(self):pass


class VoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):self.f=ContextUIFixture(self.app);self.v=self.f.c.voice
    def tearDown(self):self.v.close();self.f.close()

    def test_removed_input_cannot_start_process_or_append_old_transcription(self):
        w=self.f.window;w.input.setPlainText('原来的草稿')
        with patch('starpet.voice.QProcess') as process:
            with self.assertRaises(ValueError):self.v.start('record')
            process.assert_not_called()
        payload=b'text\t'+base64.b64encode('不应进入草稿'.encode())+b'\n'
        p=FakeProcess(payload);self.v.process=p;self.v.mode='speak';self.v._read(p)
        self.assertEqual(w.input.toPlainText(),'原来的草稿')
        self.assertFalse(hasattr(self.f.c,'receive_voice'));self.assertFalse(hasattr(self.v,'transcript'))
        self.assertIsNone(w.job);self.assertEqual(self.f.chat.calls,[])

    def test_read_reply_starts_only_output_and_can_stop_it(self):
        w=self.f.window;w.last_voice_reply='这是一条回复';w.input.setPlainText('未发送草稿')
        with patch.object(self.v,'start') as start:
            w.read_button.click();start.assert_called_once_with('speak','这是一条回复')
        self.v.process=FakeProcess();self.v.mode='speak'
        w.read_button.click();self.assertEqual(self.v.process.written,[b'stop\n'])
        self.assertEqual(w.input.toPlainText(),'未发送草稿');self.assertEqual(self.f.chat.calls,[])

    def test_failed_save_does_not_publish_settings(self):
        before=dict(self.v.settings)
        with patch('starpet.voice.atomic_json',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.v.save('test',3,True)
        self.assertEqual(self.v.settings,before)

    def test_output_settings_survive_reload_without_changing_chat_provider(self):
        before=dict(self.f.c.companion_store.settings)
        self.v.save('Microsoft Huihui Desktop',1,False)
        loaded=VoiceSession(self.f.state)
        self.assertEqual(loaded.settings,self.v.settings)
        self.assertNotIn('language',loaded.settings)
        self.assertEqual(self.f.c.companion_store.settings,before)

    def test_output_sound_probe_does_not_speak_or_send_chat(self):
        w=self.f.window
        with patch.object(self.v,'start') as start:
            w.probe_voice();start.assert_called_once_with('probe')
        w.voice_capabilities({'voices':['Microsoft Huihui Desktop']})
        self.assertGreaterEqual(w.voice_name.findData('Microsoft Huihui Desktop'),0)
        self.assertEqual(self.f.chat.calls,[])
