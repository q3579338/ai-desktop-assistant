"""Build a data-only 2D role ZIP from common raster images using trusted source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import zipfile


PERSONALITIES = ('gentle', 'playful', 'concise')
FORM_MODES = ('cat', 'catgirl', 'chat')
SOURCE_KINDS = ('user_image', 'local_img2img', 'local_background_removal', 'local_img2img_background_removal')
INPUT_EXTENSIONS=('.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif')


def normalize_input(data,suffix,destination,root):
    """Use the application's bounded decoder; never reinterpret code or workflows."""
    sys.path[:0]=[str(root),str(root/'.vendor')]
    try:
        from starpet import image_input
    except ImportError as error:
        raise ValueError('常见格式转换需要助手1.12或更新源码及对应PySide6；透明WebP回退另需Pillow。') from error
    if Path(image_input.__file__).resolve()!=root/'starpet/image_input.py':
        raise ValueError('图片解码模块不属于指定源码，请用新进程重试。')
    try:image,info=image_input.decode_image(data,suffix)
    except ImportError as error:raise ValueError('当前图片编码需要Pillow解码，请使用已准备好的制作环境。') from error
    if Path(destination).exists():raise ValueError('标准化图片已存在，不能覆盖。')
    if not image.save(str(destination),'PNG'):raise ValueError('标准化图片未能保存。')
    return info


def find_source(requested=None):
    candidates = [Path(requested)] if requested is not None else [Path.cwd(), Path(__file__).resolve().parents[3]]
    for candidate in candidates:
        root = candidate.resolve()
        if (root / 'main.py').is_file() and (root / 'starpet/rolepacks.py').is_file():
            return root
    raise ValueError('未找到星伴源码。请用 --app-source 指向同时包含 main.py 和 starpet/rolepacks.py 的项目目录。')


