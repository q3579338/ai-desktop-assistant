"""Recoverable, opt-in image creation; all durable results belong to one folder.

No service, model download, production role library, settings, or memory is
opened here. A resume_report starts a new folder and never resubmits generation.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from . import creation_tools, rolepacks
from .storage import atomic_json


MAX_REPORT = 512 * 1024
KINDS = {'user_image', 'local_img2img', 'local_background_removal', 'local_img2img_background_removal'}
TERMINAL = {'ready', 'failed', 'cancelled'}
PLAN_KEYS = {'image', 'generate', 'remove_background', 'url', 'checkpoint', 'prompt', 'negative',
             'steps', 'denoise', 'python_path', 'background_model', 'resume_report'}
REPORT_KEYS = {'schema', 'id', 'status', 'stage', 'folder', 'report', 'source_kind', 'completed_stages',
               'artifacts', 'image', 'sha256', 'available_image', 'available_sha256', 'generation',
               'background_removal', 'message', 'resumed_from', 'package', 'recovery_snapshot', 'interruption'}
REPORT_KEYS.add('input_original')


class CreationError(ValueError):
    """A creation result cannot be safely prepared, resumed, or packaged."""


def _path(value, exists=True):
    if not isinstance(value, (str, Path)) or not str(value):
        raise CreationError('制作文件路径不完整。')
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(value).startswith(('\\\\', '//'))
            or '://' in str(value)):
        raise CreationError('制作文件必须使用明确的绝对路径。')
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
            raise CreationError('制作文件必须位于本机，不能使用映射网络盘。')
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise CreationError('制作目录与文件不能使用链接或目录联接。')
    try:
        return path.resolve(strict=exists)
    except OSError:
        raise CreationError('制作文件或目录不存在。') from None


def _owned(value, folder):
    path = _path(value)
    if not path.is_relative_to(folder) or path == folder or not path.is_file():
        raise CreationError('制作结果必须属于本次独立目录。')
    return path


def _read(path, limit):
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise CreationError('制作文件为空、缺失或超过大小限制。')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CreationError('制作文件在读取时超过大小限制。')
    return data


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CreationError('制作报告含有重复字段。')
        result[key] = value
    return result


def _json(path):
    try:
        value = json.loads(_read(path, MAX_REPORT).decode('utf-8-sig'), object_pairs_hook=_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError, RecursionError):
        raise CreationError('制作报告不是有效的有界 JSON。') from None
    if not isinstance(value, dict):
        raise CreationError('制作报告格式不受支持。')
    return value


def _write(path, value):
    _path(path, exists=False)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_REPORT:
        raise CreationError('制作报告超过大小限制。')
    atomic_json(path, value)


def _png(path, folder, kind, stage):
    path = _owned(path, folder)
    if path.suffix.lower() != '.png':
        raise CreationError('制作结果必须是静态 PNG。')
    data = _read(path, rolepacks.MAX_IMAGE)
    try:
        width, height = rolepacks._image_info(path)
    except rolepacks.RolePackError as error:
        raise CreationError(str(error)) from None
    if _hash(_read(path, rolepacks.MAX_IMAGE)) != _hash(data):
        raise CreationError('图片在检查期间发生变化，请重新检查。')
    return {'path': str(path), 'sha256': _hash(data), 'bytes': len(data), 'width': width,
            'height': height, 'source_kind': kind, 'stage': stage}


def _tool_record(value, folder):
    path = _owned(value, folder)
    data = _json(path)
    status = data.get('status')
    if not isinstance(status, str) or not re.fullmatch('[a-z_]{1,40}', status):
        raise CreationError('本机工具报告缺少明确状态。')
    result = {'path': str(path), 'sha256': _hash(_read(path, MAX_REPORT)), 'status': status,
              'server_may_continue': data.get('server_may_continue') is True}
    identity = data.get('prompt_id') or data.get('requested_prompt_id')
    if isinstance(identity, str) and re.fullmatch('[A-Za-z0-9_-]{1,128}', identity):
        result['prompt_id'] = identity
    return result


def _publish_image(report, artifact):
    report['artifacts'].append(artifact)
    report.update(available_image=artifact['path'], available_sha256=artifact['sha256'],
                  source_kind=artifact['source_kind'])
    report['completed_stages'].append(artifact['stage'])


def _check_cancel(job):
    if job.cancelled.is_set():
        raise CreationError('本次制作已停止；已完成图片和任务报告仍保留。')


def _load_creation(report_path, allow_running=False):
    """Read a completed/failed report and revalidate every referenced artifact.

    Pending reports are never upgraded by guessing that a partially written
    output is complete. Tool job IDs remain evidence, never instructions to retry.
    """
    path = _path(report_path)
    folder = path.parent
    value = _json(path)
    if (path.name != 'creation.json' or set(value) - REPORT_KEYS or type(value.get('schema')) is not int
            or value['schema'] != 1 or value.get('status') not in (TERMINAL | {'running'} if allow_running else TERMINAL)
            or not isinstance(value.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', value['id'])
            or folder.name != 'creation-' + value['id'] or value.get('folder') != str(folder)
            or value.get('report') != str(path) or value.get('source_kind') not in KINDS):
        raise CreationError('制作报告未完成、版本不支持或目录归属不一致。')
    if (value.get('stage') not in ('validate', 'copy_source', 'generate', 'remove_background', 'complete')
            or not isinstance(value.get('message'), str) or len(value['message']) > 2000):
        raise CreationError('制作报告的阶段或说明无效。')
    if 'resumed_from' in value:
        origin = value['resumed_from']
        if (not isinstance(origin, dict) or set(origin) != {'report', 'sha256'}
                or not isinstance(origin.get('report'), str) or not Path(origin['report']).is_absolute()
                or not isinstance(origin.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', origin['sha256'])):
            raise CreationError('恢复来源记录不完整。')
    if 'interruption' in value or 'recovery_snapshot' in value:
        notice, snapshot = value.get('interruption'), value.get('recovery_snapshot')
        if (not isinstance(notice, dict) or set(notice) != {'stage', 'original_may_continue', 'submission_unknown'}
                or notice['stage'] not in ('validate', 'copy_source', 'generate', 'remove_background', 'complete')
                or notice['original_may_continue'] is not True or type(notice['submission_unknown']) is not bool
                or not isinstance(snapshot, dict) or set(snapshot) != {'path', 'sha256'}
                or _hash(_read(_owned(snapshot['path'], folder), MAX_REPORT)) != snapshot['sha256']):
            raise CreationError('中断恢复的快照或来源标记无效。')
    artifacts = value.get('artifacts')
    if 'input_original' in value:
        original=value['input_original']
        if (not isinstance(original,dict) or set(original)!={'path','sha256','format','first_frame_only'}
                or type(original['first_frame_only']) is not bool
                or _hash(_read(_owned(original['path'],folder),rolepacks.MAX_IMAGE))!=original['sha256']):
            raise CreationError('原始图片副本与制作记录不一致。')
    stages = value.get('completed_stages')
    if (not isinstance(artifacts, list) or len(artifacts) > 3 or not isinstance(stages, list)
            or len(stages) != len(artifacts)):
        raise CreationError('制作报告缺少完整的图片阶段记录。')
    for index, artifact in enumerate(artifacts):
        if (not isinstance(artifact, dict) or set(artifact) != {'path', 'sha256', 'bytes', 'width', 'height', 'source_kind', 'stage'}
                or artifact.get('source_kind') not in KINDS
                or artifact.get('stage') not in ('copy_source', 'generate', 'remove_background')
                or stages[index] != artifact['stage']
                or any(type(artifact.get(key)) is not int for key in ('bytes', 'width', 'height'))):
            raise CreationError('图片阶段记录无效。')
        actual = _png(artifact['path'], folder, artifact['source_kind'], artifact['stage'])
        if actual != artifact:
            raise CreationError('已完成图片与报告摘要不一致，不能恢复。')
    if artifacts:
        if stages[0] != 'copy_source' or len(set(stages)) != len(stages) or stages not in (
                ['copy_source'], ['copy_source', 'generate'], ['copy_source', 'remove_background'],
                ['copy_source', 'generate', 'remove_background']):
            raise CreationError('制作阶段顺序无效。')
        if 'resumed_from' not in value and artifacts[0]['source_kind'] != 'user_image':
            raise CreationError('原图来源不能冒充已完成的本机制作。')
        for index, artifact in enumerate(artifacts[1:], 1):
            prior = artifacts[index - 1]['source_kind']
            expected = ('local_img2img' if artifact['stage'] == 'generate' else
                        'local_img2img_background_removal' if prior in ('local_img2img', 'local_img2img_background_removal')
                        else 'local_background_removal')
            if artifact['source_kind'] != expected:
                raise CreationError('图片来源与实际完成阶段不一致。')
    if artifacts:
        final = artifacts[-1]
        if (value.get('available_image') != final['path'] or value.get('available_sha256') != final['sha256']
                or value['source_kind'] != final['source_kind']):
            raise CreationError('可恢复图片与完成记录不一致。')
    elif value.get('available_image') or value.get('available_sha256'):
        raise CreationError('尚未完成的文件不能作为可恢复图片。')
    if value['status'] == 'ready':
        if not artifacts or value.get('image') != final['path'] or value.get('sha256') != final['sha256']:
            raise CreationError('制作完成报告缺少已校验的最终图片。')
    elif value.get('image') or value.get('sha256'):
        raise CreationError('未完成的制作不能标记为最终图片。')
    for key in ('generation', 'background_removal'):
        if key not in value:
            continue
        record = value[key]
        if not isinstance(record, dict):
            raise CreationError('工具报告引用无效。')
        if 'path' in record:
            actual = _tool_record(record['path'], folder)
            if {k: v for k, v in record.items() if k != 'inherited'} != actual:
                raise CreationError('本机工具报告与保存的摘要不一致。')
            if 'inherited' in record and record['inherited'] is not True:
                raise CreationError('工具报告来源标记无效。')
        elif (set(record) - {'status', 'server_may_continue', 'prompt_id'}
                or record.get('status') != 'unavailable' or type(record.get('server_may_continue')) is not bool
                or (key == 'generation' and not record['server_may_continue'])):
            raise CreationError('缺失的工具报告不能标记为已完成。')
        if 'prompt_id' in record and (not isinstance(record['prompt_id'], str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}', record['prompt_id'])):
            raise CreationError('本机任务编号无效。')
    for index, artifact in enumerate(artifacts[1:], 1):
        key = 'generation' if artifact['stage'] == 'generate' else 'background_removal'
        record = value.get(key, {})
        if record.get('status') != 'completed' or 'path' not in record or record.get('inherited'):
            raise CreationError('图片阶段缺少本次已完成的工具记录。')
        tool = _json(_owned(record['path'], folder))
        if (tool.get('image') != artifact['path'] or tool.get('output_sha256') != artifact['sha256']
                or tool.get('input_sha256') != artifacts[index - 1]['sha256']):
            raise CreationError('图片与本次工具报告的输入输出不一致。')
    for marker, key in (('local_img2img', 'generation'), ('background_removal', 'background_removal')):
        if marker in value['source_kind'] and value.get(key, {}).get('status') != 'completed':
            raise CreationError('图片来源缺少已完成的工具记录。')
    if 'package' in value:
        package = value['package']
        if (not isinstance(package, dict) or set(package) != {'path', 'sha256', 'import_verified'}
                or package['import_verified'] is not True
                or _hash(_read(_owned(package['path'], folder), rolepacks.MAX_TOTAL)) != package['sha256']):
            raise CreationError('角色包与制作报告不一致。')
    return copy.deepcopy(value)


def load_creation(report_path):
    """Load a terminal report or fork a validated running snapshot for recovery.

    Recovery never changes the source report or stops its possibly live worker.
    Unrecorded output files are not inspected, accepted, or automatically retried.
    """
    try:
        value = _load_creation(report_path, allow_running=True)
        return recover_creation(report_path) if value['status'] == 'running' else value
    except CreationError:
        raise
    except (OSError, TypeError, KeyError, AttributeError, OverflowError):
        raise CreationError('制作报告或关联文件无效，请保留目录并重新核对。') from None


def _recover_creation(report_path):
    """Fork only recorded, fully verified artifacts into a new cancelled result.

    This explicit read action is safe even if the old worker is still active:
    old files are read-only, the new UUID owns all candidates/tool reports, and
    its message leaves original server status unknown. No tool is invoked.
    """
    path = _path(report_path)
    original_bytes = _read(path, MAX_REPORT)
    previous = _load_creation(path, allow_running=True)
    if previous['status'] != 'running':
        return previous
    if not previous['artifacts'] or not previous.get('available_image'):
        raise CreationError('中断报告尚未记录任何已完成图片，不能猜测未完成文件；请保留原制作目录。')
    if _read(path, MAX_REPORT) != original_bytes:
        raise CreationError('原制作报告正在更新，请稍后重新读取；没有修改原任务。')
    previous_folder = Path(previous['folder'])
    artifact = previous['artifacts'][-1]
    image_bytes = _read(_owned(artifact['path'], previous_folder), rolepacks.MAX_IMAGE)
    if _hash(image_bytes) != artifact['sha256']:
        raise CreationError('原制作图片发生变化，未恢复不确定结果。')
    tool_data = {}
    for key in ('generation', 'background_removal'):
        if key not in previous:
            continue
        record = previous[key]
        if 'path' in record:
            owned = _owned(record['path'], previous_folder)
            raw = _read(owned, MAX_REPORT)
            if _hash(raw) != record['sha256']:
                raise CreationError('原工具报告正在更新，请稍后重新读取。')
            tool_data[key] = _json(owned)
            if _read(owned, MAX_REPORT) != raw:
                raise CreationError('原工具报告正在更新，请稍后重新读取。')
        else:
            tool_data[key] = None
    if _read(path, MAX_REPORT) != original_bytes:
        raise CreationError('原制作报告正在更新，请稍后重新读取；没有修改原任务。')
    identity = uuid.uuid4().hex
    folder = previous_folder.parent / ('creation-' + identity)
    _path(folder, exists=False)
    folder.mkdir()
    report = {'schema': 1, 'id': identity, 'folder': str(folder), 'report': str(folder / 'creation.json'),
        'status': 'cancelled', 'stage': previous['stage'], 'source_kind': previous['source_kind'],
        'completed_stages': [], 'artifacts': [],
        'resumed_from': {'report': str(path), 'sha256': _hash(original_bytes)},
        'interruption': {'stage': previous['stage'], 'original_may_continue': True,
            'submission_unknown': previous['stage'] == 'generate' and 'generate' not in previous['completed_stages']},
        'message': '已另存报告明确记录的完成图片。原制作可能仍在运行；未重发或中断原任务，可预览此候选后继续。'}
    copied = folder / 'recovered.png'
    with copied.open('xb') as output:
        output.write(image_bytes)
    recovered = _png(copied, folder, previous['source_kind'], 'copy_source')
    if recovered['sha256'] != artifact['sha256']:
        raise CreationError('恢复图片副本与原记录不一致。')
    _publish_image(report, recovered)
    for key, data in tool_data.items():
        if data is None:
            report[key] = copy.deepcopy(previous[key])
        else:
            target = folder / ('previous-' + key + '.json')
            _write(target, data)
            report[key] = {**_tool_record(target, folder), 'inherited': True}
    if report['interruption']['submission_unknown'] and 'generation' not in report:
        report['generation'] = {'status': 'unavailable', 'server_may_continue': True}
    snapshot = folder / 'interrupted-snapshot.json'
    # Preserve the already-read JSON content; atomic_json handles this snapshot
    # as data only, never following its old artifact paths.
    _write(snapshot, json.loads(original_bytes.decode('utf-8-sig'), object_pairs_hook=_object))
    report['recovery_snapshot'] = {'path': str(snapshot), 'sha256': _hash(_read(snapshot, MAX_REPORT))}
    _write(Path(report['report']), report)
    return _load_creation(report['report'])


def recover_creation(report_path):
    """Recover a recorded running snapshot; invalid inputs raise CreationError."""
    try:
        return _recover_creation(report_path)
    except CreationError:
        raise
    except (OSError, TypeError, KeyError, AttributeError, OverflowError):
        raise CreationError('中断制作无法恢复，原报告与图片保持不变，请检查文件或可用空间。') from None


def prepare_creation(plan, base, job, progress=None):
    """Make a new isolated result; optional resume_report never requeues a job.

    progress receives a Chinese status string. Return a terminal report even on
    cancellation; invalid paths before a folder can be created raise CreationError.
    """
    base = _path(base, exists=False)
    base.mkdir(parents=True, exist_ok=True)
    base = _path(base)
    if not base.is_dir():
        raise CreationError('制作输出位置必须是文件夹。')
    identity = uuid.uuid4().hex
    folder = base / ('creation-' + identity)
    folder.mkdir()
    report = {'schema': 1, 'id': identity, 'folder': str(folder), 'report': str(folder / 'creation.json'),
              'status': 'running', 'stage': 'validate', 'source_kind': 'user_image',
              'completed_stages': [], 'artifacts': [], 'message': '正在检查图片。'}

    def stage(name, text):
        report.update(stage=name, message=text)
        _write(Path(report['report']), report)
        if progress is not None:
            progress(text)
        _check_cancel(job)

    try:
        stage('validate', '正在检查图片与制作选项。')
        if not isinstance(plan, dict) or set(plan) - PLAN_KEYS:
            raise CreationError('制作选项包含不支持的字段。')
        for key in ('generate', 'remove_background'):
            if type(plan.get(key, False)) is not bool:
                raise CreationError('重绘与去背景必须明确开启或关闭。')
        if plan.get('resume_report'):
            previous = load_creation(plan['resume_report'])
            if plan.get('generate'):
                raise CreationError('恢复已有制作不能再次提交重绘；请先核对原任务状态。')
            selected = previous.get('available_image')
            if not selected:
                raise CreationError('这份报告没有已完成的可恢复图片。')
            if plan.get('image') and _path(plan['image']) != _path(selected):
                raise CreationError('恢复时只能选择报告已校验的图片。')
            report['source_kind'] = previous['source_kind']
            report['resumed_from'] = {'report': previous['report'], 'sha256': _hash(_read(_path(previous['report']), MAX_REPORT))}
            for key in ('generation', 'background_removal'):
                if key in previous:
                    record = copy.deepcopy(previous[key])
                    if 'path' in record:
                        copied = folder / ('previous-' + key + '.json')
                        _write(copied, _json(_owned(record['path'], Path(previous['folder']))))
                        record = {**_tool_record(copied, folder), 'inherited': True}
                    report[key] = record
        else:
            selected = plan.get('image')
        source = _path(selected)
        original = _read(source, rolepacks.MAX_IMAGE)
        stage('copy_source', '正在保存本次制作的图片副本。')
        copied = folder / 'input.png'
        if source.suffix.lower()=='.png':
            with copied.open('xb') as output:output.write(original)
        else:
            from .image_input import decode_image
            image,info=decode_image(original,source.suffix)
            preserved=folder/('original'+source.suffix.lower())
            with preserved.open('xb') as output:output.write(original)
            report['input_original']={'path':str(preserved),'sha256':_hash(original),**info}
            if not image.save(str(copied),'PNG'):raise CreationError('标准图片副本未能保存。')
        artifact = _png(copied, folder, report['source_kind'], 'copy_source')
        if source.suffix.lower()=='.png' and artifact['sha256'] != _hash(original):
            raise CreationError('图片副本与输入摘要不一致。')
        _publish_image(report, artifact)
        _write(Path(report['report']), report)
        selected = copied
        if plan.get('generate'):
            stage('generate', '正在本机重绘；停止等待不会删除服务中的任务。')
            options = {key: plan[key] for key in ('url', 'checkpoint', 'prompt', 'negative') if key in plan}
            options.update(steps=plan.get('steps', 16), denoise=plan.get('denoise', .45))
            result = creation_tools.generate_image(selected, folder / 'generation', options, job)
            report['generation'] = _tool_record(result.get('report'), folder)
            if result.get('status') != 'completed' or report['generation']['status'] != 'completed':
                raise CreationError('本机重绘未明确完成，保留原任务报告。')
            artifact = _png(result.get('image'), folder, 'local_img2img', 'generate')
            if (result.get('input_sha256') != report['available_sha256']
                    or result.get('output_sha256') != artifact['sha256']
                    or not Path(artifact['path']).is_relative_to(folder / 'generation')):
                raise CreationError('本机重绘的输入或输出摘要不一致。')
            recorded = _json(_owned(report['generation']['path'], folder))
            if any(recorded.get(key) != result.get(key) for key in ('status', 'image', 'input_sha256', 'output_sha256')):
                raise CreationError('本机重绘图片与任务报告不一致。')
            _publish_image(report, artifact)
            selected = Path(artifact['path'])
            _write(Path(report['report']), report)
            _check_cancel(job)
        if plan.get('remove_background'):
            stage('remove_background', '正在本机去背景，完成后仍需检查边缘。')
            result = creation_tools.remove_background(selected, folder / 'cutout.png',
                plan.get('python_path', ''), plan.get('background_model', ''), job)
            report['background_removal'] = _tool_record(result.get('report'), folder)
            kind = ('local_img2img_background_removal' if report['source_kind'] in
                    ('local_img2img', 'local_img2img_background_removal') else 'local_background_removal')
            artifact = _png(result.get('image'), folder, kind, 'remove_background')
            if (result.get('status') != 'completed' or report['background_removal']['status'] != 'completed'
                    or result.get('model_verified') is not True or result.get('rgb_preserved') is not True
                    or result.get('input_sha256') != report['available_sha256']
                    or result.get('output_sha256') != artifact['sha256']
                    or Path(artifact['path']) != folder / 'cutout.png'):
                raise CreationError('去背景结果未通过完成状态、来源和摘要检查。')
            low, high = result.get('alpha_min'), result.get('alpha_max')
            if type(low) is not int or type(high) is not int or not 0 <= low < high <= 255:
                raise CreationError('去背景未得到可辨认的透明区域与前景，请查看图片。')
            recorded = _json(_owned(report['background_removal']['path'], folder))
            if any(recorded.get(key) != result.get(key) for key in ('status', 'image', 'input_sha256', 'output_sha256')):
                raise CreationError('去背景图片与任务报告不一致。')
            _publish_image(report, artifact)
            _write(Path(report['report']), report)
            _check_cancel(job)
        _check_cancel(job)
        report.update(status='ready', stage='complete', image=report['available_image'],
                      sha256=report['available_sha256'], message='图片已准备好，请预览后创建角色。')
        _write(Path(report['report']), report)
        return load_creation(report['report'])
    except Exception as error:
        stage_name = report['stage']
        if stage_name in ('generate', 'remove_background') and isinstance(error, creation_tools.CreationToolError):
            key = 'generation' if stage_name == 'generate' else 'background_removal'
            record = {'status': 'unavailable', 'server_may_continue': stage_name == 'generate'}
            if getattr(error, 'report', None):
                try:
                    record = _tool_record(error.report, folder)
                except (OSError, ValueError, TypeError):
                    pass
            identity = getattr(error, 'prompt_id', None)
            if 'path' not in record and isinstance(identity, str) and re.fullmatch('[A-Za-z0-9_-]{1,128}', identity):
                record['prompt_id'] = identity
            report[key] = record
        report.pop('image', None)
        report.pop('sha256', None)
        if isinstance(error, CreationError):
            message = str(error)
        elif isinstance(error, creation_tools.CreationToolError):
            message = str(error)[:1500] + ' 已完成图片和本次任务报告仍保留在制作目录。'
        else:
            message = '本次制作未完成；已完成图片和任务报告保留在制作目录。'
        report.update(status='cancelled' if job.cancelled.is_set() else 'failed',
                      message=message)
        try:
            _write(Path(report['report']), report)
        except (OSError, ValueError, TypeError):
            report['message'] += ' 制作报告也未能更新，请保留此目录并检查磁盘空间与权限。'
        return copy.deepcopy(report)


def build_creation_package(report, name, persona, personality):
    """Export and re-import in isolated libraries, never into a production role.

    Failed/cancelled reports may package their verified available_image when
    explicitly requested. An in-memory edited report cannot override disk data.
    """
    if not isinstance(report, dict) or 'report' not in report:
        raise CreationError('请先准备图片或打开已校验的制作报告。')
    checked = load_creation(report['report'])
    image = checked.get('available_image')
    if not image:
        raise CreationError('没有已完成的图片可创建角色。')
    folder = Path(checked['folder'])
    # Keep nested role UUID paths off a potentially deep AppData/test folder:
    # Windows may still enforce MAX_PATH for role.json's atomic temporary file.
    temporary_base = _path(Path(tempfile.gettempdir()))
    staging = temporary_base / ('ada-' + uuid.uuid4().hex[:12])
    staging.mkdir()
    destination = folder / ('assistant-' + uuid.uuid4().hex + '.aipet')
    delivered_hash = None
    try:
        library = rolepacks.RoleLibrary(staging / 'b')
        identity = library.import_image(image, name=name, persona=persona)
        metadata = library.root / identity / 'role.json'
        manifest = _json(metadata)
        manifest['source']['kind'] = checked['source_kind']
        _write(metadata, manifest)
        package = staging / 'candidate.aipet'
        library.export_package(identity, package, profile={'personality': personality, 'formMode': 'cat'})
        verification = rolepacks.RoleLibrary(staging / 'v')
        imported = verification.import_package(package)
        actual = _json(verification.root / imported / 'role.json')
        expected = {'kind': checked['source_kind'], 'filename': Path(image).name, 'sha256': checked['available_sha256']}
        if (actual.get('source') != expected or verification.profile(imported)['personality'] != personality
                or _hash(_read(verification.spec(imported)['image'], rolepacks.MAX_IMAGE)) != checked['available_sha256']):
            raise CreationError('角色包重新导入后的图片、来源或风格不一致。')
        data = _read(package, rolepacks.MAX_TOTAL)
        with destination.open('xb') as output:
            output.write(data)
        delivered_hash = _hash(data)
        if _hash(_read(destination, rolepacks.MAX_TOTAL)) != delivered_hash:
            raise CreationError('交付角色包与已验证的数据不一致。')
        checked['package'] = {'path': str(destination), 'sha256': delivered_hash, 'import_verified': True}
        _write(Path(checked['report']), checked)
        return str(destination)
    except Exception:
        if delivered_hash is not None and destination.is_file() and not destination.is_symlink():
            if _hash(_read(destination, rolepacks.MAX_TOTAL)) == delivered_hash:
                destination.unlink()
        raise
    finally:
        resolved = _path(staging)
        if resolved.parent != temporary_base or resolved != staging or not re.fullmatch('ada-[a-f0-9]{12}', resolved.name):
            raise CreationError('临时角色目录归属发生变化，停止自动清理。')
        shutil.rmtree(resolved)
