"""Local Windows text-to-speech. No microphone or speech recognition code."""
import base64
import os
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from .storage import atomic_json, read_json


# A separate OS PowerShell process owns only the synthesizer and its callbacks.
WORKER = r'''
using System;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Speech.Synthesis;
public static class PetVoice {
 static readonly object outputGate=new object();
 static string Read() { return Encoding.UTF8.GetString(Convert.FromBase64String(Console.ReadLine() ?? "")); }
 static void Emit(string kind,string text) { lock(outputGate) { Console.WriteLine(kind+"\t"+Convert.ToBase64String(Encoding.UTF8.GetBytes(text ?? ""))); Console.Out.Flush(); } }
 public static void Run() {
  try {
   string mode=Read(), voice=Read(), rateText=Read(), text=Read();
   Emit("ready","");
   if(mode=="probe") {
    using(var s=new SpeechSynthesizer()) foreach(var v in s.GetInstalledVoices()) if(v.Enabled) Emit("voice",v.VoiceInfo.Name);
    return;
   }
   if(mode!="speak") throw new Exception("不支持此朗读操作。");
   using(var s=new SpeechSynthesizer()) {
    if(voice.Length>0) s.SelectVoice(voice);
    s.Rate=Math.Max(-5,Math.Min(5,int.Parse(rateText)));
    s.SetOutputToDefaultAudioDevice();
    var done=new ManualResetEvent(false);Exception speechError=null;bool cancelled=false;
    s.SpeakCompleted+=(o,e)=>{ speechError=e.Error;cancelled=e.Cancelled;done.Set(); };
    s.SpeakAsync(text);Emit("playing","");
    var stop=Task.Run(()=>Console.ReadLine());var until=DateTime.UtcNow.AddSeconds(120);
    while(!stop.IsCompleted && !done.WaitOne(0) && DateTime.UtcNow<until) Thread.Sleep(50);
    bool timedOut=!stop.IsCompleted && !done.WaitOne(0);
    s.SpeakAsyncCancelAll();done.WaitOne(1000);
    if(speechError!=null && !(stop.IsCompleted && cancelled)) throw speechError;
    if(timedOut) throw new Exception("朗读超时，已停止。");
    if(cancelled && !stop.IsCompleted) throw new Exception("朗读被系统中断，请重试。");
    Emit("done","");
   }
  } catch(Exception e) { Emit("error",e.Message); }
 }
}
'''


def worker_command():
    source=WORKER.replace("'", "''")
    script=("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; try { "
            "Add-Type -AssemblyName System.Speech; Add-Type -ReferencedAssemblies System.Speech -TypeDefinition '"
            +source+"'; [PetVoice]::Run() } catch { "
            "$detail=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_.Exception.Message)); "
            "[Console]::Out.WriteLine('error'+[char]9+$detail); [Console]::Out.Flush(); exit 1 }")
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


