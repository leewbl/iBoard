from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from workbench import worktree_usage as usage


class UsageMetadataTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.claude = Path(root.name) / 'sessions'
        self.claude.mkdir()
        self.env = patch.dict(os.environ, CLAUDE_CONFIG_DIR=root.name)
        self.env.start()
        self.addCleanup(self.env.stop)

    def session(self, pid, cwd, **other):
        path = self.claude / f'{pid}.json'
        path.write_text(json.dumps({'pid': pid, 'cwd': cwd, **other}))
        return path

    def scan(self, paths, processes, files=()):
        with patch.object(usage.sys, 'platform', 'darwin'), patch.object(usage, '_lsof', return_value=(processes, files)):
            return usage.scan(paths)

    def test_current_claude_workspace_is_independent_from_process_cwd(self):
        candidate, twin = '/repo/lower tree', '/repo/lower tree-2'
        self.session(101, candidate, sessionId='private-talk', name='private-title')
        self.session(102, twin)  # Stale PID reused by an unrelated command.
        self.session(103, twin)  # No process with this PID was observed.
        summary, matches = self.scan([candidate, twin], {101: 'claude', 102: 'python'},
                                     [(101, 'cwd', '/other/private')])
        self.assertEqual(matches, {candidate: [{'pid': 101, 'access': 'claude-session'}]})
        self.assertEqual(summary['status'], 'available')
        self.assertNotIn('private', json.dumps((summary, matches)))

    def test_unreadable_live_manifest_keeps_process_evidence_and_marks_partial_coverage(self):
        self.session(101, '/repo/active').write_text('{broken')
        summary, matches = self.scan(['/repo/active'], {101: 'claude'}, [(101, 'cwd', '/repo/active')])
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['claude_sessions']['unreadable'], 1)
        self.assertEqual(matches['/repo/active'], [{'pid': 101, 'access': 'cwd'}])

    def test_lsof_null_fields_preserve_spaces_and_select_deepest_registered_tree(self):
        raw = (b'p101\0cPython\0\nfcwd\0n/repo/tree name\0\n'
               b'f5\0n/repo/tree name/nested/file\0\nfrtd\0n/\0\n')
        processes, files = usage._lsof_records(raw)
        paths = ['/repo/tree name', '/repo/tree name/nested']
        _, matches = self.scan(paths, processes, files)
        self.assertEqual(matches, {paths[0]: [{'pid': 101, 'access': 'cwd'}],
                                   paths[1]: [{'pid': 101, 'access': 'open-file'}]})
