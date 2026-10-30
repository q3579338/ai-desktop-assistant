"""Small local ComfyUI image-to-image job using only existing core nodes/models.

Standard-library only. Does not install models, read authentication, start services,
or interrupt other jobs. A trusted local ComfyUI installation is a prerequisite.
"""
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import ssl
import struct
import time
from urllib.parse import urlencode
import uuid
import zlib

if __package__:
    from .probe_local_runtime import parse_loopback_url
else:
    from probe_local_runtime import parse_loopback_url

MAX_PNG = 16 * 1024 * 1024
MAX_JSON = 512 * 1024
ILLUSTRATION_STYLE = (
    '2D hand-drawn illustration, expressive clean line art, soft painted shading, '
    'appealing silhouette, harmonious colors, polished desktop companion artwork. '
    'Preserve the reference identity, face, eye color, markings and accessories. '
    'Cats are adorable and rounded; human characters are attractive adults. '
    'Keep ears, hair, paws or hands, feet and tail inside the canvas. '
)
ILLUSTRATION_NEGATIVE = '3D render, CGI, plastic figurine, low-poly, photorealistic, turntable, wireframe'
NODES = {
    'LoadImage': {'image'},
    'CheckpointLoaderSimple': {'ckpt_name'},
    'CLIPTextEncode': {'text', 'clip'},
    'VAEEncode': {'pixels', 'vae'},
    'KSampler': {'model', 'seed', 'steps', 'cfg', 'sampler_name', 'scheduler', 'positive', 'negative', 'latent_image', 'denoise'},
    'VAEDecode': {'samples', 'vae'},
    'SaveImage': {'images', 'filename_prefix'},
}


class GenerationError(Exception):
    def __init__(self, message, *, prompt_id=None, report=None):
        super().__init__(message)
        self.prompt_id = prompt_id
        self.report = report


class GenerationCancelled(GenerationError):
    """Only stop this caller's wait; never interrupt a shared ComfyUI queue."""


def check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCancelled('本机等待已停止；已提交的任务可能继续执行，未中断服务任务。')


def request(target, method, path, body=None, content_type='application/json', *, limit=MAX_JSON, timeout=10):
    """Direct numeric loopback socket; no proxy lookup and no redirect handling."""
    connection_type = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(target.host, target.port, timeout=timeout)
    response = None
    try:
        connection.request(method, path, body=body, headers={'Content-Type': content_type, 'Accept': 'application/json, image/png', 'Connection': 'close'})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise GenerationError('本机服务要求重定向，已拒绝跟随。')
        if response.status != 200:
            raise GenerationError(f'本机接口返回 HTTP {response.status}。')
        length = response.getheader('Content-Length')
        if length is not None:
            try:
                if not 0 <= int(length) <= limit:
                    raise GenerationError('本机接口回复超过允许大小。')
            except ValueError:
                raise GenerationError('本机接口返回无效长度。') from None
        data = response.read(limit + 1)
        if len(data) > limit:
            raise GenerationError('本机接口回复超过允许大小。')
        return data
    except ssl.SSLError:
        raise GenerationError('本机 HTTPS 证书验证失败。') from None
    except (OSError, http.client.HTTPException):
        raise GenerationError('本机连接超时、中断或不可用。') from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def json_request(target, method, path, payload=None, *, timeout=10):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    raw = request(target, method, path, body, timeout=timeout)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise GenerationError('本机接口未返回有效 JSON。') from None
    if not isinstance(data, dict):
        raise GenerationError('本机接口 JSON 结构不匹配。')
    return data


