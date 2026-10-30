"""Create a verified PNG assistant role pack; local repainting is explicit opt-in."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

import build_rolepack
import generate_local_image
from probe_local_runtime import parse_loopback_url


MAX_SOURCE = 32 * 1024 * 1024
STAGE_NAMES = {'validate': '检查输入', 'copy_source': '保存原图副本', 'generate': '本机重绘',
               'remove_background': '本机去背景',
               'package': '构建并重新导入角色包', 'verify': '核对交付文件',
               'report': '写入交付说明', 'complete': '完成'}
JOB_STATUSES = {'uploading', 'submission_unknown', 'queued', 'timed_out', 'generation_failed',
                'client_stopped', 'failed', 'completed'}


class CreationError(ValueError):
    """Controlled user-facing message without backend output or settings."""


def _owned_file(value, folder):
    path = Path(value)
    if not path.is_absolute():
        raise CreationError('工具返回的交付文件路径不完整。')
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise CreationError('交付文件不能使用链接或联接目录。')
    path = path.resolve(strict=True)
    if not path.is_relative_to(folder) or not path.is_file():
        raise CreationError('工具返回的文件不属于本次制作目录。')
    return path


def _read_bounded(path, limit):
    if path.stat().st_size > limit:
        raise CreationError('输入文件超过允许大小。')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CreationError('输入文件超过允许大小。')
    return data


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _remove_background(image, output, model):
    # Optional dependency stays unloaded on the default PNG-only path.
    from remove_local_background import remove_background
    return remove_background(image, output, model)


@contextmanager
def _package_workspace():
    # The authoritative builder creates nested role-library UUID directories.
    # A short independent staging root avoids Windows MAX_PATH failures when
    # the user chooses a deep delivery directory. No permanent state is here.
    base = Path(tempfile.gettempdir()).resolve()
    base.mkdir(parents=True, exist_ok=True)
    folder = base / ('ada-' + uuid.uuid4().hex[:12])
    folder.mkdir()  # Inherit normal ACLs; do not reuse another invocation.
    try:
        yield folder
    finally:
        resolved = folder.resolve()
        if resolved.parent != base or resolved != folder or not resolved.name.startswith('ada-'):
            raise CreationError('临时构建目录发生变化，已停止自动清理。')
        shutil.rmtree(resolved)


def _job_summary(value, folder):
    """Keep only bounded protocol identifiers; never copy backend text/config."""
    result = {}
    if isinstance(value, dict):
        if value.get('status') in JOB_STATUSES:
            result['status'] = value['status']
        for key in ('prompt_id', 'requested_prompt_id'):
            identity = value.get(key)
            if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
                result[key] = identity
        if type(value.get('server_may_continue')) is bool:
            result['server_may_continue'] = value['server_may_continue']
    return result


def _generation_failure(error, folder):
    summary = ({'status': 'failed_before_job_record', 'server_may_continue': False}
               if isinstance(error, generate_local_image.GenerationError) else
               {'status': 'unknown', 'server_may_continue': True})
    path = getattr(error, 'report', None)
    if path:
        # An unreadable existing record is uncertain, never grounds to resubmit.
        summary = {'status': 'unknown', 'server_may_continue': True}
        try:
            report = _owned_file(path, folder)
            value = json.loads(_read_bounded(report, 512 * 1024))
            summary.update(_job_summary(value, folder), report=report.relative_to(folder).as_posix())
        except (OSError, ValueError, TypeError, RecursionError):
            pass
    identity = getattr(error, 'prompt_id', None)
    if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
        summary['prompt_id'] = identity
        if summary['status'] == 'failed_before_job_record':
            summary.update(status='unknown', server_may_continue=True)
    return summary


def _readme(report):
    successful = report['status'] == 'completed'
    lines = ['# 你的 AI 桌面助手角色', '',
             '制作状态：' + ('已完成，并已重新导入验证。' if successful else
                          '未完成，停在“' + STAGE_NAMES.get(report['stage'], report['stage']) + '”。'), '']
    if successful:
        lines += ['- 角色包：`assistant.rolepack.zip`。',
                  '- 在 AI 桌面助手的“角色”页面选择“导入角色包”，再选择是否使用。',
                  '- 创建工具没有切换当前角色，也没有改写真实聊天、记忆或模型设置。']
    else:
        lines += [report.get('message', '请核对 result.json 中的制作阶段。'),
                  '本次没有交付经过完整核验的角色包。已有文件仅用于检查，不应当作完成结果导入。']
    if report.get('source'):
        lines += ['', '原图副本：`'+report['source']['copy']+'`，保留读取时的原始字节。原始文件没有被本工具改写。']
    if report.get('normalization'):
        lines += ['内部PNG：`source.png`，已按照片方向显示，保留透明度。'+('动态/多页只取第一帧；完整原文件仍保留。' if report['normalization']['first_frame_only'] else '')]
    generated = report.get('generation', {})
    background = report.get('background_removal', {})
    if generated.get('image'):
        lines += ['本机出图：`' + generated['image'] + '`。' +
                  ('这是去背景前的重绘图。' if background.get('image') else '角色包使用这张出图。')]
    elif not report['generation_requested']:
        lines += ['图像来源：已有图片（必要时标准化为PNG）；没有调用生图模型。']
    if generated.get('report'):
        lines += ['本次生图记录：`' + generated['report'] + '`。']
    if generated.get('server_may_continue'):
        lines += ['', '本机服务可能仍在执行这一次任务。请先按记录中的任务编号检查；本工具没有自动重试，也没有中断其他任务。']
    if background.get('image'):
        lines += ['', '去背景图：`' + background['image'] + '`。角色包使用这张图片，原图和重绘图都保留。',
                  '本机去背景已生成透明度通道；仍需目视检查头发、衣边和细小部件，不能把重新导入验证当作边缘质量验收。',
                  '去背景记录：`' + background['report'] + '`。']
    elif not report['background_removal_requested']:
        lines += ['', '本次没有请求去背景；透明度沿用输入图片，不保证有透明背景。']
    lines += ['', '这是静态 PNG 角色包，可使用程序提供的互动效果；没有自动生成连续动画、Live2D 或三维骨骼。',
              '本工具不会安装依赖、下载模型、启动服务或读取账号凭据。机器可读结果见 `result.json`。', '']
    return '\n'.join(lines)


def create(image, name, app_source, output_dir, *, persona='', personality='playful',
           generate=False, comfy_url=None, checkpoint=None, prompt=None,
           negative='', steps=16, cfg=6.0, denoise=.55, seed=None, timeout=300,
           remove_background=False, background_model=None):
    """Return a stage report; every invocation owns a new assistant-UUID folder.

    The existing image generator owns network restrictions and unknown-job
    handling. The existing builder owns image parsing and import verification.
    Only explicit role data enters the role pack; full tool results are filtered.
    """
    base = Path(output_dir).absolute()
    for item in (base, *base.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise CreationError('输出目录不能使用链接或联接目录。')
    base.mkdir(parents=True, exist_ok=True)
    folder = base.resolve() / ('assistant-' + uuid.uuid4().hex)
    folder.mkdir()  # An existing delivery, even a UUID collision, is not reused.
    manifest = folder / 'result.json'
    report = {'schema': 1, 'status': 'running', 'stage': 'validate', 'created': False,
              'output_dir': str(folder), 'generation_requested': bool(generate),
              'background_removal_requested': bool(remove_background),
              'completed_stages': [], 'production_state_used': False,
              'models_downloaded': False, 'services_started': False, 'credentials_read': False,
              'rendering': 'static_png_with_program_interaction'}
    original = None
    original_hash = None

    def stage(value):
        report['stage'] = value
        generate_local_image.save_report(manifest, report)

    try:
        stage('validate')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 40
                or any(ord(char) < 32 for char in name)):
            raise CreationError('助手名字须为 1～40 字，且不能带控制字符。')
        if not isinstance(persona, str) or len(persona) > 2000 or '\x00' in persona:
            raise CreationError('角色设定最多 2000 字，且不能带空字符。')
        if personality not in build_rolepack.PERSONALITIES:
            raise CreationError('说话风格只支持 gentle、playful 或 concise。')
        if generate:
            if not comfy_url or not checkpoint or not prompt:
                raise CreationError('本机重绘需要同时指定 --comfy-url、--checkpoint 和 --prompt。')
            comfy_url = parse_loopback_url(comfy_url, 8188).base_url
        elif comfy_url is not None or checkpoint is not None or prompt is not None or negative:
            raise CreationError('重绘参数必须与 --generate 一起明确使用；默认只打包已有图片。')
        if remove_background:
            if background_model is None:
                raise CreationError('去背景需要 --background-model 指向已安装并经过摘要核对的本机模型。')
            background_model = Path(background_model).resolve(strict=True)
            if not background_model.is_file():
                raise CreationError('去背景模型文件不存在。')
        elif background_model is not None:
            raise CreationError('去背景模型必须与 --remove-background 一起明确使用。')
        try:
            root = build_rolepack.find_source(app_source)
        except ValueError:
            raise CreationError('未找到助手源码，请检查 --app-source 是否包含 main.py 和 starpet/rolepacks.py。') from None
        original = Path(image).resolve(strict=True)
        if original.suffix.lower() not in build_rolepack.INPUT_EXTENSIONS or not original.is_file():
            raise CreationError('请选择已存在的PNG、JPEG、WebP、BMP、TIFF或GIF原图。')
        data = _read_bounded(original, MAX_SOURCE)
        if original.suffix.lower()=='.png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise CreationError('原图不是 PNG；请提供现有静态 PNG。')
        original_hash = _digest(data)
        report.update(name=name.strip(), personality=personality)
        report['completed_stages'].append('validate')

        stage('copy_source')
        preserved = folder / ('source'+original.suffix.lower())
        with preserved.open('xb') as target:
            target.write(data)
        report['source'] = {'copy':preserved.name, 'sha256': original_hash, 'unchanged': None}
        snapshot=folder/'source.png'
        if original.suffix.lower()!='.png':
            try:report['normalization']=build_rolepack.normalize_input(data,original.suffix,snapshot,root)
            except ValueError as error:raise CreationError(str(error)) from None
            report['normalization']['sha256']=_digest(_read_bounded(snapshot,MAX_SOURCE))
        snapshot_hash=_digest(_read_bounded(snapshot,MAX_SOURCE))
        report['completed_stages'].append('copy_source')
        selected = snapshot
        if generate:
            stage('generate')
            generated = generate_local_image.generate(snapshot, folder / 'generation', checkpoint, prompt,
                url=comfy_url, negative=negative, steps=steps, cfg=cfg, denoise=denoise, seed=seed, timeout=timeout)
            if not isinstance(generated, dict) or generated.get('status') != 'completed':
                raise CreationError('本机重绘没有返回明确完成结果，未继续打包。')
            job_report = _owned_file(generated.get('report', ''), folder)
            report['generation'] = {**_job_summary(generated, folder),
                                    'report': job_report.relative_to(folder).as_posix()}
            selected = _owned_file(generated.get('image', ''), folder)
            if selected.suffix.lower() != '.png':
                raise CreationError('本机重绘没有返回本次目录中的 PNG。')
            selected_hash = _digest(_read_bounded(selected, generate_local_image.MAX_PNG))
            if generated.get('input_sha256') != snapshot_hash or generated.get('output_sha256') != selected_hash:
                raise CreationError('本机出图与本次输入或输出摘要不一致。')
            report['generation'] = {**_job_summary(generated, folder), 'image': selected.relative_to(folder).as_posix(),
                                    'report': job_report.relative_to(folder).as_posix(), 'sha256': selected_hash}
            report['completed_stages'].append('generate')

        if remove_background:
            stage('remove_background')
            previous_hash = _digest(_read_bounded(selected, MAX_SOURCE))
            cutout = folder / 'cutout.png'
            background = _remove_background(selected, cutout, background_model)
            if (not isinstance(background, dict) or background.get('status') != 'completed'
                    or background.get('model_verified') is not True or background.get('rgb_preserved') is not True):
                raise CreationError('本机去背景没有返回已核验模型和保留原色的完成记录。')
            actual = _owned_file(background.get('image', ''), folder)
            if actual != cutout:
                raise CreationError('去背景返回的图片不属于本次指定输出。')
            cutout_hash = _digest(_read_bounded(actual, MAX_SOURCE))
            if background.get('input_sha256') != previous_hash or background.get('output_sha256') != cutout_hash:
                raise CreationError('去背景输入或输出摘要不一致，未继续打包。')
            model_hash = background.get('model_sha256')
            if not isinstance(model_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', model_hash):
                raise CreationError('去背景缺少已核对的模型摘要。')
            low, high = background.get('alpha_min'), background.get('alpha_max')
            if type(low) is not int or type(high) is not int or not 0 <= low < high <= 255:
                raise CreationError('去背景没有得到有效前景与透明区域，请检查图片后再制作。')
            background_report = _owned_file(background.get('report', ''), folder)
            report['background_removal'] = {'image': actual.relative_to(folder).as_posix(),
                'report': background_report.relative_to(folder).as_posix(), 'input_sha256': previous_hash,
                'output_sha256': cutout_hash, 'model_sha256': model_hash, 'model_verified': True,
                'rgb_preserved': True, 'alpha_min': low, 'alpha_max': high, 'needs_visual_review': True}
            selected = actual
            report['completed_stages'].append('remove_background')

        stage('package')
        package = folder / 'assistant.rolepack.zip'
        source_kind = ('local_img2img_background_removal' if generate and remove_background else
                       'local_img2img' if generate else 'local_background_removal' if remove_background else 'user_image')
        with _package_workspace() as workspace:
            temporary_package = workspace / 'role.zip'
            built = build_rolepack.build(selected, temporary_package, name.strip(), persona, root,
                                         personality=personality, source_kind=source_kind)
            if (not isinstance(built, dict) or built.get('created') is not True
                    or built.get('import_verified') is not True or built.get('source_unchanged') is not True
                    or built.get('production_state_used') is not False or built.get('model_called') is not False
                    or built.get('source_kind') != source_kind):
                raise CreationError('角色包没有完成独立重新导入验证。')
            package_data = _read_bounded(_owned_file(str(temporary_package), workspace), 40 * 1024 * 1024)
            with package.open('xb') as destination:
                destination.write(package_data)
        report['completed_stages'].append('package')

        stage('verify')
        package = _owned_file(str(package), folder)
        selected_hash = _digest(_read_bounded(selected, MAX_SOURCE))
        package_hash = _digest(_read_bounded(package, 40 * 1024 * 1024))
        if built.get('sha256') != package_hash or built.get('source_sha256') != selected_hash:
            raise CreationError('角色包或打包图片摘要不一致。')
        if _digest(_read_bounded(original, MAX_SOURCE)) != original_hash:
            report['source']['unchanged'] = False
            raise CreationError('制作期间原图被其他操作改变；本次停止交付。')
        report['source']['unchanged'] = True
        report['completed_stages'].append('verify')
        report.update(status='completed', stage='complete', created=True,
                      rolepack={'file': package.name, 'sha256': package_hash, 'import_verified': True,
                                'image': selected.relative_to(folder).as_posix(), 'image_sha256': selected_hash,
                                'source_kind': source_kind},
                      message='角色包已完成并重新导入验证，可在程序中手动导入。' +
                              ('去背景边缘仍需目视检查。' if remove_background else ''))
    except (Exception, KeyboardInterrupt) as error:
        report.update(status='cancelled' if isinstance(error, KeyboardInterrupt) else 'failed', created=False,
                      message=str(error) if isinstance(error, CreationError) else
                      STAGE_NAMES.get(report['stage'], '制作') + '未完成；请核对本次阶段和已有记录。')
        if report['stage'] == 'generate':
            if 'generation' not in report or getattr(error, 'report', None):
                report['generation'] = _generation_failure(error, folder)
        # Do not include exception text from libraries, arguments, settings,
        # environment variables, prompt content, or persona in failure reports.
    try:
        generate_local_image.save_report(manifest, report)
        with (folder / 'README.md').open('x', encoding='utf-8') as output:
            output.write(_readme(report))
    except OSError:
        report.update(status='failed', stage='report', created=False, message='交付说明未能完整写入，不能按完成交付。')
        try:
            generate_local_image.save_report(manifest, report)
        except OSError:
            pass
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--app-source', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--persona', default='')
    parser.add_argument('--personality', choices=build_rolepack.PERSONALITIES, default='playful')
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--comfy-url')
    parser.add_argument('--checkpoint')
    parser.add_argument('--prompt')
    parser.add_argument('--negative', default='')
    parser.add_argument('--steps', type=int, default=16)
    parser.add_argument('--cfg', type=float, default=6.0)
    parser.add_argument('--denoise', type=float, default=.55)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--remove-background', action='store_true')
    parser.add_argument('--background-model', type=Path)
    args = parser.parse_args(argv)
    try:
        report = create(**vars(args))
    except (ValueError, OSError):
        report = {'schema': 1, 'status': 'failed', 'stage': 'create_output', 'created': False,
                  'message': '无法建立新的制作目录；已有输出未被覆盖。'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('created') else 1


if __name__ == '__main__':
    raise SystemExit(main())
