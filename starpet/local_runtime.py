"""Explicit, owned-process control for the already installed local chat service.

Constructing this object and checking status never start a model. The descriptor
is an explicit installation choice, not a discovery or download mechanism.
"""
import ctypes
import http.client
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import threading
import time


URL = 'http://127.0.0.1:11536'
MODEL = 'qwen3-4b-local'
ENGINE = 'transformers'
ADDRESS = ('127.0.0.1', 11536)
HEALTH_TIMEOUT = 0.5
MAX_HEALTH = 8192
CONFIG_NAME = 'local-model-runtime.json'


class LocalRuntimeError(RuntimeError):
    """A configured installation cannot be safely started."""


def _checked_path(value, directory=False):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise LocalRuntimeError('本机模型安装路径不完整。')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise LocalRuntimeError('本机模型安装路径必须是明确的绝对路径。')
    if path.drive.startswith('\\\\') or (os.name == 'nt' and ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4):
        raise LocalRuntimeError('本机模型安装路径不能是网络共享或网络盘。')
    # Reject every ancestor too: resolving first would hide a junction/symlink.
    try:
        for candidate in (path, *path.parents):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise LocalRuntimeError('本机模型安装路径不能包含链接或联接目录。')
        if not (path.is_dir() if directory else path.is_file()):
            raise LocalRuntimeError('本机模型安装文件或目录不存在。')
        return path.resolve(strict=True)
    except OSError as exc:
        raise LocalRuntimeError('本机模型安装文件或目录不可用。') from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _configuration(state):
    descriptor = state / CONFIG_NAME
    if not descriptor.exists() and not descriptor.is_symlink():
        return None
    descriptor = _checked_path(str(descriptor))
    try:
        if descriptor.stat().st_size > 16384:
            raise ValueError('too large')
        with descriptor.open('rb') as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError('too large')
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise LocalRuntimeError('本机模型安装说明无法读取，请重新核对安装。') from exc
    keys = {'schema', 'python', 'script', 'modelDir', 'dependencyDir', 'url'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['schema']) is not int or value['schema'] != 1 or value['url'] != URL):
        raise LocalRuntimeError('本机模型安装说明格式不支持，地址必须为 ' + URL + '。')
    value['python'] = _checked_path(value['python'])
    value['script'] = _checked_path(value['script'])
    value['modelDir'] = _checked_path(value['modelDir'], directory=True)
    value['dependencyDir'] = _checked_path(value['dependencyDir'], directory=True)
    python_names = ('python.exe', 'python3.exe') if os.name == 'nt' else ('python', 'python3', 'python3.13')
    if value['python'].name.lower() not in python_names or value['script'].name != 'run_local_chat.py':
        raise LocalRuntimeError('请选择已安装的 Python 程序和 run_local_chat.py 服务脚本。')
    return value


def _environment():
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'NUMBER_OF_PROCESSORS',
               'PROCESSOR_ARCHITECTURE', 'CUDA_PATH'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_DISABLE_IMPLICIT_TOKEN='1', DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false')
    return env


class _BytesSocket:
    def __init__(self, data):
        self.data = data

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.data)


