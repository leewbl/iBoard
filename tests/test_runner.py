import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from runner.sandbox import MAX_CREATE_OUTPUT, SandboxError, _copy_screenshot, _time_ns, _validated_png, run_sandbox


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
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return self.fake(args, **kwargs)
        report = self.run_it(timeout)
        self.assertTrue(report["timed_out"])
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
        for suffix, filename in (('.cpuprofile', 'cpu.cpuprofile'), ('.heapsnapshot', 'heap.heapsnapshot')):
            with self.subTest(suffix=suffix):
                destination = self.workspace / ('saved' + suffix)
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
                    return self.fake(args, **kwargs)

                report = run_sandbox(self.workspace, 'example:latest', ['node', '/workspace/node-profile.mjs'],
                                     10, invoke=invoke, find_executable=lambda _: '/usr/bin/docker',
                                     source_root='/app', workspace_read_only=True, profile_output=destination)
                self.assertEqual(report['image'], pinned)
                self.assertEqual(report['profile_path'], str(destination.resolve()))
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
        ):
            with self.subTest(command=command, changed=changed):
                options = {'workspace_read_only': True, 'source_root': '/app',
                           'profile_output': self.workspace / 'target.cpuprofile'}
                with self.assertRaisesRegex(SandboxError, 'scoped profile input'):
                    run_sandbox(self.workspace, 'example', command, 10, invoke=self.fake,
                                find_executable=lambda _: '/usr/bin/docker', **{**options, **changed})
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


if __name__ == "__main__":
    unittest.main()
