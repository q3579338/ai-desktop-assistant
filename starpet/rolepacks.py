"""Validated, data-only character packs; artwork bytes are never transformed.

A pack contains a root role.json, a static PNG fallback, and optionally GIF or
WebP clips. Import validates the complete pack before installing a new identity.
No chat settings, history, memory, code, or credentials enter an exported pack.
"""
import copy
import hashlib
import json
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

from .characters import character_spec, STATES


MAX_TOTAL = 128 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_FILES = 32
MAX_DIMENSION = 4096
MAX_PIXELS = 16 * 1024 * 1024
MAX_FRAMES = 600
MAX_ANIMATION_PIXELS = 256 * 1024 * 1024
CLIP_KEYS = frozenset(STATES + ('head_pat', 'poke', 'tail', 'drag_start', 'drag_end', 'wave'))
SOURCE_LIMITS = {'kind': 160, 'filename': 240, 'sha256': 64, 'license': 1000,
                 'attribution': 1000, 'promptSummary': 4000}
BUILTINS = {
    'tabby': {'name': '团团', 'description': '可爱橘猫，陪你慢慢完成工作。',
              'persona': '你是团团，一只机灵温暖的原创橘猫助手。认真陪用户工作，也会自然地聊日常。',
              'personality': 'playful', 'formMode': 'chat'},
    'catgirl': {'name': '橘宁', 'description': '成年猫娘，温柔而有自己的判断。',
                'persona': '你是橘宁，一位二十多岁的成年猫娘助手。温柔、独立、有自己的判断，认真陪用户工作和交流日常。',
                'personality': 'gentle', 'formMode': 'catgirl'},
    'robot': {'name': '小序', 'description': '办公机器人，清楚可靠地陪你做事。',
              'persona': '你是小序，一位原创办公机器人助手。说话清楚简洁，有一点温和幽默，认真整理思路，不假装执行没有完成的操作。',
              'personality': 'concise', 'formMode': 'cat'},
}


class RolePackError(ValueError):
    """A pack is invalid or cannot be safely imported/exported."""


def _fail(message):
    raise RolePackError(message)


def _text(value, field, limit, empty=True):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        _fail(f'角色的 {field} 必须是最多 {limit} 字的文字。')
    value = value.strip()
    if not value and not empty:
        _fail(f'角色的 {field} 不能为空。')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        _fail('资源路径无效。')
    if '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        _fail('资源只允许安全的相对路径。')
    parts = value.split('/')
    if len(parts) > 4 or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts):
        _fail('资源路径不能跨出角色目录，也不能嵌套超过四层。')
    for part in parts:
        if any(c in part for c in '<>"|?*') or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])', part.split('.')[0], re.I):
            _fail('资源路径包含 Windows 不支持的名称。')
    if PurePosixPath(value).is_absolute():
        _fail('资源不能使用绝对路径。')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('角色清单包含重复字段。')
        result[key] = value
    return result


