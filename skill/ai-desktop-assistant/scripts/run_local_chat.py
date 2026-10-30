"""Serve an existing local Qwen3 safetensors model; no downloads or credentials.

Small Ollama-compatible text-only endpoint, not the Ollama engine. Dependencies
must already exist. Only the official Qwen3 architecture is currently supported.
"""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = 'qwen3-4b-local'
MAX_BODY = 192 * 1024
HANDOFF_WAIT_SECONDS = 1.0


def model_files(directory):
    root = Path(directory).resolve(strict=True)
    def checked(name):
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('模型资源必须位于所选文件夹内。')
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('模型资源缺失或含链接。')
        return path
    for filename in ('config.json', 'model.safetensors.index.json'):
        if checked(filename).stat().st_size > 2 * 1024 * 1024:
            raise ValueError('模型配置或索引过大。')
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    if (config.get('model_type') != 'qwen3' or config.get('hidden_size') != 2560
            or config.get('num_hidden_layers') != 36 or config.get('vocab_size') != 151936):
        raise ValueError('目前只支持已验证的 Qwen3-4B 文本模型配置。')
    index = json.loads((root / 'model.safetensors.index.json').read_text(encoding='utf-8'))
    names = set(index.get('weight_map', {}).values())
    if not names or len(names) > 32:
        raise ValueError('模型索引不完整。')
    for name in names | {'config.json', 'tokenizer.json', 'tokenizer_config.json'}:
        checked(name)
    if any(not name.endswith('.safetensors') for name in names):
        raise ValueError('只加载 safetensors 权重。')
    return root


def validate_request(payload, model_name=MODEL_NAME):
    if not isinstance(payload, dict) or payload.get('model') != model_name:
        raise ValueError('所选本机模型不可用。')
    if set(payload) - {'model', 'messages', 'stream', 'options'} or payload.get('stream') is not False:
        raise ValueError('此服务仅支持非流式纯文字聊天。')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 50:
        raise ValueError('对话轮数超出范围。')
    count = 0
    for item in messages:
        if (not isinstance(item, dict) or set(item) != {'role', 'content'}
                or item['role'] not in ('system', 'user', 'assistant')
                or not isinstance(item['content'], str) or len(item['content']) > 32000):
            raise ValueError('只接受文字角色和消息。')
        count += len(item['content'])
    if count > 40000 or messages[-1]['role'] != 'user':
        raise ValueError('对话过长或缺少用户消息，请减少最近对话轮数。')
    options = payload.get('options', {})
    if not isinstance(options, dict) or set(options) - {'num_predict'}:
        raise ValueError('不支持这些推理选项。')
    maximum = options.get('num_predict', 512)
    if type(maximum) is not int or maximum < 1:
        raise ValueError('回复长度应为正整数。')
    return messages, min(maximum, 1024)


