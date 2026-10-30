import copy
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch,Mock
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from starpet.companion import (ChatStore,ChatJob,ChatError,Cancelled,validated_settings,
                              codex_request,ollama_request)

ROOT=Path(__file__).resolve().parents[1]


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.state=ROOT/'evidence/tests'/('chat-'+uuid.uuid4().hex[:10])
        self.store=ChatStore(self.state)

    def test_restart_retains_explicit_memory_and_complete_conversation(self):
        self.store.save_settings(name='团团',address='星宝',personality='gentle')
        self.store.add_fact('我喜欢简洁的中文回答')
        self.store.commit_turn('你好','你好，星宝。')
        restored=ChatStore(self.state)
        self.assertEqual(restored.settings['address'],'星宝')
        self.assertEqual(restored.facts,self.store.facts)
        self.assertEqual(restored.turns,self.store.turns)
        messages=restored.messages('记得我喜欢什么吗')
        self.assertIn('我喜欢简洁的中文回答',messages[0]['content'])
        self.assertEqual([m['role'] for m in messages],['system','user','assistant','user'])

    def test_edit_and_clear_memory_are_separate_from_chat_history(self):
        self.store.add_fact('我喜欢猫');identity=self.store.facts[0]['id']
        self.store.commit_turn('一句话','回答')
        self.store.edit_fact(identity,'我喜欢橘猫')
        self.assertEqual(ChatStore(self.state).facts[0]['text'],'我喜欢橘猫')
        self.store.clear_history()
        self.assertTrue(ChatStore(self.state).facts)
        self.assertEqual(ChatStore(self.state).turns,[])
        self.store.commit_turn('新一句','新回答')
        self.store.remove_fact(identity)
        self.assertEqual(ChatStore(self.state).facts,[])
        self.assertEqual(len(ChatStore(self.state).turns),1)

    def test_explicit_commands_are_labelled_local_and_do_not_infer_from_other_text(self):
        self.assertIsNone(self.store.local_command('今天工作有点累'))
        self.assertIn('已在本机记住',self.store.local_command('记住：我喜欢猫'))
        self.store.local_command('记住：我喜欢猫')
        self.assertEqual(len(self.store.facts),1)
        self.assertIn('我喜欢猫',self.store.local_command('查看记忆'))
        self.store.save_settings(rememberCommands=False)
        self.assertIsNone(self.store.local_command('记住：我喜欢狗'))

    def test_context_limit_and_memory_switch_apply_before_sending(self):
        self.store.add_fact('固定偏好')
        for n in range(6):self.store.commit_turn('user'+str(n),'reply'+str(n))
        self.store.save_settings(recentRounds=2,useMemory=False)
        messages=self.store.messages('新问题')
        self.assertEqual(len(messages),6)
        self.assertNotIn('固定偏好',messages[0]['content'])
        self.assertEqual(messages[1]['content'],'user4')
        self.store.save_settings(recentRounds=0)
        self.assertEqual(len(self.store.messages('新问题')),2)

    def test_task_sharing_is_opt_in_and_contains_only_state_counts(self):
        snap={'sessions':[{'clientLabel':'Codex','label':'正在思考','key':'PRIVATE-ID','text':'PRIVATE-TASK'}]}
        self.assertNotIn('Codex',self.store.messages('你好',snap)[0]['content'])
        self.store.save_settings(shareTaskState=True)
        content=self.store.messages('你好',snap)[0]['content']
        self.assertIn('Codex',content);self.assertNotIn('PRIVATE',content)

    def test_disabling_history_stops_new_disk_records_without_erasing_old_ones(self):
        self.store.commit_turn('保留旧对话','旧回复')
        self.store.save_settings(saveHistory=False)
        self.store.commit_turn('仅在当前窗口','不落盘的新回复')
        self.store.add_fact('新记忆')
        restored=ChatStore(self.state)
        self.assertEqual(len(restored.turns),1)
        self.assertEqual(len(self.store.turns),2)
        self.assertEqual(restored.facts[0]['text'],'新记忆')

    def test_large_chinese_history_remains_loadable_and_bounded(self):
        for n in range(60):self.store.commit_turn(str(n)+'中'*3990,'答'*11990)
        self.assertLess(self.store.memory_path.stat().st_size,1024*1024)
        self.assertEqual(ChatStore(self.state).turns,self.store.turns)
        self.assertIn('59',self.store.turns[-1]['user'])

    def test_invalid_settings_and_corrupt_memory_do_not_change_other_app_files(self):
        settings=validated_settings({'provider':'xai','recentRounds':999,'timeoutSeconds':True,'name':'a'*50,'formMode':'unknown'})
        self.assertEqual(settings['provider'],'codex')
        self.assertEqual(settings['recentRounds'],24)
        self.assertEqual(settings['timeoutSeconds'],120)
        self.assertEqual(len(settings['name']),24)
        self.state.mkdir(parents=True);self.store.memory_path.write_text('BROKEN')
        self.assertEqual(ChatStore(self.state).facts,[])
        self.assertFalse((self.state/'preferences.json').exists())

    def test_old_settings_load_new_companion_defaults_without_rewriting_user_data(self):
        self.store.add_fact('保留我的偏好');self.store.commit_turn('旧问题','旧回答')
        legacy={'provider':'ollama','localUrl':'http://localhost:11434','name':'橘子',
                'personality':'gentle','formMode':'catgirl','useMemory':False,'saveHistory':False}
        self.store.settings_path.write_text(json.dumps(legacy,ensure_ascii=False),encoding='utf-8')
        before_settings=self.store.settings_path.read_bytes();before_memory=self.store.memory_path.read_bytes()
        restored=ChatStore(self.state)
        self.assertEqual(restored.settings['activity'],'normal')
        self.assertTrue(restored.settings['idleMessages']);self.assertTrue(restored.settings['touchEnabled'])
        self.assertEqual(restored.settings['bubbleMode'],'always')
        for key,value in legacy.items():self.assertEqual(restored.settings[key],value)
        self.assertEqual(self.store.settings_path.read_bytes(),before_settings)
        self.assertEqual(self.store.memory_path.read_bytes(),before_memory)
        restored.save_settings(activity='calm',idleMessages=False,touchEnabled=False,bubbleMode='events')
        saved=ChatStore(self.state)
        self.assertEqual(saved.settings['activity'],'calm')
        self.assertFalse(saved.settings['idleMessages']);self.assertFalse(saved.settings['touchEnabled'])
        self.assertEqual(saved.settings['bubbleMode'],'events')
        for key,value in legacy.items():self.assertEqual(saved.settings[key],value)
        self.assertEqual(self.store.memory_path.read_bytes(),before_memory)

    def test_invalid_interaction_settings_fall_back_without_coercing_strings_or_numbers(self):
        settings=validated_settings({'activity':'always','idleMessages':'false',
                                     'touchEnabled':0,'bubbleMode':False})
        self.assertEqual(settings['activity'],'normal')
        self.assertTrue(settings['idleMessages']);self.assertTrue(settings['touchEnabled'])
        self.assertEqual(settings['bubbleMode'],'always')
        self.assertEqual(validated_settings({'bubbleMode':'bad'})['bubbleMode'],'always')

    def test_codex_uses_stdin_ephemeral_no_tools_and_does_not_pass_api_keys(self):
        proc=Mock();proc.returncode=0
        proc.communicate.return_value=(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'真实输出'}}).encode(),b'')
        job=ChatJob()
        with (patch('starpet.companion.find_codex',return_value=Path('C:/Codex/codex.exe')),
             patch('starpet.companion.subprocess.Popen',return_value=proc) as launch,
             patch.dict(os.environ,{'CODEX_API_KEY':'PRIVATE','OPENAI_API_KEY':'PRIVATE','CODEX_ACCESS_TOKEN':'PRIVATE'})):
            result=codex_request(self.store.settings,self.store.messages('PRIVATE-MESSAGE'),self.state,job)
        args=launch.call_args.args[0];env=launch.call_args.kwargs['env']
        self.assertEqual(result,'真实输出')
        self.assertIn('--ephemeral',args);self.assertIn('--ignore-user-config',args)
        self.assertIn('read-only',args);self.assertIn('features.shell_tool=false',args)
        self.assertNotIn('PRIVATE-MESSAGE',' '.join(args))
        self.assertNotIn('CODEX_API_KEY',env);self.assertNotIn('OPENAI_API_KEY',env)
        self.assertIn(b'PRIVATE-MESSAGE',proc.communicate.call_args.args[0])
        self.assertEqual(job.audit,['agent_message'])
        self.assertFalse(launch.call_args.kwargs.get('shell',False))

    def test_cancelled_codex_result_is_not_accepted(self):
        job=ChatJob();job.cancel()
        with patch('starpet.companion.find_codex',return_value=Path('C:/Codex/codex.exe')):
            with self.assertRaises(Cancelled):codex_request(self.store.settings,[],self.state,job)

    def test_cli_verification_refuses_existing_memory_and_production_state(self):
        import main
        self.store.add_fact('保留这条正式记忆')
        before=self.store.memory_path.read_bytes()
        with patch('sys.argv',['main.py','--verify-companion','--state-dir',str(self.state)]),patch('starpet.companion.model_request') as request:
            with self.assertRaises(ValueError):main.main()
            request.assert_not_called()
        self.assertEqual(self.store.memory_path.read_bytes(),before)
        empty=self.state/'production-fixture'
        with patch('main.default_state',return_value=empty),patch('sys.argv',['main.py','--verify-companion']),patch('starpet.companion.model_request') as request:
            with self.assertRaises(ValueError):main.main()
            request.assert_not_called()
        self.assertFalse(empty.exists())

    def test_local_model_rejects_remote_urls_and_does_not_follow_redirects(self):
        for url in ('https://example.com','http://example.com','http://user:pass@localhost:11434','http://127.0.0.1/api/chat'):
            settings={**self.store.settings,'localUrl':url}
            with self.assertRaises(ChatError):ollama_request(settings,[],ChatJob(),models=True)
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path);self.send_response(302);self.send_header('Location','https://example.com/steal');self.end_headers()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            settings={**self.store.settings,'localUrl':'http://127.0.0.1:'+str(server.server_port)}
            with self.assertRaisesRegex(ChatError,'跳转'):ollama_request(settings,[],ChatJob(),models=True)
            self.assertEqual(seen,['/api/tags'])
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
