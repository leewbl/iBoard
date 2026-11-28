"""A narrow stdio MCP server for isolated specimen file access."""
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

LIMIT = 12_000
MAX_CALLS = 40


def fail(message):
    raise ValueError(message)


def config(path):
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or set(raw) != {'workspace', 'files', 'editable', 'trace'}:
        fail('invalid configuration')
    workspace, trace = Path(raw['workspace']), Path(raw['trace'])
    if (not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir()
            or not trace.is_absolute() or trace.parent.resolve(strict=True) != trace.parent
            or trace.is_relative_to(workspace)):
        fail('invalid private paths')
    files, editable = raw['files'], raw['editable']
    if (not isinstance(files, list) or not files or not isinstance(editable, list)
            or any(not isinstance(p, str) or not p or PurePosixPath(p).is_absolute()
                   or any(x in ('', '.', '..') for x in p.split('/')) or '\\' in p for p in files)
            or len(set(files)) != len(files) or len(set(editable)) != len(editable)
            or not set(editable) <= set(files)):
        fail('invalid file selection')
    return workspace, files, editable, trace


def access(root, path, contents=None):
    # Each component is opened relative to the previous directory without
    # following symlinks, closing rename/symlink races inside the workspace.
    parts = path.split('/')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open(root, flags)]
    try:
        for component in parts[:-1]:
            fds.append(os.open(component, flags, dir_fd=fds[-1]))
        mode = os.O_RDONLY if contents is None else os.O_RDWR
        fd = os.open(parts[-1], mode | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                fail('invalid file')
            if contents is None:
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    fail('file too large')
                return raw.decode('utf-8')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                existing = stream.read(LIMIT + 1)
            if len(existing) > LIMIT:
                fail('file too large')
            existing.decode('utf-8')
            raw = contents.encode('utf-8')
            if len(raw) > LIMIT:
                fail('file too large')
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
            return 'File written.'
        finally:
            os.close(fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def serve(workspace, files, editable, trace):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(trace, flags, 0o600)
    count = 0
    with os.fdopen(fd, 'w', encoding='utf-8', buffering=1) as log:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                    continue
                ident = request.get('id')
                method = request.get('method')
                if 'id' not in request:
                    continue
                if not isinstance(ident, (str, int)) or isinstance(ident, bool):
                    continue
                params = request.get('params') or {}
                if method == 'initialize':
                    version = params.get('protocolVersion')
                    version = version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2024-11-05'
                    result = {'protocolVersion': version, 'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'files', 'version': '1.0.0'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    def tool(name, description, allowed, write=False):
                        properties = {'path': {'type': 'string', 'enum': allowed}}
                        if write:
                            properties['text'] = {'type': 'string'}
                        return {'name': name, 'description': description,
                                'inputSchema': {'type': 'object', 'properties': properties,
                                                'required': list(properties), 'additionalProperties': False}}
                    result = {'tools': [tool('read_file', 'Read a selected file.', files),
                                        tool('write_file', 'Write an editable file.', editable, True)]}
                elif method == 'tools/call':
                    count += 1
                    name = params.get('name')
                    args = params.get('arguments')
                    operation = {'read_file': 'Read', 'write_file': 'Write'}.get(name)
                    selected = args.get('path') if isinstance(args, dict) else None
                    allowed = editable if name == 'write_file' else files
                    valid_path = selected if isinstance(selected, str) and selected in allowed else ''
                    ok = False
                    try:
                        if count > MAX_CALLS or operation is None or not isinstance(args, dict):
                            fail('invalid call')
                        if set(args) != ({'path'} if name == 'read_file' else {'path', 'text'}):
                            fail('invalid arguments')
                        if not valid_path or (name == 'write_file' and valid_path not in editable):
                            fail('not allowed')
                        if name == 'write_file' and not isinstance(args['text'], str):
                            fail('invalid text')
                        text = access(workspace, valid_path, args['text'] if name == 'write_file' else None)
                        ok = True
                    except (OSError, ValueError, UnicodeError):
                        text = 'File operation rejected.'
                    if operation is not None and count <= MAX_CALLS:
                        log.write(json.dumps({'id': str(ident), 'tool': operation, 'path': valid_path, 'ok': ok}) + '\n')
                        log.flush()
                    result = {'content': [{'type': 'text', 'text': text}], 'isError': not ok}
                else:
                    print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                    continue
                print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}), flush=True)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or not Path(sys.argv[1]).is_absolute():
            fail('invalid configuration')
        serve(*config(sys.argv[1]))
    except (OSError, ValueError, TypeError, KeyError):
        sys.exit(1)
