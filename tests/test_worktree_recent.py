import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench import recall_discovery, worktree_recent


class RecentWorktreeTests(unittest.TestCase):
    ONE = '01234567-89ab-cdef-0123-456789abcdef'
    TWO = '01234567-89ab-cdef-0123-456789abcde0'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        self.main = self.home / 'primary'
        self.tree = self.home / 'linked 中文 tree'
        self.twin = self.home / 'linked 中文 tree-extra'
        for path in (self.main, self.tree, self.twin):
            path.mkdir()
        config = patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
                            CODEX_HOME=str(self.home / '.codex'))
        config.start()
        self.addCleanup(config.stop)

    def write(self, host, file, *rows):
        (folder, _), *_ = recall_discovery._locations(host, self.main, self.home)
        target = folder / file
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return target

    def scan(self, **kwargs):
        return worktree_recent.scan(list(map(str, (self.main, self.tree, self.twin))), home=self.home, **kwargs)

    def test_reaches_a_sibling_through_claude_subagent_and_cursor_records(self):
        self.write('claude', f'{self.ONE}/subagents/agent-evidence.jsonl',
                   {'cwd':str(self.main), 'input':{'file_path':str(self.tree / 'file')}, 'private':'SECRET_SENTINEL'})
        self.write('cursor', f'{self.TWO}/{self.TWO}.jsonl',
                   {'role':'assistant', 'message':{'path':str(self.twin)}})
        with patch.object(worktree_recent, 'CHUNK', 22):
            receipt, matches = self.scan()
        self.assertEqual(receipt['status'], 'available', receipt)
        self.assertEqual(matches[str(self.tree)][0]['host'], 'claude')
        self.assertEqual(matches[str(self.tree)][0]['session_id'], 'agent-evidence')
        self.assertEqual(matches[str(self.twin)][0]['host'], 'cursor')
        self.assertNotIn('SECRET_SENTINEL', json.dumps(matches))

    def test_cutoff_and_codex_primary_scope_are_enforced(self):
        fields = {'type':'session_meta', 'payload':{'id':self.ONE, 'cwd':str(self.tree)}}
        older = self.write('codex', '2026/11/20/rollout-old.jsonl', fields)
        anchor = time.time()
        os.utime(older, (anchor - 5 * 86400, anchor - 5 * 86400))
        self.write('codex', '2026/11/28/rollout-other.jsonl',
                   {'type':'session_meta','payload':{'id':self.TWO,'cwd':'/project-not-in-inventory'}},
                   {'cwd':str(self.tree)})
        receipt, matches = self.scan(now=anchor)
        self.assertEqual(receipt['status'], 'available')
        self.assertEqual(receipt['bytes_considered'], 0)
        self.assertEqual(matches, {})

    def test_partial_byte_budget_records_unknown_coverage(self):
        self.write('claude', f'{self.ONE}.jsonl', {'cwd':str(self.tree)})
        with patch.object(worktree_recent, 'MAX_BYTES', 5):
            receipt, matches = self.scan()
        self.assertEqual(receipt['status'], 'partial')
        self.assertEqual(receipt['limit'], 'recent transcript byte limit')
        self.assertEqual(matches, {})
