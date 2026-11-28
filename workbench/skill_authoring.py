"""Author a skill and retain revision-bound proof for the normal PR gate."""

import argparse
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from runner.sandbox import MAX_OUTPUT, SandboxError, run_sandbox
from workbench import cli, correct, project_verify, refactor, skill_sources
from workbench.automate_me import SHA, _digest, _file, _keep, _read, _require
from workbench.task_lock import locked_task


SOURCE = 'skills/poteto-mode/playbooks/authoring-a-skill.md'
ERRORS = (*correct.ERRORS, OSError, ValueError, KeyError, TypeError, SandboxError, subprocess.SubprocessError)


def draft(*, name, description, body, root):
    root = Path(root).expanduser().resolve(strict=True)
    _require(root.is_dir() and skill_sources.NAME.fullmatch(name) and len(name) <= 64, 'choose a skill root and slug')
    _require(isinstance(description, str) and description == description.strip() and description.isprintable(),
             'describe the skill in one line')
    content = _read(body, 300_000).decode('utf-8').strip()
    markdown = f'---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{content}\n'
    skill_sources._metadata(markdown, name)
    path = root / name
    path.mkdir(mode=0o700)
    guide = path / 'SKILL.md'
    with guide.open('x', encoding='utf-8') as stream:
        stream.write(markdown)
    return {'path': str(guide), 'sha256': cli.digest(guide)}


def _spec(probe, include, image, memory, static, tree):
    if not probe:
        _require(not (include or image), 'supply a structural probe when using an image or source scope')
        _require(not static['structural'], 'changed skill scripts require a committed structural probe')
        return None
    _require(refactor.IMAGE.fullmatch(image or ''), 'structural probes require a local immutable image ID')
    _require(1 <= len(probe) <= 6 and len(set(probe)) == len(probe), 'select one to six distinct structural probes')
    refactor._memory(memory)
    for path in probe:
        project_verify._source_path(path)
        _require(Path(path).suffix in project_verify.ENGINES and tree.get(path, ('',))[0] in skill_sources.REGULAR,
                 'each structural probe needs a supported tracked script')
    scopes = sorted(set(probe) | set(include) | {
        root for root in static['authored'] if any(name.startswith(root + '/') for name in tree)})
    correct._paths(scopes)
    return {'probe': list(probe), 'scope': scopes, 'image': image, 'memory_mb': memory}


def _command(path):
    return [project_verify.ENGINES[Path(path).suffix], '/workspace/' + path]


def check(*, repo, base, candidate, out, probe=(), include=(), image=None, memory_mb=512, seconds=300,
          sandbox=run_sandbox):
    repo = Path(repo).expanduser().resolve(strict=True)
    _require(Path(skill_sources._run(repo, 'rev-parse', '--show-toplevel')).resolve() == repo,
             'choose the target worktree root')
    remaining = refactor._deadline(seconds)
    base = correct._revision(repo, base, remaining)
    head = correct._revision(repo, candidate, remaining)
    skill_sources._run(repo, 'merge-base', '--is-ancestor', base, head)
    target = Path(out).expanduser().absolute()
    _require(not target.exists() and not target.is_symlink(), 'choose a new skill authoring receipt')
    static = skill_sources.compare(repo, base, head)
    tree = skill_sources._tree(repo, head)
    spec = _spec(probe, include, image, memory_mb, static, tree)
    _require(static['authored'] or static['affected'], 'candidate does not change any skill or referenced file')
    report = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'skill-authoring', 'base': base,
              'candidate': head, 'static': static, 'spec': spec, 'runs': [], 'status': 'failed'}
    if not static['problems']:
        try:
            if spec:
                _run_cases(repo, target, report, remaining, sandbox)
            report['status'] = 'passed'
        except ERRORS as error:
            report['error'] = str(error)
    return {**_keep(target, report), 'status': report['status'], 'checks': len(report['runs']),
            'problems': static['problems'], 'error': report.get('error')}


def _observation(execution, spec, probe, containers):
    try:
        result = refactor._observe(execution, _command(probe), spec['image'], spec['memory_mb'],
                                   containers, source_root='/workspace')
    except ValueError as error:
        raise ValueError('probe lacks a fresh, complete, cleaned sandbox receipt') from error
    _require(result['exit_code'] == 0, f'structural probe failed: {probe}')


def _run_ref(path, receipt, head, probe, repeat):
    return {'file': path.name, 'sha256': receipt['sha256'], 'candidate': head, 'probe': probe, 'repeat': repeat}


