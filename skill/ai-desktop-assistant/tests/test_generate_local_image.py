import contextlib
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid
import zlib

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import generate_local_image as image_tool


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def png(width=256, height=256, color=2, pixels=None):
    channels = 4 if color == 6 else 3
    raw = pixels if pixels is not None else (b'\0' + b'\x88' * (width * channels)) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


@contextlib.contextmanager
def directory():
    base = (SKILL / 'tests').resolve()
    folder = base / ('image-fixture-' + uuid.uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        resolved = folder.resolve()
        assert resolved.parent == base and resolved.name.startswith('image-fixture-')
        shutil.rmtree(resolved)


@contextlib.contextmanager
def comfy_fixture(**options):
    state = {'requests': [], 'post_bodies': [], 'views': [], 'graph': None, 'prompt_id': None, 'upload': None}
    class Handler(BaseHTTPRequestHandler):
        def respond(self, data, status=200, **headers):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            state['requests'].append((self.command, self.path))
            path = urlsplit(self.path).path
            if options.get('redirect') and path.startswith(options['redirect']):
                self.respond(b'', 302, Location='http://192.0.2.1/not-followed')
                return
            if path.startswith('/object_info/'):
                name = path.rsplit('/', 1)[-1]
                if options.get('missing') == name:
                    self.respond({})
                    return
                required = {key: ['STRING'] for key in image_tool.NODES[name]}
                if name == 'CheckpointLoaderSimple':
                    required['ckpt_name'] = [options.get('models', ['fixture.safetensors'])]
                if name == 'KSampler':
                    required['sampler_name'] = [['euler']]
                    required['scheduler'] = [['normal']]
                self.respond({name: {'python_module': options.get('module', 'nodes'), 'api_node': options.get('api_node', False),
                                     'input': None if options.get('bad_schema') else {'required': required}}})
            elif path == '/upload/image':
                body = self.rfile.read(int(self.headers['Content-Length']))
                state['post_bodies'].append(body)
                message = BytesParser(policy=default).parsebytes(('Content-Type: ' + self.headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
                parts = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
                state['upload'] = {'filename': parts['image'].get_filename(), 'image': parts['image'].get_payload(decode=True),
                                   'overwrite': parts['overwrite'].get_payload(decode=True), 'type': parts['type'].get_payload(decode=True)}
                self.respond({'name': '../unsafe.png' if options.get('bad_upload') else state['upload']['filename'], 'subfolder': '', 'type': 'input'})
            elif path == '/prompt':
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['post_bodies'].append(payload)
                state['graph'] = payload['prompt']
                state['prompt_id'] = payload['prompt_id']
                self.respond({'prompt_id': state['prompt_id'], 'node_errors': {'8': ['fixture-error']} if options.get('queue_error') else {}})
            elif path.startswith('/history/'):
                if options.get('pending'):
                    self.respond({'another-job': {'private': 'must-not-use'}})
                    return
                prefix = state['graph']['8']['inputs']['filename_prefix']
                output = {'filename': prefix + '_00001_.png', 'subfolder': '', 'type': 'output'}
                output.update(options.get('output', {}))
                recorded = state['graph'] if not options.get('wrong_graph') else {}
                row = {'prompt': [0, state['prompt_id'], recorded],
                       'status': {'completed': True, 'status_str': 'error' if options.get('job_error') else 'success'},
                       'outputs': {'8': {'images': [output]}}}
                self.respond({state['prompt_id']: row})
            elif path == '/view':
                state['views'].append(parse_qs(urlsplit(self.path).query, keep_blank_values=True))
                self.respond(options.get('image', png()))
            else:
                self.respond({}, 404)

        do_GET = route
        do_POST = route

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
        worker.join(timeout=2)


class GenerateLocalImageTests(unittest.TestCase):
    def run_job(self, folder, url, **kwargs):
        original = folder / 'original.png'
        if not original.exists():
            original.write_bytes(png())
        return image_tool.generate(original, folder / 'outputs', 'fixture.safetensors', '原创成年猫娘，全身，清楚轮廓',
                                   url=url, negative='模糊', seed=7, poll_interval=.01, **kwargs)

    def test_real_http_fixture_builds_only_core_img2img_and_preserves_input(self):
        with directory() as folder, comfy_fixture() as (url, state), patch.dict(os.environ, {
                'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            result = self.run_job(folder, url)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual((folder / 'original.png').read_bytes(), png())
            self.assertEqual(Path(result['image']).read_bytes(), png())
            self.assertEqual(state['upload']['image'], png())
            self.assertEqual(state['upload']['overwrite'], b'false')
            self.assertEqual(state['upload']['type'], b'input')
            self.assertEqual([method for method, _ in state['requests'][:7]], ['GET'] * 7)
            self.assertEqual(state['requests'][7][1], '/upload/image')
            graph = state['graph']
            self.assertEqual(len(graph), 8)
            self.assertEqual({row['class_type'] for row in graph.values()}, set(image_tool.NODES))
            self.assertEqual(graph['1']['inputs']['image'], state['upload']['filename'])
            self.assertEqual(graph['2']['inputs'], {'ckpt_name': 'fixture.safetensors'})
            self.assertEqual(graph['5']['inputs'], {'pixels': ['1', 0], 'vae': ['2', 2]})
            self.assertEqual(graph['6']['inputs']['latent_image'], ['5', 0])
            self.assertEqual(graph['6']['inputs']['seed'], 7)
            self.assertEqual(graph['6']['inputs']['steps'], 16)
            self.assertEqual(graph['6']['inputs']['denoise'], .55)
            self.assertEqual(graph['7']['inputs']['vae'], ['2', 2])
            self.assertTrue(graph['3']['inputs']['text'].startswith('2D hand-drawn illustration'))
            self.assertIn('原创成年猫娘，全身，清楚轮廓', graph['3']['inputs']['text'])
            self.assertIn('3D render', graph['4']['inputs']['text'])
            self.assertIn('模糊', graph['4']['inputs']['text'])
            self.assertEqual(result['art_style'], '2d_illustration')
            self.assertEqual(state['views'][0]['type'], ['output'])
            self.assertNotIn('extra_data', state['post_bodies'][-1])
            self.assertFalse(any('/interrupt' in path or path == '/history' for _, path in state['requests']))

    def test_non_loopback_and_credential_urls_fail_before_io(self):
        invalid = ('http://example.com', 'http://192.168.0.2:8188', 'http://0.0.0.0', 'http://127.0.0.1.evil.test',
                   'http://user:secret@127.0.0.1', 'http://127.0.0.1/path', 'http://127.0.0.1?token=secret',
                   'http://127.0.0.1/#fragment', 'http://[::2]', 'http://[::1%eth0]', 'file:///tmp/test')
        with patch.object(image_tool, 'request') as request:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    image_tool.generate('unused.png', 'unused', 'fixture.safetensors', 'test', url=url)
            request.assert_not_called()

    def test_localhost_is_pinned_and_ignores_dns_aliases(self):
        original_lookup = socket.getaddrinfo
        queried = []
        def lookup(host, *args, **kwargs):
            queried.append(host)
            if host != '127.0.0.1':
                raise AssertionError('Only pinned numeric loopback may reach resolver')
            return original_lookup(host, *args, **kwargs)
        with directory() as folder, comfy_fixture() as (url, _), patch.object(socket, 'getaddrinfo', side_effect=lookup):
            result = self.run_job(folder, url.replace('127.0.0.1', 'localhost'))
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(queried)

    def test_no_checkpoint_missing_core_and_custom_nodes_never_upload(self):
        failures = ({'models': []}, {'models': ['different.safetensors']}, {'missing': 'VAEEncode'},
                    {'module': 'custom_nodes.cloud_bridge'}, {'api_node': True}, {'bad_schema': True})
        for options in failures:
            with self.subTest(options=options), directory() as folder, comfy_fixture(**options) as (url, state):
                with self.assertRaises(image_tool.GenerationError):
                    self.run_job(folder, url)
                self.assertTrue(all(method == 'GET' for method, _ in state['requests']))
                self.assertFalse((folder / 'outputs').exists())

    def test_redirect_at_each_protocol_phase_is_never_followed(self):
        for phase in ('/object_info/', '/upload/image', '/prompt', '/history/', '/view'):
            with self.subTest(phase=phase), directory() as folder, comfy_fixture(redirect=phase) as (url, state):
                with self.assertRaisesRegex(image_tool.GenerationError, '重定向') as error:
                    self.run_job(folder, url)
                self.assertFalse(any('not-followed' in path for _, path in state['requests']))
                self.assertFalse(list((folder / 'outputs').glob('*/portrait.png')))
                if phase in ('/history/', '/view'):
                    self.assertEqual(error.exception.prompt_id, state['prompt_id'])

    def test_upload_path_mismatch_prevents_queueing(self):
        with directory() as folder, comfy_fixture(bad_upload=True) as (url, state):
            with self.assertRaisesRegex(image_tool.GenerationError, '路径'):
                self.run_job(folder, url)
            self.assertIsNone(state['graph'])

    def test_other_jobs_are_ignored_and_timeout_keeps_current_prompt_id(self):
        with directory() as folder, comfy_fixture(pending=True) as (url, state):
            with self.assertRaisesRegex(image_tool.GenerationError, '超时') as error:
                self.run_job(folder, url, timeout=.1)
            report = json.loads(Path(error.exception.report).read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'timed_out')
            self.assertEqual(report['prompt_id'], state['prompt_id'])
            self.assertTrue(report['server_may_continue'])
            self.assertEqual(error.exception.prompt_id, state['prompt_id'])
            self.assertFalse(state['views'])
            self.assertFalse(any('/interrupt' in path or path == '/queue' for _, path in state['requests']))

    def test_output_traversal_wrong_source_and_other_prefix_never_download(self):
        rows = ({'filename': '../private.png'}, {'filename': 'unrelated_00001_.png'}, {'subfolder': '../outside'},
                {'subfolder': 'another-job'}, {'type': 'input'}, {'filename': 'C:\\private.png'})
        for row in rows:
            with self.subTest(row=row), directory() as folder, comfy_fixture(output=row) as (url, state):
                with self.assertRaisesRegex(image_tool.GenerationError, '输出路径'):
                    self.run_job(folder, url)
                self.assertFalse(state['views'])

    def test_history_graph_mismatch_is_rejected(self):
        with directory() as folder, comfy_fixture(wrong_graph=True) as (url, state):
            with self.assertRaisesRegex(image_tool.GenerationError, '工作流不一致'):
                self.run_job(folder, url)
            self.assertFalse(state['views'])

    def test_invalid_or_resized_output_is_not_saved(self):
        for data in (png(512, 256), png(2048, 256), b'not-a-png', png()[:-1], b'x' * (image_tool.MAX_PNG + 1)):
            with self.subTest(bytes=len(data)), directory() as folder, comfy_fixture(image=data) as (url, _):
                with self.assertRaises(image_tool.GenerationError):
                    self.run_job(folder, url)
                self.assertFalse(list((folder / 'outputs').glob('*/portrait.png')))

    def test_invalid_input_and_excessive_settings_do_not_contact_server(self):
        with directory() as folder, patch.object(image_tool, 'request') as request:
            image = folder / 'input.png'
            for data in (png(128, 256), png(257, 256), png() + b'extra', png(pixels=b'\0' * (256 * 769 + 1))):
                image.write_bytes(data)
                with self.assertRaises(image_tool.GenerationError):
                    image_tool.generate(image, folder / 'out', 'fixture.safetensors', 'test')
            image.write_bytes(png())
            for kwargs in ({'steps': 31}, {'steps': True}, {'cfg': float('nan')}, {'denoise': 0}, {'seed': -1}, {'timeout': 1801}):
                with self.subTest(kwargs=kwargs), self.assertRaises(image_tool.GenerationError):
                    image_tool.generate(image, folder / 'out', 'fixture.safetensors', 'test', **kwargs)
            request.assert_not_called()

    def test_distinct_runs_never_overwrite_existing_results(self):
        with directory() as folder, comfy_fixture() as (url, _):
            first = self.run_job(folder, url)
            second = self.run_job(folder, url)
            self.assertNotEqual(first['image'], second['image'])
            self.assertEqual(Path(first['image']).read_bytes(), png())
            self.assertEqual(Path(second['image']).read_bytes(), png())
            self.assertNotEqual(first['output_prefix'], second['output_prefix'])

    def test_job_failure_keeps_id_without_claiming_output_or_interrupting(self):
        with directory() as folder, comfy_fixture(job_error=True) as (url, state):
            with self.assertRaisesRegex(image_tool.GenerationError, '任务失败') as error:
                self.run_job(folder, url)
            report = json.loads(Path(error.exception.report).read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'generation_failed')
            self.assertFalse(report['server_may_continue'])
            self.assertEqual(error.exception.prompt_id, state['prompt_id'])
            self.assertFalse(state['views'])

    def test_queue_error_still_preserves_returned_id(self):
        with directory() as folder, comfy_fixture(queue_error=True) as (url, state):
            with self.assertRaisesRegex(image_tool.GenerationError, '节点错误') as error:
                self.run_job(folder, url)
            report = json.loads(Path(error.exception.report).read_text(encoding='utf-8'))
            self.assertEqual(report['prompt_id'], state['prompt_id'])
            self.assertEqual(error.exception.prompt_id, state['prompt_id'])
            self.assertFalse(any('/history/' in path for _, path in state['requests']))

    def test_ambiguous_submission_keeps_requested_id_without_resubmitting(self):
        with directory() as folder, comfy_fixture(redirect='/prompt') as (url, state):
            with self.assertRaises(image_tool.GenerationError) as error:
                self.run_job(folder, url)
            report = json.loads(Path(error.exception.report).read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'submission_unknown')
            self.assertIsNone(report['prompt_id'])
            self.assertTrue(report['requested_prompt_id'])
            self.assertTrue(report['server_may_continue'])
            self.assertEqual(sum(path == '/prompt' for _, path in state['requests']), 1)

    def test_keyboard_stop_records_id_and_does_not_interrupt_server(self):
        real_request = image_tool.json_request
        def controlled(target, method, path, *args, **kwargs):
            if path.startswith('/history/'):
                raise KeyboardInterrupt
            return real_request(target, method, path, *args, **kwargs)
        with directory() as folder, comfy_fixture() as (url, state), patch.object(image_tool, 'json_request', side_effect=controlled):
            with self.assertRaises(image_tool.GenerationError) as error:
                self.run_job(folder, url)
            report = json.loads(Path(error.exception.report).read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'client_stopped')
            self.assertEqual(report['prompt_id'], state['prompt_id'])
            self.assertTrue(report['server_may_continue'])
            self.assertFalse(any('/interrupt' in path for _, path in state['requests']))


if __name__ == '__main__':
    unittest.main()
