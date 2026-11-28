import hashlib
import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from runner.sandbox import (
    MAX_CREATE_OUTPUT, MAX_OUTPUT, MAX_RECORDING, SandboxError, _copy_screenshot, _time_ns, _validated_png, run_sandbox,
)


def tiny_png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.calls = []

    def fake(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout="null\n", stderr="")
        if args[1] == "start":
            kwargs["stdout"].write(b"hello\n")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def run_it(self, invoke=None, **kwargs):
        return run_sandbox(self.workspace, "example:latest", ["printf", "hello"], 10,
                           invoke=invoke or self.fake, find_executable=lambda _: "/usr/bin/docker",
                           **kwargs)

    def test_docker_missing_never_runs_host_command(self):
        with self.assertRaisesRegex(SandboxError, "Docker is unavailable"):
            run_sandbox(self.workspace, "example", ["echo", "hello"], 1,
                        invoke=self.fake, find_executable=lambda _: None)
        self.assertEqual(self.calls, [])

    def test_creates_locked_down_container_and_records_output(self):
        report = self.run_it()
        args = next(args for args, _ in self.calls if args[1] == "create")
        self.assertEqual(args[1], "create")
        for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges"):
            self.assertIn(flag, args)
        self.assertEqual(args.count("--mount"), 1)
        self.assertIn(f"type=bind,src={self.workspace.resolve()},dst=/workspace", args)
        self.assertIn("/tmp:rw,nosuid,nodev,size=1024m", args)
        self.assertNotIn("--env", args)
        self.assertIn("--entrypoint", args)
        self.assertEqual(args[args.index("--workdir") + 1], "/workspace")
        self.assertEqual(report["source_root"], "/workspace")
        self.assertEqual(report["container_workdir"], "/workspace")
        self.assertEqual(report["stdout"], "hello\n")
        self.assertEqual(report["stdout_sha256"], hashlib.sha256(b"hello\n").hexdigest())
        self.assertEqual(report["stderr_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["tmpfs_mb"], 1024)
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(self.calls[-1][0][1:3], ["rm", "--force"])

    def test_focused_regression_mount_is_read_only_pinned_and_discarded(self):
        test_source = b"console.log('hello');\n"
        pinned_id = 'sha256:' + 'a' * 64
        artifact_paths = []

        def invoke(args, **kwargs):
            if args[1:3] == ['image', 'inspect']:
                return subprocess.CompletedProcess(args, 0, stdout=pinned_id + '\nnull\n', stderr='')
            if args[1] == 'create':
                mount = next(arg for arg in args if arg.endswith(',dst=/workbench-regression,readonly'))
                test_path = Path(mount.split(',')[1].removeprefix('src=')) / 'check.mjs'
                self.assertEqual(test_path.read_bytes(), test_source)
                artifact_paths.append(test_path)
                self.assertIn(pinned_id, args)
                self.assertIn('--network=none', args)
                self.assertIn(f'type=bind,src={self.workspace.resolve()},dst=/workspace,readonly', args)
            return self.fake(args, **kwargs)

        report = run_sandbox(
            self.workspace, 'example:latest', ['node', '/workbench-regression/check.mjs'], 10,
            invoke=invoke, find_executable=lambda _: '/usr/bin/docker',
            workspace_read_only=True, regression_source=test_source)
        self.assertEqual(report['image'], pinned_id)
        self.assertTrue(report['cleanup_confirmed'])
        self.assertEqual(len(artifact_paths), 1)
        self.assertFalse(artifact_paths[0].exists())

    def test_focused_regression_captures_screenshot_without_writable_driver_alias(self):
        source = b"console.log('visual regression');\n"
        for exit_code, capture in ((0, False), (1, True), (1, False)):
            with self.subTest(exit_code=exit_code, capture=capture):
                paths = []
                def invoke(args, **kwargs):
                    if args[1:3] == ['image', 'inspect']:
                        return subprocess.CompletedProcess(args, 0, stdout='sha256:' + 'a' * 64 + '\nnull\n', stderr='')
                    if args[1] == 'create':
                        self.assertIn('--network=none', args)
                        self.assertIn(f'type=bind,src={self.workspace.resolve()},dst=/workspace,readonly', args)
                        driver = next(arg for arg in args if arg.endswith(',dst=/workbench-regression,readonly'))
                        artifacts = next(arg for arg in args if arg.endswith(',dst=/artifacts'))
                        driver_dir = Path(driver.split(',')[1].removeprefix('src='))
                        artifact_dir = Path(artifacts.split(',')[1].removeprefix('src='))
                        self.assertNotEqual(driver_dir, artifact_dir)
                        self.assertFalse(driver_dir.is_relative_to(artifact_dir))
                        self.assertFalse(artifact_dir.is_relative_to(driver_dir))
                        self.assertEqual((driver_dir / 'check.mjs').read_bytes(), source)
                        self.assertFalse((artifact_dir / 'check.mjs').exists())
                        self.assertIn('WORKBENCH_SCREENSHOT_PATH=/artifacts/screenshot.png', args)
                        (artifact_dir / 'screenshot.png').write_bytes(tiny_png())
                        paths.extend([driver_dir, artifact_dir])
                    if args[1] == 'start':
                        return subprocess.CompletedProcess(args, exit_code)
                    return self.fake(args, **kwargs)
                dest = self.workspace / f'regression-{exit_code}-{capture}.png'
                report = run_sandbox(self.workspace, 'example:latest', ['node', '/workbench-regression/check.mjs'],
                                     10, invoke=invoke, find_executable=lambda _: '/usr/bin/docker',
                                     workspace_read_only=True, regression_source=source,
                                     screenshot_output=dest, capture_failure_screenshot=capture)
                self.assertEqual(report['regression_sha256'], hashlib.sha256(source).hexdigest())
                self.assertEqual(report['exit_code'], exit_code)
                self.assertEqual(dest.exists(), exit_code == 0 or capture)
                if dest.exists():
                    self.assertEqual(report['screenshot_sha256'], hashlib.sha256(tiny_png()).hexdigest())
                self.assertTrue(report['cleanup_confirmed'])
                self.assertEqual(len(paths), 2)
                self.assertTrue(all(not path.exists() for path in paths))

    def test_native_regression_rejects_screenshot(self):
        path = 'src/agents/prompt.regression.test.ts'
        with self.assertRaisesRegex(SandboxError, 'invalid read-only focused regression'):
            run_sandbox(self.workspace, 'example:latest', ['node', '/app/node_modules/vitest/vitest.mjs',
                        'run', path], 10, invoke=self.fake, find_executable=lambda _: '/usr/bin/docker',
                        source_root='/app', workspace_read_only=True, regression_file=path,
                        screenshot_output=self.workspace / 'out.png')
        self.assertFalse(any(args[1] == 'create' for args, _ in self.calls))

    def test_native_focused_regression_mounts_only_the_baseline_test(self):
        path = 'src/agents/prompt.regression.test.ts'
        command = ['node', '/app/node_modules/vitest/vitest.mjs', 'run', path]
        test_source = b"it('keeps the prompt usable', () => {});\n"
        image = 'sha256:' + 'a' * 64
        for source, expected in ((test_source, 'isolated-input'), (None, 'verified-image')):
            with self.subTest(source=expected):
                observed = []

                def invoke(args, **kwargs):
                    if args[1:3] == ['image', 'inspect']:
                        return subprocess.CompletedProcess(args, 0, stdout=image + '\nnull\n', stderr='')
                    if args[1] == 'create':
                        mounts = [arg for arg in args if 'dst=/app/' in arg]
                        self.assertIn('--network=none', args)
                        if source is not None:
                            self.assertEqual(len(mounts), 1)
                            host = Path(mounts[0].split(',')[1].removeprefix('src='))
                            self.assertEqual(host.read_bytes(), source)
                            self.assertTrue(mounts[0].endswith(f'dst=/app/{path},readonly'))
                            observed.append(host)
                        else:
                            self.assertEqual(mounts, [])
                    return self.fake(args, **kwargs)

                report = run_sandbox(self.workspace, image, command, 10, invoke=invoke,
                                     find_executable=lambda _: '/usr/bin/docker', source_root='/app',
                                     workspace_read_only=True, regression_file=path, regression_source=source)
                self.assertEqual(report['regression_input'], expected)
                self.assertEqual(report['regression_file'], path)
                self.assertTrue(report['cleanup_confirmed'])
                self.assertFalse(any(host.exists() for host in observed))

    def test_image_source_root_uses_app_without_changing_workspace_mount(self):
        (self.workspace / 'packages').mkdir()
        report = self.run_it(source_root='/app', workdir='packages')
        args = next(args for args, _ in self.calls if args[1] == 'create')
        self.assertEqual(args[args.index('--workdir') + 1], '/app/packages')
        self.assertIn(f'type=bind,src={self.workspace.resolve()},dst=/workspace', args)
        self.assertIn('--network=none', args)
        self.assertIn('--read-only', args)
        self.assertIn('--memory=2048m', args)
        self.assertEqual(report['source_root'], '/app')
        self.assertEqual(report['container_workdir'], '/app/packages')
        self.assertEqual(report['workdir'], 'packages')

    def test_read_only_workspace_has_no_writable_bind(self):
        report = self.run_it(workspace_read_only=True)
        args = next(args for args, _ in self.calls if args[1] == "create")
        self.assertEqual(args.count("--mount"), 1)
        self.assertIn(f"type=bind,src={self.workspace.resolve()},dst=/workspace,readonly", args)
        self.assertTrue(report["workspace_read_only"])

    def test_other_source_roots_are_rejected_before_docker_operations(self):
        for root in ('', '/', '/tmp', '/app/../tmp', '/app/', Path('/app'), None, True):
            with self.subTest(root=root):
                self.calls.clear()
                with self.assertRaisesRegex(SandboxError, 'source root'):
                    self.run_it(source_root=root)
                self.assertEqual(self.calls, [])

    def test_creation_output_does_not_contaminate_execution_json_streams(self):
        payload = '{"success":true,"numPassedTests":4}\n'
        container_id = 'a' * 64 + '\n'
        def output(args, **kwargs):
            if args[1] == 'create':
                self.calls.append((args, kwargs))
                kwargs['stdout'].write(container_id.encode())
                kwargs['stderr'].write(b'Docker creation diagnostic\n')
                return subprocess.CompletedProcess(args, 0)
            if args[1] == 'start':
                self.calls.append((args, kwargs))
                kwargs['stdout'].write(payload.encode())
                kwargs['stderr'].write(b'application diagnostic\n')
                return subprocess.CompletedProcess(args, 0)
            return self.fake(args, **kwargs)
        report = self.run_it(output)
        self.assertEqual(report['stdout'], payload)
        self.assertEqual(json.loads(report['stdout'])['numPassedTests'], 4)
        self.assertEqual(report['stderr'], 'application diagnostic\n')
        self.assertEqual(report['create_stdout'], container_id)
        self.assertEqual(report['create_stderr'], 'Docker creation diagnostic\n')
        self.assertEqual(report['create_exit_code'], 0)
        self.assertFalse(report['create_stdout_truncated'])
        self.assertFalse(report['create_stderr_truncated'])
        self.assertTrue(report['cleanup_confirmed'])

    def test_output_hashes_distinguish_bytes_with_the_same_decoded_output(self):
        reports = []
        for value in (b'\xff', b'\xfe'):
            def invoke(args, **kwargs):
                if args[1] == 'start':
                    kwargs['stdout'].write(value + b'\n')
                    kwargs['stderr'].write(b'error: ' + value)
                    return subprocess.CompletedProcess(args, 2)
                return self.fake(args, **kwargs)

            report = self.run_it(invoke)
            self.assertEqual(report['exit_code'], 2)
            self.assertEqual(report['stdout_sha256'], hashlib.sha256(value + b'\n').hexdigest())
            self.assertEqual(report['stderr_sha256'], hashlib.sha256(b'error: ' + value).hexdigest())
            reports.append(report)
        for stream in ('stdout', 'stderr'):
            self.assertEqual(reports[0][stream], reports[1][stream])
            self.assertNotEqual(reports[0][stream + '_sha256'], reports[1][stream + '_sha256'])

    def test_only_the_truncated_stream_loses_its_output_hash(self):
        complete = b'x' * MAX_OUTPUT
        for truncated, bounded in (('stdout', 'stderr'), ('stderr', 'stdout')):
            with self.subTest(truncated=truncated):
                def invoke(args, **kwargs):
                    if args[1] == 'start':
                        kwargs[truncated].write(complete + b'!')
                        kwargs[bounded].write(complete)
                        return subprocess.CompletedProcess(args, 0)
                    return self.fake(args, **kwargs)

                report = self.run_it(invoke)
                self.assertEqual(report[truncated], 'x' * MAX_OUTPUT)
                self.assertTrue(report[truncated + '_truncated'])
                self.assertNotIn(truncated + '_sha256', report)
                self.assertFalse(report[bounded + '_truncated'])
                self.assertEqual(report[bounded + '_sha256'], hashlib.sha256(complete).hexdigest())

    def test_creation_failure_retains_bounded_diagnostics_without_starting(self):
        diagnostic = b'x' * (MAX_CREATE_OUTPUT + 1)
        def failed_create(args, **kwargs):
            if args[1] == 'create':
                self.calls.append((args, kwargs))
                kwargs['stdout'].write(diagnostic)
                kwargs['stderr'].write(diagnostic)
                return subprocess.CompletedProcess(args, 125)
            return self.fake(args, **kwargs)
        report = self.run_it(failed_create)
        self.assertEqual(report['error'], 'container creation failed')
        self.assertEqual(report['exit_code'], 125)
        self.assertEqual(report['create_exit_code'], 125)
        self.assertEqual(report['stdout'], '')
        self.assertEqual(report['stderr'], '')
        self.assertNotIn('stdout_sha256', report)
        self.assertNotIn('stderr_sha256', report)
        for stream in ('create_stdout', 'create_stderr'):
            self.assertEqual(report[stream], 'x' * MAX_CREATE_OUTPUT)
            self.assertTrue(report[stream + '_truncated'])
        self.assertFalse(report['container_created'])
        self.assertTrue(report['cleanup_confirmed'])
        self.assertFalse(any(args[1] == 'start' for args, _ in self.calls))

    def test_creation_timeout_retains_partial_diagnostic_and_attempts_cleanup(self):
        def timed_out_create(args, **kwargs):
            if args[1] == 'create':
                self.calls.append((args, kwargs))
                kwargs['stderr'].write(b'partial Docker diagnostic\n')
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return self.fake(args, **kwargs)
        report = self.run_it(timed_out_create)
        self.assertTrue(report['timed_out'])
        self.assertIn('timed out', report['error'])
        self.assertIsNone(report['create_exit_code'])
        self.assertEqual(report['create_stderr'], 'partial Docker diagnostic\n')
        self.assertFalse(report['create_stderr_truncated'])
        self.assertEqual(report['stderr'], '')
        self.assertFalse(report['container_created'])
        self.assertTrue(report['cleanup_confirmed'])
        self.assertFalse(any(args[1] == 'start' for args, _ in self.calls))

    def test_explicit_seven_gib_and_upper_bound_preserve_container_controls(self):
        for memory in (7168, 8192):
            with self.subTest(memory=memory):
                self.calls.clear()
                report = self.run_it(memory_mb=memory)
                args = next(args for args, _ in self.calls if args[1] == 'create')
                self.assertIn(f'--memory={memory}m', args)
                self.assertIn('--network=none', args)
                self.assertIn('--pids-limit=256', args)
                self.assertEqual(report['memory_mb'], memory)

    def test_invalid_memory_is_rejected_before_any_docker_operation(self):
        for memory in (True, False, 127, 8193, 7168.0, '7168', None):
            with self.subTest(memory=memory):
                self.calls.clear()
                with self.assertRaises(SandboxError):
                    self.run_it(memory_mb=memory)
                self.assertEqual(self.calls, [])

    def test_timeout_attempts_to_force_remove_container(self):
        def timeout(args, **kwargs):
            if args[1] == "start":
                self.calls.append((args, kwargs))
                kwargs['stdout'].write(b'partial output\n')
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return self.fake(args, **kwargs)
        report = self.run_it(timeout)
        self.assertTrue(report["timed_out"])
        self.assertEqual(report['stdout'], 'partial output\n')
        self.assertFalse(report['stdout_truncated'])
        self.assertNotIn('stdout_sha256', report)
        self.assertNotIn('stderr_sha256', report)
        self.assertEqual(report['runtime_diagnostics']['classification'], 'command-timeout')
        self.assertEqual(report['runtime_diagnostics']['unavailable'], ['container-id', 'state', 'oom-events'])
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(self.calls[-1][0][1:3], ["rm", "--force"])

    def test_docker_event_proves_child_oom_despite_outer_exit_two(self):
        container = 'a' * 64
        def invoke(args, **kwargs):
            if args[1] == 'create':
                kwargs['stdout'].write((container + '\n').encode())
            elif args[1] == 'start':
                return subprocess.CompletedProcess(args, 2)
            elif args[1:3] == ['container', 'inspect']:
                self.assertEqual(args[-1], container)
                kwargs['stdout'].write(b'{"Status":"exited","ExitCode":2,"OOMKilled":false,"Pid":0}')
            elif args[1] == 'events':
                self.assertIn(f'container={container}', args)
                self.assertIn('event=oom', args)
                self.assertIn('type=container', args)
                self.assertLessEqual(kwargs['timeout'], 3)
                time_ns = _time_ns(args[args.index('--until') + 1])
                event = {'Type': 'container', 'Action': 'oom', 'Actor': {'ID': container,
                                                                        'Attributes': {'unneeded': 'secret'}},
                         'timeNano': time_ns}
                kwargs['stdout'].write(json.dumps(event).encode() + b'\n')
            return self.fake(args, **kwargs)

        report = self.run_it(invoke)
        self.assertEqual(report['exit_code'], 2)
        diagnostics = report['runtime_diagnostics']
        self.assertEqual(diagnostics['classification'], 'docker-oom')
        self.assertEqual(diagnostics['container_id'], container)
        self.assertEqual(diagnostics['state'], {'status': 'exited', 'exit_code': 2, 'oom_killed': False})
        self.assertEqual(len(diagnostics['oom_events']), 1)
        self.assertEqual(set(diagnostics['oom_events'][0]), {'time_ns'})
        self.assertEqual(diagnostics['unavailable'], [])
        operations = [args[1] for args, _ in self.calls]
        self.assertEqual(operations[-3:], ['container', 'events', 'rm'])
        self.assertTrue(report['cleanup_confirmed'])

    def test_exit_137_needs_docker_evidence_and_invalid_events_are_unavailable(self):
        container = 'a' * 64
        for event_kind in ('none', 'malformed', 'wrong-container', 'outside-window', 'collection-timeout'):
            with self.subTest(event_kind=event_kind):
                self.calls.clear()

                def invoke(args, **kwargs):
                    if args[1] == 'create':
                        kwargs['stdout'].write(container.encode())
                    elif args[1] == 'start':
                        return subprocess.CompletedProcess(args, 137)
                    elif args[1:3] == ['container', 'inspect']:
                        kwargs['stdout'].write(b'{"Status":"exited","ExitCode":137,"OOMKilled":false}')
                    elif args[1] == 'events':
                        if event_kind == 'collection-timeout':
                            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                        if event_kind == 'malformed':
                            kwargs['stdout'].write(b'bad JSON\n')
                        if event_kind in ('wrong-container', 'outside-window'):
                            time_ns = _time_ns(args[args.index('--until') + 1])
                            event = {'Type': 'container', 'Action': 'oom',
                                     'Actor': {'ID': 'b' * 64 if event_kind == 'wrong-container' else container},
                                     'timeNano': time_ns if event_kind == 'wrong-container' else 1}
                            kwargs['stdout'].write(json.dumps(event).encode() + b'\n')
                    return self.fake(args, **kwargs)

                report = self.run_it(invoke)
                diagnostics = report['runtime_diagnostics']
                self.assertEqual(diagnostics['classification'], 'command-failed')
                self.assertEqual(diagnostics['oom_events'], [] if event_kind == 'none' else None)
                self.assertEqual(diagnostics['unavailable'], [] if event_kind == 'none' else ['oom-events'])
                self.assertTrue(report['cleanup_confirmed'])
                self.assertEqual(self.calls[-1][0][1:3], ['rm', '--force'])

    def test_container_inspect_timeout_does_not_prevent_cleanup(self):
        container = 'a' * 64
        def invoke(args, **kwargs):
            if args[1] == 'create':
                kwargs['stdout'].write(container.encode())
            elif args[1] in ('start', 'container'):
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return self.fake(args, **kwargs)

        report = self.run_it(invoke)
        diagnostics = report['runtime_diagnostics']
        self.assertEqual(diagnostics['classification'], 'command-timeout')
        self.assertEqual(diagnostics['container_id'], container)
        self.assertIsNone(diagnostics['state'])
        self.assertEqual(diagnostics['unavailable'], ['state'])
        self.assertTrue(report['cleanup_confirmed'])
        self.assertEqual(self.calls[-1][0][1:3], ['rm', '--force'])

    def test_scoped_profile_copies_only_the_fixed_artifact_to_an_owner_only_file(self):
        pinned = 'sha256:' + 'a' * 64
        payload = b'{"profile":true}\n'
        for mode, suffix, filename in (('cpu', '.cpuprofile', 'cpu.cpuprofile'),
                                       ('heap', '.heapsnapshot', 'heap.heapsnapshot'),
                                       ('heap-allocations', '.heapsnapshot', 'heap.heapsnapshot')):
            with self.subTest(mode=mode):
                destination = self.workspace / (mode + suffix)
                ephemeral = []

                def invoke(args, **kwargs):
                    if args[1:3] == ['image', 'inspect']:
                        return subprocess.CompletedProcess(args, 0, stdout=pinned + '\nnull\n', stderr='')
                    if args[1] == 'create':
                        bind = next(arg for arg in args if arg.endswith('dst=/artifacts'))
                        directory = Path(bind.split(',')[1].removeprefix('src='))
                        ephemeral.append(directory)
                        (directory / filename).write_bytes(payload)
                        self.assertNotEqual(directory, destination.parent)
                        self.assertIn('--network=none', args)
                        self.assertIn(f'type=bind,src={self.workspace.resolve()},dst=/workspace,readonly', args)
                        if mode == 'heap-allocations':
                            self.assertEqual(args[args.index('--ulimit') + 1], 'fsize=67108864:67108864')
                        else:
                            self.assertNotIn('--ulimit', args)
                    return self.fake(args, **kwargs)

                report = run_sandbox(self.workspace, 'example:latest', ['node', '/workspace/node-profile.mjs', mode],
                                     10, invoke=invoke, find_executable=lambda _: '/usr/bin/docker',
                                     source_root='/app', workspace_read_only=True, profile_output=destination)
                self.assertEqual(report['image'], pinned)
                self.assertEqual(report['profile_path'], str(destination.resolve()))
                self.assertEqual(report.get('profile_file_limit_bytes'), 64 << 20 if mode == 'heap-allocations' else None)
                self.assertRegex(report['profile_sha256'], r'^[0-9a-f]{64}$')
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                self.assertTrue(all(not path.exists() for path in ephemeral))
                self.assertTrue(report['cleanup_confirmed'])

    def test_profile_input_must_use_scoped_read_only_helper_without_other_artifacts(self):
        for command, changed in (
            (['node', '/app/other.mjs'], {}),
            (['node', '/workspace/node-profile.mjs'], {'workspace_read_only': False}),
            (['node', '/workspace/node-profile.mjs'], {'source_root': '/workspace'}),
            (['node', '/workspace/node-profile.mjs'], {'screenshot_output': self.workspace / 'target.png'}),
            (['node', '/workspace/node-profile.mjs'], {'regression_source': b'console.log(1)'}),
            (['node', '/workspace/node-profile.mjs'], {'profile_output': self.workspace / 'target.json'}),
            (['node', '/workspace/node-profile.mjs', 'heap-allocations'], {}),
        ):
            with self.subTest(command=command, changed=changed):
                options = {'workspace_read_only': True, 'source_root': '/app',
                           'profile_output': self.workspace / 'target.cpuprofile'}
                with self.assertRaisesRegex(SandboxError, 'scoped profile input'):
                    run_sandbox(self.workspace, 'example', command, 10, invoke=self.fake,
                                find_executable=lambda _: '/usr/bin/docker', **{**options, **changed})
        self.assertEqual(self.calls, [])

    def test_gateway_and_browser_can_share_a_bounded_process_budget(self):
        report = self.run_it(pids_limit=384)
        args = next(command for command, _ in self.calls if command[1] == 'create')
        self.assertIn('--pids-limit=384', args)
        self.assertIn('--network=none', args)
        self.assertEqual(report['pids_limit'], 384)

    def test_invalid_process_limit_is_rejected_before_docker_operations(self):
        for limit in (0, 63, 513, True, '384'):
            with self.subTest(limit=limit), self.assertRaisesRegex(SandboxError, 'PID limit'):
                self.run_it(pids_limit=limit)
        self.assertEqual(self.calls, [])

    def test_scoped_profile_rejects_symlink_and_existing_destination(self):
        pinned = 'sha256:' + 'a' * 64
        destination = self.workspace / 'saved.cpuprofile'
        destination.write_bytes(b'original')
        for symlink in (True, False):
            with self.subTest(symlink=symlink):
                def invoke(args, **kwargs):
                    if args[1:3] == ['image', 'inspect']:
                        return subprocess.CompletedProcess(args, 0, stdout=pinned + '\nnull\n', stderr='')
                    if args[1] == 'create':
                        bind = next(arg for arg in args if arg.endswith('dst=/artifacts'))
                        source = Path(bind.split(',')[1].removeprefix('src=')) / 'cpu.cpuprofile'
                        if symlink:
                            source.symlink_to(destination)
                        else:
                            source.write_bytes(b'replacement')
                    return self.fake(args, **kwargs)

                report = run_sandbox(self.workspace, 'example:latest', ['node', '/workspace/node-profile.mjs'],
                                     10, invoke=invoke, find_executable=lambda _: '/usr/bin/docker',
                                     source_root='/app', workspace_read_only=True, profile_output=destination)
                self.assertIn('cannot collect runtime profile', report['error'])
                self.assertNotIn('profile_sha256', report)
                self.assertEqual(destination.read_bytes(), b'original')
                self.assertTrue(report['cleanup_confirmed'])

    def test_cleanup_failure_is_reported_not_success(self):
        def failed_cleanup(args, **kwargs):
            if args[1] == "rm":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="daemon unavailable")
            return self.fake(args, **kwargs)
        report = self.run_it(failed_cleanup)
        self.assertFalse(report["cleanup_confirmed"])
        self.assertIn("daemon unavailable", report["cleanup_error"])

    def test_workdir_cannot_escape_or_follow_symlink_outside(self):
        (self.workspace / "escape").symlink_to(self.workspace.parent, target_is_directory=True)
        for root in ('/workspace', '/app'):
            for workdir in ('../', '/tmp', 'escape'):
                with self.subTest(root=root, workdir=workdir), self.assertRaises(SandboxError):
                    self.run_it(source_root=root, workdir=workdir)
        self.assertEqual(self.calls, [])

    def test_image_volume_is_rejected_before_execution(self):
        def with_volume(args, **kwargs):
            if args[1] == "context":
                return self.fake(args, **kwargs)
            self.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"/data": {}}), stderr="")
        with self.assertRaisesRegex(SandboxError, "extra volumes"):
            self.run_it(with_volume)
        self.assertFalse(any(args[1] == "create" for args, _ in self.calls))

    def test_missing_image_is_not_pulled(self):
        def absent(args, **kwargs):
            if args[1] == "context":
                return self.fake(args, **kwargs)
            self.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
        with self.assertRaisesRegex(SandboxError, "no image was pulled"):
            self.run_it(absent)
        self.assertFalse(any(args[1] == "create" for args, _ in self.calls))

    def test_screenshot_rejects_symlink_and_corrupt_png(self):
        screenshot = self.workspace / 'screenshot.png'
        screenshot.symlink_to(self.workspace.parent / 'outside')
        with self.assertRaises(SandboxError):
            _copy_screenshot(self.workspace, self.workspace / 'output.png')
        screenshot.unlink()
        os.mkfifo(screenshot)
        with self.assertRaisesRegex(SandboxError, 'regular file'):
            _copy_screenshot(self.workspace, self.workspace / 'output.png')
        screenshot.unlink()
        data = bytearray(tiny_png()); data[-1] ^= 1
        with self.assertRaisesRegex(SandboxError, 'checksum'):
            _validated_png(bytes(data))
        screenshot.write_bytes(tiny_png())
        digest = _copy_screenshot(self.workspace, self.workspace / 'output.png')
        self.assertEqual(len(digest), 64)
        self.assertEqual((self.workspace / 'output.png').read_bytes(), tiny_png())

    def test_declared_failure_can_retain_a_screenshot_without_turning_it_into_a_pass(self):
        def invoke(args, **kwargs):
            if args[1] == 'create':
                mount = next(arg for arg in args if arg.endswith('dst=/artifacts'))
                (Path(mount.split(',')[1].removeprefix('src=')) / 'screenshot.png').write_bytes(tiny_png())
            if args[1] == 'start':
                return subprocess.CompletedProcess(args, 1)
            return self.fake(args, **kwargs)

        for capture in (False, True):
            destination = self.workspace / f'failure-{capture}.png'
            report = self.run_it(invoke, screenshot_output=destination, capture_failure_screenshot=capture)
            self.assertTrue(report['cleanup_confirmed'])
            self.assertEqual(report['exit_code'], 1)
            self.assertEqual(destination.is_file(), capture)
            if capture:
                self.assertEqual(report['screenshot_sha256'], hashlib.sha256(tiny_png()).hexdigest())

    def recording_run(self, screenshot, recording, write_recording, *, exit_code=0, capture_failure=False,
                      screenshot_contents=None):
        def invoke(args, **kwargs):
            if args[1] == 'create':
                self.assertIn('--network=none', args)
                self.assertIn('--read-only', args)
                self.assertIn('WORKBENCH_RECORDING_PATH=/artifacts/recording.jsonl', args)
                self.assertIn('WORKBENCH_SCREENSHOT_PATH=/artifacts/screenshot.png', args)
                self.assertEqual(args.count('--mount'), 2)
                mount = next(arg for arg in args if arg.endswith(',dst=/artifacts'))
                artifacts = Path(mount.split(',')[1].removeprefix('src='))
                (artifacts / 'screenshot.png').write_bytes(
                    tiny_png() if screenshot_contents is None else screenshot_contents)
                write_recording(artifacts / 'recording.jsonl')
            if args[1] == 'start' and exit_code:
                return subprocess.CompletedProcess(args, exit_code)
            return self.fake(args, **kwargs)

        return self.run_it(invoke, source_root='/app', workspace_read_only=True,
                           screenshot_output=screenshot, recording_output=recording,
                           capture_failure_screenshot=capture_failure)

    def test_recording_is_copied_and_bound_to_the_screenshot_on_allowed_exits(self):
        frames = b'{"frame":1}\n'
        for exit_code, capture, expected in ((0, False, True), (1, True, True),
                                             (1, False, False), (2, True, False)):
            with self.subTest(exit_code=exit_code, capture_failure=capture):
                screenshot = self.workspace / f'{exit_code}-{capture}.png'
                recording = self.workspace / f'{exit_code}-{capture}.jsonl'
                report = self.recording_run(screenshot, recording, lambda path: path.write_bytes(frames),
                                            exit_code=exit_code, capture_failure=capture)
                self.assertEqual(report['exit_code'], exit_code)
                self.assertTrue(report['cleanup_confirmed'])
                self.assertEqual(recording.is_file(), expected)
                self.assertEqual(screenshot.is_file(), expected)
                if expected:
                    self.assertEqual(report['screenshot_path'], str(screenshot.resolve()))
                    self.assertEqual(recording.read_bytes(), frames)
                    self.assertEqual(report['recording_path'], str(recording.resolve()))
                    self.assertEqual(report['recording_sha256'], hashlib.sha256(frames).hexdigest())
                else:
                    self.assertNotIn('recording_sha256', report)

    def test_invalid_recording_cannot_produce_a_recording_receipt(self):
        def oversized(path):
            with path.open('wb') as stream:
                stream.truncate(MAX_RECORDING + 1)

        for name, writer, png in (
                ('absent', lambda path: None, tiny_png()),
                ('oversized', oversized, tiny_png()),
                ('source-link', lambda path: path.symlink_to(self.workspace), tiny_png()),
                ('bad-png', lambda path: path.write_bytes(b'{"frame":1}\n'), b'invalid')):
            with self.subTest(name=name):
                recording = self.workspace / f'{name}.jsonl'
                report = self.recording_run(self.workspace / f'{name}.png', recording, writer,
                                            screenshot_contents=png)
                self.assertTrue(report['cleanup_confirmed'])
                self.assertIn('error', report)
                self.assertNotIn('recording_sha256', report)
                self.assertFalse(recording.exists())

    def test_recording_cannot_overwrite_an_existing_host_artifact(self):
        recording = self.workspace / 'existing.jsonl'
        prior_bytes = b'{"existing":true}\n'
        recording.write_bytes(prior_bytes)
        report = self.recording_run(self.workspace / 'new.png', recording,
                                    lambda path: path.write_bytes(b'{"recorded":true}\n'))
        self.assertTrue(report['cleanup_confirmed'])
        self.assertIn('cannot collect recording', report['error'])
        self.assertNotIn('recording_sha256', report)
        self.assertEqual(recording.read_bytes(), prior_bytes)

    def test_recording_cannot_use_the_screenshot_path_or_an_unscoped_run(self):
        png = self.workspace / 'screenshot.png'
        jsonl = self.workspace / 'recording.jsonl'
        allowed = {'source_root': '/app', 'workspace_read_only': True,
                   'screenshot_output': png, 'recording_output': jsonl}
        for override in ({'source_root': '/workspace'}, {'workspace_read_only': False},
                         {'screenshot_output': None}, {'recording_output': png},
                         {'screenshot_output': jsonl}, {'profile_output': self.workspace / 'cpu.cpuprofile'},
                         {'statechart_output': self.workspace / 'statechart'}, {'regression_source': b'fixture'}):
            with self.subTest(fields=override):
                with self.assertRaisesRegex(SandboxError, 'recording requires'):
                    self.run_it(**{**allowed, **override})
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
