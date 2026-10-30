"""Read-only local engine discovery. Never starts a model or reads credentials."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit
import uuid


MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LoopbackTarget:
    scheme: str
    host: str
    port: int

    @property
    def base_url(self):
        host = '[' + self.host + ']' if ':' in self.host else self.host
        return f'{self.scheme}://{host}:{self.port}'


class ProbeError(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_loopback_url(value, default_port):
    """Reject external names, credentials and URL paths before opening a socket."""
    if not isinstance(value, str) or not 0 < len(value) <= 240:
        raise ValueError('请输入本机 HTTP 或 HTTPS 地址。')
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('本机地址不能包含空白或控制字符。')
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if (url.scheme not in ('http', 'https') or url.username is not None or
                url.password is not None or url.path not in ('', '/') or
                '?' in value or '#' in value or not host):
            raise ValueError
        if host == 'localhost':
            # Do not let a hosts-file change or DNS resolution turn localhost
            # into a remote request. IPv6-only services can use [::1] explicitly.
            host = '127.0.0.1'
        else:
            address = ipaddress.ip_address(host)
            if not address.is_loopback or '%' in host:
                raise ValueError
            host = str(address)
        port = default_port if port is None else port
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('只接受回环地址，不能带用户名、密码、路径、查询参数或片段。') from None
    return LoopbackTarget(url.scheme, host, port)


def get_json(target, endpoint, timeout=2.0):
    """Direct http.client connections do not consult proxy environment variables."""
    connection_type = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(target.host, target.port, timeout=timeout)
    try:
        connection.request('GET', endpoint, headers={'Accept': 'application/json', 'Connection': 'close'})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ProbeError('redirect_refused', '本机接口要求跳转，已拒绝跟随。')
        if response.status != 200:
            raise ProbeError('http_error', f'本机接口返回 HTTP {response.status}。')
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProbeError('response_too_large', '本机接口回复超过 1 MiB，已停止读取。')
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProbeError('invalid_response', '本机接口没有返回有效 JSON。') from None
        if not isinstance(data, dict):
            raise ProbeError('invalid_response', '本机接口返回了不支持的数据结构。')
        return data
    except ssl.SSLError:
        raise ProbeError('tls_error', '本机 HTTPS 证书或安全连接验证未通过。') from None
    except (TimeoutError, socket.timeout):
        raise ProbeError('timeout', '本机服务在限定时间内没有响应。') from None
    except (OSError, http.client.HTTPException):
        raise ProbeError('unreachable', '未能连接本机服务，或连接在响应前中断。') from None
    finally:
        connection.close()


def failure(engine, target, error):
    return {'engine': engine, 'available': False, 'base_url': target.base_url,
            'status': error.status, 'reason': error.reason, 'inference_tested': False}


def probe_ollama(target, timeout=2.0):
    try:
        data = get_json(target, '/api/tags', timeout)
        rows = data.get('models')
        if not isinstance(rows, list):
            raise ProbeError('invalid_response', '接口可达，但没有 Ollama 模型列表。')
        models = []
        seen = set()
        for row in rows[:100]:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                raise ProbeError('invalid_response', 'Ollama 模型列表中存在无法识别的条目。')
            name = row['name']
            if not name or len(name) > 240 or any(ord(char) < 32 for char in name):
                raise ProbeError('invalid_response', 'Ollama 返回了无效模型名。')
            if name in seen:
                continue
            seen.add(name)
            remote = bool(row.get('remote_host') or row.get('remote_model'))
            cloud_hint = 'cloud' in name.lower()
            locality = 'cloud' if remote else ('cloud_name_unverified' if cloud_hint else 'local_candidate')
            models.append({'name': name, 'locality': locality})
        candidates = [row['name'] for row in models if row['locality'] == 'local_candidate']
        reason = '已发现 Ollama 兼容模型接口；尚未加载模型或测试推理。'
        if not models:
            reason = 'Ollama 兼容接口可达，但没有列出已安装模型。'
        elif not candidates:
            reason = 'Ollama 兼容接口可达，但当前列表只有云端或待确认的远程模型。'
        return {'engine': 'ollama', 'available': True, 'base_url': target.base_url,
                'status': 'models_found' if models else 'no_models', 'reason': reason,
                'models': models, 'local_model_candidates': candidates,
                'model_count_reported': len(rows), 'model_list_truncated': len(rows) > 100,
                'inference_tested': False, 'offline_inference_verified': False}
    except ProbeError as error:
        return failure('ollama', target, error)


def probe_comfyui(target, timeout=2.0):
    try:
        data = get_json(target, '/system_stats', timeout)
        system, devices = data.get('system'), data.get('devices')
        if not isinstance(system, dict) or not isinstance(devices, list):
            raise ProbeError('invalid_response', '接口可达，但没有 ComfyUI 系统信息。')
        version = system.get('comfyui_version')
        version = version[:80] if isinstance(version, str) else None
        # system_stats may contain argv, installation paths, hardware and other
        # unnecessary details. Return a strict small allowlist, not the raw data.
        return {'engine': 'comfyui', 'available': True, 'base_url': target.base_url,
                'status': 'service_found', 'reason': '已发现 ComfyUI；模型、工作流和实际生图尚未验证。',
                'version': version, 'device_count': len(devices), 'inference_tested': False,
                'workflow_checked': False, 'offline_inference_verified': False}
    except ProbeError as error:
        return failure('comfyui', target, error)


def probe_codex(local_app_data=None):
    """Look only in the known official install layout; never run a PATH shim."""
    local_app_data = local_app_data if local_app_data is not None else os.environ.get('LOCALAPPDATA')
    result = {'engine': 'codex', 'available': False, 'status': 'not_found',
              'reason': '未在已知官方安装位置发现 codex.exe；这不排除其他位置的安装。',
              'executable': None, 'executed': False, 'login_checked': False,
              'inference_tested': False, 'signature_checked': False}
    if not local_app_data:
        return result
    base = Path(local_app_data) / 'OpenAI' / 'Codex' / 'bin'
    try:
        if not base.is_dir() or base.is_symlink():
            return result
        candidates = [path for path in base.glob('*/codex.exe')
                      if path.is_file() and not path.is_symlink() and not path.parent.is_symlink()]
        if not candidates:
            return result
        executable = max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        result.update(status='access_error', reason='已知 Codex 安装目录无法读取。')
        return result
    result.update(available=True, status='executable_found', executable=str(executable),
                  reason='发现官方安装目录中的程序；未运行、未检查签名、登录或调用参数。')
    return result


def probe(ollama_url='http://127.0.0.1:11434', comfy_url='http://127.0.0.1:8188', timeout=2.0):
    # Validate both requested addresses before making either network request.
    ollama = parse_loopback_url(ollama_url, 11434)
    comfy = parse_loopback_url(comfy_url, 8188)
    engines = {'ollama': probe_ollama(ollama, timeout), 'comfyui': probe_comfyui(comfy, timeout),
               'codex': probe_codex()}
    return {'schema': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'detected_engines': [name for name, row in engines.items() if row['available']],
            'engines': engines, 'read_only': True, 'credentials_read': False,
            'models_downloaded': False, 'services_started': False, 'inference_tested': False}


def write_report(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex[:10] + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--comfy-url', default='http://127.0.0.1:8188')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = probe(args.ollama_url, args.comfy_url)
    except ValueError as error:
        parser.error(str(error))
    if args.output:
        write_report(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
