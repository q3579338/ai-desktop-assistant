"""Deterministic opt-in companionship tests; no model, cloud, or production state."""
import copy
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
import uuid

from starpet.companion import ChatError, ChatStore, validated_settings
from starpet.proactive import ProactiveCompanion, companionship_messages, validate_line


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class Backend:
    def __init__(self, reply='我在这里，慢慢来', held=False, error=None):
        self.reply = reply
        self.error = error
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not held: self.release.set()

    def __call__(self, settings, messages, state, job):
        self.calls.append((copy.deepcopy(settings), copy.deepcopy(messages), state, job))
        self.entered.set()
        if not self.release.wait(3): raise ChatError('test backend timed out')
        if self.error: raise self.error
        return self.reply


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.settings = validated_settings({'provider':'ollama','model':'fixture-local',
            'name':'小灯','persona':'温柔简洁的虚构测试角色','personality':'gentle',
            'proactiveEnabled':True,'proactiveMinutes':5})
        self.facts = [{'id':'one','text':'EXPLICIT_FACT_ONLY：我喜欢茶','createdAt':1}]
        self.state = ROOT/'evidence/tests/proactive-no-state-access'
        self.actors = []
        self.actor, self.backend = self.make()

    def tearDown(self):
        for actor, backend in self.actors:
            actor.close()
            backend.release.set()

    def make(self, **kwargs):
        backend = Backend(**kwargs)
        actor = ProactiveCompanion(clock=self.clock, backend=backend)
        self.actors.append((actor, backend))
        return actor, backend

    def tick(self, actor=None, settings=None, facts=None, **flags):
        current = {'visible':True,'chatting':False,'protected':False,'quiet':False,'chat_busy':False}
        current.update(flags)
        (actor or self.actor).tick(settings or self.settings, self.facts if facts is None else facts,
                                   self.state, **current)

    def queued_result(self, actor=None):
        actor = actor or self.actor
        result = actor.results.get(timeout=2)
        actor.results.put(result)

    def launch(self, actor=None, settings=None, manual=False):
        actor = actor or self.actor
        self.assertTrue(actor.request(settings or self.settings, self.facts, self.state, manual=manual))

    def test_defaults_do_not_generate_automatically_but_explicit_preview_is_allowed(self):
        settings = validated_settings({'provider':'ollama','model':'fixture-local'})
        self.assertFalse(settings['proactiveEnabled'])
        self.tick(settings=settings)
        self.clock.advance(86400)
        self.tick(settings=settings)
        self.assertEqual(self.backend.calls, [])
        self.launch(settings=settings, manual=True)
        self.queued_result(); self.tick(settings=settings, chatting=True)
        self.assertEqual(self.actor.last_preview, self.backend.reply)
        self.assertEqual(self.actor.line, '')

    def test_codex_codebuddy_and_missing_local_model_never_call_backend(self):
        for provider, model in (('codex','cloud'),('codebuddy','cloud'),('ollama','')):
            with self.subTest(provider=provider):
                settings = {**self.settings,'provider':provider,'model':model}
                self.assertFalse(self.actor.request(settings,self.facts,self.state,manual=True))
                self.tick(settings=settings); self.clock.advance(4000); self.tick(settings=settings)
                self.assertEqual(self.backend.calls, [])
                self.assertIsNone(self.actor.job)

    def test_minimum_and_maximum_valid_intervals_require_a_full_idle_period(self):
        for minutes in (5,60):
            with self.subTest(minutes=minutes):
                actor, backend = self.make()
                settings = {**self.settings,'proactiveMinutes':minutes}
                self.tick(actor, settings)
                self.clock.advance(minutes*60-1)
                self.tick(actor, settings)
                self.assertEqual(backend.calls, [])
                self.clock.advance(1); self.tick(actor, settings)
                self.assertTrue(backend.entered.wait(2))
                self.assertEqual(len(backend.calls), 1)
                self.queued_result(actor); self.tick(actor, settings)

    def test_successful_line_is_visible_for_exactly_fourteen_seconds(self):
        self.tick(); self.launch(); self.queued_result(); self.tick()
        self.assertEqual(self.actor.line, self.backend.reply)
        self.clock.advance(13.999); self.tick()
        self.assertEqual(self.actor.line, self.backend.reply)
        self.clock.advance(.001); self.tick()
        self.assertEqual(self.actor.line, '')
        self.assertEqual(len(self.backend.calls), 1)

    def test_hidden_quiet_chat_window_tasks_and_chat_jobs_suppress_auto_without_catchup(self):
        for flags in ({'visible':False},{'quiet':True},{'chatting':True},{'protected':True},{'chat_busy':True}):
            with self.subTest(flags=flags):
                actor, backend = self.make()
                self.tick(actor, **flags)
                self.clock.advance(5000); self.tick(actor, **flags)
                self.assertEqual(backend.calls, [])
                self.tick(actor)
                self.clock.advance(299); self.tick(actor)
                self.assertEqual(backend.calls, [])
                self.clock.advance(1); self.tick(actor)
                self.assertTrue(backend.entered.wait(2))
                self.queued_result(actor); self.tick(actor)

    def test_active_automatic_request_is_cancelled_and_late_reply_cannot_appear(self):
        for flags in ({'visible':False},{'quiet':True},{'chatting':True},{'protected':True},{'chat_busy':True}):
            with self.subTest(flags=flags):
                actor, backend = self.make(held=True)
                self.tick(actor); self.launch(actor)
                self.assertTrue(backend.entered.wait(2))
                job = actor.job
                self.tick(actor, **flags)
                self.assertTrue(job.cancelled.is_set())
                self.assertIsNone(actor.job)
                backend.release.set(); self.queued_result(actor)
                self.tick(actor, **flags); self.tick(actor)
                self.assertEqual(actor.line, '')
                self.assertEqual(actor.last_preview, '')
                self.assertEqual(len(backend.calls), 1)

    def test_new_task_rejects_a_reply_already_queued_before_the_same_tick(self):
        self.tick(); self.launch(); self.queued_result()
        self.tick(protected=True)
        self.assertEqual(self.actor.line, '')
        self.assertEqual(self.actor.last_preview, '')
        self.assertIsNone(self.actor.job)

    def test_manual_preview_can_finish_in_settings_but_never_overrides_tasks_or_chat(self):
        actor, backend = self.make(held=True)
        self.tick(actor, chatting=True); self.launch(actor, manual=True)
        self.assertTrue(backend.entered.wait(2))
        self.tick(actor, chatting=True)
        self.assertFalse(actor.job.cancelled.is_set())
        backend.release.set(); self.queued_result(actor); self.tick(actor, chatting=True)
        self.assertEqual(actor.last_preview, backend.reply)
        self.assertEqual(actor.line, '')
        for flag in ('protected','chat_busy'):
            with self.subTest(flag=flag):
                actor, backend = self.make(held=True)
                self.launch(actor, manual=True); self.assertTrue(backend.entered.wait(2))
                job = actor.job
                self.tick(actor, chatting=True, **{flag:True})
                self.assertTrue(job.cancelled.is_set())
                backend.release.set(); self.queued_result(actor); self.tick(actor)
                self.assertEqual(actor.last_preview, '')

    def test_manual_preview_still_cancels_when_hidden_or_quiet(self):
        for flags in ({'visible':False},{'quiet':True}):
            with self.subTest(flags=flags):
                actor, backend = self.make(held=True)
                self.launch(actor, manual=True); self.assertTrue(backend.entered.wait(2))
                job = actor.job
                self.tick(actor, **flags)
                self.assertTrue(job.cancelled.is_set())
                backend.release.set(); self.queued_result(actor); self.tick(actor)
                self.assertEqual(actor.last_preview, '')

    def test_role_or_request_setting_change_cancels_and_cannot_show_previous_identity(self):
        changes = {'roleId':'robot','name':'新名字','persona':'新设定','personality':'concise',
                   'provider':'codex','model':'another-model','localUrl':'http://127.0.0.1:12345',
                   'proactiveEnabled':False,'proactiveMemory':True,'proactiveMinutes':20,'useMemory':False}
        for key, value in changes.items():
            with self.subTest(key=key):
                actor, backend = self.make(held=True)
                self.tick(actor); self.launch(actor); self.assertTrue(backend.entered.wait(2))
                job = actor.job
                modified = {**self.settings,key:value}
                self.tick(actor, modified)
                self.assertTrue(job.cancelled.is_set())
                backend.release.set(); self.queued_result(actor); self.tick(actor, modified)
                self.assertEqual(actor.line, '')
                self.assertEqual(actor.last_preview, '')
                self.assertEqual(len(backend.calls), 1)

    def test_tabby_form_setting_does_not_claim_the_wrong_body_and_cancels_old_generation(self):
        for form in ('catgirl','chat'):
            with self.subTest(form=form):
                messages = companionship_messages({**self.settings,'roleId':'tabby','formMode':form},self.facts)
                self.assertNotIn('目前是橘猫形态',messages[0]['content'])
        actor,backend = self.make(held=True)
        settings = {**self.settings,'formMode':'cat'}
        self.tick(actor,settings); self.launch(actor,settings)
        self.assertTrue(backend.entered.wait(2))
        job = actor.job
        modified = {**settings,'formMode':'catgirl'}
        self.tick(actor,modified)
        self.assertTrue(job.cancelled.is_set())
        backend.release.set(); self.queued_result(actor); self.tick(actor,modified)
        self.assertEqual(actor.last_preview,'')

    def test_memory_is_attached_only_when_both_explicit_switches_are_enabled(self):
        for proactive, general in ((False,False),(False,True),(True,False),(True,True)):
            with self.subTest(proactive=proactive, general=general):
                settings = {**self.settings,'proactiveMemory':proactive,'useMemory':general}
                messages = companionship_messages(settings,self.facts)
                self.assertEqual('EXPLICIT_FACT_ONLY' in json.dumps(messages), proactive and general)
                self.assertEqual([m['role'] for m in messages], ['system','user'])
                self.assertNotIn('createdAt',json.dumps(messages))
                self.assertNotIn('"id"',json.dumps(messages))

    def test_changing_allowed_facts_cancels_but_unused_facts_do_not_restart_generation(self):
        for enabled in (False,True):
            with self.subTest(memory=enabled):
                actor, backend = self.make(held=True)
                settings = {**self.settings,'proactiveMemory':enabled}
                self.launch(actor, settings); self.assertTrue(backend.entered.wait(2))
                job = actor.job
                self.tick(actor, settings, facts=[{'text':'CHANGED_FACT'}])
                self.assertEqual(job.cancelled.is_set(), enabled)
                backend.release.set(); self.queued_result(actor)
                self.tick(actor, settings, facts=[{'text':'CHANGED_FACT'}])
                self.assertEqual(actor.last_preview, '' if enabled else backend.reply)

    def test_settings_and_facts_are_unchanged_and_request_uses_short_local_limits(self):
        before_settings, before_facts = copy.deepcopy(self.settings),copy.deepcopy(self.facts)
        self.launch(); self.queued_result(); self.tick()
        settings,messages,state,job = self.backend.calls[0]
        self.assertEqual(settings['provider'], 'ollama')
        self.assertEqual(settings['replyTokens'], 256)
        self.assertEqual(settings['timeoutSeconds'], 30)
        self.assertEqual(state, self.state)
        self.assertEqual(len(messages), 2)
        self.assertEqual(self.settings, before_settings)
        self.assertEqual(self.facts, before_facts)

    def test_existing_disk_history_never_enters_messages_and_proactive_writes_nothing(self):
        base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT/'evidence/tests').resolve()
        base.mkdir(parents=True, exist_ok=True)
        folder = base/('proactive-memory-'+uuid.uuid4().hex)
        folder.mkdir()
        try:
            store = ChatStore(folder)
            store.save_settings(**{**{k:v for k,v in self.settings.items() if k!='roleId'}, 'proactiveMemory':True})
            store.add_fact('PERMITTED_EXPLICIT_FACT')
            store.commit_turn('PRIVATE_HISTORY_USER','PRIVATE_HISTORY_REPLY')
            before = {p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
            self.assertTrue(self.actor.request(store.settings,store.facts,folder,manual=True))
            self.queued_result()
            self.actor.tick(store.settings,store.facts,folder,visible=True,chatting=False,
                            protected=False,quiet=False,chat_busy=False)
            sent = json.dumps(self.backend.calls[0][1])
            self.assertIn('PERMITTED_EXPLICIT_FACT',sent)
            self.assertNotIn('PRIVATE_HISTORY',sent)
            self.assertEqual({p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()},before)
            self.assertEqual(len(ChatStore(folder).turns),1)
        finally:
            resolved = folder.resolve()
            if resolved.parent!=base or not resolved.name.startswith('proactive-memory-'):
                raise RuntimeError('Unexpected proactive fixture path')
            shutil.rmtree(resolved)

    def test_failure_and_invalid_reply_back_off_a_full_interval(self):
        for kwargs in ({'error':ChatError('本机模型不可用')},{'error':RuntimeError('PRIVATE_ERROR')},
                       {'reply':'字'*29}):
            with self.subTest(kwargs=kwargs):
                actor,backend = self.make(**kwargs)
                self.tick(actor); self.launch(actor); self.queued_result(actor); self.tick(actor)
                self.assertEqual(actor.line,'')
                self.assertNotIn('PRIVATE_ERROR',actor.status)
                self.clock.advance(299)
                for _ in range(10): self.tick(actor)
                self.assertEqual(len(backend.calls),1)
                self.clock.advance(1); self.tick(actor)
                self.queued_result(actor); self.tick(actor)
                self.assertEqual(len(backend.calls),2)

    def test_busy_generation_is_not_duplicated_and_close_cancels_permanently(self):
        actor,backend = self.make(held=True)
        self.tick(actor); self.launch(actor); self.assertTrue(backend.entered.wait(2))
        job = actor.job
        self.clock.advance(5000)
        for _ in range(4):
            self.tick(actor)
            self.assertFalse(actor.request(self.settings,self.facts,self.state,manual=True))
        self.assertEqual(len(backend.calls),1)
        actor.close()
        self.assertTrue(job.cancelled.is_set())
        backend.release.set(); self.queued_result(actor); self.tick(actor)
        self.assertEqual(actor.line,'')
        self.assertFalse(actor.request(self.settings,self.facts,self.state,manual=True))
        self.clock.advance(5000); self.tick(actor)
        self.assertEqual(len(backend.calls),1)

    def test_short_line_boundary_and_blank_or_control_text_rejection(self):
        self.assertEqual(validate_line('字'*28),'字'*28)
        self.assertEqual(validate_line('  “慢慢来”  '),'慢慢来')
        for value in (None,123,[], '', '   ', '"   "', '字'*29, '两行\n文字','内嵌\t制表'):
            with self.subTest(value=value), self.assertRaises(ChatError):
                validate_line(value)

    def test_unverified_completion_screen_claims_and_questions_are_not_displayed(self):
        for value in ('任务已完成','操作成功啦','全部搞定了','已经替你上传了','我看见你在喝茶',
                      '我看到了你的屏幕','你刚刚操作得很好','要不要喝杯茶？',
                      '任务做完啦','已经替你处理好了'):
            with self.subTest(value=value), self.assertRaises(ChatError):
                validate_line(value)
        for value in ('照自己的节奏来','我在这里安静陪着','偶尔放松一下也好'):
            self.assertEqual(validate_line(value),value)
        actor,backend = self.make(reply='任务已完成')
        self.tick(actor); self.launch(actor); self.queued_result(actor); self.tick(actor)
        self.assertEqual(actor.line,'')
        self.assertEqual(actor.last_preview,'')
        self.assertIsNone(actor.job)
        self.assertGreater(actor.next_due,self.clock())

    def test_unverified_time_or_personal_state_advice_is_not_displayed(self):
        for value in ('夜深了，记得休息。', '晚安，记得喝热水。', '陪你加班到天亮。', '你累了吧', '别难过'):
            with self.subTest(value=value), self.assertRaises(ChatError):
                validate_line(value)
        self.assertEqual(validate_line('按你的节奏就好。'), '按你的节奏就好。')


if __name__=='__main__':
    unittest.main()
