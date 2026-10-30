"""Content-free agent state. One identity per client/session, never per window."""
import hashlib
import threading
import time
from dataclasses import dataclass
from .storage import read_json, atomic_json

LABELS = {"idle": "等候新任务", "thinking": "正在思考", "working": "正在执行工具",
          "waiting": "需要你确认", "completed": "本轮完成", "error": "出现错误",
          "interrupted": "本轮已中断", "unknown": "状态待确认", "sleeping": "会话已关闭",
          "disconnected": "等待新的状态"}
PRIORITY = {"waiting": 90, "error": 80, "working": 60, "thinking": 50,
            "unknown": 40, "completed": 30, "interrupted": 20, "idle": 10,
            "sleeping": 0, "disconnected": 0}
CLIENTS = {"workbuddy": "WorkBuddy", "codex": "Codex"}


def hook_state(event, payload):
    """Map only documented metadata; never inspect prompts or tool arguments."""
    if event == "Notification":
        kind = str(payload.get("notification_type", "")).strip().lower()
        return {"permission_prompt": "waiting", "elicitation_dialog": "waiting",
                "idle_prompt": "idle", "auth_success": None}.get(kind, "unknown")
    if event == "PreToolUse" and str(payload.get("tool_name", "")).lower() in (
            "askuserquestion", "ask_user_question", "request_user_input", "exitplanmode", "exit_plan_mode"):
        return "waiting"
    return {"SessionStart": "idle", "SessionEnd": "sleeping", "UserPromptSubmit": "thinking",
            "PreToolUse": "working", "PostToolUse": "working", "PostToolUseFailure": "error",
            "Stop": "completed", "StopFailure": "error", "Elicitation": "waiting",
            "ElicitationResult": "thinking", "SubagentStart": "working", "SubagentStop": "working",
            "PreCompact": "working", "PostCompact": "thinking"}.get(event)


@dataclass
class Session:
    key: str
    client: str
    number: int
    state: str
    updated_at: float
    source_event: str
    observed_at: float
    context_percent: int = None


