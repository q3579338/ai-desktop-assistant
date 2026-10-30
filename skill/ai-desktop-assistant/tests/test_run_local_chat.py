"""Local HTTP contract checks; no torch, model, credentials, or upstream service."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / 'scripts/run_local_chat.py'
spec = importlib.util.spec_from_file_location('run_local_chat_under_test', SCRIPT)
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


def payload(text='你好', **changes):
    value = {'model': local.MODEL_NAME, 'messages': [{'role': 'user', 'content': text}],
             'stream': False, 'options': {'num_predict': 80}}
    value.update(changes)
    return value


class FakeEngine:
    def __init__(self, reply=None):
        self.model_name = local.MODEL_NAME
        self.model = None
        self.device = 'cpu'
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.calls = []
        self.reply_function = reply

    def reply(self, messages, maximum, disconnected):
        self.calls.append((messages, maximum))
        if self.reply_function:
            return self.reply_function(messages, maximum, disconnected)
        return {'model': self.model_name, 'message': {'role': 'assistant', 'content': '我在。'},
                'done': True, 'done_reason': 'stop'}


@contextlib.contextmanager
def running_server(engine=None):
    engine = engine or FakeEngine()
    server = local.LocalServer(0, engine)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield server, engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        if thread.is_alive():
            raise AssertionError('Isolated HTTP server did not stop')


def request(server, method='POST', path='/api/chat', value=None, body=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        if body is None and method == 'POST':
            body = json.dumps(payload() if value is None else value, ensure_ascii=False).encode('utf-8')
        request_headers = {'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


def raw_request(server, headers, body=b'', method='POST', path='/api/chat', half_close=False):
    with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as stream:
        head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers) + '\r\n'
        stream.sendall(head.encode('ascii') + body)
        if half_close:
            stream.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(stream)
        response.begin()
        return response.status, dict(response.getheaders()), json.loads(response.read())


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return bool(predicate())


@contextlib.contextmanager
def fixture_directory():
    base = Path(os.environ.get('STARPET_SKILL_TEST_TMPDIR') or tempfile.gettempdir()).resolve()
    if base == SKILL or SKILL in base.parents:
        raise RuntimeError('Local chat tests require a temporary directory outside the Skill installation')
    base.mkdir(parents=True, exist_ok=True)
    folder = base / ('local-chat-fixture-' + uuid.uuid4().hex[:10])
    folder.mkdir()
    try:
        yield folder
    finally:
        resolved = folder.resolve()
        assert resolved.parent == base and resolved.name.startswith('local-chat-fixture-')
        shutil.rmtree(resolved)


class LocalChatHTTPTests(unittest.TestCase):
    def test_health_and_tags_are_lazy_and_do_not_keep_idle_process_alive(self):
        with running_server() as (server, engine):
            before = engine.last_activity
            self.assertEqual(server.server_address[0], '127.0.0.1')
            for path in ('/health', '/api/tags'):
                status, headers, body = request(server, 'GET', path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['Connection'], 'close')
                self.assertNotIn('Access-Control-Allow-Origin', headers)
                if path == '/health':
                    self.assertFalse(body['loaded'])
                    self.assertFalse(body['busy'])
                else:
                    self.assertEqual([m['name'] for m in body['models']], [local.MODEL_NAME])
            self.assertEqual(engine.calls, [])
            self.assertEqual(engine.last_activity, before)

    def test_chinese_roundtrip_limits_output_and_updates_activity(self):
        with running_server() as (server, engine):
            engine.last_activity = 0
            status, _, body = request(server, value=payload('请记得我叫小灯', options={'num_predict': 9000}))
            self.assertEqual(status, 200)
            self.assertEqual(body['message']['content'], '我在。')
            self.assertEqual(engine.calls, [([{'role': 'user', 'content': '请记得我叫小灯'}], 1024)])
            self.assertTrue(eventually(lambda: not engine.lock.locked()))
            self.assertGreater(engine.last_activity, 0)

    def test_host_must_be_one_exact_loopback_authority(self):
        with running_server() as (server, engine):
            right = f'127.0.0.1:{server.server_port}'
            variants = [[], [('Host', 'localhost:' + str(server.server_port))],
                        [('Host', 'evil.example')], [('Host', '127.0.0.1:1')],
                        [('Host', right), ('Host', right)], [('Host', right), ('Host', 'evil.example')]]
            for headers in variants:
                with self.subTest(headers=headers):
                    status, _, _ = raw_request(server, headers, method='GET', path='/health')
                    self.assertEqual(status, 403)
            self.assertEqual(engine.calls, [])

    def test_browser_origin_and_fetch_headers_rejected_on_get_and_post(self):
        with running_server() as (server, engine):
            for headers in ({'Origin': 'https://example.com'}, {'Origin': 'null'}, {'Origin': ''},
                            {'Sec-Fetch-Site': 'same-origin'}, {'Sec-Fetch-Site': 'none'}):
                for method, path in (('GET', '/api/tags'), ('POST', '/api/chat')):
                    with self.subTest(headers=headers, method=method):
                        self.assertEqual(request(server, method, path, headers=headers)[0], 403)
            self.assertEqual(engine.calls, [])

    def test_only_exact_supported_routes_reach_the_engine(self):
        with running_server() as (server, engine):
            for method, path in (('GET', '/api/chat'), ('GET', '/health?prompt=secret'),
                                 ('POST', '/api/chat/'), ('POST', '/api/chat?model=x'),
                                 ('POST', '/api/generate')):
                self.assertEqual(request(server, method, path)[0], 404)
            self.assertEqual(engine.calls, [])

    def test_json_framing_rejects_chunked_duplicate_missing_and_oversize_length(self):
        with running_server() as (server, engine):
            base = [('Host', f'127.0.0.1:{server.server_port}'), ('Content-Type', 'application/json')]
            invalid = [[], [('Content-Length', '0')], [('Content-Length', '-1')],
                       [('Content-Length', 'invalid')], [('Content-Length', str(local.MAX_BODY + 1))],
                       [('Content-Length', '2'), ('Content-Length', '2')],
                       [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')]]
            for extra in invalid:
                with self.subTest(headers=extra):
                    self.assertEqual(raw_request(server, base + extra, b'{}')[0], 400)
            self.assertEqual(request(server, headers={'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(engine.calls, [])

    def test_body_byte_limit_accepts_exact_limit_and_rejects_invalid_or_truncated_json(self):
        with running_server() as (server, engine):
            content = json.dumps(payload()).encode()
            padded = content + b' ' * (local.MAX_BODY - len(content))
            self.assertEqual(request(server, body=padded)[0], 200)
            for body in (b'{', b'\xff', b'null', b'[]'):
                with self.subTest(body=body):
                    self.assertEqual(request(server, body=body)[0], 400)
            headers = [('Host', f'127.0.0.1:{server.server_port}'), ('Content-Type', 'application/json'),
                       ('Content-Length', str(len(content) + 3))]
            self.assertEqual(raw_request(server, headers, content, half_close=True)[0], 400)
            self.assertEqual(len(engine.calls), 1)

    def test_request_schema_rejects_nontext_tools_remote_models_and_invalid_limits(self):
        invalid = [payload(model='cloud-model'), payload(stream=True), payload(tools=[]),
                   payload(messages=[]), payload(messages=[{'role': 'tool', 'content': 'x'}]),
                   payload(messages=[{'role': 'assistant', 'content': 'x'}]),
                   payload(messages=[{'role': 'user', 'content': ['image']}]),
                   payload(messages=[{'role': 'user', 'content': 'x', 'images': []}]),
                   payload(options={'temperature': .7}), payload(options=[]),
                   payload(options={'num_predict': True}), payload(options={'num_predict': 0}),
                   payload(options={'num_predict': 1.5}), payload(messages=[{'role': 'user', 'content': 'x'}] * 51),
                   payload('x' * 32001), payload(messages=[{'role': 'system', 'content': 'x' * 32000},
                                                         {'role': 'user', 'content': 'y' * 8001}])]
        with running_server() as (server, engine):
            for index, value in enumerate(invalid):
                with self.subTest(case=index):
                    self.assertEqual(request(server, value=value)[0], 400)
            self.assertEqual(engine.calls, [])

    def test_exact_message_and_character_limits_are_accepted(self):
        with running_server() as (server, engine):
            messages = [{'role': 'system', 'content': 'x' * 32000}, {'role': 'user', 'content': 'y' * 8000}]
            self.assertEqual(request(server, value=payload(messages=messages))[0], 200)
            self.assertEqual(request(server, value=payload(messages=[{'role': 'user', 'content': 'x'}] * 50))[0], 200)
            self.assertEqual(len(engine.calls), 2)

    def test_busy_request_returns_429_without_queueing_and_health_remains_available(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release the engine')
            return {'message': {'role': 'assistant', 'content': '完成'}, 'done': True}
        engine = FakeEngine(blocking)
        with running_server(engine) as (server, _):
            result = []
            worker = threading.Thread(target=lambda: result.append(request(server)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(request(server, 'GET', '/health')[2]['busy'])
                self.assertEqual(request(server)[0], 429)
                self.assertEqual(len(engine.calls), 1)
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0][0], 200)
            self.assertTrue(eventually(lambda: not engine.lock.locked()))

    def test_connection_slots_are_bounded_and_recover_after_stalled_clients_close(self):
        with running_server() as (server, engine):
            streams = []
            try:
                # Four incomplete requests occupy the bounded HTTP workers,
                # without ever reaching a model or waiting for its timeout.
                for _ in range(4):
                    stream = socket.create_connection(('127.0.0.1', server.server_port), timeout=3)
                    streams.append(stream)
                    stream.sendall(b'GET /health HTTP/1.1\r\n')
                self.assertTrue(eventually(lambda: server.slots._value == 0))
                with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as excess:
                    self.assertEqual(excess.recv(1), b'')
            finally:
                for stream in streams:
                    stream.close()
            self.assertTrue(eventually(lambda: server.slots._value == 4))
            self.assertEqual(request(server, 'GET', '/health')[0], 200)
            self.assertEqual(engine.calls, [])

    def test_disconnected_client_cancels_inference_and_releases_engine_for_next_request(self):
        entered, cancelled = threading.Event(), threading.Event()
        def until_disconnect(messages, maximum, disconnected):
            entered.set()
            if eventually(disconnected):
                cancelled.set()
                raise ConnectionAbortedError()
            raise AssertionError('Disconnected socket was not observed')
        with running_server(FakeEngine(until_disconnect)) as (server, engine):
            content = json.dumps(payload('取消测试')).encode()
            stream = socket.create_connection(('127.0.0.1', server.server_port), timeout=3)
            head = (f'POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'
                    f'Content-Type: application/json\r\nContent-Length: {len(content)}\r\n\r\n')
            try:
                stream.sendall(head.encode() + content)
                self.assertTrue(entered.wait(2))
            finally:
                stream.close()
            self.assertTrue(cancelled.wait(2))
            self.assertTrue(eventually(lambda: not engine.lock.locked()))
            engine.reply_function = None
            self.assertEqual(request(server)[0], 200)

    def test_engine_failures_release_lock_and_do_not_expose_prompts_or_paths(self):
        secret = 'private-text-and-private-model-path'
        for failure, expected in ((ValueError, 400), (TimeoutError, 408),
                                  (ConnectionAbortedError, 408), (RuntimeError, 503)):
            def fail(*args):
                raise failure(secret)
            with self.subTest(failure=failure.__name__), running_server(FakeEngine(fail)) as (server, engine):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    status, _, body = request(server, value=payload(secret))
                self.assertEqual(status, expected)
                self.assertNotIn(secret, json.dumps(body) + output.getvalue())
                self.assertTrue(eventually(lambda: not engine.lock.locked()))
                engine.reply_function = None
                self.assertEqual(request(server)[0], 200)


class LocalChatLifecycleTests(unittest.TestCase):
    def run_idle_main(self, initially_busy=False):
        clock = [0]
        engine = FakeEngine()
        engine.last_activity = 0
        if initially_busy:
            engine.lock.acquire()
        servers, hooks = [], []
        class FakeServer:
            def __init__(self, port, actual_engine):
                self.port = port
                self.engine = actual_engine
                self.calls = 0
                self.closed = False
                servers.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.closed = True
            def handle_request(self):
                self.calls += 1
                clock[0] += 10
                if initially_busy and clock[0] == 40:
                    engine.last_activity = clock[0]
                    engine.lock.release()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['run_local_chat.py', '--model-dir', str(SCRIPT.parent),
                                       '--port', '11536', '--idle-seconds', '30']), \
             patch.object(local, 'model_files', return_value=SCRIPT.parent), \
             patch.object(local, 'LocalEngine', return_value=engine), \
             patch.object(local, 'LocalServer', FakeServer), \
             patch.object(local.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(sys, 'addaudithook', side_effect=hooks.append), \
             patch.dict(os.environ, {}), contextlib.redirect_stdout(output):
            local.main()
            self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
            self.assertEqual(os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')
            self.assertEqual(os.environ['TRANSFORMERS_OFFLINE'], '1')
        self.assertEqual(len(hooks), 1)
        with self.assertRaises(OSError):
            hooks[0]('socket.connect', ())
        hooks[0]('unrelated.event', ())
        self.assertTrue(servers[0].closed)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(lines[0]['ready'])
        self.assertEqual(lines[-1], {'stopped': 'idle_timeout'})
        return servers[0].calls, clock[0]

    def test_idle_main_exits_without_loading_model(self):
        self.assertEqual(self.run_idle_main(), (3, 30))

    def test_busy_main_does_not_exit_until_full_idle_interval_after_reply(self):
        self.assertEqual(self.run_idle_main(initially_busy=True), (7, 70))

    def test_cli_rejects_unsafe_port_and_idle_values_before_model_access(self):
        for args in (['--port', '0'], ['--port', '65536'], ['--idle-seconds', '29'], ['--idle-seconds', '3601']):
            with self.subTest(args=args), patch.object(sys, 'argv', ['run_local_chat.py', '--model-dir', 'unused', *args]), \
                 patch.object(local, 'model_files') as files, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    local.main()
                self.assertEqual(raised.exception.code, 2)
                files.assert_not_called()

    def test_model_inventory_rejects_wrong_architecture_missing_files_and_path_escape(self):
        with fixture_directory() as root:
            def write_index(name='weights.safetensors', model_type='qwen3'):
                (root/'config.json').write_text(json.dumps({'model_type': model_type, 'hidden_size': 2560,
                    'num_hidden_layers': 36, 'vocab_size': 151936}), encoding='utf-8')
                (root/'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'weight': name}}), encoding='utf-8')
            for name in ('tokenizer.json', 'tokenizer_config.json', 'weights.safetensors'):
                (root/name).write_text('{}', encoding='utf-8')
            write_index()
            self.assertEqual(local.model_files(root), root.resolve())
            for name, arch in (('../outside.safetensors', 'qwen3'), ('..\\outside.safetensors', 'qwen3'),
                               ('missing.safetensors', 'qwen3'), ('weights.bin', 'qwen3'),
                               ('weights.safetensors', 'other')):
                with self.subTest(name=name, arch=arch):
                    write_index(name, arch)
                    with self.assertRaises(ValueError):
                        local.model_files(root)

    def test_model_metadata_links_are_rejected_before_reading_them(self):
        # Simulate the file-link metadata rather than requiring Windows link
        # creation privileges. No target outside the fixture is ever accessed.
        with fixture_directory() as root:
            (root/'config.json').write_text(json.dumps({'model_type': 'qwen3', 'hidden_size': 2560,
                'num_hidden_layers': 36, 'vocab_size': 151936}), encoding='utf-8')
            (root/'model.safetensors.index.json').write_text(json.dumps({
                'weight_map': {'weight': 'weights.safetensors'}}), encoding='utf-8')
            for name in ('tokenizer.json', 'tokenizer_config.json', 'weights.safetensors'):
                (root/name).write_text('{}', encoding='utf-8')
            read_text = Path.read_text
            for name in ('config.json', 'model.safetensors.index.json'):
                reads = []
                def tracked_read(path, *args, **kwargs):
                    reads.append(path.name)
                    return read_text(path, *args, **kwargs)
                with self.subTest(name=name), \
                     patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == root/name), \
                     patch.object(Path, 'read_text', autospec=True, side_effect=tracked_read):
                    with self.assertRaises(ValueError):
                        local.model_files(root)
                self.assertNotIn(name, reads)


if __name__ == '__main__':
    unittest.main()
