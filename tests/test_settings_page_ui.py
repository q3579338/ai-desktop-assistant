"""Settings-page presentation tests with synthetic widgets, only offscreen."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '.vendor')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QVBoxLayout, QWidget
from starpet.settings_gui import SettingsPage
from starpet.gui import STYLE, initialize_fonts


SECTIONS = [('profile', '名字与性格'), ('connection', '聊天连接'), ('memory', '聊天与记忆'),
            ('appearance', '外观与显示'), ('companionship', '互动与陪伴'), ('clients', '任务提醒')]


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() != 'offscreen':
            raise RuntimeError('Offscreen required; no foreground fallback')
        initialize_fonts(cls.app)

    def setUp(self):
        self.page = SettingsPage()
        self.page.setStyleSheet(STYLE)
        self.page.resize(360, 540)
        self.fields = {}
        self.layouts = {}
        self.saves = []
        self.page.save_button.clicked.connect(lambda: self.saves.append(True))
        for key, title in SECTIONS:
            layout = self.page.add_section(key, title,
                '在这里调整这一类设置。切换分类会保留尚未保存的内容，点击下方按钮后才生效。')
            self.layouts[key] = layout
            edit = QLineEdit('尚未保存的' + title)
            edit.setAccessibleName(title + '测试输入')
            checkbox = QCheckBox('测试开关，未保存前不会应用')
            layout.addWidget(edit)
            layout.addWidget(checkbox)
            for index in range(10):
                note = QLabel(f'第 {index + 1} 项说明：内容保持完整，窄窗口中换行显示。')
                note.setWordWrap(True)
                note.setTextFormat(Qt.TextFormat.PlainText)
                layout.addWidget(note)
            target = QLineEdit('末尾测试选项')
            layout.addWidget(target)
            self.fields[key] = (edit, checkbox, target)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def scroll(self, key):
        return self.page._sections[key]['scroll']

    def test_six_categories_and_fixed_confirmation_do_not_save_on_open(self):
        self.assertEqual(self.page.selector.count(), 6)
        self.assertEqual(self.page.current_key, 'profile')
        self.assertEqual(self.page.status_label.text(), '修改需保存后生效')
        self.assertEqual(self.page.save_button.text(), '保存全部设置')
        self.assertEqual(self.saves, [])
        self.page.save_button.click()
        self.assertEqual(self.saves, [True])

    def test_switching_categories_preserves_drafts_and_checkbox_values(self):
        edit, checkbox, _ = self.fields['profile']
        edit.setText('保留未保存的角色名')
        checkbox.setChecked(True)
        self.page.select_section('connection')
        self.fields['connection'][0].setText('保留未保存的连接草稿')
        self.page.select_section('profile')
        self.app.processEvents()
        self.assertEqual(edit.text(), '保留未保存的角色名')
        self.assertTrue(checkbox.isChecked())
        self.assertEqual(self.fields['connection'][0].text(), '保留未保存的连接草稿')
        self.assertEqual(self.saves, [])

    def test_each_category_retains_its_scroll_position(self):
        first = self.scroll('profile').verticalScrollBar()
        first.setValue(first.maximum())
        first_position = first.value()
        self.assertGreater(first_position, 0)
        self.page.select_section('connection')
        self.app.processEvents()
        second = self.scroll('connection').verticalScrollBar()
        second.setValue(second.maximum() // 2)
        second_position = second.value()
        self.page.select_section('profile')
        self.app.processEvents()
        self.assertEqual(first.value(), first_position)
        self.page.select_section('connection')
        self.app.processEvents()
        self.assertEqual(second.value(), second_position)

    def test_reveal_selects_the_right_category_and_scrolls_to_nested_control(self):
        wrapper = QWidget()
        nested = QVBoxLayout(wrapper)
        target = QLineEdit('嵌套目标')
        nested.addWidget(target)
        self.layouts['memory'].addWidget(wrapper)
        self.assertTrue(self.page.reveal(target))
        QTest.qWait(20)
        self.assertEqual(self.page.current_key, 'memory')
        scroll = self.scroll('memory')
        target_rect = target.rect().translated(target.mapTo(scroll.viewport(), QPoint(0, 0)))
        self.assertTrue(scroll.viewport().rect().contains(target_rect))
        self.assertEqual(self.saves, [])

    def test_foreign_control_and_unknown_category_do_not_change_selection(self):
        foreign = QLineEdit()
        try:
            self.assertFalse(self.page.reveal(foreign))
            self.assertFalse(self.page.select_section('unknown'))
            self.assertEqual(self.page.current_key, 'profile')
        finally:
            foreign.deleteLater()

    def test_duplicate_and_empty_category_keys_are_rejected_without_mutation(self):
        for key in ('profile', '', None):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.page.add_section(key, '不可用标题')
        self.assertEqual(self.page.selector.count(), 6)

    def test_title_summary_and_status_are_plain_text(self):
        self.page.add_section('plain', '<b>不是HTML标题</b>', '<a href="https://example.invalid">只是说明</a>')
        self.page.select_section('plain')
        self.page.set_status('<b>原样显示错误内容</b>', error=True)
        section = self.page._sections['plain']
        for label in (section['title'], section['summary'], self.page.status_label):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<b>', self.page.status_label.text())
        self.assertEqual(self.saves, [])

    def test_status_and_save_stay_visible_for_small_and_regular_windows(self):
        self.page.set_status('设置尚未保存。请检查连接信息后再试；其他分类里的修改会继续保留。', error=True)
        for width, height in ((360, 540), (540, 760)):
            for key, _ in SECTIONS:
                with self.subTest(size=(width, height), category=key):
                    self.page.resize(width, height)
                    self.page.select_section(key)
                    self.app.processEvents()
                    scroll = self.scroll(key)
                    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                    self.app.processEvents()
                    self.assertEqual((self.page.width(), self.page.height()), (width, height))
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    for control in (self.page.selector, self.page.status_label, self.page.save_button):
                        rectangle = control.rect().translated(control.mapTo(self.page, QPoint(0, 0)))
                        self.assertTrue(self.page.rect().contains(rectangle))
                    for label in (self.page.status_label, self.page._sections[key]['title'], self.page._sections[key]['summary']):
                        needed = label.heightForWidth(label.contentsRect().width()) if label.hasHeightForWidth() else label.sizeHint().height()
                        self.assertGreaterEqual(label.height(), needed, label.text())
                    self.assertGreaterEqual(self.page.save_button.width(), self.page.save_button.sizeHint().width())

    def test_later_reveal_wins_and_manual_navigation_cancels_old_reveal(self):
        self.page.reveal(self.fields['memory'][2])
        self.page.reveal(self.fields['clients'][2])
        QTest.qWait(20)
        self.assertEqual(self.page.current_key, 'clients')
        self.assertGreater(self.scroll('clients').verticalScrollBar().value(), 0)
        self.page.reveal(self.fields['profile'][2])
        self.page.select_section('connection')
        before = self.scroll('connection').verticalScrollBar().value()
        QTest.qWait(20)
        self.assertEqual(self.page.current_key, 'connection')
        self.assertEqual(self.scroll('connection').verticalScrollBar().value(), before)

    def test_deleting_reveal_target_before_timer_runs_is_safe(self):
        target = self.fields['memory'][2]
        self.page.reveal(target)
        target.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(self.page.current_key, 'memory')
        self.assertEqual(self.saves, [])


if __name__ == '__main__':
    unittest.main()
