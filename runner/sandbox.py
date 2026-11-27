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

TMPFS_MB = 1024
MAX_OUTPUT = 1_000_000
MAX_CREATE_OUTPUT = 65_536
MAX_SCREENSHOT = 8_000_000
MAX_RECORDING = 24_000_000
MAX_DIAGNOSTIC_OUTPUT = 65_536
DIAGNOSTIC_TIMEOUT = 3
PROFILE_ARTIFACTS = {
    '.cpuprofile': ('cpu.cpuprofile', 16 * 1024 * 1024),
    '.heapsnapshot': ('heap.heapsnapshot', 64 * 1024 * 1024),
    '.cdptrace': ('trace.cdptrace', 32 * 1024 * 1024),
}
BROWSER_MAPS_ARTIFACT = 'browser-source-maps.json'
BROWSER_MAPS_LIMIT = 16 * 1024 * 1024
STATECHART_FILE_LIMIT = 16 * 1024 * 1024
STATECHART_TOTAL_LIMIT = 64 * 1024 * 1024
STATECHART_REQUIRED = {'report.md', 'runtime.json', 'tlc.out'}
STATECHART_OPTIONAL = {'chart.mmd', 'chart.svg', 'render-error.txt'}


class SandboxError(Exception):
    pass


def _read_limited_bytes(stream, limit):
    stream.seek(0)
    content = stream.read(limit + 1)
    return content[:limit], len(content) > limit


def _read_limited(stream, limit=MAX_OUTPUT):
    content, truncated = _read_limited_bytes(stream, limit)
    return content.decode("utf-8", errors="replace"), truncated


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


def _copy_artifact(directory, name, output, limit, description, *, validate=None):
    source = Path(directory) / name
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SandboxError(f'{description} must be a regular file')
            if validate is None and not 0 < metadata.st_size <= limit:
                raise SandboxError(f'{description} must be nonempty and at most {limit} bytes')
            data = stream.read(limit + 1)
        if validate is not None:
            validate(data)
        elif not data or len(data) > limit:
            raise SandboxError(f'{description} must be nonempty and at most {limit} bytes')
        out = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, 'wb') as stream:
                stream.write(data)
        except BaseException:
            Path(output).unlink(missing_ok=True)
            raise
        return hashlib.sha256(data).hexdigest()
    except OSError as error:
        raise SandboxError(f'cannot collect {description}: {type(error).__name__}') from error


def _copy_screenshot(directory, output):
    return _copy_artifact(directory, 'screenshot.png', output, MAX_SCREENSHOT, 'screenshot',
                          validate=_validated_png)


def _copy_statechart(directory, output):
    files = sorted(Path(directory).iterdir())
    names = {file.name for file in files}
    if (len(files) > 128 or not STATECHART_REQUIRED <= names
            or any(name not in STATECHART_REQUIRED | STATECHART_OPTIONAL
                   and not re.fullmatch(r'tlc-[A-Za-z0-9_]{1,150}\.out', name) for name in names)):
        raise SandboxError('statechart output did not match the expected artifact names')
    if sum(file.lstat().st_size for file in files) > STATECHART_TOTAL_LIMIT:
        raise SandboxError('statechart artifacts exceeded the combined size limit')
    target = Path(output)
    target.mkdir(mode=0o700)
    return {file.name: _copy_artifact(directory, file.name, target / file.name, STATECHART_FILE_LIMIT,
                                     'statechart artifact') for file in files}


def _diagnostic_output(invoke, args):
    try:
        with tempfile.TemporaryFile() as stdout:
            result = invoke(args, stdout=stdout, stderr=subprocess.DEVNULL,
                            timeout=DIAGNOSTIC_TIMEOUT, check=False)
            output, truncated = _read_limited(stdout, MAX_DIAGNOSTIC_OUTPUT)
            if result.returncode == 0 and not truncated:
                return output
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _container_state(invoke, docker, container):
    output = _diagnostic_output(
        invoke, [docker, 'container', 'inspect', '--format', '{{json .State}}', container])
    try:
        state = json.loads(output)
        status, exit_code, oom_killed = state['Status'], state['ExitCode'], state['OOMKilled']
        if (status in ('created', 'restarting', 'running', 'removing', 'paused', 'exited', 'dead')
                and type(exit_code) is int and 0 <= exit_code <= 255 and type(oom_killed) is bool):
            return {'status': status, 'exit_code': exit_code, 'oom_killed': oom_killed}
    except (TypeError, ValueError, KeyError):
        pass
    return None


