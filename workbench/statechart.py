"""Run the pinned pstack statechart engine with TLC inside offline Docker."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import uuid

from runner.sandbox import run_sandbox
from workbench import cli, refactor
from workbench.prompt_profiles import PSTACK_COMMIT


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/statechart'
UPSTREAM = 'skills/statechart/SKILL.md'
TLC_SHA256 = '936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88'
JRE_SHA256 = {
    'arm64': 'c6161dcfb365024fbfbcda265343d562d6f821c78dd8e7aeaeaf294893f6ff8b',
    'amd64': '2edef9e4f81ec5ee426d023336d4cfbd7b4644e99884e7a73a6a867b70b17acf',
}
PELE_SHA512 = 'KUmBt7P5JduVKQaP6FKfXiRgH3LBVIKsmdgPJZU7jD3ye6F+Z8fbms+OjivE7rWY09Qx+5X65VKe/9ywKFJ9Sw=='
INPUT_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,100}\.(?:tla|cfg|mjs|js|ts|json)')
TEXT_LIMIT = 1_000_000


def _command(args, *, timeout, env=None, cwd=None):
    result = subprocess.run(args, timeout=timeout, env=env, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip()[-2000:] or f'{args[0]} returned {result.returncode}')
    return result.stdout.strip()


def _freeze_archive(source, target, algorithm, limit):
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= limit:
        raise ValueError(f'invalid runtime archive: {source.name}')
    digest = hashlib.new(algorithm)
    size = 0
    with source.open('rb') as stream, target.open('xb') as frozen:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(block)
            if size > limit:
                raise ValueError(f'runtime archive grew beyond its limit: {source.name}')
            digest.update(block)
            frozen.write(block)
    return digest


def prepare(*, base_image, java_archive, tlc_jar, pele_archive, seconds=300):
    """Package hash-pinned local assets without fetching during the image build."""
    remaining = refactor._deadline(seconds)
    if not refactor.IMAGE.fullmatch(base_image):
        raise ValueError('pass the immutable local Node image ID as --base-image')
    host = os.environ.get('DOCKER_HOST') or _command(
        ['docker', 'context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}'],
        timeout=min(15, remaining()))
    if not host.startswith('unix://'):
        raise ValueError('preparing the statechart image requires a local Docker engine')
    arch = _command(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', base_image],
                    timeout=min(15, remaining()))
    if arch not in JRE_SHA256:
        raise ValueError('prepare supports Linux arm64 and amd64 Node images')
    label = 'workbench-statechart:' + uuid.uuid4().hex
    with TemporaryDirectory(prefix='iboard-statechart-image-') as directory:
        scratch = Path(directory)
        root = scratch / 'build'
        root.mkdir()
        java = scratch / 'java.tar.gz'
        jar = root / 'tla2tools.jar'
        pele = scratch / 'pele.tgz'
        if (_freeze_archive(java_archive, java, 'sha256', 100_000_000).hexdigest() != JRE_SHA256[arch]
                or _freeze_archive(tlc_jar, jar, 'sha256', 8_000_000).hexdigest() != TLC_SHA256
                or base64.b64encode(_freeze_archive(pele_archive, pele, 'sha512', 20_000_000).digest()).decode()
                != PELE_SHA512):
            raise ValueError('statechart runtime archive does not match its pinned checksum')
        for name, archive in (('jre', java), ('pele', pele)):
            target = root / name
            target.mkdir()
            _command(['tar', '-xf', str(archive.absolute()), '--strip-components=1', '-C', str(target)],
                     timeout=min(60, remaining()))
        shutil.copyfile(ROOT / 'images/statechart/Dockerfile', root / 'Dockerfile')
        env = {**os.environ, 'DOCKER_BUILDKIT': '0'}
        env.pop('BUILDKIT_HOST', None)
        _command(['docker', 'build', '--network=none', '--pull=false', '--build-arg', f'NODE_BASE={base_image}',
                  '--tag', label, str(root)], timeout=remaining(), env=env)
    image = _command(['docker', 'image', 'inspect', '--format', '{{.Id}}', label],
                     timeout=min(15, remaining()))
    if not refactor.IMAGE.fullmatch(image):
        raise ValueError('cannot resolve prepared runtime to an immutable image ID')
    return {'image': image, 'tag': label, 'node_base': base_image, 'architecture': arch,
            'tlc': '1.7.4', 'temurin_jre': '25.0.5.1+2', 'pele': '1.9.0'}


def _input(path, extensions):
    path = Path(path).expanduser()
    if not INPUT_NAME.fullmatch(path.name) or path.suffix not in extensions:
        raise ValueError(f'invalid statechart input name: {path.name}')
    data = refactor._read(path, TEXT_LIMIT)
    data.decode('utf-8')
    return path.name, data


def _snapshot(spec, config, view, support):
    spec_path = Path(spec).expanduser()
    model = _input(spec_path, {'.tla'})
    cfg = _input(config or spec_path.with_suffix('.cfg'), {'.cfg'})
    presentation = _input(view, {'.js', '.mjs', '.ts'}) if view else None
    if len(support) > 16:
        raise ValueError('provide at most sixteen additional local model or view files')
    files = [model, cfg]
    if presentation:
        files.append(presentation)
    files.extend(_input(item, {'.tla', '.mjs', '.js', '.ts', '.json'}) for item in support)
    if len(dict(files)) != len(files):
        raise ValueError('statechart inputs must have unique basenames')
    return dict(files), model[0], cfg[0], presentation[0] if presentation else None


def _write_files(root, files):
    root.mkdir(mode=0o700)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _engine():
    source = SKILL / 'vendor'
    files = {'run-statechart.mjs': refactor._read(SKILL / 'run-statechart.mjs', TEXT_LIMIT)}
    expected = json.loads(refactor._read(SKILL / 'vendor-files.json', 16_000))
    seen = {path.relative_to(source).as_posix() for path in source.rglob('*') if path.is_file()}
    if seen != set(expected):
        raise ValueError('the pinned pstack engine has missing or unexpected files')
    for name, digest in expected.items():
        data = refactor._read(source / name, TEXT_LIMIT)
        if refactor._digest(data) != digest:
            raise ValueError(f'the pinned pstack engine file changed: {name}')
        files['engine/' + name] = data
    return files


def _retained(root, execution, mermaid_only):
    files = execution['statechart_artifacts']
    for name, digest in files.items():
        if Path(name).name != name or cli.digest(root / name) != digest:
            raise ValueError('retained statechart file changed after collection')
    runtime = json.loads(refactor._read(root / 'runtime.json', 10_000))
    if (runtime['verdict'] != execution['exit_code'] or runtime['tlc'] != '1.7.4'
            or runtime['tlc_sha256'] != TLC_SHA256 or not runtime['java'] or not runtime['node']):
        raise ValueError('runtime receipt does not match the pinned TLC execution')
    renderer = runtime['renderer']
    expected_chart = {
        'no-chart': set(), 'skipped': {'chart.mmd'}, 'Pele 1.9.0': {'chart.mmd', 'chart.svg'},
        'failed': {'chart.mmd', 'render-error.txt'},
    }
    if (renderer not in expected_chart
            or set(files) & {'chart.mmd', 'chart.svg', 'render-error.txt'} != expected_chart[renderer]
            or (renderer == 'skipped' and not mermaid_only)
            or (mermaid_only and renderer not in ('no-chart', 'skipped'))):
        raise ValueError('statechart files do not match their renderer receipt')
    return runtime


def run(*, spec, config=None, view=None, support=(), image, out, seconds=180, memory_mb=2048,
        mermaid_only=False, sandbox=run_sandbox):
    output = Path(out).expanduser().absolute()
    output.mkdir(mode=0o700, parents=True)
    remaining = refactor._deadline(seconds)
    memory = refactor._memory(memory_mb)
    report = {'schema': cli.SCHEMA, 'outcome': 'blocked', 'image': image, 'pstack_commit': PSTACK_COMMIT,
              'pstack_source': UPSTREAM}
    try:
        if not refactor.IMAGE.fullmatch(image):
            raise ValueError('pass an immutable local statechart image ID')
        model_files, model, cfg, labels = _snapshot(spec, config, view, support)
        engines = _engine()
        _write_files(output / 'inputs', model_files)
        report['input_sha256'] = {name: refactor._digest(data) for name, data in model_files.items()}
        report['engine_sha256'] = {name: refactor._digest(data) for name, data in engines.items()}
        command = ['node', '/workspace/run-statechart.mjs', 'mermaid' if mermaid_only else 'svg',
                   '/workspace/model/' + model, '--config', '/workspace/model/' + cfg]
        if labels:
            command.extend(['--view', '/workspace/model/' + labels])
        with TemporaryDirectory(prefix='iboard-statechart-input-') as directory:
            scratch = Path(directory) / 'frozen'
            _write_files(scratch, {**engines, **{'model/' + name: data for name, data in model_files.items()}})
            raw = sandbox(str(scratch), image, command, remaining(), memory_mb=memory, workspace_read_only=True,
                          statechart_output=output / 'artifacts')
        receipt = output / 'execution.json'
        cli.write(receipt, raw)
        report['execution'] = {'path': receipt.name, 'sha256': cli.digest(receipt)}
        refactor._observe(raw, command, image, memory, set(), source_root='/workspace')
        if raw['exit_code'] not in (0, 1):
            raise ValueError('statechart exited before returning a model verdict; see execution.json')
        report['runtime'] = _retained(output / 'artifacts', raw, mermaid_only)
        report['artifacts'] = {f'artifacts/{name}': digest for name, digest in raw['statechart_artifacts'].items()}
        report['outcome'] = 'holds' if raw['exit_code'] == 0 else 'violated'
    except refactor.ERRORS as error:
        report['reason'] = str(error)
    cli.write(output / 'result.json', report)
    return report


def main(argv=None):
    cli_parser = argparse.ArgumentParser(description=__doc__)
    commands = cli_parser.add_subparsers(dest='mode', required=True)
    build = commands.add_parser('prepare', help='Build an offline runtime using pinned local tool archives')
    for option in ('base-image', 'java-archive', 'tlc-jar', 'pele-archive'):
        build.add_argument(f'--{option}', required=True)
    build.add_argument('--seconds', type=int, default=300)
    check = commands.add_parser('check', help='Model-check and export evidence in a fresh offline container')
    check.add_argument('spec')
    check.add_argument('--config')
    check.add_argument('--view')
    check.add_argument('--support', action='append', default=[])
    check.add_argument('--image', required=True)
    check.add_argument('--out', required=True)
    check.add_argument('--seconds', type=int, default=180)
    check.add_argument('--memory-mb', type=int, default=2048)
    check.add_argument('--mermaid-only', action='store_true')
    args = vars(cli_parser.parse_args(argv))
    mode = args.pop('mode')
    try:
        report = prepare(**args) if mode == 'prepare' else run(**args)
    except refactor.ERRORS as error:
        cli_parser.error(str(error))
    if mode == 'prepare':
        print(json.dumps(report))
        return 0
    print(json.dumps({'outcome': report['outcome'], 'report': str(Path(args['out']).resolve() / 'result.json'),
                      'renderer': report.get('runtime', {}).get('renderer')}))
    return {'holds': 0, 'violated': 1, 'blocked': 2}[report['outcome']]


if __name__ == '__main__':
    raise SystemExit(main())
