"""Freeze a focused regression test before modifying the implementation."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

from workbench import cli, local_vitest


COMMAND = ['node', '/workbench-regression/check.mjs']
VITEST = 'openclaw-vitest'
KINDS = ('tdd-red', 'tdd-green')
REPORTS = {'TDD_RED': 'tdd-red', 'TDD_GREEN': 'tdd-green'}
PLAN_INSTRUCTION = (
    'If a cheap standalone Node regression test can exercise the actual bug, also return '
    '"tdd":{"path":"scripts/example.regression.test.mjs","source":"complete ESM source",'
    '"red":"specific assertion message","green":"specific success marker"}. Use node:assert/strict, '
    'load real project code via an absolute path derived from process.cwd(), assert the intended '
    'behavior with the red message, and print the green marker only after the assertion. '
    'The same frozen test runs first on the clean baseline and then on the candidate. '
    'Keep the ordinary reproduction, resolution, and neighboring tests in the plan. '
    'Omit tdd if this focused test is impractical; an ordinary executable reproduction is still required.\n'
)
VITEST_INSTRUCTION = (
    'For an OpenClaw change with an existing Vitest project, first consider a native test. Return '
    '"tdd":{"adapter":"openclaw-vitest","project":"unit, unit-fast, gateway, or agents-core",'
    '"path":"src/.../example.regression.test.ts","source":"complete Vitest source",'
    '"red":"specific assertion message","test_name":"exact full test name"}. Include one focused Vitest '
    'top-level test using an expectation message. Import the real code using the repository test conventions. '
    'Choose the actual registered project for the new file; lightweight agent tests can belong to unit-fast. '
    'Keep the native project configuration untouched. A standalone test is appropriate if the native '
    'project cannot cover this scenario.\n'
)


class RegressionError(cli.WorkbenchError):
    pass


def contract(spec):
    if not isinstance(spec, dict):
        raise RegressionError('focused regression must be an object')
    native = spec.get('adapter') == VITEST
    required = {'adapter', 'project', 'path', 'source', 'red', 'test_name'} if native else {'path', 'source', 'red', 'green'}
    if set(spec) != required:
        raise RegressionError('focused regression fields do not match its test adapter')
    path, source, red = (spec[key] for key in ('path', 'source', 'red'))
    green = spec['test_name'] if native else spec['green']
    if native:
        command(spec)
    elif not isinstance(path, str) or len(path) > 240 or not re.fullmatch(
            r'(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.regression\.test\.mjs', path):
        raise RegressionError('invalid standalone focused regression path')
    if not isinstance(source, str) or not source.strip() or '\0' in source or len(source.encode()) > 16_384:
        raise RegressionError('focused regression must be bounded JavaScript or TypeScript source')
    if (any(not isinstance(s, str) or not s or s != s.strip() or len(s) > 160 or not s.isprintable()
            for s in (red, green))
            or red in green or green in red):
        raise RegressionError('focused regression needs distinct bounded red and green markers')
    if red not in source or green not in source:
        raise RegressionError('focused regression source must contain both markers')
    return spec


def command(spec):
    if spec.get('adapter') != VITEST:
        return COMMAND
    try:
        return local_vitest.regression_command(spec['path'], spec['project'])
    except local_vitest.GuardError as error:
        raise RegressionError(str(error)) from error


def require_native_context(data, spec, source_mode, revision, kind):
    if spec.get('adapter') != VITEST:
        return
    repo = data['repo']
    base = data['base_commit']
    if source_mode != 'image' or json.loads(cli.git(repo, 'show', f'{base}:package.json')).get('name') != 'openclaw':
        raise RegressionError('native focused regression requires a source-verified OpenClaw image')
    path = destination(repo, spec)
    baseline_entry = cli.git(repo, 'ls-tree', base, '--', spec['path'])
    if baseline_entry or (kind == 'tdd-red' and (path.exists() or not path.parent.is_dir())):
        raise RegressionError('native baseline may mount only a new test under an existing source directory')
    if kind == 'tdd-green' and cli.git(
            repo, 'diff', '--name-only', base, revision, '--', 'test/vitest', 'test/setup.ts', 'test/setup',
            'package.json', 'pnpm-lock.yaml', 'tsconfig.json'):
        raise RegressionError('native focused regression requires unchanged Vitest configuration and dependencies')


def digest(spec):
    return hashlib.sha256(contract(spec)['source'].encode()).hexdigest()


def destination(workspace, spec):
    root = Path(workspace).resolve(strict=True)
    path = root / contract(spec)['path']
    if any(part.is_symlink() for part in (*path.parents, path) if part.is_relative_to(root)):
        raise RegressionError('focused regression path crosses a symlink')
    return path


def add_test(workspace, spec):
    path = destination(workspace, spec)
    ignored = subprocess.run(
        ['git', '-C', str(workspace), 'check-ignore', '--quiet', '--', spec['path']], timeout=30, check=False)
    if ignored.returncode != 1:
        raise RegressionError('focused regression path must be trackable in Git')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as file:
            file.write(contract(spec)['source'].encode())
    except FileExistsError as error:
        raise RegressionError('focused regression path must be new on the baseline') from error


def verify_test(workspace, spec, commit=None):
    path = destination(workspace, spec)
    try:
        if commit is None:
            content = path.read_bytes() if path.is_file() and path.stat().st_size <= 16_384 else b''
        else:
            entry = cli.git(workspace, 'ls-tree', commit, '--', spec['path'], timeout=30)
            if not entry.startswith(('100644 blob ', '100755 blob ')):
                raise RegressionError('candidate must keep the frozen focused regression as a regular Git blob')
            blob = entry.split()[2]
            size = cli.git(workspace, 'cat-file', '-s', blob, timeout=30)
            if not size.isdecimal() or int(size) > 16_384:
                raise RegressionError('candidate focused regression source exceeds size limit')
            content = subprocess.run(
                ['git', '-C', str(workspace), 'cat-file', 'blob', blob],
                capture_output=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RegressionError('candidate focused regression source unavailable') from error
    if hashlib.sha256(content).hexdigest() != digest(spec):
        raise RegressionError('candidate changed the frozen focused regression test')


def expected(spec, kind):
    spec = contract(spec)
    if kind == 'tdd-red':
        return 1, spec['red']
    if kind == 'tdd-green':
        return 0, spec['test_name'] if spec.get('adapter') == VITEST else spec['green']
    raise RegressionError('invalid focused regression evidence type')


def matches(report, spec, kind):
    code, marker = expected(spec, kind)
    stdout, stderr = report.get('stdout'), report.get('stderr')
    if (not isinstance(stdout, str) or not isinstance(stderr, str)
            or report.get('command') != command(spec) or report.get('regression_sha256') != digest(spec)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(report.get('image', '')))
            or report.get('workspace_read_only') is not True or report.get('exit_code') != code
            or report.get('expected_exit') != code or report.get('expected_marker') != marker
            or report.get('stdout_truncated') or report.get('stderr_truncated')):
        return False
    if spec.get('adapter') == VITEST:
        if (report.get('source_mode') != 'image' or report.get('regression_file') != spec['path']
                or report.get('regression_input') != ('isolated-input' if kind == 'tdd-red' else 'verified-image')):
            return False
        try:
            local_vitest.validate_regression_json(stdout, spec['path'], spec['test_name'],
                                                 red=marker if kind == 'tdd-red' else None, stderr=stderr)
        except local_vitest.GuardError:
            return False
        return True
    if kind == 'tdd-red':
        headers = {f'AssertionError [ERR_ASSERTION]: {marker}', f'AssertionError: {marker}'}
        return bool(headers.intersection(stderr.splitlines())) and spec['green'] not in stdout.splitlines()
    return marker in stdout.splitlines() and spec['red'] not in stderr


def validate_citations(assessments):
    if not any(REPORTS.keys() <= set(assessment['evidence']) for assessment in assessments):
        raise RegressionError('the criterion for the bug must cite both independent regression runs')