def _manifest(raw):
    if len(raw) > MAX_MANIFEST:
        _fail('角色清单超过 64 KiB。')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object)
    except RolePackError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail('role.json 必须是有效的 UTF-8 JSON。')
    allowed = {'schema', 'name', 'description', 'persona', 'personality', 'formMode',
               'image', 'atlas', 'clips', 'source', 'human'}
    if not isinstance(value, dict) or set(value) - allowed:
        _fail('角色清单包含不支持的字段；不能携带设置、记忆或凭据。')
    if type(value.get('schema')) is not int or value['schema'] != 1:
        _fail('只支持 schema 为 1 的角色包。')
    result = {'schema': 1, 'name': _text(value.get('name'), '名字', 40, False),
              'description': _text(value.get('description', ''), '简介', 500),
              'persona': _text(value.get('persona', ''), '角色设定', 2000),
              'personality': value.get('personality', 'playful'),
              'formMode': value.get('formMode', 'cat'), 'image': _relative(value.get('image'))}
    if result['personality'] not in ('gentle', 'playful', 'concise') or result['formMode'] not in ('cat', 'catgirl', 'chat'):
        _fail('角色的说话风格或形态设置无效。')
    if PurePosixPath(result['image']).suffix.lower() != '.png':
        _fail('角色必须带一张静态 PNG 作为回退图片。')
    if 'atlas' in value:
        atlas = value['atlas']
        if not isinstance(atlas, dict) or set(atlas) != {'columns', 'rows', 'states'}:
            _fail('图集必须包含 columns、rows 和 states。')
        columns, rows, states = (atlas[k] for k in ('columns', 'rows', 'states'))
        if any(type(n) is not int or not 1 <= n <= 8 for n in (columns, rows)):
            _fail('图集的行列数必须在 1～8 之间。')
        if not isinstance(states, dict) or set(states) != set(STATES) or any(type(n) is not int or not 0 <= n < rows * columns for n in states.values()):
            _fail('图集必须提供六个有效的状态索引。')
        result['atlas'] = copy.deepcopy(atlas)
    if 'clips' in value:
        clips = value['clips']
        if not isinstance(clips, dict) or set(clips) - CLIP_KEYS:
            _fail('动画包含不支持的状态名称。')
        result['clips'] = {}
        for key, clip in clips.items():
            if not isinstance(clip, dict) or set(clip) != {'file', 'loop'} or type(clip['loop']) is not bool:
                _fail('每段动画必须指定 file 和布尔值 loop。')
            filename = _relative(clip['file'])
            if PurePosixPath(filename).suffix.lower() not in ('.gif', '.webp'):
                _fail('动画只支持 GIF 或 WebP。')
            result['clips'][key] = {'file': filename, 'loop': clip['loop']}
    if 'source' in value:
        source = value['source']
        if not isinstance(source, dict) or set(source) - set(SOURCE_LIMITS):
            _fail('来源信息只能包含规定的文字字段。')
        result['source'] = {key: _text(v, '来源 '+key, SOURCE_LIMITS[key]) for key, v in source.items()}
        sha = result['source'].get('sha256', '')
        if sha and not re.fullmatch('[0-9a-fA-F]{64}', sha):
            _fail('来源中的 SHA-256 格式不正确。')
        filename = result['source'].get('filename', '')
        if filename and ('/' in filename or '\\' in filename or ':' in filename):
            _fail('来源文件名不能包含本机目录。')
    if 'human' in value:
        human=value['human']
        if not isinstance(human,dict) or set(human)-{'image','atlas','clips','source'}:
            _fail('人物形态只允许图片、图集、动作和来源，不能包含其它角色或记忆。')
        checked=_manifest(_json_bytes({'schema':1,'name':result['name'],**human}))
        result['human']={key:checked[key] for key in ('image','atlas','clips','source') if key in checked}
        # The two bodies must use distinct files, so changing one cannot silently
        # replace the other. They remain one role identity and one memory store.
        base_refs={result['image']}|{c['file'] for c in result.get('clips',{}).values()}
        if base_refs & _references(result['human']):
            _fail('猫与人物形态必须使用各自独立的资源文件。')
    return result


def _references(manifest):
    result={manifest['image']} | {clip['file'] for clip in manifest.get('clips', {}).values()}
    if 'human' in manifest:result|=_references(manifest['human'])
    return result


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _bounded_bytes(path, limit):
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        _fail('资源在读取时超过大小上限。')
    return data


def _within(folder, relative):
    path = folder.joinpath(*_relative(relative).split('/'))
    current = path
    while current != folder:
        if _linked(current):
            _fail('角色资源不能是符号链接或目录联接。')
        current = current.parent
    if not path.resolve().is_relative_to(folder.resolve()):
        _fail('角色资源跨出了目录。')
    return path