def _health():
    """Read only fixed-address health, with a total socket deadline and byte cap.

    A raw loopback socket cannot use environment proxies or follow redirects.
    Parsing occurs only after the bounded response is collected in memory.
    """
    deadline = time.monotonic() + HEALTH_TIMEOUT
    request = (b'GET /health HTTP/1.1\r\nHost: 127.0.0.1:11536\r\n'
               b'Accept: application/json\r\nConnection: close\r\n\r\n')
    data = bytearray()
    with socket.create_connection(ADDRESS, timeout=HEALTH_TIMEOUT) as connection:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(request)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('health deadline')
            connection.settimeout(remaining)
            chunk = connection.recv(min(4096, MAX_HEALTH + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_HEALTH:
                raise ValueError('health response too large')
    with http.client.HTTPResponse(_BytesSocket(bytes(data))) as response:
        response.begin()
        lengths = response.headers.get_all('Content-Length', [])
        if (response.status != 200 or response.headers.get('Transfer-Encoding') is not None
                or len(lengths) != 1 or not lengths[0].isdecimal()
                or not 1 <= int(lengths[0]) <= MAX_HEALTH
                or response.headers.get_content_type() != 'application/json'):
            raise ValueError('unexpected health response')
        content = response.read(MAX_HEALTH + 1)
        if len(content) != int(lengths[0]):
            raise ValueError('incomplete health response')
        value = json.loads(content, object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or value.get('ok') is not True
            or value.get('engine') != ENGINE or value.get('model') != MODEL
            or type(value.get('loaded')) is not bool or type(value.get('busy')) is not bool):
        raise ValueError('unrecognized local engine')
    return value


class LocalRuntime:
    """Keep one process handle; a new object cannot stop an existing service.

    ``start()`` is explicit and returns immediately after process creation.
    ``status()`` returns status/installed/owned/running/url/model/loaded/busy/
    message; health checks consume at most 0.5 seconds of socket time. An idle
    healthy service is available even before its first on-demand model load.
    ``stop()`` returns the same status shape and stops only its own live child.
    ``close()`` permanently refuses later starts, then stops an owned child.
    """
    def __init__(self, state):
        self.state = Path(state).absolute()
        self._process = None
        self._lock = threading.RLock()
        self._closing = False

    def _result(self, status, message, installed=True, running=False, loaded=False, busy=False):
        owned = self._process is not None and self._process.poll() is None
        return {'status': status, 'installed': installed, 'owned': owned,
                'running': bool(running or owned), 'url': URL, 'model': MODEL, 'loaded': loaded,
                'busy': busy, 'message': message}

    def status(self):
        with self._lock:
            try:
                config = _configuration(self.state)
            except LocalRuntimeError as exc:
                return self._result('error', str(exc), installed=False)
            if config is None:
                return self._result('not_installed', '尚未配置已安装的本机模型。', installed=False)
            try:
                health = _health()
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                if self._process is not None and self._process.poll() is None:
                    return self._result('loading', '本机聊天服务正在启动。', running=True)
                return self._result('unavailable', '本机聊天服务未运行或健康检查未通过。')
            status = ('loading' if not health['loaded'] else 'busy') if health['busy'] else 'available'
            messages = {'loading': '本机模型正在载入。', 'busy': '本机模型正在回复。',
                        'available': '本机聊天服务可用。' if health['loaded'] else '本机聊天服务可用，首次聊天将载入模型。'}
            return self._result(status, messages[status], running=True,
                                loaded=health['loaded'], busy=health['busy'])

    def start(self):
        with self._lock:
            if self._closing:
                raise LocalRuntimeError('助手正在关闭，不能再启动本机聊天服务。')
            config = _configuration(self.state)
            if config is None:
                raise LocalRuntimeError('尚未配置本机模型，请先完成本机安装。')
            if self._process is not None and self._process.poll() is None:
                return self._result('loading', '本机聊天服务已启动，请刷新状态。', running=True)
            # Do not adopt or terminate an existing listener, even if healthy.
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    listener.bind(ADDRESS)
            except OSError as exc:
                raise LocalRuntimeError('本机端口 11536 已被使用，请刷新状态；不会替换现有服务。') from exc
            command = [str(config['python']), '-I', '-B', '-u', str(config['script']),
                       '--model-dir', str(config['modelDir']), '--dependency-dir', str(config['dependencyDir']),
                       '--port', '11536', '--idle-seconds', '600']
            try:
                self._process = subprocess.Popen(command, cwd=str(config['script'].parent),
                    env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except OSError as exc:
                self._process = None
                raise LocalRuntimeError('本机聊天服务无法启动，请检查已安装的程序。') from exc
            if self._process.poll() is not None:
                return self._result('unavailable', '本机聊天服务启动后已退出，请检查已安装的程序。')
            return self._result('loading', '正在后台启动本机聊天服务。', running=True)

    def stop(self):
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._process = None
                result = self.status()
                if result['running']:
                    result['message'] = '本机服务由其他窗口启动，未停止该服务。'
                else:
                    result.update(status='stopped', message='本窗口没有正在运行的本机聊天服务。')
                return result
            try:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
            except OSError:
                # The owned child may have exited between poll and terminate.
                if process.poll() is None:
                    raise LocalRuntimeError('本机服务尚未停止，请稍后重试。')
            except subprocess.TimeoutExpired as exc:
                raise LocalRuntimeError('本机服务尚未停止，请稍后重试。') from exc
            self._process = None
            return self._result('stopped', '本窗口启动的本机聊天服务已停止。')

    def close(self):
        """Permanently close this owner, including against a concurrent start."""
        with self._lock:
            self._closing = True
            if self._process is not None and self._process.poll() is None:
                self.stop()