def _time_ns(stamp):
    elapsed = dt.datetime.fromisoformat(stamp) - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds) * 1000


def _container_oom_events(invoke, docker, container, since, until):
    output = _diagnostic_output(
        invoke, [docker, 'events', '--since', since, '--until', until,
                 '--filter', 'type=container', '--filter', 'event=oom',
                 '--filter', f'container={container}', '--format', '{{json .}}'])
    if output is None:
        return None
    start_ns, end_ns = _time_ns(since), _time_ns(until)
    events = []
    try:
        for line in output.splitlines():
            event = json.loads(line)
            stamp = event['timeNano']
            if (event['Type'] != 'container' or event['Action'] != 'oom'
                    or event['Actor']['ID'] != container or type(stamp) is not int
                    or not start_ns <= stamp <= end_ns or len(events) >= 32):
                return None
            events.append({'time_ns': stamp})
    except (TypeError, ValueError, KeyError):
        return None
    return events


def _failure_diagnostics(invoke, docker, container_output, report, until):
    classification = 'command-timeout' if report['timed_out'] else 'command-failed'
    diagnostics = {'classification': classification, 'container_id': None, 'state': None,
                   'oom_events': None, 'unavailable': []}
    unavailable = diagnostics['unavailable']
    container = container_output.strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        unavailable.extend(('container-id', 'state', 'oom-events'))
        return diagnostics
    diagnostics['container_id'] = container
    state = _container_state(invoke, docker, container)
    events = _container_oom_events(invoke, docker, container, report['started_at'], until)
    diagnostics.update(state=state, oom_events=events)
    if state is None:
        unavailable.append('state')
    if events is None:
        unavailable.append('oom-events')
    if (state and state['oom_killed']) or events:
        diagnostics['classification'] = 'docker-oom'
    return diagnostics


