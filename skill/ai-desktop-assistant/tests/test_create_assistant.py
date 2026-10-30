import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile
import zlib


SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import create_assistant as creator
from generate_local_image import GenerationError


def png(red=80):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    header = chunk(b'IHDR', struct.pack('!IIBBBBB', 8, 8, 8, 2, 0, 0, 0))
    pixels = zlib.compress((b'\0' + bytes((red, 120, 180)) * 8) * 8)
    return b'\x89PNG\r\n\x1a\n' + header + chunk(b'IDAT', pixels) + chunk(b'IEND', b'')


class CreateAssistantTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get('STARPET_SKILL_TEST_TMPDIR') or tempfile.gettempdir()).resolve()
        if self.base == SKILL or SKILL in self.base.parents:
            raise RuntimeError('Skill tests must use an independent temporary workspace')
        self.base.mkdir(parents=True, exist_ok=True)
        self.folder = self.base / ('create-assistant-fixture-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.image = self.folder / 'my original.png'
        self.image.write_bytes(png())
        self.original = self.image.read_bytes()
        self.output = self.folder / 'deliveries'
        self.source = self.folder / 'app-source'
        self.background_model = self.folder / 'fixture-model.onnx'
        self.background_model.write_bytes(b'fixture; never loaded')
        (self.source / 'starpet').mkdir(parents=True)
        (self.source / 'main.py').write_text('# fixture', encoding='utf-8')
        (self.source / 'starpet' / 'rolepacks.py').write_text('# fixture', encoding='utf-8')

    def tearDown(self):
        resolved = self.folder.resolve()
        if resolved.parent != self.base or not resolved.name.startswith('create-assistant-fixture-'):
            raise RuntimeError('Unexpected cleanup path')
        shutil.rmtree(resolved)

    def create(self, **kwargs):
        return creator.create(self.image, '我的小灯', self.source, self.output, **kwargs)

    @staticmethod
    def fake_builder(image, output, name, persona, app_source, personality='playful', source_kind='user_image'):
        data = Path(image).read_bytes()
        with zipfile.ZipFile(output, 'x') as archive:
            archive.writestr('image.png', data)
            archive.writestr('role.json', json.dumps({'name': name, 'persona': persona, 'personality': personality,
                'source': {'kind': source_kind, 'filename': Path(image).name, 'sha256': hashlib.sha256(data).hexdigest()}}))
        return {'created': True, 'import_verified': True, 'source_unchanged': True,
                'production_state_used': False, 'model_called': False,
                'source_sha256': hashlib.sha256(data).hexdigest(),
                'sha256': hashlib.sha256(Path(output).read_bytes()).hexdigest(),
                'source_kind': source_kind,
                'private_config': 'DO-NOT-COPY-CONFIG', 'app_source': 'DO-NOT-COPY-SOURCE-PATH'}

    @staticmethod
    def fake_generator(image, output_dir, checkpoint, prompt, **kwargs):
        folder = Path(output_dir) / 'local-image-fixture'
        folder.mkdir(parents=True)
        path = folder / 'portrait.png'
        path.write_bytes(png(200))
        report = folder / 'job.json'
        value = {'status': 'completed', 'image': str(path), 'report': str(report),
                 'input_sha256': hashlib.sha256(Path(image).read_bytes()).hexdigest(),
                 'output_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'prompt_id': 'fixture-job-123', 'server_may_continue': False,
                 'private_config': 'DO-NOT-COPY-CONFIG'}
        report.write_text(json.dumps({key: value[key] for key in ('status', 'prompt_id', 'server_may_continue')}), encoding='utf-8')
        return value

    def generation_args(self):
        return {'generate': True, 'comfy_url': 'http://localhost:8188',
                'checkpoint': 'existing-model.safetensors', 'prompt': 'PRIVATE-PROMPT-ONLY-FOR-LOCAL-GENERATOR'}

    @staticmethod
    def fake_background(image, output, model):
        output = Path(output)
        output.write_bytes(png(230))  # Protocol fixture only; no inference.
        report = output.with_name(output.stem + '.background.json')
        report.write_text('{"status":"completed"}', encoding='utf-8')
        return {'status': 'completed', 'image': str(output), 'report': str(report),
                'input_sha256': hashlib.sha256(Path(image).read_bytes()).hexdigest(),
                'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'model_sha256': hashlib.sha256(Path(model).read_bytes()).hexdigest(),
                'model_verified': True, 'rgb_preserved': True, 'alpha_min': 0, 'alpha_max': 255}

    def test_default_build_uses_original_copy_and_never_calls_generator_or_copies_config(self):
        with patch.object(creator.build_rolepack, 'build', side_effect=self.fake_builder) as builder, patch.object(creator.generate_local_image, 'generate') as generator, patch.object(creator, '_remove_background') as background:
            result = self.create(persona='PRIVATE-PERSONA-ONLY-FOR-ROLE', personality='gentle')
        self.assertTrue(result['created'])
        generator.assert_not_called()
        background.assert_not_called()
        folder = Path(result['output_dir'])
        self.assertEqual((folder / 'source.png').read_bytes(), self.original)
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertEqual(builder.call_args.args[0], folder / 'source.png')
        self.assertEqual(builder.call_args.kwargs['personality'], 'gentle')
        self.assertEqual(builder.call_args.kwargs['source_kind'], 'user_image')
        self.assertFalse(builder.call_args.args[1].parent.exists())
        with zipfile.ZipFile(folder / 'assistant.rolepack.zip') as archive:
            self.assertEqual(archive.read('image.png'), self.original)
            self.assertEqual(json.loads(archive.read('role.json'))['persona'], 'PRIVATE-PERSONA-ONLY-FOR-ROLE')
        public = (folder / 'result.json').read_text(encoding='utf-8') + (folder / 'README.md').read_text(encoding='utf-8')
        for private in ('PRIVATE-PERSONA', 'DO-NOT-COPY-CONFIG', 'DO-NOT-COPY-SOURCE-PATH'):
            self.assertNotIn(private, public)
        self.assertIn('手动导入', result['message'])
        self.assertEqual(result['completed_stages'], ['validate', 'copy_source', 'package', 'verify'])

    def test_explicit_generation_passes_normalized_local_url_then_packages_generated_image(self):
        with patch.object(creator.build_rolepack, 'build', side_effect=self.fake_builder) as builder, patch.object(creator.generate_local_image, 'generate', side_effect=self.fake_generator) as generator:
            result = self.create(**self.generation_args(), personality='concise', seed=42, steps=12)
        self.assertTrue(result['created'])
        generator.assert_called_once()
        self.assertEqual(generator.call_args.kwargs['url'], 'http://127.0.0.1:8188')
        self.assertEqual(generator.call_args.kwargs['seed'], 42)
        self.assertEqual(generator.call_args.kwargs['steps'], 12)
        self.assertEqual(builder.call_args.args[0].name, 'portrait.png')
        self.assertEqual(builder.call_args.kwargs['source_kind'], 'local_img2img')
        folder = Path(result['output_dir'])
        with zipfile.ZipFile(folder / 'assistant.rolepack.zip') as archive:
            self.assertEqual(archive.read('image.png'), png(200))
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertEqual(result['generation']['prompt_id'], 'fixture-job-123')
        self.assertNotIn('PRIVATE-PROMPT', json.dumps(result))
        self.assertNotIn('DO-NOT-COPY', json.dumps(result))

    def test_missing_opt_in_or_required_generation_arguments_stops_before_any_call(self):
        for options in ({'prompt': 'not opted in'}, {'comfy_url': 'http://127.0.0.1:8188'},
                        {'generate': True}, {'generate': True, 'checkpoint': 'existing.safetensors', 'prompt': 'x'}):
            with self.subTest(options=options), patch.object(creator.build_rolepack, 'build') as builder, patch.object(creator.generate_local_image, 'generate') as generator:
                result = self.create(**options)
                self.assertFalse(result['created'])
                self.assertEqual(result['stage'], 'validate')
                generator.assert_not_called()
                builder.assert_not_called()

    def test_external_or_credential_url_rejected_without_leaking_input(self):
        for url in ('https://remote.example', 'http://user:PRIVATE-KEY@127.0.0.1:8188',
                    'http://127.0.0.1:8188/?token=PRIVATE-KEY'):
            with self.subTest(url=url), patch.object(creator.generate_local_image, 'generate') as generator:
                result = self.create(**{**self.generation_args(), 'comfy_url': url})
                self.assertFalse(result['created'])
                generator.assert_not_called()
                self.assertNotIn('PRIVATE-KEY', json.dumps(result))
                self.assertNotIn('PRIVATE-KEY', (Path(result['output_dir']) / 'README.md').read_text(encoding='utf-8'))

    def test_unknown_submission_keeps_exact_job_stage_and_does_not_build_or_retry(self):
        def unknown(image, output_dir, *args, **kwargs):
            folder = Path(output_dir) / 'local-image-uncertain'
            folder.mkdir(parents=True)
            report = folder / 'job.json'
            report.write_text(json.dumps({'status': 'submission_unknown', 'requested_prompt_id': 'request-42',
                                          'prompt_id': None, 'server_may_continue': True}), encoding='utf-8')
            raise GenerationError('PRIVATE-ERROR-TEXT', report=str(report))
        with patch.object(creator.generate_local_image, 'generate', side_effect=unknown) as generator, patch.object(creator.build_rolepack, 'build') as builder:
            result = self.create(**self.generation_args())
        self.assertFalse(result['created'])
        self.assertEqual(result['stage'], 'generate')
        self.assertEqual(result['generation']['status'], 'submission_unknown')
        self.assertEqual(result['generation']['requested_prompt_id'], 'request-42')
        self.assertTrue(result['generation']['server_may_continue'])
        generator.assert_called_once()
        builder.assert_not_called()
        self.assertNotIn('PRIVATE-ERROR', json.dumps(result))
        self.assertIn('没有自动重试', (Path(result['output_dir']) / 'README.md').read_text(encoding='utf-8'))

    def test_timed_out_job_id_and_unexpected_failure_are_reported_conservatively(self):
        for error in (GenerationError('timeout', prompt_id='existing-job-7'), RuntimeError('PRIVATE-CONFIG-ERROR')):
            with self.subTest(error=type(error).__name__), patch.object(creator.generate_local_image, 'generate', side_effect=error), patch.object(creator.build_rolepack, 'build') as builder:
                result = self.create(**self.generation_args())
                builder.assert_not_called()
                self.assertFalse(result['created'])
                self.assertNotIn('PRIVATE-CONFIG', json.dumps(result))
                if isinstance(error, GenerationError):
                    self.assertEqual(result['generation']['prompt_id'], 'existing-job-7')
                    self.assertTrue(result['generation']['server_may_continue'])
                else:
                    self.assertTrue(result['generation']['server_may_continue'])

    def test_build_failure_or_missing_import_verification_cannot_claim_success(self):
        for outcome in (RuntimeError('PRIVATE-KEY'), {'created': True, 'import_verified': False}):
            with self.subTest(outcome=type(outcome).__name__):
                with patch.object(creator.build_rolepack, 'build') as builder:
                    if isinstance(outcome, Exception):
                        builder.side_effect = outcome
                    else:
                        builder.return_value = outcome
                    result = self.create()
                self.assertEqual(result['stage'], 'package')
                self.assertFalse(result['created'])
                self.assertNotIn('rolepack', result)
                self.assertNotIn('PRIVATE-KEY', json.dumps(result))
                self.assertIn('未完成', (Path(result['output_dir']) / 'README.md').read_text(encoding='utf-8'))
                self.assertFalse(builder.call_args.args[1].parent.exists())

    def test_generated_image_cannot_escape_this_invocation_or_use_wrong_hash(self):
        for outside in (True, False):
            def bad(*args, **kwargs):
                value = self.fake_generator(*args, **kwargs)
                if outside:
                    value['image'] = str(self.image)
                else:
                    value['output_sha256'] = '0' * 64
                return value
            with self.subTest(outside=outside), patch.object(creator.generate_local_image, 'generate', side_effect=bad), patch.object(creator.build_rolepack, 'build') as builder:
                result = self.create(**self.generation_args())
                self.assertFalse(result['created'])
                self.assertEqual(result['stage'], 'generate')
                self.assertEqual(result['generation']['status'], 'completed')
                builder.assert_not_called()

    def test_repeated_invocations_are_unique_and_existing_delivery_is_untouched(self):
        with patch.object(creator.build_rolepack, 'build', side_effect=self.fake_builder):
            first = self.create()
            folder = Path(first['output_dir'])
            before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
            second = self.create()
        self.assertNotEqual(first['output_dir'], second['output_dir'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})
        identity = folder.name.removeprefix('assistant-')
        with patch.object(creator.uuid, 'uuid4', return_value=type('Id', (), {'hex': identity})()), self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})

    def test_external_original_change_fails_final_verification(self):
        def changed(*args, **kwargs):
            result = self.fake_builder(*args, **kwargs)
            self.image.write_bytes(png(180))  # Deliberate external-fixture change.
            return result
        with patch.object(creator.build_rolepack, 'build', side_effect=changed):
            result = self.create()
        self.assertFalse(result['created'])
        self.assertEqual(result['stage'], 'verify')
        self.assertFalse(result['source']['unchanged'])

    def test_background_removal_requires_explicit_flag_and_existing_model(self):
        for options in ({'background_model': self.background_model}, {'remove_background': True},
                        {'remove_background': True, 'background_model': self.folder / 'missing.onnx'}):
            with self.subTest(options=options), patch.object(creator, '_remove_background') as background, patch.object(creator.build_rolepack, 'build') as builder:
                result = self.create(**options)
                self.assertFalse(result['created'])
                self.assertEqual(result['stage'], 'validate')
                background.assert_not_called()
                builder.assert_not_called()

    def test_background_can_follow_original_or_generation_and_keeps_each_image(self):
        for repaint in (False, True):
            with self.subTest(repaint=repaint), patch.object(creator, '_remove_background', side_effect=self.fake_background) as background, patch.object(creator.generate_local_image, 'generate', side_effect=self.fake_generator), patch.object(creator.build_rolepack, 'build', side_effect=self.fake_builder) as builder:
                options = self.generation_args() if repaint else {}
                result = self.create(**options, remove_background=True, background_model=self.background_model)
                self.assertTrue(result['created'])
                self.assertEqual(background.call_args.args[0].name, 'portrait.png' if repaint else 'source.png')
                self.assertEqual(builder.call_args.args[0].name, 'cutout.png')
                expected_kind = 'local_img2img_background_removal' if repaint else 'local_background_removal'
                self.assertEqual(builder.call_args.kwargs['source_kind'], expected_kind)
                self.assertEqual(result['rolepack']['source_kind'], expected_kind)
                folder = Path(result['output_dir'])
                self.assertEqual((folder / 'source.png').read_bytes(), self.original)
                self.assertTrue((folder / result['background_removal']['image']).is_file())
                self.assertTrue(result['background_removal']['needs_visual_review'])
                self.assertEqual(result['rolepack']['image'], 'cutout.png')
                if repaint:
                    self.assertTrue((folder / result['generation']['image']).is_file())
                self.assertEqual(self.image.read_bytes(), self.original)
                self.assertIn('目视检查', (folder / 'README.md').read_text(encoding='utf-8'))
                with zipfile.ZipFile(folder / 'assistant.rolepack.zip') as archive:
                    self.assertEqual(archive.read('image.png'), png(230))
                    self.assertEqual(json.loads(archive.read('role.json'))['source']['kind'], expected_kind)

    def test_unverified_or_failed_background_stops_before_packaging(self):
        for error in ('model', 'hash', 'alpha', 'failure'):
            def bad(*args, **kwargs):
                if error == 'failure':
                    raise RuntimeError('PRIVATE-MODEL-CONFIG')
                value = self.fake_background(*args, **kwargs)
                if error == 'model':
                    value['model_verified'] = False
                elif error == 'hash':
                    value['output_sha256'] = '0' * 64
                else:
                    value['alpha_min'] = value['alpha_max'] = 255
                return value
            with self.subTest(error=error), patch.object(creator, '_remove_background', side_effect=bad), patch.object(creator.build_rolepack, 'build') as builder:
                result = self.create(remove_background=True, background_model=self.background_model)
                self.assertFalse(result['created'])
                self.assertEqual(result['stage'], 'remove_background')
                builder.assert_not_called()
                self.assertNotIn('PRIVATE-MODEL-CONFIG', json.dumps(result))

    @unittest.skipUnless((PROJECT / 'starpet/rolepacks.py').is_file() and (PROJECT / 'assets/starpet.png').is_file(),
                         'Actual import roundtrip requires the adjacent application checkout')
    def test_actual_default_cli_build_reimports_role_without_models_or_real_state(self):
        original = PROJECT / 'assets/starpet.png'
        before = original.read_bytes()
        command = [sys.executable, '-B', '-X', 'utf8', str(SKILL / 'scripts/create_assistant.py'),
                   '--image', str(original), '--name', '离线制作测试', '--app-source', str(PROJECT),
                   '--output-dir', str(self.output), '--personality', 'gentle', '--persona', '虚构的测试助手。']
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result['created'])
        self.assertTrue(result['rolepack']['import_verified'])
        self.assertFalse(result['generation_requested'])
        self.assertFalse(result['production_state_used'])
        self.assertEqual(original.read_bytes(), before)
        folder = Path(result['output_dir'])
        self.assertFalse(list(folder.glob('.rolepack-build-*')))
        with zipfile.ZipFile(folder / result['rolepack']['file']) as archive:
            self.assertEqual(set(archive.namelist()), {'role.json', 'image.png'})
            role = json.loads(archive.read('role.json'))
            self.assertEqual(role['name'], '离线制作测试')
            self.assertEqual(role['personality'], 'gentle')
            self.assertEqual(role['persona'], '虚构的测试助手。')
            self.assertEqual(role['source']['kind'], 'user_image')
            self.assertEqual(archive.read('image.png'), before)


if __name__ == '__main__':
    unittest.main()
