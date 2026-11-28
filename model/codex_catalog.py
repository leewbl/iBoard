"""Read the local Codex app-server model catalog; entries are candidates, not probes."""
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time

from model import codex_review


class CatalogError(ValueError):
    pass


MAX_RESPONSE = 1_000_000
MAX_PAGES = 20
EFFORTS = ('low', 'medium', 'high', 'xhigh')
MODEL = re.compile(r'gpt-[a-z0-9][a-z0-9.-]{0,64}\Z')


def list_models(timeout=20):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise CatalogError('invalid catalog timeout')
    deadline = time.monotonic() + timeout
    env = {key: os.environ[key] for key in codex_review.ENV if key in os.environ}
    command = ['codex', 'app-server', '--listen', 'stdio://', '-c', 'model_provider="openai"',
               '-c', 'mcp_servers={}', '-c', 'analytics.enabled=false',
               '--disable', 'plugins', '--disable', 'apps', '--disable', 'hooks']
    process, selector, complete = None, None, False
    with tempfile.TemporaryDirectory(prefix='iboard-model-catalog-') as workspace:
        try:
            process = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = b''
            total = 0

            def send(value):
                if time.monotonic() >= deadline:
                    raise CatalogError('model catalog timed out')
                process.stdin.write((json.dumps(value, separators=(',', ':')) + '\n').encode())
                process.stdin.flush()

            def response(ident):
                nonlocal pending, total
                while True:
                    while b'\n' in pending:
                        raw, pending = pending.split(b'\n', 1)
                        if not raw.strip():
                            continue
                        try:
                            row = json.loads(raw)
                        except (ValueError, UnicodeError) as error:
                            raise CatalogError('invalid catalog response') from error
                        if not isinstance(row, dict):
                            raise CatalogError('invalid catalog response')
                        if row.get('id') == ident:
                            if 'error' in row or not isinstance(row.get('result'), dict):
                                raise CatalogError('catalog request failed')
                            return row['result']
                        if 'id' in row:
                            raise CatalogError('unexpected catalog response id')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise CatalogError('model catalog timed out')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise CatalogError('catalog process ended early')
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        raise CatalogError('catalog response exceeds limit')
                    pending += chunk

            send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                  'params': {'clientInfo': {'name': 'iboard', 'title': 'iBoard', 'version': '1.0.0'}}})
            response(1)
            send({'jsonrpc': '2.0', 'method': 'initialized'})
            cursor = None
            seen_cursors, seen_models, models = set(), set(), []
            for page in range(MAX_PAGES):
                params = {'limit': 50, 'includeHidden': False}
                if cursor is not None:
                    params['cursor'] = cursor
                send({'jsonrpc': '2.0', 'id': page + 2, 'method': 'model/list', 'params': params})
                result = response(page + 2)
                rows = result.get('data')
                if not isinstance(rows, list) or len(rows) > 50 or 'nextCursor' not in result:
                    raise CatalogError('invalid catalog page')
                for row in rows:
                    if not isinstance(row, dict):
                        raise CatalogError('invalid catalog row')
                    model, hidden, default = row.get('model'), row.get('hidden'), row.get('isDefault')
                    efforts = row.get('supportedReasoningEfforts')
                    if not isinstance(model, str) or type(hidden) is not bool or type(default) is not bool or not isinstance(efforts, list):
                        raise CatalogError('invalid catalog row')
                    if any(not isinstance(item, dict) or not isinstance(item.get('reasoningEffort'), str) for item in efforts):
                        raise CatalogError('invalid catalog effort')
                    supported = [effort for effort in EFFORTS if any(item['reasoningEffort'] == effort for item in efforts)]
                    if hidden or not MODEL.fullmatch(model) or not supported:
                        continue
                    if model in seen_models:
                        raise CatalogError('duplicate catalog model')
                    seen_models.add(model)
                    models.append({'model': model, 'efforts': supported, 'default': default})
                cursor = result['nextCursor']
                if cursor is None:
                    complete = True
                    return models
                if not isinstance(cursor, str) or not cursor or len(cursor) > 1000 or cursor in seen_cursors:
                    raise CatalogError('invalid catalog cursor')
                seen_cursors.add(cursor)
            raise CatalogError('catalog page limit exceeded')
        except (OSError, subprocess.SubprocessError) as error:
            raise CatalogError('unable to query model catalog') from error
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                if complete:
                    process.stdin.close()
                    try:
                        process.wait(timeout=max(0, min(0.5, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                # Also reap descendants if the parent failed or lingered after EOF.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
