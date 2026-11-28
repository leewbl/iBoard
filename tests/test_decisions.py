from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench import cli, decisions


class DecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        repo = root / 'repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        cli.git(repo, '-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid',
                'commit', '--allow-empty', '-qm', 'base')
        self.head = cli.sha(repo, 'HEAD')
        self.task = root / 'task.json'
        self.log = root / 'decisions.tsv'
        cli.write(self.task, {'schema':cli.SCHEMA, 'repo':str(repo), 'id':'audit'})

    def test_append_preserves_history_and_marks_handoff_with_escaped_cells(self):
        self.log.write_text(decisions.HEADER)
        decisions.record(self.task, 'first', 'plan', ' \t=2+2\nchosen', 'reason',
                         'commit:' + self.head, '@check')
        before = self.log.read_bytes()
        decisions.record(self.task, 'second', 'verify', 'Corrected decision', 'Observed output',
                         'https://github.com/test/fork/pull/7', 'Pull request recorded')
        decisions.record(self.task, 'second', 'handoff', 'Retained the same commit', 'Source still matches',
                         'commit:' + self.head, 'Inspection complete')
        self.assertTrue(self.log.read_bytes().startswith(before))
        report = decisions.audit(self.task)
        rows = report['rows']
        self.assertEqual([row['phase'] for row in rows], ['start', 'plan', 'start', 'verify', 'handoff'])
        self.assertEqual(rows[1]['decision'], "'=2+2 chosen")
        self.assertEqual(rows[1]['result'], "'@check")
        self.assertIn(rows[1]['ts'], rows[2]['why'])
        self.assertEqual(rows[3]['evidence_status'], 'remote-not-checked')
        self.assertEqual(rows[4]['evidence_status'], 'commit-resolves')
        self.assertEqual([row['row'] for row in decisions.audit(self.task, run='second')['rows']], [4, 5, 6])
        with self.assertRaisesRegex(decisions.DecisionError, 'does not exist'):
            decisions.audit(self.task, run='unknown')

    def test_audit_detects_missing_or_replaced_artifacts_without_rewriting_rows(self):
        artifact = self.task.with_name('report #1.json')
        artifact.write_text('original evidence')
        proof = decisions.artifact(self.task, artifact)
        decisions.record(self.task, 'first', 'verify', 'Accepted runtime evidence', 'The output matched the scenario',
                         proof, 'Verified')
        old_rows = self.log.read_bytes()
        self.assertEqual(decisions.audit(self.task)['unresolved_rows'], [])
        artifact.write_text('different bytes')
        self.assertEqual(decisions.audit(self.task)['rows'][-1]['evidence_status'], 'artifact-changed')
        artifact.unlink()
        report = decisions.audit(self.task)
        self.assertEqual(report['unresolved_rows'], [3])
        self.assertEqual(report['rows'][-1]['evidence_status'], 'artifact-unavailable')
        self.assertEqual(self.log.read_bytes(), old_rows)

    def test_malformed_log_and_symlink_are_not_overwritten(self):
        self.log.write_text(decisions.HEADER + 'torn row')
        with self.assertRaisesRegex(decisions.DecisionError, 'incomplete'):
            decisions.begin(self.task)
        self.assertEqual(self.log.read_text(), decisions.HEADER + 'torn row')
        self.log.unlink()
        self.log.symlink_to(self.task)
        before = self.task.read_bytes()
        with self.assertRaises(OSError):
            decisions.begin(self.task)
        self.assertEqual(self.task.read_bytes(), before)
