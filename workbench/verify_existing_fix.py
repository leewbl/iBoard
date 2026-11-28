"""Reproduce a reported failure and recheck its existing GitHub fix in offline Docker."""

import argparse
import json
from pathlib import Path
import re
import time

from workbench import cli, delivery, pr_watch, project_triage, project_verify


ARTIFACT = re.compile(fr'https://github\.com/({pr_watch.REPO.pattern})/(pull|commit)/([a-z0-9]+)')


class Reader(pr_watch.GitHubReader):
    def artifact(self, repo, kind, identity):
        endpoint = 'repos/' + repo
        if kind == 'pull':
            return self._call('api', '--method', 'GET', f'{endpoint}/pulls/{identity}')
        details = self._call('api', '--method', 'GET', endpoint)
        branch = details.get('default_branch', '') if isinstance(details, dict) else ''
        if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9._/-]+', branch) or '..' in branch:
            raise ValueError('GitHub did not identify the repository default branch')
        return self._call('api', '--method', 'GET', f'{endpoint}/compare/{identity}...{branch}')


def _select(repo, artifact, reader):
    match = ARTIFACT.fullmatch(artifact)
    if not match:
        raise ValueError('select a canonical github.com pull request or full commit URL')
    destination, kind, identity = match.groups()
    if kind == 'pull' and not re.fullmatch(r'[1-9][0-9]{0,9}', identity):
        raise ValueError('invalid GitHub PR number')
    if kind == 'commit' and not pr_watch.SHA.fullmatch(identity):
        raise ValueError('use the full 40-character GitHub commit SHA')

    data = reader.artifact(destination, kind, identity)
    if kind == 'pull':
        if (not isinstance(data, dict) or data.get('number') != int(identity)
                or str(data.get('html_url', '')).casefold() != artifact.casefold()
                or not all(isinstance(data.get(key), dict) for key in ('head', 'base'))):
            raise ValueError('GitHub reply does not identify the requested PR')
        head, base = data['head'].get('sha'), data['base'].get('sha')
        if not all(isinstance(value, str) and pr_watch.SHA.fullmatch(value) for value in (head, base)):
            raise ValueError('GitHub did not identify the PR base and head')
        if data.get('state') == 'open' and data.get('merged') is False:
            state = 'open'
        elif data.get('state') == 'closed' and data.get('merged') is True:
            landed = data.get('merge_commit_sha')
            if not isinstance(landed, str) or not pr_watch.SHA.fullmatch(landed):
                raise ValueError('GitHub did not identify the merged PR commit')
            if landed != head:
                base = cli.sha(repo, landed + '^1', timeout=15)
            head, state = landed, 'merged'
        else:
            raise ValueError('the pull request is neither open nor merged')
    else:
        if (not isinstance(data, dict) or data.get('status') not in ('ahead', 'identical')
                or not isinstance(data.get('merge_base_commit'), dict) or data['merge_base_commit'].get('sha') != identity):
            raise ValueError('the selected commit is not confirmed on the GitHub default branch')
        head, state = identity, 'merged'
        base = cli.sha(repo, identity + '^1', timeout=15)
    return {'url': artifact, 'repository': destination, 'kind': kind, 'state': state, 'base': base, 'head': head}


def verify(*, repo, artifact, report, image, manifest, helpers, journey, oracle, oracle_marker,
           oracle_failure_marker, out, timeout=900, memory_mb=3328, expected_base=None, expected_head=None,
           reader=None, compare=project_triage.run):
    if (type(timeout) is not int or not 1 <= timeout <= 3600
            or type(memory_mb) is not int or not 128 <= memory_mb <= 8192):
        raise ValueError('time and memory bounds are 1–3600 s and 128–8192 MiB')
    deadline = time.monotonic() + timeout
    repo = Path(repo).expanduser().resolve(strict=True)
    source = Path(report).expanduser().resolve(strict=True)
    with source.open('rb') as stream:
        request = stream.read(64_001)
    if not request.strip() or len(request) > 64_000 or b'\0' in request:
        raise ValueError('provide the original report as a nonempty UTF-8 text file of at most 64 KB')
    request.decode('utf-8')
    reader = reader or Reader()
    if isinstance(reader, Reader):
        reader.set_deadline(deadline)
    pair = _select(repo, artifact, reader)
    base, head = pair['base'], pair['head']
    if ((expected_base is not None and expected_base != base) or (expected_head is not None and expected_head != head)
            or base == head or cli.sha(repo, base, timeout=15) != base or cli.sha(repo, head, timeout=15) != head
            or cli.git(repo, 'rev-parse', '--verify', base + '^{tree}', timeout=15)
            == cli.git(repo, 'rev-parse', '--verify', head + '^{tree}', timeout=15)):
        raise ValueError('the exact selected GitHub pair must be present locally with a changed source tree')
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise ValueError('budget expired before measuring the existing fix')
    result = compare(repo=repo, base=base, revision=head, image=image, manifest=manifest, helpers=helpers,
                     journey=journey, oracle=oracle, oracle_marker=oracle_marker,
                     oracle_failure_marker=oracle_failure_marker, out=out, timeout=remaining,
                     memory_mb=memory_mb, existing_fix=(pair, request))
    try:
        current = _select(repo, artifact, reader)
    except (ValueError, delivery.DeliveryError, cli.WorkbenchError, OSError):
        current = None
    if current == pair:
        result['outcome'] = result['runtime_outcome']
    elif current is not None:
        result['outcome'] = 'stale'
    result['artifact_recheck'] = current
    cli.write(Path(out).expanduser().resolve() / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'artifact', 'report', 'image', 'manifest', 'helpers', 'journey', 'oracle',
                 'oracle-marker', 'oracle-failure-marker', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--memory-mb', type=int, default=3328)
    parser.add_argument('--expected-base')
    parser.add_argument('--expected-head')
    args = vars(parser.parse_args(argv))
    try:
        result = verify(**args)
    except (*project_verify.ERRORS, delivery.DeliveryError) as error:
        parser.error(str(error))
    print(json.dumps({'outcome': result['outcome'], 'report': str(Path(args['out']).resolve() / 'result.json')},
                     ensure_ascii=False))
    return {'confirmed': 0, 'insufficient-fix': 1}.get(result['outcome'], 2)


if __name__ == '__main__':
    raise SystemExit(main())
