import json
import argparse
import hashlib
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from workbench import cli, delivery, local_gate
from test_delivery import DeliveryTests


class LocalGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DeliveryTests(methodName='test_good_evidence_and_strict_checks_permit_merge')
        self.fixture.setUp()
        self.task, self.github = self.fixture.task, self.fixture.github
        self.github.actions_permissions = lambda repo: {'enabled': False}
        self.fixture.runcheck('test-result', 'independent-verifier', 'after', 0, 'PASS')
        self.image = 'sha256:' + 'a' * 64
        self.command = ['node', 'fixed-vitest']
        self.command_patch = patch('workbench.local_vitest.command', return_value=self.command)
        self.json_patch = patch('workbench.local_vitest.validate_json', return_value=1)
        self.command_patch.start()
        self.json_patch.start()
        data = cli.read(self.task)
        for e in data['evidence']:
            if e['kind'] == 'test-result' and e['actor'] in ('implementer', 'independent-verifier'):
                path = Path(e['path'])
                report = json.loads(path.read_text())
                report.update(command=self.command, source_mode='image', image=self.image,
                              verified_image_id=self.image, expected_exit=0, expected_marker='', stdout='{}')
                path.write_text(json.dumps(report))
                e['sha256'] = cli.digest(path)
        data['status'] = 'paused'
        cli.write(self.task, data)
        local_gate.configure(self.task, ['src/unit.test.ts'], 'Explicit sandbox unit gate')
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        paths = [next(e['path'] for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == actor)
                 for actor in ('implementer', 'independent-verifier')]
        local_gate.record_run(self.task, self.fixture.head, self.image, [{'report': p} for p in paths])
        self.bind_review(self.fixture.head)
        self.github.protected = False
        self.github.ci = False
        self.github.branch_rules = lambda repo, branch: []

    def tearDown(self):
        self.command_patch.stop()
        self.json_patch.stop()
        self.fixture.tearDown()

    def test_explicit_local_gate_allows_guarded_personal_fast_forward(self):
        self.assertTrue(delivery.guarded_merge(self.task, 'alice/project', 1, self.github)['merged'])
        self.assertTrue(self.github.pushed)

    def test_protected_branch_and_rules_are_rejected(self):
        self.github.protected = True
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.github.protected = False
        self.github.branch_rules = lambda repo, branch: [{'type': 'required_status_checks'}]
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_changed_acceptance_or_command_is_rejected(self):
        for key in ('criteria', 'local_gate'):
            with self.subTest(key=key):
                data = cli.read(self.task)
                original = json.loads(json.dumps(data))
                if key == 'criteria': data[key][0]['text'] = 'different'
                else: data[key]['command'] = ['sh', '-c', 'echo PASS']
                cli.write(self.task, data)
                with self.assertRaises(delivery.DeliveryError):
                    delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
                cli.write(self.task, original)

    def test_mismatched_candidate_image_is_rejected(self):
        data = cli.read(self.task)
        e = next(e for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == 'independent-verifier')
        path = Path(e['path'])
        report = json.loads(path.read_text())
        report['image'] = report['verified_image_id'] = 'sha256:' + 'b' * 64
        path.write_text(json.dumps(report))
        e['sha256'] = cli.digest(path)
        cli.write(self.task, data)
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)

    def test_tampered_report_is_rejected(self):
        data = cli.read(self.task)
        e = next(e for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == 'independent-verifier')
        Path(e['path']).write_text('{}')
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)

    def test_rules_change_before_push_is_rejected(self):
        count = 0
        def rules(*args):
            nonlocal count
            count += 1
            return [] if count < 4 else [{'type': 'required_status_checks'}]
        self.github.branch_rules = rules
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_repair_new_head_requires_two_new_reports_and_new_image(self):
        workspace = self.fixture.workspace
        (workspace / 'app').write_text('fixed again')
        subprocess.run(['git', '-C', str(workspace), 'add', 'app'], check=True)
        subprocess.run(['git', '-C', str(workspace), 'commit', '-qm', 'repair'], check=True)
        head = cli.sha(workspace, 'HEAD')
        data = cli.read(self.task)
        with self.assertRaises(local_gate.LocalGateError):
            local_gate.validate(data, head)
        for actor in ('implementer', 'independent-verifier'):
            self.fixture.runcheck('test-result', actor, 'after', 0, 'PASS')
        data = cli.read(self.task)
        image = 'sha256:' + 'b' * 64
        reports = []
        for actor in ('implementer', 'independent-verifier'):
            e = next(e for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == actor and e['commit'] == head)
            path = Path(e['path'])
            report = json.loads(path.read_text())
            report.update(command=self.command, source_mode='image', image=image,
                          verified_image_id=image, expected_exit=0, expected_marker='', stdout='{}')
            path.write_text(json.dumps(report))
            e['sha256'] = cli.digest(path)
            reports.append({'report': str(path)})
        cli.write(self.task, data)
        local_gate.record_run(self.task, head, image, reports)
        self.bind_review(head)
        local_gate.validate(cli.read(self.task), head)

    def bind_review(self, head):
        data = cli.read(self.task)
        run = next(e for e in reversed(data['events']) if e.get('type') == 'local-gate-run' and e.get('head') == head)
        independent = run['reports'][1]
        previous = next(e for e in data['evidence'] if e['kind'] == 'independent-review')
        review = json.loads(Path(previous['path']).read_text())
        review.update(candidate_commit=head, baseline_commit=data['base_commit'],
                      criterion_digest=cli.criteria_digest(data), guard_assessments=[])
        review['reports']['GUARD'] = {'path': independent['path'], 'sha256': independent['sha256']}
        review['reports']['DIFF'] = {'kind': 'git-diff', 'base': data['base_commit'], 'head': head,
            'sha256': hashlib.sha256(delivery.candidate_diff(data['repo'], data['base_commit'], head)).hexdigest()}
        path = self.task.parent / ('guard-review-' + head + '.json')
        path.write_text(json.dumps(review))
        cli.evidence_add(argparse.Namespace(file=str(path), kind='independent-review', commit=head,
                        actor='independent-verifier', description='bound independent guard review', criterion=['c1']), data)
        cli.write(self.task, data)

    def test_missing_independent_review_is_rejected(self):
        data = cli.read(self.task)
        data['evidence'] = [e for e in data['evidence'] if e['kind'] != 'independent-review']
        with self.assertRaisesRegex(local_gate.LocalGateError, 'independent review'):
            local_gate.validate(data, self.fixture.head)

    def test_wrong_diff_or_old_report_in_review_is_rejected(self):
        data = cli.read(self.task)
        evidence = next(e for e in reversed(data['evidence']) if e['kind'] == 'independent-review')
        path = Path(evidence['path'])
        original = json.loads(path.read_text())
        for field in ('diff', 'report'):
            with self.subTest(field=field):
                review = json.loads(json.dumps(original))
                if field == 'diff': review['reports']['DIFF']['sha256'] = '0' * 64
                else: review['reports']['GUARD']['sha256'] = '0' * 64
                path.write_text(json.dumps(review))
                evidence['sha256'] = cli.digest(path)
                with self.assertRaisesRegex(local_gate.LocalGateError, 'not bound'):
                    local_gate.validate(data, self.fixture.head)
        path.write_text(json.dumps(original))


if __name__ == '__main__': unittest.main()
