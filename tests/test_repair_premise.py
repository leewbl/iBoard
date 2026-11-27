import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workbench import autopilot, cli, repair_premise, workflow


class RepairPremiseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        repo = root / 'repo'
        repo.mkdir()
        cli.git(repo, 'init', '-q')
        cli.git(repo, 'config', 'user.name', 'Test')
        cli.git(repo, 'config', 'user.email', 'test@example.invalid')
        (repo / 'settings.json').write_text('{"legacyMode":"broken","route":"broken"}')
        (repo / 'app.js').write_text('exports.resolve = (settings) => settings.route;\n')
        (repo / 'repro.js').write_text(
            "console.log(require('./app').resolve(require('./settings.json')).toUpperCase());\n")
        (repo / 'probe.js').write_text(
            "const resolve = require('./app').resolve;\n"
            "const result = resolve({legacyMode: 'fixed', route: 'broken'});\n"
            "console.log(result === 'fixed' ? 'USES_LEGACY_MODE' : 'IGNORES_LEGACY_MODE');\n")
        cli.git(repo, 'add', '.')
        cli.git(repo, 'commit', '-qm', 'baseline')
        info = workflow.prepare(task_id='premise', repo=repo, tasks_root=root / 'tasks',
                                workspaces_root=root / 'workspaces', source='reported bug', kind='bug',
                                surface='non-ui', iterations=4, seconds=240, implementer='implementer')
        self.task = Path(info['task'])
        self.workspace = Path(info['workspace'])
        self.issue = root / 'issue.txt'
        self.issue.write_text('The runtime should resolve the route to fixed. Check via repro.js.\n')
        self.probe = {'command': ['node', '/workspace/probe.js'], 'exit_code': 0,
                      'if_supported': 'USES_LEGACY_MODE', 'if_refuted': 'IGNORES_LEGACY_MODE'}
        self.events = []

    def model(self, workspace, prompt, timeout, *, read_only):
        def check(marker):
            return {'command': ['node', '/workspace/repro.js'], 'marker': marker, 'exit_code': 0}

        output = {'status': 'ready'}
        if 'Return only JSON:' in prompt:
            output.update(criteria=[{'id': 'route', 'text': 'The active route resolves to fixed.'}],
                          reproduction=check('BROKEN'), resolution=check('FIXED'), tests=check('FIXED'))
        elif 'actual baseline run' in prompt:
            output['verdict'] = 'PASS'
        elif 'Implement this explicitly' in prompt:
            self.assertFalse(read_only)
            self.events.append('first-attempt')
            (workspace / 'settings.json').write_text('{"legacyMode":"fixed","route":"broken"}')
        elif 'Diagnose the shared premise' in prompt:
            self.assertTrue(read_only)
            self.assertIn('FIRST FIX DIFF', prompt)
            self.assertIn('NEXT FIX DIFF', prompt)
            self.events.append('diagnose')
            output.update(premise='The runtime uses legacyMode to decide the active route.', probe=self.probe)
        elif 'Repair the current candidate' in prompt:
            self.assertFalse(read_only)
            if '"outcome": "refuted"' in prompt:
                self.events.append('grounded-repair')
                (workspace / 'settings.json').write_text('{"legacyMode":"fixed","route":"fixed"}')
            else:
                self.events.append('first-repair')
                (workspace / 'settings.json').write_text('{"legacyMode":"FIXED","route":"broken"}')
        else:
            output.update(verdict='PASS', assessments=[
                {'id': 'route', 'verdict': 'PASS', 'reason': 'The active route was observed.', 'evidence': ['R1']}])
        return {'exit_code': 0, 'timed_out': False, 'stdout': json.dumps(output), 'stderr': ''}

    def sandbox_fixture(self, workspace, image, command, timeout, **kwargs):
        if command == self.probe['command']:
            stdout = 'IGNORES_LEGACY_MODE\n'
        else:
            settings = json.loads((Path(workspace) / 'settings.json').read_text())
            stdout = settings['route'].upper() + '\n'
        return {'command': command, 'workspace': str(workspace), 'image': 'sha256:' + 'a' * 64,
                'source_root': kwargs.get('source_root', '/workspace'),
                'workspace_read_only': kwargs.get('workspace_read_only', False),
                'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                'exit_code': 0, 'stdout': stdout, 'stderr': ''}

    def check_fixture(self, **kwargs):
        return workflow.run_check(**kwargs, runner=self.sandbox_fixture)

    def run_case(self, check):
        return autopilot.run(self.task, self.issue, 'node:24-slim', model=self.model,
                             check=check, memory_mb=128, local_handoff=True)

    def proof(self):
        path, = self.task.parent.glob('repair-premise-*.json')
        return cli.read(path)

    def assert_result(self, state):
        self.assertEqual(self.events, ['first-attempt', 'first-repair', 'diagnose', 'grounded-repair'])
        self.assertEqual(state['phase'], 'delivery')
        proof = self.proof()
        self.assertEqual(proof['outcome'], 'refuted')
        self.assertEqual(len({row['head'] for row in proof['failures']}), 2)
        self.assertEqual(proof['failures'][1]['head'], proof['head'])
        self.assertEqual(proof['head'], cli.sha(self.workspace, 'HEAD^'))
        self.assertTrue(proof['sandbox']['workspace_read_only'])
        self.assertTrue(proof['sandbox']['cleanup_confirmed'])
        self.assertEqual(cli.read(self.task)['iterations'], 3)
        self.assertFalse(cli.evidence_gate(cli.read(self.task), state['candidate_commit'])[1])
        self.assertTrue(all('repair-premise-' not in item['path'] for item in cli.read(self.task)['evidence']))

    def test_harness_runs_probe_and_then_repairs_against_unchanged_acceptance(self):
        with patch.object(repair_premise, 'run_sandbox', self.sandbox_fixture):
            self.assert_result(self.run_case(self.check_fixture))

    def test_ambiguous_probe_holds_next_edit_and_keeps_both_failures(self):
        def sandbox(*args, **kwargs):
            result = self.sandbox_fixture(*args, **kwargs)
            return {**result, 'stdout': 'USES_LEGACY_MODE\nIGNORES_LEGACY_MODE\n'}

        with patch.object(repair_premise, 'run_sandbox', sandbox):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'premise probe was inconclusive'):
                self.run_case(self.check_fixture)
        self.assertEqual(self.events, ['first-attempt', 'first-repair', 'diagnose'])
        self.assertEqual(self.proof()['outcome'], 'inconclusive')
        self.assertEqual(cli.read(self.task)['status'], 'paused')
        self.assertEqual(cli.read(self.task)['iterations'], 2)

    def test_same_head_or_another_gate_does_not_count_as_two_fixes(self):
        state = {}
        gate = {'kind': 'resolution', 'command': self.probe['command']}
        first = {'gate': gate, 'head': 'a' * 40}
        self.assertIsNone(repair_premise.retain(state, first))
        self.assertIsNone(repair_premise.retain(state, first))
        self.assertIsNone(repair_premise.retain(state, {'gate': {**gate, 'kind': 'test-result'}, 'head': 'b' * 40}))
        self.assertEqual(repair_premise.retain(state, {'gate': gate, 'head': 'b' * 40})[0], first)

    def test_changed_failure_receipt_blocks_diagnosis_before_the_probe(self):
        model = self.model

        def tamper(workspace, prompt, timeout, *, read_only):
            result = model(workspace, prompt, timeout, read_only=read_only)
            if 'Diagnose the shared premise' in prompt:
                state = cli.read(self.task.with_name('autopilot.json'))
                pair, = state['premise_failures'].values()
                path = Path(pair[0]['path'])
                path.write_bytes(path.read_bytes() + b'\n')
            return result

        with patch.object(self, 'model', tamper), patch.object(repair_premise, 'run_sandbox') as sandbox:
            with self.assertRaisesRegex(autopilot.AutopilotError, 'related failure contents changed'):
                self.run_case(self.check_fixture)
        sandbox.assert_not_called()
        self.assertEqual(self.events, ['first-attempt', 'first-repair', 'diagnose'])
        self.assertEqual(cli.read(self.task)['iterations'], 2)

    def test_docker_start_error_without_a_completed_process_is_not_a_product_failure(self):
        report = {'container_created': True, 'cleanup_confirmed': True, 'exit_code': 1,
                  'runtime_diagnostics': {'state': {'status': 'created', 'exit_code': 0}}}
        self.assertFalse(repair_premise._observed(report))
        report['runtime_diagnostics']['state'] = {'status': 'exited', 'exit_code': 1}
        self.assertTrue(repair_premise._observed(report))

    @unittest.skipUnless(os.environ.get('WORKBENCH_DOCKER_TEST') == '1', 'requires local Docker and node:24-slim')
    def test_two_failed_repairs_are_diagnosed_in_real_docker(self):
        self.assert_result(self.run_case(workflow.run_check))
        output = os.environ.get('WORKBENCH_PREMISE_EVIDENCE')
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            record = self.proof()
            path, = self.task.parent.glob('repair-premise-*.json')
            shutil.copyfile(path, directory / 'diagnosis.json')
            for index, row in enumerate(record['failures'], 1):
                shutil.copyfile(row['path'], directory / f'failed-check-{index}.json')
            shutil.copyfile(self.task.parent / 'decisions.tsv', directory / 'decisions.tsv')

    @unittest.skipUnless(os.environ.get('WORKBENCH_DOCKER_TEST') == '1', 'requires local Docker and node:24-slim')
    def test_new_probe_executes_from_read_only_input_without_changing_candidate_files(self):
        self.probe.pop('command')
        script = (
            "import { createRequire } from 'node:module';\n"
            "const require = createRequire(import.meta.url);\n"
            "const { resolve } = require(process.cwd() + '/app.js');\n"
            "const observed = resolve({ legacyMode: 'fixed', route: 'broken' });\n"
            "console.log(observed === 'fixed' ? 'USES_LEGACY_MODE' : 'IGNORES_LEGACY_MODE');"
        )
        self.probe['source'] = script
        self.assert_result(self.run_case(workflow.run_check))
        proof = self.proof()
        self.assertEqual(proof['sandbox']['regression_sha256'], hashlib.sha256(script.encode()).hexdigest())
        self.assertEqual(proof['diagnosis']['probe']['source'], script)
        self.assertEqual(cli.git(self.workspace, 'diff', '--name-only', proof['base'], 'HEAD'), 'settings.json')
        output = os.environ.get('WORKBENCH_PREMISE_EVIDENCE')
        if output:
            cli.write(Path(output) / 'generated-probe.json', proof)


if __name__ == '__main__':
    unittest.main()
