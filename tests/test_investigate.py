import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import investigate


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.repo = Path(scratch.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'code.txt').write_text('committed entry\ncommitted handler\n')
        self.git('add', 'code.txt')
        self.git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
                 'commit', '-qm', 'Keep handler independent of presentation')
        self.head = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'code.txt').write_text('uncommitted changes\n')
        (self.repo / 'local-secret.txt').write_text('UNTRACKED_SENTINEL')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def test_teach_combines_isolated_source_and_history_calls(self):
        def model(workspace, prompt, timeout, *, read_only, file_tools_only):
            self.assertTrue(read_only and file_tools_only)
            self.assertFalse(any(workspace.iterdir()))
            self.assertNotIn('UNTRACKED_SENTINEL', prompt)
            self.assertNotIn('uncommitted changes', prompt)
            inputs = json.loads(prompt.split('\n', 1)[-1])
            evidence = inputs['evidence']
            if inputs['prior']:
                self.assertEqual(set(inputs['prior']), {'how', 'why'})
                references = [{'id':'S1', 'line':2}, {'id':'H1'}]
                text = 'The handler is separate from presentation.'
            elif 'S1' in evidence:
                self.assertEqual(set(evidence), {'S1'})
                self.assertEqual(evidence['S1']['text'], '2: committed handler')
                references, text = [{'id':'S1', 'line':2}], 'The handler is committed.'
            else:
                self.assertEqual(set(evidence), {'H1'})
                self.assertEqual(evidence['H1']['commit'], self.head)
                references, text = [{'id':'H1'}], 'The commit states the separation goal.'
            answer = {'status':'ready', 'findings':[{'text':text, 'basis':'observed', 'citations':references}],
                      'unknowns':['No runtime measurements were provided.']}
            return {'exit_code':0, 'timed_out':False, 'stdout':json.dumps(answer)}

        report = investigate.investigate(self.repo, self.head, 'Explain the design.', ['code.txt:2'],
                                         'teach', 30, model=model)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        self.assertEqual(report['commit'], self.head)
        self.assertIn('why', report['stages']['teach']['upstream_unknowns'])
        self.assertEqual((self.repo / 'code.txt').read_text(), 'uncommitted changes\n')

    def test_invalid_proof_blocks_investigation(self):
        answer = {'status':'ready', 'findings':[{'text':'Unsupported claim.', 'basis':'observed',
                                               'citations':[{'id':'S1', 'line':900}]}], 'unknowns':[]}
        result = {'exit_code':0, 'timed_out':False, 'stdout':json.dumps(answer)}
        report = investigate.investigate(self.repo, self.head, 'How?', ['code.txt'], 'how', 30,
                                         model=lambda *a, **kw: result)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('outside the supplied source', report['reason'])

    def test_cli_refuses_reports_inside_source_repository_before_model_call(self):
        with patch.object(investigate, 'investigate') as run, self.assertRaises(SystemExit):
            investigate.main(['how', '--repo', str(self.repo), '--question', 'How?', '--file', 'code.txt',
                              '--report', str(self.repo / 'output.json')])
        run.assert_not_called()
        self.assertFalse((self.repo / 'output.json').exists())

    def test_pr_discussion_requires_a_linked_commit_and_carries_its_origin(self):
        url = 'https://github.com/alice/repo/pull/9'
        metadata = {'number':9, 'html_url':url, 'head':{'sha':'a' * 40},
                    'title':'Separate presentation', 'body':'Keep the handler reusable.'}
        repo = 'repos/alice/repo'

        def gh(command, **kwargs):
            endpoint = command[-1]
            self.assertEqual(command[:4], ['gh', 'api', '--method', 'GET'])
            if endpoint == f'{repo}/pulls/9':
                return json.dumps(metadata)
            if endpoint == f'{repo}/issues/9/comments?per_page=5&page=1':
                return json.dumps([{'html_url':url + '#issuecomment-5', 'user':{'login':'alice'},
                                    'body':'The request handler has no view dependencies.'}])
            return '[]'

        with patch.object(investigate.subprocess, 'check_output', side_effect=gh) as fetch:
            with self.assertRaisesRegex(investigate.InvestigationError, 'absent from the selected'):
                investigate._pull_discussion(url, {self.head}, lambda:20)
            self.assertEqual(fetch.call_count, 1)
            metadata.update(merged=True, merge_commit_sha=self.head)
            report = investigate._pull_discussion(url, {self.head}, lambda:20)
        self.assertEqual(report['P2']['url'], url + '#issuecomment-5')
        self.assertEqual(report['P2']['commit'], self.head)
        self.assertEqual(report['P2']['author'], 'alice')
