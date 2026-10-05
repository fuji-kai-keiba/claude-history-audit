import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from claude_history_audit import collection as c
from claude_history_audit._export import export
from claude_history_audit.audit import audit
from claude_history_audit.cli import main
from claude_history_audit.report import render_html


def row(mid='message-one', sid='session-one'):
    return {'type': 'assistant', 'sessionId': sid, 'timestamp': '2026-10-01T12:00:00Z',
            'message': {'id': mid, 'model': 'claude-sonnet-4-6', 'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'PRIVATE_TRANSCRIPT_SENTINEL'}],
                        'usage': {'input_tokens': 100, 'output_tokens': 20,
                                  'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 10}}}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.stdout = patch('sys.stdout', new_callable=io.StringIO).start()
        self.stderr = patch('sys.stderr', new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'remote-history'
        self.source.mkdir()
        self.log = self.source / 'session.jsonl'
        self.log.write_text(json.dumps(row()) + '\n')
        self.ssh = {'kind': 'ssh', 'name': 'private-host-name', 'host': 'user@host', 'path': str(self.source)}
        self.config = {'version': 1, 'include_local': False, 'sources': [self.ssh]}

    def archive(self, files, extras=None):
        target = self.root / 'test.zip'
        manifest = {'version': 1, 'root': '/private/root', 'skipped_symlinks': 0,
                    'files': [{'name': k, 'size': len(v)} for k, v in files.items()]}
        with zipfile.ZipFile(target, 'w') as z:
            for name, content in files.items():
                z.writestr(name, content)
            for name, content in (extras or {}).items():
                z.writestr(name, content)
            z.writestr('manifest.json', json.dumps(manifest))
        return target

    def test_config_rejects_shell_and_option_injection(self):
        for host in ['-oProxyCommand=evil', 'server;touch x', '$(whoami)', 'host\nother', 'host other']:
            with self.subTest(host=host):
                bad = dict(self.ssh, host=host)
                with self.assertRaises(ValueError):
                    c.validate_config(dict(self.config, sources=[bad]))
        with self.assertRaises(ValueError):
            c.validate_config(dict(self.config, sources=[self.ssh, self.ssh]))
        with self.assertRaises(ValueError):
            c.validate_config(dict(self.config, sources=[dict(self.ssh, timeout_seconds=True)]))

    def test_registration_is_private_and_makes_no_connection(self):
        config_path = self.root / 'settings.json'
        with patch.object(c, 'fetch_archive', side_effect=AssertionError('unexpected network')):
            self.assertEqual(c.sources_main(['--config', str(config_path), 'add-ssh', 'work', 'user@host']), 0)
            self.assertEqual(c.sources_main(['--config', str(config_path), 'add-ssh', 'work', 'user@host']), 2)
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(c.read_config(config_path)['sources'][0]['host'], 'user@host')
        self.assertEqual(c.sources_main(['--config', str(config_path), 'remove', 'work']), 0)
        self.assertEqual(c.read_config(config_path)['sources'], [])

    def test_export_only_jsonl_skips_symlinks_and_preserves_original(self):
        (self.source / '.credentials.json').write_text('SECRET_NOT_FOR_TRANSFER')
        (self.source / 'history.jsonl').write_text('PROMPT_INDEX')
        (self.source / 'linked.jsonl').symlink_to(self.log)
        (self.source / 'linked-dir').symlink_to(self.root, target_is_directory=True)
        before = hashlib.sha256(self.log.read_bytes()).hexdigest()
        out = io.BytesIO()
        export({'path': str(self.source), 'max_bytes': 100000}, out)
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:
            self.assertEqual(set(z.namelist()), {'files/session.jsonl', 'manifest.json'})
            self.assertEqual(json.loads(z.read('manifest.json'))['skipped_symlinks'], 2)
        self.assertEqual(before, hashlib.sha256(self.log.read_bytes()).hexdigest())

    def test_export_enforces_size_limit(self):
        with self.assertRaises(ValueError):
            export({'path': str(self.source), 'max_bytes': 1}, io.BytesIO())

    def test_real_export_receiver_roundtrip_and_dedup(self):
        child = self.source / 'session-one' / 'subagents'
        child.mkdir(parents=True)
        (child / 'agent-abc.jsonl').write_text(json.dumps(row('message-two', 'session-one')) + '\n')
        # Run exactly the SSH stdin program through a local Python process.
        with patch.object(c, 'ssh_command', return_value=[sys.executable, '-']):
            paths, status, private = c.collect(self.config, self.root / 'snapshot')
        self.assertEqual(status['successful'], 1)
        self.assertEqual(status['failed'], 0)
        self.assertEqual(status['sources'][0]['files'], 2)
        report, evidence = audit([self.source] + paths)
        self.assertEqual(report['coverage']['unique_requests'], 2)
        self.assertEqual(report['totals']['output_tokens'], 40)
        self.assertEqual(sum(s['subagent'] for s in report['sessions']), 1)
        report['collection'] = status
        public = json.dumps(report) + render_html(report)
        for secret in ['PRIVATE_TRANSCRIPT_SENTINEL', str(self.source), 'user@host', 'private-host-name']:
            self.assertNotIn(secret, public)
        self.assertIn('remote_root', private['source-001'])
        for p in paths[0].rglob('*.jsonl'):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'snapshot').stat().st_mode), 0o700)

    def test_paths_are_data_not_shell_commands(self):
        strange = self.root / '$(touch INJECTED); spaces'
        strange.mkdir()
        (strange / 'a.jsonl').write_text(self.log.read_text())
        with patch.object(c, 'ssh_command', return_value=[sys.executable, '-']):
            archive = self.root / 'literal.zip'
            c.fetch_archive(dict(self.ssh, path=str(strange)), archive)
        self.assertFalse((self.root / 'INJECTED').exists())
        self.assertEqual(c.extract_archive(archive, self.root / 'extracted', 10000)['root'], str(strange))

    def test_archive_blocks_traversal_and_unexpected_members(self):
        for name in ['files/../outside.jsonl', '/files/absolute.jsonl', 'files/a\\b.jsonl', 'files/C:bad.jsonl', 'files//a.jsonl']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                c.extract_archive(self.archive({name: b'x'}), self.root / 'out', 10000)
        with self.assertRaises(ValueError):
            c.extract_archive(self.archive({'files/a.jsonl': b'x'}, {'.credentials.json': b'secret'}), self.root / 'out', 10000)
        self.assertFalse((self.root / 'out').exists())

    def test_archive_blocks_symlink_and_expanded_size(self):
        target = self.archive({'files/a.jsonl': b'x'})
        with self.assertRaises(ValueError):
            c.extract_archive(target, self.root / 'out', 0)
        manifest = {'version': 1, 'root': '/private', 'skipped_symlinks': 0, 'files': [{'name': 'files/a.jsonl', 'size': 1}]}
        with zipfile.ZipFile(target, 'w') as z:
            info = zipfile.ZipInfo('files/a.jsonl')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, b'x')
            z.writestr('manifest.json', json.dumps(manifest))
        with self.assertRaises(ValueError):
            c.extract_archive(target, self.root / 'out', 10000)

    def test_ssh_timeout_and_nonzero_are_failures(self):
        with patch.object(c, 'ssh_command', return_value=[sys.executable, '-c', 'import time; time.sleep(20)']):
            with self.assertRaisesRegex(ValueError, 'connection_timeout'):
                c.fetch_archive(dict(self.ssh, timeout_seconds=1), self.root / 'timeout.zip')
        with patch.object(c, 'ssh_command', return_value=[sys.executable, '-c', 'import sys; sys.exit(2)']):
            with self.assertRaisesRegex(ValueError, 'ssh_or_export_failed'):
                c.fetch_archive(self.ssh, self.root / 'failure.zip')

    def test_failed_collection_does_not_reuse_prior_snapshot(self):
        with patch.object(c, 'ssh_command', return_value=[sys.executable, '-']):
            c.collect(self.config, self.root / 'old')
        with patch.object(c, 'fetch_archive', side_effect=ValueError('ssh_or_export_failed')):
            paths, status, _ = c.collect(self.config, self.root / 'new')
        self.assertEqual(paths, [])
        self.assertEqual(status['failed'], 1)
        self.assertFalse((self.root / 'new/source-001').exists())
        self.assertTrue((self.root / 'old/source-001/files/session.jsonl').exists())

    def test_partial_cli_requires_opt_in_and_returns_nonzero(self):
        config = dict(self.config, sources=[{'name': 'good', 'kind': 'path', 'path': str(self.source)}, self.ssh])
        cp = self.root / 'config.json'
        c.save_config(cp, config)
        with patch('claude_history_audit.cli.state_home', return_value=self.root / 'state'), patch.object(c, 'fetch_archive', side_effect=ValueError('ssh_or_export_failed')):
            result = main(['--sources-config', str(cp), '--all', '--output', str(self.root / 'strict')])
            self.assertEqual(result, 2)
            self.assertFalse((self.root / 'strict').exists())
            result = main(['--sources-config', str(cp), '--all', '--allow-partial', '--output', str(self.root / 'partial')])
        self.assertEqual(result, 3)
        report = json.loads((self.root / 'partial/report.json').read_text())
        self.assertEqual(report['collection']['failed'], 1)
        self.assertEqual(report['coverage']['unique_requests'], 1)
        self.assertIn('1件が失敗', (self.root / 'partial/report.html').read_text())

    def test_registered_paths_are_automatically_collected(self):
        cp = self.root / 'config.json'
        c.save_config(cp, dict(self.config, sources=[{'kind': 'path', 'name': 'mounted', 'path': str(self.source)}]))
        with patch('claude_history_audit.cli.default_config', return_value=cp), patch('claude_history_audit.cli.state_home', return_value=self.root / 'state'):
            self.assertEqual(main(['--all', '--output', str(self.root / 'automatic')]), 0)
        report = json.loads((self.root / 'automatic/report.json').read_text())
        self.assertEqual(report['collection']['mode'], 'registered')
        self.assertEqual(report['collection']['successful'], 1)

    def test_explicit_sources_do_not_connect(self):
        cp = self.root / 'config.json'
        c.save_config(cp, self.config)
        with patch('claude_history_audit.cli.default_config', return_value=cp), patch.object(c, 'fetch_archive', side_effect=AssertionError('must stay offline')):
            self.assertEqual(main(['--source', str(self.source), '--all', '--output', str(self.root / 'explicit')]), 0)
        report = json.loads((self.root / 'explicit/report.json').read_text())
        self.assertEqual(report['collection']['mode'], 'paths')

    def test_collection_never_writes_inside_local_source(self):
        cp = self.root / 'config.json'
        c.save_config(cp, dict(self.config, sources=[{'kind': 'path', 'name': 'mounted', 'path': str(self.source)}]))
        with patch('claude_history_audit.cli.state_home', return_value=self.source / 'bad-state'):
            self.assertEqual(main(['--sources-config', str(cp), '--all']), 2)
        self.assertFalse((self.source / 'bad-state').exists())

    def test_invalid_output_stops_before_remote_connection(self):
        cp = self.root / 'config.json'
        c.save_config(cp, self.config)
        with patch.object(c, 'fetch_archive', side_effect=AssertionError('unexpected connection')):
            self.assertEqual(main(['--sources-config', str(cp), '--output', str(self.source)]), 2)

    def test_symlink_only_source_is_partial_not_empty_success(self):
        (self.source / 'alias.jsonl').symlink_to(self.log)
        config = dict(self.config, sources=[{'kind': 'path', 'name': 'mounted', 'path': str(self.source)}])
        paths, status, _ = c.collect(config, self.root / 'snapshot')
        self.assertEqual(status['failed'], 1)
        self.assertEqual(status['sources'][0]['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
