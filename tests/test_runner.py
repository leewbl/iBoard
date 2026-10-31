import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from runner.sandbox import SandboxError, _copy_screenshot, _validated_png, run_sandbox


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
        self.assertNotIn("--env", args)
        self.assertIn("--entrypoint", args)
        self.assertEqual(report["stdout"], "hello\n")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(self.calls[-1][0][1:3], ["rm", "--force"])

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
        self.assertTrue(report["cleanup_confirmed"])
        self.assertEqual(self.calls[-1][0][1:3], ["rm", "--force"])

    def test_cleanup_failure_is_reported_not_success(self):
        def failed_cleanup(args, **kwargs):
            if args[1] == "rm":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="daemon unavailable")
            return self.fake(args, **kwargs)
        report = self.run_it(failed_cleanup)
        self.assertFalse(report["cleanup_confirmed"])
        self.assertIn("daemon unavailable", report["cleanup_error"])

    def test_workdir_cannot_escape_or_follow_symlink_outside(self):
        with self.assertRaises(SandboxError):
            self.run_it(workdir="../")
        (self.workspace / "escape").symlink_to(self.workspace.parent, target_is_directory=True)
        with self.assertRaises(SandboxError):
            self.run_it(workdir="escape")
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
