"""Bounded read-only rollout adapter; no auth, prompts, commands or replies stored."""
import datetime as dt
import json
import os
from pathlib import Path
import threading
import time

MAX_CHUNK = 256 * 1024
MAX_LINE = 1024 * 1024


class CodexWatcher:
    def __init__(self, root, core):
        self.root, self.core = Path(root), core
        self.files = {}
        self.booted = False
        self.last_discovery = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.bytes_read = 0
        self.errors = 0
        self.paused = False

    def discover(self):
        if not self.root.is_dir():
            self.core.health["codex"] = "未找到本机会话目录"
            return
        count = 0
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
            if len(Path(directory).relative_to(self.root).parts) > 4:
                dirs[:] = []
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                count += 1
                if count > 5000:
                    self.core.health["codex"] = "会话较多，已限制扫描范围"
                    return
                p = Path(directory) / name
                if p in self.files or p.is_symlink():
                    continue
                try:
                    stat = p.stat()
                except OSError:
                    continue
                self.files[p] = {"offset": stat.st_size if not self.booted else 0, "inode": stat.st_ino,
                                 "carry": b"", "discard": False, "sid": None, "ignored": False,
                                 "state": "idle", "didWork": False, "meta": False}
        self.core.health["codex"] = "只读监听已就绪 · 等待新事件"

    def metadata(self, tracker, row):
        value = row.get("payload", {})
        if not isinstance(value, dict):
            return
        sid = value.get("id") or value.get("session_id")
        if isinstance(sid, str) and 0 < len(sid) <= 256:
            tracker["sid"] = sid
        src = value.get("source")
        if value.get("thread_source") == "subagent" or (isinstance(src, dict) and "subagent" in src):
            tracker["ignored"] = True
        tracker["meta"] = True

    def emit(self, tracker, state, event, completion_id=None):
        if tracker["sid"] and not tracker["ignored"]:
            tracker["state"] = state
            self.core.ingest("codex", tracker["sid"], state, event, completion_id=completion_id)

    def handle(self, tracker, row):
        if not isinstance(row, dict):
            return
        kind, payload = row.get("type"), row.get("payload", {})
        if not isinstance(payload, dict):
            return
        if kind == "session_meta":
            self.metadata(tracker, row)
            return
        if tracker["ignored"] or not tracker["sid"]:
            return
        event = payload.get("type")
        if kind == "response_item":
            if event in ("function_call", "custom_tool_call", "web_search_call"):
                tracker["didWork"] = True
                name = str(payload.get("name", "")).lower()
                waiting = any(t in name for t in ("request_user_input", "ask_user_question", "askuserquestion"))
                self.emit(tracker, "waiting" if waiting else "working", "PreToolUse")
            elif event in ("function_call_output", "custom_tool_call_output"):
                tracker["didWork"] = True
                self.emit(tracker, "working", "PostToolUse")
            elif event == "reasoning" and tracker["state"] != "waiting":
                self.emit(tracker, "working" if tracker["didWork"] else "thinking", "Reasoning")
        elif kind == "event_msg":
            if event in ("task_started", "user_message"):
                tracker["didWork"] = False
                self.emit(tracker, "thinking", "TaskStarted")
            elif event == "task_complete":
                tracker["didWork"] = False
                self.emit(tracker, "completed", "TaskComplete", payload.get('turn_id') or payload.get('root_turn_id'))
            elif event == "turn_aborted":
                tracker["didWork"] = False
                self.emit(tracker, "interrupted", "TurnAborted")
            elif event in ("error", "stream_error"):
                self.emit(tracker, "error", "Error")
            elif isinstance(event, str) and (event.endswith("approval_request") or event in ("request_user_input", "elicitation_request")):
                self.emit(tracker, "waiting", "ApprovalRequest")
            elif event in ("patch_apply_end", "mcp_tool_call_end", "web_search_end"):
                tracker["didWork"] = True
                self.emit(tracker, "error" if payload.get("success") is False else "working", "ToolResult")
            elif event == "token_count":
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    usage = info.get("last_token_usage") or {}
                    limit = info.get("model_context_window")
                    used = usage.get("total_tokens") if isinstance(usage, dict) else None
                    if isinstance(limit, (int, float)) and limit > 0 and isinstance(used, (int, float)):
                        self.core.context(tracker["sid"], round(100 * used / limit))

    def pump(self, p, tracker):
        try:
            stat = p.stat()
            if stat.st_ino != tracker["inode"] or stat.st_size < tracker["offset"]:
                # A replacement is bootstrapped silently, never replay old completions.
                tracker.update(offset=stat.st_size, inode=stat.st_ino, carry=b"", discard=False, meta=False)
                if tracker["sid"]:
                    self.emit(tracker, "unknown", "SourceReplaced")
                return 0
            if stat.st_size <= tracker["offset"]:
                return 0
            if not tracker["meta"]:
                with p.open("rb") as f:
                    head = f.readline(MAX_LINE)
                self.bytes_read += len(head)
                try:
                    meta = json.loads(head)
                    if isinstance(meta, dict) and meta.get("type") == "session_meta":
                        self.metadata(tracker, meta)
                except (ValueError, UnicodeDecodeError):
                    pass
            if tracker["ignored"]:
                tracker["offset"] = stat.st_size
                return 0
            with p.open("rb") as f:
                f.seek(tracker["offset"])
                chunk = f.read(MAX_CHUNK)
            tracker["offset"] += len(chunk)
            self.bytes_read += len(chunk)
            if tracker["discard"]:
                nl = chunk.find(b"\n")
                if nl < 0:
                    return len(chunk)
                chunk = chunk[nl + 1:]
                tracker["discard"] = False
            lines = (tracker["carry"] + chunk).split(b"\n")
            tracker["carry"] = lines.pop()
            if len(tracker["carry"]) > MAX_LINE:
                tracker["carry"] = b""
                tracker["discard"] = True
            for line in lines:
                if len(line) > MAX_LINE:
                    continue
                try:
                    self.handle(tracker, json.loads(line))
                except (ValueError, UnicodeDecodeError, TypeError, OverflowError):
                    continue
            return len(chunk)
        except OSError:
            self.errors += 1
            return 0

    def tick(self):
        if not self.core.enabled["codex"]:
            if not self.paused:
                self.files.clear()
                self.booted = False
                self.paused = True
            return
        self.paused = False
        now = time.monotonic()
        if not self.booted or now - self.last_discovery >= 8:
            self.discover()
            self.last_discovery = now
        budget = 0
        for p, tracker in list(self.files.items()):
            budget += self.pump(p, tracker)
            if budget >= 2 * 1024 * 1024:
                break
        self.booted = True

    def start(self):
        def run():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    self.errors += 1
                    self.core.health["codex"] = "会话暂时不可读，将自动重试"
                self.stop_event.wait(1.5)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
