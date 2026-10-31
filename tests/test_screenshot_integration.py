"""Opt-in Docker test of PNG artifact transport, not a browser screenshot test."""
import argparse
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_runner import tiny_png
from workbench import cli, workflow


@unittest.skipUnless(os.environ.get('WORKBENCH_DOCKER_TEST') == '1', 'requires local Docker and node:24-slim')
class ScreenshotTransportTest(unittest.TestCase):
    def test_container_artifact_is_recorded_and_bound_to_commit(self):
        with tempfile.TemporaryDirectory(prefix='workbench-screenshot-test-') as directory:
            root = Path(directory); repo = root / 'repo'; repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=subprocess.DEVNULL)
            git('init', '-q'); git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'Test')
            encoded = base64.b64encode(tiny_png()).decode('ascii')
            (repo / 'capture.cjs').write_text('const fs=require("node:fs");\n'
                f'fs.writeFileSync(process.env.WORKBENCH_SCREENSHOT_PATH,Buffer.from("{encoded}","base64"));\n'
                'console.log("CAPTURE_COMPLETE");\n')
            git('add', '.'); git('commit', '-qm', 'baseline')
            info = workflow.prepare(task_id='capture', repo=repo, tasks_root=root / 'tasks',
                                    workspaces_root=root / 'workspaces', source='transport test',
                                    kind='feature', surface='ui', iterations=1, seconds=120,
                                    implementer='test')
            task = Path(info['task']); data = cli.read(task)
            cli.criterion_add(argparse.Namespace(id='capture', text='capture exists', reason='test'), data)
            cli.write(task, data)
            result = workflow.run_check(task=task, kind='screenshot', image='node:24-slim',
                                        command=['node', '/workspace/capture.cjs'], timeout=30,
                                        expect_exit=0, marker='CAPTURE_COMPLETE', actor='verifier',
                                        description='transport test')
            self.assertEqual(Path(result['screenshot']).read_bytes(), tiny_png())
            record = cli.read(task)['evidence'][0]
            self.assertEqual(record['commit'], info['base_commit'])
            self.assertEqual(record['sha256'], cli.digest(result['screenshot']))
            self.assertEqual(record['execution_report_sha256'], cli.digest(result['report']))
