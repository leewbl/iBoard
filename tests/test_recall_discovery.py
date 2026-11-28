from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workbench import cli, recall, recall_discovery


DAY = '2026-11-26T12:00:00Z'
RECENT = datetime(2026, 11, 25, tzinfo=timezone.utc)


class RecallDiscoveryTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root/'git project'
        self.home = self.root/'home'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-b', 'main')
        cli.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                'commit', '--allow-empty', '-m', 'Set up the project')

    def rollout(self, name, workspace, *, archived=False, timestamp=DAY):
        parent = self.home/('.codex/archived_sessions' if archived else '.codex/sessions/2026/11/26')
        parent.mkdir(parents=True, exist_ok=True)
        path = parent/f'rollout-2026-11-26T12-00-00-{name}.jsonl'
        header = {'timestamp': timestamp, 'type': 'session_meta',
                  'payload': {'id': name, 'timestamp': timestamp, 'cwd': str(workspace), 'source': 'cli'}}
        turn = {'timestamp': timestamp, 'type': 'response_item',
                'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Settle the receipt.'}]}}
        path.write_text(json.dumps(header) + '\n' + json.dumps(turn) + '\n')
        written = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
        os.utime(path, (written, written))
        return path

    def test_codex_checks_metadata_before_considering_conversation_and_checks_both_stores(self):
        exact = self.rollout('019a0000-0000-7000-8000-000000000001', self.repo)
        archived = self.rollout('019a0000-0000-7000-8000-000000000002', self.repo, archived=True)
        self.rollout('019a0000-0000-7000-8000-000000000003', self.repo/'child')
        self.rollout('019a0000-0000-7000-8000-000000000004', self.repo, timestamp='2026-11-01T00:00:00Z')
        current = '019a0000-0000-7000-8000-000000000005'
        self.rollout(current, self.repo)
        paths, skipped, scan = recall_discovery.discover('codex', self.repo, RECENT, current, home=self.home)
        self.assertEqual(set(paths), {exact, archived})
        self.assertEqual(skipped, [])
        self.assertEqual((scan['roots_found'], scan['paths_scanned']), (2, 5))

    def test_project_scoped_locations_exclude_nested_workers_and_foreign_cursor_scopes(self):
        for host in ('claude', 'cursor'):
            root, _ = recall_discovery._locations(host, self.repo, self.home)[0]
            ident = '019a0000-0000-7000-8000-000000000001'
            primary = root/(f'{ident}/{ident}.jsonl' if host == 'cursor' else 'top.jsonl')
            worker = root/'top/subagents/agent-child.jsonl'
            foreign = root.parent/'foreign/agent-transcripts/other/other.jsonl'
            for file in (primary, worker, foreign):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('export\n')
            found, ignored, _ = recall_discovery.discover(host, self.repo, RECENT, None, home=self.home)
            self.assertEqual((found, ignored), ([primary], []))

    def test_explicit_file_and_native_discovery_emit_a_single_source_audit(self):
        ident = '019a0000-0000-7000-8000-000000000001'
        active = self.rollout(ident, self.repo)
        self.rollout(ident, self.repo, archived=True)
        result = recall.collect(self.repo, self.root/'snapshot.json', codex_session=[active],
                                discover=['codex'], home=self.home, until='2026-11-27T00:00:00Z',
                                topic='receipt')
        packet = cli.read(result['path'])
        self.assertEqual(result['sessions'], 1)
        self.assertEqual(packet['history']['T1']['host'], 'codex')
        self.assertEqual(packet['history']['T1']['line'], 2)
        self.assertEqual(packet['sessions'][0]['id'], ident)
        self.assertEqual(packet['discovery'][0]['candidate_files'], 2)
        self.assertEqual(packet['skipped'][0]['host'], 'codex')
        report = recall.summarize(result['path'], result['sha256'], self.root/'report.json', model='fixture',
                                  reviewer=lambda *_: {
                                      'capsule': [{'text': 'The receipt needs attention.', 'citations': ['T1']}],
                                      'threads': [], 'problems': [],
                                      'next': {'text': 'Read the receipt.', 'citations': ['T1']},
                                  })
        self.assertIn(f'Codex chat {ident}:2', recall.markdown(report['path'], report['sha256']))

    def test_oversize_transcript_is_explained_without_parsing_the_body(self):
        path = self.rollout('019a0000-0000-7000-8000-000000000001', self.repo)
        with patch.object(recall_discovery, 'MAX_TRANSCRIPT_BYTES', 1):
            found, skipped, _ = recall_discovery.discover('codex', self.repo, None, None, home=self.home)
        self.assertFalse(found)
        self.assertEqual(skipped, [{'host': 'codex', 'file': str(path),
                                    'reason': 'indexed transcript exceeds the byte limit'}])
