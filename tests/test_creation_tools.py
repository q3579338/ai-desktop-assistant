"""Authoring adapters: local HTTP and process substitutes, never real models."""
import contextlib
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from starpet import creation_tools as tools


def png(rgba=False):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    row = b'\0' + (b'\x80\x90\xa0\0\x80\x90\xa0\xff' * 128 if rgba else b'\x80\x90\xa0' * 256)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 256, 8, 6 if rgba else 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(row * 256)) + chunk(b'IEND', b''))


@contextlib.contextmanager
def comfy(**options):
    tool = tools._tool('generate_local_image')
    state = {'requests': [], 'graph': None, 'id': None}

    class Handler(BaseHTTPRequestHandler):
        def route(self):
            path = urlsplit(self.path).path
            state['requests'].append((self.command, path))
            if options.get('redirect'):
                self.send_response(302)
                self.send_header('Location', 'http://192.0.2.1/secret-not-followed')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if path.startswith('/object_info/'):
                name = path.rsplit('/', 1)[-1]
                fields = {key: ['STRING'] for key in tool.NODES[name]}
                if name == 'CheckpointLoaderSimple':
                    fields['ckpt_name'] = [options.get('models', ['fixture.safetensors'])]
                if name == 'KSampler':
                    fields['sampler_name'], fields['scheduler'] = [['euler']], [['normal']]
                result = {name: {'python_module': options.get('module', 'nodes'), 'api_node': False,
                                 'input': {'required': fields}}}
            elif path == '/upload/image':
                raw = self.rfile.read(int(self.headers['Content-Length']))
                message = BytesParser(policy=default).parsebytes(
                    ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + raw)
                part = next(p for p in message.iter_parts() if p.get_param('name', header='content-disposition') == 'image')
                result = {'name': part.get_filename(), 'subfolder': '', 'type': 'input'}
            elif path == '/prompt':
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['graph'], state['id'] = value['prompt'], value['prompt_id']
                result = {'prompt_id': state['id']}
            elif path.startswith('/history/'):
                result = {} if options.get('pending') else {state['id']: {
                    'prompt': [0, state['id'], state['graph']],
                    'status': {'completed': True, 'status_str': 'success'},
                    'outputs': {'8': {'images': [{'filename': state['graph']['8']['inputs']['filename_prefix'] + '_00001_.png',
                                                 'subfolder': '', 'type': 'output'}]}}}}
            elif path == '/view':
                result = png()
            else:
                raise AssertionError('Unexpected fixture endpoint: ' + path)
            hook = options.get('hook')
            if hook:
                hook(path, state)
            body = result if isinstance(result, bytes) else json.dumps(result).encode()
            if path == '/prompt' and options.get('unknown_submission'):
                body = b'not valid JSON: credential=must-not-be-shown'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = route

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


