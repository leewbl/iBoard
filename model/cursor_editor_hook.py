"""Fail-closed, content-free Cursor hook for a single scoped editing turn."""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid

EVENTS = ('beforeSubmitPrompt', 'preToolUse', 'postToolUse', 'postToolUseFailure', 'stop')
TOOLS = {'Read': 'Read', 'Write': 'Write', 'StrReplace': 'Write', 'Edit': 'Write'}
TOKEN = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
MAX_LOG = 32_768


def require(value):
    if not value:
        raise ValueError('invalid hook input')


def load_config(filename):
    path = Path(filename)
    require(path.is_absolute() and not path.is_symlink())
    data = json.loads(path.read_text())
    require(isinstance(data, dict)
            and set(data) == {'workspace', 'model', 'request_sha256', 'files', 'editable', 'log', 'initial'})
    root, log = Path(data['workspace']), Path(data['log'])
    require(root.is_absolute() and root.resolve(strict=True) == root and root.is_dir())
    require(log.is_absolute() and log.parent.resolve(strict=True) == log.parent and not log.is_relative_to(root))
    require(isinstance(data['model'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', data['model']))
    require(isinstance(data['request_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', data['request_sha256']))
    files, editable = data['files'], data['editable']
    require(isinstance(files, list) and files and isinstance(editable, list))
    require(all(isinstance(p, str) and p and not PurePosixPath(p).is_absolute() and '\\' not in p
                and all(part not in ('', '.', '..') for part in p.split('/')) for p in files))
    require(len(files) == len(set(files)) and len(editable) == len(set(editable)) and set(editable) <= set(files))
    initial = data['initial']
    require(isinstance(initial, dict) and set(initial) == set(files)
            and all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) for value in initial.values()))
    return data, root, log


def relative_path(root, raw):
    require(isinstance(raw, str) and raw and '\0' not in raw)
    path = Path(raw)
    if not path.is_absolute():
        require(all(part not in ('', '.', '..') for part in raw.split('/')))
        path = root / path
    require(path.is_relative_to(root) and path != root)
    rel = path.relative_to(root)
    require(all(part not in ('', '.', '..') for part in rel.parts))
    current = root
    for part in rel.parts:
        current = current / part
        require(not current.is_symlink())
    require(path.resolve(strict=True) == path and path.is_file())
    return rel.as_posix()



def file_digest(root, selected):
    descriptors = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        parts = selected.split('/')
        for component in parts[:-1]:
            descriptors.append(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        with os.fdopen(fd, 'rb') as target:
            info = os.fstat(target.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 12_000)
            contents = target.read(12_001)
            require(len(contents) <= 12_000)
            return hashlib.sha256(contents).hexdigest()
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def process(data, root, log, event):
    require(isinstance(event, dict) and event.get('hook_event_name') in EVENTS)
    name = event['hook_event_name']
    session, generation = event.get('conversation_id'), event.get('generation_id')
    require(isinstance(session, str) and str(uuid.UUID(session)) == session)
    require(isinstance(generation, str) and TOKEN.fullmatch(generation))
    model = event.get('model_id') or event.get('model')
    require(model == data['model'])
    if 'workspace_roots' in event:
        require(event['workspace_roots'] == [str(root)])
    if 'cwd' in event:
        require(event['cwd'] == str(root))
    fd = os.open(log, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_LOG and info.st_nlink == 1)
        raw = stream.read(MAX_LOG + 1)
        require(len(raw.encode()) <= MAX_LOG)
        rows = [json.loads(line) for line in raw.splitlines()]
        require(len(rows) <= 82 and all(isinstance(row, dict) for row in rows))
        base = {'session': session, 'generation': generation, 'model': model}
        if name == 'beforeSubmitPrompt':
            require(not rows and event.get('attachments', []) == [])
            prompt = event.get('prompt')
            require(isinstance(prompt, str) and hashlib.sha256(prompt.encode()).hexdigest() == data['request_sha256'])
            require({path: file_digest(root, path) for path in data['files']} == data['initial'])
            row = {'event': 'submit', **base, 'request_sha256': data['request_sha256']}
            response = {'continue': True}
        else:
            require(rows and rows[0] == {'event': 'submit', **base, 'request_sha256': data['request_sha256']}
                    and not any(row.get('event') == 'stop' for row in rows))
            if name == 'stop':
                status = event.get('status')
                require(status in ('completed', 'aborted', 'error'))
                row = {'event': 'stop', **base, 'status': status}
                if status == 'completed':
                    digests = {}
                    for selected in data['files']:
                        require(relative_path(root, selected) == selected)
                        digests[selected] = file_digest(root, selected)
                    row['files'] = digests
                response = {}
            else:
                input_data = event.get('tool_input')
                tool = TOOLS.get(event.get('tool_name'))
                ident = event.get('tool_use_id')
                require(tool and isinstance(ident, str) and TOKEN.fullmatch(ident) and isinstance(input_data, dict))
                paths = [input_data[k] for k in ('path', 'file_path', 'filePath') if k in input_data]
                require(paths and len(set(paths)) == 1)
                path = relative_path(root, paths[0])
                require(path in (data['files'] if tool == 'Read' else data['editable']))
                common = {**base, 'id': ident, 'tool': tool, 'path': path}
                prior = [item for item in rows if item.get('id') == ident]
                if name == 'preToolUse':
                    require(not prior and len(rows) < 81)
                    row = {'event': 'start', **common}
                    response = {'permission': 'allow'}
                else:
                    require(prior == [{'event': 'start', **common}])
                    row = {'event': 'result', **common, 'ok': name == 'postToolUse'}
                    if row['ok'] and tool == 'Write':
                        row['sha256'] = file_digest(root, path)
                    response = {}
        encoded = json.dumps(row, separators=(',', ':')) + '\n'
        require(info.st_size + len(encoded.encode()) <= MAX_LOG)
        stream.seek(0, os.SEEK_END)
        stream.write(encoded)
        stream.flush()
        return response


def main():
    name = None
    try:
        require(len(sys.argv) == 2)
        data, root, log = load_config(sys.argv[1])
        raw = sys.stdin.buffer.read(256_001)
        require(len(raw) <= 256_000)
        event = json.loads(raw)
        name = event.get('hook_event_name') if isinstance(event, dict) else None
        print(json.dumps(process(data, root, log, event)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        if name == 'beforeSubmitPrompt':
            print('{"continue":false}')
        elif name == 'preToolUse':
            print('{"permission":"deny"}')
        else:
            print('{}')
        sys.exit(2)


if __name__ == '__main__':
    main()
