import hashlib
import json
from pathlib import Path
import stat
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.vendor'))
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QImage, QColor
from starpet.rolepacks import (RoleLibrary, RolePackError, MAX_FILES,
                               MAX_IMAGE, MAX_TOTAL, MAX_MANIFEST)


def gif_bytes(frames=2):
    # A self-contained 1x1 GIF fixture: two colors, frame-local image data.
    result = bytearray(b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff')
    result.extend(b'\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00')
    for index in range(frames):
        result.extend(b'\x21\xf9\x04\x00\x04\x00\x00\x00')
        result.extend(b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02')
        result.extend(b'\x44\x01' if index % 2 == 0 else b'\x4c\x01')
        result.extend(b'\x00')
    result.extend(b'\x3b')
    return bytes(result)


class RolePackTests(unittest.TestCase):
    def setUp(self):
        fixtures = ROOT / 'evidence' / 'rolepack-tests'
        fixtures.mkdir(parents=True, exist_ok=True)
        self.folder = fixtures / ('fixture-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.library = RoleLibrary(self.folder / 'isolated-state')
        self.source = self.folder / '原创图片.png'
        image = QImage(4, 4, QImage.Format.Format_ARGB32)
        image.fill(QColor('#123456'))
        self.assertTrue(image.save(str(self.source), 'PNG'))
        self.png = self.source.read_bytes()
        self.manifest = {'schema': 1, 'name': '测试角色', 'description': '离线测试',
                         'persona': '请自然地说话。', 'personality': 'gentle', 'formMode': 'cat',
                         'image': 'image.png', 'source': {'kind': 'test fixture',
                         'filename': '原创图片.png', 'sha256': hashlib.sha256(self.png).hexdigest()}}

    def tearDown(self):
        if self.folder.resolve().is_relative_to((ROOT / 'evidence' / 'rolepack-tests').resolve()):
            shutil.rmtree(self.folder)

    def archive(self, manifest=None, resources=None, extras=None, compression=zipfile.ZIP_DEFLATED):
        path = self.folder / ('pack-' + str(len(list(self.folder.glob('pack-*')))) + '.zip')
        with zipfile.ZipFile(path, 'w', compression=compression) as archive:
            archive.writestr('role.json', json.dumps(manifest if manifest is not None else self.manifest, ensure_ascii=False))
            for key, value in (resources if resources is not None else {'image.png': self.png}).items():
                archive.writestr(key, value)
            for key, value in (extras or []):
                archive.writestr(key, value)
        return path

    def assert_rejected_without_install(self, path):
        before = set(self.library.root.glob('*')) if self.library.root.exists() else set()
        with self.assertRaises(RolePackError):
            self.library.import_package(path)
        after = set(self.library.root.glob('*')) if self.library.root.exists() else set()
        self.assertEqual(before, after)

    def test_static_import_roundtrip_preserves_bytes_and_current_profile_only(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        identity = self.library.import_image(self.source, '../../名字不是路径', '用户自定义人设')
        self.assertRegex(identity, r'^custom-[0-9a-f]{32}$')
        spec = self.library.spec(identity)
        self.assertEqual(spec['kind'], 'image')
        self.assertEqual(spec['image'].read_bytes(), self.png)
        self.assertEqual(self.library.profile(identity)['name'], '../../名字不是路径')
        self.assertEqual(next(r for r in self.library.list_roles() if r['id'] == identity)['kind'], 'image')
        # These extra local files must never enter an export.
        for name in ('companion-memory.json', 'auth.json', 'unreferenced.png', 'run.py'):
            (spec['image'].parent / name).write_text('PRIVATE_FIXTURE', encoding='utf-8')
        exported = self.library.export_package(identity, self.folder / 'export.zip',
                    profile={'name': '更名后的角色', 'persona': '新的角色设定'})
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(set(archive.namelist()), {'role.json', 'image.png'})
            manifest = json.loads(archive.read('role.json'))
            self.assertEqual(manifest['name'], '更名后的角色')
            self.assertEqual(manifest['source']['sha256'], before)
            self.assertEqual(archive.read('image.png'), self.png)
        other = RoleLibrary(self.folder / 'second-state')
        restored = other.import_package(exported)
        self.assertEqual(other.profile(restored)['persona'], '新的角色设定')
        self.assertEqual(other.spec(restored)['image'].read_bytes(), self.png)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        with self.assertRaises(RolePackError):
            self.library.export_package(identity, self.folder / 'bad-export.zip', profile={'memory': 'PRIVATE_FIXTURE'})
        with self.assertRaises(RolePackError):
            self.library.export_package(identity, exported)

    def test_animated_pack_roundtrip_and_shared_clip(self):
        manifest = {**self.manifest, 'clips': {
            'idle': {'file': 'motions/idle.gif', 'loop': True},
            'head_pat': {'file': 'motions/idle.gif', 'loop': False}}}
        animation = gif_bytes()
        identity = self.library.import_package(self.archive(manifest,
                    {'image.png': self.png, 'motions/idle.gif': animation}))
        spec = self.library.spec(identity)
        self.assertIsInstance(spec['clips']['idle']['file'], Path)
        self.assertTrue(spec['clips']['idle']['loop'])
        self.assertFalse(spec['clips']['head_pat']['loop'])
        self.assertEqual(spec['clips']['idle']['file'].read_bytes(), animation)
        exported = self.library.export_package(identity, self.folder / 'animated-export.zip')
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(archive.read('motions/idle.gif'), animation)
        another = self.library.import_package(exported)
        self.assertNotEqual(another, identity)
        self.assertEqual(self.library.spec(another)['clips']['idle']['file'].read_bytes(), animation)

    def test_atlas_import_and_builtin_export_contract(self):
        states = {name: index for index, name in enumerate(('idle', 'thinking', 'working', 'waiting', 'completed', 'error'))}
        manifest = {**self.manifest, 'atlas': {'columns': 3, 'rows': 2, 'states': states}}
        identity = self.library.import_package(self.archive(manifest))
        spec = self.library.spec(identity)
        self.assertEqual(spec['kind'], 'atlas')
        self.assertEqual(spec['columns'], 3)
        self.assertEqual(spec['states'], states)
        builtins = self.library.list_roles()[:3]
        self.assertEqual([r['id'] for r in builtins], ['tabby', 'catgirl', 'robot'])
        self.assertTrue(all(r['kind'] == 'atlas' for r in builtins))
        # Supply tiny isolated built-in artwork instead of touching production assets.
        assets = self.folder / 'fixture-assets'
        builtin = assets / 'characters' / 'tabby'
        builtin.mkdir(parents=True)
        (builtin / 'atlas.png').write_bytes(self.png)
        (builtin / 'character.json').write_text(json.dumps({'image': 'atlas.png', 'columns': 3,
            'rows': 2, 'states': states, 'source': 'original fixture', 'generation_prompt': 'test only'}))
        library = RoleLibrary(self.folder / 'third-state', assets)
        output = library.export_package('tabby', self.folder / 'builtin.zip')
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('atlas.png'), self.png)
            self.assertEqual(json.loads(archive.read('role.json'))['source']['promptSummary'], 'test only')

    def test_reject_path_traversal_windows_paths_urls_and_reserved_names(self):
        for name in ('../evil.png', '/evil.png', 'C:/evil.png', 'C:\\evil.png',
                     '\\\\server\\evil.png', 'https://example.com/a.png', 'a/../evil.png',
                     'CON.png', 'a/NUL/file.png', 'a./file.png', 'a /file.png', 'a//file.png',
                     'a/b/c/d/file.png'):
            with self.subTest(name=name):
                self.assert_rejected_without_install(self.archive(extras=[(name, self.png)]))

    def test_reject_case_collisions_symlinks_extra_files_and_duplicate_json_fields(self):
        self.assert_rejected_without_install(self.archive(extras=[('IMAGE.PNG', self.png)]))
        self.assert_rejected_without_install(self.archive(extras=[('image.png', self.png)]))
        self.assert_rejected_without_install(self.archive(extras=[('A/x.gif', gif_bytes()), ('a/y.gif', gif_bytes())]))
        info = zipfile.ZipInfo('link.png')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assert_rejected_without_install(self.archive(extras=[(info, b'../other.png')]))
        for name in ('run.py', 'index.html', 'icon.svg', 'auth.json', 'extra.png'):
            self.assert_rejected_without_install(self.archive(extras=[(name, b'PRIVATE_FIXTURE')]))
        path = self.folder / 'duplicate-json.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('role.json', '{"schema":1,"name":"a","name":"b","image":"image.png"}')
            archive.writestr('image.png', self.png)
        self.assert_rejected_without_install(path)

    def test_reject_malformed_mislabeled_or_missing_images(self):
        for content in (b'<svg></svg>', b'print("hello")', self.png[:30], gif_bytes(), b''):
            self.assert_rejected_without_install(self.archive(resources={'image.png': content}))
        self.assert_rejected_without_install(self.archive(resources={}))
        self.assert_rejected_without_install(self.archive({**self.manifest, 'image': 'image.svg'}, {'image.svg': b'<svg/>'}))
        image = self.folder / 'animation.gif'
        image.write_bytes(gif_bytes())
        with self.assertRaises(RolePackError):
            self.library.import_image(image, '需要静态回退')

    def test_reject_manifest_data_leaks_and_invalid_clips(self):
        for manifest in ({**self.manifest, 'memory': ['private']},
                         {**self.manifest, 'source': {'apiKey': 'private'}},
                         {**self.manifest, 'source': {'filename': 'C:/private/name.png'}},
                         {**self.manifest, 'clips': {'idle': {'file': 'a.gif', 'loop': 'yes'}}},
                         {**self.manifest, 'clips': {'execute': {'file': 'a.gif', 'loop': True}}}):
            self.assert_rejected_without_install(self.archive(manifest))

    def test_reject_file_count_sizes_compression_bomb_and_frame_limit(self):
        self.assert_rejected_without_install(self.archive(extras=[(f'x{i}.png', self.png) for i in range(MAX_FILES)]))
        self.assert_rejected_without_install(self.archive(extras=[('bomb.png', b'0' * (2 * 1024 * 1024))]))
        with patch('starpet.rolepacks.MAX_IMAGE', len(self.png) - 1):
            self.assert_rejected_without_install(self.archive())
        with patch('starpet.rolepacks.MAX_TOTAL', len(self.png) - 1):
            self.assert_rejected_without_install(self.archive())
        with patch('starpet.rolepacks.MAX_MANIFEST', 20):
            self.assert_rejected_without_install(self.archive())
        manifest = {**self.manifest, 'clips': {'idle': {'file': 'animation.gif', 'loop': True}}}
        self.assert_rejected_without_install(self.archive(manifest,
            {'image.png': self.png, 'animation.gif': gif_bytes(601)}))

    def test_dimension_and_decode_budget_limits_do_not_install(self):
        oversized = self.folder / 'too-wide.png'
        image = QImage(4097, 1, QImage.Format.Format_ARGB32)
        image.fill(QColor('white'))
        self.assertTrue(image.save(str(oversized), 'PNG'))
        self.assert_rejected_without_install(self.archive(resources={'image.png': oversized.read_bytes()}))
        manifest = {**self.manifest, 'clips': {'idle': {'file': 'animation.gif', 'loop': True}}}
        with patch('starpet.rolepacks.MAX_ANIMATION_PIXELS', 16):
            self.assert_rejected_without_install(self.archive(manifest,
                {'image.png': self.png, 'animation.gif': gif_bytes(17)}))

    def test_broken_archives_and_oversized_number_are_reported_as_pack_errors(self):
        path = self.folder / 'broken.zip'
        path.write_bytes(b'not a zip file')
        self.assert_rejected_without_install(path)
        path = self.folder / 'giant-number.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('role.json', '{"schema":' + '1' * 5000 + ',"name":"x","image":"image.png"}')
            archive.writestr('image.png', self.png)
        self.assert_rejected_without_install(path)

    def test_bad_role_ids_do_not_escape_library_and_exports_reject_library_targets(self):
        for identity in ('../other', 'custom-../../x', 'custom-X', ''):
            with self.assertRaises(RolePackError):
                self.library.profile(identity)
            with self.assertRaises(RolePackError):
                self.library.spec(identity)
        identity = self.library.import_image(self.source, '正常角色')
        with self.assertRaises(RolePackError):
            self.library.export_package(identity, self.library.root / 'inside.zip')


if __name__ == '__main__':
    unittest.main()
