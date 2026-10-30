"""Remove a PNG background with one already verified local U2NetP model.

No model download, network client, credentials, GPU provider or custom ONNX ops.
Pre/post-processing adapted from danielgatis/rembg sessions/base.py and u2netp.py.
Upstream model: https://github.com/xuebinqin/U-2-Net (Apache-2.0).
Official ONNX: https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx
Published MD5: 8e83ca70e441ab06c318d82300c84806; GitHub asset 85857953.
GitHub does not publish a SHA256 for this old asset. MODEL_SHA256 below pins
the official HTTPS download after its size and published MD5 were verified.

Adapted algorithm license: MIT License, Copyright (c) 2020 Daniel Gatis.
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import time
import warnings

MODEL_BYTES = 4574861
MODEL_MD5 = '8e83ca70e441ab06c318d82300c84806'
MODEL_SHA256 = '309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8'
MODEL_SOURCE = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx'
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 16 * 1024 * 1024


class BackgroundRemovalError(ValueError):
    """A bounded local input, model, inference, or output failure."""


def _local_path(value, title):
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//')) or '://' in raw:
            raise ValueError
        path = Path(raw).expanduser().absolute()
        for item in (path, *path.parents):
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise BackgroundRemovalError(f'{title}不能经过符号链接或目录联接。')
        return path.resolve()
    except (TypeError, ValueError, OSError) as error:
        if isinstance(error, BackgroundRemovalError):
            raise
        raise BackgroundRemovalError(f'{title}必须是明确的本机文件路径，不能是网址或网络共享。') from None


def _model_data(path):
    if not path.is_file() or path.suffix.lower() != '.onnx' or path.stat().st_size != MODEL_BYTES:
        raise BackgroundRemovalError('请选择已核验的 u2netp.onnx（4,574,861 字节）；本工具不会下载模型。')
    with path.open('rb') as handle:
        data = handle.read(MODEL_BYTES + 1)
    if (len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256 or
            hashlib.md5(data, usedforsecurity=False).hexdigest() != MODEL_MD5):
        raise BackgroundRemovalError('模型内容与已核验的官方 U2NetP 摘要不一致，未加载模型。')
    return data


def _load_session(data):
    try:
        import onnxruntime as ort
    except ImportError:
        raise BackgroundRemovalError('当前 Python 缺少 onnxruntime；请使用已配置的本机环境。') from None
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    # Loading the already-hashed bytes also avoids a path-swap between hash/load.
    # Never register custom-op libraries or add a GPU provider.
    session = ort.InferenceSession(data, sess_options=options, providers=['CPUExecutionProvider'])
    if session.get_providers() != ['CPUExecutionProvider']:
        raise BackgroundRemovalError('模型没有严格使用 CPU，已停止处理。')
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if (len(inputs) != 1 or inputs[0].type != 'tensor(float)' or
            list(inputs[0].shape) != [1, 3, 320, 320] or len(outputs) != 7 or
            any(o.type != 'tensor(float)' or list(o.shape) != [1, 1, 320, 320] for o in outputs)):
        raise BackgroundRemovalError('模型输入或输出不是已核验的固定 U2NetP 结构。')
    return session, ort.__version__


def _rollback_created_file(path, identity, expected):
    """Remove only our still-identical complete/partial output after a failure."""
    if identity is None:
        return
    try:
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode) or path.is_symlink() or
                (current.st_dev, current.st_ino) != identity or current.st_size > len(expected)):
            return
        with path.open('rb') as handle:
            current = os.fstat(handle.fileno())
            if (current.st_dev, current.st_ino) != identity:
                return
            actual = handle.read(len(expected) + 1)
        if expected.startswith(actual):
            path.unlink()
    except OSError:
        # Preserve the original write failure if a filesystem refuses cleanup.
        pass


def remove_background(image, output, model):
    """Write a new RGBA PNG and <stem>.background.json, preserving RGB/size."""
    started = time.monotonic()
    image = _local_path(image, '原图')
    output = _local_path(output, '输出')
    model = _local_path(model, '模型')
    report_path = output.with_name(output.stem + '.background.json')
    _local_path(report_path, '记录')
    if output.suffix.lower() != '.png' or image == output or model == output:
        raise BackgroundRemovalError('输出必须是另一个新的 PNG 路径，不能覆盖输入。')
    if output.exists() or report_path.exists():
        raise BackgroundRemovalError('输出图片或记录已经存在，请选择新的输出名称。')
    if not image.is_file() or image.suffix.lower() != '.png' or image.stat().st_size > MAX_IMAGE_BYTES:
        raise BackgroundRemovalError('原图须为最大 32 MiB 的本机静态 PNG。')
    with image.open('rb') as handle:
        source = handle.read(MAX_IMAGE_BYTES + 1)
    if (len(source) > MAX_IMAGE_BYTES or len(source) < 33 or source[:8] != b'\x89PNG\r\n\x1a\n' or
            source[24] != 8 or source[25] not in (2, 6)):
        raise BackgroundRemovalError('只接受 8 位 RGB/RGBA PNG，以保留原始颜色。')
    model_data = _model_data(model)
    try:
        import numpy as np
        from PIL import Image, PngImagePlugin
    except ImportError:
        raise BackgroundRemovalError('当前 Python 缺少 Pillow 或 numpy；请使用已配置的本机环境。') from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as im:
                width, height = im.size
                if (im.format != 'PNG' or im.mode not in ('RGB', 'RGBA') or getattr(im, 'n_frames', 1) != 1 or
                        not 1 <= width <= MAX_SIDE or not 1 <= height <= MAX_SIDE or width * height > MAX_PIXELS):
                    raise BackgroundRemovalError('只接受单边不超过 4096、总像素不超过 16 Mi 的静态 RGB/RGBA PNG。')
                im.load()
                icc_profile = im.info.get('icc_profile')
                rgb = np.array(im.convert('RGB'), dtype=np.uint8)
                old_alpha = np.array(im.getchannel('A'), dtype=np.uint8) if im.mode == 'RGBA' else np.full((height, width), 255, dtype=np.uint8)
                resized = np.array(im.convert('RGB').resize((320, 320), Image.Resampling.LANCZOS), dtype=np.float64)
        maximum = max(float(resized.max()), 1e-6)
        values = resized / maximum
        values = (values - np.array([.485, .456, .406])) / np.array([.229, .224, .225])
        tensor = np.ascontiguousarray(values.transpose(2, 0, 1)[None], dtype=np.float32)
        session, version = _load_session(model_data)
        predicted = session.run(None, {session.get_inputs()[0].name: tensor})
        if len(predicted) != 7:
            raise BackgroundRemovalError('模型没有返回完整的 U2NetP 输出。')
        pred = np.asarray(predicted[0])
        if pred.shape != (1, 1, 320, 320) or pred.dtype != np.float32 or not np.isfinite(pred).all():
            raise BackgroundRemovalError('模型遮罩的尺寸、类型或数值无效，未保存图片。')
        low, high = float(pred.min()), float(pred.max())
        if high - low < 1e-8:
            raise BackgroundRemovalError('模型没有分辨出前景和背景，未保存空白结果。')
        normalized = np.clip((pred[0, 0] - low) / (high - low), 0, 1)
        mask = Image.fromarray((normalized * 255).astype(np.uint8)).resize((width, height), Image.Resampling.LANCZOS)
        alpha = ((np.asarray(mask, dtype=np.uint16) * old_alpha.astype(np.uint16) + 127) // 255).astype(np.uint8)
        if int(alpha.min()) == int(alpha.max()):
            raise BackgroundRemovalError('结果没有有效透明变化，未保存不可用的抠图。')
        rgba = np.dstack((rgb, alpha))
        buffer = io.BytesIO()
        # Preserve only color-management chunks, not source prompts/EXIF/text.
        pnginfo = PngImagePlugin.PngInfo()
        position = 8
        while position + 12 <= len(source):
            length = int.from_bytes(source[position:position + 4], 'big')
            kind = source[position + 4:position + 8]
            if position + 12 + length > len(source):
                raise BackgroundRemovalError('PNG 块不完整。')
            if kind in (b'gAMA', b'cHRM', b'sRGB'):
                pnginfo.add(kind, source[position + 8:position + 8 + length])
            position += length + 12
            if kind == b'IEND':
                break
        Image.fromarray(rgba).save(buffer, format='PNG', pnginfo=pnginfo, icc_profile=icc_profile)
        encoded = buffer.getvalue()
        if len(encoded) > MAX_IMAGE_BYTES:
            raise BackgroundRemovalError('输出 PNG 超过 32 MiB，未保存。')
    except BackgroundRemovalError:
        raise
    except Exception as error:
        raise BackgroundRemovalError('本地去背景失败：' + type(error).__name__ + '。原图保持不变。') from None
    result = {'schema': 1, 'status': 'completed', 'image': str(output), 'report': str(report_path),
              'input': str(image), 'input_sha256': hashlib.sha256(source).hexdigest(),
              'output_sha256': hashlib.sha256(encoded).hexdigest(), 'model_sha256': MODEL_SHA256,
              'model_verified': True, 'model_source': MODEL_SOURCE, 'model_published_md5': MODEL_MD5,
              'model_sha256_origin': 'pinned after official HTTPS download and published MD5 verification',
              'width': width, 'height': height, 'alpha_min': int(alpha.min()), 'alpha_max': int(alpha.max()),
              'foreground_pixels': int((alpha > 127).sum()), 'transparent_pixels': int((alpha == 0).sum()),
              'rgb_preserved': True, 'existing_alpha_preserved': True, 'alpha_combination': 'multiply_existing_alpha',
              'provider': 'CPUExecutionProvider', 'onnxruntime_version': version,
              'telemetry_disabled': True, 'custom_ops_registered': False,
              'network_used': False, 'models_downloaded': False, 'credentials_read': False,
              'needs_visual_review': True, 'algorithm_license': 'rembg MIT; U-2-Net Apache-2.0',
              'elapsed_seconds': round(time.monotonic() - started, 3), 'created_at': datetime.now(timezone.utc).isoformat()}
    output.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output_identity = report_identity = None
    try:
        with output.open('xb') as handle:
            created = os.fstat(handle.fileno())
            output_identity = (created.st_dev, created.st_ino)
            handle.write(encoded)
        with report_path.open('x', encoding='utf-8', newline='\n') as handle:
            created = os.fstat(handle.fileno())
            report_identity = (created.st_dev, created.st_ino)
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    except OSError as error:
        _rollback_created_file(report_path, report_identity, report_bytes)
        _rollback_created_file(output, output_identity, encoded)
        raise BackgroundRemovalError('无法保存新的图片和记录：' + type(error).__name__ + '。没有覆盖已有文件。') from None
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='使用已核验的本机 U2NetP，仅在 CPU 去背景；不下载、不联网。')
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = remove_background(args.image, args.output, args.model)
    except (BackgroundRemovalError, OSError) as error:
        print(json.dumps({'status': 'failed', 'reason': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
