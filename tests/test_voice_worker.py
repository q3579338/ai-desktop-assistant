"""Output-only speech lifecycle tests; no microphone or audible output."""
import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'.vendor')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from starpet.voice import VoiceSession,WORKER


class FakeSignal:
    def __init__(self):self.callbacks=[]
    def connect(self,callback):self.callbacks.append(callback)
    def emit(self,*args):
        for callback in self.callbacks:callback(*args)


class FakeProcess:
    ProcessState=QProcess.ProcessState
    def __init__(self,parent=None):
        self.started=FakeSignal();self.finished=FakeSignal();self.errorOccurred=FakeSignal()
        self.readyReadStandardOutput=FakeSignal();self.readyReadStandardError=FakeSignal()
        self.output=b'';self.errors=b'';self.written=[];self.killed=False;self.deleted=False
        self.code=0;self.status=QProcess.ProcessState.NotRunning
    def start(self,executable,args):
        self.executable=executable;self.args=args;self.status=QProcess.ProcessState.Starting
    def report_started(self):self.status=QProcess.ProcessState.Running;self.started.emit()
    def readAllStandardOutput(self):value=self.output;self.output=b'';return value
    def readAllStandardError(self):value=self.errors;self.errors=b'';return value
    def write(self,value):self.written.append(value);return len(value)
    def kill(self):self.killed=True
    def waitForFinished(self,_):return True
    def deleteLater(self):self.deleted=True
    def state(self):return self.status
    def exitCode(self):return self.code


def event(kind,value=''):
    return kind.encode()+b'\t'+base64.b64encode(value.encode())+b'\r\n'


class VoiceWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if cls.app.platformName()!='offscreen':raise RuntimeError('No foreground fallback')
    def setUp(self):
        folder=ROOT/'evidence/tests';folder.mkdir(parents=True,exist_ok=True)
        self.directory=folder/('voice-worker-'+uuid.uuid4().hex);self.directory.mkdir()
        self.v=VoiceSession(self.directory)
    def tearDown(self):
        self.v.close()
        for path in self.directory.iterdir():path.unlink()
        self.directory.rmdir()
    def start(self,mode='speak',text='测试朗读'):
        with patch('starpet.voice.QProcess',FakeProcess),patch('starpet.voice.Path.is_file',return_value=True):
            self.assertTrue(self.v.start(mode,text))
        return self.v.process
    def feed(self,p,*items):p.output=b''.join(event(*item) for item in items);self.v._read(p)
    def finish(self,p,code=0):p.code=code;p.status=QProcess.ProcessState.NotRunning;self.v._finished(p)

    def test_record_mode_is_rejected_before_any_process_can_start(self):
        with patch('starpet.voice.QProcess') as factory:
            with self.assertRaisesRegex(ValueError,'不支持'):self.v.start('record')
            factory.assert_not_called()
        self.assertIsNone(self.v.process)
        self.assertFalse(hasattr(self.v,'transcript'))
        self.assertFalse(hasattr(self.v,'level'))
        self.assertNotIn('SpeechRecognition',WORKER);self.assertNotIn('SetInput',WORKER)
    def test_legacy_input_settings_are_ignored_without_rewriting_on_read(self):
        original=json.dumps({'language':'zh-CN','voice':'旧声音','rate':2,'autoRead':True}).encode()
        self.v.path.write_bytes(original);loaded=VoiceSession(self.directory)
        self.assertEqual(loaded.settings,{'voice':'旧声音','rate':2,'autoRead':True})
        self.assertEqual(self.v.path.read_bytes(),original)
        loaded.save('新声音',1,False)
        self.assertEqual(json.loads(self.v.path.read_text()),{'voice':'新声音','rate':1,'autoRead':False})
        loaded.close()
    def test_request_waits_for_started_and_text_is_not_in_command(self):
        p=self.start('speak','仅放在标准输入的文字')
        self.assertEqual(p.written,[]);self.assertEqual(self.v.watchdog.interval(),15000)
        self.assertNotIn('仅放在标准输入的文字',' '.join(p.args));p.report_started()
        values=[base64.b64decode(value).decode() for value in p.written[0].splitlines()]
        self.assertEqual(values,['speak','','0','仅放在标准输入的文字'])
    def test_finished_requires_playback_and_completion_not_only_worker_ready(self):
        p=self.start();self.feed(p,('ready',));self.finish(p)
        self.assertTrue(self.v.has_error);self.assertIn('未返回结果',self.v.message)
    def test_playback_and_completion_report_success(self):
        p=self.start();p.report_started();self.feed(p,('ready',),('playing',))
        self.assertEqual(self.v.state,'playing');self.assertEqual(self.v.watchdog.interval(),125000)
        self.feed(p,('done',));self.finish(p)
        self.assertFalse(self.v.has_error);self.assertEqual(self.v.message,'朗读结束。')
        self.assertEqual(self.v.button_text,'朗读回复')
    def test_stop_is_forwarded_and_confirmed_without_false_natural_completion(self):
        p=self.start();p.report_started();self.feed(p,('ready',),('playing',));self.v.stop()
        self.assertEqual(p.written[-1],b'stop\n');self.assertEqual(self.v.state,'stopping')
        self.feed(p,('done',));self.finish(p)
        self.assertEqual(self.v.message,'朗读已停止。');self.assertFalse(self.v.has_error)
    def test_cancel_during_startup_does_not_queue_late_playback(self):
        p=self.start();self.v.stop();p.report_started()
        self.assertTrue(p.killed);self.assertEqual(p.written,[])
        self.feed(p,('playing',),('done',));self.finish(p,1)
        self.assertEqual(self.v.message,'已取消朗读。');self.assertFalse(self.v.has_error)
    def test_startup_timeout_keeps_specific_error_after_killed_exit(self):
        p=self.start();self.v._timeout();message=self.v.message
        self.assertTrue(p.killed);self.assertIn('启动超时',message)
        self.feed(p,('done',));self.finish(p,1)
        self.assertTrue(self.v.has_error);self.assertEqual(self.v.message,message)
    def test_worker_error_is_not_replaced_by_generic_exit_error(self):
        p=self.start();self.feed(p,('error','找不到所选声音。'));self.finish(p,1)
        self.assertEqual(self.v.message,'朗读失败：找不到所选声音。');self.assertTrue(self.v.has_error)
    def test_qprocess_failure_survives_cleanup(self):
        p=self.start();p.status=QProcess.ProcessState.NotRunning;self.v._failed(p)
        self.assertIsNone(self.v.process);self.assertTrue(self.v.has_error)
        self.assertEqual(self.v.message,'无法启动朗读程序，请重试。')
    def test_stderr_is_bounded_without_raw_output_in_status(self):
        p=self.start();p.errors=b'compiler detail '+b'x'*9000;self.v._read_errors(p);self.finish(p,1)
        self.assertEqual(len(self.v.diagnostics),8192);self.assertNotIn('xxx',self.v.message)
    def test_probe_only_emits_output_voices(self):
        found=[];self.v.capabilities.connect(found.append)
        p=self.start('probe');p.report_started()
        self.feed(p,('ready',),('language','zh-CN'),('voice','测试声音'));self.finish(p)
        self.assertEqual(found,[{'voices':['测试声音']}])
        self.assertEqual(base64.b64decode(p.written[0].splitlines()[0]),b'probe')
        self.assertEqual(self.v.message,'找到 1 个朗读声音。')
    def test_removed_input_protocol_events_cannot_reach_a_transcript(self):
        p=self.start();self.feed(p,('recording',),('text','旧输入'),('level','99'),('recognized','2'))
        self.assertEqual(self.v.state,'starting');self.assertEqual(self.v.message,'正在准备朗读')
        self.assertFalse(hasattr(self.v,'transcript'));self.assertFalse(hasattr(self.v,'recognized_count'))
    def test_late_previous_generation_cannot_touch_new_session(self):
        previous=self.start();self.v.close();current=self.start()
        previous.output=event('done')+event('error','旧错误')
        self.v._read(previous);self.v._finished(previous);self.v._started(previous,b'old')
        self.assertIs(self.v.process,current);self.assertEqual(current.written,[])
        self.assertEqual(self.v.message,'正在准备朗读');self.assertFalse(self.v.has_error)


if __name__=='__main__':unittest.main()
