import copy,unittest
from test_companion_context_ui import ContextUIFixture,QApplication
class ContextMenuRoutes(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):
  self.f=ContextUIFixture(self.app);self.c=self.f.c;self.before=copy.deepcopy(self.c.companion_store.settings)
 def tearDown(self):
  self.assertEqual(self.c.companion_store.settings,self.before);self.assertFalse(self.f.chat.calls);self.f.close()
 def test_single_settings_entry_opens_home(self):
  menu=self.c.menu();self.assertEqual(len([a for a in menu.actions() if not a.isSeparator()]),3);self.assertEqual(menu.actions()[0].text(),'设置');self.assertEqual(menu.actions()[-1].text(),'退出助手');menu.actions()[0].trigger();self.assertEqual(self.f.window.settings_page.current_key,'home')
 def test_categories_keep_drafts(self):
  self.f.window.name.setText('未保存名字')
  for key in ('home','profile','connection','clients','memory','appearance','companionship'):
   self.c.show_settings_section(key);self.assertEqual(self.f.window.name.text(),'未保存名字')
 def test_memory_growth_routes(self):
  self.f.window.settings_shortcuts['记忆'].click();self.assertEqual(self.f.window.tabs.currentIndex(),1)
  self.f.window.settings_shortcuts['成长档案'].click();self.assertEqual(self.f.window.tabs.currentIndex(),5)
 def test_features_retained(self):
  for title in ('小屋与树木','角色与自定义形象','从图片创建','猫与对应人物','任务与诊断','连接客户端','摸摸头','戳戳身体','打招呼','显示助手','隐藏助手','退出助手'):
   self.assertIn(title,self.f.window.settings_shortcuts)
