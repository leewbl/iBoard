"""Run Claude Code using a locally stored GLM key and restricted workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def execute(workspace: Path, prompt: str, timeout: int, *, read_only: bool = False, evidence_paths=()):
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir() or not prompt.strip() or not 1 <= timeout <= 86400:
        raise ValueError("a workspace, nonempty prompt and valid timeout are required")
    if not isinstance(evidence_paths, (list, tuple)) or len(evidence_paths) > 4:
        raise ValueError("bounded explicit evidence directories are required")
    if evidence_paths and not read_only:
        raise ValueError("evidence access is restricted to read-only model calls")
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
    with tempfile.TemporaryDirectory(prefix="workbench-claude-") as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        path.chmod(0o600)
        tools = "Bash,Read,Glob,Grep" if read_only else "Bash,Read,Edit,Write,Glob,Grep"
        cmd = ["claude", "--bare", "--restricted", "--tools", tools, "--allowedTools", tools,
               "--permission-mode", "acceptEdits", "--settings", str(path), "--strict-mcp-config",
               "--no-session-persistence", "--print"]
        process = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return {"exit_code": None, "timed_out": True, "stdout": stdout.replace(key, "[REDACTED]"),
                    "stderr": stderr.replace(key, "[REDACTED]")}
        except BaseException:
            # Claude runs in a separate session, so interrupting this process
            # alone would otherwise leave the model and its children alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return {"exit_code": process.returncode, "timed_out": False, "stdout": stdout.replace(key, "[REDACTED]"),
                "stderr": stderr.replace(key, "[REDACTED]")}


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