def _image_info(path, animation=False):
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_IMAGE:
        _fail('每个图片文件必须存在、非空且不超过 32 MiB。')
    # Decode a bounded in-memory copy: QImageReader otherwise keeps a Windows
    # file handle alive through an exception traceback, preventing rollback.
    buffer = QBuffer()
    buffer.setData(QByteArray(_bounded_bytes(path, MAX_IMAGE)))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(False)
    if not reader.canRead():
        _fail('图片无法读取，可能已损坏或不是图片。')
    actual = bytes(reader.format()).decode('ascii', 'ignore').lower()
    expected = path.suffix.lower().lstrip('.')
    if actual != expected or actual not in (('gif', 'webp') if animation else ('png',)):
        _fail('图片实际格式与文件扩展名不符。')
    size = reader.size()
    width, height = size.width(), size.height()
    if not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or width * height > MAX_PIXELS:
        _fail('图片单边最多 4096 像素，总像素最多 16M。')
    count = reader.imageCount()
    if count > MAX_FRAMES or (not animation and count > 1):
        _fail('动画最多 600 帧，回退 PNG 必须是静态图片。')
    frames = 0
    decoded_pixels = 0
    while reader.canRead():
        frames += 1
        if frames > MAX_FRAMES or (not animation and frames > 1):
            _fail('动画最多 600 帧，回退 PNG 必须是静态图片。')
        decoded_pixels += width * height
        if decoded_pixels > MAX_ANIMATION_PIXELS:
            _fail('动画解码量过大，请缩小尺寸或减少帧数。')
        image = reader.read()
        if image.isNull() or image.width() > MAX_DIMENSION or image.height() > MAX_DIMENSION:
            _fail('图片帧无法完整读取。')
        if not animation:
            break
    if frames < 1 or (count > 0 and frames != count):
        _fail('动画帧不完整。')
    return width, height


def _validate_files(folder, manifest):
    references = _references(manifest)
    names = ['role.json'] + sorted(references)
    if len(names) > MAX_FILES or len({n.casefold() for n in names}) != len(names):
        _fail('资源过多或包含大小写重复文件名。')
    total = len(_json_bytes(manifest))
    for name in references:
        path = _within(folder, name)
        total += path.stat().st_size if path.is_file() else MAX_TOTAL + 1
        if total > MAX_TOTAL:
            _fail('角色资源总量不能超过 128 MiB。')
        forms=[manifest]+([manifest['human']] if 'human' in manifest else [])
        still=next((form for form in forms if name==form['image']),None)
        size = _image_info(path, still is None)
        if still and 'atlas' in still:
            if size[0] < still['atlas']['columns'] or size[1] < still['atlas']['rows']:
                _fail('图集尺寸小于行列数。')


def _json_bytes(manifest):
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


