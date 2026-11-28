import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench import cli, publish, verify_openclaw


class GatewayGateTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.head = 'a' * 40
        self.data = {'id': 'pilot', 'repo': str(self.root), 'base_commit': 'b' * 40,
                     'implementer': 'builder', 'evidence': [], 'criteria': [{'id': 'x', 'text': 'x'}]}
        self.task = self.root / 'task.json'
        cli.write(self.task, {'schema': cli.SCHEMA, **self.data})
        self.destination = 'owner/myopenclaw'
        changed_paths = patch.object(cli, 'git', return_value='src/gateway/server.ts\0')
        self.changed_paths = changed_paths.start()
        self.addCleanup(changed_paths.stop)

    def reports(self, *, same_workspace=False, fail=False, report_head=None, skills_image=None,
                skills_inspect_image=None, config_image=None, workshop_image=None, rename=False, delete=False, status_filter=False,
                browser_receipt=True, same_container=False, restart=None, deletion=None, list_scope='all',
                filter_fault=None, rename_fields=None, archive=False, archive_fault=None, archive_tree=False,
                tree_fault=None, archive_sidebar=False, sidebar_fault=None, archive_running=False, running_fault=None,
                library_image=None, lifecycle_image=None, lifecycle_fault=None, import_image=None, import_fault=None,
                team_image=None, team_fault=None):
        refs = {}
        self.data['evidence'] = []
        gateway_image = 'sha256:' + 'f' * 64
        journeys = {'gateway-health': gateway_image}
        if skills_image is not None:
            journeys['skills-check'] = skills_image
        if skills_inspect_image is not None:
            journeys['skills-inspect'] = skills_inspect_image
        if config_image is not None:
            journeys['config-validate'] = config_image
        if workshop_image is not None:
            journeys['workshop-roundtrip'] = workshop_image
        if library_image is not None:
            journeys['skills-library-help'] = library_image
        if lifecycle_image is not None:
            journeys['skills-library-lifecycle'] = lifecycle_image
        if import_image is not None:
            journeys['skills-library-import'] = import_image
        if team_image is not None:
            journeys['skills-library-team'] = team_image
        if rename:
            journeys[verify_openclaw.SESSION_RENAME] = gateway_image
        if delete:
            journeys[verify_openclaw.SESSION_DELETE] = gateway_image
        if status_filter:
            journeys[verify_openclaw.SESSION_FILTER] = gateway_image
        if archive:
            journeys[verify_openclaw.SESSION_ARCHIVE] = gateway_image
        if archive_tree:
            journeys[verify_openclaw.SESSION_ARCHIVE_TREE] = gateway_image
        if archive_sidebar:
            journeys[verify_openclaw.SESSION_ARCHIVE_SIDEBAR] = gateway_image
        if archive_running:
            journeys[verify_openclaw.SESSION_ARCHIVE_RUNNING] = gateway_image
        for journey, image in journeys.items():
            helper, marker, command = verify_openclaw._journey(journey)
            for actor in ('builder', 'independent-verifier'):
                name = f'{journey}-{actor}'
                path = self.root / f'{name}.json'
                container = journey if same_container else name
                extra = {'stdout': marker + '\n', 'create_stdout': hashlib.sha256(container.encode()).hexdigest()}
                if journey == 'skills-library-lifecycle':
                    extra['stdout'] = self.lifecycle_stdout(lifecycle_fault)
                if journey == 'skills-library-import':
                    extra['stdout'] = self.import_stdout(import_fault)
                if journey == 'skills-library-team':
                    extra['stdout'] = self.team_stdout(team_fault)
                if journey in verify_openclaw.BROWSER_JOURNEYS:
                    png = path.with_suffix('.png')
                    png.write_bytes(name.encode())
                    rename_evidence = journey == verify_openclaw.SESSION_RENAME
                    stdout = self.browser_stdout(journey, fresh=browser_receipt if rename_evidence else True,
                                                 restart=restart if rename_evidence else None, deletion=deletion,
                                                 list_scope=list_scope, filter_fault=filter_fault, rename_fields=rename_fields,
                                                 archive_fault=archive_fault, tree_fault=tree_fault,
                                                 sidebar_fault=sidebar_fault, running_fault=running_fault)
                    extra.update(stdout=stdout, screenshot_path=str(png.resolve()), screenshot_sha256=cli.digest(png))
                cli.write(path, {
                    'journey': journey, 'actor': actor, 'commit': report_head or self.head,
                    'image': image, 'verified_image_id': image,
                    'helper_sha256': cli.digest(verify_openclaw.SKILL / helper),
                    'passed': not fail, 'exit_code': int(fail), 'cleanup_confirmed': True, 'timed_out': False,
                    'container_created': True, 'source_root': '/app', 'command': command,
                    'workspace_read_only': True, 'workspace': 'shared' if same_workspace else name,
                    **extra,
                })
                refs[name] = {'path': str(path), 'sha256': cli.digest(path)}
                self.data['evidence'].append({'kind': 'runtime-log', 'commit': self.head,
                                              'actor': actor, **refs[name]})
        path = self.root / 'review.json'
        cli.write(path, {'verdict': 'PASS', 'candidate_commit': self.head, 'reports': refs,
                         'baseline_commit': self.data['base_commit'], 'criterion_digest': cli.criteria_digest(self.data)})
        self.data['evidence'].append({
            'kind': 'independent-review', 'actor': 'independent-verifier', 'commit': self.head,
            'criteria_digest': cli.criteria_digest(self.data), 'path': str(path), 'sha256': cli.digest(path),
        })

    def team_stdout(self, fault=None):
        content = hashlib.sha256(b'---\nname: iboard-team-fixture\ndescription: Offline team library fixture\n---\n\nCheck the shared checklist.\n').hexdigest()
        support = hashlib.sha256(b'Team checklist attachment.\n').hexdigest()
        pin = {'skillId': 'skill-1', 'revision': 'a' * 64}
        profiles = {'owner': 'owner-id', 'member': 'member-id', 'admin': 'admin-id'}
        events = [
            {'phase': 'doctor', 'gatewayPid': 19, 'ready': True, 'live': True},
            {'phase': 'team', 'profiles': profiles, 'skill': {**pin, 'contentSha': content, 'supportSha': support},
             'unauthenticatedStatus': 401, 'sessions': {
                 'shared': {'key': 'agent:main:iboard-team-shared', 'id': 's1', 'selections': [pin]},
                 'unshared': {'key': 'agent:main:iboard-team-unshared', 'id': 's2', 'selections': []},
                 'team': {'key': 'agent:main:iboard-team-transferred', 'id': 's3', 'selections': [pin]},
             }, 'steps': {
                 'private': {'visibleCount': 0, 'directReadCode': 'SKILL_LIBRARY_NOT_FOUND'},
                 'shared': {'ownerId': 'owner-id', 'memberVisibleId': 'skill-1', 'memberCanEdit': False,
                            'memberMutationCode': 'SKILL_LIBRARY_FORBIDDEN', 'ownerTransferCode': 'SKILL_LIBRARY_FORBIDDEN'},
                 'unshared': {'ownerId': 'owner-id', 'visibleCount': 0, 'directReadCode': 'SKILL_LIBRARY_NOT_FOUND',
                              'contentSha': content, 'supportSha': support},
                 'transferred': {'ownerProfileId': None, 'ownerLabel': 'Team', 'authorProfileId': 'owner-id',
                                 'ownerMineCount': 0, 'adminCanEdit': True, 'formerOwnerMutationCode': 'SKILL_LIBRARY_FORBIDDEN',
                                 'adminUnshareCode': 'SKILL_LIBRARY_FORBIDDEN', 'contentSha': content, 'supportSha': support},
             }},
            {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True},
        ]
        if fault:
            fault(events)
        return ''.join(json.dumps(row) + '\n' for row in events) + 'VERIFY_OPENCLAW_SKILLS_LIBRARY_TEAM_PASS\n'

    def import_stdout(self, fault=None):
        content_hashes = (
            '9e2faa74838a991c575e68b03f0a0f3f64aba2ab74d3badb9ff0be127de14316',
            '9f4d6abb570bcb300e0f1dd5b0a8fc518b846d9dbbbfcb29d50a91b2638b4b22',
            '6b9ccfbb09aface5aff1fa5fa62742b5e411ee4f0cf789778a1610f9cf28d583',
        )
        support_hash = 'f66b77e64e5417fe76aecaf0b6859b59e78069a668b6a46377e27edc3abffab4'
        rows = [{'source': source, 'slug': f'iboard-import-{source}', 'skillId': f'skill-{index}',
                 'revision': hex(index)[2:] * 64, 'ownerProfileId': 'profile-1', 'enabled': True,
                 'shared': False, 'contentSha': content_hashes[index - 1], 'supportSha': support_hash}
                for index, source in enumerate(('directory', 'zip', 'clawhub'), 1)]
        events = [
            {'phase': 'doctor', 'gatewayPid': 19, 'ready': True, 'live': True},
            {'phase': 'imports', 'profileId': 'profile-1', 'rows': rows,
             'existingSessionId': 'session-1', 'newSessionId': 'session-2', 'existingSelections': [],
             'newSelections': [{'skillId': row['skillId'], 'revision': row['revision']} for row in rows],
             'rejected': {'malformedZipExit': 1, 'duplicateExit': 1, 'blockedRegistryExit': 1, 'libraryCount': 3},
             'registry': ['detail', 'trust', 'download', 'verify', 'blocked-detail', 'blocked-trust']},
            {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True},
        ]
        if fault:
            fault(events)
        return ''.join(json.dumps(row) + '\n' for row in events) + 'VERIFY_OPENCLAW_SKILLS_LIBRARY_IMPORT_PASS\n'

    def lifecycle_stdout(self, fault=None):
        a, b, old_hash, new_hash, support = (char * 64 for char in 'abcde')
        steps = {
            'initial': {'libraryCount': 0, 'targetSelections': 0, 'controlSelections': 0},
            'created': {'revision': a, 'ownerProfileId': 'profile-1', 'enabled': True, 'shared': False, 'supportSha': support},
            'attached': {'pin': a, 'activation': 'next-turn'},
            'updated': {'revision': b, 'pin': a, 'supportSha': support},
            'stale': {'exitCode': 1, 'preservedRevision': b, 'conflict': True},
            'refreshed': {'pin': b, 'oldPinRejected': True, 'controlUnselected': True, 'controlReadRejected': True},
            'toggled': {'disabled': False, 'enabled': True, 'revision': b},
            'rollback': {'revision': a, 'pin': b, 'supportSha': support},
            'restarted': {'profileId': 'profile-1', 'revision': a, 'pin': b, 'targetId': 'session-1',
                          'controlId': 'session-2', 'pinContentSha': new_hash, 'supportSha': support},
            'removed': {'listedCount': 0, 'directReadMarkedRemoved': True, 'directRevision': a,
                        'directContentSha': old_hash, 'editRejected': True, 'reattachRejected': True,
                        'pin': b, 'pinContentSha': new_hash},
            'detached': {'activation': 'next-turn', 'remaining': 0, 'pinReadRejected': True},
        }
        rows = [
            {'phase': 'doctor', 'gatewayPid': 19, 'ready': True, 'live': True},
            {'phase': 'restart', 'gatewayPid': 71, 'previousPid': 19, 'previousExit': {'code': 0, 'signal': None},
             'portClosed': True, 'ready': True, 'live': True},
            {'phase': 'library', 'profileId': 'profile-1', 'skillId': 'skill-1',
             'revisions': {'original': a, 'updated': b},
             'hashes': {'original': old_hash, 'updated': new_hash, 'support': support},
             'sessions': {'targetKey': 'agent:main:iboard-library-session', 'targetId': 'session-1',
                          'controlKey': 'agent:main:iboard-library-control', 'controlId': 'session-2'}, 'steps': steps},
            {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True},
        ]
        turns = []
        for index, (stage, identity, revision, content) in enumerate((
            ('attached', 'target', a, old_hash), ('after-update', 'target', a, old_hash),
            ('refreshed', 'target', b, new_hash), ('control', 'control', None, None),
            ('restarted', 'target', b, new_hash), ('detached', 'target', None, None),
        )):
            turns.append({'stage': stage,
                          'sessionKey': rows[2]['sessions'][identity + 'Key'],
                          'sessionId': rows[2]['sessions'][identity + 'Id'],
                          'runId': f'00000000-0000-4000-8000-{index + 1:012d}',
                          'status': 'ok', 'cliPid': 100 + index,
                          'skillPath': f'skill-library/skill-1/revisions/{revision}/SKILL.md' if revision else None,
                          'contentSha': content, 'supportSha': support if revision else None})
        rows[2]['turns'] = turns
        if fault:
            fault(rows)
        return ''.join(json.dumps(row) + '\n' for row in rows) + 'VERIFY_OPENCLAW_SKILLS_LIBRARY_LIFECYCLE_PASS\n'

    def browser_stdout(self, journey, *, fresh=True, restart=None, deletion=None, list_scope='all', filter_fault=None,
                       rename_fields=None, archive_fault=None, tree_fault=None, sidebar_fault=None, running_fault=None):
        writing = journey in verify_openclaw.SESSION_WRITES
        healthy = {'ready': True, 'live': True, 'origin': 'http://127.0.0.1:18789'}
        clean_exit = {'code': 0, 'signal': None}
        events = [
            {'phase': 'doctor', 'gatewayPid': 19, **healthy},
            {'phase': 'ui', 'route': '/sessions', 'servedCommit': self.head, 'screenshot': True,
             'gatewayPid': 71 if writing else 19,
             'handshake': {'protocol': 4, 'method': 'token', 'role': 'operator'},
             'readback': {'method': 'sessions.list', 'count': int(writing)}},
            {'phase': 'cleanup', 'exit': clean_exit, 'scratchRemoved': True},
        ]
        if journey == verify_openclaw.SESSION_RENAME:
            events[1]['mutation'] = {'method': 'sessions.patch', 'key': 'agent:main:iboard-browser-verification',
                                     'sessionId': 'fixture-session-id', 'originalLabel': 'iBoard verification fixture',
                                     'finalLabel': 'iBoard verification persisted', 'canceledWithoutWrite': True,
                                     'accepted': True, 'freshContextReadback': fresh,
                                     'menuEntries': ['context-menu', 'row-button'],
                                     'expectedSessionId': 'fixture-session-id', 'requestCount': 1,
                                     **(rename_fields or {})}
        if journey == verify_openclaw.SESSION_DELETE:
            events[1]['readback']['archived'] = list_scope
            events[1]['mutation'] = {
                'method': 'sessions.delete', 'key': 'agent:main:iboard-browser-verification',
                'sessionId': 'fixture-session-id', 'expectedSessionId': 'fixture-session-id',
                'responseKey': 'agent:main:iboard-browser-verification',
                'requestCount': 1, 'afterCancelCount': 2, 'canceledWithoutWrite': True,
                'cancelBrowserReplaced': True, 'cancelGatewayPid': 19,
                'deleted': True, 'freshContextReadback': fresh, 'targetAbsent': True,
                'survivor': {'key': 'agent:main:iboard-browser-survivor', 'sessionId': 'other-id'},
                **(deletion or {}),
            }
        if journey == verify_openclaw.SESSION_FILTER:
            active = {'key': 'agent:main:iboard-browser-verification', 'sessionId': 'fixture-session-id'}
            archived = {'key': 'agent:main:iboard-browser-archived', 'sessionId': 'archived-session-id'}
            steps = []
            for number, (state, trigger, visible) in enumerate((
                ('active', 'load', [active]), ('archived', 'filter', [archived]),
                ('all', 'filter', [active, archived]), ('active', 'filter-refresh', [active]),
            )):
                step = {'requestId': f'read-{number}', 'source': 'sessions-page', 'state': state,
                        'trigger': trigger, 'gateway': visible, 'visibleKeys': [item['key'] for item in visible]}
                if state != 'active':
                    step['archived'] = True if state == 'archived' else 'all'
                steps.append(step)
            events[1]['readback']['count'] = 1
            evidence = {'fixtures': {'active': active, 'archived': archived}, 'steps': steps, 'sessionMutations': 0}
            if filter_fault is not None:
                filter_fault(evidence)
            events[1]['filter'] = evidence
        if writing and restart is not False:
            events.insert(1, {'phase': 'restart', **healthy, 'gatewayPid': 71,
                              'previousPid': 19, 'previousExit': clean_exit, 'portClosed': True, **(restart or {})})
        if journey == verify_openclaw.SESSION_ARCHIVE_RUNNING:
            target = {'key': 'agent:main:iboard-browser-verification', 'sessionId': 'fixture-session-id'}
            survivor = {'key': 'agent:main:iboard-browser-survivor', 'sessionId': 'other-id'}
            ui = events[-2]
            ui['readback'].update(count=2, archived='all')
            ui['mutation'] = {**target, 'survivor': survivor, 'canceledWithoutWrite': True,
                'run': {'id': 'run-1', 'childPid': 47, 'activeBefore': True, 'activeAfterCancel': True,
                        'archiveEvent': {'runId': 'run-1', 'sessionKey': target['key'],
                                         'state': 'aborted', 'stopReason': 'archive'},
                        'wait': {'runId': 'run-1', 'status': 'error', 'stopReason': 'aborted'},
                        'childExitSignal': 'SIGTERM', 'childExited': True, 'noCompletion': True},
                'write': {'method': 'sessions.patch', 'requestId': 'write-0', 'key': target['key'],
                          'expectedSessionId': target['sessionId'], 'archived': True, 'accepted': True,
                          'responseKey': target['key'], 'responseSessionId': target['sessionId'], 'responseArchived': True},
                'steps': [{'stage': stage, 'gatewayPid': 71 if index == 2 else 19, 'requestId': f'read-{index}',
                           'rows': [{**target, 'archived': index == 2}, {**survivor, 'archived': False}]}
                          for index, stage in enumerate(('initial', 'after-cancel', 'after-archive-restart'))]}
            if running_fault:
                running_fault(events, ui['mutation'])
        if journey == verify_openclaw.SESSION_ARCHIVE:
            target = {'key': 'agent:main:iboard-browser-verification', 'sessionId': 'fixture-session-id'}
            survivor = {'key': 'agent:main:iboard-browser-survivor', 'sessionId': 'other-id'}
            ui = events[-2]
            ui['gatewayPid'] = 93
            ui['readback']['count'] = 2
            ui['mutation'] = {**target, 'survivor': survivor, 'writes': [
                {'method': 'sessions.patch', 'requestId': f'write-{index}', 'key': target['key'],
                 'expectedSessionId': target['sessionId'], 'archived': archived, 'accepted': True,
                 'responseSessionId': target['sessionId'], 'responseKey': target['key'], 'responseArchived': archived}
                for index, archived in enumerate((True, False))], 'steps': [
                {'state': state, 'requestId': f'read-{index}', 'gatewayPid': pid, 'rows': rows}
                for index, (state, pid, rows) in enumerate((
                    ('active', 19, [target, survivor]), ('archived', 71, [target]), ('active', 93, [target, survivor])))
            ]}
            events.insert(2, {'phase': 'restart', **healthy, 'gatewayPid': 93,
                              'previousPid': 71, 'previousExit': clean_exit, 'portClosed': True})
            if archive_fault:
                archive_fault(events, ui['mutation'])
        if journey in (verify_openclaw.SESSION_ARCHIVE_TREE, verify_openclaw.SESSION_ARCHIVE_SIDEBAR):
            sidebar = journey == verify_openclaw.SESSION_ARCHIVE_SIDEBAR
            second = 'second' if sidebar else 'nested'
            fixtures = [
                {'key': 'agent:main:iboard-browser-verification', 'sessionId': 'parent-id'},
                {'key': f'agent:main:iboard-browser-{second}', 'sessionId': f'{second}-id'},
                {'key': 'agent:main:iboard-browser-survivor', 'sessionId': 'survivor-id'},
            ]
            ui = events[-2]
            ui['readback'].update(count=3, archived='all')
            ui['mutation'] = {**fixtures[0], second: fixtures[1], 'survivor': fixtures[2],
                'canceledWithoutWrite': True, 'writes': [
                    {'method': 'sessions.patchMany', 'requestId': f'write-{index}', 'archived': archived, 'accepted': True,
                     'targets': [{'key': row['key'], 'expectedSessionId': row['sessionId'], 'agentId': 'main'}
                                 for row in fixtures[:2]],
                     'outcomes': [{'key': row['key'], 'agentId': 'main', 'ok': True} for row in fixtures[:2]]}
                    for index, archived in enumerate((True, False))],
                'steps': [{'stage': stage, 'requestId': f'read-{index}', 'gatewayPid': 71 if index == 3 else 19,
                           'rows': [{**row, 'archived': index == 2 and order < 2} for order, row in enumerate(fixtures)]}
                          for index, stage in enumerate(('initial', 'after-cancel', 'after-archive', 'after-undo-restart'))]}
            if sidebar:
                selected = sorted(row['key'] for row in fixtures[:2])
                ui['mutation'].update(selections=[list(selected), list(selected)],
                    warningJobId='selected-job', sidebarVisibilityChecked=True, automationReads=[
                        {'stage': step['stage'], 'gatewayPid': step['gatewayPid'], 'jobs': [
                            {'id': 'selected-job', 'name': 'iBoard verification fixture automation',
                             'sessionTarget': 'main', 'sessionKey': fixtures[0]['key'],
                             'enabled': index < 2, 'neverRun': True},
                            {'id': 'survivor-job', 'name': 'iBoard unselected fixture automation',
                             'sessionTarget': 'main', 'sessionKey': fixtures[2]['key'],
                             'enabled': True, 'neverRun': True},
                        ]} for index, step in enumerate(ui['mutation']['steps'])
                    ])
                if sidebar_fault:
                    sidebar_fault(ui['mutation'])
            elif tree_fault:
                tree_fault(events, ui['mutation'])
        return '\n'.join([*(json.dumps(event) for event in events), verify_openclaw._journey(journey)[1], ''])

    def test_publish_and_republish_block_before_any_github_call(self):
        github = Mock()
        for kwargs in ({'fail': True}, {'same_workspace': True}, {'same_container': True},
                       {'report_head': 'b' * 40}, None):
            if kwargs is None:
                self.data['evidence'] = []
            else:
                self.reports(**kwargs)
            with patch.object(cli, 'read', return_value=self.data), patch.object(cli, 'gate', return_value=(self.head, [])), patch.object(publish.delivery, '_independent_verification'):
                for call, extra in ((publish.publish, ()), (publish.republish, (1, 'b' * 40))):
                    with self.assertRaisesRegex(publish.PublishError, 'two passing'):
                        call(self.task, self.destination, *extra, github=github)
        self.assertEqual(github.mock_calls, [])

    def test_passes_matching_pair_and_blocks_modified_evidence(self):
        self.reports()
        verify_openclaw.require(self.data, self.head, self.destination)
        (self.root / 'gateway-health-builder.json').write_text('{}')
        with self.assertRaises(verify_openclaw.GatewayGateError):
            verify_openclaw.require(self.data, self.head, self.destination)

    def test_skills_change_requires_all_journeys_reviewed_on_one_image(self):
        self.changed_paths.return_value = 'src/skills/discovery/status.ts\0'
        for skills_image in (None, 'sha256:' + 'e' * 64):
            with self.subTest(skills_image=skills_image):
                self.reports(skills_image=skills_image)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'skills-check'):
                    verify_openclaw.require(self.data, self.head, self.destination)
        image = 'sha256:' + 'f' * 64
        self.reports(skills_image=image)
        with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'skills-inspect'):
            verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(skills_image=image, skills_inspect_image=image)
        self.data.update(kind='feature', delivery_repo=self.destination)
        self.data['evidence'].append({'kind': 'test-result', 'commit': self.head,
                                     'actor': 'independent-verifier', 'sha256': '0' * 64})
        with patch.object(publish, '_feature_doc_link', return_value='https://example.com/doc'), \
                patch.object(publish, '_report_summary', return_value=('test passed', '')):
            body = publish._pr_body(self.data, self.head)
        for journey in ('gateway-health', 'skills-check', 'skills-inspect'):
            for actor in ('builder', 'independent-verifier'):
                digest = cli.digest(self.root / f'{journey}-{actor}.json')
                self.assertIn(digest, body)

    def test_map_audit_stops_when_a_registered_journey_is_missing_from_index(self):
        features = self.root / 'features'
        features.mkdir()
        (features / 'README.md').write_text('| [gateway-health](gateway-health.md) | Health |')
        for name in verify_openclaw.JOURNEYS:
            (features / f'{name}.md').write_text(name)
        with patch.object(verify_openclaw, 'SKILL', self.root), patch.object(verify_openclaw, 'verify') as run:
            report = verify_openclaw.audit(self.root, 'local', timeout=30, memory_mb=512)
        self.assertEqual(report['status'], 'map-drift')
        self.assertFalse(report['passed'])
        self.assertIn('skills-check', report['registered'])
        run.assert_not_called()

    def test_configuration_changes_need_config_receipts_on_the_shared_image(self):
        self.changed_paths.return_value = 'src/config/zod-schema.gateway.ts\0'
        for image in (None, 'sha256:' + 'e' * 64):
            self.reports(config_image=image)
            with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'config-validate'):
                verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(config_image='sha256:' + 'f' * 64)
        receipts = verify_openclaw.require(self.data, self.head, self.destination)
        self.assertEqual(set(receipts), {'gateway-health', 'config-validate'})

    def test_workshop_changes_require_two_matching_roundtrip_receipts(self):
        image = 'sha256:' + 'f' * 64
        for changed in ('src/cli/skills-cli.ts', 'src/skills/workshop/library.ts'):
            with self.subTest(changed=changed):
                self.changed_paths.return_value = changed + '\0'
                self.reports(skills_image=image, skills_inspect_image=image)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'workshop-roundtrip'):
                    verify_openclaw.require(self.data, self.head, self.destination)
                self.reports(skills_image=image, skills_inspect_image=image, workshop_image='sha256:' + 'e' * 64)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'workshop-roundtrip'):
                    verify_openclaw.require(self.data, self.head, self.destination)
                self.reports(skills_image=image, skills_inspect_image=image, workshop_image=image,
                             library_image=image, lifecycle_image=image, import_image=image, team_image=image)
                receipts = verify_openclaw.require(self.data, self.head, self.destination)
                self.assertEqual(len(receipts['workshop-roundtrip']), 2)

    def test_library_help_requires_both_independent_receipts_on_the_same_image(self):
        image = 'sha256:' + 'f' * 64
        journey = 'skills-library-help'
        helper, marker, command = verify_openclaw._journey(journey)
        self.assertEqual((helper, marker, command, verify_openclaw._timeout(journey)),
                         ('skills-library-help.mjs', 'VERIFY_OPENCLAW_SKILLS_LIBRARY_HELP_PASS',
                          ['node', '/workspace/skills-library-help.mjs'], 320))
        self.assertNotIn(journey, verify_openclaw.BROWSER_JOURNEYS)
        for path in ('src/cli/skills-library-cli.ts', 'src/cli/skills-cli.ts',
                     'src/cli/program/parent-default-help.ts'):
            with self.subTest(path=path):
                self.changed_paths.return_value = path + '\0'
                selected = verify_openclaw.required_journeys(self.data, self.head)
                self.assertIn(journey, selected)
                if path == 'src/cli/skills-cli.ts':
                    self.assertEqual(selected, ['gateway-health', 'skills-check', 'skills-inspect',
                                                'workshop-roundtrip', journey, 'skills-library-lifecycle', 'skills-library-import',
                                                'skills-library-team'])
                elif path == 'src/cli/skills-library-cli.ts':
                    self.assertEqual(selected, ['gateway-health', journey, 'skills-library-lifecycle', 'skills-library-import',
                                                'skills-library-team'])
                else:
                    self.assertEqual(selected, ['gateway-health', journey])
                options = {'skills_image': image, 'skills_inspect_image': image, 'workshop_image': image}
                self.reports(**options)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
                github = Mock()
                with patch.object(cli, 'read', return_value=self.data), \
                        patch.object(cli, 'gate', return_value=(self.head, [])), \
                        patch.object(publish.delivery, '_independent_verification'):
                    for call, extra in ((publish.publish, ()), (publish.republish, (1, 'b' * 40))):
                        with self.assertRaisesRegex(publish.PublishError, journey):
                            call(self.task, self.destination, *extra, github=github)
                self.assertEqual(github.mock_calls, [])
                self.reports(**options, library_image='sha256:' + 'e' * 64)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
                self.reports(**options, library_image=image, lifecycle_image=image, import_image=image, team_image=image)
                self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)
                (self.root / f'{journey}-independent-verifier.json').write_text('{}')
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.changed_paths.return_value = 'src/cli/skills-library-cli.test.ts\0'
        self.assertNotIn(journey, verify_openclaw.required_journeys(self.data, self.head))

    def test_library_lifecycle_binds_real_restart_and_all_observations(self):
        journey = 'skills-library-lifecycle'
        image = 'sha256:' + 'f' * 64
        self.assertEqual(verify_openclaw._timeout(journey), 600)
        for path in ('src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts', 'src/skills/library/store.ts',
                     'src/gateway/server-methods/skills-library.ts', 'src/gateway/gateway-owner-profile.ts',
                     'packages/gateway-protocol/src/schema/skill-library.ts',
                     'src/agents/cli-runner/skills-prompt.ts', 'src/agents/cli-runner/prepare.ts',
                     'src/agents/cli-runner/execute.ts', 'src/agents/cli-runner/execute-process.ts',
                     'src/agents/cli-runner/helpers.ts', 'src/agents/embedded-agent-runner/skills-prompt.ts',
                     'src/skills/runtime/session-snapshot.ts', 'src/skills/runtime/embedded-run-entries.ts',
                     'src/skills/loading/workspace-skill-prompt.ts'):
            self.changed_paths.return_value = path + '\0'
            self.assertIn(journey, verify_openclaw.required_journeys(self.data, self.head))
        self.changed_paths.return_value = 'src/gateway/server-methods/skills-library.ts\0'
        faults = (
            lambda r: r[0].update(gatewayPid=True),
            lambda r: r[1].update(gatewayPid=19),
            lambda r: r[1].update(previousPid=17),
            lambda r: r[1].update(previousExit={'code': None, 'signal': 'SIGKILL'}),
            lambda r: r[1].update(portClosed=False),
            lambda r: r[2].update(profileId=''),
            lambda r: r[2]['revisions'].update(updated=r[2]['revisions']['original']),
            lambda r: r[2]['hashes'].update(updated=r[2]['hashes']['original']),
            lambda r: r[2]['hashes'].update(support='bad-hash'),
            lambda r: r[2]['sessions'].update(targetKey='agent:main:other'),
            lambda r: r[2]['sessions'].update(controlId=r[2]['sessions']['targetId']),
            lambda r: r[2]['steps']['initial'].update(libraryCount=False),
            lambda r: r[2]['steps']['created'].update(ownerProfileId='other'),
            lambda r: r[2]['steps']['created'].update(shared=True),
            lambda r: r[2]['steps']['attached'].update(pin=r[2]['revisions']['updated']),
            lambda r: r[2]['steps']['updated'].update(pin=r[2]['revisions']['updated']),
            lambda r: r[2]['steps']['updated'].update(supportSha='bad'),
            lambda r: r[2]['steps']['stale'].update(exitCode=True),
            lambda r: r[2]['steps']['stale'].update(conflict=False),
            lambda r: r[2]['steps']['refreshed'].update(oldPinRejected=False),
            lambda r: r[2]['steps']['refreshed'].update(controlUnselected=False),
            lambda r: r[2]['steps']['refreshed'].update(controlReadRejected=False),
            lambda r: r[2]['steps']['refreshed'].pop('controlReadRejected'),
            lambda r: r[2]['steps']['toggled'].update(disabled=0),
            lambda r: r[2]['steps']['rollback'].update(revision=r[2]['revisions']['updated']),
            lambda r: r[2]['steps']['rollback'].update(pin=r[2]['revisions']['original']),
            lambda r: r[2]['steps']['restarted'].update(profileId='other'),
            lambda r: r[2]['steps']['restarted'].update(targetId='other'),
            lambda r: r[2]['steps']['restarted'].update(pinContentSha=r[2]['hashes']['original']),
            lambda r: r[2]['steps']['removed'].update(directReadMarkedRemoved=False),
            lambda r: r[2]['steps']['removed'].update(directRevision=r[2]['revisions']['updated']),
            lambda r: r[2]['steps']['removed'].update(directContentSha=r[2]['hashes']['updated']),
            lambda r: r[2]['steps']['removed'].update(editRejected=False),
            lambda r: r[2]['steps']['removed'].update(reattachRejected=False),
            lambda r: r[2]['steps']['removed'].pop('directReadMarkedRemoved'),
            lambda r: r[2]['steps']['removed'].update(pinContentSha=r[2]['hashes']['original']),
            lambda r: r[2]['steps']['detached'].update(pinReadRejected=False),
            lambda r: r[2]['steps']['detached'].update(remaining=False),
            lambda r: r[3].update(scratchRemoved=1),
            lambda r: r[2]['turns'].pop(),
            lambda r: r[2]['turns'][0].update(stage='after-update'),
            lambda r: r[2]['turns'][0].update(sessionKey=r[2]['sessions']['controlKey']),
            lambda r: r[2]['turns'][0].update(sessionId=r[2]['sessions']['controlId']),
            lambda r: r[2]['turns'][0].update(runId='not-a-uuid'),
            lambda r: r[2]['turns'][0].update(runId='00000000-0000-1000-8000-000000000001'),
            lambda r: r[2]['turns'][1].update(runId=r[2]['turns'][0]['runId']),
            lambda r: r[2]['turns'][0].update(status='error'),
            lambda r: r[2]['turns'][0].update(cliPid=True),
            lambda r: r[2]['turns'][0].update(cliPid=r[0]['gatewayPid']),
            lambda r: r[2]['turns'][0].update(skillPath=r[2]['turns'][2]['skillPath']),
            lambda r: r[2]['turns'][1].update(contentSha=r[2]['hashes']['updated']),
            lambda r: r[2]['turns'][2].update(supportSha='bad'),
            lambda r: r[2]['turns'][3].update(skillPath=r[2]['turns'][0]['skillPath']),
            lambda r: r[2]['turns'][3].update(contentSha=r[2]['hashes']['original']),
            lambda r: r[2]['turns'][4].update(contentSha=r[2]['hashes']['original']),
            lambda r: r[2]['turns'][5].update(supportSha=r[2]['hashes']['support']),
            lambda r: r[2]['turns'][0].update(extra=True),
        )
        for fault in faults:
            with self.subTest(fault=fault):
                self.reports(lifecycle_image=image, lifecycle_fault=fault)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(lifecycle_image=image, import_image=image, team_image=image)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

        run = {'exit_code': 0, 'cleanup_confirmed': True, 'container_created': True,
               'workspace_read_only': True, 'stdout': self.lifecycle_stdout()}
        with patch.object(cli, 'sha', return_value=self.head), \
                patch.object(verify_openclaw.workflow, '_verified_image', return_value=image), \
                patch.object(verify_openclaw, 'run_sandbox', return_value=run):
            self.assertTrue(verify_openclaw.verify(self.root, image, journey, timeout=600, memory_mb=512)['passed'])
            run['stdout'] = self.lifecycle_stdout(lambda rows: rows[2]['steps']['stale'].update(conflict=False))
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=600, memory_mb=512)['passed'])
            run['stdout'] = 'VERIFY_OPENCLAW_SKILLS_LIBRARY_LIFECYCLE_PASS\n'
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=600, memory_mb=512)['passed'])

    def test_library_import_binds_real_sources_rejections_and_new_session_selection(self):
        journey = 'skills-library-import'
        image = 'sha256:' + 'f' * 64
        self.assertEqual(verify_openclaw._journey(journey), ('skills-library-lifecycle.mjs',
            'VERIFY_OPENCLAW_SKILLS_LIBRARY_IMPORT_PASS', ['node', '/workspace/skills-library-lifecycle.mjs', 'import']))
        self.assertEqual(verify_openclaw._timeout(journey), 480)
        for path in ('src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts', 'src/skills/library/import.ts',
                     'src/skills/library/import.kernel.ts', 'src/skills/library/bundle.ts',
                     'src/skills/library/receipt.ts', 'src/gateway/server-methods/skills-library.ts',
                     'src/gateway/server-methods/sessions-create.ts', 'src/gateway/skill-library-session.ts',
                     'packages/gateway-protocol/src/schema/skill-library.ts',
                     'src/infra/clawhub-client.ts', 'src/infra/clawhub-artifacts.ts',
                     'src/infra/clawhub-install-trust.ts', 'src/skills/lifecycle/clawhub.ts',
                     'src/skills/lifecycle/clawhub-install-core.ts', 'src/skills/lifecycle/clawhub-install-rollback.ts'):
            self.changed_paths.return_value = path + '\0'
            self.assertIn(journey, verify_openclaw.required_journeys(self.data, self.head))
        self.changed_paths.return_value = 'src/infra/clawhub-client.ts\0'
        faults = (
            lambda e: e[0].update(ready=False),
            lambda e: e[0].update(gatewayPid=True),
            lambda e: e[1].update(profileId=''),
            lambda e: e[1].update(newSessionId=e[1]['existingSessionId']),
            lambda e: e[1].update(existingSelections=[{'skillId': 'unexpected'}]),
            lambda e: e[1]['rows'].pop(),
            lambda e: e[1]['rows'].reverse(),
            lambda e: e[1]['rows'][0].update(slug='other'),
            lambda e: e[1]['rows'][0].update(ownerProfileId='other'),
            lambda e: e[1]['rows'][0].update(enabled=1),
            lambda e: e[1]['rows'][0].update(shared=0),
            lambda e: e[1]['rows'][1].update(skillId=e[1]['rows'][0]['skillId']),
            lambda e: e[1]['rows'][1].update(contentSha=e[1]['rows'][0]['contentSha']),
            lambda e: e[1]['rows'][0].update(revision='invalid'),
            lambda e: e[1]['rows'][0].update(supportSha='invalid'),
            lambda e: e[1]['rows'][0].update(supportSha='0' * 64),
            lambda e: e[1]['rows'][0].update(contentSha='0' * 64),
            lambda e: e[1]['newSelections'][0].update(revision=e[1]['rows'][1]['revision']),
            lambda e: e[1]['newSelections'][0].update(skillId='other'),
            lambda e: e[1]['newSelections'].pop(),
            lambda e: e[1]['rejected'].update(duplicateExit=0),
            lambda e: e[1]['rejected'].update(blockedRegistryExit=True),
            lambda e: e[1]['rejected'].update(libraryCount=4),
            lambda e: e[1]['rejected'].pop('malformedZipExit'),
            lambda e: e[1]['registry'].remove('verify'),
            lambda e: e[1]['registry'].reverse(),
            lambda e: e[2].update(scratchRemoved=1),
        )
        for fault in faults:
            with self.subTest(fault=fault):
                self.reports(import_image=image, import_fault=fault)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(import_image=image, import_fault=lambda e: e[1]['newSelections'].reverse())
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)
        run = {'exit_code': 0, 'cleanup_confirmed': True, 'container_created': True,
               'workspace_read_only': True, 'stdout': self.import_stdout()}
        with patch.object(cli, 'sha', return_value=self.head), \
                patch.object(verify_openclaw.workflow, '_verified_image', return_value=image), \
                patch.object(verify_openclaw, 'run_sandbox', return_value=run):
            self.assertTrue(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])
            run['stdout'] = self.import_stdout(lambda e: e[1]['rejected'].update(blockedRegistryExit=0))
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])
            run['stdout'] = 'VERIFY_OPENCLAW_SKILLS_LIBRARY_IMPORT_PASS\n'
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])

    def test_team_library_gate_proves_private_shared_retained_and_transferred_authority(self):
        journey = 'skills-library-team'
        image = 'sha256:' + 'f' * 64
        self.assertEqual(verify_openclaw._journey(journey), ('skills-library-team.mjs',
            'VERIFY_OPENCLAW_SKILLS_LIBRARY_TEAM_PASS', ['node', '/workspace/skills-library-team.mjs']))
        self.assertEqual(verify_openclaw._timeout(journey), 480)
        for path in ('src/cli/skills-cli.ts', 'src/cli/skills-library-cli.ts', 'src/skills/library/store.ts',
                     'src/skills/library/selection.ts', 'src/gateway/server-methods/skills-library.ts',
                     'src/gateway/auth.ts', 'src/gateway/role-policy.ts', 'src/gateway/server-methods/users.ts',
                     'src/gateway/operator-role-policy.ts', 'src/gateway/server-methods/sessions-create.ts',
                     'src/gateway/skill-library-session.ts', 'src/gateway/server/ws-connection/connect-auth.ts',
                     'src/gateway/server/ws-connection/connect-admission.ts',
                     'packages/gateway-protocol/src/schema/skill-library.ts'):
            self.changed_paths.return_value = path + '\0'
            self.assertIn(journey, verify_openclaw.required_journeys(self.data, self.head))
        self.changed_paths.return_value = 'src/gateway/auth.ts\0'
        faults = (
            lambda e: e[0].update(ready=False),
            lambda e: e[0].update(gatewayPid=True),
            lambda e: e[1].update(unauthenticatedStatus=200),
            lambda e: e[1]['profiles'].update(member=e[1]['profiles']['owner']),
            lambda e: e[1]['profiles'].update(admin=''),
            lambda e: e[1]['skill'].update(revision='bad'),
            lambda e: e[1]['skill'].update(contentSha='0' * 64),
            lambda e: e[1]['skill'].update(supportSha='0' * 64),
            lambda e: e[1]['sessions']['shared'].update(key='other'),
            lambda e: e[1]['sessions']['team'].update(id=e[1]['sessions']['shared']['id']),
            lambda e: e[1]['sessions']['shared']['selections'][0].update(revision='b' * 64),
            lambda e: e[1]['sessions']['unshared'].update(selections=[e[1]['skill']]),
            lambda e: e[1]['steps']['private'].update(visibleCount=False),
            lambda e: e[1]['steps']['private'].update(directReadCode='OK'),
            lambda e: e[1]['steps']['shared'].update(ownerId=e[1]['profiles']['member']),
            lambda e: e[1]['steps']['shared'].update(memberVisibleId='other'),
            lambda e: e[1]['steps']['shared'].update(memberCanEdit=0),
            lambda e: e[1]['steps']['shared'].update(memberMutationCode='OK'),
            lambda e: e[1]['steps']['shared'].update(ownerTransferCode='OK'),
            lambda e: e[1]['steps']['unshared'].update(visibleCount=1),
            lambda e: e[1]['steps']['unshared'].update(supportSha='0' * 64),
            lambda e: e[1]['steps']['transferred'].update(ownerProfileId=e[1]['profiles']['owner']),
            lambda e: e[1]['steps']['transferred'].update(authorProfileId=e[1]['profiles']['admin']),
            lambda e: e[1]['steps']['transferred'].update(adminCanEdit=False),
            lambda e: e[1]['steps']['transferred'].update(formerOwnerMutationCode='OK'),
            lambda e: e[1]['steps']['transferred'].update(adminUnshareCode='OK'),
            lambda e: e[2].update(scratchRemoved=1),
        )
        for fault in faults:
            with self.subTest(fault=fault):
                self.reports(team_image=image, team_fault=fault)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(team_image=image)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)
        run = {'exit_code': 0, 'cleanup_confirmed': True, 'container_created': True,
               'workspace_read_only': True, 'stdout': self.team_stdout()}
        with patch.object(cli, 'sha', return_value=self.head), \
                patch.object(verify_openclaw.workflow, '_verified_image', return_value=image), \
                patch.object(verify_openclaw, 'run_sandbox', return_value=run):
            self.assertTrue(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])
            run['stdout'] = self.team_stdout(lambda e: e[1]['steps']['shared'].update(memberCanEdit=True))
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])
            run['stdout'] = 'VERIFY_OPENCLAW_SKILLS_LIBRARY_TEAM_PASS\n'
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, timeout=480, memory_mb=512)['passed'])

    def test_connected_ui_requires_a_bound_screenshot_in_its_own_output(self):
        journey = 'control-ui-connected'
        helper, _ = verify_openclaw.BROWSER_JOURNEYS[journey]
        image = 'sha256:' + 'f' * 64
        screenshot = self.root / 'connected.png'
        screenshot.write_bytes(b'verified capture')
        options = {'timeout': 90, 'memory_mb': 3328}
        with self.assertRaisesRegex(ValueError, 'requires a screenshot path'):
            verify_openclaw.verify(self.root, image, journey, **options)
        sandbox = {'exit_code': 0, 'cleanup_confirmed': True, 'stdout': self.browser_stdout(journey)}
        replies = iter((sandbox, {**sandbox, 'screenshot_sha256': cli.digest(screenshot),
                                  'screenshot_path': str(screenshot.resolve())}))

        def execute(scratch, *args, **kwargs):
            self.assertEqual((Path(scratch) / 'expected-commit.txt').read_text(), self.head + '\n')
            return next(replies)

        with patch.object(cli, 'sha', return_value=self.head), \
                patch.object(verify_openclaw.workflow, '_verified_image', return_value=image), \
                patch.object(verify_openclaw, 'run_sandbox', side_effect=execute) as run:
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, screenshot=screenshot, **options)['passed'])
            result = verify_openclaw.verify(self.root, image, journey, screenshot=screenshot, **options)
        self.assertTrue(result['passed'])
        self.assertEqual(result['helper_sha256'], cli.digest(verify_openclaw.SKILL / helper))
        self.assertEqual(run.call_args.args[1:3], (image, ['node', f'/workspace/{helper}']))
        self.assertEqual(run.call_args.kwargs['screenshot_output'], screenshot)
        self.assertEqual(run.call_args.kwargs['pids_limit'], 384)
        self.assertNotIn(journey, verify_openclaw.required_journeys(self.data, self.head))

    def test_sessions_ui_delivery_rechecks_browser_receipts_before_writing(self):
        self.changed_paths.return_value = 'ui/src/pages/sessions/page-dialog.ts\0'
        github = Mock()
        cases = ({}, {'delete': True}, {'rename': True}, *(
            {'rename': True, 'delete': True, 'status_filter': True, **options} for options in (
                {'browser_receipt': False}, {'same_container': True},
                {'restart': False}, {'restart': {'portClosed': False}},
                {'restart': {'gatewayPid': 19}}, {'restart': {'gatewayPid': 73}},
                {'restart': {'previousExit': {'code': None, 'signal': 'SIGKILL'}}},
                {'rename_fields': {'menuEntries': ['context-menu', 'context-menu']}},
                {'rename_fields': {'menuEntries': None}}, {'rename_fields': {'requestCount': 2}},
                {'rename_fields': {'requestCount': True}}, {'rename_fields': {'expectedSessionId': 'different-session'}},
            )
        ))
        for options in cases:
            with self.subTest(options=options):
                self.reports(**options)
                with patch.object(cli, 'read', return_value=self.data), \
                        patch.object(cli, 'gate', return_value=(self.head, [])), \
                        patch.object(publish.delivery, '_independent_verification'):
                    with self.assertRaisesRegex(publish.PublishError, 'control-ui-session-rename'):
                        publish.publish(self.task, self.destination, github=github)
        self.assertEqual(github.mock_calls, [])
        self.reports(rename=True, delete=True, status_filter=True, archive=True, archive_tree=True, archive_sidebar=True, archive_running=True)
        receipts = verify_openclaw.require(self.data, self.head, self.destination)
        _, verifier = receipts['control-ui-session-rename']
        self.data.update(kind='feature', delivery_repo=self.destination)
        self.data['evidence'].append({'kind': 'test-result', 'commit': self.head,
                                     'actor': 'independent-verifier', 'sha256': '0' * 64})
        with patch.object(publish, '_feature_doc_link', return_value='https://example.com/doc'), \
                patch.object(publish, '_report_summary', return_value=('test passed', '')):
            self.assertIn(verifier['screenshot_sha256'], publish._pr_body(self.data, self.head))
        (self.root / 'control-ui-session-rename-independent-verifier.png').write_bytes(b'exchanged image')
        with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'control-ui-session-rename'):
            verify_openclaw.require(self.data, self.head, self.destination)

    def test_session_delete_gate_requires_cancellation_target_and_survivor_after_restart(self):
        self.changed_paths.return_value = 'src/gateway/server-methods/sessions-delete.ts\0'
        self.assertEqual(verify_openclaw.required_journeys(self.data, self.head),
                         ['gateway-health', verify_openclaw.SESSION_DELETE])
        self.assertTrue(verify_openclaw.needs_browser(self.data, self.head))
        mismatches = ({'deleted': False}, {'targetAbsent': False}, {'requestCount': 2},
                      {'canceledWithoutWrite': False}, {'afterCancelCount': 1},
                      {'cancelBrowserReplaced': False}, {'cancelGatewayPid': 71},
                      {'expectedSessionId': 'wrong-id'}, {'responseKey': 'other-key'}, {'freshContextReadback': False},
                      {'survivor': {'key': 'agent:main:iboard-browser-survivor',
                                    'sessionId': 'fixture-session-id'}})
        github = Mock()
        with patch.object(cli, 'read', return_value=self.data), \
                patch.object(cli, 'gate', return_value=(self.head, [])), \
                patch.object(publish.delivery, '_independent_verification'):
            for fields in mismatches:
                with self.subTest(fields=fields):
                    self.reports(delete=True, deletion=fields)
                    with self.assertRaisesRegex(publish.PublishError, 'control-ui-session-delete'):
                        publish.publish(self.task, self.destination, github=github)
        self.assertEqual(github.mock_calls, [])
        self.reports(delete=True, list_scope='active')
        with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'control-ui-session-delete'):
            verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(delete=True)
        passed = verify_openclaw.require(self.data, self.head, self.destination)
        self.assertEqual(len(passed[verify_openclaw.SESSION_DELETE]), 2)

    def test_status_filter_gate_requires_real_read_scopes_and_visible_rows(self):
        journey = verify_openclaw.SESSION_FILTER
        for path in ('src/gateway/server-methods/sessions-read.ts', 'src/gateway/session-list-view-cache.ts',
                     'src/gateway/session-row-presentation.ts', 'packages/gateway-protocol/src/session-row-fields.ts'):
            with self.subTest(path=path):
                self.changed_paths.return_value = path + '\0'
                self.assertEqual(verify_openclaw.required_journeys(self.data, self.head),
                                 ['gateway-health', journey, verify_openclaw.SESSION_ARCHIVE,
                                  verify_openclaw.SESSION_ARCHIVE_TREE, verify_openclaw.SESSION_ARCHIVE_SIDEBAR,
                                  verify_openclaw.SESSION_ARCHIVE_RUNNING])
        github = Mock()
        faults = (
            lambda evidence: evidence['steps'].pop(),
            lambda evidence: evidence['steps'][1].update(archived='all'),
            lambda evidence: evidence['steps'][0].update(archived=False),
            lambda evidence: evidence['steps'][1].update(source='sidebar'),
            lambda evidence: evidence['steps'][3].update(requestId=evidence['steps'][0]['requestId']),
            lambda evidence: evidence['steps'][3].update(trigger='filter'),
            lambda evidence: evidence['steps'][2].update(visibleKeys=['wrong-key']),
            lambda evidence: evidence['steps'][2].update(gateway=evidence['steps'][1]['gateway']),
            lambda evidence: evidence.update(sessionMutations=1),
        )
        with patch.object(cli, 'read', return_value=self.data), \
                patch.object(cli, 'gate', return_value=(self.head, [])), \
                patch.object(publish.delivery, '_independent_verification'):
            self.reports()
            with self.assertRaisesRegex(publish.PublishError, journey):
                publish.publish(self.task, self.destination, github=github)
            for fault in faults:
                self.reports(status_filter=True, filter_fault=fault)
                with self.assertRaisesRegex(publish.PublishError, journey):
                    publish.publish(self.task, self.destination, github=github)
        self.assertEqual(github.mock_calls, [])
        self.reports(status_filter=True, archive=True, archive_tree=True, archive_sidebar=True, archive_running=True)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

    def test_archive_gate_requires_both_writes_and_both_graceful_restarts(self):
        self.changed_paths.return_value = 'ui/src/components/session-organizer-archive.runtime.ts\0'
        journey = verify_openclaw.SESSION_ARCHIVE
        self.assertEqual(verify_openclaw.required_journeys(self.data, self.head),
                         ['gateway-health', journey, verify_openclaw.SESSION_ARCHIVE_TREE,
                          verify_openclaw.SESSION_ARCHIVE_SIDEBAR, verify_openclaw.SESSION_ARCHIVE_RUNNING])
        self.assertTrue(verify_openclaw.needs_browser(self.data, self.head))
        faults = (
            lambda events, mutation: events.pop(2),
            lambda events, mutation: events[2].update(previousPid=19),
            lambda events, mutation: mutation['writes'][0].update(responseSessionId='replaced'),
            lambda events, mutation: mutation['writes'][1].update(archived=True),
            lambda events, mutation: mutation['writes'][1].update(responseArchived=True),
            lambda events, mutation: mutation['steps'][1].update(gatewayPid=19),
            lambda events, mutation: mutation['steps'][1]['rows'].append(mutation['survivor']),
            lambda events, mutation: mutation['steps'][2].update(rows=[]),
            lambda events, mutation: mutation['steps'][2].update(requestId=mutation['writes'][1]['requestId']),
        )
        for fault in faults:
            self.reports(archive=True, archive_fault=fault)
            with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(archive=True, archive_tree=True, archive_sidebar=True, archive_running=True)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

    def test_subtree_archive_gate_rechecks_cancel_batch_targets_and_undo_state(self):
        journey = verify_openclaw.SESSION_ARCHIVE_TREE
        self.changed_paths.return_value = 'packages/gateway-protocol/src/schema/sessions-create.ts\0'
        self.assertEqual(verify_openclaw.required_journeys(self.data, self.head), ['gateway-health', journey])
        self.assertTrue(verify_openclaw.needs_browser(self.data, self.head))
        faults = (
            lambda events, mutation: mutation.update(canceledWithoutWrite=False),
            lambda events, mutation: mutation['steps'][1]['rows'][1].update(archived=True),
            lambda events, mutation: mutation['writes'][0]['targets'][1].update(expectedSessionId='different'),
            lambda events, mutation: mutation['writes'][0]['targets'].append(mutation['writes'][0]['targets'][0]),
            lambda events, mutation: mutation['writes'][1]['outcomes'][1].update(ok=False),
            lambda events, mutation: mutation['steps'][2]['rows'][2].update(archived=True),
            lambda events, mutation: mutation['steps'][3]['rows'][0].update(archived=True),
            lambda events, mutation: mutation['steps'][3].update(gatewayPid=19),
            lambda events, mutation: mutation['steps'][3].update(requestId=mutation['writes'][1]['requestId']),
        )
        for fault in faults:
            self.reports(archive_tree=True, tree_fault=fault)
            with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(archive_tree=True)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

    def test_running_archive_gate_binds_abort_child_exit_write_and_restart(self):
        journey = verify_openclaw.SESSION_ARCHIVE_RUNNING
        self.assertEqual(verify_openclaw._timeout(journey), 600)
        self.assertEqual(verify_openclaw._journey(journey)[1],
                         'VERIFY_OPENCLAW_CONTROL_UI_SESSION_ARCHIVE_RUNNING_PASS')
        for path in ('src/gateway/server-methods/sessions-lifecycle-drain.ts',
                     'src/gateway/server-methods/chat-abort-runtime.ts',
                     'src/gateway/server-methods/sessions-subscriptions.ts', 'src/gateway/chat-abort.ts',
                     'src/gateway/chat-abort-ops.ts', 'src/sessions/session-controller.lifecycle.ts',
                     'src/gateway/server-methods/sessions-patch-effects.ts',
                     'src/agents/cli-runner/execute-process.ts',
                     'ui/src/components/session-organizer-patch.runtime.ts',
                     'ui/src/components/confirm-dialog-view.tsx'):
            self.changed_paths.return_value = path + '\0'
            self.assertIn(journey, verify_openclaw.required_journeys(self.data, self.head))
        self.changed_paths.return_value = 'src/gateway/server-methods/sessions-lifecycle-drain.ts\0'
        faults = (
            lambda e, m: m.update(canceledWithoutWrite=False),
            lambda e, m: m['run'].update(activeBefore=False),
            lambda e, m: m['run'].update(activeAfterCancel=False),
            lambda e, m: m['run'].update(id=''),
            lambda e, m: m['run'].update(childPid=True),
            lambda e, m: m['run'].update(childPid=19),
            lambda e, m: m['run'].update(childPid=71),
            lambda e, m: m['run']['archiveEvent'].update(runId='other'),
            lambda e, m: m['run']['archiveEvent'].update(sessionKey='agent:main:other'),
            lambda e, m: m['run']['archiveEvent'].update(state='completed'),
            lambda e, m: m['run']['archiveEvent'].update(stopReason='aborted'),
            lambda e, m: m['run']['archiveEvent'].pop('sessionKey'),
            lambda e, m: m['run']['wait'].update(runId='other'),
            lambda e, m: m['run']['wait'].update(status='running'),
            lambda e, m: m['run']['wait'].update(status='success'),
            lambda e, m: m['run']['wait'].update(stopReason='cancel'),
            lambda e, m: m['run']['wait'].update(extra=True),
            lambda e, m: m['run'].update(childExitSignal='SIGKILL'),
            lambda e, m: m['run'].update(childExited=False),
            lambda e, m: m['run'].update(noCompletion=False),
            lambda e, m: m['write'].update(expectedSessionId='other'),
            lambda e, m: m['write'].update(responseKey='other'),
            lambda e, m: m['write'].update(responseSessionId='other'),
            lambda e, m: m['write'].update(responseArchived=False),
            lambda e, m: m['write'].update(accepted=False),
            lambda e, m: m['write'].update(requestId=m['steps'][0]['requestId']),
            lambda e, m: m['steps'][1].update(gatewayPid=71),
            lambda e, m: m['steps'][1]['rows'][0].update(archived=True),
            lambda e, m: m['steps'][1]['rows'][0].update(archived=0),
            lambda e, m: m['steps'][2]['rows'][0].update(archived=1),
            lambda e, m: m['steps'][2]['rows'][1].update(archived=True),
            lambda e, m: m['steps'][2]['rows'][0].update(sessionId='other'),
            lambda e, m: m['steps'][2].update(gatewayPid=19),
            lambda e, m: e[1].update(previousExit={'code': None, 'signal': 'SIGKILL'}),
            lambda e, m: e[-2]['readback'].update(archived=False),
        )
        for fault in faults:
            with self.subTest(fault=fault):
                self.reports(archive_running=True, running_fault=fault)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(archive_running=True)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)
        self.reports(archive_running=True, running_fault=lambda e, m: m['run']['wait'].update(stopReason='archive'))
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

    def test_sidebar_review_requires_pause_scope_and_persistence_across_undo(self):
        journey = verify_openclaw.SESSION_ARCHIVE_SIDEBAR
        for path in ('ui/src/components/app-sidebar-session-navigation-logic.ts',
                     'src/cron/job-session-bindings.ts', 'src/gateway/server-methods/sessions-patch-effects.ts'):
            with self.subTest(path=path):
                self.changed_paths.return_value = path + '\0'
                expected = ['gateway-health', journey]
                if path == 'src/gateway/server-methods/sessions-patch-effects.ts':
                    expected.append(verify_openclaw.SESSION_ARCHIVE_RUNNING)
                self.assertEqual(verify_openclaw.required_journeys(self.data, self.head), expected)
        faults = (
            lambda mutation: mutation['selections'][0].pop(),
            lambda mutation: mutation.update(warningJobId='survivor-job'),
            lambda mutation: mutation.update(sidebarVisibilityChecked=False),
            lambda mutation: mutation['automationReads'][0]['jobs'].pop(),
            lambda mutation: mutation['automationReads'][1]['jobs'][0].update(enabled=False),
            lambda mutation: mutation['automationReads'][2]['jobs'][0].update(enabled=True),
            lambda mutation: mutation['automationReads'][2]['jobs'][1].update(enabled=False),
            lambda mutation: mutation['automationReads'][3]['jobs'][0].update(enabled=True),
            lambda mutation: mutation['automationReads'][3]['jobs'][0].update(neverRun=False),
            lambda mutation: mutation['automationReads'][3]['jobs'][0].update(sessionKey='different'),
            lambda mutation: mutation['automationReads'][3].update(gatewayPid=19),
        )
        for fault in faults:
            self.reports(archive_sidebar=True, sidebar_fault=fault)
            with self.assertRaisesRegex(verify_openclaw.GatewayGateError, journey):
                verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(archive_sidebar=True, archive_running=True)
        self.assertEqual(len(verify_openclaw.require(self.data, self.head, self.destination)[journey]), 2)

    def test_collect_keeps_failed_browser_run_for_task_recovery(self):
        self.changed_paths.return_value = 'ui/src/lib/session-rename.ts\0'
        calls = []

        def execute(repo, image, journey, **options):
            calls.append((journey, options))
            return {'commit': self.head, 'passed': len(calls) < 4, 'timed_out': len(calls) == 4,
                    'cleanup_confirmed': True, 'stdout': ''}

        task = self.root / 'task.json'
        with patch.object(verify_openclaw, 'verify', side_effect=execute):
            with self.assertRaisesRegex(verify_openclaw.workflow.WorkflowError, 'raw report saved at') as blocked:
                verify_openclaw.collect(task, self.data, self.head, 'image', ('builder', 'independent-verifier'),
                                        remaining=lambda: 360, memory_mb=3328)
        self.assertEqual([options['timeout'] for _, options in calls], [320, 320, 360, 360])
        paths = [options['screenshot'] for journey, options in calls if journey == 'control-ui-session-rename']
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(*paths)
        self.assertTrue(all(path.with_suffix('.json').is_file() for path in paths))
        self.assertIn(str(paths[1].with_suffix('.json')), str(blocked.exception))