class Core:
    def __init__(self, clock=time.time, notice_path=None):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.client_counts = {client: 0 for client in CLIENTS}
        self.received = {client: 0 for client in CLIENTS}
        self.last_received = {}
        self.enabled = {client: True for client in CLIENTS}
        self.health = {"workbuddy": "等待接入", "codex": "正在寻找本机会话"}
        self.notices = []
        self.notice_sequence = 0
        self.completed_turns = []
        self.notice_path = notice_path
        if notice_path:
            saved=read_json(notice_path,{})
            items=saved.get('notices',[]) if isinstance(saved,dict) else []
            if isinstance(items,list):
                for item in items[-50:]:
                    if not isinstance(item,dict) or item.get('client') not in CLIENTS or type(item.get('id')) is not int:
                        continue
                    when=item.get('createdAt')
                    if not isinstance(when,(int,float)) or not 0<=self.clock()-when<=7*86400:
                        continue
                    name=item.get('name','')
                    if not isinstance(name,str) or not name.startswith('会话 ') or len(name)>20:
                        continue
                    self.notices.append({'id':item['id'],'client':item['client'],'clientLabel':CLIENTS[item['client']],
                        'name':name,'state':'completed','label':'本轮已完成','createdAt':when,
                        'read':item.get('read') is True,'shown':True})
                self.notice_sequence=max((n['id'] for n in self.notices),default=0)

    def save_notices(self):
        if self.notice_path:
            try:atomic_json(self.notice_path,{'schema':1,'notices':self.notices})
            except OSError:pass

    @staticmethod
    def key(client, sid):
        return hashlib.sha256((client + ":" + sid).encode()).hexdigest()[:24]

    def ingest(self, client, sid, state, event, observed_at=None, context=None, completion_id=None):
        if client not in CLIENTS or state not in LABELS or not isinstance(sid, str) or not 0 < len(sid) <= 256:
            return False
        now = self.clock()
        stamp = observed_at if isinstance(observed_at, (float, int)) else now
        if stamp > now + 60 or stamp < now - 120:
            stamp = now
        key = self.key(client, sid)
        with self.lock:
            if not self.enabled[client]:
                return False
            previous = self.sessions.get(key)
            if previous and stamp < previous.observed_at:
                return False
            # An idle timer is not evidence that a real approval was answered.
            if event == "Notification" and state == "idle" and previous and previous.state in (
                    "waiting", "completed", "error", "interrupted"):
                return False
            if not previous:
                if len(self.sessions) >= 100:
                    oldest = min(self.sessions, key=lambda k: self.sessions[k].updated_at)
                    del self.sessions[oldest]
                self.client_counts[client] += 1
            number = previous.number if previous else self.client_counts[client]
            percent = previous.context_percent if previous else None
            if isinstance(context, int):
                percent = min(100, max(0, context))
            self.sessions[key] = Session(key, client, number, state, now, event[:64], stamp, percent)
            # Completion is an independent event, not a low-priority pet state.
            # Repeated Stop events for an already completed turn must not spam.
            completion_key = (client, key, completion_id) if isinstance(completion_id,str) and 0<len(completion_id)<=256 else None
            new_completion = (completion_key not in self.completed_turns if completion_key else
                              previous is None or previous.state != "completed")
            if state == "completed" and new_completion:
                if completion_key:
                    self.completed_turns.append(completion_key)
                    self.completed_turns=self.completed_turns[-512:]
                self.notice_sequence += 1
                self.notices.append({"id": self.notice_sequence, "client": client, "clientLabel": CLIENTS[client],
                                     "name": "会话 " + str(number), "state": "completed", "label": "本轮已完成",
                                     "createdAt": now, "shown": False, "read": False})
                self.notices = self.notices[-50:]
                self.save_notices()
            self.received[client] += 1
            self.last_received[client] = now
            return True

    def context(self, sid, percent):
        with self.lock:
            s = self.sessions.get(self.key("codex", sid))
            if s:
                s.context_percent = max(0, min(100, int(percent)))

    def set_enabled(self, client, enabled):
        with self.lock:
            self.enabled[client] = enabled
            if not enabled:
                for key in [key for key, s in self.sessions.items() if s.client == client]:
                    del self.sessions[key]

    def clear_finished(self):
        with self.lock:
            for key in [key for key, s in self.sessions.items() if s.state in
                        ("completed", "error", "interrupted", "sleeping")]:
                del self.sessions[key]

    def mark_notice_shown(self, notice_id):
        with self.lock:
            for n in self.notices:
                if n["id"] == notice_id:
                    n["shown"] = True
            self.save_notices()

    def acknowledge_notice(self, notice_id=None):
        with self.lock:
            for n in self.notices:
                if notice_id is None or n["id"] == notice_id:
                    n["read"] = True
                    n["shown"] = True
            self.save_notices()

    def snapshot(self):
        now = self.clock()
        with self.lock:
            rows = []
            for s in self.sessions.values():
                if not self.enabled[s.client] or now - s.updated_at > 24 * 3600:
                    continue
                state = s.state
                # Preserve uncertainty rather than claiming silent work completed.
                if state in ("thinking", "working") and now - s.updated_at > 15 * 60:
                    state = "disconnected"
                rows.append({"key": s.key, "client": s.client, "clientLabel": CLIENTS[s.client],
                             "name": "会话 " + str(s.number), "state": state,
                             "label": LABELS[state], "updatedAt": s.updated_at,
                             "sourceEvent": s.source_event, "contextPercent": s.context_percent})
            def priority(row):
                score = PRIORITY[row["state"]]
                # A finished turn remains in the list but does not dominate the pet.
                if row["state"] == "completed" and now - row["updatedAt"] > 12:
                    score = 5
                return score, row["updatedAt"]
            rows.sort(key=priority, reverse=True)
            best = rows[0] if rows and priority(rows[0])[0] >= 10 else None
            return {"sessions": rows, "active": best, "received": dict(self.received),
                    "lastReceived": dict(self.last_received), "health": dict(self.health),
                    "enabled": dict(self.enabled), "at": now,
                    "notices": [dict(n) for n in self.notices],
                    "unreadCompletions": sum(not n["read"] for n in self.notices)}
