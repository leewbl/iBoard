"""Replay a repository guard on independent historical mistakes and the current code."""

import argparse
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from tempfile import TemporaryDirectory

from runner.sandbox import MAX_OUTPUT, run_sandbox
from workbench import autopilot, cli, project_verify, refactor, stack_rebase, swarm


SOURCE = 'skills/correct/SKILL.md'
PROBE = '.iboard-correction-probe'
MAX_SOURCE = 16_000_000
MEMORY = 512
ERRORS = (*swarm.ERRORS, autopilot.AutopilotError, tarfile.TarError)
LEVELS = ('architecture', 'type', 'lint', 'behavior-test')
KEY_HEADER = re.compile(rb'(?m)^-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\r ]*$')


class CorrectionError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise CorrectionError(message)


def _git(repo, *args, remaining=lambda: 15, limit=MAX_SOURCE):
    raw = stack_rebase._output(stack_rebase._git(repo, '--literal-pathspecs', *args, seconds=min(15, remaining())))
    _require(len(raw) <= limit, 'historical source or history exceeds its scope')
    return raw


def _revision(repo, value, remaining):
    commit = _git(repo, 'rev-parse', '--verify', '--end-of-options', value + '^{commit}',
                  remaining=remaining, limit=100).decode().strip()
    _require(swarm.SHA.fullmatch(commit), 'select a valid Git commit')
    return commit


def _paths(value):
    _require(isinstance(value, list) and 1 <= len(value) <= 24 and len(set(value)) == len(value),
             'scope must name one to twenty-four distinct source paths')
    for path in value:
        project_verify._source_path(path)
        _require(not ({'.', '.git', PROBE} & set(path.split('/'))), 'probe and Git metadata are reserved')
    return value


def _settings(spec):
    _require(set(spec) == {'schema', 'rule', 'scope', 'cases', 'driver', 'image', 'seconds'},
             'invalid correction manifest')
    rule = spec['rule']
    _require(set(rule) == {'id', 'description', 'level', 'mechanism', 'higher_levels'}
             and swarm.IDENT.fullmatch(rule['id']) and rule['level'] in LEVELS
             and all(swarm._bounded(rule[field], 1600) for field in ('description', 'mechanism')),
             'declare the recurring error, preferred level and enforcement mechanism')
    higher = rule['higher_levels']
    _require(isinstance(higher, dict) and set(higher) == set(LEVELS[:LEVELS.index(rule['level'])])
             and all(swarm._bounded(note, 1000) for note in higher.values()),
             'explain why each stronger form of enforcement is insufficient')
    scope = _paths(spec['scope'])
    cases = spec['cases']
    _require(isinstance(cases, list) and 2 <= len(cases) <= 5, 'a recurring mistake needs at least two separate events')
    objects = set()
    for item in cases:
        _require(set(item) == {'id', 'bad', 'fix', 'paths', 'observation'}
                 and swarm.IDENT.fullmatch(item['id']) and swarm._bounded(item['observation'], 1600)
                 and all(swarm.SHA.fullmatch(item[field]) for field in ('bad', 'fix')), 'invalid history case')
        _paths(item['paths'])
        _require(all(any(p == root or p.startswith(root + '/') for root in scope) for p in item['paths']),
                 'the historical evidence must fall in the source scope')
        _require(item['bad'] != item['fix'] and item['id'] not in objects and item['fix'] not in objects
                 and item['bad'] not in objects, 'each case needs different before and repair commits')
        objects.update((item['id'], item['bad'], item['fix']))
    driver = spec['driver']
    _require(set(driver) == {'path', 'engine'} and driver['engine'] in ('node', 'python3')
             and swarm.IMAGE.fullmatch(spec['image']) and swarm._number(spec['seconds'], 15, 900),
             'pin the local image, probe engine and total replay budget')
    _paths([driver['path']])


def _driver(repo, revision, name, remaining):
    row = _git(repo, 'ls-tree', revision, '--', name, remaining=remaining, limit=1200).split()
    _require(len(row) == 4 and row[0] in (b'100644', b'100755') and row[1] == b'blob',
             'driver must be a regular committed file')
    size = int(_git(repo, 'cat-file', '-s', row[2].decode(), remaining=remaining, limit=40))
    _require(0 < size <= 60_000, 'historical driver exceeds its size bound')
    content = _git(repo, 'cat-file', 'blob', row[2].decode(), remaining=remaining, limit=60_000)
    autopilot._reject_credential_artifact(name, content)
    return content