def build(source_image, output, name, persona='', app_source=None,
          personality='playful', form_mode='cat', source_kind='user_image'):
    if personality not in PERSONALITIES:
        raise ValueError('说话风格只支持 gentle、playful 或 concise。')
    if form_mode not in FORM_MODES:
        raise ValueError('角色形态只支持 cat、catgirl 或 chat。')
    if source_kind not in SOURCE_KINDS:
        raise ValueError('图片来源只支持已有原图、本机重绘、本机去背景或本机重绘后去背景。')
    source = Path(source_image).resolve()
    destination = Path(output).resolve()
    if not source.is_file():
        raise ValueError('未找到原始图片，请提供现有的常见格式图片。')
    if source.suffix.lower() not in INPUT_EXTENSIONS:
        raise ValueError('支持PNG、JPEG、WebP、BMP、TIFF和GIF。')
    if destination.suffix.lower() != '.zip':
        raise ValueError('输出文件请使用 .zip 后缀。')
    if source == destination or destination.exists():
        raise ValueError('输出文件已经存在，请选用新文件名；原图和已有交付不会被覆盖。')
    root = find_source(app_source)
    sys.path[:0] = [str(root), str(root / '.vendor')]
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    try:
        from PySide6.QtGui import QGuiApplication
        from starpet import rolepacks
    except ImportError as error:
        raise ValueError('项目的 Python/PySide6 依赖尚不可用。请使用项目 Python 环境或准备好 .vendor，再重试。') from error
    if Path(rolepacks.__file__).resolve() != root / 'starpet/rolepacks.py':
        raise ValueError('已载入的角色库不属于指定源码目录，请在新的 Python 进程中重试。')
    application = QGuiApplication.instance() or QGuiApplication([])
    if application.platformName() != 'offscreen':
        raise ValueError('角色包构建只允许无界面环境，已停止。')
    if source.stat().st_size > rolepacks.MAX_IMAGE:
        raise ValueError('原图超过当前角色库的大小上限。')
    original_hash = hashlib.sha256(rolepacks._bounded_bytes(source,rolepacks.MAX_IMAGE)).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / ('.rolepack-build-' + uuid.uuid4().hex)
    staging.mkdir()
    created_hash = None
    try:
        selected=source
        image_hash=original_hash
        normalization=None
        if source.suffix.lower()!='.png':
            selected=staging/'normalized.png'
            raw=rolepacks._bounded_bytes(source,rolepacks.MAX_IMAGE)
            if hashlib.sha256(raw).hexdigest()!=original_hash:raise ValueError('原图在读取时发生变化。')
            normalization=normalize_input(raw,source.suffix,selected,root)
            image_hash=hashlib.sha256(selected.read_bytes()).hexdigest()
        library = rolepacks.RoleLibrary(staging / 'state')
        identity = library.import_image(selected, name=name, persona=persona)
        # Change provenance only in this invocation's independent temporary
        # library. The application importer remains unchanged and export will
        # revalidate the complete manifest and actual final-image hash.
        manifest_path = library.root / identity / 'role.json'
        imported_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        expected_source = {'kind': source_kind, 'filename': source.name, 'sha256': image_hash}
        if normalization is not None:
            expected_source['attribution']='原始文件SHA-256: '+original_hash+'；已标准化PNG'+('；动态/多页只取第一帧' if normalization['first_frame_only'] else '')
        if (imported_manifest['source']['filename'] != selected.name
                or imported_manifest['source']['sha256'] != image_hash):
            raise ValueError('临时角色的图片来源文件或摘要不一致，停止交付。')
        imported_manifest['source'] = expected_source
        manifest_path.write_text(json.dumps(imported_manifest, ensure_ascii=False), encoding='utf-8')
        expected_profile = {'personality': personality, 'formMode': form_mode}
        library.export_package(identity, destination, profile=expected_profile)
        created_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        # Reuse the same authoritative importer against a different temporary
        # library, so the output is actually importable, not just valid ZIP.
        verification = rolepacks.RoleLibrary(staging / 'verification')
        imported = verification.import_package(destination)
        imported_profile = verification.profile(imported)
        spec = verification.spec(imported)
        verified_manifest = json.loads((verification.root / imported / 'role.json').read_text(encoding='utf-8'))
        if hashlib.sha256(spec['image'].read_bytes()).hexdigest() != image_hash:
            raise ValueError('导出角色图片与原图摘要不一致，停止交付。')
        if hashlib.sha256(rolepacks._bounded_bytes(source,rolepacks.MAX_IMAGE)).hexdigest() != original_hash:
            raise ValueError('构建期间原图发生变化，请使用稳定的图片副本重试。')
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise ValueError('生成的角色包未通过完整性检查。')
            manifest = json.loads(archive.read('role.json'))
            if manifest.get('source') != expected_source or verified_manifest.get('source') != expected_source:
                raise ValueError('导出角色包或重新导入后的图片来源不一致，停止交付。')
            if any(manifest.get(key) != value or imported_profile.get(key) != value
                   for key, value in expected_profile.items()):
                raise ValueError('角色包清单或重新导入后的风格、形态与所选设置不一致，停止交付。')
            count = len(archive.infolist())
        return {'created': True, 'output': str(destination), 'name': imported_profile['name'],
                'personality': personality, 'form_mode': form_mode,
                'source_kind': source_kind,
                'sha256': created_hash, 'files': count,
                'source_sha256': image_hash, 'original_sha256':original_hash,
                'normalization':normalization,'source_unchanged': True, 'import_verified': True,
                'schema': 1, 'app_source': str(root), 'production_state_used': False,
                'model_called': False, 'rendering': 'static_png_with_program_interaction'}
    except Exception:
        # Remove only the exact output bytes this invocation created. Do not
        # erase a file another process might have replaced during verification.
        if created_hash is not None and destination.is_file():
            if hashlib.sha256(destination.read_bytes()).hexdigest() == created_hash:
                destination.unlink()
        raise
    finally:
        resolved = staging.resolve()
        if resolved.parent != destination.parent or not resolved.name.startswith('.rolepack-build-'):
            raise RuntimeError('构建临时目录不在预期位置，已停止自动清理。')
        shutil.rmtree(resolved)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path, help='PNG/JPEG/WebP/BMP/TIFF/GIF; animated/multipage input uses its first frame')
    parser.add_argument('--name', required=True, help='Role display name')
    parser.add_argument('--persona', default='', help='Optional role personality description')
    parser.add_argument('--personality', choices=PERSONALITIES, default='playful',
                        help='Speaking style preset (default: playful for compatibility)')
    parser.add_argument('--form-mode', choices=FORM_MODES, default='cat',
                        help='Application display mode (default: cat; does not transform the image)')
    parser.add_argument('--source-kind', choices=SOURCE_KINDS, default='user_image',
                        help='Final-image provenance; defaults to an existing user image')
    parser.add_argument('--output', required=True, type=Path, help='New ZIP file; existing files are not overwritten')
    parser.add_argument('--app-source', type=Path, help='StarPet source root containing main.py and starpet/rolepacks.py')
    args = parser.parse_args(argv)
    try:
        result = build(args.image, args.output, args.name, args.persona, args.app_source,
                       personality=args.personality, form_mode=args.form_mode, source_kind=args.source_kind)
    except (ValueError, OSError) as error:
        parser.exit(2, '角色包未完成：' + str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
