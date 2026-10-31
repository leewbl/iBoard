"""Narrow OpenClaw Vitest adapter; not a complete CI replacement."""
import json
import hashlib
import re
import subprocess
from pathlib import Path


class GuardError(Exception):
    pass


PREFIX = ['node', '/app/node_modules/vitest/vitest.mjs', 'run', '--reporter=json', '--outputFile=/dev/stdout',
          '--config', '/app/test/vitest/vitest.gateway.config.ts']
CONFIG = 'test/vitest/vitest.gateway.config.ts'


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


def validate_json(stdout, files):
    try:
        result, end = json.JSONDecoder().raw_decode(stdout)
        if stdout[end:].strip() != 'JSON report written to /dev/stdout':
            raise ValueError('unexpected Vitest output trailer')
    except (TypeError, ValueError) as error:
        raise GuardError('Vitest must produce a valid JSON report') from error
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
