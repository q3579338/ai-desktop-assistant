"""Offscreen creation-dialog review with synthetic PNGs and callback spies."""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from starpet.creation_gui import CharacterCreationDialog
from starpet.gui import STYLE, initialize_fonts


class CreationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Qt offscreen is required; no foreground fallback')
        initialize_fonts(cls.app)
        cls.folder = ROOT / 'evidence' / 'tests' / ('creation-dialog-' + uuid.uuid4().hex)
        cls.folder.mkdir(parents=True)
        cls.original = cls.folder / 'original.png'
        cls.output = cls.folder / 'output.png'
        for path, color in ((cls.original, '#AAE6D18C'), (cls.output, '#77F5A57A')):
            pixmap = QPixmap(80, 120)
            pixmap.fill(QColor(color))
            assert pixmap.save(str(path), 'PNG')
        cls.original_hash = hashlib.sha256(cls.original.read_bytes()).hexdigest()
        cls.output_hash = hashlib.sha256(cls.output.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        assert hashlib.sha256(cls.original.read_bytes()).hexdigest() == cls.original_hash
        assert hashlib.sha256(cls.output.read_bytes()).hexdigest() == cls.output_hash
        target = cls.folder.resolve()
        if target.is_relative_to((ROOT / 'evidence' / 'tests').resolve()):
            shutil.rmtree(target)

    def setUp(self):
        self.calls = {key: [] for key in ('check', 'prepare', 'create', 'cancel', 'load')}
        self.parent = QWidget()
        self.parent.setStyleSheet(STYLE)
        self.dialog = CharacterCreationDialog(self.parent,
            lambda value: self.calls['check'].append(value),
            lambda value: self.calls['prepare'].append(value),
            lambda *values: self.calls['create'].append(values),
            lambda: self.calls['cancel'].append(True),
            lambda value: self.calls['load'].append(value),
            {'url': 'http://127.0.0.1:8188', 'python_path': 'D:/isolated/python.exe',
             'background_model': 'D:/isolated/u2netp.onnx'})

    def tearDown(self):
        self.dialog.reject()
        self.dialog.deleteLater()
        self.parent.deleteLater()
        self.app.processEvents()

    def report(self, status='ready', image=True):
        value = {'status': status, 'report': str(self.folder / 'creation.json'),
                 'message': '这是已校验的隔离测试报告。',
                 'artifacts': [{'stage': 'copy_source', 'path': str(self.original)}]}
        if image:
            value['image' if status == 'ready' else 'available_image'] = str(self.output)
        return value

    def ready(self):
        self.assertTrue(self.dialog.set_image(str(self.original)))
        self.dialog.name.setText('隔离伙伴')
        self.assertTrue(self.dialog.show_result(self.report()))
        self.assertTrue(self.dialog.create_button.isEnabled())

    def test_open_is_nonblocking_modal_and_has_no_implicit_calls(self):
        self.dialog.open()
        self.app.processEvents()
        self.assertEqual(self.dialog.windowModality(), Qt.WindowModality.WindowModal)
        self.assertFalse(self.dialog.generate.isChecked())
        self.assertFalse(self.dialog.remove_background.isChecked())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertEqual(self.dialog.url.text(), 'http://127.0.0.1:8188')
        self.assertTrue(all(not calls for calls in self.calls.values()))

    def test_original_preparation_only_passes_explicit_plan(self):
        self.assertTrue(self.dialog.set_image(str(self.original)))
        self.assertFalse(self.dialog.original_preview.pixmap.isNull())
        self.dialog.prepare_button.click()
        self.assertEqual(len(self.calls['prepare']), 1)
        plan = self.calls['prepare'][0]
        self.assertEqual(set(plan), {'image', 'generate', 'remove_background', 'url',
            'checkpoint', 'prompt', 'negative', 'steps', 'denoise', 'python_path', 'background_model'})
        self.assertEqual(plan['image'], str(self.original.resolve()))
        self.assertFalse(plan['generate'])
        self.assertFalse(plan['remove_background'])
        self.assertEqual(self.calls['create'], [])
        self.assertEqual(self.calls['check'], [])

    def test_generation_validation_and_model_check_are_separate_callbacks(self):
        self.dialog.set_image(str(self.original))
        self.dialog.generate.setChecked(True)
        self.dialog.prepare_button.click()
        self.assertEqual(self.calls['prepare'], [])
        self.assertIn('重绘描述', self.dialog.status_label.text())
        self.dialog.check_button.click()
        self.assertEqual(self.calls['check'], ['http://127.0.0.1:8188'])
        self.assertTrue(self.dialog.show_models(['sdxl.safetensors', 'sdxl.safetensors']))
        self.assertEqual(self.dialog.checkpoint.count(), 1)
        self.dialog.prompt.setPlainText('保留角色，调整为手绘风格')
        self.dialog.negative.setPlainText('模糊')
        self.dialog.steps.setValue(22)
        self.dialog.denoise.setValue(.6)
        self.dialog.prepare_button.click()
        plan = self.calls['prepare'][0]
        self.assertTrue(plan['generate'])
        self.assertEqual(plan['checkpoint'], 'sdxl.safetensors')
        self.assertEqual(plan['steps'], 22)
        self.assertEqual(plan['denoise'], .6)
        self.assertEqual(plan['negative'], '模糊')

    def test_removal_requires_paths_but_does_not_probe_or_open_them(self):
        self.dialog.set_image(str(self.original))
        self.dialog.remove_background.setChecked(True)
        self.dialog.background_model.clear()
        self.dialog.prepare_button.click()
        self.assertEqual(self.calls['prepare'], [])
        self.assertIn('U2NetP', self.dialog.status_label.text())
        self.dialog.background_model.setText('D:/does-not-exist/u2netp.onnx')
        self.dialog.prepare_button.click()
        self.assertTrue(self.calls['prepare'][0]['remove_background'])
        self.assertEqual(self.calls['check'], [])

    def test_ready_result_never_creates_until_explicit_confirmation(self):
        self.ready()
        self.assertEqual(self.calls['create'], [])
        self.dialog.persona.setPlainText('温和、清楚，认真陪伴。')
        self.dialog.personality.setCurrentIndex(self.dialog.personality.findData('gentle'))
        self.dialog.create_button.click()
        self.assertEqual(self.calls['create'], [('隔离伙伴', '温和、清楚，认真陪伴。', 'gentle')])
        self.assertEqual(self.calls['prepare'], [])

    def test_profile_changes_do_not_invalidate_prepared_image(self):
        self.ready()
        self.dialog.name.setText('另一个名字')
        self.dialog.persona.setPlainText('新角色设定')
        self.dialog.personality.setCurrentIndex(2)
        self.assertTrue(self.dialog.create_button.isEnabled())
        self.assertIsNotNone(self.dialog.result)
        self.assertEqual(self.calls['prepare'], [])

    def test_each_image_processing_change_invalidates_result_but_keeps_report(self):
        self.ready()
        changes = [lambda: self.dialog.generate.setChecked(True),
            lambda: self.dialog.remove_background.setChecked(True),
            lambda: self.dialog.url.setText('http://127.0.0.1:8189'),
            lambda: self.dialog.show_models(['another.safetensors']),
            lambda: self.dialog.prompt.setPlainText('变化'),
            lambda: self.dialog.negative.setPlainText('模糊'),
            lambda: self.dialog.steps.setValue(17),
            lambda: self.dialog.denoise.setValue(.5),
            lambda: self.dialog.python_path.setText('D:/other/python.exe'),
            lambda: self.dialog.background_model.setText('D:/other/u2netp.onnx'),
            lambda: self.dialog.set_image(str(self.output))]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.dialog.show_result(self.report())
                self.assertTrue(self.dialog.create_button.isEnabled())
                change()
                self.assertFalse(self.dialog.create_button.isEnabled())
                self.assertIn('重新制作', self.dialog.status_label.text())
                self.assertIsNotNone(self.dialog.result)
                self.dialog.create_button.click()
        self.assertEqual(self.calls['create'], [])

    def test_model_refresh_and_folding_do_not_stale_unchanged_image(self):
        self.dialog.show_models(['a.safetensors', 'b.safetensors'])
        self.ready()
        self.dialog.show_models(['a.safetensors', 'b.safetensors'])
        self.dialog.advanced_button.setChecked(True)
        self.dialog.advanced_button.setChecked(False)
        self.assertTrue(self.dialog.create_button.isEnabled())

    def test_failed_and_cancelled_results_explain_partial_image(self):
        self.dialog.name.setText('候选角色')
        for status in ('failed', 'cancelled'):
            with self.subTest(status=status):
                report = self.report(status)
                self.assertTrue(self.dialog.show_result(report))
                self.assertIn('上一步已完成', self.dialog.result_note.text())
                self.assertEqual(self.dialog.create_button.text(), '用当前结果创建角色')
                self.assertTrue(self.dialog.create_button.isEnabled())
                self.assertEqual(self.calls['create'], [])
                report['available_image'] = 'mutated after display'
                self.assertEqual(self.dialog.result['available_image'], str(self.output))

    def test_resume_uses_report_path_disables_generation_and_keeps_original(self):
        self.ready()
        self.dialog.generate.setChecked(True)
        self.dialog.remove_background.setChecked(True)
        self.dialog.show_result(self.report('failed'))
        original_key = self.dialog.original_preview.pixmap.cacheKey()
        self.dialog.resume_button.click()
        self.assertEqual(self.dialog.image_path.text(), str(self.output.resolve()))
        self.assertFalse(self.dialog.generate.isChecked())
        self.assertTrue(self.dialog.remove_background.isChecked())
        self.assertEqual(self.dialog.original_preview.pixmap.cacheKey(), original_key)
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertEqual(self.calls['prepare'], [])
        self.dialog.prepare_button.click()
        self.assertEqual(self.calls['prepare'][0]['resume_report'], str(self.folder / 'creation.json'))
        self.assertFalse(self.calls['prepare'][0]['generate'])

    def test_new_original_clears_resume_reference(self):
        self.ready()
        self.dialog.resume_button.click()
        self.dialog.set_image(str(self.original))
        self.dialog.prepare_button.click()
        self.assertNotIn('resume_report', self.calls['prepare'][0])

    def test_resuming_cannot_accidentally_submit_generation_again(self):
        self.ready()
        self.dialog.resume_button.click()
        self.dialog.generate.setChecked(True)
        self.dialog.prepare_button.click()
        self.assertEqual(self.calls['prepare'], [])
        self.assertIn('不重复重绘', self.dialog.status_label.text())

    def test_report_without_usable_image_or_terminal_status_cannot_create(self):
        self.dialog.name.setText('不可创建')
        for report in (self.report('failed', False), self.report('cancelled', False),
                self.report('ready', False), self.report('running'), {'status': 'ready', 'image': 'missing.png'}):
            with self.subTest(report=report):
                self.assertFalse(self.dialog.show_result(report))
                self.assertFalse(self.dialog.create_button.isEnabled())
                self.assertFalse(self.dialog.resume_button.isEnabled())
        self.assertEqual(self.calls['create'], [])

    def test_clear_result_and_errors_keep_user_edits(self):
        self.ready()
        self.dialog.persona.setPlainText('草稿仍然保留')
        self.dialog.show_error('<b>这只是错误文字</b>')
        self.assertEqual(self.dialog.status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(self.dialog.create_button.isEnabled())
        self.dialog.clear_result()
        self.assertIsNone(self.dialog.result)
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertEqual(self.dialog.name.text(), '隔离伙伴')
        self.assertEqual(self.dialog.persona.toPlainText(), '草稿仍然保留')

    def test_name_and_persona_limits_gate_creation(self):
        self.ready()
        self.dialog.name.setText('名' * 30)
        self.assertEqual(len(self.dialog.name.text()), 24)
        self.dialog.persona.setPlainText('设' * 2001)
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertIn('请缩短', self.dialog.persona_count.text())
        self.dialog.persona.setPlainText('设' * 2000)
        self.assertTrue(self.dialog.create_button.isEnabled())
        self.dialog.name.setText('   ')
        self.assertFalse(self.dialog.create_button.isEnabled())

    def test_busy_controls_and_stop_close_cancel_only_once(self):
        self.ready()
        self.dialog.set_busy(True)
        for control in (self.dialog.prepare_button, self.dialog.create_button,
                self.dialog.choose_button, self.dialog.load_button, self.dialog.name, self.dialog.persona):
            self.assertFalse(control.isEnabled())
        self.assertTrue(self.dialog.stop_button.isEnabled())
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.dialog.stop_button.click()
        self.dialog.stop_button.click()
        self.dialog.reject()
        self.assertEqual(self.calls['cancel'], [True])
        self.assertIsNone(self.dialog.result)
        self.assertFalse(self.dialog.show_result(self.report()))
        self.dialog._prepare()
        self.dialog._create()
        self.assertEqual(self.calls['prepare'], [])
        self.assertEqual(self.calls['create'], [])

    def test_close_while_busy_cancels_without_creating_and_idle_close_does_not(self):
        self.ready()
        self.dialog.set_busy(True)
        self.dialog.reject()
        self.assertEqual(self.calls['cancel'], [True])
        self.assertEqual(self.calls['create'], [])

    def test_destroyed_dialog_ignores_late_results_and_callbacks(self):
        extra = CharacterCreationDialog(self.parent,
            lambda value: self.calls['check'].append(value),
            lambda value: self.calls['prepare'].append(value),
            lambda *values: self.calls['create'].append(values),
            lambda: self.calls['cancel'].append(True),
            lambda value: self.calls['load'].append(value))
        extra.set_image(str(self.original))
        extra.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(extra.show_result(self.report()))
        extra._prepare()
        extra._create()
        extra._check()
        extra._cancel()
        self.assertTrue(all(not values for values in self.calls.values()))

    def test_file_picker_callbacks_are_explicit_and_do_not_read_report(self):
        nonexistent_report = str(self.folder / 'not-written.json')
        with patch('starpet.creation_gui.QFileDialog.getOpenFileName', return_value=(nonexistent_report, '')):
            self.dialog.load_button.click()
        self.assertEqual(self.calls['load'], [nonexistent_report])
        self.assertFalse(Path(nonexistent_report).exists())
        with patch('starpet.creation_gui.QFileDialog.getOpenFileName', return_value=(str(self.original), '')):
            self.dialog.choose_button.click()
        self.assertEqual(self.dialog.image_path.text(), str(self.original.resolve()))
        self.assertEqual(self.calls['prepare'], [])

    def test_invalid_input_keeps_previous_source_untouched(self):
        self.dialog.set_image(str(self.original))
        key = self.dialog.original_preview.pixmap.cacheKey()
        self.assertFalse(self.dialog.set_image(str(self.folder / 'missing.png')))
        self.assertEqual(self.dialog.image_path.text(), str(self.original.resolve()))
        self.assertEqual(self.dialog.original_preview.pixmap.cacheKey(), key)

    def test_loaded_report_can_show_preserved_source_without_preparing(self):
        self.dialog.show_result(self.report())
        self.assertFalse(self.dialog.original_preview.pixmap.isNull())
        self.assertIn('恢复', self.dialog.original_note.text())
        self.assertEqual(self.calls['prepare'], [])

    def test_loading_another_report_aligns_next_input_and_discards_old_resume(self):
        self.ready()
        before = copy.deepcopy(self.dialog._plan())
        other = self.report()
        other['artifacts'][0]['path'] = str(self.output)
        other['resumed_from'] = {'report': str(self.folder / 'previous.json')}
        self.dialog.show_result(other)
        self.assertEqual(self.dialog._original_image, str(self.output.resolve()))
        self.assertIn('输入副本', self.dialog.original_note.text())
        self.assertEqual(self.dialog.image_path.text(), str(self.output.resolve()))
        after = self.dialog._plan()
        self.assertNotIn('resume_report', after)
        self.assertEqual({key: value for key, value in before.items() if key not in ('image', 'resume_report')},
                         {key: value for key, value in after.items() if key != 'image'})
        self.assertTrue(all(not values for values in self.calls.values()))
        self.dialog.prepare_button.click()
        self.assertEqual(self.calls['prepare'][0]['image'], str(self.output.resolve()))
        self.assertNotIn('resume_report', self.calls['prepare'][0])

    def test_loading_report_clears_prior_resume_context(self):
        self.ready()
        self.dialog.resume_button.click()
        self.assertIn('resume_report', self.dialog._plan())
        self.dialog.show_result(self.report())
        self.assertNotIn('resume_report', self.dialog._plan())
        self.assertEqual(self.dialog.image_path.text(), str(self.original.resolve()))

    def test_ready_and_partial_results_scroll_preview_into_view_after_layout(self):
        self.dialog.set_image(str(self.original))
        self.dialog.name.setText('检查预览')
        self.dialog.generate.setChecked(True)
        self.dialog.advanced_button.setChecked(True)
        self.dialog.remove_background.setChecked(True)
        self.dialog.resize(360, 540)
        self.dialog.open()
        for status in ('ready', 'failed'):
            with self.subTest(status=status):
                self.dialog.scroll.verticalScrollBar().setValue(0)
                self.dialog.show_result(self.report(status))
                QTest.qWait(25)
                viewport = self.dialog.scroll.viewport()
                rectangle = self.dialog.result_preview.rect().translated(
                    self.dialog.result_preview.mapTo(viewport, QPoint(0, 0)))
                self.assertTrue(viewport.rect().contains(rectangle))
                self.assertEqual(self.calls['create'], [])
                self.assertEqual(self.calls['prepare'], [])

    def test_result_loaded_before_open_is_revealed_without_implicit_creation(self):
        self.ready()
        self.dialog.resize(360, 540)
        self.dialog.open()
        QTest.qWait(25)
        viewport = self.dialog.scroll.viewport()
        rectangle = self.dialog.result_preview.rect().translated(
            self.dialog.result_preview.mapTo(viewport, QPoint(0, 0)))
        self.assertTrue(viewport.rect().contains(rectangle))
        self.assertEqual(self.calls['create'], [])

    def test_small_and_regular_windows_keep_status_actions_and_labels_readable(self):
        self.ready()
        self.dialog.generate.setChecked(True)
        self.dialog.advanced_button.setChecked(True)
        self.dialog.remove_background.setChecked(True)
        self.dialog.show_result(self.report('failed'))
        self.dialog.show_status('上一步图片已经完成，本次去背景没有完成。可以用当前结果创建角色，或从已有结果继续。')
        self.dialog.open()
        for width, height in ((360, 540), (600, 780)):
            self.dialog.resize(width, height)
            QTest.qWait(20)
            for bottom in (False, True):
                with self.subTest(size=(width, height), bottom=bottom):
                    bar = self.dialog.scroll.verticalScrollBar()
                    bar.setValue(bar.maximum() if bottom else 0)
                    self.app.processEvents()
                    self.assertEqual((self.dialog.width(), self.dialog.height()), (width, height))
                    self.assertEqual(self.dialog.scroll.horizontalScrollBar().maximum(), 0)
                    for control in (self.dialog.status_label, self.dialog.prepare_button,
                            self.dialog.stop_button, self.dialog.close_button, self.dialog.create_button):
                        box = control.rect().translated(control.mapTo(self.dialog, QPoint(0, 0)))
                        self.assertTrue(self.dialog.rect().contains(box))
                    for label in self.dialog.findChildren(QLabel):
                        if label.isVisible():
                            required = label.heightForWidth(label.contentsRect().width()) if label.hasHeightForWidth() else label.sizeHint().height()
                            self.assertGreaterEqual(label.height(), required, label.text())
                    for button in self.dialog.findChildren(QPushButton):
                        if button.isVisible():
                            self.assertGreaterEqual(button.width(), button.sizeHint().width(), button.text())

    def test_editing_enter_never_implicitly_confirms(self):
        self.ready()
        self.dialog.open()
        self.dialog.name.setFocus()
        QTest.keyClick(self.dialog.name, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.calls['create'], [])
        self.assertEqual(self.calls['prepare'], [])
        for button in self.dialog.findChildren(QPushButton):
            self.assertFalse(button.isDefault())
            self.assertFalse(button.autoDefault())


if __name__ == '__main__':
    unittest.main()
