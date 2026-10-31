"""Run one command inside a pre-existing Docker image, never on the host."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

MAX_OUTPUT = 1_000_000
MAX_SCREENSHOT = 8_000_000


class SandboxError(Exception):
    pass


def _read_limited(stream):
    stream.seek(0)
    content = stream.read(MAX_OUTPUT + 1)
    truncated = len(content) > MAX_OUTPUT
    return content[:MAX_OUTPUT].decode("utf-8", errors="replace"), truncated


def _validated_png(data):
    if len(data) > MAX_SCREENSHOT or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise SandboxError('screenshot is not a bounded PNG')
    pos, seen_ihdr, seen_idat, seen_end = 8, False, False, False
    while pos + 12 <= len(data):
        length = struct.unpack('>I', data[pos:pos+4])[0]
        end = pos + 12 + length
        if end > len(data):
            raise SandboxError('screenshot PNG is truncated')
        kind, content = data[pos+4:pos+8], data[pos+8:pos+8+length]
        expected = struct.unpack('>I', data[pos+8+length:end])[0]
        if zlib.crc32(kind + content) & 0xffffffff != expected:
            raise SandboxError('screenshot PNG checksum is invalid')
        if not seen_ihdr:
            if kind != b'IHDR' or length != 13:
                raise SandboxError('screenshot PNG has no valid header')
            width, height = struct.unpack('>II', content[:8])
            if not (1 <= width <= 10000 and 1 <= height <= 10000 and width * height <= 30_000_000):
                raise SandboxError('screenshot dimensions exceed limits')
            seen_ihdr = True
        if kind == b'IDAT':
            seen_idat = True
        if kind == b'IEND':
            seen_end = length == 0 and end == len(data)
            break
        pos = end
    if not seen_idat or not seen_end:
        raise SandboxError('screenshot PNG is incomplete')


def _copy_screenshot(directory, output):
    source = Path(directory) / 'screenshot.png'
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SandboxError('screenshot must be a regular file')
            data = stream.read(MAX_SCREENSHOT + 1)
        _validated_png(data)
        out = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, 'wb') as stream:
                stream.write(data)
        except BaseException:
            Path(output).unlink(missing_ok=True)
            raise
        return hashlib.sha256(data).hexdigest()
    except OSError as error:
        raise SandboxError(f'cannot collect screenshot: {type(error).__name__}') from error


def run_sandbox(workspace, image, command, timeout, workdir=".", *, memory_mb=2048, invoke=subprocess.run,
                find_executable=shutil.which, screenshot_output=None):
    """Return an execution report. The chosen workspace must contain test data only.

    ``invoke`` and ``find_executable`` permit unit testing without a Docker daemon.
    """
    docker = find_executable("docker")
    if not docker:
        raise SandboxError("Docker is unavailable; no host command was executed")
    source = Path(workspace).expanduser().resolve(strict=True)
    if not source.is_dir() or "," in str(source):
        raise SandboxError("workspace must be an existing directory without commas")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*", image):
        raise SandboxError("invalid Docker image reference")
    if not command or not command[0] or command[0].startswith("-"):
        raise SandboxError("a command with a valid executable is required")
    if not 0 < timeout <= 86400:
        raise SandboxError("timeout must be in (0, 86400] seconds")
    if not isinstance(memory_mb, int) or not 128 <= memory_mb <= 6144:
        raise SandboxError("memory limit must be between 128 and 6144 MiB")
    relative = PurePosixPath(workdir)
    if relative.is_absolute() or ".." in relative.parts:
        raise SandboxError("workdir must be relative to the mounted workspace")
    host_workdir = (source / str(relative)).resolve(strict=True)
    if not host_workdir.is_relative_to(source) or not host_workdir.is_dir():
        raise SandboxError("workdir resolves outside the workspace or is not a directory")
    configured_host = os.environ.get("DOCKER_HOST", "")
    if configured_host and not configured_host.startswith("unix://"):
        raise SandboxError("remote Docker hosts are not supported")

    if not configured_host:
        try:
            context = invoke([docker, "context", "inspect", "--format",
                              '{{(index .Endpoints "docker").Host}}'], capture_output=True,
                             text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"cannot inspect Docker context: {exc}") from exc
        if context.returncode or not context.stdout.strip().startswith("unix://"):
            raise SandboxError("only local Unix-socket Docker contexts are supported")

    # An image's VOLUME directive could add unrequested mounts. Reject such images.
    try:
        inspected = invoke([docker, "image", "inspect", image, "--format", "{{json .Config.Volumes}}"],
                           capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"cannot inspect local Docker image: {exc}") from exc
    if inspected.returncode:
        raise SandboxError("Docker daemon unavailable or image not present locally; no image was pulled")
    try:
        volumes = json.loads(inspected.stdout.strip())
    except (json.JSONDecodeError, AttributeError) as exc:
        raise SandboxError("cannot establish image volume configuration") from exc
    if volumes:
        raise SandboxError("image declares extra volumes; use an image without VOLUME directives")

    name = "workbench-" + uuid.uuid4().hex
    container_dir = "/workspace" if str(relative) == "." else "/workspace/" + str(relative)
    create = [docker, "create", "--name", name, "--pull=never", "--network=none",
              "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
              "--pids-limit=256", f"--memory={memory_mb}m", "--cpus=2", "--init",
              "--user", f"{os.getuid()}:{os.getgid()}",
              "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
              "--mount", f"type=bind,src={source},dst=/workspace",
              "--workdir", container_dir, "--entrypoint", command[0], image, *command[1:]]
    started = time.monotonic()
    report = {"workspace": str(source), "image": image, "command": command,
              "workdir": str(relative), "timeout_seconds": timeout, "memory_mb": memory_mb,
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "exit_code": None, "timed_out": False, "cleanup_confirmed": False}
    created = False
    with tempfile.TemporaryDirectory(prefix='workbench-artifact-') as artifact_dir, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            if screenshot_output is not None:
                create[create.index('--workdir'):create.index('--workdir')] = [
                    '--mount', f'type=bind,src={artifact_dir},dst=/artifacts',
                    '--env', 'WORKBENCH_SCREENSHOT_PATH=/artifacts/screenshot.png']
            result = invoke(create, stdout=stdout, stderr=stderr, timeout=timeout, check=False)
            if result.returncode:
                report["error"] = "container creation failed"
                report["exit_code"] = result.returncode
            else:
                created = True
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(create, timeout)
                result = invoke([docker, "start", "--attach", name], stdout=stdout, stderr=stderr,
                                timeout=remaining, check=False)
                report["exit_code"] = result.returncode
                if screenshot_output is not None and result.returncode == 0:
                    report['screenshot_sha256'] = _copy_screenshot(artifact_dir, screenshot_output)
                    report['screenshot_path'] = str(Path(screenshot_output).resolve())
        except subprocess.TimeoutExpired:
            report["timed_out"] = True
            report["error"] = "execution timed out"
        except OSError as exc:
            report["error"] = f"Docker command failed: {exc}"
        except SandboxError as exc:
            report['error'] = str(exc)
        finally:
            # A timed-out start may leave the container running after its CLI exits.
            try:
                cleanup = invoke([docker, "rm", "--force", name], capture_output=True,
                                 text=True, timeout=15, check=False)
                report["cleanup_confirmed"] = cleanup.returncode == 0
                if not report["cleanup_confirmed"]:
                    report["cleanup_error"] = cleanup.stderr.strip()[:2000]
            except (OSError, subprocess.TimeoutExpired) as exc:
                report["cleanup_error"] = str(exc)
            report["stdout"], report["stdout_truncated"] = _read_limited(stdout)
            report["stderr"], report["stderr_truncated"] = _read_limited(stderr)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["container_created"] = created
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--image", required=True, help="already installed image; no implicit pull")
    parser.add_argument("--timeout", required=True, type=float, help="maximum execution seconds")
    parser.add_argument("--memory-mb", type=int, default=2048, help="container memory limit in MiB")
    parser.add_argument("--workdir", default=".", help="relative to workspace")
    parser.add_argument("--report", required=True, help="output JSON path on the host")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        report = run_sandbox(args.workspace, args.image, command, args.timeout, args.workdir, memory_mb=args.memory_mb)
    except (SandboxError, OSError, ValueError) as exc:
        report = {"error": str(exc), "exit_code": None, "timed_out": False,
                  "cleanup_confirmed": False, "container_created": False}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("error") or report.get("exit_code") != 0 or not report.get("cleanup_confirmed"):
        print(f"sandbox command failed; see {report_path}", file=sys.stderr)
        return 1
    print(f"sandbox command completed; see {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
