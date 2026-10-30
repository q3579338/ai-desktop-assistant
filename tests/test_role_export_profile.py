"""Role export uses saved presentation only; no account, model or production state."""
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'.vendor')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QFileDialog,QLabel
from starpet.companion import ChatStore
from starpet.rolepacks import RoleLibrary
from starpet.roles_gui import RoleWorkshop,PROFILE_FIELDS
from starpet.storage import atomic_json,read_json


class RoleExportProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        if cls.app.platformName()!='offscreen':
            raise RuntimeError('Role export tests require Qt offscreen')

    def setUp(self):
        self.base=Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT/'evidence/tests').resolve()
        self.base.mkdir(parents=True,exist_ok=True)
        self.folder=self.base/('role-export-'+uuid.uuid4().hex)
        self.folder.mkdir()
        self.state=self.folder/'isolated-state'
        self.store=ChatStore(self.state)
        self.library=RoleLibrary(self.state)
        source=self.folder/'fixture.png'
        image=QImage(8,8,QImage.Format.Format_ARGB32);image.fill(QColor('#3D8278'))
        self.assertTrue(image.save(str(source)))
        self.original=source.read_bytes()
        self.identity=self.library.import_image(source,'Original name','Original persona')
        self.window=SimpleNamespace(c=SimpleNamespace(companion_store=self.store,roles=self.library),
                                    job=None,feedback=QLabel())
        self.workshop=RoleWorkshop(self.window)

    def tearDown(self):
        self.workshop.hide();self.workshop.deleteLater();self.window.feedback.deleteLater()
        self.app.processEvents()
        resolved=self.folder.resolve()
        if resolved.parent!=self.base or not resolved.name.startswith('role-export-'):
            raise RuntimeError('Unexpected role export test directory')
        shutil.rmtree(resolved)

    def select(self, identity):
        self.workshop.refresh(identity)
        self.assertEqual(self.workshop.selected_id(),identity)

    def list_text(self, identity):
        for index in range(self.workshop.roles.count()):
            item=self.workshop.roles.item(index)
            if item.data(Qt.ItemDataRole.UserRole)==identity:return item.text()
        self.fail('Role missing from list')

    def export(self, suffix):
        destination=self.folder/(suffix+'.aipet')
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(destination),'aipet')):
            self.workshop.export_package()
        self.assertTrue(destination.exists(),self.window.feedback.text())
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()),{'role.json','image.png'})
            self.assertEqual(archive.read('image.png'),self.original)
            return json.loads(archive.read('role.json'))

    def test_inactive_role_exports_saved_profile_and_ignores_sensitive_extra_keys(self):
        self.store.switch_role(self.identity,self.library.profile(self.identity))
        expected={'name':'Saved custom name','persona':'Saved custom persona',
                  'personality':'concise','formMode':'cat'}
        self.store.save_settings(**expected,provider='ollama',model='PRIVATE_MODEL')
        self.store.add_fact('PRIVATE_MEMORY')
        self.store.commit_turn('PRIVATE_USER_TEXT','PRIVATE_REPLY')
        memory=self.store.memory_path
        before=memory.read_bytes()
        self.store.switch_role('robot',self.library.profile('robot'))
        profiles=read_json(self.store.profiles_path)
        profiles[self.identity].update(provider='PRIVATE_PROVIDER',model='PRIVATE_MODEL',
                                       facts=['PRIVATE_FACT'],turns=['PRIVATE_TURN'],token='PRIVATE_TOKEN')
        atomic_json(self.store.profiles_path,profiles)
        self.select(self.identity)
        self.assertTrue(self.list_text(self.identity).startswith(expected['name']+'\n'))
        with patch('starpet.roles_gui.read_json',wraps=read_json) as reads:
            manifest=self.export('inactive')
        self.assertEqual([call.args[0] for call in reads.call_args_list],[self.store.profiles_path])
        self.assertEqual({key:manifest[key] for key in PROFILE_FIELDS},expected)
        self.assertNotIn('PRIVATE_',json.dumps(manifest))
        self.assertEqual(memory.read_bytes(),before)
        self.assertEqual(self.store.settings['roleId'],'robot')

    def test_active_role_uses_latest_saved_settings_instead_of_stale_profile(self):
        self.store.switch_role(self.identity,self.library.profile(self.identity))
        self.store.save_settings(name='Old saved name',persona='Old saved persona')
        self.store.switch_role('robot',self.library.profile('robot'))
        self.store.switch_role(self.identity,self.library.profile(self.identity))
        expected={'name':'Latest name','persona':'Latest persona','personality':'gentle','formMode':'cat'}
        self.store.save_settings(**expected)
        self.select(self.identity)
        self.assertTrue(self.list_text(self.identity).startswith('Latest name · 使用中\n'))
        with patch('starpet.roles_gui.read_json',side_effect=AssertionError('Active export must not read profiles or memory')):
            manifest=self.export('active')
        self.assertEqual({key:manifest[key] for key in PROFILE_FIELDS},expected)

    def test_role_without_saved_profile_uses_library_defaults(self):
        for suffix,value in (('missing',{}),('unrelated',{'robot':{'name':'Other name'}}),
                             ('malformed',{'bad':'shape',self.identity:'not a profile'})):
            with self.subTest(suffix=suffix):
                atomic_json(self.store.profiles_path,value)
                self.select(self.identity)
                self.assertTrue(self.list_text(self.identity).startswith('Original name\n'))
                manifest=self.export(suffix)
                self.assertEqual(manifest['name'],'Original name')
                self.assertEqual(manifest['persona'],'Original persona')
                self.assertEqual(manifest['personality'],'playful')

    def test_returning_to_role_page_refreshes_the_active_custom_name(self):
        self.store.switch_role(self.identity,self.library.profile(self.identity))
        self.workshop.show();self.app.processEvents()
        self.workshop.hide()
        self.store.save_settings(name='Renamed while hidden')
        self.workshop.show();self.app.processEvents()
        self.assertTrue(self.list_text(self.identity).startswith('Renamed while hidden · 使用中\n'))


if __name__=='__main__':
    unittest.main()