def png_dimensions(data):
    """Validate bounded non-interlaced 8-bit RGB/RGBA PNG without a GUI decoder."""
    if not 8 < len(data) <= MAX_PNG or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise GenerationError('图片必须是最大 16 MiB 的有效 PNG。')
    offset, header, compressed, ended, after_idat = 8, None, bytearray(), False, False
    while offset < len(data):
        if offset + 12 > len(data):
            raise GenerationError('PNG 数据不完整。')
        size = struct.unpack_from('>I', data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data) or size > MAX_PNG:
            raise GenerationError('PNG 块大小不合法。')
        value = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from('>I', data, offset + 8 + size)[0]
        if zlib.crc32(kind + value) & 0xffffffff != crc:
            raise GenerationError('PNG 校验失败。')
        if header is None:
            if kind != b'IHDR' or size != 13:
                raise GenerationError('PNG 缺少有效图像头。')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', value)
            if not (256 <= width <= 1024 and 256 <= height <= 1024 and width % 8 == height % 8 == 0):
                raise GenerationError('输入和输出 PNG 单边须为 256～1024 且是 8 的倍数。')
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise GenerationError('当前只接受非交错、8 位 RGB 或 RGBA 静态 PNG。')
            header = (width, height, 3 if color == 2 else 4)
        elif kind == b'IHDR' or kind in (b'acTL', b'fcTL', b'fdAT'):
            raise GenerationError('不接受重复图像头或动画 PNG。')
        elif kind == b'IDAT':
            if after_idat:
                raise GenerationError('PNG 图像数据块顺序不合法。')
            compressed.extend(value)
        elif kind == b'IEND':
            if size or end != len(data) or not compressed:
                raise GenerationError('PNG 结束数据不合法。')
            ended = True
            break
        else:
            if compressed:
                after_idat = True
            if not re.fullmatch(b'[A-Za-z]{4}', kind) or (kind[0] & 32 == 0 and kind != b'PLTE'):
                raise GenerationError('PNG 包含未知关键块。')
        offset = end
    if not ended or header is None:
        raise GenerationError('PNG 未完整结束。')
    width, height, channels = header
    expected = height * (width * channels + 1)
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise GenerationError('PNG 解码长度超限或数据不完整。')
        stride = width * channels + 1
        if any(pixels[row * stride] > 4 for row in range(height)):
            raise GenerationError('PNG 使用了无效行过滤器。')
    except zlib.error:
        raise GenerationError('PNG 压缩内容无效。') from None
    return width, height


def model_name(value):
    if not isinstance(value, str) or not value or len(value) > 240 or any(ord(c) < 32 for c in value):
        raise GenerationError('请指定 ComfyUI 已列出的 checkpoint 名称。')
    parts = value.replace('\\', '/').split('/')
    if any(part in ('', '.', '..') for part in parts) or ':' in value or value.startswith(('/', '\\')):
        raise GenerationError('checkpoint 必须是已安装列表中的安全相对名称。')
    return value


def check_nodes(target, checkpoint):
    found = {}
    for name, inputs in NODES.items():
        row = json_request(target, 'GET', '/object_info/' + name).get(name)
        if not isinstance(row, dict) or row.get('python_module') != 'nodes' or row.get('api_node'):
            raise GenerationError(f'缺少官方核心节点 {name}，或节点来源不符合此流程。')
        declared = row.get('input')
        required = declared.get('required') if isinstance(declared, dict) else None
        if not isinstance(required, dict) or set(required) != inputs:
            raise GenerationError(f'核心节点 {name} 的参数不兼容，尚未上传图片。')
        found[name] = required
    choices = found['CheckpointLoaderSimple']['ckpt_name']
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], list) or checkpoint not in choices[0]:
        raise GenerationError('指定 checkpoint 未在本机实际模型枚举中找到，尚未上传图片。')
    for field, choice in (('sampler_name', 'euler'), ('scheduler', 'normal')):
        values = found['KSampler'][field]
        if not isinstance(values, list) or not values or not isinstance(values[0], list) or choice not in values[0]:
            raise GenerationError('当前 KSampler 不支持所需的 euler / normal。')
    return found


def upload(target, data, filename):
    boundary = 'assistant-' + uuid.uuid4().hex
    chunks = []
    for key, value in (('type', 'input'), ('subfolder', ''), ('overwrite', 'false')):
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode(), data,
                   f'\r\n--{boundary}--\r\n'.encode()])
    raw = request(target, 'POST', '/upload/image', b''.join(chunks), 'multipart/form-data; boundary=' + boundary)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise GenerationError('上传结果不是有效 JSON。') from None
    if not isinstance(result, dict) or result.get('name') != filename or result.get('subfolder') != '' or result.get('type') != 'input':
        raise GenerationError('上传返回的图片路径与本次唯一文件名不一致，未排队。')
    return filename


