"""Exercise historical OpenClaw delivery checks on controlled local receipts."""

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from workbench import cli, verify_openclaw


HEAD = 'a' * 40
BASE = 'b' * 40
IMAGE = 'sha256:' + 'f' * 64
RENAME = 'control-ui-session-rename'
ACTORS = ('builder', 'independent-verifier')


def _spec(journey):
    if hasattr(verify_openclaw, '_journey'):
        return verify_openclaw._journey(journey)
    helper, marker = verify_openclaw.JOURNEYS[journey]
    return helper, marker, ['node', '/workspace/' + helper]


def _browser(path, marker, restarted):
    path.write_bytes(b'Local screenshot attachment used for receipt validation')
    health = {'origin': 'http://127.0.0.1:18789', 'ready': True, 'live': True}
    exit_state = {'code': 0, 'signal': None}
    events = [{'phase': 'doctor', 'gatewayPid': 20, **health}]
    if restarted:
        events.append({'phase': 'restart', 'gatewayPid': 53, 'previousPid': 20,
                       'previousExit': exit_state, 'portClosed': True, **health})
    events.extend([
        {'phase': 'ui', 'route': '/sessions', 'servedCommit': HEAD, 'screenshot': True,
         'gatewayPid': 53 if restarted else 20,
         'handshake': {'role': 'operator', 'method': 'token', 'protocol': 4},
         'readback': {'method': 'sessions.list', 'count': 1},
         'mutation': {'method': 'sessions.patch', 'key': 'agent:main:iboard-browser-verification',
                      'sessionId': 'local-fixture', 'originalLabel': 'iBoard verification fixture',
                      'finalLabel': 'iBoard verification persisted', 'canceledWithoutWrite': True,
                      'accepted': True, 'freshContextReadback': True}},
        {'phase': 'cleanup', 'exit': exit_state, 'scratchRemoved': True},
    ])
    return {'screenshot_path': str(path.resolve()), 'screenshot_sha256': cli.digest(path),
            'stdout': '\n'.join([*(json.dumps(event) for event in events), marker, ''])}


def _gate(root, case, good):
    data = {'schema': 1, 'id': 'regression', 'repo': str(root), 'base_commit': BASE,
            'implementer': 'builder', 'criteria': [{'id': 'C1', 'text': 'Require original observations'}],
            'evidence': []}
    reports = {}
    browser_case = case == 'restart'
    journeys = ('gateway-health', RENAME) if browser_case else ('gateway-health',)
    for journey in journeys:
        helper, marker, command = _spec(journey)
        for actor in ACTORS:
            path = root / f'{journey}-{actor}.json'
            container_subject = journey if case == 'shared-container' and not good else path.name
            run = {'journey': journey, 'actor': actor, 'commit': HEAD, 'image': IMAGE, 'verified_image_id': IMAGE,
                   'helper_sha256': cli.digest(verify_openclaw.SKILL / helper), 'passed': True, 'exit_code': 0,
                   'cleanup_confirmed': True, 'timed_out': False, 'container_created': True,
                   'source_root': '/app', 'command': command, 'workspace_read_only': True,
                   'workspace': path.stem, 'stdout': marker + '\n',
                   'create_stdout': hashlib.sha256(container_subject.encode()).hexdigest()}
            if journey == RENAME:
                # The control uses the defined receipt format of this historical API.
                restart = good and hasattr(verify_openclaw, 'SESSION_RENAME')
                run.update(_browser(path.with_suffix('.png'), marker, restart))
            cli.write(path, run)
            ref = {'path': str(path), 'sha256': cli.digest(path)}
            reports[path.stem] = ref
            if good or case != 'registration':
                data['evidence'].append({'kind': 'runtime-log', 'commit': HEAD, 'actor': actor, **ref})
    criteria = cli.criteria_digest(data)
    review = root / 'review.json'
    cli.write(review, {'verdict': 'PASS', 'candidate_commit': HEAD, 'baseline_commit': BASE,
                       'criterion_digest': criteria, 'reports': reports})
    data['evidence'].append({'kind': 'independent-review', 'actor': ACTORS[1], 'commit': HEAD,
                             'criteria_digest': criteria, 'path': str(review), 'sha256': cli.digest(review)})
    change = 'ui/src/pages/sessions/page.ts' if browser_case else 'src/gateway/entry.ts'
    with patch.object(cli, 'git', return_value=change + '\0'):
        try:
            return bool(verify_openclaw.require(data, HEAD, 'fixture/myopenclaw'))
        except verify_openclaw.GatewayGateError:
            return False


def run(case):
    if case not in ('registration', 'restart', 'shared-container'):
        raise ValueError('unknown controlled receipt case')
    with TemporaryDirectory() as valid, TemporaryDirectory() as invalid:
        if not _gate(Path(valid), case, good=True):
            raise AssertionError('a valid evidence control was rejected')
        return _gate(Path(invalid), case, good=False)


if __name__ == '__main__':
    selected = sys.argv[1]
    missed = run(selected)
    print(f'GUARD_{"MISSED" if missed else "HOLDS"} {selected}')
    sys.exit(7 if missed else 0)
