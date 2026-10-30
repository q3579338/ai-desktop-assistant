"""Find the installed public CLI without executing it or inspecting a profile."""
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from starpet import codebuddy_backend as backend


ROOT = Path(__file__).resolve().parents[1]


class CodeBuddyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get('STARPET_TEST_TMPDIR') or ROOT / 'evidence/tests').resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.folder = self.base / ('codebuddy-discovery-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.workspace = self.folder / 'workspace'
        self.project = self.workspace / 'starpet'
        self.node = self.folder / 'node/node.exe'
        self.node.parent.mkdir()
        self.node.write_bytes(b'not executable: fixture only')
        self.local = self.folder / 'local'
        self.roaming = self.folder / 'roaming'
        self.path_candidates = {}
        self.patchers = [
            patch.object(backend, '__file__', str(self.project / 'starpet/codebuddy_backend.py')),
            patch.object(backend.sys, 'frozen', False, create=True),
            patch.object(backend.shutil, 'which', side_effect=self.which),
            patch.dict(os.environ, {'LOCALAPPDATA': str(self.local), 'APPDATA': str(self.roaming)}, clear=True),
            patch.object(backend.subprocess, 'Popen', side_effect=AssertionError('Discovery must not launch a process')),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        target = self.folder.resolve()
        if target.parent != self.base or not target.name.startswith('codebuddy-discovery-'):
            raise RuntimeError('Unexpected discovery fixture path')
        shutil.rmtree(target)

    def which(self, name):
        if name == 'node.exe':
            return str(self.node) if self.node is not None else None
        return self.path_candidates.get(name)

    def package(self, root, name=backend.OFFICIAL_PACKAGE, version='2.177.1', bin_value='bin/codebuddy'):
        root.mkdir(parents=True, exist_ok=True)
        entry = root / 'bin/codebuddy'
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text('fixture only; never execute', encoding='utf-8')
        (root / 'package.json').write_text(json.dumps({'name': name, 'version': version,
            'bin': {'codebuddy': bin_value}}), encoding='utf-8')
        return entry

    def owned(self, root=None, version='2.177.1', **kwargs):
        return self.package((root or self.workspace) / 'ai-desktop-assistant-runtime/codebuddy' / version,
                            version=version, **kwargs)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not executable: fixture only')
        return str(path)

    def expected(self, entry):
        return [str(self.node.resolve()), str(entry.resolve())]

    def test_source_layout_discovers_the_owned_reviewed_package(self):
        entry = self.owned()
        self.assertEqual(backend.find_codebuddy(), self.expected(entry))

    def test_explicit_path_has_priority_over_path_and_owned_installations(self):
        explicit = self.package(self.folder / 'explicit')
        self.path_candidates['codebuddy.exe'] = self.executable(self.folder / 'path/codebuddy.exe')
        self.owned()
        self.assertEqual(backend.find_codebuddy(str(explicit)), self.expected(explicit))

    def test_invalid_explicit_path_does_not_fall_back(self):
        self.path_candidates['codebuddy.exe'] = self.executable(self.folder / 'path/codebuddy.exe')
        self.owned()
        self.assertIsNone(backend.find_codebuddy(str(self.folder / 'missing/codebuddy.exe')))

    def test_path_priority_is_preserved_before_owned_and_platform_paths(self):
        path_entry = self.executable(self.folder / 'path/codebuddy.exe')
        self.path_candidates['codebuddy.exe'] = path_entry
        self.owned()
        self.executable(self.local / 'codebuddy/bin/codebuddy.exe')
        self.assertEqual(backend.find_codebuddy(), [path_entry])

    def test_invalid_path_candidate_can_fall_back_to_owned_package(self):
        self.path_candidates['codebuddy'] = self.executable(self.folder / 'path/arbitrary.js')
        entry = self.owned()
        self.assertEqual(backend.find_codebuddy(), self.expected(entry))

    def test_owned_package_precedes_platform_installation(self):
        entry = self.owned()
        self.executable(self.local / 'codebuddy/bin/codebuddy.exe')
        self.package(self.roaming / 'npm/node_modules/@tencent-ai/codebuddy-code')
        self.assertEqual(backend.find_codebuddy(), self.expected(entry))

    def test_closest_owned_package_wins(self):
        self.owned()
        closest = self.owned(self.project)
        self.assertEqual(backend.find_codebuddy(), self.expected(closest))

    def test_frozen_deployed_and_staging_layouts_reach_the_same_owned_package(self):
        entry = self.owned()
        for relative in ('release/StarPet/StarPet.exe', 'release/staging-1.6.0/StarPet/StarPet.exe'):
            with self.subTest(relative=relative):
                with patch.object(backend.sys, 'frozen', True), patch.object(backend.sys, 'executable', str(self.project / relative)):
                    self.assertEqual(backend.find_codebuddy(), self.expected(entry))

    def test_frozen_search_does_not_extend_beyond_four_parents(self):
        self.owned(self.folder)
        executable = self.project / 'release/staging-1.6.0/StarPet/StarPet.exe'
        with patch.object(backend.sys, 'frozen', True), patch.object(backend.sys, 'executable', str(executable)):
            self.assertIsNone(backend.find_codebuddy())

    def test_unknown_owned_versions_are_not_discovered(self):
        self.owned(version='999.0.0')
        self.owned(version='2.177.2')
        self.assertIsNone(backend.find_codebuddy())

    def test_owned_candidate_requires_official_manifest_and_matching_bin(self):
        entry = self.owned(name='not-the-official-package')
        self.assertIsNone(backend.find_codebuddy())
        self.owned(bin_value='bin/other')
        self.assertIsNone(backend.find_codebuddy())
        (entry.parent.parent / 'package.json').unlink()
        self.assertIsNone(backend.find_codebuddy())

    def test_owned_candidate_requires_node_and_rejects_shell_shims(self):
        self.owned()
        for node in (None, self.folder / 'node.cmd', self.folder / 'node.ps1'):
            with self.subTest(node=node):
                self.node = node
                self.assertIsNone(backend.find_codebuddy())

    def test_existing_local_and_roaming_platform_paths_remain_supported(self):
        local_entry = self.executable(self.local / 'codebuddy/bin/codebuddy.exe')
        roaming_entry = self.package(self.roaming / 'npm/node_modules/@tencent-ai/codebuddy-code')
        self.assertEqual(backend.find_codebuddy(), [local_entry])
        Path(local_entry).unlink()
        self.assertEqual(backend.find_codebuddy(), self.expected(roaming_entry))

    def test_only_public_package_manifest_is_read_and_discovery_creates_nothing(self):
        entry = self.owned()
        for name in ('codebuddy-profile', 'workbuddy', '.codebuddy'):
            directory = self.workspace / name
            directory.mkdir()
            (directory / 'auth.json').write_text('PRIVATE SENTINEL', encoding='utf-8')
        before = {str(p.relative_to(self.folder)) for p in self.folder.rglob('*')}
        original = Path.read_text
        reads = []

        def read_public(path, *args, **kwargs):
            reads.append(path)
            self.assertEqual(path, entry.parent.parent / 'package.json')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_public), patch.object(Path, 'cwd', side_effect=AssertionError('Do not discover from current directory')):
            self.assertEqual(backend.find_codebuddy(), self.expected(entry))
        self.assertEqual(reads, [entry.parent.parent / 'package.json'])
        self.assertEqual({str(p.relative_to(self.folder)) for p in self.folder.rglob('*')}, before)

    def test_workbuddy_desktop_internal_candidate_stays_rejected(self):
        desktop = self.package(self.folder / 'WorkBuddy/app.asar.unpacked/internal')
        self.assertIsNone(backend.find_codebuddy(str(desktop)))


if __name__ == '__main__':
    unittest.main()
