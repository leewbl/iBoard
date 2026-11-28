"""Invoke Codex with a bounded file interface for blinded comparisons."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile

from model import codex_review


MODEL = re.compile(r'gpt-[a-z0-9][a-z0-9.-]{0,64}')
BROKER = Path(__file__).with_name('specimen_files.py').resolve()
TOOLS = {'read_file': 'Read', 'write_file': 'Write'}
FEATURES = (*codex_review.FEATURES, 'apps', 'plugins', 'remote_plugin', 'memories',
            'image_generation', 'view_image', 'browser_use_external', 'hooks', 'goals')
FILE_INSTRUCTIONS = ('Use the desk file tools to work with the project. The desk write_file tool is authorized '
                     'to edit the paths in its schema when the user asks for a change.')
MAX_OUTPUT = 1_000_000
MAX_TRACE = 16_384
MAX_CALLS = 40


class EditError(ValueError):
    pass


def _calls(output):
    """Accept completed turns using only the expected file tools."""
    completed = False
    calls = []
    for line in output.splitlines():
        event = json.loads(line)
        kind = event['type']
        if kind in ('error', 'turn.failed'):
            raise EditError('Codex reported a failed turn')
        if kind == 'turn.completed':
            completed = True
        if not kind.startswith('item.'):
            continue
        item = event['item']
        if item['type'] in ('agent_message', 'reasoning', 'todo_list', 'error'):
            continue
        if (item['type'] != 'mcp_tool_call' or item['server'] != 'desk'
                or item['tool'] not in TOOLS):
            raise EditError('Codex reported an unexpected tool')
        if kind == 'item.completed':
            args = item.get('arguments')
            if not isinstance(args, dict) or item.get('status') not in ('completed', 'failed'):
                raise EditError('Codex omitted file tool arguments')
            path = args.get('path')
            calls.append((TOOLS[item['tool']], path, item['status'] == 'completed'))
    if not completed or not 1 <= len(calls) <= MAX_CALLS:
        raise EditError('Codex did not return a complete bounded turn with file calls')
    return calls


def _observed(rows, calls, files, editable):
    """Correlate Codex's completed calls with the file broker's own receipts."""
    allowed = {'Read': files, 'Write': editable}
    normalized = [(tool, path if isinstance(path, str) and path in allowed[tool] else '', delivered)
                  for tool, path, delivered in calls]
    trace = []
    ids = set()
    observed = []
    for line in rows.splitlines():
        row = json.loads(line)
        if (set(row) != {'id', 'tool', 'path', 'ok'} or type(row['ok']) is not bool
                or not isinstance(row['id'], str) or not row['id'] or len(row['id']) > 80
                or row['id'] in ids or row['tool'] not in TOOLS.values()
                or (row['path'] not in allowed[row['tool']] and (row['ok'] or row['path'] != ''))):
            raise EditError('invalid private file receipt')
        ids.add(row['id'])
        trace.extend(({'type': 'tool_use', 'tool': row['tool'], 'id': row['id'], 'path': row['path']},
                      {'type': 'tool_result', 'id': row['id'], 'error': not row['ok']}))
        observed.append((row['tool'], row['path'], row['ok']))
    # Concurrent MCP results can arrive in a different order from the broker log.
    def keys(entries):
        return Counter((tool, path) for tool, path, _ in entries)

    completed = keys(row for row in normalized if row[2])
    succeeded = keys(row for row in observed if row[2])
    if keys(observed) != keys(normalized) or succeeded - completed:
        raise EditError('Codex tool events disagree with the file broker receipts')
    return trace


def execute(workspace, prompt, timeout, *, requested_model, files, editable):
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir() or not prompt.strip() or not 1 <= timeout <= 300 or not MODEL.fullmatch(requested_model):
        raise EditError('invalid scoped Codex request')
    with TemporaryDirectory(prefix='desk-config-') as directory, TemporaryFile() as output:
        private = Path(directory).resolve()
        config = private / 'desk.json'
        trace = private / 'receipt.jsonl'
        config.write_text(json.dumps({'workspace': str(workspace), 'files': list(files),
                                      'editable': list(editable), 'trace': str(trace)}), encoding='utf-8')
        config.chmod(0o600)
        args = json.dumps(['-I', '-B', str(BROKER), str(config)])
        # The frozen plan authorizes only the broker's declared file interface.
        server = ('mcp_servers={desk={command=' + json.dumps(sys.executable) + ',args=' + args
                  + ',required=true,enabled_tools=["read_file","write_file"],default_tools_approval_mode="approve"}}')
        command = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '-C', str(workspace), '--model', requested_model,
                   '-c', 'model_provider="openai"', '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                   '-c', 'project_doc_max_bytes=0', '-c', 'model_reasoning_effort="low"', '-c', server,
                   '-c', 'developer_instructions=' + json.dumps(FILE_INSTRUCTIONS)]
        for flag in FEATURES:
            command.extend(('--disable', flag))
        env = {key: os.environ[key] for key in codex_review.ENV if key in os.environ}
        process = subprocess.Popen([*command, '--json', '-'], cwd=workspace, env=env,
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                   text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=timeout)
        except BaseException as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                raise EditError('Codex editor exceeded its time limit') from None
            raise
        output.seek(0)
        raw = output.read(MAX_OUTPUT + 1)
        if process.returncode != 0 or len(raw) > MAX_OUTPUT or trace.is_symlink() or not trace.is_file():
            raise EditError('Codex did not complete its scoped file session')
        with trace.open('rb') as stream:
            rows = stream.read(MAX_TRACE + 1)
        if len(rows) > MAX_TRACE:
            raise EditError('Codex file receipts exceed the trace limit')
        events = _observed(rows, _calls(raw), files, editable)
    return {'exit_code': 0, 'model': requested_model, 'trace': events,
            'trace_completed': True, 'trace_truncated': False, 'host': 'codex'}
