"""Build a short, independently verified pull request description for an exact task."""

import argparse
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import quote

from workbench import cli, delivery, run_control
from workbench.automate_me import SHA, _file, _keep, _read, _require
from workbench.task_lock import locked_task


SOURCE = 'skills/poteto-mode/playbooks/opening-a-pr.md'
TYPES = {'fix', 'feat', 'refactor', 'test', 'docs', 'perf', 'chore', 'build', 'ci'}
SCOPE = re.compile(r'[a-z0-9][a-z0-9._-]{0,39}')
SYMBOL = re.compile(r'[A-Za-z_][A-Za-z0-9_$]{0,99}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
REQUIRED = {'type', 'scope', 'subject', 'why', 'changes'}
OPTIONAL = {'tradeoff', 'symbol', 'parent_pr'}
ERRORS = (OSError, ValueError, KeyError, TypeError, cli.WorkbenchError, delivery.DeliveryError, run_control.ControlError)
TEST_HEADINGS = {'Reproduction', 'Resolution', 'Test Result', 'Tdd Red', 'Tdd Green'}
LINK_HEADINGS = {'Screenshot', 'Functional documentation'}
EVIDENCE_HEADINGS = {'Root cause', 'Visual parity', 'Refactoring equivalence', 'Skill verification',
                     'Phase acceptance', 'Offline OpenClaw verification', 'Performance verification',
                     'Blast-radius verification'}


def _line(value, limit):
    _require(isinstance(value, str) and value == value.strip() and 1 <= len(value) <= limit
             and value.isprintable(), 'PR briefing text must be a bounded single line')
    return value


def _markdown(value, limit=280):
    text = escape(_line(value, limit), quote=False).replace('@', '&#64;')
    return re.sub(r'([\\\x60*_~\[\]])', r'\\\1', text)


def _symbol_token(symbol):
    return r'(?<![A-Za-z0-9_$])' + re.escape(symbol) + r'(?![A-Za-z0-9_$])'


def _request(spec):
    _require(isinstance(spec, dict) and REQUIRED <= spec.keys() <= REQUIRED | OPTIONAL,
             'brief requires type, scope, subject, why, and changes')
    _require(spec['type'] in TYPES and SCOPE.fullmatch(spec['scope']), 'choose a conventional PR type and scope')
    _line(spec['subject'], 86)
    _line(spec['why'], 300)
    changes = spec['changes']
    _require(isinstance(changes, list) and 1 <= len(changes) <= 4, 'describe one to four meaningful changes')
    for change in changes:
        _line(change, 250)
    if 'tradeoff' in spec:
        _line(spec['tradeoff'], 260)
    if 'symbol' in spec:
        pin = spec['symbol']
        _require(isinstance(pin, dict) and set(pin) == {'name', 'path'} and SYMBOL.fullmatch(pin['name'])
                 and re.search(_symbol_token(pin['name']), spec['subject']), 'name the selected source symbol in the title')
        _line(pin['path'], 300)
    if 'parent_pr' in spec:
        _require(type(spec['parent_pr']) is int and 1 <= spec['parent_pr'] <= 2_147_483_647,
                 'select a positive parent PR number')
    return spec


def _git(repo, *args):
    return cli.git(repo, *args, timeout=20)


def _scope(repo, base, head, spec):
    _git(repo, 'merge-base', '--is-ancestor', base, head)
    changed = [value for value in _git(repo, 'diff', '--name-only', '--no-renames', '-z',
                                      base, head, '--').split('\0') if value]
    _require(changed, 'the candidate has no changes relative to its verified base')
    _git(repo, 'diff', '--check', base, head, '--')
    commits = int(_git(repo, 'rev-list', '--count', f'{base}..{head}'))
    pin = spec.get('symbol')
    if pin:
        _require(pin['path'] in changed, 'the named symbol must belong to a changed file')
        token = _symbol_token(pin['name'])
        for commit in (head, base):
            blob = f"{commit}:{pin['path']}"
            try:
                if int(_git(repo, 'cat-file', '-s', blob)) > 512_000:
                    continue
                source = _git(repo, 'cat-file', 'blob', blob)
            except cli.WorkbenchError:
                continue
            if '\0' not in source and re.search(token, source):
                break
        else:
            raise ValueError('the named symbol was not observed in the candidate or baseline file')
    return {'files': len(changed), 'commits': commits, 'paths': changed[:4]}


def _impact(data, review):
    if not data.get('impact_review'):
        return None
    report = json.loads(_read(review['path'], 2_000_000, review['sha256']))
    impact = report['impact']
    return {name: impact[name] for name in ('surface', 'invariant', 'observed', 'path', 'line')}


def _parent_from_forge(data, repo, number, github):
    result = github.pull(repo, number)
    branch = result.get('headRefName')
    _require(result.get('number') == number and result.get('state') == 'OPEN'
             and result.get('isCrossRepository') is False and result.get('headRefOid') == data['base_commit']
             and isinstance(branch, str) and branch not in {'main', f"workbench/{data['id']}"},
             'parent PR must be open in the destination at the verified task base')
    _git(data['repo'], 'check-ref-format', f'refs/heads/{branch}')
    return {'pr': number, 'branch': branch, 'head': data['base_commit']}


def verify_parent(github, repo, data, parent):
    try:
        _require(parent == _parent_from_forge(data, repo, parent['pr'], github)
                 and github.branch(repo, parent['branch']) == parent['head'],
                 'parent PR or base branch changed since verification')
    except ERRORS as error:
        raise delivery.DeliveryError(f'PR stack base: {error}') from error


def _snapshot(data, head, repository, spec, parent):
    _require(REPO.fullmatch(repository) and all(part not in {'.', '..'} for part in repository.split('/')),
             'select a GitHub owner/repository')
    _require((data.get('delivery_repo') or repository).casefold() == repository.casefold(),
             'the briefing destination differs from the task destination')
    _require(_git(data['repo'], 'rev-parse', 'HEAD') == head and
             not _git(data['repo'], '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all'),
             'PR briefing requires the exact clean task worktree')
    base = data['base_commit']
    if 'parent_pr' in spec:
        _require(isinstance(parent, dict) and set(parent) == {'pr', 'branch', 'head'}
                 and parent['pr'] == spec['parent_pr'] and parent['head'] == base,
                 'stack briefing must pin the parent PR and verified base')
        _git(data['repo'], 'check-ref-format', f"refs/heads/{parent['branch']}")
    else:
        _require(parent is None, 'unexpected parent PR for the briefing')
    review = delivery._independent_verification(data, head, destination=repository)
    _require(isinstance(review.get('sha256'), str) and SHA.fullmatch(review['sha256']), 'invalid independent review digest')
    return {'schema': cli.SCHEMA, 'kind': 'pr-brief', 'source': SOURCE, 'task': data['id'],
            'title': f"{spec['type']}({spec['scope']}): {spec['subject']}",
            'repository': repository, 'base': base, 'head': head, 'parent': parent, 'spec': spec,
            'scope': _scope(data['repo'], base, head, spec),
            'verification': {'review_sha256': review['sha256'], 'impact': _impact(data, review)}}


def prepare(task_path, repository, manifest, out, *, github=None):
    data = cli.read(_file(task_path))
    _require('verification_target' not in data, 'verification-only tasks cannot hand off a PR')
    head, issues = cli.evidence_gate(data, 'HEAD')
    _require(not issues, 'publication evidence is incomplete: ' + ', '.join(issues))
    spec = _request(json.loads(_read(manifest, 40_000)))
    parent = None
    if 'parent_pr' in spec:
        _require(REPO.fullmatch(repository) and all(part not in {'.', '..'} for part in repository.split('/')),
                 'select a GitHub owner/repository')
        github = github or delivery.GitHub()
        parent = _parent_from_forge(data, repository, spec['parent_pr'], github)
        _require(github.branch(repository, parent['branch']) == parent['head'],
                 'parent branch differs from the verified task base')
    packet = _snapshot(data, head, repository, spec, parent)
    return {**_keep(out, packet), 'title': packet['title'], 'head': head}


def _load(ref):
    _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'}
             and isinstance(ref['sha256'], str) and SHA.fullmatch(ref['sha256']), 'PR briefing needs a pinned path and digest')
    path = _file(ref['path'])
    _require(str(path) == ref['path'], 'PR briefing file path changed')
    return json.loads(_read(path, 140_000, ref['sha256']))


