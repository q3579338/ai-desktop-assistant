"""Isolated offscreen memory review; no model, account or filesystem state."""
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication, QWidget
from starpet.memory_gui import MemoryReviewDialog


ROWS = [
    {'text': '喜欢先听结论。', 'sourceId': 'turn-1', 'quote': '我希望你先告诉我结论。'},
    {'text': '喜欢喝无糖茶。', 'sourceId': 'turn-2', 'quote': '平时我最喜欢无糖茶。'},
]


class MemoryReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('This test requires offscreen; no foreground fallback')

    def setUp(self):
        self.store = SimpleNamespace(settings={'name': '隔离角色', 'provider': 'codex', 'saveHistory': False},
                                     facts=[{'text': '已有事实'}], turns=[{'user': '不可由窗口读取的聊天'}])
        self.before = copy.deepcopy(vars(self.store))
        self.calls = {'generate': [], 'cancel': [], 'save': []}
        self.parent = QWidget()
        self.dialog = MemoryReviewDialog(self.parent, self.store,
            lambda rounds: self.calls['generate'].append(rounds),
            lambda: self.calls['cancel'].append(True),
            lambda texts: self.calls['save'].append(texts))

    def tearDown(self):
        self.dialog.reject()
        self.dialog.deleteLater()
        self.parent.deleteLater()
        self.app.processEvents()

    def rows(self):
        self.assertTrue(self.dialog.show_candidates(copy.deepcopy(ROWS)))

    def check(self, index):
        self.dialog.candidate_list.item(index).setCheckState(Qt.CheckState.Checked)

    def test_open_is_nonblocking_window_modal_and_does_not_generate(self):
        self.dialog.open()
        self.app.processEvents()
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.windowModality(), Qt.WindowModality.WindowModal)
        self.assertEqual(self.dialog.rounds.value(), 8)
        self.assertEqual((self.dialog.rounds.minimum(), self.dialog.rounds.maximum()), (1, 24))
        self.assertIn('不发送助手回复或已有记忆', self.dialog.scope_note.text())
        self.assertIn('临时聊天', self.dialog.scope_note.text())
        self.assertIn('Codex', self.dialog.scope_note.text())
        self.assertEqual(self.calls, {'generate': [], 'cancel': [], 'save': []})
        self.assertEqual(vars(self.store), self.before)

    def test_generation_is_explicit_and_passes_only_rounds(self):
        self.dialog.rounds.setValue(3)
        self.dialog.generate_button.click()
        self.assertEqual(self.calls['generate'], [3])
        self.assertEqual(self.calls['save'], [])
        self.assertEqual(vars(self.store), self.before)

    def test_candidates_default_unchecked_no_implicit_save(self):
        self.rows()
        self.assertEqual(self.dialog.candidate_list.count(), 2)
        for index in range(2):
            self.assertEqual(self.dialog.candidate_list.item(index).checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.dialog._save()
        self.assertEqual(self.calls['save'], [])

    def test_edited_text_survives_selection_changes_and_only_checked_saved(self):
        self.rows()
        self.dialog.editor.setPlainText('  请先给结论，再说明原因。  ')
        self.check(0)
        self.dialog.candidate_list.setCurrentRow(1)
        self.dialog.editor.setPlainText('不选中的修改不应该保存。')
        self.dialog.candidate_list.setCurrentRow(0)
        self.assertEqual(self.dialog.editor.toPlainText(), '  请先给结论，再说明原因。  ')
        self.assertEqual(self.dialog.source_quote.toPlainText(), ROWS[0]['quote'])
        self.assertTrue(self.dialog.source_quote.isReadOnly())
        self.dialog.save_button.click()
        self.assertEqual(self.calls['save'], [['请先给结论，再说明原因。']])
        self.assertEqual(vars(self.store), self.before)

    def test_all_selected_edits_submitted_without_silent_truncation(self):
        self.rows()
        self.check(0)
        self.check(1)
        self.dialog.editor.setPlainText('字' * 300)
        self.dialog.save_button.click()
        self.assertEqual(self.calls['save'][0], ['字' * 300, ROWS[1]['text']])
        self.dialog.editor.setPlainText('字' * 301)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.dialog._save()
        self.assertEqual(len(self.calls['save']), 1)
        self.assertEqual(self.dialog.editor.toPlainText(), '字' * 301)
        self.dialog.editor.setPlainText('  \n  ')
        self.assertFalse(self.dialog.save_button.isEnabled())

    def test_busy_blocks_generation_editing_selection_and_save_but_allows_stop(self):
        self.rows()
        self.check(0)
        self.dialog.set_busy(True)
        for control in (self.dialog.rounds, self.dialog.generate_button, self.dialog.save_button,
                        self.dialog.editor, self.dialog.candidate_list):
            self.assertFalse(control.isEnabled())
        self.assertTrue(self.dialog.stop_button.isEnabled())
        self.dialog.generate_button.click()
        self.dialog._save()
        self.dialog.stop_button.click()
        self.assertEqual(self.calls, {'generate': [], 'cancel': [True], 'save': []})
        self.dialog.set_busy(False)
        self.assertTrue(self.dialog.editor.isEnabled())
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.stop_button.isEnabled())

    def test_error_preserves_selection_edited_text_and_original_quote(self):
        self.rows()
        self.check(0)
        self.dialog.editor.setPlainText('保留修改以便重试。')
        self.dialog.show_error('当前只剩一条空位，请减少选择。')
        self.assertEqual(self.dialog.editor.toPlainText(), '保留修改以便重试。')
        self.assertEqual(self.dialog.source_quote.toPlainText(), ROWS[0]['quote'])
        self.assertEqual(self.dialog.candidate_list.item(0).checkState(), Qt.CheckState.Checked)
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertEqual(self.calls['save'], [])

    def test_zero_candidates_is_normal_and_clears_old_selection(self):
        self.rows()
        self.check(0)
        self.dialog.show_candidates([])
        self.assertEqual(self.dialog.candidate_list.count(), 0)
        self.assertIn('没有需要长期保存的内容', self.dialog.status.text())
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertEqual(self.dialog.editor.toPlainText(), '')
        self.assertEqual(self.dialog.source_quote.toPlainText(), '')

    def test_invalid_result_preserves_previous_edits(self):
        self.rows()
        self.dialog.editor.setPlainText('有效修改。')
        self.assertFalse(self.dialog.show_candidates([{'text': '伪候选', 'sourceId': 1}]))
        self.assertEqual(self.dialog.editor.toPlainText(), '有效修改。')
        self.assertEqual(self.dialog.candidate_list.count(), 2)

    def test_close_while_busy_cancels_once_and_discards_all_candidate_text(self):
        self.dialog.open()
        self.rows()
        self.check(0)
        self.dialog.set_busy(True)
        self.dialog.close()
        self.app.processEvents()
        self.dialog.reject()
        self.assertEqual(self.calls['cancel'], [True])
        self.assertEqual(self.dialog._candidates, [])
        self.assertEqual(self.dialog.editor.toPlainText(), '')
        self.assertEqual(self.dialog.source_quote.toPlainText(), '')
        self.assertEqual(self.calls['save'], [])
        self.assertEqual(vars(self.store), self.before)

    def test_idle_close_discards_candidates_without_cancel_or_save(self):
        self.dialog.open()
        self.rows()
        self.check(0)
        self.dialog.reject()
        self.assertEqual(self.dialog._candidates, [])
        self.assertEqual(self.calls['cancel'], [])
        self.assertEqual(self.calls['save'], [])

    def test_new_result_does_not_retain_previous_checks_or_mutate_input(self):
        source = copy.deepcopy(ROWS)
        self.dialog.show_candidates(source)
        self.check(0)
        self.dialog.editor.setPlainText('只编辑本次窗口副本。')
        self.assertEqual(source, ROWS)
        self.dialog.show_candidates(source)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertEqual(self.dialog.editor.toPlainText(), ROWS[0]['text'])

    def test_capacity_refresh_only_reads_facts_and_updates_display(self):
        self.assertIn('剩余 49 条', self.dialog.capacity_note.text())
        self.store.facts.extend({'text': str(i)} for i in range(49))
        after = copy.deepcopy(vars(self.store))
        self.dialog.refresh_capacity()
        self.assertIn('50 / 50', self.dialog.capacity_note.text())
        self.assertIn('剩余 0 条', self.dialog.capacity_note.text())
        self.assertEqual(vars(self.store), after)

    def test_small_window_scrolls_and_confirmation_stays_reachable(self):
        self.dialog.open()
        self.dialog.resize(360, 440)
        self.rows()
        self.app.processEvents()
        self.assertLessEqual(self.dialog.width(), 360)
        self.assertGreater(self.dialog.scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.dialog.scroll.horizontalScrollBar().maximum(), 0)
        self.assertTrue(self.dialog.rect().contains(self.dialog.save_button.geometry()))
        self.assertTrue(self.dialog.rect().contains(self.dialog.close_button.geometry()))
        for button in (self.dialog.generate_button, self.dialog.save_button, self.dialog.close_button):
            self.assertFalse(button.autoDefault())
            self.assertFalse(button.isDefault())

    def test_bottom_save_reveals_error_and_success_without_losing_failed_edit(self):
        self.dialog.open()
        self.dialog.resize(360, 440)
        self.rows()
        self.check(0)
        self.dialog.editor.setPlainText('写入失败后要继续修改的内容。')
        self.app.processEvents()
        calls = []
        def save(texts):
            calls.append(texts)
            if len(calls) == 1:
                self.dialog.show_error('模拟写入失败，本次没有保存，请调整后重试。')
            else:
                self.dialog.clear_candidates()
                self.dialog.show_status('已保存 1 条新记忆。')
        self.dialog._on_save = save
        def scroll_bottom():
            bar = self.dialog.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            self.app.processEvents()
        def feedback_visible():
            box = self.dialog.status.rect().translated(
                self.dialog.status.mapTo(self.dialog.scroll.viewport(), QPoint(0, 0)))
            return self.dialog.scroll.viewport().rect().contains(box)
        scroll_bottom()
        self.assertFalse(feedback_visible())
        self.dialog.save_button.click()
        self.app.processEvents()
        self.assertTrue(feedback_visible())
        self.assertEqual(self.dialog.editor.toPlainText(), '写入失败后要继续修改的内容。')
        self.assertEqual(self.dialog.candidate_list.item(0).checkState(), Qt.CheckState.Checked)
        self.dialog.editor.setPlainText('错误后明确修改并重新确认。')
        scroll_bottom()
        self.dialog.save_button.click()
        self.app.processEvents()
        self.assertTrue(feedback_visible())
        self.assertEqual(calls, [['写入失败后要继续修改的内容。'], ['错误后明确修改并重新确认。']])
        self.assertFalse(self.dialog.save_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
