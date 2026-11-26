"""Revalidate frozen refactoring evidence without running Docker or writing artifacts."""

import json
from pathlib import Path

from workbench import cli, refactor


class RefactorGateError(cli.WorkbenchError):
    pass


def _reference(ref, label):
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
            or not isinstance(ref['path'], str) or not isinstance(ref['sha256'], str)
            or not refactor.SHA.fullmatch(ref['sha256'])):
        raise ValueError(f'a pinned refactoring {label} is required')
    path = Path(ref['path'])
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError(f'refactoring {label} path changed')
    return path, ref['sha256']


def bind(path, sha256):
    try:
        path = Path(path).expanduser().absolute()
        if path.is_symlink():
            raise ValueError('refactoring contract must be a regular file')
        path = path.resolve(strict=True)
        ref = {'path': str(path), 'sha256': sha256}
        refactor.load_frozen(*_reference(ref, 'contract'))
        return ref
    except refactor.ERRORS as error:
        raise RefactorGateError(str(error)) from error


def _frozen(data):
    loaded = refactor.load_frozen(*_reference(data.get('refactor_contract'), 'contract'))
    if loaded[0]['baseline'] != data.get('base_commit'):
        raise ValueError('frozen refactoring baseline differs from the task base')
    return loaded


def contract(data):
    """Return the task's validated baseline plan and its approved scope."""
    try:
        return _frozen(data)[0]
    except refactor.ERRORS as error:
        raise RefactorGateError(str(error)) from error


def require(data, head):
    """Return the report reference after replaying all local proof checks."""
    try:
        plan, frozen, baseline, containers = _frozen(data)
        ref = data.get('refactor_result')
        path, digest = _reference(ref, 'result')
        payload = refactor._read(path, 512_000)
        if refactor._digest(payload) != digest:
            raise ValueError('refactoring result SHA-256 does not match')
        report = json.loads(payload)
        if (not isinstance(report, dict)
                or set(report) != {'schema', 'source', 'status', 'baseline', 'candidate', 'contract_sha256', 'steps'}
                or type(report.get('schema')) is not int or report['schema'] != cli.SCHEMA
                or report['source'] != refactor.SOURCE or report['status'] != 'equivalent'
                or report['baseline'] != plan['baseline'] or report['candidate'] != head
                or report['contract_sha256'] != data['refactor_contract']['sha256']):
            raise ValueError('refactoring result differs from the required candidate or frozen contract')
        if not isinstance(head, str) or not refactor.COMMIT.fullmatch(head):
            raise ValueError('refactoring delivery requires an exact Git commit')
        repo = Path(data['repo']).expanduser().resolve(strict=True)
        if cli.sha(repo, 'HEAD', timeout=15) != head:
            raise ValueError('refactoring result is not for the current Git HEAD')
        spec, memory = plan['spec'], plan['memory_mb']
        expected = refactor._steps(repo, plan['baseline'], head, spec['scope'], refactor._deadline(60))
        steps = report['steps']
        if not isinstance(steps, list) or len(steps) != len(expected):
            raise ValueError('refactoring result must cover every ordered commit')
        for number, (step, (commit, paths)) in enumerate(zip(steps, expected.items()), 1):
            if (not isinstance(step, dict)
                    or set(step) != {'commit', 'paths', 'runs', 'status', 'image', 'observed'}
                    or step['commit'] != commit or step['paths'] != paths or step['status'] != 'equivalent'
                    or not isinstance(step['image'], str) or not refactor.IMAGE.fullmatch(step['image'])):
                raise ValueError(f'refactoring result has an invalid step for {commit}')
            folder = path.parent / f'step-{number}'
            if folder.is_symlink():
                raise ValueError(f'refactoring evidence directory changed for {commit}')
            observed = refactor._receipts(folder, step['runs'], spec, frozen, commit, step['image'],
                                          memory, containers)
            if observed != baseline or observed != step['observed']:
                raise ValueError(f'refactoring behavior differs from the frozen baseline at {commit}')
        return ref
    except refactor.ERRORS as error:
        raise RefactorGateError(str(error)) from error
