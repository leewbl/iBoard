from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import investigate


class InvestigationPRDiscoveryTests(unittest.TestCase):
    repository = 'alice/project'
    endpoint_prefix = 'repos/alice/project'
    pr_url = 'https://github.com/alice/project/pull/9'

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.repo = self.scratch / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.origin = self.commit('separate handler from presentation', 0)
        for index in range(1, 10):
            self.head = self.commit(f'revise unrelated line {index}', index)
        self.assertNotIn(self.origin, self.git('log', '-8', '--format=%H', '--', 'code.txt').splitlines())
        (self.repo / 'code.txt').write_text('uncommitted replacement\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def commit(self, message, index):
        (self.repo / 'code.txt').write_text(f'selected line\nunrelated line {index}\n')
        self.git('add', 'code.txt')
        self.git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def association(self, number=9):
        return {'number':number, 'html_url':f'https://github.com/{self.repository}/pull/{number}',
                'merged_at':'2025-01-01T00:00:00Z', 'base':{'repo':{'full_name':self.repository}}}

    def detail(self, number=9):
        return {**self.association(number), 'merged':True, 'head':{'sha':'a' * 40},
                'merge_commit_sha':'b' * 40, 'title':f'Design decision for #{number}',
                'body':'Keep the handler independent of the view.', 'user':{'login':'alice'}}

    def run_why(self, *, model=None, repo=None, **options):
        answer = {'status':'ready', 'findings':[], 'unknowns':['The available record is limited.']}

        def fallback(*_args, **_kwargs):
            return {'exit_code':0, 'timed_out':False, 'stdout':json.dumps(answer)}

        return investigate.investigate(repo or self.repo, self.head, 'Why is the handler separate?',
                                        ['code.txt:1'], 'why', 120, model=model or fallback, **options)

    def lookup(self, commit):
        return f'{self.endpoint_prefix}/commits/{commit}/pulls?per_page=5&page=1'

    def test_selected_line_origin_discovers_intermediate_pr_commit_and_cited_discussion(self):
        calls = []

        def github(command, **_kwargs):
            self.assertEqual(command[:4], ['gh', 'api', '--method', 'GET'])
            self.assertEqual(command[command.index('--hostname') + 1], 'github.com')
            endpoint = command[-1]
            self.assertTrue(endpoint.startswith(self.endpoint_prefix + '/'))
            calls.append(endpoint)
            if '/commits/' in endpoint:
                return json.dumps([self.association()] if endpoint == self.lookup(self.origin) else [])
            if endpoint == f'{self.endpoint_prefix}/pulls/9':
                return json.dumps(self.detail())
            if endpoint == f'{self.endpoint_prefix}/issues/9/comments?per_page=2&page=1':
                return json.dumps([{'html_url':self.pr_url + '#issuecomment-5', 'user':{'login':'alice'},
                                    'body':'The request handler has no view dependency.'}])
            self.assertIn(endpoint, (f'{self.endpoint_prefix}/pulls/9/reviews?per_page=2&page=1',
                                     f'{self.endpoint_prefix}/pulls/9/comments?per_page=2&page=1'))
            return '[]'

        def model(_workspace, prompt, _timeout, **flags):
            self.assertEqual(flags, {'read_only':True, 'file_tools_only':True})
            self.assertNotIn('uncommitted replacement', prompt)
            evidence = json.loads(prompt.split('\n', 1)[1])['evidence']
            labels = {item.get('record_type'):label for label, item in evidence.items()}
            self.assertIn('pull-request', labels)
            self.assertIn('comment', labels)
            answer = {'status':'ready', 'findings':[{
                'text':'The PR records that the handler should have no view dependency.',
                'basis':'observed', 'citations':[{'id':labels['comment']}],
            }], 'unknowns':[]}
            return {'exit_code':0, 'timed_out':False, 'stdout':json.dumps(answer)}

        with patch.object(investigate.subprocess, 'check_output', side_effect=github):
            report = self.run_why(discover_prs=self.repository, model=model)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        self.assertEqual(calls[0], self.lookup(self.origin))
        self.assertEqual(report['pr_discovery']['repository'], self.repository)
        self.assertEqual(report['pr_discovery']['line_origins'][0], {'commit':self.origin, 'lines':1})
        self.assertEqual(report['pr_discovery']['discussions'][0]['status'], 'read')
        cited = report['stages']['why']['findings'][0]['citations'][0]['id']
        source = report['evidence'][cited]
        self.assertEqual(source['commit'], self.origin)
        self.assertEqual(source['association'], 'github-commit')
        self.assertEqual(source['pr_url'], self.pr_url)
        self.assertEqual(source['url'], self.pr_url + '#issuecomment-5')

    def test_empty_unreachable_and_foreign_responses_remain_distinguishable(self):
        calls = []

        def github(command, **_kwargs):
            endpoint = command[-1]
            calls.append(endpoint)
            self.assertIn('/commits/', endpoint)
            if endpoint == self.lookup(self.origin):
                foreign = {**self.association(), 'html_url':'https://github.com/other/repo/pull/9',
                           'base':{'repo':{'full_name':'other/repo'}}}
                return json.dumps([foreign])
            if endpoint == self.lookup(self.head):
                raise subprocess.CalledProcessError(1, command)
            return '[]'

        with patch.object(investigate.subprocess, 'check_output', side_effect=github):
            report = self.run_why(discover_prs=self.repository)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        self.assertEqual(len(calls), 6)
        lookups = {item['commit']:item['status'] for item in report['pr_discovery']['lookups']}
        self.assertEqual(lookups[self.origin], 'invalid-response')
        self.assertEqual(lookups[self.head], 'unavailable')
        self.assertIn('none', lookups.values())
        self.assertEqual(report['pr_discovery']['discussions'], [])
        self.assertFalse(any(item.get('record_type') == 'pull-request' for item in report['evidence'].values()))

    def test_discovery_deduplicates_pulls_without_reusing_evidence_labels(self):
        seen_details = []

        def github(command, **_kwargs):
            endpoint = command[-1]
            if '/commits/' in endpoint:
                pulls = ([self.association()] if endpoint == self.lookup(self.origin) else
                         [{**self.association(11), 'merged_at':None}, self.association(), self.association(10)])
                return json.dumps(pulls)
            number = endpoint.removeprefix(self.endpoint_prefix + '/pulls/')
            if number.isdigit():
                seen_details.append(int(number))
                return json.dumps(self.detail(int(number)))
            return '[]'

        with patch.object(investigate.subprocess, 'check_output', side_effect=github):
            report = self.run_why(discover_prs=self.repository)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        self.assertEqual(seen_details, [9, 10])
        evidence = [item for item in report['evidence'].values() if item.get('record_type') == 'pull-request']
        self.assertEqual([item['pr_url'].split('/')[-1] for item in evidence], ['9', '10'])

    def test_partial_discussion_keeps_available_author_records(self):
        def github(command, **_kwargs):
            endpoint = command[-1]
            if '/commits/' in endpoint:
                return json.dumps([self.association()] if endpoint == self.lookup(self.origin) else [])
            if endpoint == f'{self.endpoint_prefix}/pulls/9':
                return json.dumps(self.detail())
            if endpoint == f'{self.endpoint_prefix}/issues/9/comments?per_page=2&page=1':
                raise subprocess.CalledProcessError(1, command)
            if endpoint == f'{self.endpoint_prefix}/pulls/9/reviews?per_page=2&page=1':
                return json.dumps([{'html_url':self.pr_url + '#pullrequestreview-12',
                                    'user':{'login':'reviewer'}, 'body':'The old pipeline depended on this.'}])
            return '[]'

        with patch.object(investigate.subprocess, 'check_output', side_effect=github):
            report = self.run_why(discover_prs=self.repository)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        discussion = report['pr_discovery']['discussions'][0]
        self.assertEqual(discussion['status'], 'partial')
        self.assertTrue(discussion['gaps'])
        records = {item['record_type']:item for item in report['evidence'].values()
                   if item.get('pr_url') == self.pr_url}
        self.assertEqual(set(records), {'pull-request', 'review'})
        self.assertEqual(records['review']['url'], self.pr_url + '#pullrequestreview-12')
        self.assertEqual(records['review']['author'], 'reviewer')

    def test_retrieved_pull_details_must_confirm_the_associated_repository(self):
        def github(command, **_kwargs):
            endpoint = command[-1]
            if '/commits/' in endpoint:
                return json.dumps([self.association()] if endpoint == self.lookup(self.origin) else [])
            self.assertEqual(endpoint, f'{self.endpoint_prefix}/pulls/9')
            return json.dumps({**self.detail(), 'base':{'repo':{'full_name':'other/repo'}},
                               'title':'Unverified author statement'})

        with patch.object(investigate.subprocess, 'check_output', side_effect=github):
            report = self.run_why(discover_prs=self.repository)
        self.assertEqual(report['status'], 'complete', report.get('reason'))
        self.assertEqual(report['pr_discovery']['discussions'][0]['status'], 'unavailable')
        self.assertFalse(any(item.get('record_type') == 'pull-request' for item in report['evidence'].values()))

    def test_shallow_boundary_requires_selected_path_from_pull_files(self):
        shallow = self.scratch / 'shallow'
        self.git('clone', '--quiet', '--depth', '1', self.repo.as_uri(), str(shallow))
        self.assertEqual(subprocess.check_output(
            ['git', '-C', str(shallow), 'rev-parse', '--is-shallow-repository'], text=True).strip(), 'true')
        pr_detail = f'{self.endpoint_prefix}/pulls/9'
        pr_files = pr_detail + '/files?per_page=30&page=1'

        for files, confirmed in (([{'filename':'other/path.ts'}], False),
                                 ([{'filename':'handler.txt', 'previous_filename':'code.txt', 'status':'renamed'}], True)):
            with self.subTest(files=files):
                calls = []

                def github(command, **_kwargs):
                    endpoint = command[-1]
                    calls.append(endpoint)
                    if endpoint == self.lookup(self.head):
                        return json.dumps([{**self.association(), 'title':'Author rationale for confirmed paths'}])
                    if endpoint == pr_files:
                        return json.dumps(files)
                    self.assertTrue(confirmed, f'unconfirmed PR requested {endpoint}')
                    if endpoint == pr_detail:
                        return json.dumps(self.detail())
                    self.assertIn(endpoint, (f'{self.endpoint_prefix}/issues/9/comments?per_page=2&page=1',
                                             f'{pr_detail}/reviews?per_page=2&page=1',
                                             f'{pr_detail}/comments?per_page=2&page=1'))
                    return '[]'

                with patch.object(investigate.subprocess, 'check_output', side_effect=github):
                    report = self.run_why(repo=shallow, discover_prs=self.repository)
                self.assertEqual(report['status'], 'complete', report.get('reason'))
                discovery = report['pr_discovery']
                self.assertIs(discovery['shallow_history'], True)
                self.assertEqual(discovery['line_origins'][0]['commit'], self.head)
                self.assertEqual(calls[:2], [self.lookup(self.head), pr_files])
                discussion = discovery['discussions'][0]
                pr_evidence = [entry for entry in report['evidence'].values() if entry.get('pr_url') == self.pr_url]
                if confirmed:
                    self.assertEqual(discussion['status'], 'read')
                    self.assertEqual(discussion['matching_paths'], ['code.txt'])
                    self.assertEqual(calls[2], pr_detail)
                    self.assertEqual(len(pr_evidence), 1)
                else:
                    self.assertEqual(discussion['status'], 'path-unconfirmed')
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(pr_evidence, [])

    def test_discovery_is_opt_in_and_rejects_prohibited_options_before_network(self):
        with patch.object(investigate.subprocess, 'check_output') as github:
            report = self.run_why()
            self.assertEqual(report['status'], 'complete', report.get('reason'))
            self.assertNotIn('pr_discovery', report)
            for options in ({'discover_prs':self.repository, 'pull_url':self.pr_url},
                            {'discover_prs':'https://other.invalid/alice/project'}):
                with self.subTest(options=options), self.assertRaises((investigate.InvestigationError, ValueError)):
                    self.run_why(**options)
            with self.assertRaises(investigate.InvestigationError):
                investigate.investigate(self.repo, self.head, 'How?', ['code.txt:1'], 'how', 120,
                                         discover_prs=self.repository)
            github.assert_not_called()

    def test_cli_forwards_the_explicit_github_repository(self):
        destination = self.scratch / 'investigation.json'
        with patch.object(investigate, 'investigate', return_value={'status':'complete'}) as run:
            with redirect_stdout(StringIO()):
                result = investigate.main(['why', '--repo', str(self.repo), '--revision', self.head,
                                           '--question', 'Why?', '--file', 'code.txt:1',
                                           '--discover-prs', self.repository, '--report', str(destination)])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.kwargs['discover_prs'], self.repository)
        self.assertEqual(json.loads(destination.read_text()), {'status':'complete'})
