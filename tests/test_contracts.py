import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from starpet.core import Core, hook_state
from starpet.codex import CodexWatcher, MAX_CHUNK, MAX_LINE
from starpet.hooks import configure, EVENTS, MARKER
from starpet.storage import Preferences, read_json
from starpet.transport import IPC, emit_hook, request
from starpet.characters import character_spec, atlas_rect, STATES

ROOT = Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / "evidence" / "tests" / uuid.uuid4().hex[:12]
        self.base.mkdir(parents=True)


class StateTests(unittest.TestCase):
    def test_notification_semantics(self):
        for event, state in [("idle_prompt", "idle"), ("permission_prompt", "waiting"),
                             ("elicitation_dialog", "waiting"), ("auth_success", None), ("new-version-notice", "unknown")]:
            self.assertEqual(hook_state("Notification", {"notification_type": event}), state)
        self.assertEqual(hook_state("ElicitationResult", {}), "thinking")
        self.assertEqual(hook_state("PostToolUse", {}), "working")
        self.assertEqual(hook_state("PreToolUse", {"tool_name": "AskUserQuestion"}), "waiting")

    def test_multi_client_and_priority_do_not_mix_sessions(self):
        c = Core(); c.ingest("workbuddy", "same-id", "working", "PreToolUse")
        c.ingest("codex", "same-id", "completed", "TaskComplete")
        self.assertEqual(c.snapshot()["active"]["client"], "workbuddy")
        self.assertEqual(len(c.snapshot()["sessions"]), 2)
        c.ingest("codex", "other-id", "waiting", "ApprovalRequest")
        best = c.snapshot()["active"]
        self.assertEqual(best["client"], "codex"); self.assertEqual(best["state"], "waiting")

    def test_idle_prompt_does_not_clear_real_approval_or_completed_turn(self):
        c = Core(); c.ingest("workbuddy", "s", "waiting", "Elicitation")
        self.assertFalse(c.ingest("workbuddy", "s", "idle", "Notification"))
        self.assertEqual(c.snapshot()["active"]["state"], "waiting")
        c.ingest("workbuddy", "s", "thinking", "ElicitationResult")
        self.assertEqual(c.snapshot()["active"]["state"], "thinking")

    def test_out_of_order_events_and_stale_work(self):
        now = [1000.0]; c = Core(clock=lambda: now[0])
        c.ingest("codex", "s", "thinking", "TaskStarted", 1000)
        self.assertFalse(c.ingest("codex", "s", "completed", "TaskComplete", 999))
        now[0] += 901
        self.assertEqual(c.snapshot()["sessions"][0]["state"], "disconnected")

    def test_turn_completion_expires_only_for_pet_not_history(self):
        now = [1000.0]; c = Core(clock=lambda: now[0])
        c.ingest("workbuddy", "s", "completed", "Stop")
        self.assertEqual(c.snapshot()["active"]["label"], "本轮完成")
        now[0] += 13
        self.assertIsNone(c.snapshot()["active"])
        self.assertEqual(c.snapshot()["sessions"][0]["state"], "completed")

    def test_pause_clears_only_selected_client(self):
        c = Core(); c.ingest("workbuddy", "s", "working", "PreToolUse")
        c.ingest("codex", "s", "thinking", "TaskStarted")
        c.set_enabled("codex", False)
        self.assertEqual(len(c.snapshot()["sessions"]), 1)
        self.assertFalse(c.ingest("codex", "s", "working", "PreToolUse"))

    def test_clear_finished_keeps_active_and_waiting(self):
        c = Core()
        for sid, state in [('a','error'),('b','completed'),('c','waiting'),('d','working')]:
            c.ingest('workbuddy',sid,state,'Fixture')
        c.clear_finished()
        self.assertEqual({r['state'] for r in c.snapshot()['sessions']},{'waiting','working'})

    def test_completion_notice_survives_other_work_and_timeout(self):
        now=[1000.0];c=Core(clock=lambda:now[0])
        c.ingest('codex','running','working','PreToolUse')
        c.ingest('workbuddy','finished','completed','Stop')
        snap=c.snapshot()
        self.assertEqual(snap['active']['state'],'working')
        self.assertEqual(snap['unreadCompletions'],1)
        self.assertEqual(snap['notices'][0]['client'],'workbuddy')
        now[0]+=60
        self.assertEqual(c.snapshot()['unreadCompletions'],1)
        c.mark_notice_shown(1)
        self.assertTrue(c.snapshot()['notices'][0]['shown'])
        self.assertEqual(c.snapshot()['unreadCompletions'],1)
        c.acknowledge_notice(1)
        self.assertEqual(c.snapshot()['unreadCompletions'],0)

    def test_completions_deduplicate_and_new_round_notifies(self):
        c=Core()
        c.ingest('workbuddy','s','completed','Stop');c.ingest('workbuddy','s','completed','Stop')
        self.assertEqual(c.snapshot()['unreadCompletions'],1)
        c.ingest('workbuddy','s','thinking','UserPromptSubmit')
        c.ingest('workbuddy','s','completed','Stop')
        self.assertEqual(c.snapshot()['unreadCompletions'],2)
        c.ingest('codex','s','completed','TaskComplete',completion_id='turn-1')
        c.ingest('codex','s','thinking','Reasoning')
        c.ingest('codex','s','completed','TaskComplete',completion_id='turn-1')
        self.assertEqual(c.snapshot()['unreadCompletions'],3)
        c.ingest('codex','s','completed','TaskComplete',completion_id='turn-2')
        self.assertEqual(c.snapshot()['unreadCompletions'],4)


