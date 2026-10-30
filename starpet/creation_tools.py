"""Explicit local authoring adapters around the bundled, trusted skill tools.

No downloads, service launches, account discovery or global model interrupts.
The caller owns the output directory and must obtain an explicit user choice
before using a model or a separately installed Python environment.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import types


MAX_REPORT = 64 * 1024
BACKGROUND_TIMEOUT = 300
_LOAD_LOCK = threading.RLock()
_TOOLS = {}
_TOOL_NAMES = {'probe_local_runtime', 'generate_local_image', 'remove_local_background'}


class CreationToolError(RuntimeError):
    def __init__(self, message, *, report=None, prompt_id=None):
        super().__init__(message)
        self.report = report
        self.prompt_id = prompt_id


class CreationJob:
    """Cancellation belongs to this request and its own child processes only."""
    def __init__(self):
        self.cancelled = threading.Event()
        self._lock = threading.RLock()
        self._processes = set()

    def check(self):
        if self.cancelled.is_set():
            raise CreationToolError('已停止本次制作；已提交的生图任务可能仍在本机服务中执行。')

    @staticmethod
    def _stop(process):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _spawn(self, command, **kwargs):
        with self._lock:
            self.check()
            process = subprocess.Popen(command, **kwargs)
            self._processes.add(process)
        if self.cancelled.is_set():
            self._stop(process)
            self._release(process)
            self.check()
        return process

    def _release(self, process):
        with self._lock:
            if process.poll() is not None:
                self._processes.discard(process)

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            self._stop(process)


def _local_path(value, title, *, required=False):
    try:
        raw = os.fspath(value)
        if (not isinstance(raw, str) or not raw or '\x00' in raw or
                raw.startswith(('\\\\', '//')) or '://' in raw):
            raise ValueError
        path = Path(raw).absolute()
        if '..' in path.parts:
            raise ValueError
        if os.name == 'nt':
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
                raise ValueError
        for item in (path, *path.parents):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError
        if required and not path.is_file():
            raise ValueError
        return path.resolve()
    except (TypeError, ValueError, OSError):
        raise CreationToolError(title + '必须是可用的本机文件路径，不能经过网络共享、链接或目录联接。') from None


def _scripts_directory():
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', None)
        if not base:
            raise CreationToolError('程序缺少随包制作工具，请重新安装完整版本。')
        directory = Path(base) / 'creation-tools'
    else:
        directory = Path(__file__).resolve().parents[1] / 'skill' / 'ai-desktop-assistant' / 'scripts'
    return _local_path(directory, '随包制作工具目录')


def _tool(name):
    if name not in _TOOL_NAMES:
        raise CreationToolError('不支持这个制作步骤。')
    with _LOAD_LOCK:
        directory = _scripts_directory()
        key = (str(directory), name)
        if key in _TOOLS:
            return _TOOLS[key]
        if name == 'generate_local_image':
            _tool('probe_local_runtime')
        package_name = '_starpet_creation_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(directory)]
            package.__package__ = package_name
            sys.modules[package_name] = package
        full_name = package_name + '.' + name
        path = _local_path(directory / (name + '.py'), '随包制作脚本', required=True)
        try:
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(full_name, None)
            raise CreationToolError('随包制作工具无法加载，请检查完整安装。') from None
        _TOOLS[key] = module
        return module


def discover_checkpoints(url, job):
    """Read only the official checkpoint node's local model enumeration."""
    job.check()
    tool = _tool('generate_local_image')
    try:
        target = tool.parse_loopback_url(url, 8188)
        row = tool.json_request(target, 'GET', '/object_info/CheckpointLoaderSimple').get('CheckpointLoaderSimple')
        job.check()
        if (not isinstance(row, dict) or row.get('python_module') != 'nodes' or row.get('api_node') or
                not isinstance(row.get('input'), dict) or
                not isinstance(row['input'].get('required'), dict) or
                set(row['input']['required']) != {'ckpt_name'}):
            raise CreationToolError('本机服务未提供兼容的官方 checkpoint 核心节点。')
        choices = row['input']['required']['ckpt_name']
        if (not isinstance(choices, list) or not choices or not isinstance(choices[0], list)
                or len(choices[0]) > 2048):
            raise CreationToolError('本机模型列表格式不兼容或过大。')
        return list(dict.fromkeys(tool.model_name(value) for value in choices[0]))
    except CreationToolError:
        raise
    except (tool.GenerationError, ValueError) as error:
        raise CreationToolError(str(error)) from None
    except Exception:
        raise CreationToolError('未能读取本机模型列表，请检查服务地址和安装。') from None


