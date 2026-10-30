"""Poll only session identity/status/time from WorkBuddy's database, read-only."""
import sqlite3,threading,time
from contextlib import closing
from pathlib import Path

class WorkBuddyWatcher:
    def __init__(self,profile,core):
        self.path=Path(profile)/'.workbuddy/workbuddy.db';self.core=core
        self.seen={};self.booted=False;self.stop_event=threading.Event();self.thread=None
        self.last_success=None;self.errors=0
    def poll(self):
        if not self.core.enabled.get('workbuddy',False):
            # Re-enabling starts from a fresh baseline rather than replaying
            # task completions that happened while reminders were disabled.
            self.seen.clear();self.booted=False;self.last_success=None
            return
        if not self.path.is_file():
            self.core.health['workbuddy']='只读监听：未找到国内版状态库';return
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.15)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.set_progress_handler(lambda:1 if self.stop_event.is_set() else 0,1000)
                rows=conn.execute('SELECT id,status,updated_at FROM sessions WHERE deleted_at IS NULL AND COALESCE(is_background_automation,0)=0 ORDER BY updated_at DESC LIMIT 512').fetchall()
            current={}
            for sid,status,stamp in rows:
                if not isinstance(sid,str) or not 0<len(sid)<=256 or not isinstance(status,str) or type(stamp) not in (int,float):continue
                current[sid]=(status,stamp)
                previous=self.seen.get(sid)
                # First observation of every conversation is a baseline only.
                # A fresh unknown terminal row must not replay an old completion.
                if previous is not None and previous!=(status,stamp):
                    if status=='working':self.core.ingest('workbuddy',sid,'working','ReadOnlyDB:working')
                    elif status=='completed' and previous[0]=='working':self.core.ingest('workbuddy',sid,'completed','ReadOnlyDB:completed',completion_id=str(stamp))
                    elif status=='error' and previous[0]=='working':self.core.ingest('workbuddy',sid,'error','ReadOnlyDB:error')
                    elif status in ('terminated','archived','pending'):self.core.ingest('workbuddy',sid,'unknown','ReadOnlyDB:'+status)
            self.seen=current;self.booted=True;self.last_success=time.time()
            self.core.health['workbuddy']='只读状态库已连接 · 仅新变化提醒，审批等待未验证'
        except (sqlite3.Error,OSError,ValueError):
            self.errors+=1;self.core.health['workbuddy']='只读状态库暂不可用或结构不兼容 · 未改动客户端'
    def start(self):
        def run():
            while not self.stop_event.is_set():
                self.poll();self.stop_event.wait(1)
        self.thread=threading.Thread(target=run,name='WorkBuddyReadOnly',daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(2)
