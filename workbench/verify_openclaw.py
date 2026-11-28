"""Verify OpenClaw user journeys using iBoard's offline Docker runner."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from runner.sandbox import SandboxError, run_sandbox
from workbench import browser_recording, cli, workflow

JOURNEYS = {
    "gateway-health": ("gateway.mjs", "VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS"),
    "skills-check": ("skills-check.mjs", "VERIFY_OPENCLAW_SKILLS_CHECK_PASS"),
    "skills-inspect": ("skills-inspect.mjs", "VERIFY_OPENCLAW_SKILLS_INSPECT_PASS"),
    "workshop-roundtrip": ("workshop-roundtrip.mjs", "VERIFY_OPENCLAW_WORKSHOP_ROUNDTRIP_PASS"),
    "skills-library-help": ("skills-library-help.mjs", "VERIFY_OPENCLAW_SKILLS_LIBRARY_HELP_PASS"),
    "skills-library-lifecycle": ("skills-library-lifecycle.mjs", "VERIFY_OPENCLAW_SKILLS_LIBRARY_LIFECYCLE_PASS"),
    "skills-library-import": ("skills-library-lifecycle.mjs", "VERIFY_OPENCLAW_SKILLS_LIBRARY_IMPORT_PASS"),
    "skills-library-team": ("skills-library-team.mjs", "VERIFY_OPENCLAW_SKILLS_LIBRARY_TEAM_PASS"),
    "config-validate": ("config-validate.mjs", "VERIFY_OPENCLAW_CONFIG_VALIDATE_PASS"),
}
SESSION_RENAME = 'control-ui-session-rename'
SESSION_DELETE = 'control-ui-session-delete'
SESSION_FILTER = 'control-ui-session-status-filter'
SESSION_ARCHIVE = 'control-ui-session-archive-restore'
SESSION_ARCHIVE_TREE = 'control-ui-session-archive-subtree-undo'
SESSION_ARCHIVE_SIDEBAR = 'control-ui-session-sidebar-archive-automations'
SESSION_ARCHIVE_RUNNING = 'control-ui-session-archive-running'
SESSION_WRITES = (SESSION_RENAME, SESSION_DELETE, SESSION_ARCHIVE, SESSION_ARCHIVE_TREE, SESSION_ARCHIVE_SIDEBAR,
                  SESSION_ARCHIVE_RUNNING)
SESSION_JOURNEYS = (*SESSION_WRITES, SESSION_FILTER)
BROWSER_JOURNEYS = {
    'control-ui-connected': ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_CONNECTED_PASS'),
    SESSION_RENAME: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_RENAME_PASS'),
    SESSION_DELETE: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_DELETE_PASS'),
    SESSION_FILTER: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_STATUS_FILTER_PASS'),
    SESSION_ARCHIVE: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_ARCHIVE_RESTORE_PASS'),
    SESSION_ARCHIVE_TREE: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_ARCHIVE_SUBTREE_UNDO_PASS'),
    SESSION_ARCHIVE_SIDEBAR: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_SIDEBAR_ARCHIVE_AUTOMATIONS_PASS'),
    SESSION_ARCHIVE_RUNNING: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_ARCHIVE_RUNNING_PASS'),
}
SKILL = Path(__file__).resolve().parents[1] / "skills" / "verify-openclaw"


def _timeout(journey):
    if journey == 'skills-library-lifecycle':
        return 600
    if journey == 'skills-library-import':
        return 480
    if journey == 'skills-library-team':
        return 480
    if journey in (SESSION_ARCHIVE, SESSION_ARCHIVE_TREE, SESSION_ARCHIVE_SIDEBAR, SESSION_ARCHIVE_RUNNING):
        return 600
    return 480 if journey in SESSION_JOURNEYS else 320


def _journey(journey):
    helper, marker = (BROWSER_JOURNEYS if journey in BROWSER_JOURNEYS else JOURNEYS)[journey]
    command = ['node', f'/workspace/{helper}']
    if journey == 'skills-library-import':
        command.append('import')
    if journey in SESSION_JOURNEYS:
        command.append(journey.removeprefix('control-ui-'))
    return helper, marker, command


def _filter_evidence(ui, readback):
    evidence = ui['filter']
    fixtures = evidence['fixtures']
    active, archived = fixtures['active'], fixtures['archived']
    if not (readback['count'] == 1 and 'archived' not in readback
            and type(evidence['sessionMutations']) is int and evidence['sessionMutations'] == 0
            and active['key'] == 'agent:main:iboard-browser-verification'
            and archived['key'] == 'agent:main:iboard-browser-archived'
            and all(isinstance(item['sessionId'], str) and item['sessionId'].strip() for item in (active, archived))
            and active['sessionId'] != archived['sessionId']):
        return False
    expectations = (
        ('active', 'load', [active]),
        ('archived', 'filter', [archived]),
        ('all', 'filter', [active, archived]),
        ('active', 'filter-refresh', [active]),
    )
    steps = evidence['steps']
    if not isinstance(steps, list) or len(steps) != len(expectations):
        return False
    requests = set()
    for step, (state, trigger, expected) in zip(steps, expectations):
        if state == 'active':
            scope_matches = 'archived' not in step
        elif state == 'archived':
            scope_matches = step.get('archived') is True
        else:
            scope_matches = step.get('archived') == 'all'
        request = step['requestId']
        if not (scope_matches and (step.get('state'), step.get('trigger'), step.get('source')) ==
                (state, trigger, 'sessions-page') and isinstance(request, str) and request.strip()
                and request not in requests):
            return False
        if (not isinstance(step['gateway'], list) or not isinstance(step['visibleKeys'], list)
                or sorted(step['gateway'], key=lambda item: item['key']) != sorted(expected, key=lambda item: item['key'])
                or sorted(step['visibleKeys']) != sorted(item['key'] for item in expected)):
            return False
        requests.add(request)
    return True


def _archive_evidence(mutation, readback, pids):
    target = {key: mutation[key] for key in ('key', 'sessionId')}
    survivor = mutation['survivor']
    if (readback['count'] != 2 or 'archived' in readback or survivor['key'] != 'agent:main:iboard-browser-survivor'
            or not isinstance(survivor['sessionId'], str) or not survivor['sessionId'].strip()
            or survivor['sessionId'] == target['sessionId']):
        return False
    writes, steps = mutation['writes'], mutation['steps']
    if not isinstance(writes, list) or len(writes) != 2 or not isinstance(steps, list) or len(steps) != 3:
        return False
    requests = [row['requestId'] for row in [*writes, *steps]]
    if not all(isinstance(request, str) and request.strip() for request in requests) or len(set(requests)) != 5:
        return False
    for write, archived in zip(writes, (True, False)):
        if (write['method'] != 'sessions.patch' or write['accepted'] is not True
                or write['archived'] is not archived or write['responseArchived'] is not archived
                or write['key'] != target['key'] or write['responseKey'] != target['key']
                or write['expectedSessionId'] != target['sessionId'] or write['responseSessionId'] != target['sessionId']):
            return False
    expected = (('active', [target, survivor]), ('archived', [target]), ('active', [target, survivor]))
    return all(step['state'] == state and step['gatewayPid'] == pid and step['rows'] == rows
               for step, (state, rows), pid in zip(steps, expected, pids))


def _archive_running_evidence(mutation, readback, pids):
    target = {key: mutation[key] for key in ('key', 'sessionId')}
    survivor = mutation['survivor']
    run, write, steps = mutation['run'], mutation['write'], mutation['steps']
    if not (readback.get('archived') == 'all' and readback['count'] == 2
            and mutation['canceledWithoutWrite'] is True
            and survivor['key'] == 'agent:main:iboard-browser-survivor'
            and isinstance(survivor['sessionId'], str) and survivor['sessionId'].strip()
            and survivor['sessionId'] != target['sessionId']):
        return False
    run_id = run['id']
    if not (isinstance(run_id, str) and run_id.strip()
            and type(run['childPid']) is int and run['childPid'] > 1 and run['childPid'] not in pids
            and run['activeBefore'] is True and run['activeAfterCancel'] is True
            and run['archiveEvent'] == {'runId': run_id, 'sessionKey': target['key'],
                                        'state': 'aborted', 'stopReason': 'archive'}
            and isinstance(run['wait'], dict) and set(run['wait']) == {'runId', 'status', 'stopReason'}
            and run['wait']['runId'] == run_id and run['wait']['status'] == 'error'
            and run['wait']['stopReason'] in ('aborted', 'archive')
            and run['childExitSignal'] in ('SIGTERM', 'SIGINT')
            and run['childExited'] is True and run['noCompletion'] is True):
        return False
    if not (write['method'] == 'sessions.patch' and write['key'] == target['key']
            and write['expectedSessionId'] == target['sessionId'] and write['archived'] is True
            and write['accepted'] is True and write['responseKey'] == target['key']
            and write['responseSessionId'] == target['sessionId'] and write['responseArchived'] is True
            and isinstance(steps, list) and len(steps) == 3):
        return False
    requests = [write['requestId'], *(step['requestId'] for step in steps)]
    if not all(isinstance(req, str) and req.strip() for req in requests) or len(set(requests)) != 4:
        return False
    for index, (step, stage) in enumerate(zip(steps, ('initial', 'after-cancel', 'after-archive-restart'))):
        if (step['stage'] != stage or step['gatewayPid'] != pids[int(index == 2)]
                or not isinstance(step['rows'], list) or len(step['rows']) != 2):
            return False
        for row, fixture, archived in zip(step['rows'], (target, survivor), (index == 2, False)):
            if (row['key'] != fixture['key'] or row['sessionId'] != fixture['sessionId']
                    or row['archived'] is not archived):
                return False
    return True


def _library_evidence(report):
    """Bind each lifecycle observation to the same profile, sessions, revisions and bytes."""
    try:
        lines = report['stdout'].splitlines()
        if (report.get('stdout_truncated') or len(lines) != 5
                or lines[-1] != JOURNEYS['skills-library-lifecycle'][1]):
            return False
        doctor, restart, library, cleanup = map(json.loads, lines[:-1])
        initial_pid, restarted_pid = doctor['gatewayPid'], restart['gatewayPid']
        clean_exit = {'code': 0, 'signal': None}
        if not (doctor.get('phase') == 'doctor' and doctor.get('ready') is True and doctor.get('live') is True
                and type(initial_pid) is int and initial_pid > 1 and restart.get('phase') == 'restart'
                and restart.get('ready') is True and restart.get('live') is True and restart.get('portClosed') is True
                and type(restart.get('previousPid')) is int and restart.get('previousPid') == initial_pid
                and restart.get('previousExit') == clean_exit and type(restart['previousExit']['code']) is int
                and type(restarted_pid) is int and restarted_pid > 1 and restarted_pid != initial_pid
                and cleanup == {'phase': 'cleanup', 'exit': clean_exit, 'scratchRemoved': True}
                and cleanup.get('scratchRemoved') is True and type(cleanup['exit']['code']) is int
                and library.get('phase') == 'library'):
            return False
        profile, skill = library['profileId'], library['skillId']
        revisions, hashes, sessions, steps = library['revisions'], library['hashes'], library['sessions'], library['steps']
        if not (all(isinstance(value, str) and value.strip() for value in (profile, skill))
                and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                        for value in (revisions['original'], revisions['updated'],
                                      hashes['original'], hashes['updated'], hashes['support']))
                and revisions['original'] != revisions['updated'] and hashes['original'] != hashes['updated']
                and sessions['targetKey'] == 'agent:main:iboard-library-session'
                and sessions['controlKey'] == 'agent:main:iboard-library-control'
                and all(isinstance(sessions[key], str) and sessions[key].strip() for key in ('targetId', 'controlId'))
                and sessions['targetId'] != sessions['controlId']):
            return False
        a, b, support = revisions['original'], revisions['updated'], hashes['support']
        expected = {
            'initial': {'libraryCount': 0, 'targetSelections': 0, 'controlSelections': 0},
            'created': {'revision': a, 'ownerProfileId': profile, 'enabled': True, 'shared': False, 'supportSha': support},
            'attached': {'pin': a, 'activation': 'next-turn'},
            'updated': {'revision': b, 'pin': a, 'supportSha': support},
            'stale': {'exitCode': 1, 'preservedRevision': b, 'conflict': True},
            'refreshed': {'pin': b, 'oldPinRejected': True, 'controlUnselected': True, 'controlReadRejected': True},
            'toggled': {'disabled': False, 'enabled': True, 'revision': b},
            'rollback': {'revision': a, 'pin': b, 'supportSha': support},
            'restarted': {'profileId': profile, 'revision': a, 'pin': b, 'targetId': sessions['targetId'],
                          'controlId': sessions['controlId'], 'pinContentSha': hashes['updated'], 'supportSha': support},
            'removed': {'listedCount': 0, 'directReadMarkedRemoved': True, 'directRevision': a,
                        'directContentSha': hashes['original'], 'editRejected': True, 'reattachRejected': True,
                        'pin': b, 'pinContentSha': hashes['updated']},
            'detached': {'activation': 'next-turn', 'remaining': 0, 'pinReadRejected': True},
        }
        def matches(actual, wanted):
            if isinstance(wanted, dict):
                return (isinstance(actual, dict) and set(actual) == set(wanted)
                        and all(matches(actual[key], value) for key, value in wanted.items()))
            return type(actual) is type(wanted) and actual == wanted
        if not matches(steps, expected):
            return False
        turns = library['turns']
        if not isinstance(turns, list) or len(turns) != 6:
            return False
        expectations = (
            ('attached', 'target', a, hashes['original']),
            ('after-update', 'target', a, hashes['original']),
            ('refreshed', 'target', b, hashes['updated']),
            ('control', 'control', None, None),
            ('restarted', 'target', b, hashes['updated']),
            ('detached', 'target', None, None),
        )
        ids = set()
        for row, (stage, identity, revision, content) in zip(turns, expectations):
            if not (isinstance(row, dict) and set(row) == {
                'stage', 'sessionKey', 'sessionId', 'runId', 'status', 'cliPid', 'skillPath', 'contentSha', 'supportSha'
            }):
                return False
            run_id = row['runId']
            if not (isinstance(run_id, str)
                    and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', run_id)
                    and run_id not in ids and type(row['cliPid']) is int and row['cliPid'] > 1
                    and row['cliPid'] not in (initial_pid, restarted_pid)
                    and row['stage'] == stage and row['status'] == 'ok'
                    and row['sessionKey'] == sessions[identity + 'Key']
                    and row['sessionId'] == sessions[identity + 'Id']
                    and row['skillPath'] == (f'skill-library/{skill}/revisions/{revision}/SKILL.md' if revision else None)
                    and row['contentSha'] == content and row['supportSha'] == (support if revision else None)):
                return False
            ids.add(run_id)
        return True
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _library_import_evidence(report):
    try:
        lines = report['stdout'].splitlines()
        if (report.get('stdout_truncated') or len(lines) != 4
                or lines[-1] != JOURNEYS['skills-library-import'][1]):
            return False
        doctor, imports, cleanup = map(json.loads, lines[:-1])
        if not (doctor.get('phase') == 'doctor' and doctor.get('ready') is True and doctor.get('live') is True
                and type(doctor.get('gatewayPid')) is int and doctor['gatewayPid'] > 1
                and imports.get('phase') == 'imports'
                and cleanup == {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True}
                and cleanup['scratchRemoved'] is True and type(cleanup['exit']['code']) is int):
            return False
        profile = imports['profileId']
        if not (isinstance(profile, str) and profile.strip()
                and all(isinstance(imports[key], str) and imports[key].strip()
                        for key in ('existingSessionId', 'newSessionId'))
                and imports['existingSessionId'] != imports['newSessionId']
                and imports['existingSelections'] == []
                and imports['registry'] == ['detail', 'trust', 'download', 'verify', 'blocked-detail', 'blocked-trust']):
            return False
        rejected = imports['rejected']
        if not (isinstance(rejected, dict) and set(rejected) == {
            'malformedZipExit', 'duplicateExit', 'blockedRegistryExit', 'libraryCount'
        } and all(type(rejected[key]) is int and rejected[key] == value for key, value in (
            ('malformedZipExit', 1), ('duplicateExit', 1), ('blockedRegistryExit', 1), ('libraryCount', 3)
        ))):
            return False
        rows = imports['rows']
        if not isinstance(rows, list) or len(rows) != 3:
            return False
        ids, selections = set(), []
        support_sha = hashlib.sha256(b'The support file should survive a single-file edit.\n').hexdigest()
        for row, source in zip(rows, ('directory', 'zip', 'clawhub')):
            slug = f'iboard-import-{source}'
            content = f'---\nname: {slug}\ndescription: Offline import fixture\n---\n\nRead the {source} procedure.\n'
            if not (isinstance(row, dict) and set(row) == {
                'source', 'slug', 'skillId', 'revision', 'ownerProfileId', 'enabled', 'shared', 'contentSha', 'supportSha'
            } and row['source'] == source and row['slug'] == slug
                    and isinstance(row['skillId'], str) and row['skillId'].strip() and row['skillId'] not in ids
                    and row['ownerProfileId'] == profile and row['enabled'] is True and row['shared'] is False
                    and isinstance(row['revision'], str) and re.fullmatch(r'[0-9a-f]{64}', row['revision'])
                    and row['contentSha'] == hashlib.sha256(content.encode()).hexdigest()
                    and row['supportSha'] == support_sha):
                return False
            ids.add(row['skillId'])
            selections.append({'skillId': row['skillId'], 'revision': row['revision']})
        actual = imports['newSelections']
        return (isinstance(actual, list) and len(actual) == 3
                and all(isinstance(row, dict) and set(row) == {'skillId', 'revision'} for row in actual)
                and sorted(actual, key=lambda row: row['skillId']) == sorted(selections, key=lambda row: row['skillId']))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _library_team_evidence(report):
    try:
        lines = report['stdout'].splitlines()
        if (report.get('stdout_truncated') or len(lines) != 4
                or lines[-1] != JOURNEYS['skills-library-team'][1]):
            return False
        doctor, team, cleanup = map(json.loads, lines[:-1])
        if not (doctor.get('phase') == 'doctor' and doctor.get('ready') is True and doctor.get('live') is True
                and type(doctor.get('gatewayPid')) is int and doctor['gatewayPid'] > 1
                and team.get('phase') == 'team'
                and cleanup == {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True}
                and cleanup['scratchRemoved'] is True and type(cleanup['exit']['code']) is int):
            return False
        profiles, skill, sessions = team['profiles'], team['skill'], team['sessions']
        if not (isinstance(profiles, dict) and set(profiles) == {'owner', 'member', 'admin'}
                and all(isinstance(value, str) and value.strip() for value in profiles.values())
                and len(set(profiles.values())) == 3 and isinstance(skill, dict)
                and set(skill) == {'skillId', 'revision', 'contentSha', 'supportSha'}
                and isinstance(skill['skillId'], str) and skill['skillId'].strip()
                and isinstance(skill['revision'], str) and re.fullmatch(r'[0-9a-f]{64}', skill['revision'])
                and skill['contentSha'] == hashlib.sha256(b'---\nname: iboard-team-fixture\ndescription: Offline team library fixture\n---\n\nCheck the shared checklist.\n').hexdigest()
                and skill['supportSha'] == hashlib.sha256(b'Team checklist attachment.\n').hexdigest()
                and type(team['unauthenticatedStatus']) is int and team['unauthenticatedStatus'] == 401
                and isinstance(sessions, dict) and set(sessions) == {'shared', 'unshared', 'team'}):
            return False
        pin = {'skillId': skill['skillId'], 'revision': skill['revision']}
        session_ids = set()
        for name, label, selections in (('shared', 'shared', [pin]), ('unshared', 'unshared', []),
                                         ('team', 'transferred', [pin])):
            session = sessions[name]
            if not (isinstance(session, dict) and set(session) == {'key', 'id', 'selections'}
                    and session['key'] == f'agent:main:iboard-team-{label}'
                    and isinstance(session['id'], str) and session['id'].strip()
                    and session['id'] not in session_ids and session['selections'] == selections):
                return False
            session_ids.add(session['id'])
        expected = {
            'private': {'visibleCount': 0, 'directReadCode': 'SKILL_LIBRARY_NOT_FOUND'},
            'shared': {'ownerId': profiles['owner'], 'memberVisibleId': skill['skillId'], 'memberCanEdit': False,
                       'memberMutationCode': 'SKILL_LIBRARY_FORBIDDEN', 'ownerTransferCode': 'SKILL_LIBRARY_FORBIDDEN'},
            'unshared': {'ownerId': profiles['owner'], 'visibleCount': 0, 'directReadCode': 'SKILL_LIBRARY_NOT_FOUND',
                         'contentSha': skill['contentSha'], 'supportSha': skill['supportSha']},
            'transferred': {'ownerProfileId': None, 'ownerLabel': 'Team', 'authorProfileId': profiles['owner'],
                            'ownerMineCount': 0, 'adminCanEdit': True, 'formerOwnerMutationCode': 'SKILL_LIBRARY_FORBIDDEN',
                            'adminUnshareCode': 'SKILL_LIBRARY_FORBIDDEN', 'contentSha': skill['contentSha'],
                            'supportSha': skill['supportSha']},
        }
        def matches(actual, wanted):
            if isinstance(wanted, dict):
                return (isinstance(actual, dict) and set(actual) == set(wanted)
                        and all(matches(actual[key], value) for key, value in wanted.items()))
            return type(actual) is type(wanted) and actual == wanted
        return matches(team['steps'], expected)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _archive_batch_evidence(mutation, readback, pids, *, sidebar=False):
    first = {key: mutation[key] for key in ('key', 'sessionId')}
    second, survivor = mutation['second' if sidebar else 'nested'], mutation['survivor']
    fixtures = (first, second, survivor)
    second_key = 'agent:main:iboard-browser-' + ('second' if sidebar else 'nested')
    if (readback['count'] != 3 or readback['archived'] != 'all' or mutation['canceledWithoutWrite'] is not True
            or second['key'] != second_key or survivor['key'] != 'agent:main:iboard-browser-survivor'
            or not all(isinstance(row['sessionId'], str) and row['sessionId'].strip() for row in fixtures)
            or len({row['sessionId'] for row in fixtures}) != 3):
        return False
    writes, steps = mutation['writes'], mutation['steps']
    if not isinstance(writes, list) or len(writes) != 2 or not isinstance(steps, list) or len(steps) != 4:
        return False
    requests = [row['requestId'] for row in [*writes, *steps]]
    if not all(isinstance(request, str) and request.strip() for request in requests) or len(set(requests)) != 6:
        return False
    targets = sorted((dict(key=row['key'], expectedSessionId=row['sessionId'], agentId='main')
                      for row in (first, second)), key=lambda row: row['key'])
    for write, archived in zip(writes, (True, False)):
        if write['method'] != 'sessions.patchMany' or write['accepted'] is not True or write['archived'] is not archived:
            return False
        if not isinstance(write['targets'], list) or sorted(write['targets'], key=lambda row: row['key']) != targets:
            return False
        outcomes = write['outcomes']
        if (not isinstance(outcomes, list) or len(outcomes) != 2
                or sorted(row['key'] for row in outcomes) != [row['key'] for row in targets]
                or not all(row['ok'] is True and row['agentId'] == 'main' for row in outcomes)):
            return False
    stages = ('initial', 'after-cancel', 'after-archive', 'after-undo-restart')
    for index, (step, stage) in enumerate(zip(steps, stages)):
        if (step['stage'] != stage or step['gatewayPid'] != pids[int(index == 3)]
                or not isinstance(step['rows'], list) or len(step['rows']) != len(fixtures)):
            return False
        for row, fixture in zip(step['rows'], fixtures):
            if (row['key'] != fixture['key'] or row['sessionId'] != fixture['sessionId']
                    or row['archived'] is not (index == 2 and fixture != survivor)):
                return False
    return True


def _archive_sidebar_evidence(mutation, pids):
    selected = sorted((mutation['key'], mutation['second']['key']))
    if mutation['selections'] != [selected, selected] or mutation['sidebarVisibilityChecked'] is not True:
        return False
    reads = mutation['automationReads']
    if not isinstance(reads, list) or len(reads) != len(mutation['steps']):
        return False
    if any(not isinstance(read['jobs'], list) or len(read['jobs']) != 2 for read in reads):
        return False
    selected_id = mutation['warningJobId']
    survivor_id = reads[0]['jobs'][1]['id']
    if (not all(isinstance(job_id, str) and job_id.strip() for job_id in (selected_id, survivor_id))
            or selected_id == survivor_id):
        return False
    for index, (read, step) in enumerate(zip(reads, mutation['steps'])):
        if read['gatewayPid'] != pids[int(index == 3)] or read['stage'] != step['stage']:
            return False
        for job, job_id, key, enabled, name in zip(read['jobs'], (selected_id, survivor_id),
                (mutation['key'], mutation['survivor']['key']), (index < 2, True),
                ('iBoard verification fixture automation', 'iBoard unselected fixture automation')):
            if (job['id'] != job_id or job['name'] != name
                    or job['sessionKey'] != key or job['sessionTarget'] != 'main'
                    or job['enabled'] is not enabled or job['neverRun'] is not True):
                return False
    return True


def _browser_evidence(report, screenshot):
    """Check the artifact attachment and the observed session behavior."""
    try:
        if (screenshot.is_symlink() or str(screenshot.resolve(strict=True)) != report.get('screenshot_path')
                or cli.digest(screenshot) != report.get('screenshot_sha256')):
            return False
        marker = _journey(report['journey'])[1]
        restarts = 2 if report['journey'] == SESSION_ARCHIVE else int(report['journey'] in SESSION_WRITES)
        lines = report['stdout'].splitlines()
        if report.get('stdout_truncated') or len(lines) != 4 + restarts or lines[-1] != marker:
            return False
        doctor, *activity, ui, cleanup = map(json.loads, lines[:-1])
        initial_pid = doctor['gatewayPid']
        clean_exit = {'code': 0, 'signal': None}
        handshake, readback = ui['handshake'], ui['readback']
        if not (doctor.get('phase') == 'doctor' and doctor.get('ready') is True and doctor.get('live') is True
                and doctor.get('origin') == 'http://127.0.0.1:18789' and type(initial_pid) is int and initial_pid > 1
                and ui.get('phase') == 'ui' and ui.get('route') == '/sessions'
                and ui.get('servedCommit') == report['commit'] and ui.get('screenshot') is True
                and handshake.get('role') == 'operator' and handshake.get('method') == 'token'
                and type(handshake.get('protocol')) is int and handshake['protocol'] > 0
                and readback.get('method') == 'sessions.list' and type(readback.get('count')) is int
                and cleanup == {'phase': 'cleanup', 'exit': clean_exit, 'scratchRemoved': True}):
            return False
        if not restarts:
            if 'mutation' in ui or ui.get('gatewayPid') != initial_pid:
                return False
            if report['journey'] == SESSION_FILTER:
                return _filter_evidence(ui, readback)
            return readback['count'] == 0
        pids = [initial_pid]
        for restart in activity:
            restarted_pid = restart['gatewayPid']
            if not (restart.get('phase') == 'restart' and restart.get('origin') == doctor['origin']
                    and restart.get('ready') is True and restart.get('live') is True and restart.get('portClosed') is True
                    and restart.get('previousPid') == pids[-1] and restart.get('previousExit') == clean_exit
                    and type(restarted_pid) is int and restarted_pid > 1 and restarted_pid not in pids):
                return False
            pids.append(restarted_pid)
        if ui.get('gatewayPid') != pids[-1]:
            return False
        mutation = ui['mutation']
        if not (mutation.get('key') == 'agent:main:iboard-browser-verification'
                and isinstance(mutation.get('sessionId'), str) and mutation['sessionId'].strip()):
            return False
        if report['journey'] == SESSION_ARCHIVE:
            return _archive_evidence(mutation, readback, pids)
        if report['journey'] == SESSION_ARCHIVE_RUNNING:
            return _archive_running_evidence(mutation, readback, pids)
        if report['journey'] == SESSION_ARCHIVE_TREE:
            return _archive_batch_evidence(mutation, readback, pids)
        if report['journey'] == SESSION_ARCHIVE_SIDEBAR:
            return (_archive_batch_evidence(mutation, readback, pids, sidebar=True)
                    and _archive_sidebar_evidence(mutation, pids))
        if report['journey'] == SESSION_DELETE:
            survivor = mutation['survivor']
            return (readback['count'] == 1 and readback.get('archived') == 'all'
                    and mutation.get('method') == 'sessions.delete' and mutation.get('responseKey') == mutation['key']
                    and (mutation.get('expectedSessionId'), mutation.get('requestCount'),
                         mutation.get('afterCancelCount')) == (mutation['sessionId'], 1, 2)
                    and type(mutation['requestCount']) is int and type(mutation['afterCancelCount']) is int
                    and mutation.get('cancelBrowserReplaced') is True and mutation.get('cancelGatewayPid') == initial_pid
                    and all(mutation.get(key) is True for key in (
                        'canceledWithoutWrite', 'deleted', 'freshContextReadback', 'targetAbsent'))
                    and survivor.get('key') == 'agent:main:iboard-browser-survivor'
                    and isinstance(survivor.get('sessionId'), str) and survivor['sessionId'].strip() != ''
                    and survivor['sessionId'] != mutation['sessionId'])
        return (readback['count'] > 0 and mutation.get('method') == 'sessions.patch'
                and mutation.get('originalLabel') == 'iBoard verification fixture'
                and mutation.get('finalLabel') == 'iBoard verification persisted'
                and mutation.get('menuEntries') == ['context-menu', 'row-button']
                and mutation.get('expectedSessionId') == mutation['sessionId']
                and type(mutation.get('requestCount')) is int and mutation['requestCount'] == 1
                and all(mutation.get(key) is True for key in ('canceledWithoutWrite', 'accepted', 'freshContextReadback')))
    except (OSError, KeyError, ValueError, TypeError, AttributeError, cli.WorkbenchError):
        return False


def verify(repo, image, journey, *, timeout, memory_mb, screenshot=None, recording=None):
    browser = journey in BROWSER_JOURNEYS
    if browser != (screenshot is not None):
        raise ValueError('a Control UI browser journey requires a screenshot path')
    if recording is not None and not browser:
        raise ValueError('a recording is only available for the Control UI browser journeys')
    helper, marker, command = _journey(journey)
    deadline = time.monotonic() + timeout
    report = {"journey": journey, "requested_image": image}
    try:
        repo = repo.expanduser().resolve(strict=True)
        head = cli.sha(repo, "HEAD")
        report["commit"] = head
        pinned = workflow._verified_image(repo, image, min(180, timeout), commit=head)
        report["verified_image_id"] = pinned
        # Only the selected harness is mounted. Never mount the target checkout,
        # the iBoard repository (which may contain credentials), or a home directory.
        with tempfile.TemporaryDirectory(prefix="iboard-openclaw-") as scratch:
            copy = Path(scratch) / helper
            shutil.copyfile(SKILL / helper, copy)
            if browser:
                (Path(scratch) / 'expected-commit.txt').write_text(head + '\n', encoding='utf-8')
            report["helper_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
            report.update(run_sandbox(
                scratch, pinned, command, deadline - time.monotonic(),
                memory_mb=memory_mb, source_root="/app", workspace_read_only=True,
                **({'screenshot_output': screenshot, 'pids_limit': 384} if browser else {}),
                **({'recording_output': recording} if recording is not None else {}),
            ))
    except (OSError, ValueError, SandboxError, workflow.WorkflowError, cli.WorkbenchError) as error:
        report["error"] = str(error)
    report["passed"] = (
        not report.get("error") and not report.get("timed_out") and report.get("exit_code") == 0
        and report.get("cleanup_confirmed") is True
        and marker in report.get("stdout", "").splitlines()
        and (not browser or _browser_evidence(report, Path(screenshot)))
        and (journey != 'skills-library-lifecycle' or _library_evidence(report))
        and (journey != 'skills-library-import' or _library_import_evidence(report))
        and (journey != 'skills-library-team' or _library_team_evidence(report))
        and (recording is None or browser_recording.receipt(
            report, recording, report.get('commit'), expected_journey=journey.removeprefix('control-ui-')))
    )
    return report


def audit(repo, image, *, timeout, memory_mb):
    """Check the documented feature map and exercise every registered journey."""
    report = {'journey': 'audit', 'requested_image': image, 'runs': {}, 'passed': False}
    feature_dir = SKILL / 'features'
    try:
        links = re.findall(r'^\|\s*\[[^\]\n]+\]\(([a-z0-9-]+)\.md\)\s*\|',
                           (feature_dir / 'README.md').read_text(encoding='utf-8'), re.MULTILINE)
        documented = {p.stem for p in feature_dir.glob('*.md') if p.name != 'README.md'}
        expected = set(JOURNEYS)
        if len(links) != len(set(links)) or set(links) != expected or documented != expected:
            report.update(status='map-drift', listed=links, documented=sorted(documented),
                          registered=sorted(expected))
            return report
    except (OSError, ValueError) as error:
        report.update(status='map-unavailable', error=str(error))
        return report

    deadline = time.monotonic() + timeout
    commit, pinned = None, None
    for journey in links:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            report['status'] = 'timeout'
            return report
        run = verify(repo, pinned or image, journey, timeout=remaining, memory_mb=memory_mb)
        report['runs'][journey] = run
        if not run['passed']:
            report['status'] = 'check-failed'
            return report
        if commit is None:
            commit, pinned = run['commit'], run['verified_image_id']
        if run['commit'] != commit or run['verified_image_id'] != pinned:
            report['status'] = 'target-changed'
            return report
    report.update(status='map-verified', commit=commit, verified_image_id=pinned, passed=True)
    return report


class GatewayGateError(cli.WorkbenchError):
    pass


def applies(data, destination=None):
    if destination and destination.lower().endswith(('/openclaw', '/myopenclaw')):
        return True
    try:
        return json.loads(cli.git(data['repo'], 'show', f"{data['base_commit']}:package.json")).get('name') == 'openclaw'
    except (cli.WorkbenchError, ValueError, AttributeError):
        return False


def required_journeys(data, head):
    """Select the smoke check and mapped regressions from the committed diff."""
    try:
        paths = cli.git(data['repo'], 'diff', '--name-only', '--no-renames', '-z',
                        data['base_commit'], head, '--').split('\0')
    except (KeyError, OSError, cli.WorkbenchError) as error:
        raise GatewayGateError('Cannot determine required OpenClaw journeys') from error
    selected = ['gateway-health']
    if any(path in ('src/shared/frontmatter.ts', 'src/agents/agent-scope-config.ts')
           or path.startswith(('src/skills/', 'src/cli/skills-cli.', 'src/cli/skills-cli/'))
           for path in paths):
        selected.extend(('skills-check', 'skills-inspect'))
    if any(path == 'src/cli/skills-cli.ts' or path.startswith('src/skills/workshop/') for path in paths):
        selected.append('workshop-roundtrip')
    if any(path in ('src/cli/skills-library-cli.ts', 'src/cli/skills-cli.ts',
                    'src/cli/program/parent-default-help.ts') for path in paths):
        selected.append('skills-library-help')
    if any(path.startswith('src/skills/library/') or path in (
        'src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts', 'src/gateway/server-methods/skills-library.ts',
        'src/gateway/gateway-owner-profile.ts', 'packages/gateway-protocol/src/schema/skill-library.ts',
        'src/agents/cli-runner/skills-prompt.ts', 'src/agents/cli-runner/prepare.ts',
        'src/agents/cli-runner/execute.ts', 'src/agents/cli-runner/execute-process.ts',
        'src/agents/cli-runner/helpers.ts', 'src/agents/embedded-agent-runner/skills-prompt.ts',
        'src/skills/runtime/session-snapshot.ts', 'src/skills/runtime/embedded-run-entries.ts',
        'src/skills/loading/workspace-skill-prompt.ts',
    ) for path in paths):
        selected.append('skills-library-lifecycle')
    if any(path in (
        'src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts',
        'src/skills/library/import.ts', 'src/skills/library/import.kernel.ts', 'src/skills/library/bundle.ts',
        'src/skills/library/receipt.ts', 'src/gateway/server-methods/skills-library.ts',
        'src/gateway/server-methods/sessions-create.ts', 'src/gateway/skill-library-session.ts',
        'packages/gateway-protocol/src/schema/skill-library.ts',
        'src/infra/clawhub-client.ts', 'src/infra/clawhub-artifacts.ts', 'src/infra/clawhub-integrity.ts',
        'src/infra/clawhub-install-trust.ts', 'src/infra/clawhub-skill-security.ts', 'src/infra/clawhub-skills.ts',
        'src/skills/lifecycle/clawhub.ts', 'src/skills/lifecycle/clawhub-store.ts',
        'src/skills/lifecycle/clawhub-install-core.ts', 'src/skills/lifecycle/clawhub-install-rollback.ts',
        'src/skills/lifecycle/clawhub-request-error.ts', 'src/skills/lifecycle/install-paths.ts',
        'src/skills/security/clawhub-verdicts.ts', 'src/infra/install-flow.ts',
    ) for path in paths):
        selected.append('skills-library-import')
    if any(path.startswith('src/skills/library/') or path in (
        'src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts',
        'src/gateway/server-methods/skills-library.ts', 'packages/gateway-protocol/src/schema/skill-library.ts',
        'src/gateway/auth.ts', 'src/gateway/role-policy.ts', 'src/gateway/role-policy.types.ts',
        'src/gateway/operator-role-policy.ts', 'src/gateway/server-methods/users.ts',
        'src/gateway/server-methods/sessions-create.ts', 'src/gateway/skill-library-session.ts',
        'src/gateway/server/ws-connection/connect-auth.ts',
        'src/gateway/server/ws-connection/connect-admission.ts', 'src/gateway/server/ws-connection/connect-session.ts',
        'src/gateway/server/ws-connection/authenticated-request-dispatch.ts',
    ) for path in paths):
        selected.append('skills-library-team')
    if any(path.startswith(('src/config/', 'src/cli/config-cli.', 'src/cli/config-cli-')) for path in paths):
        selected.append('config-validate')
    sessions_ui = any(path == 'ui/src/styles/sessions.css'
                      or path.startswith(('ui/src/pages/sessions/', 'ui/src/lib/sessions/')) for path in paths)
    if sessions_ui or 'ui/src/lib/session-rename.ts' in paths:
        selected.append(SESSION_RENAME)
    if sessions_ui or 'src/gateway/server-methods/sessions-delete.ts' in paths:
        selected.append(SESSION_DELETE)
    session_roster = sessions_ui or any(path in (
        'packages/gateway-protocol/src/session-row-fields.ts',
        'src/gateway/session-list-view-cache.ts',
        'src/gateway/session-row-presentation.ts',
        'src/gateway/server-methods/sessions-read.ts',
        'src/gateway/server-methods/sessions-patch-archive.ts',
    ) for path in paths)
    if session_roster or 'ui/src/components/solid/settings-ui.tsx' in paths:
        selected.append(SESSION_FILTER)
    archive_paths = session_roster or any(path.startswith(('ui/src/components/session-organizer-archive',
                                          'ui/src/components/session-menu-',
                                          'src/gateway/server-methods/sessions-patch-archive'))
                          or path in ('src/gateway/server-methods/sessions-mutations.ts',
                                      'src/gateway/server-methods/sessions-patch-engine.ts')
                          for path in paths)
    if archive_paths:
        selected.append(SESSION_ARCHIVE)
    shared_batch = archive_paths or any(path in (
        'ui/src/components/session-organizer-batch-mutations.ts',
        'ui/src/components/confirm-dialog-view.tsx', 'ui/src/lib/toast.ts', 'ui/src/lib/toast-view.tsx',
    ) for path in paths)
    if shared_batch or 'packages/gateway-protocol/src/schema/sessions-create.ts' in paths:
        selected.append(SESSION_ARCHIVE_TREE)
    if shared_batch or any(path.startswith((
        'ui/src/components/app-sidebar-session-', 'ui/src/components/sidebar-menus',
        'ui/src/components/session-organizer-menu.', 'ui/src/components/session-organizer-operations.',
    )) or path in (
        'ui/src/components/session-automation-confirm.ts', 'src/cron/job-session-bindings.ts',
        'src/gateway/session-automation-index.ts', 'src/gateway/server-methods/cron-list.ts',
        'src/gateway/server-methods/sessions-patch-effects.ts',
    ) for path in paths):
        selected.append(SESSION_ARCHIVE_SIDEBAR)
    if archive_paths or any(path.startswith(('ui/src/components/session-organizer-operations.',
                                           'ui/src/components/session-organizer-patch.')) or path in (
        'ui/src/components/confirm-dialog-view.tsx',
        'ui/src/components/session-automation-confirm.ts',
        'src/gateway/server-methods/sessions-patch-effects.ts',
        'src/gateway/server-methods/sessions-lifecycle-drain.ts',
        'src/gateway/server-methods/chat-abort-runtime.ts',
        'src/gateway/server-methods/sessions-subscriptions.ts', 'src/gateway/chat-abort.ts',
        'src/gateway/chat-abort-ops.ts', 'src/gateway/chat-abort-lifecycle-internal.ts',
        'src/sessions/session-controller.lifecycle.ts', 'src/sessions/session-controller.settlement.ts',
        'src/agents/run-termination.ts', 'src/agents/cli-runner/execute-process.ts',
    ) for path in paths):
        selected.append(SESSION_ARCHIVE_RUNNING)
    return selected


def needs_browser(data, head):
    return any(journey in BROWSER_JOURNEYS for journey in required_journeys(data, head))


def build_browser_image(repo, base, image, head, timeout):
    from workbench import visual_parity
    # Reuse the existing offline OpenClaw UI build with frozen dependencies.
    plan = {'rebuild': {'profile': visual_parity.OPENCLAW_PROFILE,
                        'sha256': cli.digest(visual_parity.OPENCLAW_RECIPE)}}
    return visual_parity.rebuild_image(repo, base, image, head, plan, timeout)['image']


def collect(task, data, head, image, actors, *, remaining, memory_mb, record_browser=False):
    results = []
    for journey in required_journeys(data, head):
        for actor in actors:
            path = task.parent / f'{journey}-{uuid.uuid4().hex}.json'
            options = {'screenshot': path.with_suffix('.png')} if journey in BROWSER_JOURNEYS else {}
            if options and record_browser:
                options['recording'] = path.with_suffix('.jsonl')
            report = verify(Path(data['repo']), image, journey,
                            timeout=min(_timeout(journey), remaining()), memory_mb=memory_mb, **options)
            report.update(actor=actor, source_mode='image', source_verified_commit=report.get('commit'))
            cli.write(path, report)
            if not report['passed'] or report['commit'] != head:
                if workflow.docker_oom(report):
                    raise workflow.WorkflowError(f'OpenClaw {journey} exceeded available Docker memory; raw report saved at {path}')
                raise workflow.WorkflowError(f'OpenClaw {journey} verification failed; raw report saved at {path}')
            results.append({'report': str(path), 'actor': actor, 'journey': journey})
    return results


def require(data, head, destination, *, review_item=None):
    """Require two distinct executions per journey in one accepted review."""
    if not applies(data, destination):
        return
    required = required_journeys(data, head)
    helpers = {journey: cli.digest(SKILL / _journey(journey)[0]) for journey in required}
    for item in data['evidence'] if review_item is None else (review_item,):
        if (item.get('superseded') or item.get('kind') != 'independent-review'
                or item.get('commit') != head or item.get('actor') != 'independent-verifier'
                or item.get('actor') == data['implementer']
                or item.get('criteria_digest') != cli.criteria_digest(data)):
            continue
        try:
            if cli.digest(item['path']) != item.get('sha256'):
                continue
            review = json.loads(Path(item['path']).read_text())
            if (review.get('verdict') != 'PASS' or review.get('candidate_commit') != head
                    or review.get('baseline_commit') != data['base_commit']
                    or review.get('criterion_digest') != cli.criteria_digest(data)):
                continue
            reports = {journey: [] for journey in required}
            for entry in review['reports'].values():
                if 'path' not in entry or cli.digest(entry['path']) != entry['sha256']:
                    continue
                run = json.loads(Path(entry['path']).read_text())
                if not any(e.get('kind') == 'runtime-log' and e.get('commit') == head
                           and e.get('actor') == run.get('actor') and not e.get('superseded')
                           and e.get('path') == entry['path'] and e.get('sha256') == entry['sha256']
                           for e in data['evidence']):
                    continue
                journey = run.get('journey')
                if journey not in reports:
                    continue
                _, marker, command = _journey(journey)
                container = run.get('create_stdout')
                if (not isinstance(container, str) or run.get('create_stdout_truncated') is True
                        or not re.fullmatch(r'[0-9a-f]{64}', container.strip())):
                    continue
                if journey in BROWSER_JOURNEYS:
                    if not _browser_evidence(run, Path(entry['path']).with_suffix('.png')):
                        continue
                    if ('recording_path' in run or 'recording_sha256' in run) and not browser_recording.receipt(
                            run, Path(entry['path']).with_suffix('.jsonl'), head,
                            expected_journey=journey.removeprefix('control-ui-')):
                        continue
                if journey == 'skills-library-lifecycle' and not _library_evidence(run):
                    continue
                if journey == 'skills-library-import' and not _library_import_evidence(run):
                    continue
                if journey == 'skills-library-team' and not _library_team_evidence(run):
                    continue
                if (run.get('commit') == head
                        and run.get('helper_sha256') == helpers[journey] and run.get('passed') is True
                        and run.get('exit_code') == 0 and run.get('cleanup_confirmed') is True
                        and run.get('container_created') is True and run.get('source_root') == '/app'
                        and run.get('command') == command
                        and run.get('workspace_read_only') is True and run.get('timed_out') is False
                        and not run.get('error') and marker in run.get('stdout', '').splitlines()
                        and run.get('verified_image_id') == run.get('image') and
                        re.fullmatch(r'sha256:[0-9a-f]{64}', str(run.get('image'))) and run.get('workspace')):
                    reports[journey].append({**run, 'sha256': entry['sha256']})
            # Use the same immutable image across every required journey.
            pairs_by_image = {
                journey: {a['image']: (a, b) for a in runs for b in runs
                 if a.get('actor') == data['implementer'] and b.get('actor') == item['actor']
                 and a['image'] == b['image'] and a['workspace'] != b['workspace']
                 and a['create_stdout'].strip() != b['create_stdout'].strip()}
                for journey, runs in reports.items()
            }
            common = set.intersection(*(set(pairs) for pairs in pairs_by_image.values()))
            if common:
                image = min(common)
                return {journey: pairs[image] for journey, pairs in pairs_by_image.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError):
            continue
    raise GatewayGateError(
        'OpenClaw PR requires two passing, independently reviewed offline runs '
        f'on the same candidate image for each journey: {", ".join(required)}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journey", choices=(*JOURNEYS, *BROWSER_JOURNEYS, "audit"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument('--screenshot', type=Path, help='PNG evidence path for a Control UI browser journey')
    parser.add_argument('--recording', type=Path, help='JSONL recording path for a Control UI browser journey')
    parser.add_argument("--timeout", type=int, help='seconds (defaults to 320, 480 or 600 depending on the journey)')
    parser.add_argument("--memory-mb", type=int, default=3328)
    args = parser.parse_args(argv)
    if (args.journey in BROWSER_JOURNEYS) != (args.screenshot is not None):
        parser.error('--screenshot is required only for a Control UI browser journey')
    if args.screenshot is not None:
        args.screenshot = args.screenshot.expanduser().absolute()
        if args.screenshot.exists() or args.screenshot.is_symlink() or args.screenshot.resolve() == args.report.expanduser().resolve():
            parser.error('choose a new screenshot path separate from the JSON report')
    if args.recording is not None:
        args.recording = args.recording.expanduser().absolute()
        if (args.screenshot is None or args.recording.suffix != '.jsonl' or args.recording.exists()
                or args.recording.is_symlink()
                or args.recording.resolve() in (args.report.expanduser().resolve(), args.screenshot.resolve())):
            parser.error('choose a new JSONL recording path separate from the report and browser screenshot')
    # Reserve the evidence path before running; retries must use a new file.
    def private_create(path, flags):
        return os.open(path, flags, 0o600)

    with open(args.report.expanduser(), "x", encoding="utf-8", opener=private_create) as output:
        options = dict(timeout=_timeout(args.journey) if args.timeout is None else args.timeout, memory_mb=args.memory_mb)
        if args.journey == 'audit':
            report = audit(args.repo, args.image, **options)
        else:
            if args.screenshot is not None:
                options['screenshot'] = args.screenshot
            if args.recording is not None:
                options['recording'] = args.recording
            report = verify(args.repo, args.image, args.journey, **options)
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