def _history(repo, spec, remaining):
    rows = []
    for item in spec['cases']:
        parents = _git(repo, 'rev-list', '--parents', '-n', '1', item['fix'], remaining=remaining,
                       limit=200).decode().split()
        _require(parents == [item['fix'], item['bad']], 'historical bad commit must be the repair parent')
        changes = _git(repo, 'diff', '--name-only', '--no-renames', '-z', item['bad'], item['fix'],
                       '--', *item['paths'], remaining=remaining, limit=80_000).decode().strip('\0').split('\0')
        _require(any(changes), 'historical repair did not modify the named paths')
        subject = _git(repo, 'show', '-s', '--format=%s', item['fix'], remaining=remaining, limit=3000).decode().strip()
        rows.append({'id': item['id'], 'changes': changes, 'subject': subject})
    return rows


def _read_approval(path, sha256):
    _require(isinstance(sha256, str) and refactor.SHA.fullmatch(sha256), 'use the approved SHA-256')
    return swarm._read(Path(path).expanduser(), sha256)


def freeze(manifest, repo, current, driver_revision, out):
    spec = swarm._read(Path(manifest).expanduser())
    _settings(spec)
    repo = Path(repo).expanduser().resolve(strict=True)
    remaining = refactor._deadline(spec['seconds'])
    candidate, revision = (_revision(repo, value, remaining) for value in (current, driver_revision))
    driver = _driver(repo, revision, spec['driver']['path'], remaining)
    record = {'schema': cli.SCHEMA, 'source': SOURCE, 'repo': str(repo), 'spec': spec,
              'candidate': candidate, 'driver_revision': revision, 'driver_sha256': refactor._digest(driver),
              'history': _history(repo, spec, remaining)}
    out = Path(out).expanduser().resolve()
    _require(not out.is_relative_to(repo) and not out.exists(), 'use a new plan outside the target checkout')
    cli.write(out, record)
    return {'plan': str(out), 'sha256': cli.digest(out)}


def _load(plan, sha256):
    approved = _read_approval(plan, sha256)
    repo = Path(approved['repo']).resolve(strict=True)
    spec = approved['spec']
    _settings(spec)
    remaining = refactor._deadline(spec['seconds'])
    driver = _driver(repo, approved['driver_revision'], spec['driver']['path'], remaining)
    _require(approved['source'] == SOURCE and approved['history'] == _history(repo, spec, remaining)
             and approved['driver_sha256'] == refactor._digest(driver)
             and _revision(repo, approved['candidate'], remaining) == approved['candidate']
             and _revision(repo, approved['driver_revision'], remaining) == approved['driver_revision'],
             'approval source or historical inputs changed')
    return repo, approved, spec, driver, remaining


def _snapshot(repo, commit, scope, destination, remaining):
    raw = _git(repo, 'archive', '--format=tar', commit, '--', *scope, remaining=remaining)
    count = 0
    files = {}
    with tarfile.open(fileobj=BytesIO(raw), mode='r:') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if member.isdir():
                continue
            _require(member.isfile() and not name.is_absolute() and set(name.parts).isdisjoint({'..', '.git', PROBE})
                     and member.size <= 3_000_000 and str(name) not in files and count < 650,
                     'archives for historical probes need bounded regular source files')
            count += 1
            data = archive.extractfile(member).read()
            autopilot._reject_credential_artifact(str(name))
            _require(not KEY_HEADER.search(data), 'private-key material cannot enter a replay snapshot')
            file = destination / name
            file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            file.write_bytes(data)
            file.chmod(0o700 if member.mode & 0o111 else 0o600)
            files[str(name)] = refactor._digest(data)
    _require(files, 'selected revision has no source snapshot')
    return files


def _entries(approved):
    for case in approved['spec']['cases']:
        yield case['id'], 'historical', case['bad'], 7
        yield case['id'], 'repaired', case['fix'], 0
        yield case['id'], 'current', approved['candidate'], 0


def _command(spec, case):
    return [spec['driver']['engine'], '/workspace/' + PROBE, case]


