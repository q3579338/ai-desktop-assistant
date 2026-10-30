"""Loopback-only, authenticated IPC. No model calls or outbound requests."""
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import time

from . import VERSION
from .core import CLIENTS, LABELS, hook_state
from .storage import atomic_json, read_json


def request(runtime, endpoint, payload=None, timeout=.35):
    if not isinstance(runtime, dict) or runtime.get("app") != "satloot-starpet":
        return None
    port, token = runtime.get("port"), runtime.get("token")
    if not isinstance(port, int) or not 1024 <= port <= 65535 or not isinstance(token, str):
        return None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request("POST" if body else "GET", endpoint, body=body,
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        res = conn.getresponse()
        data = res.read(128 * 1024)
        return json.loads(data) if res.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def emit_hook(event, payload, state_dir):
    state = hook_state(event, payload)
    sid = payload.get("session_id")
    if state is None or not isinstance(sid, str) or not 0 < len(sid) <= 256:
        return False
    runtime = read_json(Path(state_dir) / "runtime.json")
    result = request(runtime, "/event", {"client": "workbuddy", "sessionId": sid, "state": state,
                                        "event": event, "observedAt": time.time(),
                                        "completionId":payload.get('turn_id') if isinstance(payload.get('turn_id'),str) else None}, timeout=.2)
    return bool(result and result.get("ok"))


class IPC:
    def __init__(self, core, state_dir):
        self.core, self.state_dir = core, Path(state_dir)
        self.actions = queue.Queue(maxsize=32)
        self.appearance = {}
        self.token = secrets.token_hex(32)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def setup(self):
                super().setup()
                self.connection.settimeout(1)
            def allowed(self):
                host = self.headers.get("Host", "")
                return (self.client_address[0] == "127.0.0.1" and
                        host in ("127.0.0.1:" + str(owner.server.server_port), "localhost:" + str(owner.server.server_port)) and
                        not self.headers.get("Origin") and not self.headers.get("Referer") and
                        hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token))
            def reply(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
            def do_GET(self):
                if not self.allowed():
                    return self.reply(403, {"ok": False})
                if self.path == "/health":
                    return self.reply(200, {"app": "satloot-starpet", "version": VERSION, "ok": True})
                if self.path == "/appearance":
                    return self.reply(200, owner.appearance)
                if self.path == "/snapshot":
                    return self.reply(200, owner.core.snapshot())
                self.reply(404, {"ok": False})
            def do_POST(self):
                if not self.allowed():
                    return self.reply(403, {"ok": False})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        return self.reply(413, {"ok": False})
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, dict):
                        raise ValueError()
                except (ValueError, OSError):
                    return self.reply(400, {"ok": False})
                if self.path == "/event":
                    event = value.get("event")
                    if not isinstance(event, str) or len(event) > 64 or value.get("client") != "workbuddy":
                        return self.reply(400, {"ok": False})
                    accepted = owner.core.ingest("workbuddy", value.get("sessionId"), value.get("state"),
                                                 event, value.get("observedAt"),completion_id=value.get('completionId'))
                    return self.reply(200, {"ok": accepted})
                if self.path == "/control" and value.get("action") in ("show", "hide", "quit", "chat"):
                    try:
                        owner.actions.put_nowait(value["action"])
                    except queue.Full:
                        return self.reply(429, {"ok": False})
                    return self.reply(200, {"ok": True})
                self.reply(404, {"ok": False})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        atomic_json(self.state_dir / "runtime.json", {"app": "satloot-starpet", "port": self.server.server_port,
                                                     "token": self.token, "pid": os.getpid()})
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        target = self.state_dir / "runtime.json"
        current = read_json(target, {})
        if isinstance(current, dict) and current.get("token") == self.token:
            try:
                target.unlink()
            except OSError:
                pass
