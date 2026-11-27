"""Run Claude Code using a locally stored GLM key and restricted workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def _trace(stdout, workspace=None):
    events, final, model, truncated = [], None, None, False
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'system' and item.get('subtype') == 'init':
            model = item.get('model')
        if item.get('type') == 'result':
            final = item
        message = item.get('message')
        if item.get('type') not in {'assistant', 'user'} or not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text' and item['type'] == 'assistant' and isinstance(block.get('text'), str):
                event = {'type':'assistant', 'text':block['text'][:4000]}
                truncated |= len(block['text']) > 4000
            elif kind == 'tool_use':
                event = {'type':'tool_use', 'tool':block.get('name'), 'id':block.get('id')}
                args = block.get('input')
                if workspace is not None and event['tool'] == 'Read' and isinstance(args, dict):
                    name = args.get('file_path')
                    if isinstance(name, str) and len(name) <= 512 and name.isprintable():
                        target = (workspace / name).resolve()
                        if target.is_relative_to(workspace):
                            event['path'] = str(target.relative_to(workspace))
            elif kind == 'tool_result':
                event = {'type':'tool_result', 'id':block.get('tool_use_id'), 'error':block.get('is_error') is True}
            else:
                continue
            if len(events) < 80:
                events.append(event)
            else:
                truncated = True
    return {'provider':'zhipu', 'model':model, 'trace':events, 'trace_truncated':truncated,
            'stdout':final['result'] if isinstance(final, dict) and isinstance(final.get('result'), str) else '',
            'trace_completed':bool(final and final.get('subtype') == 'success' and not final.get('is_error'))}


def execute(workspace: Path, prompt: str, timeout: int, *, read_only: bool = False, evidence_paths=(),
            file_tools_only=False, capture_trace=False, requested_model=None, private_workspace=False):
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir() or not prompt.strip() or not 1 <= timeout <= 86400:
        raise ValueError("a workspace, nonempty prompt and valid timeout are required")
    if requested_model is not None and (not isinstance(requested_model, str)
                                        or not re.fullmatch(r'glm-[a-z0-9][a-z0-9.-]{0,64}', requested_model)):
        raise ValueError('invalid explicit GLM model')
    if not isinstance(evidence_paths, (list, tuple)) or len(evidence_paths) > 4:
        raise ValueError("bounded explicit evidence directories are required")
    if evidence_paths and not read_only:
        raise ValueError("evidence access is restricted to read-only model calls")
    if private_workspace and (not file_tools_only or not capture_trace or evidence_paths):
        raise ValueError("private workspaces require traced file tools and no additional directories")
    evidence_dirs = []
    home = Path.home().resolve()
    credential_root = ROOT.resolve()
    sensitive = [home / name for name in ('.claude', '.codex', '.ssh', '.config', '.aws')]
    for raw in evidence_paths:
        candidate = Path(raw).expanduser()
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("evidence directory is missing or unsafe")
        resolved = candidate.resolve(strict=True)
        if (home.is_relative_to(resolved) or credential_root.is_relative_to(resolved) or
                any(resolved.is_relative_to(item) or item.is_relative_to(resolved) for item in sensitive)):
            raise ValueError("evidence access cannot include home or credential roots")
        # Preserve ordinary platform root aliases, but never follow links inside
        # an evidence path provided by the trusted coordinator.
        for ancestor in (candidate, *candidate.parents):
            if ancestor.is_symlink():
                platform_alias = {Path('/tmp'): Path('/private/tmp'), Path('/var'): Path('/private/var')}
                if ancestor not in platform_alias or ancestor.resolve() != platform_alias[ancestor]:
                    raise ValueError("evidence directory has a symbolic-link ancestor")
        if str(resolved) not in evidence_dirs:
            evidence_dirs.append(str(resolved))
    key = (ROOT / "glm.ini").read_text(encoding="utf-8").strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("glm.ini must contain a single API key")
    config = json.loads((Path.home() / ".claude/settings.json").read_text(encoding="utf-8"))
    model_env = config.get("env", {})
    base = model_env.get("ANTHROPIC_BASE_URL", "")
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.hostname not in {"open.bigmodel.cn", "api.z.ai"} or parsed.username or parsed.password:
        raise ValueError("Claude provider must be a supported GLM HTTPS endpoint")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if k in os.environ}
    env.update({"ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_BASE_URL": base})
    for name, value in model_env.items():
        if name.startswith("ANTHROPIC_DEFAULT_") and not name.endswith("_NAME") and isinstance(value, str):
            env[name] = value
    settings = {
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "filesystem": {"denyRead": ["~/"], "allowRead": [str(workspace), str(Path.home() / ".nvm/versions/node"), *evidence_dirs]},
            "credentials": {"envVars": [{"name": n, "mode": "deny"} for n in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY")]},
            "network": {"allowedDomains": [], "allowLocalBinding": False, "allowAllUnixSockets": False},
        }
    }
    if read_only:
        settings["sandbox"]["filesystem"]["denyWrite"] = [str(workspace), *evidence_dirs]
    if private_workspace:
        settings["sandbox"]["filesystem"].update(denyRead=["/"], allowRead=[str(workspace)],
                                                allowWrite=[str(workspace)])
    with tempfile.TemporaryDirectory(prefix="workbench-claude-") as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        path.chmod(0o600)
        tools = "Bash,Read,Glob,Grep" if read_only else "Bash,Read,Edit,Write,Glob,Grep"
        if file_tools_only:
            tools = tools.removeprefix("Bash,")
        cmd = ["claude", "--bare", "--restricted", "--tools", tools, "--allowedTools", tools,
               "--permission-mode", "acceptEdits", "--settings", str(path), "--strict-mcp-config",
               "--no-session-persistence", "--print"]
        if file_tools_only:
            cmd.extend(["--effort", "low"])
        if capture_trace:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        if requested_model is not None:
            cmd.extend(['--model', requested_model])
        if evidence_dirs:
            cmd.extend(["--add-dir", *evidence_dirs])
        process = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        def result(exit_code, timed_out, stdout, stderr):
            stdout, stderr = stdout.replace(key, "[REDACTED]"), stderr.replace(key, "[REDACTED]")
            values = _trace(stdout, workspace if private_workspace else None) if capture_trace else {'stdout':stdout}
            if capture_trace and exit_code == 0 and not values['trace_completed']:
                exit_code = 1
            return {'exit_code':exit_code, 'timed_out':timed_out, 'stderr':stderr, **values}

        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return result(None, True, stdout, stderr)
        except BaseException:
            # Claude runs in a separate session, so interrupting this process
            # alone would otherwise leave the model and its children alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return result(process.returncode, False, stdout, stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = execute(args.workspace, args.prompt_file.read_text(encoding="utf-8"), args.timeout, read_only=args.read_only)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"GLM adapter failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["exit_code"] == 0 and not result["timed_out"] else 1


if __name__ == "__main__":
    sys.exit(main())
