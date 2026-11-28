import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench import prompt_profiles, typescript_discipline as ts


def project(*, findings=(), diagnostic=None, included=True):
    return {
        'compiler': '6.0.3', 'compilerSha256': 'a' * 64, 'strictChecks': {'strictNullChecks': True},
        'project': 'tsconfig.json', 'configOk': True, 'rootCount': 1, 'truncated': False,
        'diagnosticCount': int(diagnostic is not None), 'diagnostics': [diagnostic] if diagnostic else [],
        'files': [{'path': 'src/job.ts', 'sha256': 'b' * 64, 'compiled': included, 'findings': list(findings)}],
    }


class TypeScriptDisciplineTests(unittest.TestCase):
    def test_old_finding_survives_line_shift_but_new_occurrence_is_reported(self):
        finding = {'rule': 'type-assertion', 'line': 1, 'column': 2, 'scope': 'parse', 'signature': 'w'}
        old = project(findings=[finding])
        candidate = project(findings=[{**finding, 'line': 4}, {**finding, 'line': 5}])
        summary = ts._summarize(old, candidate, copy.deepcopy(candidate), ['a', 'b', 'c'])
        self.assertEqual(summary['status'], 'review-required')
        self.assertEqual(summary['new_findings']['F1']['line'], 5)

    def test_exclusion_and_weakened_checks_block_even_when_compiler_is_green(self):
        candidate = project(included=False)
        candidate['strictChecks']['strictNullChecks'] = False
        summary = ts._summarize(project(), candidate, copy.deepcopy(candidate), ['a', 'b', 'c'])
        self.assertEqual(summary['status'], 'blocked')
        self.assertIn('one or more selected candidate files were excluded from the program', summary['issues'])
        self.assertIn('candidate disabled TypeScript checks: strictNullChecks', summary['issues'])

    def test_actual_compiler_diagnostics_cannot_claim_a_pass(self):
        diagnostic = {'code': 2322, 'line': 8, 'path': 'src/job.ts', 'message': 'Variant is not assignable to never'}
        candidate = project(diagnostic=diagnostic)
        out = ts._summarize(project(), candidate, copy.deepcopy(candidate), ['a', 'b', 'c'])
        self.assertEqual(out['status'], 'blocked')
        self.assertEqual(out['candidate_diagnostics'], [diagnostic])

    def test_candidate_observations_require_independence_and_same_compiler(self):
        values = [project(), project(), project()]
        with self.assertRaisesRegex(ValueError, 'reused a container'):
            ts._summarize(*values, ['a', 'b', 'b'])
        values[-1]['compilerSha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'compiler entry changed'):
            ts._summarize(*values, ['a', 'b', 'c'])

    def test_runner_binding_checks_real_output_source_and_cleanup(self):
        body = project()
        text = json.dumps(body)
        image = 'sha256:' + '0' * 64
        paths = {'src/job.ts': 'b' * 64}
        raw = {'image': image, 'command': ts.COMMAND, 'source_root': '/app', 'workspace_read_only': True,
               'workspace': '/tmp/evidence', 'create_stdout': '3' * 64, 'container_created': True,
               'cleanup_confirmed': True, 'timed_out': False, 'exit_code': 0, 'stdout_truncated': False,
               'stderr_truncated': False, 'stderr': '', 'stdout': text,
               'stdout_sha256': hashlib.sha256(text.encode()).hexdigest()}
        inputs = {'files': paths, 'image': image, 'project': 'tsconfig.json', 'workspace': '/tmp/evidence'}
        self.assertEqual(ts._parse(raw, **inputs)[0], body)
        with self.assertRaisesRegex(ValueError, 'clean its container'):
            ts._parse({**raw, 'cleanup_confirmed': False}, **inputs)
        with self.assertRaisesRegex(ValueError, 'exact selected Git sources'):
            ts._parse(raw, **{**inputs, 'files': {'src/job.ts': 'c' * 64}})

    def test_applicable_model_stages_record_upstream_type_guidance(self):
        for stage in ('plan', 'implementation', 'repair-1', 'final-review'):
            instruction, provenance = prompt_profiles.guidance(stage, 'bug')
            self.assertIn('discriminated variants', instruction)
            self.assertIn(ts.SOURCE, provenance['sources'])
            self.assertEqual(provenance['guidance_sha256'], hashlib.sha256(instruction.encode()).hexdigest())

    def test_runtime_receipts_pin_driver_candidate_and_independent_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "console.log('VALUE_CHECKED')"
            script_sha = hashlib.sha256(script.encode()).hexdigest()
            image = 'sha256:' + '0' * 64
            output = 'VALUE_CHECKED\n'
            runtime = {'script': script, 'script_sha256': script_sha, 'marker': 'VALUE_CHECKED', 'runs': []}
            for number in (1, 2):
                raw = {'image': image, 'command': ts.BEHAVIOR, 'source_root': '/app', 'workspace_read_only': True,
                       'workspace': '/tmp/run', 'create_stdout': str(number) * 64, 'container_created': True,
                       'cleanup_confirmed': True, 'timed_out': False, 'exit_code': 0, 'stdout_truncated': False,
                       'stderr_truncated': False, 'stdout': output, 'input_sha256': {'behavior.cjs': script_sha},
                       'stdout_sha256': hashlib.sha256(output.encode()).hexdigest()}
                record = ts._keep(root / f'run-{number}.json', raw)
                runtime['runs'].append({**record, 'image': image, 'workspace': '/tmp/run'})
            self.assertTrue(ts._behavior(runtime, image, ['3' * 64])['passed'])
            with self.assertRaisesRegex(ValueError, 'reused a container'):
                ts._behavior(runtime, image, ['1' * 64])
            with self.assertRaisesRegex(ValueError, 'missed the real success marker'):
                ts._behavior({**runtime, 'marker': 'NOT_VERIFIED'}, image, ['3' * 64])
