"""Narrow OpenClaw Vitest adapter; not a complete CI replacement."""
import json
import hashlib
import re
import subprocess
from pathlib import Path


class GuardError(Exception):
    pass


CONFIG = 'test/vitest/vitest.gateway.config.ts'
REPORT_COMMAND = ['node', '/app/node_modules/vitest/vitest.mjs', 'run', '--reporter=json',
                  '--outputFile=/dev/stdout']
PREFIX = [*REPORT_COMMAND, '--config', '/app/' + CONFIG]
REGRESSION_CONFIGS = {
    'unit': 'test/vitest/vitest.unit.config.ts',
    'unit-fast': 'test/vitest/vitest.unit-fast.config.ts',
    'gateway': CONFIG,
    'agents-core': 'test/vitest/vitest.agents-core.config.ts',
}
REGRESSION_PATH = re.compile(r'src/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.regression\.test\.ts')


def _blob(repo, commit, path):
    result = subprocess.run(['git', '-C', str(repo), 'show', f'{commit}:{path}'],
                            capture_output=True, check=False, timeout=30)
    if result.returncode or len(result.stdout) > 1_000_000:
        raise GuardError(f'tracked guard source unavailable: {path}')
    return result.stdout


def command(repo, base, head, files):
    if not isinstance(files, list) or not files or len(files) > 30 or len(set(files)) != len(files):
        raise GuardError('explicit unique test files are required')
    for name in [CONFIG, *files]:
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', name)
                or any(part in ('', '.', '..') for part in name.split('/'))):
            raise GuardError('invalid test path')
        if name != CONFIG and not (name.startswith('src/') and name.endswith('.test.ts')):
            raise GuardError('guard supports explicit src/*.test.ts paths only')
        after = _blob(repo, head, name)
        if name == CONFIG and _blob(repo, base, name) != after:
            raise GuardError(f'guard config changed: {name}')
        if name != CONFIG and re.search(rb'\b(?:test|it|describe)\s*\.\s*(?:only|skip|todo)\b', after):
            raise GuardError(f'focused or skipped tests are not allowed: {name}')
    return [*PREFIX, *('/app/' + f for f in files)]


def _is_test_source(path):
    parts = path.split('/')
    name = parts[-1]
    # Include the project's real helper/mock naming families, not just test files.
    # This intentionally errs toward review when a changed source looks test-related.
    return (path.endswith(('.test.ts', '.spec.ts', '.test.tsx', '.spec.tsx')) or
            any(part in ('test', 'tests', 'test-helpers', 'test-fixtures',
                         'test-support', 'test-utils', '__tests__', '__mocks__', 'mocks')
                for part in parts[:-1]) or
            bool(re.search(r'(?:^|[.\-])test[.\-](?:helpers|fixtures|support|mocks|utils)(?:[.\-]|$)', name)) or
            bool(re.search(r'(?:^|[.\-])(?:mocks|test-setup)(?:[.\-]|$)', name)) or
            name.startswith(('vitest.', 'vitest-', 'vite.config.', 'test.config.',
                             'test.setup.', 'setup-tests.', 'setupTests.')))