def generate_image(image, output_dir, options, job):
    """Run the existing one-image workflow once and retain its recovery record."""
    job.check()
    allowed = {'url', 'checkpoint', 'prompt', 'negative', 'steps', 'cfg', 'denoise', 'seed', 'timeout', 'poll_interval'}
    if not isinstance(options, dict) or not {'checkpoint', 'prompt'} <= options.keys() or set(options) - allowed:
        raise CreationToolError('生图参数不完整或含不支持的项目。')
    image = _local_path(image, '原图', required=True)
    output_dir = _local_path(output_dir, '制作输出目录')
    tool = _tool('generate_local_image')
    try:
        return tool.generate(image, output_dir, **dict(options), cancel_event=job.cancelled)
    except tool.GenerationError as error:
        raise CreationToolError(str(error), report=error.report, prompt_id=error.prompt_id) from None
    except ValueError:
        raise CreationToolError('生图地址或参数不合法，请核对本机地址和制作选项。') from None
    except Exception:
        raise CreationToolError('本次生图未完成，请检查本机服务和输出目录；不要自动重复提交未知任务。') from None


def _environment(python):
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP',
               'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system_root = next((v for k, v in env.items() if k.upper() == 'SYSTEMROOT'), '')
    env['PATH'] = os.pathsep.join([str(python.parent)] + ([str(Path(system_root) / 'System32')] if system_root else []))
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_DISABLE_IMPLICIT_TOKEN='1', DO_NOT_TRACK='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


def _decode_report(raw):
    if not 0 < len(raw) <= MAX_REPORT:
        raise CreationToolError('去背景报告为空或超过允许大小。')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_json,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise CreationToolError('去背景工具未返回有效报告。') from None