class VoiceSession(QObject):
    changed=Signal()
    capabilities=Signal(object)

    def __init__(self,state,parent=None):
        super().__init__(parent)
        self.path=Path(state)/'voice-settings.json'
        loaded=read_json(self.path,{})
        if not isinstance(loaded,dict):loaded={}
        # Retain existing output choices; retired input language is ignored.
        self.settings={'voice':loaded.get('voice','')[:200] if isinstance(loaded.get('voice',''),str) else '',
                       'rate':max(-5,min(5,loaded.get('rate',0))) if type(loaded.get('rate')) is int else 0,
                       'autoRead':loaded.get('autoRead') is True}
        self.process=None;self.state='idle';self.message='';self.mode=None
        self.buffer=b'';self.found={'voices':[]};self.generation=0
        self.has_error=False;self.diagnostics='';self._cancelled=False
        self._worker_ready=False;self._played=False;self._completed=False;self._stop_requested=False
        self.watchdog=QTimer(self);self.watchdog.setSingleShot(True);self.watchdog.timeout.connect(self._timeout)

    @property
    def button_text(self):
        return '停止' if self.process is not None else '朗读回复'

    def save(self,voice,rate,auto):
        draft={'voice':str(voice)[:200],'rate':max(-5,min(5,int(rate))),'autoRead':bool(auto)}
        atomic_json(self.path,draft);self.settings=draft

    def start(self,mode,text=''):
        if mode not in ('probe','speak'):raise ValueError('不支持此朗读操作。')
        if self.process is not None:return False
        executable=Path(os.environ.get('WINDIR','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not executable.is_file():
            self.has_error=True;self.message='找不到 Windows 朗读程序。';self.changed.emit();return False
        self.generation+=1;self.mode=mode;self.buffer=b'';self.found={'voices':[]}
        self.has_error=False;self.diagnostics='';self._cancelled=False
        self._worker_ready=False;self._played=False;self._completed=False;self._stop_requested=False
        self.state='starting';self.message='正在检查朗读声音' if mode=='probe' else '正在准备朗读'
        p=QProcess(self);self.process=p
        values=[mode,self.settings['voice'],str(self.settings['rate']),str(text)[:8000]]
        payload=('\n'.join(base64.b64encode(value.encode('utf-8')).decode('ascii') for value in values)+'\n').encode('ascii')
        p.started.connect(lambda:self._started(p,payload))
        p.readyReadStandardOutput.connect(lambda:self._read(p))
        p.readyReadStandardError.connect(lambda:self._read_errors(p))
        p.finished.connect(lambda *_:self._finished(p))
        p.errorOccurred.connect(lambda _:self._failed(p))
        self.watchdog.start(15000);self.changed.emit()
        p.start(str(executable),['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',worker_command()])
        return True

    def _started(self,p,payload):
        if p is not self.process:return
        if self._cancelled:p.kill();return
        p.write(payload)

    def _read_errors(self,p):
        if p is not self.process:return
        self.diagnostics=(self.diagnostics+bytes(p.readAllStandardError()).decode('utf-8',errors='replace'))[-8192:]

    def _read(self,p):
        if p is not self.process:return
        self.buffer+=bytes(p.readAllStandardOutput())
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                kind,encoded=line.rstrip(b'\r').split(b'\t',1)
                value=base64.b64decode(encoded,validate=True).decode('utf-8')
            except (ValueError,UnicodeError):continue
            kind=kind.decode('ascii',errors='ignore')
            if self._cancelled or self.has_error:continue
            if kind=='voice':self.found['voices'].append(value)
            elif kind=='ready':self._worker_ready=True
            elif kind=='playing':
                self._played=True
                if self.state!='stopping':
                    self.state='playing';self.message='正在朗读';self.watchdog.start(125000)
            elif kind=='done':self._completed=True
            elif kind=='error':
                self.has_error=True
                self.message='朗读失败：'+value.replace('\r',' ').replace('\n',' ')[:500]
        self.changed.emit()

    def _finished(self,p):
        if p is not self.process:return
        self._read(p);self._read_errors(p)
        if not self.has_error and not self._cancelled:
            if p.exitCode()!=0:
                self.has_error=True;self.message='朗读程序未正常结束，请检查系统声音或重试。'
            elif self.mode=='probe' and self._worker_ready:
                self.capabilities.emit(self.found)
                self.message='找到 %d 个朗读声音。'%len(self.found['voices'])
            elif self.mode=='speak' and self._worker_ready and self._played and self._completed:
                self.message='朗读已停止。' if self._stop_requested else '朗读结束。'
            else:
                self.has_error=True;self.message='朗读程序未返回结果，请重试。'
        self.process=None;self.state='idle';self.watchdog.stop();p.deleteLater();self.changed.emit()

    def _failed(self,p):
        if p is not self.process:return
        if not self.has_error and not self._cancelled:
            self.has_error=True;self.message='无法启动朗读程序，请重试。'
        if p.state()==QProcess.ProcessState.NotRunning:self._finished(p)
        else:p.kill()

    def stop(self):
        if self.process is None:return
        if self.has_error:self.process.kill();return
        self._stop_requested=True
        if self.state=='starting':
            self._cancelled=True;self.state='stopping';self.message='已取消朗读。';self.process.kill()
        else:
            self.state='stopping';self.message='正在停止';self.process.write(b'stop\n')
        self.watchdog.start(4000);self.changed.emit()

    def _timeout(self):
        if self.process is None:return
        if not self._cancelled:
            self.has_error=True
            self.message='朗读程序启动超时，请重试。' if self.state=='starting' else '朗读超时，已停止，请重试。'
        self.process.kill();self.changed.emit()

    def close(self):
        p=self.process;self.process=None;self.generation+=1
        self.watchdog.stop();self.state='idle';self.message='';self.has_error=False;self._cancelled=True
        if p is not None:
            p.kill();p.waitForFinished(1000);p.deleteLater()
        self.changed.emit()