def changed_test_sources(repo, base, head):
    """Enumerate all changed test sources/config helpers for independent review."""
    result = subprocess.run(['git', '-C', str(repo), 'diff', '--name-only', '-z', base, head, '--'],
                            capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise GuardError('cannot enumerate changed test files')
    changes = []
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        try:
            path = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            raise GuardError('invalid changed path') from error
        if not _is_test_source(path):
            continue
        before = subprocess.run(['git', '-C', str(repo), 'show', f'{base}:{path}'], capture_output=True)
        after = subprocess.run(['git', '-C', str(repo), 'show', f'{head}:{path}'], capture_output=True)
        if after.returncode or len(after.stdout) > 100_000 or len(before.stdout) > 100_000:
            raise GuardError(f'deleted or oversized test source: {path}')
        changes.append({'path': path, 'base_sha256': hashlib.sha256(before.stdout).hexdigest() if before.returncode == 0 else None,
                        'head_sha256': hashlib.sha256(after.stdout).hexdigest()})
    return sorted(changes, key=lambda item: item['path'])


def _decode_report(stdout):
    try:
        result, end = json.JSONDecoder().raw_decode(stdout)
        if stdout[end:].strip() != 'JSON report written to /dev/stdout':
            raise ValueError('unexpected Vitest output trailer')
    except (TypeError, ValueError) as error:
        raise GuardError('Vitest must produce a valid JSON report') from error
    return result


def validate_json(stdout, files):
    result = _decode_report(stdout)
    if (not isinstance(result, dict) or result.get('success') is not True or
            type(result.get('numTotalTests')) is not int or result['numTotalTests'] <= 0 or
            type(result.get('numPassedTests')) is not int or result['numPassedTests'] <= 0 or
            result.get('numFailedTests') != 0 or result.get('numPendingTests') != 0 or
            result['numTotalTests'] != result['numPassedTests'] or
            not isinstance(result.get('testResults'), list)):
        raise GuardError('Vitest report contains failures, skips or zero tests')
    seen, count = set(), 0
    for suite in result['testResults']:
        if not isinstance(suite, dict) or not isinstance(suite.get('name'), str):
            raise GuardError('invalid Vitest suite')
        name = suite['name']
        matches = [f for f in files if name == '/app/' + f]
        cases = suite.get('assertionResults')
        if len(matches) != 1 or matches[0] in seen or not isinstance(cases, list) or not cases:
            raise GuardError('Vitest ran an unexpected or empty suite')
        if any(not isinstance(case, dict) or case.get('status') != 'passed' for case in cases):
            raise GuardError('Vitest suite contains non-passing cases')
        seen.add(matches[0])
        count += len(cases)
    if seen != set(files) or count != result['numTotalTests']:
        raise GuardError('Vitest report does not cover the exact bound suite set')
    return result['numTotalTests']


def _regression_path(path):
    if (not isinstance(path, str) or len(path) > 240 or not REGRESSION_PATH.fullmatch(path)
            or 'node_modules' in path.split('/')):
        raise GuardError('native regression requires a src/*.regression.test.ts path')
    return path


def regression_command(path, project):
    """Select one test file through an existing OpenClaw Vitest shard."""
    if not isinstance(project, str) or project not in REGRESSION_CONFIGS:
        raise GuardError('unsupported native regression project')
    return [*REPORT_COMMAND, '--config', '/app/' + REGRESSION_CONFIGS[project], '--maxWorkers=1',
            _regression_path(path)]


def validate_regression_json(stdout, path, test_name, *, red=None, stderr=''):
    """Require exactly the frozen Vitest test and its expected assertion outcome."""
    suite_path = '/app/' + _regression_path(path)
    if not isinstance(test_name, str) or not test_name or len(test_name) > 160 or not test_name.isprintable():
        raise GuardError('native regression requires an exact Vitest test name')
    if red is not None and (not isinstance(red, str) or not red or red.strip() != red or len(red) > 160
                            or not red.isprintable()):
        raise GuardError('native regression requires an exact assertion marker')
    if not isinstance(stderr, str):
        raise GuardError('Vitest stderr must be text')
    diagnostics = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', stderr)
    if 'Unhandled Errors' in diagnostics or re.search(r'Vitest caught \d+ unhandled error', diagnostics):
        raise GuardError('Vitest reported an unhandled error')
    report = _decode_report(stdout)
    passed = red is None
    status = 'passed' if passed else 'failed'
    if not isinstance(report, dict) or report.get('success') is not passed:
        raise GuardError('Vitest native regression reported an unexpected verdict')
    suites = report.get('testResults')
    if not isinstance(suites, list) or len(suites) != 1 or not isinstance(suites[0], dict):
        raise GuardError('Vitest must run exactly one native regression file')
    suite = suites[0]
    cases = suite.get('assertionResults')
    if (suite.get('name') != suite_path or suite.get('status') != status or suite.get('message') != ''
            or not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict)):
        raise GuardError('Vitest ran an unrelated, empty, or broken native regression suite')
    case = cases[0]
    ancestors = case.get('ancestorTitles')
    if (case.get('fullName') != test_name or case.get('status') != status or not isinstance(ancestors, list)
            or not all(isinstance(name, str) for name in ancestors)):
        raise GuardError('Vitest did not run the exact named regression test')
    suite_count = 1 + len(ancestors)
    counts = {
        'numTotalTestSuites': suite_count,
        'numPassedTestSuites': suite_count if passed else 0,
        'numFailedTestSuites': 0 if passed else suite_count,
        'numPendingTestSuites': 0,
        'numTotalTests': 1,
        'numPassedTests': int(passed),
        'numFailedTests': int(not passed),
        'numPendingTests': 0,
        'numTodoTests': 0,
    }
    if any(type(report.get(key)) is not int or report[key] != count for key, count in counts.items()):
        raise GuardError('Vitest reported skipped, additional, or inconsistent native regression results')
    failures = case.get('failureMessages')
    if passed and failures == []:
        return
    if not passed and isinstance(failures, list) and len(failures) == 1 and isinstance(failures[0], str):
        header = failures[0].partition('\n')[0]
        if (header == f'AssertionError [ERR_ASSERTION]: {red}' or header == f'AssertionError: {red}'
                or header.startswith(f'AssertionError: {red}: expected ')):
            return
    raise GuardError('Vitest did not report the designated assertion failure or clean pass')
