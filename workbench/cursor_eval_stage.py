"""Prepare private specimen folders for one approved Cursor Editor turn."""

from hashlib import sha256
import json
from pathlib import Path
import re
import shlex
import sys
from tempfile import mkdtemp
import time

from model import cursor_editor_hook
from workbench import automate_me, cli, swarm


MODEL = re.compile(r'[a-z][a-z0-9][a-z0-9.-]{0,63}')
HOOK = Path(cursor_editor_hook.__file__).resolve()


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def verify_hooks(path, digest):
    automate_me._read(path, 16_384, digest)


def _config(work, log, arm, data, original):
    return {'workspace': str(work), 'log': str(log), 'model': arm['model'],
            'request_sha256': sha256(data['request'].encode()).hexdigest(),
            'files': data['files'], 'editable': data['editable'],
            'initial': {name: sha256(body.encode()).hexdigest() for name, body in original.items()}}


def prepare(record, data, repo, projects, plan_digest, destination, materialize):
    output = Path(destination).expanduser().resolve()
    arms = {arm['name']: arm for arm in data['arms'] if arm.get('host') == 'cursor-editor'}
    _require(arms and not output.is_relative_to(repo), 'stage Cursor arms outside the evaluated checkout')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    folders = {}
    started_ns = time.time_ns()
    for index, name in enumerate((name for name in record['order'] if name in arms), 1):
        work = Path(mkdtemp(prefix='studio-')).resolve()
        materialize(work, projects[name])
        folder = work / '.cursor'
        folder.mkdir(mode=0o700)
        config = output / f'desk-{index}.json'
        log = output / f'desk-{index}.jsonl'
        cli.write(config, _config(work, log, arms[name], data, projects[name]))
        command = shlex.join([sys.executable, '-I', '-B', str(HOOK), str(config)])
        hooks = folder / 'hooks.json'
        cli.write(hooks, {'version': 1, 'hooks': {
            event: [{'command': command, 'failClosed': True}] for event in cursor_editor_hook.EVENTS}})
        folders[name] = {'workspace': str(work), 'config': config.name, 'config_sha256': cli.digest(config),
                         'log': log.name, 'hooks_sha256': cli.digest(hooks)}
    stage = output / 'stage.json'
    cli.write(stage, {'schema': cli.SCHEMA, 'plan_sha256': plan_digest, 'hook_sha256': cli.digest(HOOK),
                      'started_ns': started_ns, 'folders': folders})
    return {'stage': str(stage), 'sha256': cli.digest(stage), 'request': data['request'],
            'workspaces': {name: row['workspace'] for name, row in folders.items()}}


def load(path, digest, plan_digest, data, projects):
    arms = {arm['name']: arm for arm in data['arms'] if arm.get('host') == 'cursor-editor'}
    _require((path is None and digest is None) == (not arms), 'supply a stage and digest for Cursor Editor arms')
    if not arms:
        return {}, data['seconds']
    _require(path and digest and re.fullmatch(r'[0-9a-f]{64}', digest), 'use the approved Cursor stage digest')
    stage_path = Path(path).expanduser().absolute()
    stage = swarm._read(stage_path, digest)
    _require(stage_path.parent.resolve() == stage_path.parent and stage['plan_sha256'] == plan_digest
             and stage['hook_sha256'] == cli.digest(HOOK) and set(stage['folders']) == set(arms),
             'Cursor stage does not match the evaluation or current hook')
    elapsed = (time.time_ns() - stage['started_ns']) / 1_000_000_000
    _require(0 <= elapsed < data['seconds'], 'Cursor stage exceeded the approved time limit')
    receipts = {}
    workspaces = set()
    for name, row in stage['folders'].items():
        _require(re.fullmatch(r'desk-[1-4]\.json', row['config']) and row['log'] == row['config'] + 'l',
                 'invalid stage receipt path')
        config = json.loads(automate_me._read(stage_path.parent / row['config'], 16_384, row['config_sha256']))
        work = Path(row['workspace'])
        _require(work.resolve(strict=True) == work and work not in workspaces and work.name.startswith('studio-')
                 and not (work / '.cursor').is_symlink(), 'invalid or reused Cursor specimen workspace')
        workspaces.add(work)
        log = stage_path.parent / row['log']
        _require(config == _config(work, log, arms[name], data, projects[name]),
                 'Cursor hook input scope changed')
        verify_hooks(work / '.cursor' / 'hooks.json', row['hooks_sha256'])
        receipts[name] = {'workspace': work, 'log': log, 'hooks_sha256': row['hooks_sha256']}
    return receipts, data['seconds'] - elapsed