class CharacterTests(Fixture):
    def test_builtin_kitten_has_six_distinct_state_clips_and_static_fallback(self):
        spec=character_spec('tabby')
        self.assertIsNotNone(spec)
        self.assertEqual(set(spec['states']),set(STATES))
        self.assertEqual(set(spec['states'].values()),{0})
        self.assertEqual(len({spec['clips'][name]['file'] for name in STATES}),6)
        self.assertTrue(all(spec['clips'][name]['file'].is_file() for name in STATES))
        self.assertTrue(spec['image'].is_file())
        self.assertEqual(len(set(character_spec('catgirl')['states'].values())),6)
        self.assertIsNone(character_spec('whale'))
        self.assertIsNone(character_spec('../tabby'))

    def test_missing_or_unsafe_atlas_falls_back_without_loading_other_files(self):
        self.assertIsNone(character_spec('tabby', self.base))
        folder=self.base/'characters/tabby';folder.mkdir(parents=True)
        (folder/'atlas.png').write_bytes(b'fixture')
        obj={'image':'../outside.png','columns':3,'rows':2,'states':dict(zip(STATES,range(6)))}
        metadata=folder/'character.json';metadata.write_text(json.dumps(obj))
        self.assertIsNone(character_spec('tabby', self.base))
        obj.update(image='atlas.png',columns=0);metadata.write_text(json.dumps(obj))
        self.assertIsNone(character_spec('tabby', self.base))
        obj['columns']=3;obj['states']['completed']=6;metadata.write_text(json.dumps(obj))
        self.assertIsNone(character_spec('tabby', self.base))

    def test_odd_atlas_dimensions_cover_each_pixel_once(self):
        rects=[atlas_rect(7,5,3,2,index) for index in range(6)]
        pixels=[(x,y) for left,top,width,height in rects
                for x in range(left,left+width) for y in range(top,top+height)]
        self.assertEqual(len(pixels),35)
        self.assertEqual(len(set(pixels)),35)
        self.assertEqual(set(pixels),{(x,y) for x in range(7) for y in range(5)})
        with self.assertRaises(ValueError):atlas_rect(7,5,3,2,6)


