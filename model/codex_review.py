"""Run a cross-vendor review in an empty, read-only Codex session."""

import json
import os
import re
import signal
import subprocess
import tempfile


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


def review(prompt, model, timeout):
    if (not re.fullmatch(r'gpt-[a-z0-9][a-z0-9.-]{0,64}', model) or not 1 <= timeout <= 300
            or not prompt or len(prompt.encode('utf-8')) > 90_000):
        raise ReviewError('invalid OpenAI model, prompt size, or review timeout')
    with tempfile.TemporaryDirectory(prefix='iboard-decision-review-') as workspace:
        cmd = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'read-only', '-C', workspace, '-c', 'model_provider="openai"',
               '-c', 'approval_policy="never"', '-c', 'web_search="disabled"', '-c', 'project_doc_max_bytes=0',
               '-c', 'model_reasoning_effort="low"', '-c', 'mcp_servers={}', '--model', model]
        for flag in FEATURES:
            cmd.extend(('--disable', flag))
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