def require(data, head, repository):
    packet = _load(data['pr_brief'])
    expected = _snapshot(data, head, repository, _request(packet['spec']), packet['parent'])
    _require(packet == expected, 'PR briefing is stale for the task, destination, candidate, or independent review')
    return packet


def _section(head, legacy, packet):
    result = [f"Candidate {head}. Independent review SHA-256 {packet['verification']['review_sha256']}."]
    groups = {}
    category = ''
    resolutions = []
    extra_proof = {}
    for line in legacy.splitlines():
        if line.startswith('## '):
            category = line[3:]
        elif category in LINK_HEADINGS and '[View ' in line and line.startswith('[View '):
            result.append(line)
        elif category in TEST_HEADINGS and line.startswith('- exit '):
            summary, marker, digest = line.partition('; report SHA-256 ')
            if marker and re.fullmatch(r'\x60[0-9a-f]{64}\x60', digest):
                groups.setdefault(category, []).append((summary.removeprefix('- '), digest))
        elif category in EVIDENCE_HEADINGS and 'SHA-256' in line and not line.startswith('    '):
            extra_proof.setdefault(category, []).append(line.removeprefix('- '))
        if re.fullmatch(r'Resolves #[1-9][0-9]*', line):
            resolutions.append(line)
    for category, rows in groups.items():
        summary = rows[0][0].partition('; commit ')[0].replace('; ', '. ')
        result.append(f"{category}: {len(rows)} verified report{'s' if len(rows) != 1 else ''}. "
                      f"{summary}. First report SHA-256 {rows[0][1]}.")
    for category, rows in extra_proof.items():
        suffix = f" ({len(rows)} linked receipts in the task record)" if len(rows) > 1 else ''
        result.append(rows[0] + suffix)
    return result, resolutions


