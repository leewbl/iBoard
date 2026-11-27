import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, project_verify, verify_openclaw


IMAGE = 'sha256:' + 'a' * 64


class ProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'app.txt').write_text('\nregistered doctor\nregistered first\nregistered second\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'fixture')
        self.revision = cli.sha(self.repo, 'HEAD')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        self.sequence = []
        self.fail = set()
        self.no_cleanup = set()
        checks = []
        for name in ('doctor', 'first', 'second'):
            marker = f'JOURNEY_{name.upper()}_PASS'
            helper = name + '.sh'
            (self.helpers / helper).write_text(f"printf '%s\\n' '{marker}'\n")
            checks.append({'id': name, 'helper': helper, 'marker': marker, 'reach': 'Open the app.',
                           'drive': 'Run a user command.', 'observe': 'Inspect the result.',
                           'gotchas': 'A fixture instance is required.',
                           'source': [{'path': 'app.txt', 'contains': f'registered {name}'}]})
        self.contract = {'project': 'fixture', 'surface': 'Fixture CLI', 'launch': 'Use an isolated workdir.',
                         'cleanup': 'Remove test data.', 'doctor': checks[0], 'features': checks[1:]}
        self.manifest = self.root / 'input.json'
        self.manifest.write_text(json.dumps(self.contract))

    def attester(self, repo, image, timeout, *, commit):
        self.assertEqual(repo, self.repo)
        self.assertEqual(commit, self.revision)
        self.assertEqual(image, 'local-image')
        return IMAGE

    def sandbox(self, scratch, image, command, timeout, **opts):
        self.assertEqual(image, IMAGE)
        self.assertEqual(opts['source_root'], '/app')
        self.assertTrue(opts['workspace_read_only'])
        helper = Path(command[-1]).name
        self.assertEqual([path.name for path in Path(scratch).iterdir()], [helper])
        key = Path(helper).stem
        self.sequence.append(key)
        return {'image': image, 'command': command, 'workspace_read_only': True,
                'container_created': True, 'cleanup_confirmed': key not in self.no_cleanup,
                'exit_code': int(key in self.fail), 'stdout': f'JOURNEY_{key.upper()}_PASS\n'}

    def run_mode(self, mode, name, **kwargs):
        return project_verify.run(mode, repo=self.repo, revision=self.revision, image='local-image',
                                  out=self.root / name, memory_mb=512,
                                  sandbox=self.sandbox, attester=self.attester, **kwargs)

    def create(self):
        result = self.run_mode('create', 'created', manifest=self.manifest, helpers=self.helpers, journey='second')
        self.assertEqual(result['outcome'], 'verified')
        return self.root / 'created' / result['bundle']

    def test_generation_proves_one_feature_and_anchors_every_feature(self):
        bundle = self.create()
        self.assertEqual(self.sequence, ['doctor', 'second'])
        self.assertTrue((bundle / 'helpers' / 'first.sh').stat().st_mode & 0o111)
        self.assertIn('(second.md)', (bundle / 'features' / 'README.md').read_text())
        report = cli.read(bundle.parent / 'result.json')
        self.assertEqual(report['source']['first'][0]['line'], 3)
        for row in report['runs']:
            self.assertEqual(cli.digest(bundle.parent / row['report']), row['sha256'])
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_map_drift_requires_all_journeys_to_pass_before_exporting_a_correction(self):
        bundle = self.create()
        (bundle / 'features' / 'README.md').write_text('# Missing map\n')
        blocked = self.run_mode('maintain', 'drift', skill=bundle)
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['map_drift'], ['features/README.md'])
        self.assertEqual(blocked['features'], {'first': 'passed', 'second': 'passed'})
        self.fail.add('first')
        failed = self.run_mode('maintain', 'failed-fix', skill=bundle, manifest=self.manifest, helpers=self.helpers)
        self.assertEqual(failed['outcome'], 'blocked')
        self.assertEqual(failed['bundle'], 'draft')
        self.assertEqual(failed['features'], {'first': 'behavior-unverified', 'second': 'passed'})
        self.assertEqual(self.sequence[-4:], ['doctor', 'first', 'doctor', 'second'])
        self.assertFalse((self.root / 'failed-fix' / 'changes.patch').exists())
        self.fail.clear()
        passed = self.run_mode('maintain', 'fixed', skill=bundle, manifest=self.manifest, helpers=self.helpers)
        self.assertEqual(passed['outcome'], 'changed')
        self.assertEqual(passed['replaces_unsafe_entries'], [])
        self.assertIn('after/features/README.md', (self.root / 'fixed' / 'changes.patch').read_text())
        self.assertEqual((bundle / 'features' / 'README.md').read_text(), '# Missing map\n')
        clean = self.run_mode('maintain', 'clean', skill=self.root / 'fixed' / passed['bundle'])
        self.assertEqual(clean['outcome'], 'clean')

    def test_missing_source_or_cleanup_blocks_release_despite_pass_marker(self):
        self.contract['features'][0]['source'][0]['contains'] = 'removed anchor'
        self.manifest.write_text(json.dumps(self.contract))
        self.no_cleanup.add('second')
        result = self.run_mode('create', 'blocked', manifest=self.manifest, helpers=self.helpers, journey='second')
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(result['source_drift'], ['first'])
        self.assertEqual(result['features'], {'second': 'behavior-unverified'})
        self.assertFalse((self.root / 'blocked' / 'verify-fixture').exists())

    def test_symlinked_helper_directory_is_neither_read_nor_executed(self):
        bundle = self.create()
        (bundle / 'helpers').rename(bundle / 'relocated')
        (bundle / 'helpers').symlink_to(self.helpers, target_is_directory=True)
        self.sequence.clear()
        result = self.run_mode('maintain', 'blocked', skill=bundle)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('helpers', result['map_drift'])
        self.assertEqual(self.sequence, [])

    def test_missing_feature_helper_is_recorded_and_doctor_allows_the_next_journey(self):
        bundle = self.create()
        (bundle / 'helpers' / 'first.sh').unlink()
        result = self.run_mode('maintain', 'missing-helper', skill=bundle)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(self.sequence[-3:], ['doctor', 'doctor', 'second'])
        failed = cli.read(self.root / 'missing-helper' / 'first.json')
        self.assertEqual(failed['diagnostic'], 'harness-input')
        self.assertTrue(result['features']['second'] == 'passed')

    def test_browser_journey_is_reusable_and_rejects_a_stale_served_build(self):
        check = self.contract['features'][1]
        check.update(browser=True, screenshot=True, args=['one selection'])
        self.manifest.write_text(json.dumps(self.contract))
        served = self.revision
        recorded_path = None

        cli_sandbox = self.sandbox

        def sandbox(scratch, image, command, timeout, **opts):
            if command[1] != '/workspace/second.sh':
                return cli_sandbox(scratch, image, command, timeout, **opts)
            self.assertEqual(command, ['sh', '/workspace/second.sh', 'one selection'])
            scratch = Path(scratch)
            self.assertEqual({p.name for p in scratch.iterdir()}, {'second.sh', 'expected-commit.txt'})
            self.assertEqual((scratch / 'expected-commit.txt').read_text(), self.revision + '\n')
            self.assertEqual(opts['pids_limit'], 384)
            picture = opts['screenshot_output']
            picture.write_bytes(b'captured screenshot fixture')
            receipt = {'phase': 'ui', 'servedCommit': served, 'screenshot': True}
            return {'image': image, 'command': command, 'workspace_read_only': True,
                    'container_created': True, 'cleanup_confirmed': True, 'exit_code': 0,
                    'screenshot_path': recorded_path or str(picture), 'screenshot_sha256': cli.digest(picture),
                    'stdout': json.dumps(receipt) + '\n' + check['marker'] + '\n'}

        self.sandbox = sandbox
        result = self.run_mode('create', 'created-browser', manifest=self.manifest, helpers=self.helpers, journey='second')
        self.assertEqual(result['outcome'], 'verified')
        bundle = self.root / 'created-browser' / result['bundle']
        self.assertIn('second.png', (bundle / 'features' / 'second.md').read_text())
        self.assertIn("sh /workspace/second.sh 'one selection'", (bundle / 'SKILL.md').read_text())
        maintained = self.run_mode('maintain', 'browser-audit', skill=bundle)
        self.assertEqual(maintained['features'], {'first': 'passed', 'second': 'passed'})
        self.assertEqual(maintained['outcome'], 'clean')
        served = 'f' * 40
        blocked = self.run_mode('drive', 'stale-browser', skill=bundle, journey='second')
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['features'], {'second': 'behavior-unverified'})
        self.assertEqual([row['check'] for row in blocked['runs']], ['doctor', 'second', 'doctor'])
        served = self.revision
        recorded_path = str(self.root / 'an-unrelated.png')
        wrong_picture = self.run_mode('drive', 'wrong-picture', skill=bundle, journey='second')
        self.assertEqual(wrong_picture['outcome'], 'blocked')

    def test_browser_contract_and_openclaw_gate_share_the_same_entrypoints(self):
        check = self.contract['features'][1]
        check['browser'] = True
        self.manifest.write_text(json.dumps(self.contract))
        with self.assertRaisesRegex(ValueError, 'requires a screenshot'):
            project_verify._load_contract(self.manifest)
        check.update(screenshot=True, args='not an argv array')
        self.manifest.write_text(json.dumps(self.contract))
        with self.assertRaisesRegex(ValueError, 'literal helper arguments'):
            project_verify._load_contract(self.manifest)
        contract = project_verify._load_contract(verify_openclaw.SKILL / 'project-verification.json')
        browser = {entry['id']: entry for entry in contract['features'] if entry.get('browser')}
        self.assertEqual(set(browser), set(verify_openclaw.BROWSER_JOURNEYS))
        for key, entry in browser.items():
            helper, marker, command = verify_openclaw._journey(key)
            self.assertEqual((entry['helper'], entry['marker'], project_verify._command(entry)),
                             (helper, marker, command))

    def test_discovery_uses_committed_source_and_detects_missing_and_stale_commands(self):
        path = self.repo / 'register.ts'
        path.write_text('skills.command("first");\nskills.command("old");\n')
        cli.git(self.repo, 'add', 'register.ts')
        cli.git(self.repo, 'commit', '-qm', 'initial CLI')
        old = cli.sha(self.repo, 'HEAD')
        path.write_text(
            '// skills.command("commented");\n'
            'const example = \'skills.command("inside-string")\';\n'
            'const helper = skills\n  /* a real registration */\n  .command("first");\n'
            'attach(skills.command(\'new-subcommand\'));\n'
            'other.command("unrelated");\n'
            'skills.command("outside");\n'
        )
        cli.git(self.repo, 'add', 'register.ts')
        cli.git(self.repo, 'commit', '-qm', 'new CLI')
        self.revision = cli.sha(self.repo, 'HEAD')
        path.write_text('skills.command("only-in-worktree");\n')
        self.contract['entrypoint_inventory'] = [
            {'namespace': 'fixture skills', 'sources': [{'path': 'register.ts', 'receiver': 'skills'}],
             'excluded': {'outside': 'Needs the authenticated test service.'}}
        ]
        self.contract['features'][0]['entrypoints'] = ['fixture skills first']
        self.contract['features'][1]['entrypoints'] = ['fixture skills old']
        self.manifest.write_text(json.dumps(self.contract))

        report = project_verify.discover(repo=self.repo, revision=self.revision, since=old,
                                         out=self.root / 'discovery', manifest=self.manifest)
        self.assertEqual(report['outcome'], 'review-required')
        coverage = report['entrypoints']
        self.assertEqual(coverage['unmapped'], ['fixture skills new-subcommand'])
        self.assertEqual(coverage['stale'], ['fixture skills old'])
        entries = {entry['command']: entry for entry in coverage['entries']}
        self.assertEqual(entries['fixture skills first']['locations'][0]['line'], 5)
        self.assertEqual(entries['fixture skills outside']['status'], 'excluded')
        self.assertEqual(report['comparison']['removed'], ['fixture skills old'])
        self.assertEqual(report['comparison']['added'], ['fixture skills new-subcommand', 'fixture skills outside'])

        proof = self.run_mode('create', 'incomplete', manifest=self.manifest, helpers=self.helpers)
        self.assertEqual(proof['outcome'], 'blocked')
        self.assertEqual(proof['features'], {'first': 'passed'})
        self.assertFalse((self.root / 'incomplete' / 'verify-fixture').exists())
        self.assertIn('inventory needs map corrections', proof['reason'])

    def use_session_status_inventory(self):
        namespace = 'fixture sessions status'
        self.contract['surface'] = 'Fixture CLI and Sessions UI'
        self.contract['entrypoint_inventory'] = [{
            'namespace': namespace,
            'sources': [{'kind': 'jsx-options', 'path': 'sessions.tsx', 'component': 'SettingsSegmented',
                         'label_key': 'sessionsView.sessionState'}],
            'excluded': {},
        }]
        self.contract['features'][0]['entrypoints'] = [f'{namespace} active']
        self.contract['features'][1]['entrypoints'] = [f'{namespace} archived']
        self.manifest.write_text(json.dumps(self.contract))
        return namespace

    def commit_session_controls(self, source):
        path = self.repo / 'sessions.tsx'
        path.write_text(source)
        cli.git(self.repo, 'add', 'sessions.tsx')
        cli.git(self.repo, 'commit', '-qm', 'fixture Sessions controls')
        self.revision = cli.sha(self.repo, 'HEAD')
        return path

    def test_jsx_discovery_reports_committed_option_changes_and_source_locations(self):
        namespace = self.use_session_status_inventory()
        before = (
            '/* <SettingsSegmented ariaLabel={t("sessionsView.sessionState")} '
            'options={[{value: "commented", label: "Comment"}]} /> */\n'
            'const example = \'<SettingsSegmented ariaLabel={t("sessionsView.sessionState")} '
            'options={[{value: "quoted", label: "Quote"}]} />\';\n'
            'const unrelated = <SettingsSegmented ariaLabel={t("sessionsView.sortOrder")} '
            'options={[{value: "updated", label: "Updated"}]} />;\n'
            'const status = <SettingsSegmented<SessionStatus>\n'
            '  value={props.statusFilter}\n'
            '  ariaLabel={t("sessionsView.sessionState")}\n'
            '  options={[\n'
            '    { value: "active", label: t("common.active") },\n'
            '    { value: "archived", label: t("sessionsView.archived") },\n'
            '  ]}\n'
            '  onChange={(value) => props.onStatusFilterChange(value)}\n'
            '/>;\n'
        )
        path = self.commit_session_controls(before)
        original = self.revision
        after = before.replace('value: "archived"', "value: 'all'")
        self.commit_session_controls(after)
        path.write_text(after.replace("value: 'all'", 'value: "only-in-worktree"'))

        result = project_verify.discover(repo=self.repo, revision=self.revision, since=original,
                                         out=self.root / 'ui-discovery', manifest=self.manifest)
        self.assertEqual(result['outcome'], 'review-required')
        self.assertEqual(result['comparison']['errors'], [])
        self.assertEqual(result['comparison']['added'], [f'{namespace} all'])
        self.assertEqual(result['comparison']['removed'], [f'{namespace} archived'])
        inventory = result['entrypoints']
        self.assertEqual(inventory['errors'], [])
        self.assertEqual(inventory['unmapped'], [f'{namespace} all'])
        self.assertEqual(inventory['stale'], [f'{namespace} archived'])
        entries = {row['command']: row for row in inventory['entries']}
        self.assertEqual(set(entries), {f'{namespace} active', f'{namespace} all'})
        blob = cli.git(self.repo, 'rev-parse', f'{self.revision}:sessions.tsx')
        for option, line, status in (('active', 8, 'mapped'), ('all', 9, 'unmapped')):
            entry = entries[f'{namespace} {option}']
            self.assertEqual(entry['kind'], 'jsx-options')
            self.assertEqual(entry['status'], status)
            self.assertEqual(entry['locations'], [{'path': 'sessions.tsx', 'blob': blob, 'line': line}])
        self.assertEqual(entries[f'{namespace} active']['journey'], 'first')

    def test_unresolved_jsx_inventory_blocks_create_and_maintenance_even_with_green_drives(self):
        self.use_session_status_inventory()
        options = '[{ value: "active", label: "Active" }, { value: "archived", label: "Archived" }]'
        control = (
            '<SettingsSegmented ariaLabel={t("sessionsView.sessionState")} '
            f'options={{{options}}} />'
        )
        self.commit_session_controls(f'const status = {control};\n')
        bundle = self.create()
        cases = {
            'computed-options': control.replace(options, 'choices'),
            'computed-value': control.replace('value: "active"', 'value: currentStatus'),
            'ambiguous-control': f'<>{control}{control}</>',
            'spread-attribute': control.replace('/>', '{...configuration} />'),
            'repeated-options': control.replace('/>', 'options />'),
            'escaped-override': control.replace('value: "active"', r'value: "active", "\u0076alue": "unmapped"'),
        }
        for case, body in cases.items():
            with self.subTest(case=case):
                self.commit_session_controls(f'const status = {body};\n')
                created = self.run_mode('create', f'create-{case}', manifest=self.manifest, helpers=self.helpers)
                self.assertEqual(created['outcome'], 'blocked')
                self.assertEqual(created['features'], {'first': 'passed'})
                self.assertFalse((self.root / f'create-{case}' / 'verify-fixture').exists())
                maintained = self.run_mode('maintain', f'maintain-{case}', skill=bundle)
                self.assertEqual(maintained['outcome'], 'blocked')
                self.assertEqual(maintained['features'], {'first': 'passed', 'second': 'passed'})
                for result in (created, maintained):
                    self.assertIn('inventory needs map corrections', result['reason'])
                    self.assertTrue(result['entrypoints']['errors'])
                    self.assertTrue(all(error['path'] == 'sessions.tsx' for error in result['entrypoints']['errors']))