def _run_background(command, python, directory, job):
    process = job._spawn(command, cwd=directory, env=_environment(python), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    chunks, errors = [], []
    finished = threading.Event()

    def read_output():
        total = 0
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_REPORT:
                    errors.append('去背景工具输出超过允许大小，已停止本次进程。')
                    job._stop(process)
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            errors.append('去背景工具输出中断。')
        finally:
            finished.set()

    reader = threading.Thread(target=read_output, daemon=True, name='AssistantCreationOutput')
    reader.start()
    deadline = time.monotonic() + BACKGROUND_TIMEOUT
    try:
        while process.poll() is None or not finished.is_set():
            job.check()
            if time.monotonic() >= deadline:
                raise CreationToolError('去背景等待超时，已停止本次进程；可以核对结果后重试。')
            finished.wait(.03) if not finished.is_set() else job.cancelled.wait(.03)
        job.check()
        if errors:
            raise CreationToolError(errors[0])
        result = _decode_report(b''.join(chunks))
        if process.returncode != 0 or result.get('status') != 'completed':
            # Only fixed tool messages are shown; arbitrary child output and
            # dependency tracebacks can contain local configuration details.
            reason = result.get('reason')
            known = {
                '当前 Python 缺少 onnxruntime；请使用已配置的本机环境。',
                '当前 Python 缺少 Pillow 或 numpy；请使用已配置的本机环境。',
                '模型没有分辨出前景和背景，未保存空白结果。',
                '结果没有有效透明变化，未保存不可用的抠图。',
            }
            raise CreationToolError(reason if isinstance(reason, str) and reason in known else
                '本地去背景未完成，请检查所选 Python 的 Pillow、numpy、onnxruntime 环境及输出目录。')
        return result
    finally:
        if process.poll() is None:
            job._stop(process)
        reader.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()
        job._release(process)


def _file_bytes(path, limit):
    if not path.is_file() or path.stat().st_size > limit:
        raise CreationToolError('制作文件缺失或超过允许大小。')
    with path.open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise CreationToolError('制作文件超过允许大小。')
    return value


def remove_background(image, output, python_path, model_path, job):
    """Use only an explicitly selected Python and the fixed, hash-pinned model."""
    job.check()
    tool = _tool('remove_local_background')
    try:
        image = _local_path(image, '原图', required=True)
        output = _local_path(output, '去背景输出')
        python = _local_path(python_path, '本机 Python', required=True)
        model = _local_path(model_path, '去背景模型', required=True)
        report_path = _local_path(output.with_name(output.stem + '.background.json'), '去背景记录')
        if (python.name.lower() not in ('python.exe', 'python3.exe', 'python', 'python3') or
                output.suffix.lower() != '.png' or image.suffix.lower() != '.png' or
                output in (image, model, python) or output.exists() or report_path.exists()):
            raise CreationToolError('请选择本机 Python 与新的 PNG 输出路径；不能覆盖原图或已有记录。')
        source = _file_bytes(image, tool.MAX_IMAGE_BYTES)
        if len(source) < 33 or source[:8] != b'\x89PNG\r\n\x1a\n':
            raise CreationToolError('请选择有效的本机 PNG 原图。')
        source_sha = hashlib.sha256(source).hexdigest()
        tool._model_data(model)  # Same fixed size, SHA256 and published MD5 as CLI.
        job.check()
        script = _local_path(_scripts_directory() / 'remove_local_background.py', '随包去背景脚本', required=True)
        result = _run_background([str(python), '-I', '-B', '-X', 'utf8', str(script), '--image', str(image),
                                  '--output', str(output), '--model', str(model)], python, script.parent, job)
        job.check()
        # Verify actual files instead of trusting a successful exit or stdout.
        _local_path(image, '原图', required=True)
        _local_path(output, '去背景输出', required=True)
        _local_path(report_path, '去背景记录', required=True)
        persisted = _decode_report(_file_bytes(report_path, MAX_REPORT))
        encoded = _file_bytes(output, tool.MAX_IMAGE_BYTES)
        if (result != persisted or type(result.get('schema')) is not int or result.get('schema') != 1 or
                result.get('input') != str(image) or result.get('image') != str(output) or
                result.get('report') != str(report_path) or result.get('input_sha256') != source_sha or
                hashlib.sha256(_file_bytes(image, tool.MAX_IMAGE_BYTES)).hexdigest() != source_sha or
                result.get('output_sha256') != hashlib.sha256(encoded).hexdigest() or
                result.get('model_sha256') != tool.MODEL_SHA256 or result.get('model_verified') is not True or
                result.get('provider') != 'CPUExecutionProvider' or result.get('rgb_preserved') is not True or
                any(result.get(key) is not False for key in ('network_used', 'models_downloaded', 'credentials_read', 'custom_ops_registered')) or
                result.get('telemetry_disabled') is not True or result.get('existing_alpha_preserved') is not True or
                result.get('alpha_combination') != 'multiply_existing_alpha'):
            raise CreationToolError('去背景结果与本次输入、模型或输出记录不一致，未接受该结果。')
        width, height = int.from_bytes(source[16:20], 'big'), int.from_bytes(source[20:24], 'big')
        if (len(encoded) < 33 or encoded[:8] != source[:8] or encoded[12:16] != b'IHDR' or
                encoded[16:24] != source[16:24] or encoded[24:26] != bytes([8, 6]) or
                type(result.get('width')) is not int or type(result.get('height')) is not int or
                (result['width'], result['height']) != (width, height) or
                not 1 <= width <= tool.MAX_SIDE or not 1 <= height <= tool.MAX_SIDE or
                width * height > tool.MAX_PIXELS or
                type(result.get('alpha_min')) is not int or type(result.get('alpha_max')) is not int or
                not 0 <= result['alpha_min'] < result['alpha_max'] <= 255):
            raise CreationToolError('去背景输出的尺寸或透明通道未通过检查。')
        job.check()
        return result
    except CreationToolError:
        raise
    except tool.BackgroundRemovalError as error:
        raise CreationToolError(str(error)) from None
    except Exception:
        raise CreationToolError('本地去背景未完成，请检查本机路径、已核验模型和 Python 依赖。') from None
