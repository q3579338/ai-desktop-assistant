"""No plugin lifecycle in either official CLI entry; all processes are fakes.

The hidden flag and environment switches were checked against the installed
official 2.177.1 interactive/headless bundles. Public-source evidence is kept in
evidence/workbuddy-160-environment-review-20261029.json; help omits this flag.
"""
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
import uuid

from starpet import codebuddy_backend as backend
from starpet.companion import Cancelled, ChatError, ChatJob


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ENV = {'CODEBUDDY_DISABLE_PLUGIN_INSTALLS': '1',
              'CODEBUDDY_PLUGIN_BACKGROUND_MANAGEMENT': '0'}


def process(text=''):
    value = Mock()
    value.returncode = 0
    value.poll.return_value = 0
    value.stdin = io.BytesIO()
    value.communicate.return_value = (text.encode('utf-8'), b'')
    return value


def reply():
    return '\n'.join(json.dumps(row) for row in [
        {'type': 'system', 'subtype': 'init', 'tools': [], 'mcp_servers': []},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Fixture only'},
    ])


class CodeBuddyPluginBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT / 'evidence/tests').resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.folder = self.base / ('codebuddy-plugins-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.settings = {'codebuddyPath': '', 'model': '', 'timeoutSeconds': 25}
        self.prefix = ['fixture-node', 'fixture-official-codebuddy']
        self.help = '\n'.join(backend.REQUIRED_OPTIONS)

    def tearDown(self):
        target = self.folder.resolve()
        if target.parent != self.base or not target.name.startswith('codebuddy-plugins-'):
            raise RuntimeError('Unexpected plugin boundary fixture path')
        shutil.rmtree(target)

    def call(self, kind, state, job):
        if kind == 'login':
            return backend.launch_official_login(self.settings, state, job)
        return backend.codebuddy_request(self.settings, [{'role': 'user', 'content': 'Fixture only'}], state, job)

    def invoke(self, kind, version='2.177.1', help_text=None, job=None, last=None):
        values = [process(version), process(self.help if help_text is None else help_text), last or process(reply())]
        with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen', side_effect=values) as launch:
            result = self.call(kind, self.folder / kind, job or ChatJob())
        return result, launch

    def assert_isolation(self, call):
        args, options = call.args[0], call.kwargs
        self.assertEqual(args.count('--no-plugin-management'), 1)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(json.loads(args[args.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
        self.assertIn('--no-session-persistence', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], 'local')
        config = json.loads(args[args.index('--settings') + 1])
        self.assertTrue(config['disableAllHooks'])
        self.assertFalse(config['memory']['autoMemoryEnabled'])
        self.assertFalse(config['memory']['parentDiscovery'])
        self.assertFalse(options['shell'])
        for key, expected in PLUGIN_ENV.items():
            self.assertEqual(options['env'][key], expected)

    def test_login_and_chat_share_plugin_tools_mcp_hook_and_memory_boundary(self):
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                result, launch = self.invoke(kind)
                self.assertEqual(result, [] if kind == 'login' else 'Fixture only')
                self.assertEqual(launch.call_count, 3)
                self.assert_isolation(launch.call_args)
                args = launch.call_args.args[0]
                self.assertEqual('--print' in args, kind == 'chat')
                for call in launch.call_args_list:
                    for key, expected in PLUGIN_ENV.items():
                        self.assertEqual(call.kwargs['env'][key], expected)

    def test_inherited_system_environment_cannot_reenable_plugins_or_change_profile(self):
        hostile = {'CODEBUDDY_DISABLE_PLUGIN_INSTALLS': '0',
                   'CODEBUDDY_PLUGIN_BACKGROUND_MANAGEMENT': '1',
                   'CODEBUDDY_CONFIG_DIR': 'UNRELATED_PROFILE',
                   'CODEBUDDY_PLUGIN_DIRS': 'UNRELATED_PLUGINS',
                   'NODE_OPTIONS': '--require UNRELATED_MODULE'}
        with patch.dict(os.environ, hostile):
            for kind in ('login', 'chat'):
                with self.subTest(kind=kind):
                    _, launch = self.invoke(kind)
                    env = launch.call_args.kwargs['env']
                    self.assert_isolation(launch.call_args)
                    self.assertEqual(env['CODEBUDDY_CONFIG_DIR'], str((self.folder / kind / 'codebuddy-profile').resolve()))
                    self.assertNotIn('CODEBUDDY_PLUGIN_DIRS', env)
                    self.assertNotIn('NODE_OPTIONS', env)
            self.assertEqual({key: os.environ[key] for key in hostile}, hostile)

    def test_hidden_flag_is_not_required_in_public_help(self):
        self.assertNotIn('--no-plugin-management', backend.REQUIRED_OPTIONS)
        self.assertNotIn('--no-plugin-management', self.help)
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                _, launch = self.invoke(kind)
                self.assertEqual([call.args[0][-1] for call in launch.call_args_list[:2]], ['--version', '--help'])
                self.assert_isolation(launch.call_args)

    def test_unknown_version_remains_rejected_before_login_or_chat(self):
        self.assertEqual(backend.REVIEWED_VERSIONS, frozenset(('2.177.1',)))
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen', return_value=process('2.177.2')) as launch:
                    with self.assertRaisesRegex(ChatError, '版本'):
                        self.call(kind, self.folder / kind, ChatJob())
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.args[0][-1], '--version')

    def test_existing_public_help_requirements_are_not_weakened(self):
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen', side_effect=[process('2.177.1'), process('--tools')]) as launch:
                    with self.assertRaisesRegex(ChatError, '所需选项'):
                        self.call(kind, self.folder / kind, ChatJob())
                self.assertEqual(launch.call_count, 2)

    def test_cancel_before_entry_starts_nothing_and_creates_no_profile(self):
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                job = ChatJob()
                job.cancel()
                with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen') as launch:
                    with self.assertRaises(Cancelled):
                        self.call(kind, self.folder / kind, job)
                launch.assert_not_called()
                self.assertFalse((self.folder / kind).exists())

    def test_cancel_after_capability_check_prevents_login_or_chat_launch(self):
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                job = ChatJob()
                with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend, '_capabilities', side_effect=lambda *args: job.cancel()), patch.object(backend.subprocess, 'Popen') as launch:
                    with self.assertRaises(Cancelled):
                        self.call(kind, self.folder / kind, job)
                launch.assert_not_called()

    def test_cancelled_chat_does_not_return_success_under_new_boundary(self):
        job = ChatJob()
        last = process()

        def cancelled(*args, **kwargs):
            job.cancel()
            return reply().encode('utf-8'), b''

        last.communicate.side_effect = cancelled
        with self.assertRaises(Cancelled):
            self.invoke('chat', job=job, last=last)
        self.assertNotIn('result_success', job.audit)

    def test_existing_plugin_registry_is_rejected_and_never_deleted_or_read(self):
        for kind in ('login', 'chat'):
            with self.subTest(kind=kind):
                state = self.folder / kind
                profile, _ = backend._prepare_directories(state)
                (profile / 'plugins').mkdir()
                registry = profile / 'plugins/known_marketplaces.json'
                registry.write_bytes(b'{}\n')
                sentinel = profile / '.credentials.json'
                sentinel.write_bytes(b'ISOLATED FAKE CREDENTIAL - DO NOT READ')
                before = {str(path.relative_to(state)): path.read_bytes() for path in state.rglob('*') if path.is_file()}
                with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen') as launch, patch.object(Path, 'read_text', side_effect=AssertionError('Do not read private/config contents')):
                    with self.assertRaisesRegex(ChatError, '额外技能、插件或记忆'):
                        self.call(kind, state, ChatJob())
                launch.assert_not_called()
                self.assertEqual({str(path.relative_to(state)): path.read_bytes() for path in state.rglob('*') if path.is_file()}, before)

    def test_ancestor_rules_and_extra_local_config_still_block_both_entries(self):
        for kind in ('login', 'chat'):
            for marker in ('ancestor', 'local'):
                with self.subTest(kind=kind, marker=marker):
                    state = self.folder / (kind + '-' + marker) / 'state'
                    profile, _ = backend._prepare_directories(state)
                    target = state.parent / 'AGENTS.md' if marker == 'ancestor' else profile / 'settings.local.json'
                    target.write_bytes(b'ISOLATED MARKER - DO NOT READ')
                    with patch.object(backend, 'find_codebuddy', return_value=self.prefix), patch.object(backend.subprocess, 'Popen') as launch, patch.object(Path, 'read_text', side_effect=AssertionError('Do not read rule contents')):
                        with self.assertRaises(ChatError):
                            self.call(kind, state, ChatJob())
                    launch.assert_not_called()
                    self.assertEqual(target.read_bytes(), b'ISOLATED MARKER - DO NOT READ')


if __name__ == '__main__':
    unittest.main()