class LocalEngine:
    def __init__(self, directory, device='cuda'):
        self.directory = model_files(directory)
        self.device = device
        self.tokenizer = self.model = self.torch = None
        self.model_name = MODEL_NAME
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()

    def load(self):
        if self.model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        if self.device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA 显卡不可用；可明确选择 --device cpu，速度会较慢。')
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(str(self.directory), local_files_only=True,
                                                       trust_remote_code=False, token=False)
        self.model = AutoModelForCausalLM.from_pretrained(
            str(self.directory), local_files_only=True, trust_remote_code=False,
            token=False, use_safetensors=True,
            dtype=torch.bfloat16 if self.device == 'cuda' else torch.float32,
            attn_implementation='sdpa').to(self.device).eval()

    def reply(self, messages, maximum, disconnected):
        started = time.monotonic()
        self.load()
        from transformers import StoppingCriteria, StoppingCriteriaList
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False,
                                                    add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(prompt, return_tensors='pt').to(self.device)
        count = inputs['input_ids'].shape[1]
        if count + maximum > 8192:
            raise ValueError('本机聊天上下文超过 8192 token，请减少最近对话轮数或缩短消息。')

        class Stop(StoppingCriteria):
            reason = None

            def __call__(self, input_ids, scores, **kwargs):
                if disconnected():
                    self.reason = 'cancelled'
                elif time.monotonic() - started > 120:
                    self.reason = 'timeout'
                return self.reason is not None

        stop = Stop()
        if disconnected():
            raise ConnectionAbortedError()
        with self.torch.inference_mode():
            result = self.model.generate(
                **inputs, max_new_tokens=maximum, do_sample=True, temperature=0.7,
                top_p=0.8, top_k=20, stopping_criteria=StoppingCriteriaList([stop]))
        if stop.reason:
            raise TimeoutError('请求已经取消或超过本机推理时限。')
        tokens = result[0][count:]
        content = self.tokenizer.decode(tokens, skip_special_tokens=True).strip()
        if not content:
            raise RuntimeError('模型没有返回文字。')
        return {'model': self.model_name, 'message': {'role': 'assistant', 'content': content},
                'done': True, 'done_reason': 'length' if len(tokens) == maximum else 'stop',
                'prompt_eval_count': count, 'eval_count': len(tokens),
                'total_duration': int((time.monotonic() - started) * 1e9)}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, port, engine):
        self.engine = engine
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(('127.0.0.1', port), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'AI-Desktop-Assistant-Local'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log messages, query strings, or user text.

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ConnectionError):
            pass
        self.close_connection = True

    def permitted(self):
        host = f'127.0.0.1:{self.server.server_port}'
        if (self.headers.get_all('Host') != [host] or self.headers.get('Origin') is not None
                or self.headers.get('Sec-Fetch-Site') is not None):
            self.send_json(403, {'error': '此接口仅供本机桌面助手使用。'})
            return False
        return True

    def do_GET(self):
        if not self.permitted():
            return
        if self.path == '/api/tags':
            self.send_json(200, {'models': [{'name': self.server.engine.model_name,
                'details': {'format': 'safetensors', 'family': 'qwen3', 'parameter_size': '4B'}}]})
        elif self.path == '/health':
            self.send_json(200, {'ok': True, 'engine': 'transformers',
                'model': self.server.engine.model_name, 'loaded': self.server.engine.model is not None,
                'device': self.server.engine.device, 'busy': self.server.engine.lock.locked()})
        else:
            self.send_json(404, {'error': '接口不存在。'})

    def disconnected(self):
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def acquire_engine(self):
        # A cancelled request releases the model at its next generation step.
        # Keep the handoff bounded; never resubmit a received request.
        deadline = time.monotonic() + HANDOFF_WAIT_SECONDS
        while not self.disconnected():
            remaining = max(0, deadline - time.monotonic())
            if self.server.engine.lock.acquire(timeout=min(.05, remaining)):
                if self.disconnected():
                    self.server.engine.lock.release()
                    return False
                return True
            if time.monotonic() >= deadline:
                self.send_json(429, {'error': '本机模型正在忙，请稍后重试。'})
                return False
        return False

    def do_POST(self):
        if not self.permitted():
            return
        if self.path != '/api/chat':
            self.send_json(404, {'error': '接口不存在。'})
            return
        if (self.headers.get('Transfer-Encoding') is not None
                or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json'
                or len(self.headers.get_all('Content-Length') or []) != 1):
            self.send_json(400, {'error': '需要有界 JSON 请求。'})
            return
        try:
            length = int(self.headers['Content-Length'])
            if not 1 <= length <= MAX_BODY:
                raise ValueError('请求过大。')
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('请求不完整。')
            messages, maximum = validate_request(json.loads(body), self.server.engine.model_name)
        except (ValueError, UnicodeError, OSError):
            self.send_json(400, {'error': '请求格式不受支持或对话过长。'})
            return
        engine = self.server.engine
        if not self.acquire_engine():
            return
        engine.last_activity = time.monotonic()
        try:
            self.send_json(200, engine.reply(messages, maximum, self.disconnected))
        except ValueError:
            self.send_json(400, {'error': '本机模型上下文过长，请减少最近对话轮数。'})
        except (TimeoutError, ConnectionAbortedError):
            self.send_json(408, {'error': '本机推理已停止或超时。'})
        except Exception as error:
            # Exception type helps diagnosis without printing prompts or paths.
            print(json.dumps({'inference_error': type(error).__name__}), flush=True)
            self.send_json(503, {'error': '本机模型未能运行，请检查依赖、模型文件和显存。'})
        finally:
            engine.last_activity = time.monotonic()
            engine.lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--dependency-dir', type=Path)
    parser.add_argument('--port', type=int, default=11536)
    parser.add_argument('--device', choices=('cuda', 'cpu'), default='cuda')
    parser.add_argument('--idle-seconds', type=int, default=600)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 30 <= args.idle_seconds <= 3600:
        parser.error('端口必须为 1024～65535；空闲退出时间为 30～3600 秒。')
    root = model_files(args.model_dir)
    if args.dependency_dir:
        sys.path.insert(0, str(args.dependency_dir.resolve(strict=True)))
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK'):
        os.environ[key] = '1'
    os.environ['HF_HOME'] = str(root.parent / '.offline-cache')
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'

    def offline(event, arguments):
        if event == 'socket.connect':
            raise OSError('Local model process does not allow outbound connections.')

    sys.addaudithook(offline)
    engine = LocalEngine(root, args.device)
    with LocalServer(args.port, engine) as server:
        server.timeout = 1
        print(json.dumps({'ready': True, 'engine': 'transformers', 'url': f'http://127.0.0.1:{args.port}',
                          'model': engine.model_name, 'idle_exit_seconds': args.idle_seconds}), flush=True)
        while engine.lock.locked() or time.monotonic() - engine.last_activity < args.idle_seconds:
            server.handle_request()
    print(json.dumps({'stopped': 'idle_timeout'}), flush=True)


if __name__ == '__main__':
    main()