class FakeProcess:
    def __init__(self, raw=b'', code=0):
        self.stdout = io.BytesIO(raw)
        self.returncode = code
        self.terminated = self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class CreationToolsTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'evidence' / 'tests'
        base.mkdir(parents=True, exist_ok=True)
        self.base = base.resolve()
        # Python 3.13's mode-0700 tempfile directory can exclude a Windows
        # restricted token. Inherit the explicitly allowed workspace ACL.
        self.folder = self.base / ('creation-tools-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.image = self.folder / 'input.png'
        self.image.write_bytes(png())
        self.output = self.folder / 'cutout.png'
        self.python = self.folder / ('python.exe' if os.name == 'nt' else 'python3')
        self.python.write_bytes(b'process substitute: never execute')
        self.model = self.folder / 'u2netp.onnx'
        self.model.write_bytes(b'unverified fixture, not a real model')
        self.job = tools.CreationJob()
        self.removal = tools._tool('remove_local_background')

    def tearDown(self):
        self.job.cancel()
        target = self.folder.resolve()
        if target.parent != self.base or not target.name.startswith('creation-tools-'):
            raise RuntimeError('Refuse to remove an unexpected fixture directory')
        shutil.rmtree(target)

    def generate(self, url, **extra):
        return tools.generate_image(self.image, self.folder / 'generated',
            {'url': url, 'checkpoint': 'fixture.safetensors', 'prompt': '测试原创角色', 'seed': 7,
             'poll_interval': .01, **extra}, self.job)

    def removal_result(self):
        report = self.output.with_name(self.output.stem + '.background.json')
        return {'schema': 1, 'status': 'completed', 'image': str(self.output), 'input': str(self.image),
            'report': str(report), 'input_sha256': hashlib.sha256(png()).hexdigest(),
            'output_sha256': hashlib.sha256(png(True)).hexdigest(), 'model_sha256': self.removal.MODEL_SHA256,
            'model_verified': True, 'width': 256, 'height': 256, 'alpha_min': 0, 'alpha_max': 255,
            'provider': 'CPUExecutionProvider', 'rgb_preserved': True, 'existing_alpha_preserved': True,
            'alpha_combination': 'multiply_existing_alpha', 'telemetry_disabled': True,
            'network_used': False, 'models_downloaded': False, 'credentials_read': False, 'custom_ops_registered': False}

    def fake_removal(self, result=None, *, report_change=None, output_bytes=None):
        result = self.removal_result() if result is None else result
        def spawn(command, **kwargs):
            self.output.write_bytes(png(True) if output_bytes is None else output_bytes)
            recorded = dict(result)
            if report_change:
                recorded.update(report_change)
            self.output.with_name(self.output.stem + '.background.json').write_text(json.dumps(recorded), encoding='utf-8')
            return FakeProcess(json.dumps(result).encode())
        return spawn

    def remove(self):
        return tools.remove_background(self.image, self.output, self.python, self.model, self.job)

    def test_private_package_ignores_global_module_names_without_sys_path_changes(self):
        before = list(sys.path)
        with patch.dict(sys.modules, {'generate_local_image': types.ModuleType('bad_global'),
                                      'probe_local_runtime': types.ModuleType('bad_global_probe')}):
            module = tools._tool('generate_local_image')
            self.assertTrue(module.__name__.startswith('_starpet_creation_'))
            self.assertEqual(Path(module.__file__).resolve().parent, tools._scripts_directory())
            self.assertEqual(module.parse_loopback_url('http://localhost', 8188).host, '127.0.0.1')
        self.assertEqual(sys.path, before)

    def test_frozen_load_uses_only_bundled_three_scripts_and_no_source_fallback(self):
        bundled = self.folder / 'creation-tools'
        bundled.mkdir()
        for name in tools._TOOL_NAMES:
            shutil.copyfile(tools._scripts_directory() / (name + '.py'), bundled / (name + '.py'))
        before = list(sys.path)
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(self.folder), create=True):
            module = tools._tool('generate_local_image')
            self.assertEqual(Path(module.__file__).parent, bundled)
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(self.folder / 'missing'), create=True):
            with self.assertRaises(tools.CreationToolError):
                tools._tool('generate_local_image')
        self.assertEqual(sys.path, before)

    def test_discovery_is_read_only_uses_core_node_and_safe_unique_names(self):
        with comfy(models=['fixture.safetensors', 'folder/second.safetensors', 'fixture.safetensors']) as (url, state):
            result = tools.discover_checkpoints(url, self.job)
        self.assertEqual(result, ['fixture.safetensors', 'folder/second.safetensors'])
        self.assertEqual(state['requests'], [('GET', '/object_info/CheckpointLoaderSimple')])
        with comfy(models=[]) as (url, _):
            self.assertEqual(tools.discover_checkpoints(url, self.job), [])

    def test_discovery_rejects_custom_nodes_unsafe_names_and_redirects(self):
        for options in ({'module': 'custom_nodes.cloud'}, {'models': ['../secret']}, {'redirect': True}):
            with self.subTest(options=options), comfy(**options) as (url, state):
                with self.assertRaises(tools.CreationToolError):
                    tools.discover_checkpoints(url, self.job)
                self.assertEqual(len(state['requests']), 1)

    def test_remote_or_credential_urls_never_open_connections(self):
        module = tools._tool('generate_local_image')
        with patch.object(module, 'request') as request:
            for url in ('https://example.com', 'http://user:secret@127.0.0.1', 'http://127.0.0.1?token=secret'):
                with self.subTest(url=url), self.assertRaises(tools.CreationToolError) as error:
                    tools.discover_checkpoints(url, self.job)
                self.assertNotIn('secret', str(error.exception))
            request.assert_not_called()

    def test_generate_reuses_workflow_and_preserves_original_and_existing_outputs(self):
        before = self.image.read_bytes()
        with comfy() as (url, state):
            first = self.generate(url)
            second = self.generate(url)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['art_style'], '2d_illustration')
        self.assertTrue(state['graph']['3']['inputs']['text'].startswith('2D hand-drawn illustration'))
        self.assertIn('测试原创角色', state['graph']['3']['inputs']['text'])
        self.assertIn('3D render', state['graph']['4']['inputs']['text'])
        self.assertEqual(self.image.read_bytes(), before)
        self.assertEqual(Path(first['image']).read_bytes(), png())
        self.assertNotEqual(first['report'], second['report'])
        self.assertEqual(json.loads(Path(first['report']).read_text())['output_sha256'], first['output_sha256'])
        self.assertEqual(sum(path == '/prompt' for _, path in state['requests']), 2)

    def test_unknown_options_and_precancel_do_no_network_or_subprocess(self):
        with patch('http.client.HTTPConnection') as http, patch('subprocess.Popen') as process:
            with self.assertRaises(tools.CreationToolError):
                tools.generate_image(self.image, self.folder, {'checkpoint': 'a', 'prompt': 'b', 'custom_node': 'x'}, self.job)
            self.job.cancel()
            for action in (lambda: self.generate('http://127.0.0.1:1'),
                           lambda: tools.discover_checkpoints('http://127.0.0.1:1', self.job), self.remove):
                with self.assertRaises(tools.CreationToolError):
                    action()
            http.assert_not_called()
            process.assert_not_called()

    def test_cancel_after_preflight_never_uploads(self):
        with comfy(hook=lambda path, _: self.job.cancel() if path == '/object_info/SaveImage' else None) as (url, state):
            with self.assertRaises(tools.CreationToolError):
                self.generate(url)
        self.assertTrue(all(method == 'GET' for method, _ in state['requests']))

    def test_cancel_after_upload_preserves_record_without_submitting(self):
        with comfy(hook=lambda path, _: self.job.cancel() if path == '/upload/image' else None) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url)
        record = json.loads(Path(error.exception.report).read_text())
        self.assertEqual(record['status'], 'client_stopped')
        self.assertFalse(record['server_may_continue'])
        self.assertIsNone(record['prompt_id'])
        self.assertFalse(any(path in ('/prompt', '/interrupt', '/queue') for _, path in state['requests']))

    def test_cancel_after_submit_keeps_id_and_never_polls_or_interrupts(self):
        with comfy(hook=lambda path, _: self.job.cancel() if path == '/prompt' else None) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url)
        record = json.loads(Path(error.exception.report).read_text())
        self.assertEqual(record['prompt_id'], state['id'])
        self.assertEqual(error.exception.prompt_id, state['id'])
        self.assertTrue(record['server_may_continue'])
        self.assertEqual(record['status'], 'client_stopped')
        self.assertFalse(any(path.startswith('/history') or path == '/interrupt' for _, path in state['requests']))

    def test_cancel_after_poll_does_not_fetch_image(self):
        with comfy(hook=lambda path, _: self.job.cancel() if path.startswith('/history/') else None) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url)
        self.assertTrue(Path(error.exception.report).is_file())
        self.assertFalse(any(path in ('/view', '/interrupt', '/queue') for _, path in state['requests']))

    def test_cancel_after_download_does_not_publish_an_image(self):
        with comfy(hook=lambda path, _: self.job.cancel() if path == '/view' else None) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url)
        record = json.loads(Path(error.exception.report).read_text())
        self.assertEqual(record['status'], 'client_stopped')
        self.assertFalse(record['server_may_continue'])
        self.assertFalse(list((self.folder / 'generated').glob('*/portrait.png')))
        self.assertFalse(any(path in ('/interrupt', '/queue') for _, path in state['requests']))

    def test_discovery_cancelled_during_response_returns_no_models(self):
        with comfy(hook=lambda *_: self.job.cancel()) as (url, _):
            with self.assertRaises(tools.CreationToolError):
                tools.discover_checkpoints(url, self.job)

    def test_cancel_wakes_poll_interval_and_retains_job(self):
        timers = []
        def hook(path, _):
            if path.startswith('/history/'):
                timer = threading.Timer(.05, self.job.cancel)
                timers.append(timer)
                timer.start()
        started = time.monotonic()
        with comfy(pending=True, hook=hook) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url, poll_interval=5)
        for timer in timers:
            timer.join(1)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(Path(error.exception.report).is_file())
        self.assertEqual(sum(path == '/prompt' for _, path in state['requests']), 1)

    def test_unknown_submission_is_not_retried_and_exposes_only_recovery_path(self):
        with comfy(unknown_submission=True) as (url, state):
            with self.assertRaises(tools.CreationToolError) as error:
                self.generate(url)
        record = json.loads(Path(error.exception.report).read_text())
        self.assertEqual(record['status'], 'submission_unknown')
        self.assertTrue(record['requested_prompt_id'])
        self.assertTrue(record['server_may_continue'])
        self.assertEqual(sum(path == '/prompt' for _, path in state['requests']), 1)
        self.assertNotIn('credential', str(error.exception))

    def test_unverified_model_and_existing_output_fail_before_process(self):
        with patch('subprocess.Popen') as process:
            with self.assertRaises(tools.CreationToolError):
                self.remove()
            self.model.write_bytes(b'\0' * self.removal.MODEL_BYTES)
            with self.assertRaisesRegex(tools.CreationToolError, '摘要不一致'):
                self.remove()
            self.output.write_bytes(b'existing result')
            with self.assertRaises(tools.CreationToolError):
                self.remove()
            process.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b'existing result')

    def test_background_uses_explicit_python_fixed_script_clean_env_and_verifies_files(self):
        env = {'PYTHONPATH': 'untrusted', 'PYTHONHOME': 'untrusted', 'HTTP_PROXY': 'secret-proxy',
               'OPENAI_API_KEY': 'secret', 'HF_TOKEN': 'secret', 'PATH': 'untrusted'}
        with patch.object(self.removal, '_model_data', return_value=b'verified substitute') as model_check, \
                patch.dict(os.environ, env), patch('subprocess.Popen', side_effect=self.fake_removal()) as process:
            result = self.remove()
        model_check.assert_called_once_with(self.model)
        command = process.call_args.args[0]
        self.assertEqual(command[:3], [str(self.python), '-I', '-B'])
        self.assertEqual(command[3:5], ['-X', 'utf8'])
        self.assertEqual(Path(command[5]), tools._scripts_directory() / 'remove_local_background.py')
        self.assertEqual(command[-2:], ['--model', str(self.model)])
        kwargs = process.call_args.kwargs
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['creationflags'], getattr(tools.subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(kwargs['cwd'], tools._scripts_directory())
        for key in ('PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'OPENAI_API_KEY', 'HF_TOKEN'):
            self.assertNotIn(key, kwargs['env'])
        self.assertNotIn('untrusted', kwargs['env']['PATH'])
        self.assertEqual(result, self.removal_result())
        self.assertEqual(self.image.read_bytes(), png())
        self.assertEqual(self.job._processes, set())

    def test_failure_message_never_exposes_arbitrary_child_details(self):
        payload = {'status': 'failed', 'reason': 'Traceback password=secret private-config'}
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                patch('subprocess.Popen', return_value=FakeProcess(json.dumps(payload).encode(), 1)):
            with self.assertRaises(tools.CreationToolError) as error:
                self.remove()
        self.assertNotIn('secret', str(error.exception))
        self.assertIn('Python', str(error.exception))

    def test_missing_dependencies_have_actionable_fixed_message(self):
        reason = '当前 Python 缺少 onnxruntime；请使用已配置的本机环境。'
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                patch('subprocess.Popen', return_value=FakeProcess(json.dumps({'status': 'failed', 'reason': reason}).encode(), 1)):
            with self.assertRaisesRegex(tools.CreationToolError, '缺少 onnxruntime'):
                self.remove()

    def test_success_exit_without_artifacts_is_not_success(self):
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                patch('subprocess.Popen', return_value=FakeProcess(json.dumps(self.removal_result()).encode())):
            with self.assertRaises(tools.CreationToolError):
                self.remove()

    def test_report_and_output_boundaries_are_not_trusted_from_stdout(self):
        cases = [({'model_sha256': '0' * 64}, None), ({'image': str(self.folder / 'other.png')}, None),
                 ({'input_sha256': '0' * 64}, None), ({'network_used': True}, None),
                 ({'provider': 'CUDAExecutionProvider'}, None), ({'schema': True}, None), ({}, b'not a png')]
        for change, output_bytes in cases:
            with self.subTest(change=change, output_bytes=output_bytes):
                result = {**self.removal_result(), **change}
                with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                        patch('subprocess.Popen', side_effect=self.fake_removal(result, output_bytes=output_bytes)):
                    with self.assertRaises(tools.CreationToolError):
                        self.remove()
                self.output.unlink()
                self.output.with_name(self.output.stem + '.background.json').unlink()

    def test_persisted_report_mismatch_is_rejected(self):
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                patch('subprocess.Popen', side_effect=self.fake_removal(report_change={'alpha_min': 1})):
            with self.assertRaises(tools.CreationToolError):
                self.remove()

    def test_oversized_or_duplicate_json_report_is_rejected(self):
        for payload in (b'x' * (tools.MAX_REPORT + 1), b'{"status":"completed","status":"failed"}', b'{"x":NaN}'):
            with self.subTest(length=len(payload)), patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                    patch('subprocess.Popen', return_value=FakeProcess(payload)):
                with self.assertRaises(tools.CreationToolError):
                    self.remove()

    def test_cancellation_only_terminates_processes_owned_by_this_job(self):
        owned, other = FakeProcess(code=None), FakeProcess(code=None)
        other_job = tools.CreationJob()
        with patch('subprocess.Popen', side_effect=[owned, other]):
            self.job._spawn(['owned'])
            other_job._spawn(['other'])
        self.job.cancel()
        self.assertTrue(owned.terminated)
        self.assertFalse(other.terminated)
        other_job.cancel()
        self.assertTrue(other.terminated)

    def test_cancel_during_spawn_stops_newly_created_child(self):
        child = FakeProcess(code=None)
        def spawn(*args, **kwargs):
            self.job.cancel()
            return child
        with patch('subprocess.Popen', side_effect=spawn):
            with self.assertRaises(tools.CreationToolError):
                self.job._spawn(['fixture'])
        self.assertTrue(child.terminated)
        self.assertEqual(self.job._processes, set())

    def test_background_timeout_terminates_only_its_child(self):
        child = FakeProcess(code=None)
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), \
                patch('subprocess.Popen', return_value=child), patch.object(tools, 'BACKGROUND_TIMEOUT', .01):
            with self.assertRaisesRegex(tools.CreationToolError, '超时'):
                self.remove()
        self.assertTrue(child.terminated)
        self.assertEqual(self.job._processes, set())

    def test_cancel_background_wait_exits_without_accepting_late_success(self):
        child = FakeProcess(json.dumps(self.removal_result()).encode(), code=None)
        entered = threading.Event()
        failures = []
        def spawn(*args, **kwargs):
            entered.set()
            return child
        def run():
            try:
                self.remove()
            except tools.CreationToolError as error:
                failures.append(str(error))
        with patch.object(self.removal, '_model_data', return_value=b'fixture'), patch('subprocess.Popen', side_effect=spawn):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.job.cancel()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertTrue(child.terminated)
        self.assertEqual(len(failures), 1)
        self.assertIn('停止', failures[0])
        self.assertEqual(self.job._processes, set())

    def test_generation_report_replace_failure_preserves_previous_record(self):
        tool = tools._tool('generate_local_image')
        path = self.folder / 'job.json'
        tool.save_report(path, {'status': 'queued', 'prompt_id': 'known'})
        before = path.read_bytes()
        with patch.object(tool.os, 'replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                tool.save_report(path, {'status': 'completed'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.folder.glob('job.json.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