def _observe(raw, observation, spec, entry, containers):
    name, _, _, code = entry
    refactor._observe(raw, _command(spec, name), spec['image'], MEMORY, containers, source_root='/workspace')
    marker = f'GUARD_{"HOLDS" if code == 0 else "MISSED"} {name}'
    _require(raw['exit_code'] == code and raw.get('stdout') == marker + '\n',
             f'{name} did not produce the required historical or current observation')
    _require(raw['stdout_sha256'] == refactor._digest(raw['stdout'].encode())
             and raw['stderr_sha256'] == refactor._digest(raw.get('stderr', '').encode()),
             'sandbox output hashes changed')
    return {'exit_code': code, 'marker': marker, 'container': raw['create_stdout'].strip(),
            'snapshot': observation}


def _probe(repo, spec, driver, entry, remaining, sandbox):
    name, _, commit, _ = entry
    with TemporaryDirectory(prefix='iboard-correction-source-') as scratch:
        directory = Path(scratch)
        source = _snapshot(repo, commit, spec['scope'], directory, remaining)
        (directory / PROBE).write_bytes(driver)
        execution = sandbox(directory, spec['image'], _command(spec, name), remaining(),
                            memory_mb=MEMORY, workspace_read_only=True)
        _require(execution.get('workspace') == str(directory.resolve()), 'sandbox used a different source directory')
    return {'schema': cli.SCHEMA, 'commit': commit, 'case': name, 'source': source, 'execution': execution,
            'driver_sha256': refactor._digest(driver), 'removed': not directory.exists()}


def run(plan, sha256, out, *, sandbox=run_sandbox):
    repo, approved, spec, driver, remaining = _load(plan, sha256)
    _require(not Path(out).expanduser().exists(), 'use a new historical replay output directory')
    directory = refactor._output(repo, out)
    path = directory / 'result.json'
    state = {'schema': cli.SCHEMA, 'plan_sha256': sha256, 'status': 'blocked', 'runs': []}
    containers = set()
    try:
        for index, entry in enumerate(_entries(approved), 1):
            proof = _probe(repo, spec, driver, entry, remaining, sandbox)
            receipt = directory / f'run-{index}.json'
            cli.write(receipt, proof)
            item = {'file': receipt.name, 'sha256': cli.digest(receipt), 'role': entry[1]}
            state['runs'].append(item)
            _require(proof['removed'], 'historical source cleanup was not confirmed')
            item['observation'] = _observe(proof['execution'], proof['source'], spec, entry, containers)
        state['status'] = 'proved'
    except ERRORS as error:
        state['error'] = str(error)[:1200]
    cli.write(path, state)
    return {**state, 'report': str(path), 'sha256': cli.digest(path)}


def audit(plan, sha256, report, report_sha256):
    repo, approved, spec, driver, remaining = _load(plan, sha256)
    path = Path(report).expanduser()
    state = _read_approval(path, report_sha256)
    entries = list(_entries(approved))
    _require(state['plan_sha256'] == sha256 and state['status'] == 'proved' and len(state['runs']) == len(entries),
             'need a complete correction replay')
    containers = set()
    for index, (reference, entry) in enumerate(zip(state['runs'], entries), 1):
        name, role, commit, _ = entry
        filename = f'run-{index}.json'
        saved = swarm._read(path.parent / filename, reference['sha256'], limit=12 * MAX_OUTPUT + 512_000)
        _require(reference['file'] == filename and reference['role'] == role and saved['case'] == name
                 and saved['commit'] == commit and saved['driver_sha256'] == refactor._digest(driver)
                 and saved['removed'] is True, 'saved replay binds to different source or driver')
        with TemporaryDirectory(prefix='iboard-correction-audit-') as scratch:
            source = _snapshot(repo, commit, spec['scope'], Path(scratch), remaining)
        _require(saved['source'] == source and reference['observation'] == _observe(
            saved['execution'], source, spec, entry, containers), 'replay output or source no longer matches')
    return {'schema': cli.SCHEMA, 'rule': spec['rule']['id'], 'status': 'proved', 'runs': len(containers),
            'cases': [case['id'] for case in spec['cases']], 'candidate': approved['candidate']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action, values in [('freeze', ('manifest', 'repo', 'current', 'driver-revision', 'out')),
                           ('run', ('plan', 'sha256', 'out')),
                           ('audit', ('plan', 'sha256', 'report', 'report-sha256'))]:
        child = sub.add_parser(action)
        for name in values:
            child.add_argument('--' + name, required=True)
    args = vars(parser.parse_args(argv))
    operation = {'freeze': freeze, 'run': run, 'audit': audit}[args.pop('action')]
    try:
        result = operation(**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(result.get('status') == 'blocked')


if __name__ == '__main__':
    raise SystemExit(main())
