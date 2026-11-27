"""Run a cross-vendor review in an empty, read-only Codex session."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

from runner.sandbox import MAX_SCREENSHOT, SandboxError, _validated_png


FEATURES = ('shell_tool', 'multi_agent', 'browser_use', 'in_app_browser',
            'standalone_web_search', 'skill_search', 'tool_suggest')
ENV = ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CODEX_HOME')


class ReviewError(Exception):
    pass


def _result(stdout, status):
    if status != 0:
        raise ReviewError(f'Codex reviewer exited with status {status}')
    answer, completed = None, False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event['type']
        if kind == 'error' or kind == 'turn.failed':
            raise ReviewError('Codex reviewer reported a failed turn')
        if kind.startswith('item.'):
            item = event['item']
            if item['type'] not in {'agent_message', 'reasoning'}:
                raise ReviewError('Codex reviewer emitted an unexpected item')
            if item['type'] == 'agent_message' and kind == 'item.completed':
                answer = item.get('text')
        if kind == 'turn.completed':
            completed = True
    if not completed or not isinstance(answer, str) or not answer.strip() or len(answer) > 30_000:
        raise ReviewError('Codex reviewer did not return a bounded final answer')
    return json.loads(answer)


def review(prompt, model, timeout, *, images=(), effort='low'):
    if (not re.fullmatch(r'gpt-[a-z0-9][a-z0-9.-]{0,64}', model) or not 1 <= timeout <= 300
            or not prompt or len(prompt.encode('utf-8')) > 90_000 or effort not in ('low', 'medium', 'high', 'xhigh')):
        raise ReviewError('invalid OpenAI model, prompt size, or review timeout')
    with tempfile.TemporaryDirectory(prefix='iboard-decision-review-') as workspace:
        cmd = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'read-only', '-C', workspace, '-c', 'model_provider="openai"',
               '-c', 'approval_policy="never"', '-c', 'web_search="disabled"', '-c', 'project_doc_max_bytes=0',
               '-c', 'model_reasoning_effort=' + json.dumps(effort), '-c', 'mcp_servers={}', '--model', model]
        for flag in FEATURES:
            cmd.extend(('--disable', flag))
        if not isinstance(images, (tuple, list)) or len(images) > 4:
            raise ReviewError('provide at most four validated PNG attachments')
        for index, value in enumerate(images, 1):
            path = Path(value)
            if path.is_symlink() or not path.is_file():
                raise ReviewError('review attachment must be a local PNG file')
            try:
                with path.open('rb') as source:
                    data = source.read(MAX_SCREENSHOT + 1)
                _validated_png(data)
            except (OSError, SandboxError) as error:
                raise ReviewError('could not validate review screenshot') from error
            copied = Path(workspace) / f'attachment-{index}.png'
            copied.write_bytes(data)
            cmd.extend(('--image', str(copied)))
        env = {key:os.environ[key] for key in ENV if key in os.environ}
        proc = subprocess.Popen([*cmd, '--json', '-'], cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(prompt, timeout=timeout)
        except BaseException as error:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                raise ReviewError('Codex reviewer exceeded its time limit') from None
            raise
    try:
        return _result(output, proc.returncode)
    except (ValueError, KeyError, TypeError) as error:
        raise ReviewError('Codex reviewer returned invalid JSON') from error
