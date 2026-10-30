import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import unittest
from unittest.mock import patch
import uuid


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import probe_local_runtime as runtime


@contextlib.contextmanager
def fixture_directory():
    # Default mkdir permissions preserve the workspace ACL on managed Windows.
    # tempfile's owner-only directory mode can make its child inaccessible to
    # the restricted execution identity even when the workspace is writable.
    base = (SKILL / 'tests').resolve()
    folder = base / ('probe-fixture-' + uuid.uuid4().hex[:10])
    folder.mkdir()
    try:
        yield folder
    finally:
        resolved = folder.resolve()
        assert resolved.parent == base and resolved.name.startswith('probe-fixture-')
        shutil.rmtree(resolved)


@contextlib.contextmanager
def local_server(responses):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, payload, headers = responses[self.path]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class RuntimeProbeTests(unittest.TestCase):
    def test_addresses_are_rejected_before_any_network_request(self):
        invalid = ('http://example.com', 'http://192.168.1.2:11434', 'http://0.0.0.0:8188',
                   'http://127.0.0.1.evil.test', 'http://127.1:11434',
                   'file:///tmp/service', 'http://user:password@127.0.0.1',
                   'http://@127.0.0.1', 'http://127.0.0.1/api/tags',
                   'http://127.0.0.1?token=private', 'http://127.0.0.1?',
                   'http://127.0.0.1/#fragment', 'http://127.0.0.1:0',
                   'http://127.0.0.1:99999', 'http://[::2]', 'http://[::1%lo]',
                   'http://127.0.0.1\n:11434')
        with patch.object(runtime, 'get_json') as get:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    runtime.probe('http://127.0.0.1:11434', url)
            get.assert_not_called()
        self.assertEqual(runtime.parse_loopback_url('http://localhost:11434/', 11434).host, '127.0.0.1')
        self.assertEqual(runtime.parse_loopback_url('http://[::1]:8188', 8188).host, '::1')

    def test_live_fixture_services_ignore_proxies_and_keep_system_details_private(self):
        responses = {
            '/api/tags': (200, {'models': [{'name': 'local-fixture:small'},
                {'name': 'remote-fixture', 'remote_host': 'https://private-upstream.example', 'remote_model': 'cloud'},
                {'name': 'named-cloud:latest'}]}, {}),
            '/system_stats': (200, {'system': {'comfyui_version': 'fixture-1', 'argv': ['SECRET-ARG'],
                'python_version': 'PRIVATE-PATH'}, 'devices': [{'name': 'PRIVATE-GPU'}]}, {})}
        with local_server(responses) as (url, requests), patch.dict(os.environ, {
                'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}), patch.object(runtime, 'probe_codex', return_value={'available': False}):
            result = runtime.probe(url, url)
        self.assertEqual(requests, ['/api/tags', '/system_stats'])
        self.assertEqual(result['detected_engines'], ['ollama', 'comfyui'])
        self.assertEqual(result['engines']['ollama']['local_model_candidates'], ['local-fixture:small'])
        self.assertEqual([row['locality'] for row in result['engines']['ollama']['models']],
                         ['local_candidate', 'cloud', 'cloud_name_unverified'])
        self.assertEqual(result['engines']['comfyui']['version'], 'fixture-1')
        self.assertEqual(result['engines']['comfyui']['device_count'], 1)
        report = json.dumps(result)
        self.assertNotIn('PRIVATE', report); self.assertNotIn('SECRET', report)
        self.assertNotIn('private-upstream', report)
        self.assertFalse(result['inference_tested'])

    def test_redirects_are_not_followed(self):
        with local_server({'/api/tags': (302, b'', {'Location': '/capture-private'})}) as (url, requests):
            result = runtime.probe_ollama(runtime.parse_loopback_url(url, 11434))
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'redirect_refused')
        self.assertEqual(requests, ['/api/tags'])

    def test_no_models_is_distinct_from_a_wrong_service_or_invalid_json(self):
        for payload, expected, available in (({'models': []}, 'no_models', True),
                ({'hello': 'another-service'}, 'invalid_response', False),
                (b'<html>not-json</html>', 'invalid_response', False),
                ({'models': ['invalid-entry']}, 'invalid_response', False)):
            with self.subTest(expected=expected, payload=payload):
                with local_server({'/api/tags': (200, payload, {})}) as (url, _):
                    result = runtime.probe_ollama(runtime.parse_loopback_url(url, 11434))
                self.assertEqual(result['available'], available)
                self.assertEqual(result['status'], expected)

    def test_oversize_and_connection_errors_have_bounded_clear_results(self):
        with local_server({'/system_stats': (200, b'x' * (runtime.MAX_RESPONSE_BYTES + 1), {})}) as (url, _):
            result = runtime.probe_comfyui(runtime.parse_loopback_url(url, 8188))
        self.assertEqual(result['status'], 'response_too_large')
        # The fixture has closed its temporary port; a refusal is an ordinary
        # unavailable result, not a traceback or proof that models are absent.
        result = runtime.probe_comfyui(runtime.parse_loopback_url(url, 8188), timeout=.2)
        self.assertFalse(result['available'])
        self.assertIn(result['status'], ('unreachable', 'timeout'))

    def test_codex_discovery_never_executes_or_reads_authentication(self):
        with fixture_directory() as local:
            executable = local / 'OpenAI/Codex/bin/fixture-version/codex.exe'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'not-an-executable')
            auth = local / 'auth.json'; auth.write_text('PRIVATE-CREDENTIAL', encoding='utf-8')
            with patch.object(Path, 'read_text', side_effect=AssertionError('Must not read auth or executable contents')):
                result = runtime.probe_codex(local)
            self.assertTrue(result['available'])
            self.assertEqual(result['executable'], str(executable))
            self.assertFalse(result['executed']); self.assertFalse(result['login_checked'])
            self.assertFalse(result['signature_checked'])
            self.assertEqual(auth.read_text(encoding='utf-8'), 'PRIVATE-CREDENTIAL')
            output = local / 'report.json'
            runtime.write_report(output, result)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), result)
            self.assertFalse(list(local.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
