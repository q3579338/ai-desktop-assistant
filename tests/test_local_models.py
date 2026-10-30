import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from starpet.companion import ChatError, ChatJob, DEFAULTS, ollama_request


@contextlib.contextmanager
def fake_ollama(models):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(('GET', self.path, None))
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'models': models}).encode('utf-8'))
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(('POST', self.path, body))
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'message': {'content': '本机测试回复'}}).encode('utf-8'))
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


class LocalModelTests(unittest.TestCase):
    def test_remote_models_are_not_listed_or_given_chat_content(self):
        rows = [{'name': 'host-proxy', 'remote_host': 'https://remote.example'},
                {'name': 'model-proxy', 'remote_model': 'upstream'},
                {'name': 'fixture:cloud'}]
        with fake_ollama(rows) as (url, seen):
            settings = {**DEFAULTS, 'localUrl': url}
            self.assertEqual(ollama_request(settings, [], ChatJob(), models=True), [])
            for row in rows:
                with self.subTest(model=row['name']), self.assertRaises(ChatError):
                    ollama_request({**settings, 'model': row['name']},
                                   [{'role': 'user', 'content': 'PRIVATE-CONVERSATION'}], ChatJob())
        self.assertTrue(seen)
        self.assertTrue(all(method == 'GET' and path == '/api/tags' and body is None for method, path, body in seen))

    def test_local_installed_model_is_rechecked_then_receives_the_requested_chat(self):
        messages = [{'role': 'system', 'content': '只聊当前内容'}, {'role': 'user', 'content': '本机消息'}]
        with fake_ollama([{'name': 'local-fixture:small'}, {'name': 'cloud-copy', 'remote_model': 'other'}]) as (url, seen):
            settings = {**DEFAULTS, 'localUrl': url, 'model': 'local-fixture:small', 'replyTokens': 512}
            result = ollama_request(settings, messages, ChatJob())
        self.assertEqual(result, '本机测试回复')
        self.assertEqual([(method, path) for method, path, _ in seen], [('GET', '/api/tags'), ('POST', '/api/chat')])
        self.assertEqual(seen[-1][2], {'model': 'local-fixture:small', 'messages': messages,
                                     'stream': False, 'options': {'num_predict': 512}})

    def test_empty_local_model_list_does_not_send_chat_or_fabricate_a_reply(self):
        with fake_ollama([]) as (url, seen):
            settings = {**DEFAULTS, 'localUrl': url, 'model': 'not-installed'}
            self.assertEqual(ollama_request(settings, [], ChatJob(), models=True), [])
            with self.assertRaises(ChatError):
                ollama_request(settings, [{'role': 'user', 'content': '不得发送'}], ChatJob())
        self.assertEqual([(method, path) for method, path, _ in seen], [('GET', '/api/tags'), ('GET', '/api/tags')])


if __name__ == '__main__':
    unittest.main()
