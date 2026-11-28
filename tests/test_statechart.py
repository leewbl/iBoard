import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runner.sandbox import SandboxError, _copy_statechart
from workbench import statechart


class StatechartTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.image = 'sha256:' + 'a' * 64
        self.spec = self.root / 'Model.tla'
        self.spec.write_text('---- MODULE Model ----\n====\n')
        self.spec.with_suffix('.cfg').write_text('INIT Init\nNEXT Next\n')
        self.view = self.root / 'view.mjs'
        self.view.write_text("export const state = () => 'Ready';")
        self.extra = self.root / 'Aux.tla'
        self.extra.write_text('---- MODULE Aux ----\n====\n')

    def sandbox(self, workspace, image, command, timeout, *, statechart_output, workspace_read_only, memory_mb):
        self.assertEqual(image, self.image)
        self.assertTrue(workspace_read_only)
        mounted = Path(workspace)
        self.assertEqual((mounted / 'model/Model.tla').read_bytes(), self.spec.read_bytes())
        self.assertEqual((mounted / 'model/Aux.tla').read_bytes(), self.extra.read_bytes())
        self.assertIn('--view', command)
        self.assertTrue((mounted / 'engine/pipeline.ts').is_file())
        exit_code = 1
        output = self.root / 'emitted'
        output.mkdir()
        runtime = {'node': 'v24.22.1', 'java': '25', 'tlc': '1.7.4', 'tlc_sha256': statechart.TLC_SHA256,
                   'verdict': exit_code, 'renderer': 'Pele 1.9.0'}
        files = {'runtime.json': json.dumps(runtime), 'tlc.out': 'Invariant broken',
                 'report.md': '# Model\n## Capacity breaks\n', 'chart.mmd': 'stateDiagram-v2',
                 'chart.svg': '<svg/>', 'tlc-EventuallyDone.out': 'A liveness trace'}
        for name, content in files.items():
            (output / name).write_text(content)
        digests = _copy_statechart(output, statechart_output)
        empty = hashlib.sha256(b'').hexdigest()
        return {'command': command, 'image': image, 'source_root': '/workspace', 'memory_mb': memory_mb,
                'workspace_read_only': True, 'container_created': True, 'cleanup_confirmed': True,
                'create_exit_code': 0, 'create_stdout': 'c' * 64, 'exit_code': exit_code, 'timed_out': False,
                'stdout_truncated': False, 'stderr_truncated': False, 'stdout_sha256': empty,
                'stderr_sha256': empty, 'statechart_artifacts': digests, 'runtime_diagnostics': {
                    'state': {'status': 'exited', 'exit_code': exit_code, 'oom_killed': False}}}

    def test_violated_invariant_retains_pinned_inputs_and_both_charts(self):
        out = self.root / 'proof'
        result = statechart.run(spec=self.spec, view=self.view, support=[self.extra],
                                image=self.image, out=out, sandbox=self.sandbox)
        self.assertEqual(result['outcome'], 'violated')
        self.assertEqual(set(result['input_sha256']), {'Model.tla', 'Model.cfg', 'Aux.tla', 'view.mjs'})
        self.assertEqual(result['runtime']['renderer'], 'Pele 1.9.0')
        self.assertIn('artifacts/tlc-EventuallyDone.out', result['artifacts'])
        self.assertIn('artifacts/chart.svg', result['artifacts'])
        self.assertTrue((out / 'execution.json').is_file())

    def test_duplicate_staged_names_stop_before_docker(self):
        other = self.root / 'other'
        other.mkdir()
        duplicate = other / self.spec.name
        duplicate.write_text('a different model')
        result = statechart.run(spec=self.spec, support=[duplicate], image=self.image, out=self.root/'proof',
                                sandbox=lambda *a, **k: self.fail('Docker should not be used'))
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('unique basenames', result['reason'])

    def test_scoped_artifact_directory_does_not_follow_symbolic_links(self):
        out = self.root / 'untrusted'
        out.mkdir()
        (out / 'report.md').write_text('report')
        (out / 'runtime.json').write_text('{}')
        (out / 'tlc.out').symlink_to(self.spec)
        target = self.root / 'copied'
        with self.assertRaises(SandboxError):
            _copy_statechart(out, target)
        self.assertFalse((target / 'tlc.out').exists())

    def test_required_artifacts_and_renderer_cannot_claim_a_missing_svg(self):
        out = self.root / 'inconsistent'
        out.mkdir()
        runtime = {'node': 'v24.22.1', 'java': '25', 'tlc': '1.7.4', 'tlc_sha256': statechart.TLC_SHA256,
                   'verdict': 0, 'renderer': 'Pele 1.9.0'}
        files = {'runtime.json': json.dumps(runtime), 'chart.mmd': 'stateDiagram-v2',
                 'report.md': '# Model', 'tlc.out': 'Done'}
        for name, content in files.items():
            (out / name).write_text(content)
        execution = {'exit_code': 0, 'statechart_artifacts': {
            name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}}
        with self.assertRaisesRegex(ValueError, 'renderer receipt'):
            statechart._retained(out, execution, False)

    def test_preparation_rejects_unapproved_archive_before_tar_or_build(self):
        archive = self.root / 'runtime.tar.gz'
        archive.write_bytes(b'not the expected archive')
        with patch.dict('os.environ', {'DOCKER_HOST': 'unix:///fixture'}):
            with patch.object(statechart, '_command', return_value='arm64') as command:
                with self.assertRaisesRegex(ValueError, 'pinned checksum'):
                    statechart.prepare(base_image=self.image, java_archive=archive, tlc_jar=archive,
                                       pele_archive=archive)
                self.assertEqual(command.call_count, 1)
                self.assertEqual(command.call_args.args[0][:3], ['docker', 'image', 'inspect'])


if __name__ == '__main__':
    unittest.main()