def render(data, head, legacy):
    packet = require(data, head, data['delivery_repo'])
    spec, scope = packet['spec'], packet['scope']
    pin = spec.get('symbol')
    scope_text = [f"{scope['files']} changed files across {scope['commits']} commits from the verified base."]
    if scope['paths']:
        scope_text.append('Paths: ' + ', '.join(_markdown(path, 290) for path in scope['paths']
                                                if path.isprintable() and len(path) <= 290) + '.')
    if pin:
        scope_text.append('Symbol: ' + _markdown(pin['name']) + '.')
    if parent := packet['parent']:
        scope_text.append(f"Base: [PR #{parent['pr']}](https://github.com/"
                          f"{packet['repository']}/pull/{parent['pr']}) at {parent['head']}.")
    proof, resolutions = _section(head, legacy, packet)
    impact = packet['verification']['impact']
    impact_text = []
    if impact:
        from workbench.publish import _public_log_excerpt

        def observed(field):
            return _markdown(' '.join(_public_log_excerpt(impact[field]).split())[:260].strip())

        url = (f"https://github.com/{packet['repository']}/blob/{head}/"
               f"{quote(impact['path'], safe='/')}#L{impact['line']}")
        impact_text = [f"Surface: {observed('surface')} [source]({url}).",
                       'Invariant: ' + observed('invariant'), 'Observed: ' + observed('observed')]
    sections = [('Why', [_markdown(spec['why'], 300)]),
                ('What changed', [_markdown(line) for line in spec['changes']]), ('Scope', scope_text),
                ('Tradeoffs', [_markdown(spec['tradeoff'])] if spec.get('tradeoff') else []),
                ('Blast Radius', impact_text), ('Verification', proof)]
    blocks = [f'## {heading}\n' + '\n'.join('- ' + line for line in rows) for heading, rows in sections if rows]
    body = '\n\n'.join(blocks + resolutions) + '\n'
    _require(len(body.splitlines()) < 40 and len(body.encode()) <= 7500, 'verified briefing exceeds its publication bound')
    return body


@locked_task('task_path')
def attach(task_path, repository, briefing, sha256):
    path = _file(task_path)
    data = cli.read(path)
    _require('verification_target' not in data, 'verification-only tasks cannot hand off a PR')
    data['pr_brief'] = {'path': str(_file(briefing)), 'sha256': sha256}
    head = cli.sha(data['repo'], 'HEAD', timeout=20)
    packet = require(data, head, repository)
    cli.write(path, data)
    return {'title': packet['title'], 'head': head, 'sha256': sha256}


@locked_task('task_path')
def open_verified(task_path, repository, briefing, sha256, github=None):
    from workbench import publish
    run_control.admit(Path(task_path), publish.PublishError)
    attach(task_path, repository, briefing, sha256)
    return publish.publish(task_path, repository, github=github)


def main(argv=None):
    from workbench.publish import PublishError
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'attach', 'open'):
        stage = stages.add_parser(action)
        stage.add_argument('--task', dest='task_path', required=True)
        stage.add_argument('--repository', required=True)
        for label in ('manifest', 'out') if action == 'prepare' else ('briefing', 'sha256'):
            stage.add_argument('--' + label, required=True)
    options = vars(parser.parse_args(argv))
    action = options.pop('action')
    try:
        result = {'prepare': prepare, 'attach': attach, 'open': open_verified}[action](**options)
    except ERRORS + (PublishError,) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