class RoleLibrary:
    def __init__(self, state, assets=None):
        self.state = Path(state).resolve()
        self.root = self.state / 'roles'
        self.assets = Path(assets) if assets else Path(__file__).resolve().parents[1] / 'assets'

    def _root(self):
        if _linked(self.root) or not self.root.resolve().is_relative_to(self.state):
            _fail('角色目录不能指向其他位置。')
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _custom(self, role_id, validate=False):
        if not isinstance(role_id, str) or not re.fullmatch(r'custom-[0-9a-f]{32}', role_id):
            _fail('角色不存在。')
        folder = self.root / role_id
        if _linked(self.root) or _linked(folder) or not folder.is_dir():
            _fail('角色不存在或目录无效。')
        metadata = _within(folder, 'role.json')
        if not metadata.is_file() or metadata.stat().st_size > MAX_MANIFEST:
            _fail('角色清单不存在或过大。')
        manifest = _manifest(_bounded_bytes(metadata, MAX_MANIFEST))
        for name in _references(manifest):
            if not _within(folder, name).is_file():
                _fail('角色资源缺失。')
        if validate:
            _validate_files(folder, manifest)
        return folder, manifest

    def profile(self, role_id):
        if role_id in BUILTINS:
            return {'id': role_id, 'builtin': True, **copy.deepcopy(BUILTINS[role_id])}
        if role_id == 'whale':
            return {'id': role_id, 'builtin': True, 'name': '小鲸', 'description': '经典小鲸形象',
                    'persona': '你是一位温暖可靠的小鲸助手。', 'personality': 'gentle', 'formMode': 'cat'}
        _, manifest = self._custom(role_id)
        return {'id': role_id, 'builtin': False, **{k: manifest[k] for k in
                ('name', 'description', 'persona', 'personality', 'formMode')}}

    def list_roles(self):
        result = [{**self.profile(role_id), 'kind': 'atlas'} for role_id in BUILTINS]
        if self.root.is_dir() and not _linked(self.root):
            for folder in sorted(self.root.iterdir()):
                if folder.name.startswith('custom-'):
                    try:
                        _, manifest = self._custom(folder.name)
                        result.append({**self.profile(folder.name), 'kind': 'atlas' if 'atlas' in manifest else 'image'})
                    except (OSError, RolePackError):
                        continue
        return result

    def spec(self, role_id):
        if role_id == 'whale':
            return None
        if role_id in BUILTINS:
            spec = character_spec(role_id, self.assets)
            return {'kind': 'atlas', **spec} if spec else None
        folder, manifest = self._custom(role_id, validate=True)
        result = {'kind': 'atlas' if 'atlas' in manifest else 'image',
                  'name': manifest['name'], 'image': _within(folder, manifest['image'])}
        if 'atlas' in manifest:
            result.update(copy.deepcopy(manifest['atlas']))
        if manifest.get('clips'):
            result['clips'] = {k: {'file': _within(folder, v['file']), 'loop': v['loop']}
                               for k, v in manifest['clips'].items()}
        if 'human' in manifest:
            human=manifest['human']
            form={'kind':'atlas' if 'atlas' in human else 'image','image':_within(folder,human['image'])}
            if 'atlas' in human:form.update(copy.deepcopy(human['atlas']))
            if human.get('clips'):
                form['clips']={k:{'file':_within(folder,v['file']),'loop':v['loop']} for k,v in human['clips'].items()}
            result['human']=form
        return result

    def import_pair(self,cat_image,human_image,name,persona=''):
        """Create one identity from two raster images, without user-state access."""
        from .image_input import load_image
        if Path(cat_image).resolve()==Path(human_image).resolve():
            _fail('请选择两张不同的图片，分别用于猫与对应人物形态。')
        staging=self._root()/('.import-'+uuid.uuid4().hex);staging.mkdir()
        try:
            sources={}
            for key,path in (('cat',cat_image),('human',human_image)):
                data,image,info=load_image(path)
                target=staging/(key+'.png')
                if not image.save(str(target),'PNG'):_fail('配对图片未能保存。')
                sources[key]={'kind':'user_image','filename':Path(path).name,
                    'sha256':hashlib.sha256(data).hexdigest(),
                    'attribution':'取第一帧制作形态' if info['first_frame_only'] else '原图归一化为PNG'}
            manifest=_manifest(_json_bytes({'schema':1,'name':name,'persona':persona,
                'description':'一只猫与其对应的成年人物，共用人设和记忆。','formMode':'chat',
                'image':'cat.png','source':sources['cat'],
                'human':{'image':'human.png','source':sources['human']}}))
            return self._install(staging,manifest)
        finally:
            if staging.exists():shutil.rmtree(staging)

    def _install(self, staging, manifest):
        _validate_files(staging, manifest)
        (staging / 'role.json').write_bytes(_json_bytes(manifest))
        role_id = 'custom-' + uuid.uuid4().hex
        target = self.root / role_id
        if target.exists():
            _fail('角色目录发生冲突，请重试。')
        staging.rename(target)
        return role_id

    def import_image(self, path, name, persona=''):
        path = Path(path)
        _image_info(path)
        manifest = _manifest(_json_bytes({'schema': 1, 'name': name, 'persona': persona,
            'image': 'image.png', 'source': {'kind': 'user_image', 'filename': path.name,
            'sha256': hashlib.sha256(_bounded_bytes(path, MAX_IMAGE)).hexdigest()}}))
        staging = self._root() / ('.import-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            original = _bounded_bytes(path, MAX_IMAGE)
            if hashlib.sha256(original).hexdigest() != manifest['source']['sha256']:
                _fail('原图在导入时发生变化，请重试。')
            (staging / 'image.png').write_bytes(original)
            return self._install(staging, manifest)
        except (OSError, zipfile.BadZipFile) as error:
            raise RolePackError('角色未能保存，请检查可用空间和文件权限。') from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def import_package(self, zip_path):
        path = Path(zip_path)
        if not path.is_file() or path.stat().st_size > MAX_TOTAL + 1024 * 1024:
            _fail('角色包不存在或超过大小上限。')
        staging = None
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if not 1 <= len(infos) <= MAX_FILES:
                    _fail('角色包最多允许 32 个条目。')
                seen, prefixes, files, total = set(), {}, {}, 0
                for info in infos:
                    name = _relative(info.filename[:-1] if info.is_dir() else info.filename)
                    key = name.casefold()
                    if key in seen:
                        _fail('角色包包含重复或大小写冲突的名称。')
                    seen.add(key)
                    parts = name.split('/')
                    for length in range(1, len(parts) + 1):
                        prefix = '/'.join(parts[:length])
                        existing = prefixes.setdefault(prefix.casefold(), prefix)
                        if existing != prefix:
                            _fail('角色包目录名存在大小写冲突。')
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        _fail('角色包不能包含链接或特殊文件。')
                    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        _fail('角色包不能加密或使用不支持的压缩方式。')
                    if info.is_dir():
                        continue
                    limit = MAX_MANIFEST if name == 'role.json' else MAX_IMAGE
                    total += info.file_size
                    if info.file_size <= 0 or info.file_size > limit or total > MAX_TOTAL:
                        _fail('角色包文件大小超过上限。')
                    if info.file_size > 1024 * 1024 and info.file_size / max(1, info.compress_size) > 200:
                        _fail('角色包压缩比异常。')
                    files[name] = info
                if 'role.json' not in files:
                    _fail('角色包根目录必须包含 role.json。')
                manifest = _manifest(archive.read(files['role.json']))
                if set(files) != {'role.json'} | _references(manifest):
                    _fail('角色包只能包含 role.json 和清单引用的图片。')
                staging = self._root() / ('.import-' + uuid.uuid4().hex)
                staging.mkdir()
                for name in _references(manifest):
                    target = _within(staging, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(files[name]) as source, target.open('xb') as destination:
                        written = 0
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            written += len(block)
                            if written > files[name].file_size or written > MAX_IMAGE:
                                _fail('角色包展开大小与清单不符。')
                            destination.write(block)
                return self._install(staging, manifest)
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            raise RolePackError('角色包无法完整读取或保存。') from error
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging)

    def export_package(self, role_id, destination, profile=None):
        if role_id in BUILTINS:
            spec = self.spec(role_id)
            if not spec:
                _fail('该内置角色的图片尚未准备好。')
            folder = spec['image'].parent
            metadata_path = folder / 'character.json'
            original = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
            builtin_profile = self.profile(role_id)
            manifest = {k: builtin_profile[k] for k in ('name', 'description', 'persona', 'personality', 'formMode')}
            manifest.update(schema=1, image=spec['image'].name, atlas={k: spec[k] for k in ('columns', 'rows', 'states')},
                source={'kind': str(original.get('source', 'built-in original artwork'))[:160],
                        'filename': spec['image'].name, 'sha256': hashlib.sha256(_bounded_bytes(spec['image'], MAX_IMAGE)).hexdigest(),
                        'promptSummary': str(original.get('generation_prompt', ''))[:4000]})
            if spec.get('clips'):
                manifest['clips']={key:{'file':clip['file'].relative_to(folder).as_posix(),'loop':clip['loop']}
                                   for key,clip in spec['clips'].items()}
            manifest = _manifest(_json_bytes(manifest))
            _validate_files(folder, manifest)
        else:
            folder, manifest = self._custom(role_id, validate=True)
        if profile is not None:
            if not isinstance(profile, dict) or set(profile) - {'name', 'persona', 'personality', 'formMode'}:
                _fail('只能导出角色名字、人设、说话风格与形态；不能导出聊天或记忆。')
            manifest = _manifest(_json_bytes({**manifest, **profile}))
        destination = Path(destination)
        if destination.exists():
            _fail('目标文件已存在，请选择新的文件名。')
        if destination.resolve().is_relative_to(self.root.resolve()):
            _fail('导出文件不能保存在角色库内部。')
        temporary = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex + '.tmp')
        destination_created = False
        try:
            with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('role.json', _json_bytes(manifest))
                total = len(_json_bytes(manifest))
                for name in sorted(_references(manifest)):
                    data = _bounded_bytes(_within(folder, name), MAX_IMAGE)
                    total += len(data)
                    if total > MAX_TOTAL:
                        _fail('角色资源总量超过 128 MiB。')
                    archive.writestr(name, data)
            # Exclusive destination creation also prevents a concurrent export
            # from overwriting an existing delivery.
            with temporary.open('rb') as source, destination.open('xb') as output:
                destination_created = True
                shutil.copyfileobj(source, output)
            return destination
        except OSError as error:
            if destination_created:
                destination.unlink(missing_ok=True)
            raise RolePackError('角色包未能导出，请检查目标目录和可用空间。') from error
        finally:
            if temporary.exists():
                temporary.unlink()
