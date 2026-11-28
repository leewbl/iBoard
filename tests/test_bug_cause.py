from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from workbench import bug_cause, cli, delivery


class CausalVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'workspace'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'verify.mjs').write_text("import './app.mjs';\n")
        (self.repo / 'app.mjs').write_text('const failure = true;\nexport const output = failure;\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        self.data = {'kind': 'bug', 'base_commit': self.base, 'repo': str(self.repo),
                     'root_cause': bug_cause.MODE, 'implementer': 'owner', 'evidence': []}
        original = ['node', '/workspace/verify.mjs', 'trigger']
        control = ['node', '/workspace/verify.mjs', 'control']
        self.plan = {'reproduction': {'command': original, 'exit_code': 0, 'marker': 'OBSERVED_BUG'},
                     'resolution': {'command': original.copy(), 'exit_code': 0, 'marker': 'OBSERVED_OK'},
                     'root_cause': {
                         'mechanism': {'claim': 'The trigger sets failure.', 'path': 'app.mjs', 'line': 1},
                         'alternative': {'claim': 'The output path may always fail.', 'path': 'app.mjs', 'line': 2},
                         'control': {'command': control, 'exit_code': 0, 'marker': 'OBSERVED_OK'}}}

    def candidate(self, driver_changed=False):
        (self.repo / 'app.mjs').write_text('export const output = false;\n')
        if driver_changed:
            (self.repo / 'verify.mjs').write_text('// The old driver was replaced.\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Candidate')
        return cli.sha(self.repo, 'HEAD')

    def pair(self, commit, kind, spec, *, dual_marker=False):
        paths = []
        for actor in ('owner', 'independent'):
            path = self.root / f'run-{len(self.data["evidence"])}.json'
            cli.write(path, {'schema': cli.SCHEMA, 'actor': actor, 'kind': kind, 'commit': commit,
                             'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                             'exit_code': spec['exit_code'], 'expected_exit': spec['exit_code'],
                             'expected_marker': spec['marker'], 'command': spec['command'],
                             'stdout': spec['marker'] + ('\nOBSERVED_BUG' if dual_marker else ''),
                             'stderr': '', 'source_mode': 'workspace', 'source_verified_commit': commit,
                             'workspace': str(self.repo), 'image': 'sha256:' + 'a' * 64,
                             'create_stdout': f'{len(self.data["evidence"]):064x}\n',
                             'create_stdout_truncated': False, 'workspace_read_only': True})
            paths.append(path)
            self.data['evidence'].append({'actor': actor, 'kind': kind, 'commit': commit,
                                          'path': str(path), 'sha256': cli.digest(path)})
        return bug_cause.references(paths)

    def record(self, head, *, dual_marker=False):
        verdict = {'mechanism': 'supported', 'alternative': 'ruled-out',
                   'reason': 'The same output path is healthy with the stimulus removed.'}
        model = self.root / 'cause-baseline-fixture.json'
        cli.write(model, {'exit_code': 0, 'timed_out': False,
                          'stdout': json.dumps({'status': 'ready', 'verdict': 'PASS', 'cause_verdict': verdict})})
        return {'head': head, 'explanation': 'The trigger and control now behave as expected.',
                'baseline': {'plan': self.plan, 'source_mode': 'workspace', 'verdict': verdict,
                             'review_receipt': bug_cause.references([model])[0],
                             'trigger': self.pair(self.base, 'reproduction', self.plan['reproduction']),
                             'control': self.pair(self.base, 'runtime-log', self.plan['root_cause']['control'])},
                'trigger': self.pair(head, 'resolution', self.plan['resolution']),
                'control': self.pair(head, 'runtime-log', self.plan['root_cause']['control'], dual_marker=dual_marker)}

    def probe_plan(self):
        previous = self.plan['root_cause']
        self.plan['root_cause'] = {'mechanism': previous['mechanism'], 'probes': [
            {'id': 'output', 'alternative': previous['alternative'], 'control': previous['control']},
            {'id': 'timing', 'alternative': {'claim': 'Slow scheduling creates the failure.',
                                             'path': 'app.mjs', 'line': 2},
             'control': {'command': ['node', '/workspace/verify.mjs', 'timing'],
                         'marker': 'OBSERVED_ON_TIME', 'exit_code': 0}}]}

    def probe_record(self, head):
        self.probe_plan()
        plan = self.plan
        proof = bug_cause.contract(self.data, plan)
        verdict = {'mechanism': 'supported', 'trigger_quote': plan['reproduction']['marker'], 'probes': {
            name: {'verdict': 'ruled-out', 'reason': f'The {name} behavior is healthy.',
                   'quote': probe['control']['marker']} for name, probe in proof['probes'].items()}}
        model = self.root / 'cause-baseline-fixture.json'
        cli.write(model, {'exit_code': 0, 'timed_out': False,
                          'stdout': json.dumps({'status': 'ready', 'verdict': 'PASS', 'cause_verdict': verdict})})

        def controls(commit):
            return {name: self.pair(commit, 'runtime-log', item['control']) for name, item in proof['probes'].items()}

        return {'head': head, 'explanation': 'The trigger and both controls now behave as expected.',
                'baseline': {'plan': plan, 'source_mode': 'workspace', 'verdict': verdict,
                             'review_receipt': bug_cause.references([model])[0],
                             'trigger': self.pair(self.base, 'reproduction', plan['reproduction']),
                             'probe_runs': controls(self.base)},
                'trigger': self.pair(head, 'resolution', plan['resolution']), 'probe_runs': controls(head)}

    def test_frozen_intake_and_missing_model_verdict_cannot_be_skipped(self):
        task = self.root / 'task.json'
        cli.write(task.with_name('input-stage.json'), {'schema': cli.SCHEMA, 'root_cause': bug_cause.MODE})
        self.assertTrue(bug_cause.require_intake(self.data, cli.read(task.with_name('input-stage.json'))))
        with self.assertRaisesRegex(delivery.DeliveryError, 'approved causal verification intake'):
            delivery.require_refactor_intake(task, {'kind': 'bug'})
        with self.assertRaisesRegex(bug_cause.CauseError, 'rule out'):
            bug_cause.baseline_verdict({'mechanism': 'supported', 'alternative': 'inconclusive', 'reason': 'Unknown.'})

    def test_different_entrypoint_or_redefined_trigger_rejected(self):
        accepted = bug_cause.contract(self.data, self.plan)
        self.assertIn('failure', accepted['mechanism']['source'])
        plan = deepcopy(self.plan)
        plan['resolution']['command'][2] = 'ignored'
        with self.assertRaisesRegex(bug_cause.CauseError, 'same tracked entrypoint'):
            bug_cause.contract(self.data, plan)
        plan = deepcopy(self.plan)
        plan['root_cause']['control']['command'] = ['node', '/workspace/app.mjs', 'other']
        with self.assertRaisesRegex(bug_cause.CauseError, 'same tracked entrypoint'):
            bug_cause.contract(self.data, plan)

    def test_exact_comparison_only_and_retained_verdict_hash(self):
        candidate = self.candidate()
        record = self.record(candidate)
        bug_cause.validate(self.data, candidate, record, 'independent')
        receipt = Path(record['baseline']['review_receipt']['path'])
        receipt.write_text('{}')
        with self.assertRaisesRegex(bug_cause.CauseError, 'receipt changed'):
            bug_cause.validate(self.data, candidate, record, 'independent')

    def test_healthy_marker_does_not_cancel_the_original_fault(self):
        candidate = self.candidate()
        record = self.record(candidate, dual_marker=True)
        with self.assertRaisesRegex(bug_cause.CauseError, 'observed output'):
            bug_cause.validate(self.data, candidate, record, 'independent')

    def test_candidate_cannot_change_the_tracked_probe(self):
        candidate = self.candidate(driver_changed=True)
        with self.assertRaisesRegex(bug_cause.CauseError, 'frozen causal driver'):
            bug_cause.validate(self.data, candidate, self.record(candidate), 'independent')

    def test_distinct_probe_commands_and_complete_candidate_reruns_are_required(self):
        head = self.candidate()
        record = self.probe_record(head)
        bug_cause.validate(self.data, head, record, 'independent')
        record['probe_runs'].pop('timing')
        with self.assertRaisesRegex(bug_cause.CauseError, 'every frozen causal probe'):
            bug_cause.validate(self.data, head, record, 'independent')
        probes = self.plan['root_cause']['probes']
        probes[1]['control']['command'] = probes[0]['control']['command']
        with self.assertRaisesRegex(bug_cause.CauseError, 'control command must be distinct'):
            bug_cause.contract(self.data, self.plan)

    def test_each_exclusion_requires_a_quote_from_its_two_original_runs(self):
        record = self.probe_record(self.candidate())
        baseline = record['baseline']
        proof = bug_cause.contract(self.data, self.plan)
        verdict = deepcopy(baseline['verdict'])
        verdict['trigger_quote'] = 'UNOBSERVED_TRIGGER'
        with self.assertRaisesRegex(bug_cause.CauseError, 'quote is missing from the raw trigger'):
            bug_cause.baseline_verdict(verdict, baseline, proof)
        verdict['trigger_quote'] = baseline['verdict']['trigger_quote']
        verdict['probes']['timing']['quote'] = verdict['probes']['output']['quote']
        with self.assertRaisesRegex(bug_cause.CauseError, 'quote is missing from the raw probe timing'):
            bug_cause.baseline_verdict(verdict, baseline, proof)
        verdict['probes'].pop('timing')
        with self.assertRaisesRegex(bug_cause.CauseError, 'decide every competing hypothesis'):
            bug_cause.baseline_verdict(verdict, baseline, proof)

    def test_a_rehashed_second_probe_cannot_reuse_a_container(self):
        head = self.candidate()
        record = self.probe_record(head)
        other = cli.read(Path(record['probe_runs']['output'][0]['path']))
        ref = record['probe_runs']['timing'][1]
        path = Path(ref['path'])
        receipt = cli.read(path)
        receipt['create_stdout'] = other['create_stdout']
        cli.write(path, receipt)
        entry = next(item for item in self.data['evidence'] if item['path'] == str(path))
        ref['sha256'] = entry['sha256'] = cli.digest(path)
        with self.assertRaisesRegex(bug_cause.CauseError, 'fresh read-only Docker container'):
            bug_cause.validate(self.data, head, record, 'independent')


if __name__ == '__main__':
    unittest.main()