class HookTests(Fixture):
    def setUp(self):
        super().setUp()
        # Historical merge/uninstall logic only, not host shell compatibility.
        guard=patch('starpet.hooks.INSTALL_PAUSED',False)
        guard.start();self.addCleanup(guard.stop)
    def test_unread_notices_survive_restart_without_replaying_popup(self):
        target=self.base/'notices.json';c=Core(notice_path=target)
        c.ingest('workbuddy','private-session','completed','Stop')
        restored=Core(notice_path=target)
        self.assertEqual(restored.snapshot()['unreadCompletions'],1)
        self.assertTrue(restored.snapshot()['notices'][0]['shown'])
        self.assertNotIn('private-session',target.read_text(encoding='utf-8'))
        restored.acknowledge_notice()
        self.assertEqual(Core(notice_path=target).snapshot()['unreadCompletions'],0)

    def test_merge_repeat_and_remove_preserve_existing_stop_hook(self):
        profile = self.base / "profile"; wb = profile / ".workbuddy"; wb.mkdir(parents=True)
        state = self.base / "state"
        original = {"model": "keep-model", "hooks": {"Stop": [{"matcher": "original", "hooks": [
            {"type": "command", "command": "existing-other-hook"}]}]}, "unrelated": {"secret": "KEEP"}}
        settings = wb / "settings.json"; raw = json.dumps(original, indent=3).encode(); settings.write_bytes(raw)
        cmd = ["C:/App's Folder/StarPet.exe"]
        installed = configure(profile, state, command=cmd)
        self.assertEqual(Path(installed["backup"]).read_bytes(), raw)
        first = settings.read_bytes()
        self.assertFalse(configure(profile, state, command=cmd)["changed"])
        self.assertEqual(first, settings.read_bytes())
        active = json.loads(first)
        self.assertEqual(active["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        for event in EVENTS:
            self.assertIn("App''s Folder", active["hooks"][event][-1]["hooks"][0]["command"])
        active["userAddedAfterInstall"] = True; settings.write_text(json.dumps(active))
        configure(profile, state, uninstall=True, command=cmd)
        final = read_json(settings)
        self.assertTrue(final.pop("userAddedAfterInstall")); self.assertEqual(final, original)

    def test_refuse_invalid_and_missing_profiles(self):
        with self.assertRaises(ValueError):
            configure(self.base, self.base / "state")
        wb = self.base / ".workbuddy"; wb.mkdir(); target = wb / "settings.json"; target.write_text("BROKEN")
        with self.assertRaises(ValueError):
            configure(self.base, self.base / "state")
        self.assertEqual(target.read_text(), "BROKEN")

    def test_concurrent_change_is_not_overwritten(self):
        wb = self.base / ".workbuddy"; wb.mkdir(); settings = wb / "settings.json"; settings.write_text('{}')
        from starpet.storage import atomic_json
        def external_write(path, data):
            atomic_json(path, data)
            if path.name == "hook-manifest.json":
                settings.write_text('{"newUserSetting":true}')
        with patch("starpet.hooks.atomic_json", side_effect=external_write):
            with self.assertRaises(ValueError):
                configure(self.base, self.base / "state")
        self.assertEqual(read_json(settings), {"newUserSetting": True})


class RolloutTests(Fixture):
    def init_watcher(self, source=None):
        root = self.base / "sessions"; root.mkdir()
        self.file = root / "rollout-test.jsonl"
        meta = {"type": "session_meta", "payload": {"id": "session-test", "originator": "codex_work_desktop"}}
        if source:
            meta["payload"]["source"] = source
        self.file.write_text(json.dumps(meta) + '\n', encoding="utf-8")
        self.core = Core(); self.w = CodexWatcher(root, self.core); self.w.tick()

    def append(self, kind, payload):
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": kind, "payload": payload}, ensure_ascii=False) + '\n')
        self.w.tick()

    def test_history_not_replayed_then_live_state_and_context(self):
        self.init_watcher()
        self.assertEqual(self.core.snapshot()["sessions"], [])
        self.append("event_msg", {"type": "task_started"})
        self.append("response_item", {"type": "function_call", "name": "exec_command", "arguments": "PRIVATE-COMMAND"})
        self.append("response_item", {"type": "function_call_output", "output": "PRIVATE-RESULT"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "working")
        self.append("event_msg", {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 20}, "model_context_window": 100}})
        self.assertEqual(self.core.snapshot()["active"]["contextPercent"], 20)
        self.append("event_msg", {"type": "task_complete", "last_agent_message": "PRIVATE-REPLY"})
        snap = self.core.snapshot(); self.assertEqual(snap["active"]["state"], "completed")
        self.assertNotIn("PRIVATE", json.dumps(snap))

    def test_partial_utf8_and_json_line_are_carried(self):
        self.init_watcher()
        data = json.dumps({"type": "event_msg", "payload": {"type": "task_started", "message": "中文"}}, ensure_ascii=False).encode()
        cut = data.index("中".encode()) + 1
        with self.file.open('ab') as f: f.write(data[:cut])
        self.w.tick(); self.assertEqual(len(self.core.sessions), 0)
        with self.file.open('ab') as f: f.write(data[cut:] + b'\n')
        self.w.tick(); self.assertEqual(self.core.snapshot()["active"]["state"], "thinking")

    def test_subagents_ignored(self):
        self.init_watcher({"subagent": {"kind": "review"}})
        self.append("event_msg", {"type": "task_started"})
        self.assertEqual(len(self.core.sessions), 0)

    def test_waiting_result_and_interruption(self):
        self.init_watcher()
        self.append("response_item", {"type": "function_call", "name": "functions.request_user_input"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "waiting")
        self.append("response_item", {"type": "reasoning"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "waiting")
        self.append("response_item", {"type": "function_call_output"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "working")
        self.append("event_msg", {"type": "turn_aborted"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "interrupted")

    def test_truncation_does_not_replay_old_completion(self):
        self.init_watcher(); self.append("event_msg", {"type": "task_started"})
        self.file.write_text('{}\n')
        self.w.tick(); self.assertEqual(self.core.snapshot()["active"]["state"], "unknown")

    def test_giant_line_memory_and_reads_are_bounded(self):
        self.init_watcher()
        with self.file.open('ab') as f: f.write(b'x' * (MAX_LINE + MAX_CHUNK * 2) + b'\n')
        for _ in range(8):
            before = self.w.bytes_read; self.w.tick()
            self.assertLessEqual(self.w.bytes_read - before, MAX_CHUNK + MAX_LINE)
            self.assertLessEqual(len(self.w.files[self.file]["carry"]), MAX_LINE)
        self.append("event_msg", {"type": "task_started"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "thinking")

    def test_pause_resume_skips_history_during_pause(self):
        self.init_watcher()
        self.core.set_enabled("codex", False); self.w.tick()
        self.append("event_msg", {"type": "task_complete"})
        self.core.set_enabled("codex", True); self.w.tick()
        self.assertEqual(self.core.snapshot()["sessions"], [])
        self.append("event_msg", {"type": "task_started"})
        self.assertEqual(self.core.snapshot()["active"]["state"], "thinking")


class TransportTests(Fixture):
    def test_authenticated_hook_roundtrip_and_absence_fail_open(self):
        c = Core(); ipc = IPC(c, self.base); ipc.start()
        try:
            self.assertTrue(emit_hook("PreToolUse", {"session_id": "wb-s", "prompt": "PRIVATE"}, self.base))
            snap = request(read_json(self.base / "runtime.json"), "/snapshot")
            self.assertEqual(snap["active"]["state"], "working")
            self.assertNotIn("PRIVATE", json.dumps(snap)); self.assertNotIn("wb-s", json.dumps(snap))
            runtime = read_json(self.base / "runtime.json"); runtime["token"] = "bad"
            self.assertIsNone(request(runtime, "/snapshot"))
        finally:
            ipc.stop()
        before = time.monotonic()
        self.assertFalse(emit_hook("Stop", {"session_id": "s"}, self.base))
        self.assertLess(time.monotonic() - before, .8)

    def test_real_cli_hook_process_does_not_load_gui(self):
        core = Core(); ipc = IPC(core, self.base); ipc.start()
        try:
            env = dict(os.environ, PYTHONUTF8="1")
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--state-dir", str(self.base),
                                     MARKER, "Stop"], input=json.dumps({"session_id": "fixture", "prompt": "DO-NOT-EXPORT"}),
                                    capture_output=True, text=True, timeout=5, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(core.snapshot()["active"]["state"], "completed")
        finally:
            ipc.stop()


if __name__ == '__main__':
    unittest.main()
