import json
import argparse
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
        data = cli.read(self.task)
        review_path = self.task.parent / 'new-independent-review.json'
        review_path.write_text(json.dumps({'verdict': 'PASS', 'guard_assessments': [], 'reports': {}}))
        cli.evidence_add(argparse.Namespace(file=str(review_path), kind='independent-review', commit=head,
                          actor='independent-verifier', description='new candidate review', criterion=['c1']), data)
        cli.write(self.task, data)
        local_gate.validate(cli.read(self.task), head)


if __name__ == '__main__': unittest.main()
