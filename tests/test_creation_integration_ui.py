"""The creation wizard never commits a role after cancellation or closure."""
import copy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture, QApplication
from starpet.creation import prepare_creation
from starpet.creation_tools import CreationJob

ROOT = Path(__file__).resolve().parents[1]


class CreationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.workshop = self.f.window.workshop
        self.control = self.workshop.creation
        self.original = self.f.c.roles.spec('tabby')['image']
        self.settings = copy.deepcopy(self.f.store.settings)
        self.memory = self.f.saved_memory()

    def tearDown(self):
        self.f.close()

    def open(self):
        self.workshop.import_image()
        self.dialog = self.control.dialog
        self.assertIsNotNone(self.dialog)
        self.assertTrue(self.dialog.set_image(self.original))
        self.dialog.name.setText('隔离制作伙伴')
        return self.dialog

    def wait(self):
        self.f.wait_for(lambda: self.control.job is None, timeout=8)

    def unchanged(self):
        self.assertEqual(self.f.store.settings, self.settings)
        self.assertEqual(self.f.saved_memory(), self.memory)
        self.assertEqual(self.f.chat.calls, [])

    def test_original_to_review_to_explicit_import_without_activation(self):
        before = set(row['id'] for row in self.f.c.roles.list_roles())
        source = Path(self.original).read_bytes()
        dialog = self.open()
        dialog.prepare_button.click()
        self.wait()
        self.assertEqual(self.control.result['status'], 'ready')
        self.assertEqual(set(row['id'] for row in self.f.c.roles.list_roles()), before)
        dialog.persona.setPlainText('温柔、认真理解用户的本机伙伴。')
        dialog.personality.setCurrentIndex(dialog.personality.findData('gentle'))
        dialog.create_button.click()
        self.wait()
        after = set(row['id'] for row in self.f.c.roles.list_roles())
        self.assertEqual(len(after - before), 1, dialog.status_label.text())
        identity = next(iter(after - before))
        self.assertEqual(self.f.c.roles.profile(identity)['personality'], 'gentle')
        self.assertEqual(Path(self.f.c.roles.spec(identity)['image']).read_bytes(), source)
        self.assertEqual(Path(self.original).read_bytes(), source)
        self.assertIsNone(self.control.dialog)
        self.assertEqual(self.workshop.selected_id(), identity)
        self.unchanged()

    def test_close_during_prepare_discards_late_success_but_keeps_files(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        reports = []
        def delayed(plan, base, job, progress):
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError('fixture timeout')
                # Simulate a worker that had already completed before seeing
                # cancellation. The controller must still discard its reply.
                result = prepare_creation(plan, base, CreationJob())
                reports.append(result)
                return result
            finally:
                finished.set()
        before = self.f.c.roles.list_roles()
        with patch('starpet.creation_controller.prepare_creation', side_effect=delayed):
            self.open().prepare_button.click()
            self.assertTrue(entered.wait(2))
            job = self.control.job
            self.f.window.hide()
            self.assertTrue(job.cancelled.is_set())
            self.assertIsNone(self.control.dialog)
            release.set()
            self.assertTrue(finished.wait(4))
            self.control.poll()
        self.assertEqual(self.f.c.roles.list_roles(), before)
        self.assertTrue(Path(reports[0]['report']).is_file())
        self.unchanged()

    def test_stop_during_package_does_not_import_a_late_package(self):
        from starpet.creation import build_creation_package
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            try:
                if not release.wait(3):raise RuntimeError('fixture timeout')
                return build_creation_package(*args)
            finally:
                finished.set()
        before = self.f.c.roles.list_roles()
        dialog = self.open()
        dialog.prepare_button.click(); self.wait()
        with patch('starpet.creation_controller.build_creation_package', side_effect=delayed):
            dialog.create_button.click()
            self.assertTrue(entered.wait(2))
            dialog.stop_button.click()
            release.set()
            self.assertTrue(finished.wait(4))
            self.wait()
        self.assertEqual(self.f.c.roles.list_roles(), before)
        self.unchanged()

    def test_modified_processing_options_block_old_result(self):
        before = self.f.c.roles.list_roles()
        dialog = self.open()
        dialog.prepare_button.click(); self.wait()
        dialog.remove_background.setChecked(True)
        self.assertFalse(dialog.create_button.isEnabled())
        self.control.create('不能导入', '', 'gentle')
        self.assertEqual(self.f.c.roles.list_roles(), before)
        self.assertIsNone(self.control.job)

    def test_reload_and_resume_never_call_generation(self):
        report = prepare_creation({'image': str(self.original)}, self.f.folder / 'prior', CreationJob())
        dialog = self.open()
        with patch('starpet.creation_tools.generate_image', side_effect=AssertionError('must not generate')):
            self.control.load(report['report']); self.wait()
            dialog.resume_button.click()
            dialog.prepare_button.click(); self.wait()
        self.assertEqual(self.control.result['status'], 'ready')
        self.assertNotEqual(self.control.result['folder'], report['folder'])
        self.assertEqual(self.control.result['source_kind'], 'user_image')
        self.unchanged()

    def test_application_stop_cancels_creation(self):
        dialog = self.open()
        job = self.control.job = CreationJob()
        dialog.set_busy(True)
        self.f.c.stop_chat()
        self.assertTrue(job.cancelled.is_set())
        self.assertIsNone(self.control.dialog)

    def test_read_interrupted_report_creates_reviewable_copy(self):
        report = prepare_creation({'image': str(self.original)}, self.f.folder / 'prior', CreationJob())
        path = Path(report['report'])
        report.update(status='running', stage='generate', message='等待期间程序退出')
        report.pop('image'); report.pop('sha256')
        path.write_text(json.dumps(report), encoding='utf-8')
        before = path.read_bytes()
        dialog = self.open()
        self.control.load(str(path)); self.wait()
        self.assertEqual(self.control.result['status'], 'cancelled')
        self.assertNotEqual(self.control.result['report'], str(path))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(dialog.create_button.isEnabled())
        self.assertEqual(dialog.create_button.text(), '用当前结果创建角色')
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