def build_graph(filename, checkpoint, positive, negative, prefix, *, seed, steps, cfg, denoise):
    return {
        '1': {'class_type': 'LoadImage', 'inputs': {'image': filename}},
        '2': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': checkpoint}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': positive, 'clip': ['2', 1]}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': negative, 'clip': ['2', 1]}},
        '5': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['1', 0], 'vae': ['2', 2]}},
        '6': {'class_type': 'KSampler', 'inputs': {'model': ['2', 0], 'positive': ['3', 0], 'negative': ['4', 0], 'latent_image': ['5', 0],
              'seed': seed, 'steps': steps, 'cfg': cfg, 'denoise': denoise, 'sampler_name': 'euler', 'scheduler': 'normal'}},
        '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['6', 0], 'vae': ['2', 2]}},
        '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': prefix}},
    }


def save_report(path, report):
    # Only update the report in the exclusively created directory for this run.
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def generate(image, output_dir, checkpoint, prompt, *, url='http://127.0.0.1:8188', negative='', steps=16, cfg=6.0,
             denoise=.55, seed=None, timeout=300, poll_interval=.5, cancel_event=None):
    check_cancel(cancel_event)
    target = parse_loopback_url(url, 8188)
    checkpoint = model_name(checkpoint)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000 or not isinstance(negative, str) or len(negative) > 2000:
        raise GenerationError('正向提示不能为空；正负提示各最多 2000 字。')
    # One shared policy covers the desktop wizard and the standalone Skill CLI.
    # This is an illustration request, not a mesh/rig/texture generation route.
    prompt = ILLUSTRATION_STYLE + '\nRequested changes: ' + prompt.strip()
    negative = ILLUSTRATION_NEGATIVE + (', ' + negative.strip() if negative.strip() else '')
    if type(steps) is not int or not 1 <= steps <= 30:
        raise GenerationError('步数须为 1～30。')
    for value, lower, upper, title in ((cfg, 1, 12, 'CFG'), (denoise, .05, 1, '重绘强度'), (timeout, .1, 1800, '等待秒数'), (poll_interval, .01, 10, '轮询间隔')):
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise GenerationError(f'{title}超出允许范围。')
    seed = secrets.randbits(63) if seed is None else seed
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise GenerationError('随机种子须为 0～2^63-1 的整数。')
    image = Path(image)
    if image.suffix.lower() != '.png' or not image.is_file() or image.stat().st_size > MAX_PNG:
        raise GenerationError('请选择最大 16 MiB 的本地 PNG 文件。')
    with image.open('rb') as source:
        data = source.read(MAX_PNG + 1)
    dimensions = png_dimensions(data)
    check_cancel(cancel_event)
    check_nodes(target, checkpoint)
    check_cancel(cancel_event)
    base = Path(output_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    prefix = 'assistant_' + nonce
    folder = base / ('local-image-' + nonce)
    folder.mkdir()  # Existing paths, including links, are never reused.
    report_path = folder / 'job.json'
    requested_id = str(uuid.uuid4())
    report = {'schema': 1, 'status': 'uploading', 'endpoint': target.base_url, 'requested_prompt_id': requested_id, 'prompt_id': None,
              'checkpoint': checkpoint, 'input_sha256': hashlib.sha256(data).hexdigest(), 'width': dimensions[0], 'height': dimensions[1],
              'steps': steps, 'cfg': cfg, 'denoise': denoise, 'seed': seed, 'output_prefix': prefix,
              'models_downloaded': False, 'services_started': False, 'credentials_read': False,
              'generation_kind': 'static_image_to_image', 'art_style': '2d_illustration', 'server_may_continue': False}
    save_report(report_path, report)
    prompt_id, deadline = None, None
    try:
        check_cancel(cancel_event)
        filename = upload(target, data, prefix + '_input.png')
        check_cancel(cancel_event)
        graph = build_graph(filename, checkpoint, prompt, negative, prefix, seed=seed, steps=steps, cfg=cfg, denoise=denoise)
        report.update(status='submission_unknown', server_may_continue=True)
        save_report(report_path, report)
        if cancel_event is not None and cancel_event.is_set():
            report.update(status='client_stopped', server_may_continue=False)
            check_cancel(cancel_event)
        queued = json_request(target, 'POST', '/prompt', {'prompt': graph, 'client_id': str(uuid.uuid4()), 'prompt_id': requested_id})
        actual_id = queued.get('prompt_id')
        if not isinstance(actual_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', actual_id):
            raise GenerationError('排队回复无有效任务编号或含节点错误；请按记录核对本次任务。')
        prompt_id = actual_id
        report.update(status='queued', prompt_id=prompt_id)
        save_report(report_path, report)
        if queued.get('node_errors'):
            raise GenerationError('排队回复含节点错误；已保留服务返回的任务编号。')
        deadline = time.monotonic() + timeout
        while True:
            check_cancel(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report['status'] = 'timed_out'
                raise GenerationError('等待本次任务超时；编号已保留，服务可能继续执行，未中断其它任务。')
            history = json_request(target, 'GET', '/history/' + prompt_id, timeout=min(10, remaining))
            check_cancel(cancel_event)
            row = history.get(prompt_id)
            if row is not None:
                if not isinstance(row, dict) or not isinstance(row.get('status'), dict):
                    raise GenerationError('本次任务历史结构不兼容。')
                status = row['status']
                if status.get('status_str') == 'error':
                    report.update(status='generation_failed', server_may_continue=False)
                    raise GenerationError('本次生图任务失败；请在本机 ComfyUI 查看该任务的节点错误。')
                if status.get('completed') is True and status.get('status_str') == 'success':
                    recorded = row.get('prompt')
                    if not isinstance(recorded, list) or len(recorded) < 3 or recorded[1] != prompt_id or recorded[2] != graph:
                        raise GenerationError('任务历史与本次提交的工作流不一致，拒绝取图。')
                    break
            delay = min(poll_interval, max(0, deadline - time.monotonic()))
            if cancel_event is None:
                time.sleep(delay)
            else:
                cancel_event.wait(delay)
                check_cancel(cancel_event)
        report['server_may_continue'] = False
        outputs = row.get('outputs', {})
        if not isinstance(outputs, dict) or set(outputs) != {'8'} or not isinstance(outputs['8'], dict):
            raise GenerationError('任务输出节点与本次 SaveImage 节点不一致。')
        images = outputs['8'].get('images')
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise GenerationError('当前小型流程只接受本次生成的一张 PNG。')
        output = images[0]
        name = output.get('filename')
        if (not isinstance(name, str) or not re.fullmatch(re.escape(prefix) + r'_[0-9]+_?\.png', name)
                or output.get('subfolder') != '' or output.get('type') != 'output'):
            raise GenerationError('输出路径越界或不属于本次任务，拒绝下载。')
        parameters = urlencode({'filename': name, 'subfolder': '', 'type': 'output'})
        check_cancel(cancel_event)
        png = request(target, 'GET', '/view?' + parameters, limit=MAX_PNG)
        check_cancel(cancel_event)
        if png_dimensions(png) != dimensions:
            raise GenerationError('输出尺寸与本次输入不一致，拒绝保存。')
        portrait = folder / 'portrait.png'
        with portrait.open('xb') as destination:
            destination.write(png)
        report.update(status='completed', image=str(portrait), output_sha256=hashlib.sha256(png).hexdigest(),
                      elapsed_seconds=round(timeout - max(0, deadline - time.monotonic()), 3))
        save_report(report_path, report)
        return {**report, 'report': str(report_path)}
    except (GenerationError, OSError, KeyboardInterrupt) as error:
        cancelled = isinstance(error, (KeyboardInterrupt, GenerationCancelled))
        if deadline is not None and time.monotonic() >= deadline and report['status'] == 'queued':
            report['status'] = 'timed_out'
        if report['status'] not in ('timed_out', 'generation_failed', 'submission_unknown'):
            report['status'] = 'client_stopped' if cancelled else 'failed'
        report['reason'] = '本机等待已停止；未中断服务任务。' if cancelled else str(error)
        save_report(report_path, report)
        raise GenerationError(report['reason'], prompt_id=prompt_id, report=str(report_path)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8188')
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--negative', default='')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=16)
    parser.add_argument('--cfg', type=float, default=6)
    parser.add_argument('--denoise', type=float, default=.55)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args(argv)
    try:
        result = generate(args.image, args.output_dir, args.checkpoint, args.prompt, url=args.url, negative=args.negative,
                          steps=args.steps, cfg=args.cfg, denoise=args.denoise, seed=args.seed, timeout=args.timeout)
    except (GenerationError, ValueError, OSError) as error:
        print(json.dumps({'status': 'failed', 'reason': str(error), 'prompt_id': getattr(error, 'prompt_id', None), 'report': getattr(error, 'report', None)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
