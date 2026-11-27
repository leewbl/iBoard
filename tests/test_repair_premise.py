import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workbench import autopilot, cli, delivery, repair_premise, workflow


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
        (repo / 'app.js').write_text("exports.resolve = (settings) => settings.route ?? settings.fallbackRoute ?? 'unresolved';\n")
        (repo / 'repro.js').write_text(
            "console.log(require('./app').resolve(require('./settings.json')).toUpperCase());\n")
        (repo / 'probe.js').write_text(
            "const resolve = require('./app').resolve;\n"
            "const result = resolve({legacyMode: 'fixed', route: 'broken'});\n"
            "console.log(result === 'fixed' ? 'USES_LEGACY_MODE' : 'IGNORES_LEGACY_MODE');\n")
        (repo / 'fallback.js').write_text(
            "const { route, ...fallback } = require('./settings.json');\n"
            "console.log('FALLBACK ' + require('./app').resolve(fallback).toUpperCase());\n")
        (repo / 'test-report.js').write_text(
            "console.log('PRIMARY ' + require('./app').resolve(require('./settings.json')).toUpperCase());\n"
            "require('./fallback');\n")
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
            fallback = 'FALLBACK ' + settings.get('fallbackRoute', 'unresolved').upper() + '\n'
            primary = settings['route'].upper() + '\n'
            if command[-1].endswith('/test-report.js'):
                stdout = 'PRIMARY ' + primary + fallback
            elif command[-1].endswith('/fallback.js'):
                stdout = fallback
            else:
                stdout = primary
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

    def test_invalid_sandbox_path_gets_one_plan_correction_before_any_execution(self):
        model = self.model
        corrections = []
        commands = []

        def diagnose(workspace, prompt, timeout, *, read_only):
            result = model(workspace, prompt, timeout, read_only=read_only)
            if 'Diagnose the shared premise' in prompt:
                corrections.append('rejected the probe before execution' in prompt)
                self.assertIn('sandbox root for this task is /workspace', prompt)
                if len(corrections) == 1:
                    proposal = json.loads(result['stdout'])
                    proposal['probe'] = {**self.probe, 'command': ['node', '/app/probe.js']}
                    result['stdout'] = json.dumps(proposal)
            return result

        def sandbox(workspace, image, command, timeout, **kwargs):
            commands.append(command)
            return self.sandbox_fixture(workspace, image, command, timeout, **kwargs)

        with patch.object(self, 'model', diagnose), patch.object(repair_premise, 'run_sandbox', sandbox):
            state = self.run_case(self.check_fixture)
        self.assertEqual(corrections, [False, True])
        self.assertEqual(commands, [self.probe['command']])
        self.assertEqual(state['phase'], 'delivery')
        self.assertEqual(cli.read(self.task)['iterations'], 3)
        self.assertEqual(len(list(self.task.parent.glob('premise-2-model-previous-*.json'))), 1)

    def test_same_head_or_another_gate_does_not_count_as_two_fixes(self):
        state = {}
        gate = {'kind': 'resolution', 'command': self.probe['command']}
        first = {'gate': gate, 'head': 'a' * 40}
        self.assertIsNone(repair_premise.retain(state, first))
        self.assertIsNone(repair_premise.retain(state, first))
        self.assertIsNone(repair_premise.retain(state, {'gate': {**gate, 'kind': 'test-result'}, 'head': 'b' * 40}))
        self.assertEqual(repair_premise.retain(state, {'gate': gate, 'head': 'b' * 40})[0], first)

    def test_review_classification_can_change_without_changing_its_criterion(self):
        state = {}
        for index, finding_class in enumerate(('coverage', 'behavior')):
            head = 'ab'[index] * 40
            path = self.task.parent / f'rejected-{index}.json'
            raw = {'schema': cli.SCHEMA, 'kind': 'independent-review', 'head': head,
                   'base': 'c' * 40, 'source_mode': 'workspace', 'actor': 'independent-verifier',
                   'criteria_sha256': 'd' * 64, 'rejection': {'blocked_criteria': [
                       {'id': 'route', 'class': finding_class, 'reason': 'Review rejected the route.', 'evidence': ['R5']}]}}
            cli.write(path, raw)
            failure, = repair_premise.external_failures(self.task, path, raw, head)
            pair = repair_premise.retain(state, failure)
            if index == 0:
                self.assertIsNone(pair)
        self.assertEqual([row['head'] for row in pair], ['a' * 40, 'b' * 40])

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

    def external_model(self, workspace, prompt, timeout, *, read_only):
        settings = workspace / 'settings.json'
        if 'Implement this explicitly' in prompt:
            self.events.append('first-attempt')
            settings.write_text('{"legacyMode":"fixed","route":"fixed"}')
        elif 'Repair the ' in prompt and not read_only:
            value = json.loads(settings.read_text())
            if '"outcome": "refuted"' in prompt:
                self.events.append('grounded-repair')
                value['fallbackRoute'] = 'fixed'
            else:
                self.events.append('first-repair')
                value['legacyMode'] = 'FIXED'
            settings.write_text(json.dumps(value))
        else:
            return self.model(workspace, prompt, timeout, read_only=read_only)
        return {'exit_code': 0, 'timed_out': False, 'stdout': '', 'stderr': ''}

    def external_runtime(self):
        if os.environ.get('WORKBENCH_DOCKER_TEST') == '1':
            return workflow.run_check, nullcontext()
        return self.check_fixture, patch.object(repair_premise, 'run_sandbox', self.sandbox_fixture)

    def archive_external(self, label, proof, **extra):
        output = os.environ.get('WORKBENCH_PREMISE_EVIDENCE')
        if output:
            path = Path(output) / label
            cli.write(path / 'diagnosis.json', proof)
            cli.write(path / 'observations.json', extra)
            for index, failure in enumerate(proof['failures'], 1):
                source = Path(failure['path'])
                shutil.copyfile(source, path / f'failure-{index}.json')
                evidence = cli.read(source)
                shutil.copyfile(evidence['runtime']['path'], path / f'local-runtime-{index}.json')

    def test_required_ci_recurrence_matches_same_job_and_waits_for_new_head(self):
        check, runner = self.external_runtime()
        heads, updates, merges, ci_checks = [], [], [], []

        class GitHub:
            def __init__(self, run, jobs):
                self.run, self.jobs = run, jobs

            def _call(self, method, path):
                if '/jobs?' in path:
                    return {'total_count': len(self.jobs), 'jobs': [
                        {'id': self.run * 10 + index, 'status': 'completed', 'conclusion': 'failure', 'name': job}
                        for index, job in enumerate(self.jobs)]}
                return [{'path': 'settings.json', 'annotation_level': 'failure',
                         'message': 'Fallback route still unresolved'}]

        def wait(task, repo, number, head):
            heads.append(head)
            result = repair_premise.run_sandbox(
                self.workspace, 'node:24-slim', ['node', '/workspace/fallback.js'], 15,
                workspace_read_only=True, memory_mb=128, pin_image=True)
            self.assertTrue(result['cleanup_confirmed'])
            self.assertEqual(result['exit_code'], 0)
            ci_checks.append([head, result['stdout'].strip()])
            if len(heads) > 2:
                self.assertIn('grounded-repair', self.events)
                self.assertEqual(updates[-1]['head'], head)
                self.assertEqual(result['stdout'].strip(), 'FALLBACK FIXED')
                return
            self.assertEqual(result['stdout'].strip(), 'FALLBACK UNRESOLVED')
            run = 10 + len(heads)
            github = GitHub(run, ['regression', 'docs' if len(heads) == 1 else 'types'])
            details = {'id': run, 'run_attempt': 1, 'status': 'completed', 'workflow_id': 45,
                       'conclusion': 'failure', 'head_sha': head}
            raise autopilot.CIFailure(autopilot._ci_failure_evidence(github, repo, 'Verify', run, details, head))

        def republish(task, repo, number, previous):
            self.assertEqual(previous, heads[-1])
            updates.append({'repo': repo, 'pr': number, 'previous': previous, 'head': cli.sha(self.workspace, 'HEAD')})
            return updates[-1]

        def merge(task, repo, number):
            merges.append(cli.sha(self.workspace, 'HEAD'))
            return {'merged': True}

        simulate_ci = patch.object(autopilot, '_wait_for_ci', side_effect=wait)
        simulate_update = patch.object(autopilot.publish, 'republish', side_effect=republish)
        with runner, simulate_ci, simulate_update, patch.object(delivery, 'guarded_merge', merge):
            state = autopilot.run(self.task, self.issue, 'node:24-slim', model=self.external_model, check=check,
                                  memory_mb=128, pr_repo='user/fork', pr_number=7, merge=merge)
        self.assertEqual(self.events, ['first-attempt', 'first-repair', 'diagnose', 'grounded-repair'])
        self.assertEqual(len(set(heads)), 3)
        self.assertEqual(merges, [heads[-1]])
        self.assertEqual(state['phase'], 'complete')
        proof = self.proof()
        self.assertEqual(proof['outcome'], 'refuted')
        self.assertEqual([row['gate']['job'] for row in proof['failures']], ['regression', 'regression'])
        self.archive_external('ci-recurrence', proof, ci_heads=heads, ci_checks=ci_checks,
                              pr_updates=updates, merged_heads=merges)

    def test_independent_review_repeats_the_same_structured_criterion(self):
        check, runner = self.external_runtime()
        verdicts = []

        def model(workspace, prompt, timeout, *, read_only):
            if 'Independently inspect the candidate' in prompt:
                head = cli.sha(workspace, 'HEAD')
                current = cli.read(self.task)
                report = next(item for item in reversed(current['evidence'])
                              if item['kind'] == 'test-result' and item['actor'] == 'independent-verifier'
                              and item['commit'] == head)
                output = cli.read(report['path'])['stdout']
                if 'FALLBACK FIXED' not in output:
                    self.assertIn('FALLBACK UNRESOLVED', output)
                    verdicts.append([head, 'blocked'])
                    rejection = {'status': 'blocked', 'reason': 'The fallback route remains unresolved.',
                                 'blocked_criteria': [{'id': 'route', 'class': 'behavior',
                                                       'reason': 'The report shows FALLBACK UNRESOLVED.',
                                                       'evidence': ['R5']}]}
                    return {'exit_code': 0, 'timed_out': False, 'stdout': json.dumps(rejection), 'stderr': ''}
                verdicts.append([head, 'pass'])
            reply = self.external_model(workspace, prompt, timeout, read_only=read_only)
            if 'Return only JSON:' in prompt:
                plan = json.loads(reply['stdout'])
                plan['criteria'][0]['text'] = 'Both the primary and fallback routes resolve to fixed.'
                plan['tests'] = {'command': ['node', '/workspace/test-report.js'], 'marker': 'PRIMARY FIXED', 'exit_code': 0}
                reply['stdout'] = json.dumps(plan)
            return reply

        with runner:
            state = autopilot.run(self.task, self.issue, 'node:24-slim', model=model, check=check,
                                  memory_mb=128, local_handoff=True)
        self.assert_result(state)
        self.assertEqual([value for _, value in verdicts], ['blocked', 'blocked', 'pass'])
        proof = self.proof()
        self.assertEqual([row['head'] for row in proof['failures']], [head for head, _ in verdicts[:2]])
        self.assertEqual(proof['failures'][0]['gate']['kind'], 'independent-review')
        self.assertEqual(proof['failures'][0]['gate']['criterion'], 'route')
        self.archive_external('review-recurrence', proof, review_verdicts=verdicts)

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
