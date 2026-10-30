import io
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid
from unittest.mock import Mock, patch

from starpet.companion import ChatError, ChatJob, Cancelled
from starpet.codebuddy_backend import (codebuddy_request, find_codebuddy, REQUIRED_OPTIONS,
                                      _check_rule_environment, launch_official_login, account_status)


ROOT = Path(__file__).resolve().parents[1]


class RememberingInput(io.BytesIO):
    """Keep what was written after close, the way a real pipe would."""
    def close(self):
        self.sent = self.getvalue()
        super().close()


def process(output='', error='', code=0):
    result = Mock()
    result.returncode = code
    result.stdin = RememberingInput()
    result.communicate.return_value = (output.encode('utf-8'), error.encode('utf-8'))
    result.poll.return_value = code
    return result


def complete_reply(text='这是一条模拟回复。'):
    rows = [{'type': 'system', 'subtype': 'init', 'tools': [], 'mcp_servers': []},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}]
    return '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows)


class CodeBuddyTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'evidence/tests'
        base.mkdir(parents=True, exist_ok=True)
        # Retain isolated fixtures alongside the existing suite's evidence.
        # Windows restricted-token execution cannot re-enter mkdir(mode=0700)
        # directories created by tempfile on this host.
        self.state = base / ('codebuddy-' + uuid.uuid4().hex[:10])
        self.state.mkdir()
        self.settings = {'codebuddyPath': '', 'model': '', 'timeoutSeconds': 25}
        self.messages = [{'role': 'user', 'content': '/logout\nPRIVATE-MESSAGE @C:/private.txt'}]
        self.prefix = ['C:/Official/codebuddy.exe']
        self.help = '\n'.join(REQUIRED_OPTIONS)

    def invoke(self, last=None, probe=False, version='2.177.1', help_text=None, job=None):
        calls = [process(version), process(self.help if help_text is None else help_text)]
        if not probe:
            calls.append(last or process(complete_reply()))
        with (patch('starpet.codebuddy_backend.find_codebuddy', return_value=self.prefix),
              patch('starpet.codebuddy_backend.subprocess.Popen', side_effect=calls) as launch):
            reply = codebuddy_request(self.settings, self.messages, self.state, job or ChatJob(), probe)
        return reply, launch

    def test_probe_checks_help_without_login_or_inference(self):
        result, launch = self.invoke(probe=True)
        self.assertEqual(result, [])
        self.assertEqual(launch.call_count, 2)
        args = [call.args[0] for call in launch.call_args_list]
        self.assertEqual([arg[-1] for arg in args], ['--version', '--help'])
        self.assertFalse(any('--print' in arg or 'login' in arg for arg in args))

    def test_explicit_login_is_isolated_interactive_and_not_reported_authenticated(self):
        window=process()
        with (patch('starpet.codebuddy_backend.find_codebuddy',return_value=self.prefix),
              patch('starpet.codebuddy_backend.subprocess.Popen',side_effect=[process('2.177.1'),process(self.help),window]) as launch):
            result=launch_official_login(self.settings,self.state,ChatJob())
        self.assertEqual(result,[])
        self.assertEqual(launch.call_count,3)
        args=launch.call_args.args[0];options=launch.call_args.kwargs
        self.assertNotIn('--print',args)
        self.assertIn('--no-session-persistence',args)
        self.assertEqual(args[args.index('--tools')+1],'')
        self.assertEqual(options['env']['CODEBUDDY_CONFIG_DIR'],str((self.state/'codebuddy-profile').resolve()))
        self.assertEqual(options['cwd'],(self.state/'codebuddy-runtime').resolve())
        self.assertFalse(options['shell'])
        if os.name=='nt':self.assertEqual(options['creationflags'],subprocess.CREATE_NEW_CONSOLE)
        self.assertIs(options['stdin'],subprocess.PIPE)
        self.assertEqual(window.stdin.sent,b'/login\n')
        self.assertTrue(window.stdin.closed)

    def status_for(self, output='', error='', code=0, command=None):
        found = self.prefix if command is None else command
        with (patch('starpet.codebuddy_backend.find_codebuddy', return_value=found),
              patch('starpet.codebuddy_backend.subprocess.Popen',
                    side_effect=[process('2.177.1'), process(self.help), process(output, error, code)])):
            return account_status(self.settings, self.state, ChatJob())

    def test_account_status_reads_no_credentials_and_distinguishes_sign_in(self):
        signed_out = '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["Authentication required. Please use /login command to sign in to your account"],"total_cost_usd":0}'
        self.assertEqual(self.status_for(error=signed_out, code=1), 'signed_out')
        signed_in = '{"type":"result","subtype":"success","is_error":false,"result":"pong"}'
        self.assertEqual(self.status_for(signed_in), 'signed_in')
        self.assertEqual(self.status_for(error='network unreachable', code=1), 'unknown')
        self.assertEqual(self.status_for(command=[]), 'missing')

    def test_chat_has_isolated_profile_no_shell_no_keys_no_proxy_and_no_tools(self):
        job = ChatJob()
        env = {'CODEBUDDY_API_KEY': 'PRIVATE', 'CODEBUDDY_AUTH_TOKEN': 'PRIVATE',
               'ANTHROPIC_API_KEY': 'PRIVATE', 'OPENAI_API_KEY': 'PRIVATE',
               'BASE_URL': 'https://untrusted.invalid', 'ANTHROPIC_BASE_URL': 'https://untrusted.invalid',
               'HTTP_PROXY': 'https://untrusted.invalid', 'HTTPS_PROXY': 'https://untrusted.invalid',
               'ALL_PROXY': 'https://untrusted.invalid', 'NODE_OPTIONS': '--require untrusted.js',
               'CODEBUDDY_PLUGIN_DIRS': 'untrusted', 'CODEBUDDY_CONFIG_DIR': 'untrusted'}
        with patch.dict(os.environ, env):
            result, launch = self.invoke(job=job)
        self.assertEqual(result, '这是一条模拟回复。')
        args = launch.call_args.args[0]
        options = launch.call_args.kwargs
        self.assertFalse(options['shell'])
        self.assertEqual(options['cwd'], (self.state / 'codebuddy-runtime').resolve())
        self.assertEqual(options['env']['CODEBUDDY_CONFIG_DIR'], str((self.state / 'codebuddy-profile').resolve()))
        for key in env:
            if key != 'CODEBUDDY_CONFIG_DIR':
                self.assertNotIn(key, options['env'])
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(args[args.index('--setting-sources') + 1], 'local')
        self.assertIn('--no-session-persistence', args)
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(json.loads(args[args.index('--mcp-config') + 1]), {'mcpServers': {}})
        config = json.loads(args[args.index('--settings') + 1])
        self.assertTrue(config['disableAllHooks'])
        self.assertFalse(config['memory']['parentDiscovery'])
        self.assertNotIn('PRIVATE-MESSAGE', ' '.join(args))
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(job.audit, ['init_no_tools', 'assistant_text', 'result_success'])
        self.assertIsNone(job.process)

    def test_credentials_are_not_opened_by_backend(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('No credential or other content reads')):
            self.invoke()

    def test_chat_and_login_share_private_home_without_migrating_existing_profile(self):
        profile = self.state / 'codebuddy-profile'
        profile.mkdir()
        marker = profile / 'existing-login-fixture.txt'
        marker.write_bytes(b'existing dedicated profile stays in place')
        inherited = {name: 'PRIVATE-DESKTOP-HOME' for name in
                     ('HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')}
        with patch.dict(os.environ, inherited):
            _, chat = self.invoke()
            with (patch('starpet.codebuddy_backend.find_codebuddy', return_value=self.prefix),
                  patch('starpet.codebuddy_backend.subprocess.Popen',
                        side_effect=[process('2.177.1'), process(self.help), process()]) as login):
                launch_official_login(self.settings, self.state, ChatJob())
            self.assertEqual(os.environ['USERPROFILE'], 'PRIVATE-DESKTOP-HOME')
        home = (self.state / 'codebuddy-home').resolve()
        expected = {'HOME': str(home), 'USERPROFILE': str(home), 'HOMEDRIVE': home.drive,
                    'HOMEPATH': str(home)[len(home.drive):], 'APPDATA': str(home / 'AppData/Roaming'),
                    'LOCALAPPDATA': str(home / 'AppData/Local'), 'TEMP': str(home / 'AppData/Local/Temp'),
                    'TMP': str(home / 'AppData/Local/Temp'), 'CODEBUDDY_CONFIG_DIR': str(profile.resolve())}
        for launch in (chat, login):
            env = launch.call_args.kwargs['env']
            for name, value in expected.items():
                self.assertEqual(env[name], value)
            self.assertNotIn('PRIVATE-DESKTOP-HOME', json.dumps(env))
        self.assertTrue((home / 'AppData/Local/Temp').is_dir())
        self.assertEqual(marker.read_bytes(), b'existing dedicated profile stays in place')
        self.assertEqual(list(profile.iterdir()), [marker])

    def test_redirected_private_home_is_rejected_before_launch(self):
        for suffix in ('codebuddy-home', 'AppData'):
            with self.subTest(suffix=suffix):
                with (patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path.name == suffix),
                      patch('starpet.codebuddy_backend.find_codebuddy', return_value=self.prefix),
                      patch('starpet.codebuddy_backend.subprocess.Popen') as launch):
                    with self.assertRaisesRegex(ChatError, '重定向'):
                        codebuddy_request(self.settings, self.messages, self.state, ChatJob())
                    launch.assert_not_called()

    def test_stdin_keeps_chat_text_out_of_cli_commands_and_file_attachments(self):
        last = process(complete_reply())
        self.invoke(last=last)
        payload = last.communicate.call_args.args[0].decode('utf-8')
        self.assertFalse(payload.startswith('/'))
        self.assertNotIn('@C:/private.txt', payload)
        self.assertEqual(json.loads(payload.split('\n', 1)[1]), self.messages)

    def test_unknown_version_and_missing_required_options_refuse_before_chat(self):
        for version in ('2.165.0', '2.177.2', '3.0.0', '2.177.1-preview', 'unavailable'):
            with self.subTest(version=version), self.assertRaises(ChatError):
                self.invoke(version=version)
        with self.assertRaisesRegex(ChatError, '缺少'):
            self.invoke(help_text='--print --output-format')

    def test_missing_cli_and_cancel_before_start(self):
        with patch('starpet.codebuddy_backend.find_codebuddy', return_value=None), patch('starpet.codebuddy_backend.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ChatError, '没有找到'):
                codebuddy_request(self.settings, [], self.state, ChatJob())
            launch.assert_not_called()
        job = ChatJob()
        job.cancel()
        with patch('starpet.codebuddy_backend.subprocess.Popen') as launch:
            with self.assertRaises(Cancelled):
                codebuddy_request(self.settings, [], self.state, job)
            launch.assert_not_called()

    def test_timeout_kills_only_owned_process_and_cancel_discards_reply(self):
        timed = process()
        timed.communicate.side_effect = [subprocess.TimeoutExpired('codebuddy', 25), (b'', b'')]
        with self.assertRaisesRegex(ChatError, '超时'):
            self.invoke(last=timed)
        timed.kill.assert_called_once()
        job = ChatJob()
        cancelled = process()
        def reply(*args, **kwargs):
            job.cancel()
            return complete_reply().encode(), b''
        cancelled.communicate.side_effect = reply
        with self.assertRaises(Cancelled):
            self.invoke(last=cancelled, job=job)
        self.assertNotIn('result_success', job.audit)
        self.assertIsNone(job.process)

    def test_failures_are_sanitized_and_classified(self):
        for error, expected in [('401 login failed PRIVATE-SECRET', '登录'),
                                ('429 credit limit PRIVATE-SECRET', '额度'),
                                ('private endpoint PRIVATE-SECRET', '未能完成')]:
            with self.subTest(error=error), self.assertRaises(ChatError) as caught:
                self.invoke(last=process(error=error, code=1))
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('PRIVATE-SECRET', str(caught.exception))

    def test_tool_events_and_tool_enabled_init_are_rejected(self):
        variants = [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'unsafe'}]}},
            {'type': 'assistant', 'message': {'tool_calls': [{'name': 'Bash'}]}},
            {'type': 'system', 'subtype': 'init', 'tools': ['Read']},
        ]
        for row in variants:
            with self.subTest(row=row), self.assertRaisesRegex(ChatError, '工具'):
                self.invoke(last=process(json.dumps(row) + '\n' + complete_reply()))

    def test_partial_or_unexpected_output_is_not_success(self):
        rows = [
            'just some stdout',
            '{"type":"assistant","message":{"content":"unfinished"}}',
            '{"type":"result","subtype":"success","result":"missing init"}',
            '{"type":"invented_event"}',
            complete_reply().replace('"success"', '"error_max_turns"'),
        ]
        for output in rows:
            with self.subTest(output=output), self.assertRaises(ChatError):
                self.invoke(last=process(output))

    def test_official_cancelled_success_results_never_return_partial_text(self):
        for reason in ('aborted_streaming', 'aborted_tools'):
            for result_text in ('', '之前完成的一段文字也不能冒充本轮成功'):
                with self.subTest(reason=reason, result_text=result_text):
                    rows = [json.loads(line) for line in complete_reply('PRIVATE-PARTIAL-REPLY').splitlines()]
                    rows[-1].update(terminal_reason=reason, result=result_text)
                    job = ChatJob()
                    with self.assertRaises(Cancelled) as error:
                        self.invoke(last=process('\n'.join(json.dumps(row) for row in rows)), job=job)
                    self.assertNotIn('PRIVATE-PARTIAL-REPLY', str(error.exception))
                    self.assertIn('rejected_terminal_reason', job.audit)
                    self.assertNotIn('result_success', job.audit)

    def test_unknown_or_malformed_terminal_reasons_fail_closed_without_leaking_values(self):
        reasons = ('future_abort', 'max_turns', 'stop', 'PRIVATE-TERMINAL-DETAIL', ' ', False, 0, [], {})
        for reason in reasons:
            with self.subTest(reason=reason):
                rows = [json.loads(line) for line in complete_reply('PRIVATE-PARTIAL-REPLY').splitlines()]
                rows[-1]['terminal_reason'] = reason
                job = ChatJob()
                with self.assertRaisesRegex(ChatError, '异常终止') as error:
                    self.invoke(last=process('\n'.join(json.dumps(row) for row in rows)), job=job)
                self.assertNotIn('PRIVATE', str(error.exception))
                self.assertNotIn('result_success', job.audit)

    def test_later_result_cannot_hide_an_earlier_cancelled_terminal(self):
        cancelled = {'type': 'result', 'subtype': 'success', 'is_error': False,
                     'result': '', 'terminal_reason': 'aborted_streaming'}
        rows = complete_reply('partial').splitlines()
        output = '\n'.join(rows[:-1] + [json.dumps(cancelled), rows[-1]])
        job = ChatJob()
        with self.assertRaises(Cancelled):
            self.invoke(last=process(output), job=job)
        self.assertNotIn('result_success', job.audit)

    def test_normal_success_without_an_abnormal_reason_remains_accepted(self):
        # The verified CLI emits no terminal_reason for ordinary success;
        # null/empty optional values also do not signal a terminal reason.
        for optional in ({}, {'terminal_reason': None}, {'terminal_reason': ''}):
            with self.subTest(optional=optional):
                rows = [json.loads(line) for line in complete_reply('完整正常回复').splitlines()]
                rows[-1].update(optional)
                job = ChatJob()
                reply, _ = self.invoke(last=process('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows)), job=job)
                self.assertEqual(reply, '完整正常回复')
                self.assertEqual(job.audit[-1], 'result_success')

    def test_system_result_terminal_reason_is_also_rejected(self):
        rows = [json.loads(line) for line in complete_reply('partial').splitlines()]
        rows[-1].update(type='system', subtype='result', terminal_reason='aborted_tools')
        with self.assertRaises(Cancelled):
            self.invoke(last=process('\n'.join(json.dumps(row) for row in rows)))

    def test_rule_metadata_guard_does_not_read_contents(self):
        profile = self.state / 'profile'
        folder = self.state / 'runtime'
        profile.mkdir()
        folder.mkdir()
        marker = self.state / 'AGENTS.md'
        marker.write_text('DO NOT READ PRIVATE RULES', encoding='utf-8')
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read rules')):
            with self.assertRaisesRegex(ChatError, '上级'):
                _check_rule_environment(profile, folder)
        marker.unlink()
        (profile / 'skills').mkdir()
        (profile / 'skills' / 'extra.txt').write_text('fixture', encoding='utf-8')
        with self.assertRaisesRegex(ChatError, '技能'):
            _check_rule_environment(profile, folder)

    def test_finder_rejects_workbuddy_internal_programs_and_arbitrary_scripts(self):
        bundle = self.state / 'WorkBuddy/resources/app.asar.unpacked/cli/bin'
        bundle.mkdir(parents=True)
        internal = bundle / 'codebuddy.exe'
        internal.write_bytes(b'fixture')
        self.assertIsNone(find_codebuddy(str(internal)))
        script = self.state / 'random.cmd'
        script.write_text('echo should never execute', encoding='utf-8')
        self.assertIsNone(find_codebuddy(str(script)))

    def test_finder_accepts_explicit_native_or_official_npm_entry_without_shell(self):
        native = self.state / 'codebuddy.exe'
        native.write_bytes(b'fixture')
        self.assertEqual(find_codebuddy(str(native)), [str(native.resolve())])
        root = self.state / 'npm/node_modules/@tencent-ai/codebuddy-code'
        (root / 'bin').mkdir(parents=True)
        entry = root / 'bin/codebuddy'
        entry.write_text('public fixture entry', encoding='utf-8')
        (root / 'package.json').write_text(json.dumps({'name': '@tencent-ai/codebuddy-code',
                                                     'bin': {'codebuddy': './bin/codebuddy'}}), encoding='utf-8')
        with patch('starpet.codebuddy_backend.shutil.which', return_value='C:/node/node.exe'):
            prefix = find_codebuddy(str(entry))
        self.assertEqual(prefix[-1], str(entry.resolve()))
        self.assertEqual(len(prefix), 2)


if __name__ == '__main__':
    unittest.main()