def run_sandbox(workspace, image, command, timeout, workdir=".", *, memory_mb=2048, invoke=subprocess.run,
                find_executable=shutil.which, screenshot_output=None, source_root="/workspace",
                workspace_read_only=False, regression_source=None, regression_file=None, profile_output=None,
                statechart_output=None, pids_limit=256, capture_failure_screenshot=False,
                recording_output=None, browser_maps_output=None):
    """Return an execution report. The chosen workspace must contain test data only.

    ``source_root`` selects the mounted workspace or the verified image source;
    relative workdirs must still exist inside the host workspace. Set
    workspace_read_only when the command must not modify the mounted files.
    After the attached command returns, each untruncated output stream includes
    a SHA-256 digest of the original bytes, before UTF-8 decoding.
    Profile collection uses a scoped helper and copies its artifact out of a
    disposable container mount. The browser helper can also save its bounded
    source map evidence next to the trace.
    Statechart writes only named TLC reports and charts through a separate
    mount, including a normal invariant-violation exit.
    A declared screenshot check can also retain its PNG on an observed exit 1.
    A read-only image journey can collect a bounded JSONL recording with its PNG.
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
    if capture_failure_screenshot and screenshot_output is None:
        raise SandboxError("failure screenshot capture requires a screenshot output path")
    regression = regression_source is not None or regression_file is not None
    if recording_output is not None and (
            screenshot_output is None or source_root != '/app' or not workspace_read_only
            or regression or profile_output is not None or statechart_output is not None
            or Path(recording_output).suffix != '.jsonl'
            or Path(recording_output).resolve() == Path(screenshot_output).resolve()):
        raise SandboxError("recording requires a distinct JSONL output and screenshot in a read-only /app run")
    profile = None
    profile_file_limit = None
    if profile_output is not None:
        suffix = Path(profile_output).suffix
        profile = PROFILE_ARTIFACTS.get(suffix)
        helper = 'capture-browser.mjs' if suffix == '.cdptrace' else 'node-profile.mjs'
        if (profile is None or not workspace_read_only or source_root != '/app' or workdir != '.'
                or screenshot_output is not None or regression or list(command[:2]) != [
                    'node', '/workspace/' + helper]):
            raise SandboxError("invalid scoped profile input")
        if helper == 'node-profile.mjs' and list(command[2:3]) == ['heap-allocations']:
            if suffix != '.heapsnapshot':
                raise SandboxError("invalid scoped profile input for heap allocation tracking")
            profile_file_limit = profile[1]
    if browser_maps_output is not None and (
            profile != PROFILE_ARTIFACTS['.cdptrace']
            or list(command) != ['node', '/workspace/capture-browser.mjs']
            or Path(browser_maps_output).suffix != '.json'
            or Path(browser_maps_output).resolve() == Path(profile_output).resolve()):
        raise SandboxError('browser source maps require a separate JSON output and a scoped Chrome trace')
    if statechart_output is not None and (
            not workspace_read_only or source_root != '/workspace' or workdir != '.'
            or screenshot_output is not None or profile_output is not None or regression
            or list(command[:2]) != ['node', '/workspace/run-statechart.mjs']):
        raise SandboxError("invalid scoped statechart input")
    if regression and (not workspace_read_only or screenshot_output is not None):
        raise SandboxError("invalid read-only focused regression input")
    if regression_source is not None and (
            not isinstance(regression_source, bytes) or not 0 < len(regression_source) <= 16_384):
        raise SandboxError("invalid focused regression source")
    if regression_file is not None:
        if (not isinstance(regression_file, str) or len(regression_file) > 240
                or not re.fullmatch(r'src/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.regression\.test\.ts', regression_file)
                or 'node_modules' in regression_file.split('/')
                or source_root != '/app' or workdir != '.'
                or command[:3] != ['node', '/app/node_modules/vitest/vitest.mjs', 'run']
                or command[-1] != regression_file):
            raise SandboxError("invalid native focused regression mount or command")
    elif regression and command != ['node', '/workbench-regression/check.mjs']:
        raise SandboxError("invalid standalone focused regression command")
    if not 0 < timeout <= 86400:
        raise SandboxError("timeout must be in (0, 86400] seconds")
    if type(memory_mb) is not int or not 128 <= memory_mb <= 8192:
        raise SandboxError("memory limit must be between 128 and 8192 MiB")
    if type(pids_limit) is not int or not 64 <= pids_limit <= 512:
        raise SandboxError('PID limit must be between 64 and 512')
    if type(source_root) is not str or source_root not in ("/workspace", "/app"):
        raise SandboxError("source root must be /workspace or /app")
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
    image_format = "{{json .Config.Volumes}}"
    pin_image = regression or profile is not None
    if pin_image:
        image_format = '{{.Id}}{{println}}{{json (index .Config "Volumes")}}'
    try:
        inspected = invoke([docker, "image", "inspect", image, "--format", image_format],
                           capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"cannot inspect local Docker image: {exc}") from exc
    if inspected.returncode:
        raise SandboxError("Docker daemon unavailable or image not present locally; no image was pulled")
    try:
        details = inspected.stdout.strip()
        if pin_image:
            image, details = details.split("\n", 1)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ValueError("invalid resolved image ID")
        volumes = json.loads(details)
    except (ValueError, AttributeError) as exc:
        raise SandboxError("cannot establish image volume configuration") from exc
    if volumes:
        raise SandboxError("image declares extra volumes; use an image without VOLUME directives")

    name = "workbench-" + uuid.uuid4().hex
    container_dir = source_root if str(relative) == "." else source_root + "/" + str(relative)
    create = [docker, "create", "--name", name, "--pull=never", "--network=none",
              "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
              f"--pids-limit={pids_limit}", f"--memory={memory_mb}m", "--cpus=2", "--init",
              "--user", f"{os.getuid()}:{os.getgid()}",
              "--tmpfs", f"/tmp:rw,nosuid,nodev,size={TMPFS_MB}m",
              "--mount", f"type=bind,src={source},dst=/workspace" + (",readonly" if workspace_read_only else ""),
              "--workdir", container_dir, "--entrypoint", command[0], image, *command[1:]]
    started = time.monotonic()
    report = {"workspace": str(source), "image": image, "command": command,
              "workdir": str(relative), "source_root": source_root, "container_workdir": container_dir,
              "timeout_seconds": timeout, "memory_mb": memory_mb, "pids_limit": pids_limit,
              "workspace_read_only": workspace_read_only,
              "tmpfs_mb": TMPFS_MB,
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "exit_code": None, "create_exit_code": None, "timed_out": False, "cleanup_confirmed": False}
    if profile_file_limit is not None:
        create[create.index('--workdir'):create.index('--workdir')] = [
            '--ulimit', f'fsize={profile_file_limit}:{profile_file_limit}']
        report['profile_file_limit_bytes'] = profile_file_limit
    if regression_file is not None:
        report.update(regression_file=regression_file,
                      regression_input='isolated-input' if regression_source is not None else 'verified-image')
    created = False
    with tempfile.TemporaryDirectory(prefix='workbench-artifact-') as artifact_dir, \
            tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, \
            tempfile.TemporaryFile() as create_stdout, tempfile.TemporaryFile() as create_stderr:
        try:
            if screenshot_output is not None or profile is not None or statechart_output is not None:
                mount = ['--mount', f'type=bind,src={artifact_dir},dst=/artifacts']
                if screenshot_output is not None:
                    mount.extend(['--env', 'WORKBENCH_SCREENSHOT_PATH=/artifacts/screenshot.png'])
                if recording_output is not None:
                    mount.extend(['--env', 'WORKBENCH_RECORDING_PATH=/artifacts/recording.jsonl'])
                create[create.index('--workdir'):create.index('--workdir')] = mount
            if regression_source is not None:
                asset = Path(artifact_dir) / ('check.mjs' if regression_file is None else 'check.ts')
                asset.write_bytes(regression_source)
                mount = (f'type=bind,src={artifact_dir},dst=/workbench-regression,readonly'
                         if regression_file is None else f'type=bind,src={asset},dst=/app/{regression_file},readonly')
                create[create.index('--workdir'):create.index('--workdir')] = [
                    '--mount', mount]
                report['regression_sha256'] = hashlib.sha256(regression_source).hexdigest()
            # Docker create emits its container ID and daemon diagnostics, not application output.
            result = invoke(create, stdout=create_stdout, stderr=create_stderr, timeout=timeout, check=False)
            report["create_exit_code"] = result.returncode
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
                capture_codes = (0, 1) if capture_failure_screenshot else (0,)
                if screenshot_output is not None and result.returncode in capture_codes:
                    report['screenshot_sha256'] = _copy_screenshot(artifact_dir, screenshot_output)
                    report['screenshot_path'] = str(Path(screenshot_output).resolve())
                    if recording_output is not None:
                        report['recording_sha256'] = _copy_artifact(
                            artifact_dir, 'recording.jsonl', recording_output, MAX_RECORDING, 'recording')
                        report['recording_path'] = str(Path(recording_output).resolve())
                if profile is not None and result.returncode == 0:
                    filename, limit = profile
                    report['profile_sha256'] = _copy_artifact(artifact_dir, filename, profile_output,
                                                             limit, 'runtime profile')
                    report['profile_path'] = str(Path(profile_output).resolve())
                    if browser_maps_output is not None:
                        report['browser_maps_sha256'] = _copy_artifact(
                            artifact_dir, BROWSER_MAPS_ARTIFACT, browser_maps_output,
                            BROWSER_MAPS_LIMIT, 'browser source maps')
                        report['browser_maps_path'] = str(Path(browser_maps_output).resolve())
                if statechart_output is not None and result.returncode in (0, 1):
                    report['statechart_artifacts'] = _copy_statechart(artifact_dir, statechart_output)
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
                if created and (report['timed_out'] or report['exit_code'] != 0):
                    until = dt.datetime.now(dt.timezone.utc).isoformat()
                    container_output, truncated = _read_limited(create_stdout, MAX_CREATE_OUTPUT)
                    report['runtime_diagnostics'] = _failure_diagnostics(
                        invoke, docker, '' if truncated else container_output, report, until)
            finally:
                try:
                    cleanup = invoke([docker, "rm", "--force", name], capture_output=True,
                                     text=True, timeout=15, check=False)
                    report["cleanup_confirmed"] = cleanup.returncode == 0
                    if not report["cleanup_confirmed"]:
                        report["cleanup_error"] = cleanup.stderr.strip()[:2000]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    report["cleanup_error"] = str(exc)
            report["create_stdout"], report["create_stdout_truncated"] = _read_limited(create_stdout, MAX_CREATE_OUTPUT)
            report["create_stderr"], report["create_stderr_truncated"] = _read_limited(create_stderr, MAX_CREATE_OUTPUT)
            capture_complete = created and report["exit_code"] is not None and not report["timed_out"]
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                content, truncated = _read_limited_bytes(stream, MAX_OUTPUT)
                report[name] = content.decode("utf-8", errors="replace")
                report[name + "_truncated"] = truncated
                if capture_complete and not truncated:
                    report[name + "_sha256"] = hashlib.sha256(content).hexdigest()
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
