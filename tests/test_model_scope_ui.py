"""Provider changes cannot submit another provider's model identifier."""
import unittest
from unittest.mock import patch

from test_companion_context_ui import ContextUIFixture, QApplication
from starpet.companion import ChatStore, ChatError


class ProviderModelUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.f=ContextUIFixture(self.app)
        self.w=self.f.window

    def tearDown(self):
        self.f.close()

    def select(self,provider):
        self.w.provider.setCurrentIndex(self.w.provider.findData(provider))

    def test_local_name_does_not_become_official_cli_model(self):
        self.f.use_local()
        self.select('codebuddy')
        self.assertEqual(self.w.model.currentText(),'')
        self.assertEqual(self.w.model.count(),0)
        self.assertTrue(self.w.save_settings())
        self.assertEqual(self.f.store.settings['model'],'')
        self.assertEqual(self.f.store.settings['modelsByProvider']['ollama'],'fixture-local-model')
        self.f.send()
        self.f.wait_for(lambda:self.w.job is None)
        sent=self.f.chat.calls[-1]['settings']
        self.assertEqual((sent['provider'],sent['model']),('codebuddy',''))
        # A successful fixture chat may append history; existing facts persist.
        self.assertEqual(ChatStore(self.f.state).facts,self.f.store.facts)
        self.assertIn(b'fixture-local-model',(self.f.state/'companion-settings.json').read_bytes())

    def test_drafts_are_independent_and_save_all_survives_reload(self):
        self.w.model.setCurrentText('codex-choice')
        self.select('ollama');self.w.model.addItems(['installed-local-a','installed-local-b'])
        self.w.model.setCurrentText('installed-local-b')
        self.select('codebuddy');self.w.model.setCurrentText('buddy-choice')
        self.select('codex')
        self.assertEqual(self.w.model.currentText(),'codex-choice')
        self.assertEqual(self.w.model.count(),0)
        self.assertTrue(self.w.save_settings())
        self.w.load_settings()
        for provider,model in [('ollama','installed-local-b'),('codebuddy','buddy-choice'),('codex','codex-choice')]:
            self.select(provider);self.assertEqual(self.w.model.currentText(),model)
        saved=ChatStore(self.f.state).settings
        self.assertEqual(saved['modelsByProvider'],{'codex':'codex-choice','ollama':'installed-local-b','codebuddy':'buddy-choice'})

    def test_reload_and_role_switch_keep_saved_models_without_old_drafts(self):
        self.f.store.save_settings(provider='ollama',model='saved-local')
        self.w.load_settings()
        self.w.model.setCurrentText('unsaved-local')
        self.w.load_settings()
        self.assertEqual(self.w.model.currentText(),'saved-local')
        self.f.switch_role('robot')
        self.assertEqual(self.w.model.currentText(),'saved-local')
        self.select('codebuddy');self.assertEqual(self.w.model.currentText(),'')
        self.select('ollama');self.assertEqual(self.w.model.currentText(),'saved-local')

    def test_failed_save_keeps_saved_provider_and_recoverable_draft(self):
        before=dict(self.f.store.settings)
        self.select('codebuddy');self.w.model.setCurrentText('draft-buddy')
        with patch.object(self.f.store,'save_settings',side_effect=ChatError('测试保存失败')):
            self.assertFalse(self.w.save_settings())
        self.assertEqual(self.f.store.settings,before)
        self.assertIn('失败',self.w.feedback.text())
        self.select('codex');self.select('codebuddy')
        self.assertEqual(self.w.model.currentText(),'draft-buddy')

    def delayed_probe(self):
        self.select('ollama');self.w.model.setCurrentText('')
        self.f.chat.block=True
        def backend(settings,messages,state,job,probe=False):
            self.f.chat(settings,messages,state,job,probe=probe)
            return ['qwen3-4b-local']
        self.w.backend=backend;self.w.probe()
        self.assertTrue(self.f.chat.entered.wait(1))

    def test_late_local_probe_cannot_fill_another_provider(self):
        self.delayed_probe()
        memory=self.f.saved_memory()
        self.select('codebuddy')
        self.f.chat.release.set();self.f.wait_for(lambda:self.w.job is None)
        self.assertEqual(self.w.model.currentText(),'')
        self.assertEqual(self.w.model.count(),0)
        self.assertIn('保留当前草稿',self.w.feedback.text())
        self.assertTrue(self.w.save_settings())
        self.assertEqual((self.f.store.settings['provider'],self.f.store.settings['model']),('codebuddy',''))
        self.assertEqual(memory,self.f.saved_memory())

    def test_late_probe_preserves_new_model_or_endpoint_draft(self):
        for field,value in [('model','new-model-choice'),('local_url','http://127.0.0.1:19202')]:
            self.f.chat.entered.clear();self.f.chat.release.clear()
            self.delayed_probe()
            if field=='model':self.w.model.setCurrentText(value)
            else:self.w.local_url.setText(value)
            self.f.chat.release.set();self.f.wait_for(lambda:self.w.job is None)
            self.assertEqual(self.w.model.currentText(),value if field=='model' else '')
            self.assertEqual(self.w.model.count(),0)
            self.assertIn('保留当前草稿',self.w.feedback.text())


if __name__=='__main__':unittest.main()
