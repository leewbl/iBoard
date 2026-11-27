"""Freeze a public behavior contract and check each refactor commit in offline Docker."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from runner.sandbox import MAX_OUTPUT, SandboxError, run_sandbox
from workbench import cli, project_triage, project_verify, workflow


SHA = re.compile(r'[0-9a-f]{64}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
SOURCE = 'skills/poteto-mode/playbooks/refactoring.md'
OBSERVATION_FIELDS = ('exit_code', 'stdout_sha256', 'stderr_sha256')
ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, SandboxError,
          cli.WorkbenchError, workflow.WorkflowError, subprocess.SubprocessError)


def _read(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'missing or symbolic refactoring input {path.name}')
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if not content or len(content) > limit:
        raise ValueError(f'invalid size for refactoring input {path.name}')
    return content


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _contract(raw):
    expected = {'schema', 'intent', 'target_shape', 'scope', 'cases'}
    if not isinstance(raw, dict) or set(raw) != expected or type(raw['schema']) is not int or raw['schema'] != 1:
        raise ValueError('refactoring manifest requires schema 1, intent, target_shape, scope and cases')
    for name in ('intent', 'target_shape'):
        project_verify._text(raw[name], name)
    scope, cases = raw['scope'], raw['cases']
    if not isinstance(scope, list) or not 1 <= len(scope) <= 40:
        raise ValueError('declare one to forty allowed files or directories')
    for path in scope:
        project_verify._source_path(path)
        if path == '.':
            raise ValueError('name the specific files or directories affected by the refactor')
    if len(set(scope)) != len(scope):
        raise ValueError('duplicate refactoring scope')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
        raise ValueError('declare one to eight observable cases')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'helper', 'args', 'exit_code', 'behavior'}:
            raise ValueError('each case requires id, helper, args, exit_code and behavior')
        ident = project_verify._slug(case['id'])
        if ident in ids:
            raise ValueError('duplicate refactoring case ID')
        ids.add(ident)
        helper = case['helper']
        if (not isinstance(helper, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', helper)
                or Path(helper).suffix not in project_verify.ENGINES):
            raise ValueError('case helper must be a named standalone file with a supported engine')
        project_verify._text(case['behavior'], 'observable behavior')
        if (not isinstance(case['args'], list) or len(case['args']) > 12
                or any(not isinstance(arg, str) or len(arg) > 256 or any(not ch.isprintable() for ch in arg)
                       for arg in case['args'])
                or type(case['exit_code']) is not int or not 0 <= case['exit_code'] <= 125):
            raise ValueError('invalid refactoring case arguments or exit code')
    return raw


def _deadline(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError('refactoring time must be between 1 and 86400 seconds')
    deadline = time.monotonic() + seconds

    def remaining():
        left = deadline - time.monotonic()
        if left < 1:
            raise ValueError('refactoring time budget expired')
        return left

    return remaining


def _memory(value):
    if type(value) is not int or not 128 <= value <= 8192:
        raise ValueError('memory must be between 128 and 8192 MiB')
    return value


def _output(repo, path):
    target = Path(path).expanduser().resolve()
    if target.is_relative_to(repo):
        raise ValueError('put refactoring evidence outside the target checkout')
    target.mkdir(mode=0o700, parents=True)
    return target


def _command(case):
    helper = case['helper']
    return [project_verify.ENGINES[Path(helper).suffix], '/workspace/' + helper, *case['args']]


def _observe(raw, command, image, memory, containers, *, source_root='/app'):
    container = raw.get('create_stdout', '').strip()
    diagnostics = raw.get('runtime_diagnostics', {})
    if (raw.get('command') != command or raw.get('image') != image or raw.get('source_root') != source_root
            or raw.get('memory_mb') != memory or raw.get('workspace_read_only') is not True
            or raw.get('container_created') is not True or raw.get('cleanup_confirmed') is not True
            or raw.get('create_exit_code') != 0 or raw.get('timed_out') is not False or raw.get('error')
            or raw.get('stdout_truncated') is not False or raw.get('stderr_truncated') is not False
            or diagnostics.get('classification') == 'docker-oom'
            or not SHA.fullmatch(container) or container in containers or type(raw.get('exit_code')) is not int
            or not 0 <= raw['exit_code'] <= 125
            or not all(SHA.fullmatch(str(raw.get(name))) for name in OBSERVATION_FIELDS[1:])):
        raise ValueError('refactoring comparison requires fresh, complete and cleaned sandbox runs')
    if raw['exit_code'] and diagnostics.get('state') != {
            'status': 'exited', 'exit_code': raw['exit_code'], 'oom_killed': False}:
        raise ValueError('nonzero application exit was not confirmed by Docker')
    containers.add(container)
    return {name: raw[name] for name in OBSERVATION_FIELDS}


def _cases(root, spec, frozen, commit, image, memory, remaining, sandbox, containers, records):
    for case in spec['cases']:
        ident, helper, command = case['id'], case['helper'], _command(case)
        items = records.setdefault(ident, [])
        for attempt in (1, 2):
            with tempfile.TemporaryDirectory(prefix='iboard-refactor-driver-') as scratch:
                (Path(scratch) / helper).write_bytes(frozen[helper])
                raw = sandbox(scratch, image, command, remaining(), memory_mb=memory,
                              workspace_read_only=True, source_root='/app')
            record = root / f'{ident}-{attempt}.json'
            cli.write(record, {'commit': commit, 'case': ident, 'round': attempt,
                               'helper_sha256': _digest(frozen[helper]), 'execution': raw})
            items.append({'report': record.name, 'sha256': cli.digest(record)})
            _observe(raw, command, image, memory, containers)


def _receipts(root, records, spec, frozen, commit, image, memory, containers):
    if set(records) != {case['id'] for case in spec['cases']}:
        raise ValueError('refactoring receipt case list changed')
    observed = {}
    for case in spec['cases']:
        ident = case['id']
        if not isinstance(records[ident], list) or len(records[ident]) != 2:
            raise ValueError('refactoring requires two receipts per case')
        observations = []
        for attempt, ref in enumerate(records[ident], 1):
            name = f'{ident}-{attempt}.json'
            content = _read(root / name, 12 * MAX_OUTPUT + 512_000)
            if ref != {'report': name, 'sha256': _digest(content)}:
                raise ValueError(f'refactoring receipt changed for {ident}')
            saved = json.loads(content)
            if (saved.get('commit') != commit or saved.get('case') != ident or saved.get('round') != attempt
                    or saved.get('helper_sha256') != _digest(frozen[case['helper']])):
                raise ValueError(f'refactoring receipt has the wrong source or helper for {ident}')
            observations.append(_observe(saved['execution'], _command(case), image, memory, containers))
        if observations[0] != observations[1]:
            raise ValueError(f'refactoring observations are unstable for {ident}')
        observed[ident] = observations[0]
    return observed


def freeze(*, repo, base, image, manifest, helpers, out, seconds=600, memory_mb=1024,
           sandbox=run_sandbox, attest=workflow._verified_image):
    repo = Path(repo).expanduser().resolve(strict=True)
    remaining = _deadline(seconds)
    memory = _memory(memory_mb)
    base = cli.sha(repo, base, timeout=min(15, remaining()))
    if not COMMIT.fullmatch(base):
        raise ValueError('refactoring requires a SHA-1 Git commit')
    project_triage._image_reference(image)
    spec = _contract(json.loads(_read(Path(manifest), 120_000)))
    helper_dir = Path(helpers).expanduser().absolute()
    frozen = {name: project_verify._helper(helper_dir, name) for name in {c['helper'] for c in spec['cases']}}
    root = _output(repo, out)
    bundle = root / 'helpers'
    bundle.mkdir(mode=0o700)
    for name, data in frozen.items():
        (bundle / name).write_bytes(data)
    plan = {'schema': cli.SCHEMA, 'source': SOURCE, 'status': 'inconclusive', 'baseline': base,
            'spec': spec, 'helpers': {name: _digest(data) for name, data in frozen.items()},
            'memory_mb': memory, 'runs': {}}
    try:
        pinned = attest(repo, image, min(180, remaining()), commit=base)
        if not IMAGE.fullmatch(pinned):
            raise ValueError('baseline needs an immutable local image')
        plan['image'] = pinned
        _cases(root, spec, frozen, base, pinned, memory, remaining, sandbox, set(), plan['runs'])
        plan['observed'] = _receipts(root, plan['runs'], spec, frozen, base, pinned, memory, set())
        if any(plan['observed'][case['id']]['exit_code'] != case['exit_code'] for case in spec['cases']):
            raise ValueError('baseline exited differently from the declared behavior contract')
        plan.update(status='frozen', frozen_at=cli.now())
    except ERRORS as error:
        plan['error'] = str(error)
    cli.write(root / 'contract.json', plan)
    return plan


def _steps(repo, base, head, scope, remaining):
    paths_by_step = {}
    previous = base
    revisions = cli.git(repo, 'rev-list', '--first-parent', '--max-count=9', head, '^' + base,
                        timeout=min(15, remaining())).splitlines()[::-1]
    if not 1 <= len(revisions) <= 8:
        raise ValueError('verify one to eight ordered commits against the frozen baseline')
    for current in revisions:
        row = cli.git(repo, 'rev-list', '--parents', '-n', '1', current, timeout=min(15, remaining())).split()
        if row != [current, previous]:
            raise ValueError('candidate must be a linear sequence descended from the frozen baseline')
        paths = cli.git(repo, 'diff', '--name-only', '--no-renames', '-z', previous, current, '--',
                        timeout=min(15, remaining())).rstrip('\0').split('\0')
        if not all(path and any(path == item or path.startswith(item + '/') for item in scope) for path in paths):
            raise ValueError(f'refactoring commit changed material outside its declared scope: {current}')
        if any(Path(path).name in {'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'package-lock.json',
                                   'yarn.lock', 'bun.lock', 'bun.lockb', 'requirements.txt', 'pyproject.toml',
                                   'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', '.npmrc'}
               or path.startswith('patches/') for path in paths):
            raise ValueError('refactoring sandbox preserves the baseline dependencies')
        paths_by_step[current] = paths
        previous = current
    return paths_by_step


def load_frozen(contract, contract_sha256):
    """Load the pinned baseline and recompute its observations from the original receipts."""
    contract = Path(contract).expanduser().absolute()
    content = _read(contract, 512_000)
    if contract_sha256 != _digest(content):
        raise ValueError('frozen behavior contract SHA-256 does not match')
    plan = json.loads(content)
    if (not isinstance(plan, dict) or type(plan.get('schema')) is not int or plan['schema'] != cli.SCHEMA
            or plan.get('source') != SOURCE or plan.get('status') != 'frozen' or 'error' in plan):
        raise ValueError('a completed refactoring freeze is required')
    base, image, memory = plan['baseline'], plan['image'], _memory(plan['memory_mb'])
    if not COMMIT.fullmatch(base) or not IMAGE.fullmatch(image):
        raise ValueError('invalid frozen baseline source or image')
    spec = _contract(plan['spec'])
    frozen = {name: project_verify._helper(contract.parent / 'helpers', name)
              for name in {case['helper'] for case in spec['cases']}}
    if plan['helpers'] != {name: _digest(data) for name, data in frozen.items()}:
        raise ValueError('a frozen refactoring helper changed')
    containers = set()
    observations = _receipts(contract.parent, plan['runs'], spec, frozen, base, image, memory, containers)
    if plan['observed'] != observations or any(observations[case['id']]['exit_code'] != case['exit_code']
                                                for case in spec['cases']):
        raise ValueError('recorded baseline does not satisfy the frozen behavior contract')
    return plan, frozen, observations, containers


def verify(*, repo, contract, contract_sha256, candidate, out, seconds=600,
           sandbox=run_sandbox, attest=workflow._verified_image, derive=project_triage._derive):
    repo = Path(repo).expanduser().resolve(strict=True)
    remaining = _deadline(seconds)
    plan, frozen, observations, containers = load_frozen(contract, contract_sha256)
    base, image, memory, spec = plan['baseline'], plan['image'], plan['memory_mb'], plan['spec']
    head = cli.sha(repo, candidate, timeout=min(15, remaining()))
    steps = _steps(repo, base, head, spec['scope'], remaining)
    root = _output(repo, out)
    result = {'schema': cli.SCHEMA, 'source': SOURCE, 'status': 'inconclusive', 'baseline': base,
              'candidate': head, 'contract_sha256': contract_sha256, 'steps': []}
    try:
        for number, (revision, paths) in enumerate(steps.items(), 1):
            stage = root / f'step-{number}'
            stage.mkdir(mode=0o700)
            item = {'commit': revision, 'paths': paths, 'runs': {}, 'status': 'inconclusive'}
            result['steps'].append(item)
            selected = derive(repo, base, revision, image, remaining())
            if not IMAGE.fullmatch(selected) or attest(repo, selected, min(180, remaining()), commit=revision) != selected:
                raise ValueError('refactoring candidate image does not match its commit')
            item['image'] = selected
            _cases(stage, spec, frozen, revision, selected, memory, remaining, sandbox,
                   set(containers), item['runs'])
            observed = _receipts(stage, item['runs'], spec, frozen, revision, selected, memory, containers)
            item['observed'] = observed
            differences = {key: [field for field in OBSERVATION_FIELDS if observed[key][field] != old[field]]
                           for key, old in observations.items() if observed[key] != old}
            if differences:
                item.update(status='behavior-changed', differences=differences)
                result['status'] = 'behavior-changed'
                break
            item['status'] = 'equivalent'
        else:
            result['status'] = 'equivalent'
    except ERRORS as error:
        result['error'] = str(error)
    cli.write(root / 'report.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest='phase', required=True)
    for name in ('freeze', 'verify'):
        command = phases.add_parser(name)
        command.add_argument('--repo', type=Path, required=True)
        command.add_argument('--out', type=Path, required=True)
        command.add_argument('--seconds', type=int, default=600)
    freezer, checker = phases.choices['freeze'], phases.choices['verify']
    freezer.add_argument('--base', required=True)
    freezer.add_argument('--image', required=True)
    freezer.add_argument('--manifest', type=Path, required=True)
    freezer.add_argument('--helpers', type=Path, required=True)
    freezer.add_argument('--memory-mb', type=int, default=1024)
    checker.add_argument('--contract', type=Path, required=True)
    checker.add_argument('--contract-sha256', required=True)
    checker.add_argument('--candidate', required=True)
    values = vars(parser.parse_args(argv))
    phase = values.pop('phase')
    try:
        result = (freeze if phase == 'freeze' else verify)(**values)
    except ERRORS as error:
        parser.error(str(error))
    output = values['out'].resolve() / ('contract.json' if phase == 'freeze' else 'report.json')
    print(f"{result['status']}: {output}")
    if result['status'] == 'frozen':
        print(f"SHA-256 {cli.digest(output)}")
    return 0 if result['status'] in ('frozen', 'equivalent') else 1


if __name__ == '__main__':
    raise SystemExit(main())
