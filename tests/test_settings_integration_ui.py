"""Settings categories, explicit persistence, and honest partial-save feedback."""
import copy
import unittest
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture, QApplication, ChatStore, ChatError


class SettingsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = ContextUIFixture(self.app)
        self.w, self.store = self.f.window, self.f.store
        self.page = self.w.settings_page
        self.w.tabs.setCurrentIndex(2)

    def tearDown(self):
        self.f.close()

    def files(self):
        return {str(p.relative_to(self.f.state)): p.read_bytes()
                for p in self.f.state.rglob('*') if p.is_file()}

    def test_categories_preserve_drafts_without_writing(self):
        before = self.files()
        name = self.w.name.text()
        self.w.name.setText('未保存的名字')
        for key in ('profile', 'connection', 'memory', 'appearance', 'companionship', 'clients'):
            self.assertTrue(self.page.select_section(key))
            self.f.pump()
            self.assertIn('未保存', self.page.status_label.text())
        self.assertEqual(self.files(), before)
        self.assertEqual(self.w.name.text(), '未保存的名字')
        self.w.name.setText(name)
        self.assertIn('当前设置已保存', self.page.status_label.text())
        self.assertEqual(self.files(), before)
        self.assertEqual(self.f.chat.calls, [])

    def test_fixed_save_commits_across_categories_and_survives_reload(self):
        self.w.name.setText('已确认的伙伴')
        self.w.rounds.setValue(3)
        self.w.size.setValue(115)
        self.w.notifications.setChecked(False)
        self.w.workbuddy_tasks.setChecked(False)
        self.page.select_section('clients')
        self.page.save_button.click()
        restored = ChatStore(self.f.state)
        self.assertEqual(restored.settings['name'], '已确认的伙伴')
        self.assertEqual(restored.settings['recentRounds'], 3)
        self.assertEqual(self.f.c.prefs.values['size'], 115)
        self.assertFalse(self.f.c.prefs.values['completionNotifications'])
        self.assertFalse(self.f.c.core.snapshot()['enabled']['workbuddy'])
        self.assertEqual(self.page.current_key, 'clients')
        self.assertIn('当前设置已保存', self.page.status_label.text())
        self.assertEqual(restored.facts, self.store.facts)

    def test_save_validation_reveals_profile_without_losing_other_drafts(self):
        before = self.files()
        self.w.persona.setPlainText('长' * 2001)
        self.w.rounds.setValue(2)
        self.page.select_section('memory')
        self.page.save_button.click()
        self.f.pump()
        self.assertEqual(self.page.current_key, 'profile')
        self.assertIn('2000', self.page.status_label.text())
        self.assertEqual(self.w.rounds.value(), 2)
        self.assertEqual(self.files(), before)

    def test_profile_save_error_stays_visible_and_keeps_draft(self):
        before = self.files()
        self.w.name.setText('失败时保留的名字')
        with patch.object(self.store, 'save_settings', side_effect=ChatError('隔离测试：设置未能保存')):
            self.page.save_button.click()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.w.name.text(), '失败时保留的名字')
        self.assertIn('设置未能保存', self.page.status_label.text())

    def test_preferences_failure_reports_saved_scope_and_preserves_preferences(self):
        before = copy.deepcopy(self.f.c.prefs.values)
        memory = self.f.saved_memory()
        self.w.name.setText('角色设置已保存')
        self.w.size.setValue(135)
        self.w.workbuddy_tasks.setChecked(False)
        with patch('starpet.storage.atomic_json', side_effect=OSError('fixture disk failure')):
            self.assertFalse(self.w.save_settings())
        self.assertEqual(ChatStore(self.f.state).settings['name'], '角色设置已保存')
        self.assertEqual(self.f.c.prefs.values, before)
        self.assertTrue(self.f.c.core.snapshot()['enabled']['workbuddy'])
        self.assertEqual(self.f.saved_memory(), memory)
        self.assertIn('设置未完整保存', self.page.status_label.text())
        self.assertEqual(self.w.size.value(), 135)
        self.assertTrue(self.w.save_settings())
        self.assertEqual(self.f.c.prefs.values['size'], 135)
        self.assertFalse(self.f.c.core.snapshot()['enabled']['workbuddy'])

    def test_changing_top_setting_keeps_hidden_pet_hidden(self):
        self.f.c.pet.hide()
        self.w.top.setChecked(not self.f.c.prefs.values['alwaysOnTop'])
        self.assertTrue(self.w.save_settings())
        self.assertFalse(self.f.c.pet.isVisible())

    def test_partial_save_dirty_status_compares_to_actual_persisted_values(self):
        old_name=self.w.name.text();old_size=self.w.size.value()
        self.w.name.setText('部分成功后实际保存的名字')
        self.w.size.setValue(135)
        with patch('starpet.storage.atomic_json',side_effect=OSError('fixture failure')):
            self.assertFalse(self.w.save_settings())
        self.w.name.setText(old_name);self.w.size.setValue(old_size)
        self.assertIn('未保存的修改',self.page.status_label.text())
        self.assertEqual(ChatStore(self.f.state).settings['name'],'部分成功后实际保存的名字')
        self.w.name.setText('部分成功后实际保存的名字')
        self.assertIn('当前设置已保存',self.page.status_label.text())


if __name__ == '__main__':
    unittest.main()
