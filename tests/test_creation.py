"""Isolated creation/recovery and real role pack validation, without inference."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import unittest
from unittest.mock import patch
import uuid
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.vendor'))
from PySide6.QtGui import QImage, QColor
from starpet import creation, creation_tools, rolepacks
from starpet.storage import atomic_json


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / 'evidence/tests' / ('creation-' + uuid.uuid4().hex[:10])
        self.base.mkdir(parents=True)
        self.input = self.base / 'original.png'
        self.png(self.input)
        self.original_hash = digest(self.input)
        self.outputs = self.base / 'results'
        self.job = creation_tools.CreationJob()

    def png(self, path, color='#f2aa3c', transparent=False):
        image = QImage(256, 256, QImage.Format.Format_ARGB32)
        image.fill(QColor(color))
        if transparent:
            image.setPixelColor(0, 0, QColor(0, 0, 0, 0))
        self.assertTrue(image.save(str(path), 'PNG'))

    def plan(self, **extra):
        return {'image': str(self.input), 'generate': False, 'remove_background': False, **extra}

    def generate(self, image, directory, options, job):
        directory = Path(directory)
        directory.mkdir()
        output = directory / 'generated.png'
        self.png(output, '#31aa88')
        result = {'schema': 1, 'status': 'completed', 'image': str(output), 'prompt_id': 'fixture-job-42',
                  'input_sha256': digest(image), 'output_sha256': digest(output), 'server_may_continue': False}
        report = directory / 'job.json'
        atomic_json(report, result)
        return {**result, 'report': str(report)}

    def background(self, image, output, python, model, job):
        output = Path(output)
        self.png(output, '#eeaa22', transparent=True)
        result = {'schema': 1, 'status': 'completed', 'image': str(output), 'input_sha256': digest(image),
                  'output_sha256': digest(output), 'model_sha256': 'a' * 64, 'model_verified': True,
                  'rgb_preserved': True, 'alpha_min': 0, 'alpha_max': 255}
        report = output.with_suffix('.background.json')
        atomic_json(report, result)
        return {**result, 'report': str(report)}

    def test_static_copy_is_unique_validated_and_never_modifies_original(self):
        progress = []
        with patch.object(creation_tools, 'generate_image') as generate, patch.object(creation_tools, 'remove_background') as background:
            first = creation.prepare_creation(self.plan(), self.outputs, self.job, progress.append)
            second = creation.prepare_creation(self.plan(), self.outputs, self.job)
            generate.assert_not_called()
            background.assert_not_called()
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(first['source_kind'], 'user_image')
        self.assertNotEqual(first['folder'], second['folder'])
        self.assertEqual(digest(first['image']), self.original_hash)
        self.assertEqual(digest(self.input), self.original_hash)
        self.assertEqual(creation.load_creation(first['report']), first)
        self.assertTrue(progress and all(isinstance(message, str) for message in progress))

    def test_all_four_branches_package_via_real_independent_importer(self):
        for generate, background, kind in ((False, False, 'user_image'), (True, False, 'local_img2img'),
                (False, True, 'local_background_removal'), (True, True, 'local_img2img_background_removal')):
            with self.subTest(kind=kind), patch.object(creation_tools, 'generate_image', side_effect=self.generate), \
                    patch.object(creation_tools, 'remove_background', side_effect=self.background):
                result = creation.prepare_creation(self.plan(generate=generate, remove_background=background,
                    checkpoint='fixture.safetensors', prompt='original cat', python_path='fixture-python',
                    background_model='fixture-model'), self.outputs, self.job)
                self.assertEqual(result['status'], 'ready', result)
                self.assertEqual(result['source_kind'], kind)
                package = Path(creation.build_creation_package(result, '测试助手', '测试设定', 'gentle'))
                with zipfile.ZipFile(package) as archive:
                    self.assertEqual(set(archive.namelist()), {'role.json', 'image.png'})
                    manifest = json.loads(archive.read('role.json'))
                    self.assertEqual(manifest['source']['kind'], kind)
                    self.assertEqual(manifest['source']['sha256'], digest(result['image']))
                    self.assertEqual(manifest['personality'], 'gentle')
                    self.assertEqual(manifest['persona'], '测试设定')
                self.assertTrue(creation.load_creation(result['report'])['package']['import_verified'])
                self.assertEqual(list(Path(result['folder']).glob('.package-*')), [])
        self.assertEqual(digest(self.input), self.original_hash)

    def test_generation_defaults_and_stage_order_are_passed_without_private_configuration(self):
        seen = []
        def generate(*args):
            seen.append('generate')
            self.assertEqual(args[2], {'checkpoint': 'fixture', 'prompt': 'cat', 'steps': 16, 'denoise': .45})
            return self.generate(*args)
        def background(*args):
            seen.append('background')
            self.assertEqual(Path(args[0]).name, 'generated.png')
            return self.background(*args)
        with patch.object(creation_tools, 'generate_image', side_effect=generate), \
                patch.object(creation_tools, 'remove_background', side_effect=background):
            result = creation.prepare_creation(self.plan(generate=True, remove_background=True,
                checkpoint='fixture', prompt='cat'), self.outputs, self.job)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(seen, ['generate', 'background'])
        self.assertEqual(result['completed_stages'], ['copy_source', 'generate', 'remove_background'])

    def test_unknown_submission_keeps_job_report_and_never_resubmits_on_resume(self):
        def unknown(image, directory, options, job):
            directory = Path(directory)
            directory.mkdir()
            report = directory / 'job.json'
            atomic_json(report, {'status': 'submission_unknown', 'requested_prompt_id': 'fixture-unknown',
                                 'server_may_continue': True})
            raise creation_tools.CreationToolError('fixture unknown', report=str(report), prompt_id='fixture-unknown')
        with patch.object(creation_tools, 'generate_image', side_effect=unknown) as generate, \
                patch.object(creation_tools, 'remove_background') as background:
            failed = creation.prepare_creation(self.plan(generate=True, remove_background=True), self.outputs, self.job)
            self.assertEqual(generate.call_count, 1)
            background.assert_not_called()
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('image', failed)
        self.assertEqual(failed['generation']['status'], 'submission_unknown')
        self.assertEqual(creation.load_creation(failed['report']), failed)
        old_bytes = Path(failed['report']).read_bytes()
        with patch.object(creation_tools, 'generate_image') as generate:
            forbidden = creation.prepare_creation({'resume_report': failed['report'], 'generate': True}, self.outputs, self.job)
            restored = creation.prepare_creation({'resume_report': failed['report']}, self.outputs, self.job)
            generate.assert_not_called()
        self.assertEqual(forbidden['status'], 'failed')
        self.assertEqual(restored['status'], 'ready', restored)
        self.assertEqual(restored['source_kind'], 'user_image')
        self.assertEqual(restored['generation']['status'], 'submission_unknown')
        self.assertTrue(restored['generation']['inherited'])
        self.assertEqual(Path(failed['report']).read_bytes(), old_bytes)

    def test_background_failure_keeps_completed_generation_and_resume_preserves_origin(self):
        with patch.object(creation_tools, 'generate_image', side_effect=self.generate), \
                patch.object(creation_tools, 'remove_background', side_effect=creation_tools.CreationToolError('fixture failure')):
            failed = creation.prepare_creation(self.plan(generate=True, remove_background=True), self.outputs, self.job)
        self.assertEqual(failed['source_kind'], 'local_img2img')
        self.assertEqual(Path(failed['available_image']).name, 'generated.png')
        self.assertEqual(failed['generation']['status'], 'completed')
        old_image = Path(failed['available_image']).read_bytes()
        with patch.object(creation_tools, 'generate_image') as generate, \
                patch.object(creation_tools, 'remove_background', side_effect=self.background):
            resumed = creation.prepare_creation({'resume_report': failed['report'], 'remove_background': True},
                                                 self.outputs, self.job)
            generate.assert_not_called()
        self.assertEqual(resumed['status'], 'ready', resumed)
        self.assertEqual(resumed['source_kind'], 'local_img2img_background_removal')
        self.assertEqual(Path(failed['available_image']).read_bytes(), old_image)
        self.assertNotEqual(resumed['folder'], failed['folder'])
        self.assertEqual(creation.load_creation(resumed['report']), resumed)

    def test_cancel_after_generation_retains_only_completed_candidate_and_report(self):
        def finish_then_cancel(*args):
            result = self.generate(*args)
            self.job.cancel()
            return result
        with patch.object(creation_tools, 'generate_image', side_effect=finish_then_cancel), \
                patch.object(creation_tools, 'remove_background') as background:
            result = creation.prepare_creation(self.plan(generate=True, remove_background=True), self.outputs, self.job)
            background.assert_not_called()
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['source_kind'], 'local_img2img')
        self.assertNotIn('image', result)
        self.assertEqual(creation.load_creation(result['report']), result)
        package = creation.build_creation_package(result, '保留完成图', '', 'concise')
        self.assertTrue(Path(package).is_file())

    def test_cancel_before_copy_has_no_available_image(self):
        self.job.cancel()
        result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('available_image', result)
        self.assertEqual(creation.load_creation(result['report']), result)
        with self.assertRaises(creation.CreationError):
            creation.build_creation_package(result, '无图片', '', 'playful')

    def test_cancel_during_last_copy_write_does_not_publish_ready(self):
        real_write = creation.atomic_json
        def cancel_on_copy(path, value):
            real_write(path, value)
            if value.get('completed_stages') == ['copy_source']:
                self.job.cancel()
        with patch.object(creation, 'atomic_json', side_effect=cancel_on_copy):
            result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('image', result)
        self.assertEqual(digest(result['available_image']), self.original_hash)
        self.assertEqual(creation.load_creation(result['report']), result)

    def test_recovery_rejects_tampering_escape_running_file_and_hash_mismatch(self):
        result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        path = Path(result['report'])
        original = path.read_bytes()
        mutations = [lambda value: value.update(schema=2),
            lambda value: value.update(status=[]), lambda value: value.update(stage={}),
            lambda value: value.update(folder=str(self.base)), lambda value: value.update(extra='PRIVATE_CONFIG'),
            lambda value: value['artifacts'][0].update(path=str(self.input)),
            lambda value: value['artifacts'][0].update(sha256='0' * 64),
            lambda value: value.update(source_kind='local_img2img'),
            lambda value: value['artifacts'][0].update(bytes=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(result)
                mutate(changed)
                atomic_json(path, changed)
                with self.assertRaises(creation.CreationError):
                    creation.load_creation(path)
        path.write_bytes(original)
        Path(result['image']).write_bytes(b'\x89PNG\r\n\x1a\nINCOMPLETE')
        with self.assertRaises(creation.CreationError):
            creation.load_creation(path)

    def test_recovery_rejects_changed_tool_report_and_linked_resources(self):
        with patch.object(creation_tools, 'generate_image', side_effect=self.generate):
            result = creation.prepare_creation(self.plan(generate=True), self.outputs, self.job)
        tool_report = Path(result['generation']['path'])
        tool_report.write_text('{"status":"queued"}', encoding='utf-8')
        with self.assertRaises(creation.CreationError):
            creation.load_creation(result['report'])
        static = creation.prepare_creation(self.plan(), self.outputs, self.job)
        linked = Path(static['image'])
        original_is_symlink = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == linked or original_is_symlink(path)):
            with self.assertRaisesRegex(creation.CreationError, '链接'):
                creation.load_creation(static['report'])

    def test_tool_cannot_return_an_unowned_image_or_wrong_hash(self):
        def foreign(*args):
            result = self.generate(*args)
            result.update(image=str(self.input), output_sha256=self.original_hash)
            return result
        def mismatch(*args):
            result = self.generate(*args)
            result['output_sha256'] = '0' * 64
            return result
        def wrong_job_report(*args):
            result = self.generate(*args)
            raw = json.loads(Path(result['report']).read_text(encoding='utf-8'))
            raw['input_sha256'] = '0' * 64
            atomic_json(Path(result['report']), raw)
            return result
        for worker in (foreign, mismatch, wrong_job_report):
            with self.subTest(worker=worker), patch.object(creation_tools, 'generate_image', side_effect=worker):
                result = creation.prepare_creation(self.plan(generate=True), self.outputs, self.job)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['source_kind'], 'user_image')
            self.assertEqual(Path(result['available_image']).name, 'input.png')
        self.assertEqual(digest(self.input), self.original_hash)

    def test_local_path_guards_and_actionable_tool_error(self):
        with self.assertRaises(creation.CreationError):
            creation.prepare_creation(self.plan(), Path(r'\\server\share\creation'), self.job)
        if os.name == 'nt':
            with patch('ctypes.windll.kernel32.GetDriveTypeW', return_value=4):
                with self.assertRaisesRegex(creation.CreationError, '网络盘'):
                    creation.prepare_creation(self.plan(), self.outputs, self.job)
        with patch.object(creation_tools, 'generate_image', side_effect=creation_tools.CreationToolError('本机服务未启动，请先启动 ComfyUI。')):
            result = creation.prepare_creation(self.plan(generate=True), self.outputs, self.job)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('请先启动 ComfyUI', result['message'])
        self.assertIn('仍保留', result['message'])

    def test_report_write_failure_never_returns_ready_or_loses_completed_image_in_return(self):
        real_write = creation.atomic_json
        def fail_completion(path, value):
            if Path(path).name == 'creation.json' and value.get('status') in ('ready', 'failed'):
                raise OSError('fixture full disk')
            return real_write(path, value)
        with patch.object(creation, 'atomic_json', side_effect=fail_completion):
            result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('image', result)
        self.assertTrue(Path(result['available_image']).is_file())
        self.assertIn('未能更新', result['message'])
        previous = Path(result['report']).read_bytes()
        recovered = creation.load_creation(result['report'])
        self.assertEqual(recovered['status'], 'cancelled')
        self.assertEqual(digest(recovered['available_image']), self.original_hash)
        self.assertEqual(Path(result['report']).read_bytes(), previous)

    def test_packaging_uses_disk_report_and_rejects_invalid_role_fields(self):
        result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        forged = {**result, 'image': str(self.base / 'outside.png'), 'source_kind': 'local_img2img'}
        first = Path(creation.build_creation_package(forged, '助手一', '', 'playful'))
        second = Path(creation.build_creation_package(result, '助手二', '', 'concise'))
        self.assertNotEqual(first, second)
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(json.loads(archive.read('role.json'))['source']['kind'], 'user_image')
        for name, persona, style in (('', '', 'playful'), ('角色', '长' * 2001, 'playful'), ('角色', '', 'invalid')):
            with self.subTest(name=name, style=style), self.assertRaises(rolepacks.RolePackError):
                creation.build_creation_package(result, name, persona, style)
        self.assertEqual(list(Path(result['folder']).glob('.package-*')), [])
        self.assertEqual(len(list(Path(result['folder']).glob('*.aipet'))), 2)

    def test_deep_output_folder_uses_short_temporary_package_libraries(self):
        deep = self.base / ('nested-' + 'x' * 65)
        result = creation.prepare_creation(self.plan(), deep, self.job)
        self.assertEqual(result['status'], 'ready', result)
        roots = []
        library_class = rolepacks.RoleLibrary
        def independent(state, *args, **kwargs):
            roots.append(Path(state))
            return library_class(state, *args, **kwargs)
        with patch.object(rolepacks, 'RoleLibrary', side_effect=independent):
            package = Path(creation.build_creation_package(result, '深目录测试', '', 'gentle'))
        self.assertTrue(package.is_file())
        self.assertEqual(package.parent, Path(result['folder']))
        self.assertEqual(len(roots), 2)
        for root in roots:
            self.assertRegex(root.parent.name, r'^ada-[a-f0-9]{12}$')
            self.assertLess(len(str(root)), 140)
            self.assertFalse(root.parent.exists())

    def test_failed_package_report_write_keeps_old_report_and_no_unreported_package(self):
        result = creation.prepare_creation(self.plan(), self.outputs, self.job)
        before = Path(result['report']).read_bytes()
        real_write = creation.atomic_json
        def fail_delivery(path, value):
            if Path(path).name == 'creation.json' and 'package' in value:
                raise OSError('fixture report write failure')
            return real_write(path, value)
        with patch.object(creation, 'atomic_json', side_effect=fail_delivery), self.assertRaises(OSError):
            creation.build_creation_package(result, '尚未交付', '', 'playful')
        self.assertEqual(Path(result['report']).read_bytes(), before)
        self.assertEqual(list(Path(result['folder']).glob('*.aipet')), [])

    def test_daemon_exit_recovers_recorded_input_without_accepting_unrecorded_output(self):
        child_outputs = self.base / 'child-results'
        code = f'''
import shutil,sys,threading
from pathlib import Path
sys.path[:0]=[{str(ROOT)!r},{str(ROOT / '.vendor')!r}]
from starpet import creation,creation_tools
ready=threading.Event()
def blocked(image,directory,options,job):
    directory=Path(directory);directory.mkdir()
    shutil.copyfile(image,directory/'unrecorded-output.png')
    ready.set();threading.Event().wait(60)
creation_tools.generate_image=blocked
job=creation_tools.CreationJob()
thread=threading.Thread(target=lambda:creation.prepare_creation(
    {{'image':{str(self.input)!r},'generate':True}},Path({str(child_outputs)!r}),job),daemon=True)
thread.start()
assert ready.wait(5)
job.cancel()
'''
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), shell=False)
        self.assertEqual(child.returncode, 0, child.stderr.decode('utf-8', 'replace'))
        source = next(child_outputs.glob('creation-*/creation.json'))
        original = source.read_bytes()
        self.assertEqual(json.loads(original)['status'], 'running')
        with patch.object(creation_tools, 'generate_image') as generate, \
                patch.object(creation_tools, 'remove_background') as background:
            recovered = creation.load_creation(source)
            generate.assert_not_called(); background.assert_not_called()
        self.assertEqual(recovered['status'], 'cancelled')
        self.assertNotEqual(recovered['folder'], str(source.parent))
        self.assertEqual(recovered['source_kind'], 'user_image')
        self.assertEqual(recovered['completed_stages'], ['copy_source'])
        self.assertEqual(digest(recovered['available_image']), self.original_hash)
        self.assertTrue(recovered['interruption']['submission_unknown'])
        self.assertEqual(recovered['generation'], {'status': 'unavailable', 'server_may_continue': True})
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((Path(recovered['folder']) / 'generation/unrecorded-output.png').exists())
        self.assertEqual(creation.load_creation(recovered['report']), recovered)
        self.assertTrue(Path(creation.build_creation_package(recovered, '恢复原图', '', 'gentle')).is_file())

    def test_active_worker_is_not_modified_or_cancelled_by_loading_running_snapshot(self):
        ready, release = threading.Event(), threading.Event()
        results = []
        def waiting_generate(*args):
            ready.set()
            self.assertTrue(release.wait(10))
            return self.generate(*args)
        with patch.object(creation_tools, 'generate_image', side_effect=waiting_generate):
            worker = threading.Thread(target=lambda: results.append(creation.prepare_creation(
                self.plan(generate=True), self.outputs, self.job)))
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                source = next(self.outputs.glob('creation-*/creation.json'))
                original = source.read_bytes()
                recovered = creation.load_creation(source)
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(self.job.cancelled.is_set())
                self.assertTrue(worker.is_alive())
                copy_before = Path(recovered['report']).read_bytes()
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'ready', results)
        self.assertEqual(Path(recovered['report']).read_bytes(), copy_before)
        self.assertEqual(digest(recovered['available_image']), self.original_hash)
        self.assertNotEqual(digest(results[0]['image']), self.original_hash)

    def test_running_snapshot_preserves_completed_generated_origin_and_rejects_unrecorded_start(self):
        with patch.object(creation_tools, 'generate_image', side_effect=self.generate), \
                patch.object(creation_tools, 'remove_background', side_effect=creation_tools.CreationToolError('fixture stop')):
            result = creation.prepare_creation(self.plan(generate=True, remove_background=True), self.outputs, self.job)
        running = copy.deepcopy(result)
        running['status'] = 'running'
        atomic_json(Path(result['report']), running)
        recovered = creation.load_creation(result['report'])
        self.assertEqual(recovered['source_kind'], 'local_img2img')
        self.assertEqual(digest(recovered['available_image']), digest(result['available_image']))
        self.assertEqual(recovered['generation']['status'], 'completed')
        self.assertTrue(recovered['generation']['inherited'])
        self.assertFalse(recovered['interruption']['submission_unknown'])
        with patch.object(creation_tools, 'generate_image') as generate, \
                patch.object(creation_tools, 'remove_background', side_effect=self.background):
            continued = creation.prepare_creation({'resume_report': recovered['report'], 'remove_background': True},
                                                  self.outputs, creation_tools.CreationJob())
            generate.assert_not_called()
        self.assertEqual(continued['status'], 'ready', continued)
        self.assertEqual(continued['source_kind'], 'local_img2img_background_removal')
        empty = copy.deepcopy(result)
        empty.update(status='running', source_kind='user_image', artifacts=[], completed_stages=[])
        empty.pop('available_image'); empty.pop('available_sha256')
        empty.pop('generation'); empty.pop('background_removal')
        atomic_json(Path(result['report']), empty)
        before = set(self.outputs.iterdir())
        with self.assertRaisesRegex(creation.CreationError, '尚未记录'):
            creation.load_creation(result['report'])
        self.assertEqual(set(self.outputs.iterdir()), before)


if __name__ == '__main__':
    unittest.main()