def _run_cases(repo, target, report, remaining, sandbox):
    containers = set()
    spec, head = report['spec'], report['candidate']
    blobs = skill_sources._tree(repo, head)
    with skill_sources._blobs(repo) as read:
        digests = [_digest(read(blobs[path][1]).encode('utf-8')) for path in spec['probe']]
    for number, (probe, digest) in enumerate(zip(spec['probe'], digests), 1):
        for repeat in (1, 2):
            with TemporaryDirectory(prefix='iboard-skill-probe-') as scratch:
                scope = correct._snapshot(repo, head, spec['scope'], Path(scratch), remaining)
                _require(scope.get(probe) == digest, 'source snapshot did not contain the committed probe')
                raw = sandbox(scratch, spec['image'], _command(probe), remaining(),
                              memory_mb=spec['memory_mb'], workspace_read_only=True)
            path = target.with_name(f'{target.stem}.probe-{number}-{repeat}.json')
            receipt = _keep(path, {'candidate': head, 'probe': probe, 'repeat': repeat,
                                   'probe_sha256': digest, 'execution': raw})
            report['runs'].append(_run_ref(path, receipt, head, probe, repeat))
            _observation(raw, spec, probe, containers)


def _verify_cases(repo, path, data):
    spec, head, rows = data['spec'], data['candidate'], data['runs']
    if spec is None:
        _require(rows == [], 'unexpected structural results')
        _require(not data['static']['structural'], 'changed skill scripts require verified structural results')
        return
    tree = skill_sources._tree(repo, head)
    _require(spec == _spec(spec['probe'], spec['scope'], spec['image'], spec['memory_mb'], data['static'], tree)
             and len(rows) == 2 * len(spec['probe']), 'incomplete or changed structural results')
    containers = set()
    with skill_sources._blobs(repo) as read:
        for number, probe in enumerate(spec['probe'], 1):
            digest = _digest(read(tree[probe][1]).encode('utf-8'))
            for repeat in (1, 2):
                name = path.with_name(f'{path.stem}.probe-{number}-{repeat}.json')
                ref = rows[2 * (number - 1) + repeat - 1]
                _require(ref == _run_ref(name, ref, head, probe, repeat) and SHA.fullmatch(ref['sha256']),
                         'structural receipt identity differs')
                run = json.loads(_read(name, 12 * MAX_OUTPUT + 512_000, ref['sha256']))
                _require((run['candidate'], run['probe'], run['repeat'], run['probe_sha256']) ==
                         (head, probe, repeat, digest), 'structural result does not match the committed probe')
                _observation(run['execution'], spec, probe, containers)


def require(data, head):
    """Recheck the skill proof against the delivery candidate without rerunning any probes."""
    try:
        ref = data['skill_authoring']
        path = _file(ref['path'])
        _require(str(path) == ref['path'] and set(ref) == {'path', 'sha256'}
                 and isinstance(ref['sha256'], str) and SHA.fullmatch(ref['sha256']),
                 'skill receipt needs a stable path and SHA-256 digest')
        body = json.loads(_read(path, 2_500_000, ref['sha256']))
        base = data['base_commit']
        _require(body['schema'] == cli.SCHEMA and body['source'] == SOURCE and body['kind'] == 'skill-authoring'
                 and body['status'] == 'passed' and body['base'] == base and body['candidate'] == head,
                 'skill check must pass for the exact task baseline and candidate')
        repo = Path(data['repo']).resolve(strict=True)
        skill_sources._run(repo, 'merge-base', '--is-ancestor', base, head)
        static = skill_sources.compare(repo, base, head)
        _require(static == body['static'] and not static['problems'] and (static['authored'] or static['affected']),
                 'skill contents, metadata, or links no longer match the successful check')
        _verify_cases(repo, path, body)
        return {'sha256': ref['sha256'], 'authored': static['authored'], 'probes': len(body['runs'])}
    except ERRORS as error:
        raise cli.WorkbenchError(f'skill authoring evidence: {error}') from error


@locked_task('task_path')
def attach(task_path, receipt, sha256):
    task = Path(task_path).expanduser().resolve(strict=True)
    ref = {'path': str(_file(receipt)), 'sha256': sha256}
    data = cli.read(task)
    head = cli.sha(data['repo'], 'HEAD', timeout=20)
    data['skill_authoring'] = ref
    result = require(data, head)
    cli.write(task, data)
    return result


@locked_task('task_path')
def publish_checked(task_path, receipt, sha256, repository, github=None):
    from workbench import publish
    attach(task_path, receipt, sha256)
    return publish.publish(task_path, repository, github=github)


def main(argv=None):
    from workbench.publish import PublishError
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    build = sub.add_parser('draft')
    for key in ('name', 'description', 'body', 'root'):
        build.add_argument('--' + key, required=True)
    scan = sub.add_parser('check')
    for key in ('repo', 'base', 'candidate', 'out'):
        scan.add_argument('--' + key, required=True)
    for key in ('probe', 'include'):
        scan.add_argument('--' + key, action='append', default=[])
    scan.add_argument('--image')
    scan.add_argument('--memory-mb', type=int, default=512)
    scan.add_argument('--seconds', type=int, default=300)
    for name in ('attach', 'publish'):
        command = sub.add_parser(name)
        command.add_argument('--task', required=True, dest='task_path')
        command.add_argument('--receipt', required=True)
        command.add_argument('--sha256', required=True)
        if name == 'publish':
            command.add_argument('--repository', required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'draft': draft, 'check': check, 'attach': attach, 'publish': publish_checked}[action](**args)
    except ERRORS + (PublishError,) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if action == 'check' and result['status'] != 'passed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
