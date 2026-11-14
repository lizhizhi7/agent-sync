"""The local client uses disposable homes, synthetic credentials and immutable fixtures."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('memory_client', ROOT / 'libexec/memory.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def encoded(value):
    return base64.b64encode(value.encode()).decode()


class Credentials:
    def get(self, key):
        return 'synthetic credential'


class Memory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()
        self.repo_path = self.path / 'repo'
        self.repo_path.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo_path)], check=True)
        subprocess.run(['git', '-C', str(self.repo_path), 'remote', 'add', 'origin',
                        'https://git.example/team/repo.git'], check=True)
        self.repo = client.repository(self.repo_path)
        self.home = self.path / 'state'
        self.patcher = patch.object(client, 'root', return_value=self.home)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.item = {'name': 'repo', 'server': 'https://memory.example', 'workspace': 'one',
                     'registry_id': 'a' * 32, 'credential_key': 'test', 'mode': 'write'}
        self.snapshot = {'id': 'a' * 32, 'scope': 'one', 'protocol': 2, 'view':'1:1', 'generation': 1, 'writable': True,
            'files': {'fact.md': {'revision': 'b' * 32, 'content': encoded('base'),
                                  'sha256': client.digest(b'base')}}}

    def open(self, writable=True):
        with patch.object(client, 'request', return_value=self.snapshot), patch.object(client,'load_bodies'):
            return client.open_session(self.repo, self.item, writable=writable, credentials=Credentials())

    def test_unbound_backend_does_not_require_git_or_data_directory(self):
        with patch.dict(os.environ, {'AGENT_SYNC_DIR': str(self.path / 'absent')}, clear=False):
            self.assertEqual(client.main([str(self.path / 'not-a-repository')]), 3)

    def test_repository_identity_normalizes_ssh_and_https_and_worktrees(self):
        subprocess.run(['git', '-C', str(self.repo_path), 'remote', 'set-url', 'origin',
                        'git@git.example:team/repo.git'], check=True)
        self.assertEqual(client.repository(self.repo_path)['key'], self.repo['key'])

    def test_primary_switch_keeps_both_bindings_and_existing_session(self):
        state = self.open()
        secondary = {**self.item, 'workspace': 'two', 'registry_id': 'c' * 32}
        first = client.connection_key(self.item['server'], self.item['workspace']) + ':' + self.item['registry_id']
        second = client.connection_key(secondary['server'], secondary['workspace']) + ':' + secondary['registry_id']
        client.save_profile({'version': 1, 'workspaces': {}, 'repositories': {self.repo['key']: {
            'active': True, 'target': first, 'bindings': {first: self.item, second: secondary}}}})
        client.main(['target', '--workspace', 'two', '--project', str(self.repo_path)])
        config = client.profile()['repositories'][self.repo['key']]
        self.assertEqual(len(config['bindings']), 2)
        self.assertEqual(config['bindings'][config['target']]['workspace'], 'two')
        self.assertEqual(client.load_session(state['id'])[1]['binding']['workspace'], 'one')

    def test_missing_primary_requires_selection_instead_of_first_match(self):
        config = {'repositories': {self.repo['key']: {'target': '', 'bindings': {'first': self.item}}}}
        with self.assertRaisesRegex(client.Failure, 'Choose a target'):
            client.binding(config, self.repo)

    def test_secondary_reference_cannot_publish_even_when_grant_can_write(self):
        state = self.open(writable=False)
        (client.session_path(state['id']) / 'content/fact.md').write_text('changed')
        with self.assertRaisesRegex(client.Failure, 'read-only'):
            client.push(state['id'], Credentials())

    def test_snapshot_open_never_replaces_an_existing_projection(self):
        first = self.open()
        directory = client.session_path(first['id'])
        (directory / 'content/fact.md').write_text('active edit')
        self.snapshot['files']['fact.md'] = {'revision': 'c' * 32, 'content': encoded('remote'),
                                            'sha256': client.digest(b'remote')}
        second = self.open()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual((directory / 'content/fact.md').read_text(), 'active edit')
        self.assertEqual((client.session_path(second['id']) / 'content/fact.md').read_text(), 'remote')

    def test_offline_cache_is_explicit_and_authority_denial_never_uses_cache(self):
        self.open()
        with patch.object(client, 'request', side_effect=client.Unavailable('offline')):
            _, stale = client.snapshot(self.item, Credentials())
            self.assertTrue(stale)
        with patch.object(client, 'request', side_effect=client.Failure('revoked')):
            with self.assertRaisesRegex(client.Failure, 'revoked'):
                client.snapshot(self.item, Credentials())

    def test_lost_response_preserves_request_and_edits_after_capture(self):
        state = self.open()
        directory = client.session_path(state['id'])
        (directory / 'content/fact.md').write_text('captured')
        with patch.object(client, 'request', side_effect=client.Unavailable('lost')):
            with self.assertRaises(client.Unavailable):
                client.push(state['id'], Credentials())
        pending = client.load_session(state['id'])[1]['pending']
        (directory / 'content/fact.md').write_text('later')
        result = {'id': 'd' * 32, 'state': 'saved', 'generation': 2,
                  'revisions': {'fact.md': 'e' * 32}}
        with patch.object(client, 'request', return_value=result) as request:
            client.push(state['id'], Credentials())
            self.assertEqual(request.call_args.kwargs['body']['request_key'], pending['request_key'])
            self.assertEqual(request.call_args.kwargs['body']['changes'], pending['changes'])
        current = client.load_session(state['id'])[1]
        self.assertEqual(current['files']['fact.md']['content'], encoded('captured'))
        self.assertEqual(client.delta(current['files'], client.collect(directory / 'content'))[0]['content'], encoded('later'))

    def test_conflict_blocks_later_batch_and_preserves_original_files(self):
        state = self.open()
        directory = client.session_path(state['id'])
        (directory / 'content/fact.md').write_text('proposed')
        result = {'id': 'd' * 32, 'state': 'conflict', 'generation': 2, 'revisions': {}}
        with patch.object(client, 'request', return_value=result):
            with self.assertRaisesRegex(client.Failure, 'Retained conflict'):
                client.push(state['id'], Credentials())
        (directory / 'content/fact.md').write_text('newer edit')
        with self.assertRaisesRegex(client.Failure, 'retained conflict'):
            client.push(state['id'], Credentials())
        self.assertEqual((directory / 'content/fact.md').read_text(), 'newer edit')

    def test_resolution_keeps_newer_edits_and_untouched_central_documents(self):
        state = self.open()
        directory = client.session_path(state['id'])
        (directory / 'content/fact.md').write_text('proposed')
        conflict = {'id': 'd' * 32, 'state': 'conflict', 'generation': 2, 'revisions': {}}
        with patch.object(client, 'request', return_value=conflict):
            with self.assertRaises(client.Failure):
                client.push(state['id'], Credentials())
        (directory / 'content/fact.md').write_text('later local edit')
        current = {'files': {'fact.md': {'revision': 'e' * 32, 'content': encoded('remote')},
                             'other.md': {'revision': 'f' * 32, 'content': encoded('untouched')}}}
        with patch.object(client, 'snapshot', return_value=(current, False)):
            client.resolve(state['id'], credentials=Credentials())
        (directory / 'resolution/content/fact.md').write_text('reviewed resolution')
        saved = {'id': 'f' * 32, 'state': 'saved', 'generation': 3, 'revisions': {'fact.md': 'a' * 32}}
        with patch.object(client, 'snapshot', return_value=(current, False)), patch.object(client, 'request', return_value=saved):
            client.resolve(state['id'], apply=True, credentials=Credentials())
        original = client.load_session(state['id'])[1]
        self.assertFalse(original['writable'])
        self.assertEqual((directory / 'content/fact.md').read_text(), 'later local edit')
        resolved_id = client.read(directory / 'resolution/state.json')['session']
        resolved_dir, resolved = client.load_session(resolved_id)
        self.assertEqual((resolved_dir / 'content/other.md').read_text(), 'untouched')
        self.assertFalse(client.delta(resolved['files'], client.collect(resolved_dir / 'content')))

    def test_definitive_validation_refusal_allows_corrected_files(self):
        state = self.open()
        directory = client.session_path(state['id'])
        (directory / 'content/fact.md').write_text('invalid content')
        with patch.object(client, 'request', side_effect=client.Refused(400, 'invalid content')):
            with self.assertRaises(client.Refused):
                client.push(state['id'], Credentials())
        rejected = client.load_session(state['id'])[1]
        self.assertIsNone(rejected['pending'])
        self.assertEqual(len(rejected['rejected']), 1)
        (directory / 'content/fact.md').write_text('corrected')
        saved = {'id': 'd' * 32, 'state': 'saved', 'generation': 2, 'revisions': {'fact.md': 'e' * 32}}
        with patch.object(client, 'request', return_value=saved) as request:
            client.push(state['id'], Credentials())
            body = request.call_args.kwargs['body']
            self.assertNotEqual(body['request_key'], rejected['rejected'][0]['request_key'])
            self.assertEqual(body['changes'][0]['content'], encoded('corrected'))

    def test_capture_refuses_symlinks_and_preserves_tombstone_version(self):
        directory = self.path / 'content'
        directory.mkdir()
        (directory / 'escape.md').symlink_to(self.path / 'private.md')
        with self.assertRaisesRegex(client.Failure, 'symlinks'):
            client.collect(directory)
        changes = client.delta({'restored.md': {'revision': 'f' * 32, 'content': None}},
                               {'restored.md': encoded('restored')})
        self.assertEqual(changes[0]['expected'], 'f' * 32)
        for path in ('../x.md', '/x.md', 'x//y.md', '.git/a.md', 'x.py'):
            with self.assertRaises(client.Failure):
                client.checked_path(path)

    def test_real_launcher_memory_is_independent_of_legacy_skill_rules(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith('AGENT_SYNC_')}
        env.update(HOME=str(self.path / 'home'), AGENT_SYNC_MEMORY_HOME=str(self.home))
        result = subprocess.run(['/bin/bash', str(ROOT / 'bin/agent-sync'), 'memory', 'status',
                                 '--project', str(self.repo_path)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('git.example/team/repo', result.stdout)
        help_result = subprocess.run(['/bin/bash', str(ROOT / 'bin/agent-sync'), 'memory', '--help'],
                                     env=env, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--session', help_result.stdout)

    def test_retired_project_is_not_relinked_or_migrated(self):
        data = self.path / 'data'
        data.mkdir()
        (data / '.agent-sync-data').touch()
        (data / '.agent-sync-memory-owned').write_text('-repo\n')
        source = data / 'projects/-repo/memory'
        source.mkdir(parents=True)
        (source / 'fact.md').write_text('retained private source')
        home = self.path / 'home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith('AGENT_SYNC_')}
        env.update(HOME=str(home), AGENT_SYNC_DIR=str(data), AGENT_SYNC_BACKEND='local')
        result = subprocess.run(['/bin/bash', str(ROOT / 'bin/agent-sync'), 'link'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((home / '.claude/projects/-repo/memory').exists())
        self.assertEqual((source / 'fact.md').read_text(), 'retained private source')

    def test_archive_ownership_arrival_preserves_unsent_local_memory(self):
        home, data, incoming = self.path / 'home', self.path / 'data', self.path / 'incoming'
        for directory in (home, data, incoming):
            directory.mkdir()
        (data / '.agent-sync-data').touch()
        source = data / 'projects/-repo/memory'
        source.mkdir(parents=True)
        (source / 'fact.md').write_text('unsent local correction')
        (incoming / '.agent-sync-data').touch()
        (incoming / '.agent-sync-memory-owned').write_text('-repo\n')
        other = incoming / 'projects/-other/memory'
        other.mkdir(parents=True)
        (other / 'fact.md').write_text('other project')
        archive = self.path / 'incoming.tar.gz'
        with tarfile.open(archive, 'w:gz') as target:
            target.add(incoming, arcname='.')
        binaries = self.path / 'bin'
        binaries.mkdir()
        aws = binaries / 'aws'
        aws.write_text('#!/bin/bash\nif [ "$2" = ls ]; then exit 0; fi\ncp "$AWS_FIXTURE_ARCHIVE" "$4"\n')
        aws.chmod(0o755)
        env = {key: value for key, value in os.environ.items() if not key.startswith('AGENT_SYNC_')}
        env.update(HOME=str(home), PATH=str(binaries) + ':' + os.environ['PATH'], AGENT_SYNC_DIR=str(data),
                   AGENT_SYNC_BACKEND='s3', AGENT_SYNC_STORAGE_URI='s3://fixture/memory',
                   AWS_FIXTURE_ARCHIVE=str(archive), AGENT_SYNC_MEMORY_HOME=str(self.home))
        result = subprocess.run(['/bin/bash', str(ROOT / 'bin/agent-sync'), 'pull'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        retained = list((self.home / 'retired').glob('*/project/memory/fact.md'))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(), 'unsent local correction')
        self.assertFalse(source.exists())
        self.assertEqual((data / 'projects/-other/memory/fact.md').read_text(), 'other project')


if __name__ == '__main__':
    unittest.main()
