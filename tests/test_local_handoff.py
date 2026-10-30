"""Real loopback HTTP cancellation and single-inference handoff, without models."""
import contextlib
import http.client
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor'), str(ROOT / 'tests')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from starpet.companion import Cancelled, ChatError, ChatJob, DEFAULTS, model_request

spec = importlib.util.spec_from_file_location('handoff_local_server',
    ROOT / 'skill/ai-desktop-assistant/scripts/run_local_chat.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return bool(predicate())


class FakeEngine:
    def __init__(self, handler=None):
        self.lock = threading.Lock()
        self.model_name = local.MODEL_NAME
        self.model = None
        self.device = 'cpu'
        self.last_activity = time.monotonic()
        self.calls = []
        self.handler = handler
        self.active = self.maximum_active = 0

    def reply(self, messages, maximum, disconnected):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.calls.append(messages)
        try:
            if self.handler:
                return self.handler(messages, maximum, disconnected)
            return {'message': {'role': 'assistant', 'content': '这是正常聊天的回复。'}, 'done': True}
        finally:
            self.active -= 1


@contextlib.contextmanager
def running_server(engine):
    server = local.LocalServer(0, engine)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    worker.start()
    try:
        settings = dict(DEFAULTS, provider='ollama', model=local.MODEL_NAME,
                        localUrl=f'http://127.0.0.1:{server.server_port}', timeoutSeconds=3)
        yield server, settings
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        if worker.is_alive():
            raise AssertionError('Isolated HTTP server did not stop')


class ClientCall:
    def __init__(self, settings, text='请回答我的正常聊天问题。'):
        self.job = ChatJob()
        self.value = self.error = None
        self.finished = threading.Event()

        def work():
            try:
                self.value = model_request(settings, [{'role': 'user', 'content': text}], ROOT, self.job)
            except Exception as error:
                self.error = error
            finally:
                self.finished.set()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def close(self):
        self.job.cancel()
        self.worker.join(2)
        if self.worker.is_alive():
            raise AssertionError('Cancelled HTTP client did not stop')


class LocalHandoffTests(unittest.TestCase):
    def test_cancel_during_response_wait_is_seen_by_server_and_releases_inference(self):
        entered, disconnected = threading.Event(), threading.Event()

        def until_cancel(messages, maximum, is_disconnected):
            entered.set()
            if not eventually(is_disconnected):
                raise AssertionError('The service never received cancellation')
            disconnected.set()
            raise ConnectionAbortedError()

        engine = FakeEngine(until_cancel)
        with running_server(engine) as (server, settings):
            call = ClientCall(settings)
            try:
                self.assertTrue(entered.wait(2))
                call.job.cancel()
                self.assertTrue(disconnected.wait(.5))
                self.assertTrue(call.finished.wait(.5))
                self.assertIsInstance(call.error, Cancelled)
                self.assertTrue(eventually(lambda: not engine.lock.locked()))
                self.assertEqual(len(engine.calls), 1)
                self.assertIsNone(call.job.connection)
                self.assertIsNone(call.job._http_socket)
            finally:
                call.close()

    def test_immediate_chat_waits_for_cancelled_generation_step_then_succeeds_once(self):
        entered, disconnected = threading.Event(), threading.Event()

        def generation_step(messages, maximum, is_disconnected):
            if len(engine.calls) == 1:
                entered.set()
                if not eventually(is_disconnected):
                    raise AssertionError('The first client did not cancel')
                disconnected.set()
                time.sleep(.2)  # A running generation step cannot return immediately.
                raise ConnectionAbortedError()
            return {'message': {'content': '这是正常聊天的回复。'}}

        engine = FakeEngine(generation_step)
        with running_server(engine) as (server, settings):
            first = ClientCall(settings, '陪伴短句')
            second = None
            try:
                self.assertTrue(entered.wait(2))
                first.job.cancel()
                started = time.monotonic()
                second = ClientCall(settings)
                self.assertTrue(second.finished.wait(2))
                self.assertTrue(disconnected.is_set())
                self.assertIsNone(second.error)
                self.assertEqual(second.value, '这是正常聊天的回复。')
                self.assertGreaterEqual(time.monotonic() - started, .15)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(len(engine.calls), 2)
                self.assertEqual(engine.maximum_active, 1)
            finally:
                first.close()
                if second:
                    second.close()

    def test_continuously_busy_service_returns_local_busy_after_bounded_wait_without_retry(self):
        engine = FakeEngine()
        engine.lock.acquire()
        with running_server(engine) as (server, settings):
            call = ClientCall(settings)
            try:
                started = time.monotonic()
                self.assertTrue(call.finished.wait(2))
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, .8)
                self.assertLess(elapsed, 1.8)
                self.assertIsInstance(call.error, ChatError)
                self.assertIn('本机模型正在忙', str(call.error))
                self.assertNotIn('额度', str(call.error))
                engine.lock.release()
                time.sleep(.1)
                self.assertEqual(engine.calls, [])
                self.assertEqual(engine.maximum_active, 0)
            finally:
                if engine.lock.locked():
                    engine.lock.release()
                call.close()

    def test_cancelling_a_waiter_exits_before_busy_deadline_and_never_runs_later(self):
        engine = FakeEngine()
        engine.lock.acquire()
        waiting = threading.Event()
        original = local.Handler.acquire_engine

        def acquire(handler):
            waiting.set()
            return original(handler)

        with running_server(engine) as (server, settings), patch.object(local.Handler, 'acquire_engine', acquire):
            call = ClientCall(settings)
            try:
                self.assertTrue(waiting.wait(2))
                started = time.monotonic()
                call.job.cancel()
                self.assertTrue(call.finished.wait(.5))
                self.assertTrue(eventually(lambda: server.slots._value == 4, timeout=.5))
                self.assertLess(time.monotonic() - started, .7)
                self.assertIsInstance(call.error, Cancelled)
                self.assertTrue(engine.lock.locked())
                engine.lock.release()
                time.sleep(.1)
                self.assertEqual(engine.calls, [])
            finally:
                if engine.lock.locked():
                    engine.lock.release()
                call.close()

    def test_cancel_during_connect_or_before_request_does_not_send_or_reconnect(self):
        original_class = http.client.HTTPConnection
        original_get = local.Handler.do_GET
        for stage in ('connect', 'request'):
            with self.subTest(stage=stage):
                entered, release = threading.Event(), threading.Event()
                connections, received = [], []

                class GatedConnection(original_class):
                    def connect(connection):
                        connections.append(connection)
                        if stage == 'connect':
                            entered.set()
                            if not release.wait(2):
                                raise AssertionError('Connect gate timed out')
                        super().connect()

                    def request(connection, *args, **kwargs):
                        if stage == 'request':
                            entered.set()
                            if not release.wait(2):
                                raise AssertionError('Request gate timed out')
                        return super().request(*args, **kwargs)

                def get(handler):
                    received.append(handler.path)
                    return original_get(handler)

                engine = FakeEngine()
                with running_server(engine) as (server, settings), \
                        patch('starpet.companion.http.client.HTTPConnection', GatedConnection), \
                        patch.object(local.Handler, 'do_GET', get):
                    call = ClientCall(settings)
                    try:
                        self.assertTrue(entered.wait(2))
                        call.job.cancel()
                        release.set()
                        self.assertTrue(call.finished.wait(.5))
                        self.assertIsInstance(call.error, Cancelled)
                        self.assertTrue(eventually(lambda: server.slots._value == 4))
                        self.assertEqual(len(connections), 1)
                        self.assertEqual(received, [])
                        self.assertEqual(engine.calls, [])
                    finally:
                        release.set()
                        call.close()

    def test_cancel_after_response_headers_interrupts_detached_response_socket(self):
        sent_headers, disconnected = threading.Event(), threading.Event()
        original = local.Handler.send_json

        def partial_reply(handler, status, payload):
            if handler.path != '/api/chat':
                return original(handler, status, payload)
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '1000')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(b'{')
            sent_headers.set()
            if eventually(handler.disconnected):
                disconnected.set()

        with running_server(FakeEngine()) as (server, settings), patch.object(local.Handler, 'send_json', partial_reply):
            call = ClientCall(settings)
            try:
                self.assertTrue(sent_headers.wait(2))
                self.assertTrue(eventually(lambda: call.job.connection is not None and call.job.connection.sock is None))
                call.job.cancel()
                self.assertTrue(call.finished.wait(.5))
                self.assertTrue(disconnected.wait(.5))
                self.assertIsInstance(call.error, Cancelled)
            finally:
                call.close()

    def test_cancel_after_lock_acquisition_does_not_enter_inference(self):
        acquired, release = threading.Event(), threading.Event()
        underlying = threading.Lock()

        class GatedLock:
            def acquire(self, **kwargs):
                result = underlying.acquire(**kwargs)
                if result:
                    acquired.set()
                    if not release.wait(2):
                        raise AssertionError('Lock acquisition gate timed out')
                return result

            def release(self):
                underlying.release()

            def locked(self):
                return underlying.locked()

        engine = FakeEngine()
        engine.lock = GatedLock()
        with running_server(engine) as (server, settings):
            call = ClientCall(settings)
            try:
                self.assertTrue(acquired.wait(2))
                call.job.cancel()
                release.set()
                self.assertTrue(call.finished.wait(.5))
                self.assertTrue(eventually(lambda: not engine.lock.locked()))
                self.assertEqual(engine.calls, [])
                self.assertIsInstance(call.error, Cancelled)
            finally:
                release.set()
                call.close()

    def test_offscreen_preview_to_chat_handoff_saves_only_normal_chat(self):
        from test_companion_context_ui import ContextUIFixture, QApplication
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        fixture = ContextUIFixture(app)
        entered = threading.Event()

        def preview_then_chat(messages, maximum, disconnected):
            if len(engine.calls) == 1:
                entered.set()
                if not eventually(disconnected):
                    raise AssertionError('Preview was not cancelled')
                time.sleep(.2)
                raise ConnectionAbortedError()
            return {'message': {'content': '这是正常聊天的回复。'}}

        engine = FakeEngine(preview_then_chat)
        try:
            with running_server(engine) as (server, settings):
                fixture.use_local()
                fixture.window.local_url.setText(settings['localUrl'])
                fixture.window.model.setCurrentText(settings['model'])
                self.assertTrue(fixture.window.save_settings())
                fixture.window.backend = model_request
                fixture.c.proactive.backend = model_request
                before = len(fixture.store.turns)
                fixture.window.try_proactive()
                self.assertTrue(entered.wait(2))
                fixture.send('这是手动聊天。')
                fixture.wait_for(lambda: fixture.window.job is None)
                self.assertEqual(fixture.window.feedback.text(), '回复已收到。')
                self.assertEqual(fixture.window.error_text, '')
                self.assertEqual(len(fixture.store.turns), before + 1)
                self.assertEqual(fixture.store.turns[-1]['user'], '这是手动聊天。')
                self.assertEqual(fixture.store.turns[-1]['assistant'], '这是正常聊天的回复。')
                self.assertEqual(fixture.c.proactive.last_preview, '')
                self.assertEqual(engine.maximum_active, 1)
                self.assertEqual(len(engine.calls), 2)
                self.assertTrue(eventually(lambda: not engine.lock.locked()))
        finally:
            fixture.close()


if __name__ == '__main__':
    unittest.main()
