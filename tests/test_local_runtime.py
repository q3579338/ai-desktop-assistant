import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from starpet import local_runtime as runtime


class Process:
    def __init__(self, wait_timeout=False):
        self.returncode = None
        self.terminated = self.killed = 0
        self.wait_timeout = wait_timeout

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout):
        if self.wait_timeout and not self.killed:
            raise subprocess.TimeoutExpired('owned fixture', timeout)
        self.returncode = 0
        return 0


HEALTH = {'ok': True, 'engine': 'transformers', 'model': 'qwen3-4b-local',
          'loaded': False, 'busy': False, 'device': 'cuda'}


@contextlib.contextmanager
def local_server(payload=None, status=200, delay=0, extra_headers=None):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Host')))
            time.sleep(delay)
            data = json.dumps(HEALTH if payload is None else payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(runtime, 'ADDRESS', server.server_address):
            yield seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        # Normal inherited ACLs; TemporaryDirectory's 0700 is not usable by
        # Windows restricted-token test runners in this workspace.
        self.root = Path(__file__).resolve().parents[1] / 'evidence' / 'tests' / ('local-runtime-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.python = self.root / ('python.exe' if os.name == 'nt' else 'python3')
        self.python.write_bytes(b'not executed: fixture')
        self.script = self.root / 'run_local_chat.py'
        self.script.write_text('# not executed: fixture', encoding='utf-8')
        self.model = self.root / 'model'
        self.model.mkdir()
        self.deps = self.root / 'dependencies'
        self.deps.mkdir()
        self.config = {'schema': 1, 'python': str(self.python), 'script': str(self.script),
                       'modelDir': str(self.model), 'dependencyDir': str(self.deps), 'url': runtime.URL}
        self.owner = runtime.LocalRuntime(self.state)

    def tearDown(self):
        # Only this test's known resolved child is removed; never a configured
        # model directory or a production state path.
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parents[1] / 'evidence' / 'tests'))
        shutil.rmtree(self.root)

    def configure(self, **overrides):
        value = {**self.config, **overrides}
        (self.state / runtime.CONFIG_NAME).write_text(json.dumps(value), encoding='utf-8')

    def start_mocked(self, process=None):
        process = process or Process()
        with patch.object(runtime.socket, 'socket') as socket_call, patch.object(runtime.subprocess, 'Popen', return_value=process) as popen:
            result = self.owner.start()
        return process, result, socket_call, popen

    def test_construct_and_missing_configuration_never_start_or_probe(self):
        with patch.object(runtime.subprocess, 'Popen') as popen, patch.object(runtime, '_health') as health:
            self.assertEqual(self.owner.status()['status'], 'not_installed')
            with self.assertRaises(runtime.LocalRuntimeError):
                self.owner.start()
        popen.assert_not_called()
        health.assert_not_called()
        self.assertFalse((self.state / runtime.CONFIG_NAME).exists())

    def test_invalid_schema_urls_and_paths_fail_before_spawn(self):
        cases = [{'schema': True}, {'schema': 2}, {'url': 'http://localhost:11536'},
                 {'url': 'http://127.0.0.1:11434'}, {'url': 'https://remote.example'},
                 {'modelDir': 'relative/model'}, {'dependencyDir': str(self.root / 'missing')},
                 {'script': str(self.python)}, {'python': str(self.script)},
                 {'modelDir': str(self.root / 'model' / '..' / 'model')}]
        for invalid in cases:
            with self.subTest(invalid=invalid), patch.object(runtime.subprocess, 'Popen') as popen:
                self.configure(**invalid)
                with self.assertRaises(runtime.LocalRuntimeError):
                    self.owner.start()
                self.assertEqual(self.owner.status()['status'], 'error')
                popen.assert_not_called()

    def test_oversized_duplicate_and_unknown_configuration_rejected(self):
        descriptor = self.state / runtime.CONFIG_NAME
        for content in (' ' * 16385, '{"schema":1,"schema":1}', json.dumps({**self.config, 'command': 'unexpected'})):
            with self.subTest(content=content[:40]):
                descriptor.write_text(content, encoding='utf-8')
                with self.assertRaises(runtime.LocalRuntimeError):
                    self.owner.start()

    def test_reparse_ancestor_rejected_without_resolving_away_link(self):
        self.configure()
        actual = Path.lstat

        def marked(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if path == self.model:
                return Mock(st_mode=result.st_mode, st_file_attributes=0x400)
            return result

        with patch.object(Path, 'lstat', marked), self.assertRaises(runtime.LocalRuntimeError):
            self.owner.start()

    def test_start_is_hidden_offline_fixed_and_does_not_wait_for_health(self):
        self.configure()
        with patch.dict(os.environ, {'HF_TOKEN': 'private', 'HTTPS_PROXY': 'http://proxy.invalid',
                                     'ANTHROPIC_API_KEY': 'private', 'PYTHONPATH': 'untrusted'}), patch.object(runtime, '_health') as health:
            process, result, socket_call, popen = self.start_mocked()
        health.assert_not_called()
        self.assertEqual(result['status'], 'loading')
        self.assertTrue(result['owned'] and result['running'])
        socket_call.return_value.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', 11536))
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(self.python), '-I', '-B', '-u', str(self.script),
                                  '--model-dir', str(self.model), '--dependency-dir', str(self.deps),
                                  '--port', '11536', '--idle-seconds', '600'])
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs['shell'])
        self.assertTrue(kwargs['close_fds'])
        self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        for key in ('HF_TOKEN', 'HTTPS_PROXY', 'ANTHROPIC_API_KEY', 'PYTHONPATH'):
            self.assertNotIn(key, kwargs['env'])
        self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'], '1')
        self.assertIs(self.owner._process, process)

    def test_existing_port_and_failed_spawn_do_not_adopt_or_stop_anything(self):
        self.configure()
        with patch.object(runtime.socket, 'socket') as socket_call, patch.object(runtime.subprocess, 'Popen') as popen:
            socket_call.return_value.__enter__.return_value.bind.side_effect = OSError('occupied')
            with self.assertRaises(runtime.LocalRuntimeError):
                self.owner.start()
            popen.assert_not_called()
        with patch.object(runtime, '_health', side_effect=OSError('offline')):
            self.assertFalse(self.owner.stop()['owned'])
        with patch.object(runtime.socket, 'socket'), patch.object(runtime.subprocess, 'Popen', side_effect=OSError('fixture')):
            with self.assertRaises(runtime.LocalRuntimeError):
                self.owner.start()
        with patch.object(runtime, '_health', side_effect=OSError('offline')):
            self.assertFalse(self.owner.stop()['owned'])

    def test_start_twice_does_not_spawn_twice_and_exited_child_can_restart(self):
        self.configure()
        process, *_ = self.start_mocked()
        with patch.object(runtime.subprocess, 'Popen') as popen:
            self.assertTrue(self.owner.start()['owned'])
            popen.assert_not_called()
        process.returncode = 1
        replacement, *_ = self.start_mocked()
        self.assertIs(self.owner._process, replacement)

    def test_immediate_child_exit_and_missing_descriptor_report_actual_ownership(self):
        self.configure()
        exited = Process()
        exited.returncode = 1
        _, result, *_ = self.start_mocked(exited)
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['owned'] or result['running'])
        process, *_ = self.start_mocked()
        (self.state / runtime.CONFIG_NAME).unlink()
        result = self.owner.status()
        self.assertFalse(result['installed'])
        self.assertTrue(result['owned'] and result['running'])
        self.owner.close()
        self.assertEqual(process.terminated, 1)

    def test_health_state_mapping_including_lazy_loading(self):
        self.configure()
        for loaded, busy, expected in ((False, False, 'available'), (False, True, 'loading'),
                                       (True, True, 'busy'), (True, False, 'available')):
            with self.subTest(loaded=loaded, busy=busy), patch.object(runtime, '_health', return_value={**HEALTH, 'loaded': loaded, 'busy': busy}):
                result = self.owner.status()
                self.assertEqual(result['status'], expected)
                self.assertEqual((result['loaded'], result['busy']), (loaded, busy))
                self.assertTrue(result['running'])
                self.assertFalse(result['owned'])
        with patch.object(runtime, '_health', return_value=HEALTH):
            result = self.owner.stop()
        self.assertEqual(result['status'], 'available')
        self.assertTrue(result['running'])
        self.assertFalse(result['owned'])
        self.assertIn('未停止', result['message'])

    def test_failed_health_is_loading_only_for_own_live_child(self):
        self.configure()
        with patch.object(runtime, '_health', side_effect=OSError('offline')):
            self.assertEqual(self.owner.status()['status'], 'unavailable')
            process, *_ = self.start_mocked()
            self.assertEqual(self.owner.status()['status'], 'loading')
            process.returncode = 1
            result = self.owner.status()
            self.assertEqual(result['status'], 'unavailable')
            self.assertFalse(result['owned'] or result['running'])

    def test_stop_only_uses_current_owned_handle_and_can_kill_its_hung_child(self):
        self.configure()
        process, *_ = self.start_mocked(Process(wait_timeout=True))
        other = runtime.LocalRuntime(self.state)
        with patch.object(runtime, '_health', return_value=HEALTH):
            self.assertFalse(other.stop()['owned'])
        self.assertEqual(process.terminated, 0)
        result = self.owner.stop()
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(result['owned'] or result['running'])
        self.assertEqual((process.terminated, process.killed), (1, 1))
        with patch.object(runtime, '_health', side_effect=OSError('offline')):
            self.assertEqual(self.owner.stop()['status'], 'stopped')

    def test_close_wins_against_late_start_and_stops_inflight_start(self):
        self.configure()
        self.assertFalse(self.owner.close())
        with patch.object(runtime.subprocess, 'Popen') as popen, self.assertRaises(runtime.LocalRuntimeError):
            self.owner.start()
        popen.assert_not_called()
        self.owner = runtime.LocalRuntime(self.state)
        entered, release = threading.Event(), threading.Event()
        process = Process()

        def create(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return process

        with patch.object(runtime.socket, 'socket'), patch.object(runtime.subprocess, 'Popen', side_effect=create):
            starting = threading.Thread(target=self.owner.start)
            starting.start()
            self.assertTrue(entered.wait(2))
            closing = threading.Thread(target=self.owner.close)
            closing.start()
            release.set()
            starting.join(2)
            closing.join(2)
        self.assertFalse(starting.is_alive() or closing.is_alive())
        self.assertEqual(process.terminated, 1)
        with self.assertRaises(runtime.LocalRuntimeError):
            self.owner.start()

    def test_real_health_reads_only_loopback_health_and_ignores_proxy_environment(self):
        with local_server() as seen, patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1'}):
            self.assertEqual(runtime._health(), HEALTH)
        self.assertEqual(seen, [('/health', '127.0.0.1:11536')])

    def test_redirect_unknown_engine_and_large_output_are_rejected(self):
        cases = [(HEALTH, 302, {'Location': 'http://remote.invalid/never'}),
                 ({**HEALTH, 'engine': 'proxy'}, 200, {}), ({**HEALTH, 'model': 'other'}, 200, {}),
                 ({**HEALTH, 'busy': 'false'}, 200, {}), ({**HEALTH, 'padding': 'x' * 9000}, 200, {})]
        for payload, status, headers in cases:
            with self.subTest(status=status, model=payload.get('model')):
                with local_server(payload, status, extra_headers=headers) as seen, self.assertRaises(ValueError):
                    runtime._health()
                self.assertEqual(len(seen), 1)

    def test_health_deadline_is_bounded(self):
        with local_server(delay=0.8), self.assertRaises((TimeoutError, OSError)):
            started = time.monotonic()
            try:
                runtime._health()
            finally:
                self.assertLess(time.monotonic() - started, 0.7)


if __name__ == '__main__':
    unittest.main()
