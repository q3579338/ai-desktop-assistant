import unittest
import test_connection_drafts_ui as fixtures

class ExplicitModelUITests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):fixtures.ConnectionDraftUITests.setUpClass.__func__(cls)
 setUp=fixtures.ConnectionDraftUITests.setUp
 tearDown=fixtures.ConnectionDraftUITests.tearDown
 def test_each_provider_keeps_its_own_model_draft(self):
  self.w.model.setCurrentText('my-codex-model')
  self.w.provider.setCurrentIndex(self.w.provider.findData('codebuddy'))
  self.w.model.setCurrentText('my-workbuddy-model')
  self.w.provider.setCurrentIndex(self.w.provider.findData('codex'))
  self.assertEqual(self.w.model.currentText(),'my-codex-model')
  self.w.provider.setCurrentIndex(self.w.provider.findData('codebuddy'))
  self.assertEqual(self.w.model.currentText(),'my-workbuddy-model')
 def test_clear_is_only_a_draft_until_save(self):
  before=self.store.settings_path.read_bytes();self.w.default_model_button.click()
  self.assertEqual(self.w.model.currentText(),'');self.assertEqual(self.store.settings_path.read_bytes(),before)
 def test_header_distinguishes_requested_model_and_unspecified(self):
  self.store.save_settings(model='chosen-model');self.w.update_title();self.assertIn('指定：chosen-model',self.w.connection.text())
  self.store.save_settings(model='');self.w.update_title();self.assertIn('未指定模型',self.w.connection.text())
