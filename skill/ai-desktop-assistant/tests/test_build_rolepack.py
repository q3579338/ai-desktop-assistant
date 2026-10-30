import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile


SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]
SCRIPT = SKILL / 'scripts/build_rolepack.py'


@contextlib.contextmanager
def fixture_directory():
    # Explicit workspace override is useful on read-only Skill installations.
    # mkdir inherits the workspace ACL; tempfile.mkdtemp's owner-only mode can
    # prevent child processes under a restricted Windows identity accessing it.
    base = Path(os.environ.get('STARPET_SKILL_TEST_TMPDIR') or tempfile.gettempdir()).resolve()
    if base == SKILL or SKILL in base.parents:
        raise RuntimeError('Role-pack tests require a temporary directory outside the Skill installation')
    base.mkdir(parents=True, exist_ok=True)
    folder = base / ('build-fixture-' + uuid.uuid4().hex[:10])
    folder.mkdir()
    try:
        yield folder
    finally:
        resolved = folder.resolve()
        assert resolved.parent == base and resolved.name.startswith('build-fixture-')
        shutil.rmtree(resolved)


class RoleBuildTests(unittest.TestCase):
    def run_builder(self, *args):
        return subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def test_fixture_directory_honors_workspace_override_and_cleans_up(self):
        with fixture_directory() as parent:
            base = parent / 'chosen-workspace-temp'
            with patch.dict(os.environ, {'STARPET_SKILL_TEST_TMPDIR': str(base)}):
                with fixture_directory() as folder:
                    self.assertEqual(folder.parent, base)
                    self.assertNotIn(SKILL, folder.parents)
                    (folder / 'fixture.txt').write_text('fixture', encoding='utf-8')
                self.assertFalse(folder.exists())

    def test_fixture_directory_refuses_read_only_skill_tree(self):
        with patch.dict(os.environ, {'STARPET_SKILL_TEST_TMPDIR': str(SKILL / 'tests')}):
            with self.assertRaises(RuntimeError), fixture_directory():
                self.fail('Fixture must not be created inside the Skill installation')

    def test_missing_source_is_reported_without_creating_output_or_state(self):
        with fixture_directory() as folder:
            source = folder / 'source.png'; source.write_bytes(b'fixture content')
            output = folder / 'result.zip'
            result = self.run_builder('--image', source, '--name', '测试', '--output', output,
                                      '--app-source', folder / 'missing-source')
            self.assertEqual(result.returncode, 2)
            self.assertIn('源码', result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(set(path.name for path in folder.iterdir()), {'source.png'})

    @unittest.skipUnless((PROJECT / 'starpet/rolepacks.py').is_file() and
                         (PROJECT / 'assets/starpet.png').is_file(),
                         'Actual build roundtrip needs an adjacent StarPet source checkout')
    def test_actual_cli_roundtrip_preserves_image_and_refuses_existing_output(self):
        original = PROJECT / 'assets/starpet.png'
        original_bytes = original.read_bytes()
        with fixture_directory() as folder:
            output = folder / '我的角色.zip'
            args = ('--app-source', PROJECT, '--image', original, '--name', '离线测试助手',
                    '--persona', '仅作角色包测试，不写正式档', '--output', output)
            result = self.run_builder(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['import_verified']); self.assertTrue(report['source_unchanged'])
            self.assertFalse(report['production_state_used']); self.assertFalse(report['model_called'])
            self.assertEqual(report['source_sha256'], hashlib.sha256(original_bytes).hexdigest())
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {'role.json', 'image.png'})
                self.assertEqual(archive.read('image.png'), original_bytes)
                manifest = json.loads(archive.read('role.json'))
                self.assertEqual(manifest['name'], '离线测试助手')
                self.assertEqual(manifest['personality'], 'playful')
                self.assertEqual(manifest['formMode'], 'cat')
                self.assertEqual(report['personality'], 'playful')
                self.assertEqual(report['form_mode'], 'cat')
                self.assertEqual(report['source_kind'], 'user_image')
                self.assertEqual(manifest['source']['kind'], 'user_image')
            self.assertFalse(list(folder.glob('.rolepack-build-*')))
            before = output.read_bytes()
            repeated = self.run_builder(*args)
            self.assertEqual(repeated.returncode, 2)
            self.assertIn('已经存在', repeated.stderr)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(original.read_bytes(), original_bytes)

    @unittest.skipUnless((PROJECT / 'starpet/rolepacks.py').is_file() and
                         (PROJECT / 'assets/starpet.png').is_file(),
                         'Explicit role profiles need the actual adjacent source checkout')
    def test_explicit_style_and_form_survive_export_and_actual_reimport(self):
        original = PROJECT / 'assets/starpet.png'
        original_bytes = original.read_bytes()
        with fixture_directory() as folder:
            for personality, form_mode in (('gentle', 'cat'), ('concise', 'chat'), ('playful', 'catgirl')):
                with self.subTest(personality=personality, form_mode=form_mode):
                    output = folder / (personality + '.zip')
                    persona = '你叫小灯，温柔简洁，只使用已选择的本机模型。'
                    result = self.run_builder('--app-source', PROJECT, '--image', original,
                        '--name', '小灯', '--persona', persona, '--personality', personality,
                        '--form-mode', form_mode, '--output', output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertTrue(report['import_verified'])
                    self.assertEqual(report['personality'], personality)
                    self.assertEqual(report['form_mode'], form_mode)
                    with zipfile.ZipFile(output) as archive:
                        manifest = json.loads(archive.read('role.json'))
                        self.assertEqual(manifest['name'], '小灯')
                        self.assertEqual(manifest['persona'], persona)
                        self.assertEqual(manifest['personality'], personality)
                        self.assertEqual(manifest['formMode'], form_mode)
                        self.assertEqual(archive.read('image.png'), original_bytes)
            self.assertFalse(list(folder.glob('.rolepack-build-*')))
            self.assertEqual(original.read_bytes(), original_bytes)

    def test_invalid_style_and_form_are_rejected_before_creating_output(self):
        with fixture_directory() as folder:
            source = folder / 'source.png'; source.write_bytes(b'fixture content')
            for flag in ('--personality', '--form-mode', '--source-kind'):
                with self.subTest(flag=flag):
                    output = folder / (flag[2:] + '.zip')
                    result = self.run_builder('--image', source, '--name', '小灯',
                                              flag, 'invalid-choice', '--output', output)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(flag, result.stderr)
                    self.assertFalse(output.exists())
            self.assertEqual(set(path.name for path in folder.iterdir()), {'source.png'})

    @unittest.skipUnless((PROJECT / 'starpet/rolepacks.py').is_file() and
                         (PROJECT / 'assets/starpet.png').is_file(),
                         'Explicit provenance roundtrip needs the actual source checkout')
    def test_processed_image_provenance_survives_export_and_reimport(self):
        original = PROJECT / 'assets/starpet.png'
        image_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        with fixture_directory() as folder:
            for index, source_kind in enumerate(('local_img2img', 'local_background_removal', 'local_img2img_background_removal')):
                with self.subTest(source_kind=source_kind):
                    output = folder / f'kind-{index}.zip'
                    result = self.run_builder('--app-source', PROJECT, '--image', original,
                        '--name', '来源验证助手', '--source-kind', source_kind, '--output', output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertTrue(report['import_verified'])
                    self.assertEqual(report['source_kind'], source_kind)
                    with zipfile.ZipFile(output) as archive:
                        manifest = json.loads(archive.read('role.json'))
                        self.assertEqual(manifest['source'], {'kind': source_kind, 'filename': original.name, 'sha256': image_hash})
                    self.assertFalse(list(folder.glob('.rolepack-build-*')))

    @unittest.skipUnless((PROJECT / 'starpet/rolepacks.py').is_file(),
                         'Invalid-image validation needs the actual role library')
    def test_invalid_image_is_rejected_by_library_and_temporary_state_is_removed(self):
        with fixture_directory() as folder:
            source = folder / 'broken.png'; source.write_bytes(b'not an image')
            output = folder / 'result.zip'
            result = self.run_builder('--app-source', PROJECT, '--image', source,
                                      '--name', '坏图测试', '--output', output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            self.assertFalse(list(folder.glob('.rolepack-build-*')))
            self.assertEqual(source.read_bytes(), b'not an image')


if __name__ == '__main__':
    unittest.main()
