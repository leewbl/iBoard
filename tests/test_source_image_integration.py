"""Opt-in Docker integration check; uses a local node:24-slim image."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from workbench.workflow import WorkflowError, build_source_image


@unittest.skipUnless(os.environ.get('WORKBENCH_DOCKER_TEST') == '1', 'requires local Docker and node:24-slim')
class SourceImageIntegrationTest(unittest.TestCase):
    def test_committed_source_overlay_and_deleted_file(self):
        def run(*args, **kwargs):
            return subprocess.run(args, check=True, **kwargs)

        tag = f'workbench-test-baseline:{uuid.uuid4().hex}'
        with tempfile.TemporaryDirectory(prefix='workbench-image-test-') as directory:
            repo = Path(directory)
            run('git', 'init', '-q', str(repo))
            run('git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid')
            run('git', '-C', str(repo), 'config', 'user.name', 'Test')
            (repo / 'old.txt').write_text('old\n')
            (repo / 'code.txt').write_text('before\n')
            run('git', '-C', str(repo), 'add', '.')
            run('git', '-C', str(repo), 'commit', '-qm', 'baseline')
            base = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            dockerfile = repo / 'Dockerfile'
            dockerfile.write_text('FROM node:24-slim\nCOPY code.txt old.txt /app/\n')
            candidate_tag = None
            try:
                run('docker', 'build', '--network=none', '--pull=false', '-t', tag, str(repo),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                dockerfile.unlink()
                (repo / 'code.txt').write_text('after\n')
                (repo / 'old.txt').unlink()
                (repo / 'new.txt').write_text('new\n')
                run('git', '-C', str(repo), 'add', '-A')
                run('git', '-C', str(repo), 'commit', '-qm', 'candidate')
                image = build_source_image(repo, base, tag, 120)
                inspected = subprocess.check_output(['docker', 'image', 'inspect', image], text=True)
                import json
                candidate_tag = next(t for t in json.loads(inspected)[0]['RepoTags']
                                     if t.startswith('workbench-candidate:'))
                output = subprocess.check_output(['docker', 'run', '--rm', '--network=none',
                                                  '--entrypoint', 'sh', image, '-c',
                                                  'cat /app/code.txt /app/new.txt; test ! -e /app/old.txt'],
                                                 text=True, timeout=30)
                self.assertEqual(output, 'after\nnew\n')
                with self.assertRaisesRegex(WorkflowError, 'image source'):
                    build_source_image(repo, base, image, 120)
                (repo / 'code.txt').write_text('uncommitted\n')
                with self.assertRaisesRegex(WorkflowError, 'clean worktree'):
                    build_source_image(repo, base, tag, 120)
            finally:
                for image_tag in (candidate_tag, tag):
                    if image_tag:
                        subprocess.run(['docker', 'image', 'rm', image_tag],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
