"""Inventory Simulator resources and address deletions by exact native ID."""

import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile


UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.IGNORECASE | re.ASCII)
RUNTIME_IDENTIFIER = re.compile(r'com\.apple\.CoreSimulator\.SimRuntime\.[A-Za-z0-9.-]+', re.ASCII)
MAX_OUTPUT = 8_000_000
MAX_ENTRIES = 10_000
KINDS = {
    'testing': 'simulator-testing-device',
    'unavailable': 'simulator-unavailable-device',
    'runtime': 'simulator-runtime',
}
LIST_COMMANDS = {
    'testing': ('--set', 'testing', 'list', 'devices', '-j'),
    'unavailable': ('list', 'devices', '-j'),
    'runtime': ('runtime', 'list', '-j'),
}


class SimulatorError(ValueError):
    pass


def _is_uuid(value):
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def _label(value, default=None):
    return value if isinstance(value, str) and value.isprintable() and 0 < len(value) <= 4096 else default


def _devices(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('devices'), dict):
        raise SimulatorError('simctl returned an invalid device list')
    devices = []
    seen = set()
    for runtime, group in payload['devices'].items():
        if not _label(runtime) or not isinstance(group, list):
            raise SimulatorError('simctl returned an invalid device group')
        for info in group:
            if len(devices) >= MAX_ENTRIES:
                raise SimulatorError('simctl device list exceeded the observation limit')
            if (not isinstance(info, dict) or not _is_uuid(info.get('udid'))
                    or not _label(info.get('state')) or type(info.get('isAvailable')) is not bool):
                raise SimulatorError('simctl returned an incomplete device record')
            ident = info['udid']
            if ident.upper() in seen:
                raise SimulatorError('simctl returned a duplicate device ID')
            seen.add(ident.upper())
            devices.append((runtime, info))
    return devices


def _runtimes(payload):
    if not isinstance(payload, dict) or len(payload) > MAX_ENTRIES:
        raise SimulatorError('simctl returned an invalid runtime image list')
    images = []
    seen = set()
    for info in payload.values():
        if not isinstance(info, dict) or not _is_uuid(info.get('identifier')) or not _label(info.get('state')):
            raise SimulatorError('simctl returned an incomplete runtime image record')
        ident = info['identifier']
        if ident.upper() in seen:
            raise SimulatorError('simctl returned a duplicate runtime image ID')
        seen.add(ident.upper())
        images.append(info)
    return images


def _resource(scope, ident, info, tool, fields, reasons):
    size = info.get('sizeBytes')
    return {
        'id': f'simulator:{scope}:{ident}', 'kind': KINDS[scope], 'scope': scope, 'path': None,
        'name': _label(info.get('name'), ident), 'state': info['state'],
        'identity': {'scope': scope, 'id': ident, 'tool': tool, **fields},
        'reasons': reasons,
        'estimated_bytes': size if type(size) is int and size >= 0 else None,
    }


def _device_resource(scope, runtime, info, tool):
    reasons = []
    if info['state'] != 'Shutdown':
        reasons.append('simulator device is not shut down')
    if scope == 'unavailable' and info['isAvailable']:
        reasons.append('default-set simulator device is available')
    fields = {'runtime': runtime, 'device_type': _label(info.get('deviceTypeIdentifier')),
              'data_path': _label(info.get('dataPath')), 'is_available': info['isAvailable'],
              'last_booted_at': _label(info.get('lastBootedAt'))}
    return _resource(scope, info['udid'], info, tool, fields, reasons)


def _runtime_resource(info, device_runtimes, tool):
    runtime = _label(info.get('runtimeIdentifier'))
    reasons = []
    if info.get('deletable') is not True:
        reasons.append('simctl has not marked the runtime image deletable')
    if info['state'] not in ('Ready', 'Unusable'):
        reasons.append('runtime image is not in a stable deletion state')
    if device_runtimes is None or not runtime or not RUNTIME_IDENTIFIER.fullmatch(runtime):
        reasons.append('runtime image device dependencies could not be verified')
    elif runtime in device_runtimes:
        reasons.append('simulator devices still reference this runtime')
    fields = {'runtime': runtime, 'build': _label(info.get('build')), 'version': _label(info.get('version')),
              'image_path': _label(info.get('path')), 'deletable': info.get('deletable') is True,
              'last_used_at': _label(info.get('lastUsedAt'))}
    return _resource('runtime', info['identifier'], info, tool, fields, reasons)


class Simulator:
    def _run(self, *args, timeout=20):
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.run(['/usr/bin/xcrun', *args], stdout=output, stderr=subprocess.DEVNULL,
                                         timeout=timeout, check=False)
            except OSError as error:
                raise SimulatorError('xcrun could not be started') from error
            except subprocess.TimeoutExpired as error:
                raise SimulatorError('xcrun did not complete within its command limit') from error
            if process.returncode != 0:
                raise SimulatorError('xcrun did not successfully complete the command')
            if output.tell() > MAX_OUTPUT:
                raise SimulatorError('xcrun exceeded the output limit')
            output.seek(0)
            return output.read(MAX_OUTPUT)

    def _tool_identity(self):
        try:
            name = self._run('--find', 'simctl').decode('utf-8').strip()
            if not _label(name) or not Path(name).is_absolute():
                raise SimulatorError('xcrun did not locate an absolute simctl path')
            path = Path(name).resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise SimulatorError('the selected simctl is not a regular tool file')
        except (OSError, UnicodeError) as error:
            raise SimulatorError('the selected simctl could not be identified') from error
        return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
                'size': info.st_size, 'modified_ns': info.st_mtime_ns}

    def inventory(self):
        """Use per-collection status before treating a missing ID as absent."""
        status = dict.fromkeys(LIST_COMMANDS, 'unavailable')
        result = {'status': 'unavailable', 'tool': None, 'tool_identity': None, 'collections': status,
                  'resources': [], 'issues': {}}
        try:
            tool = self._tool_identity()
        except SimulatorError as error:
            result['issues']['tool'] = str(error)
            return result

        result['tool'] = tool['path']
        listed = {}
        for scope, arguments in LIST_COMMANDS.items():
            try:
                payload = json.loads(self._run('simctl', *arguments))
                listed[scope] = _runtimes(payload) if scope == 'runtime' else _devices(payload)
                status[scope] = 'available'
            except SimulatorError as error:
                result['issues'][scope] = str(error)
            except ValueError:
                result['issues'][scope] = 'simctl did not return valid JSON'

        try:
            same_tool = self._tool_identity() == tool
        except SimulatorError:
            same_tool = False
        if not same_tool:
            result['issues']['tool'] = 'the selected simctl changed during inventory'
            result['collections'] = dict.fromkeys(LIST_COMMANDS, 'unavailable')
        else:
            result['tool_identity'] = tool
            device_scopes = ('testing', 'unavailable')
            device_runtimes = ({runtime for scope in device_scopes for runtime, _ in listed[scope]}
                               if all(scope in listed for scope in device_scopes) else None)
            if device_runtimes is not None and not all(RUNTIME_IDENTIFIER.fullmatch(name) for name in device_runtimes):
                device_runtimes = None
            resources = [_device_resource(scope, runtime, info, tool)
                         for scope in device_scopes for runtime, info in listed.get(scope, ())]
            resources.extend(_runtime_resource(info, device_runtimes, tool) for info in listed.get('runtime', ()))
            result['resources'] = sorted(resources, key=lambda row: row['id'])
        result['status'] = 'partial' if result['issues'] else 'available'
        return result

    def remove(self, row):
        """Submit one deletion after the caller has verified its approval and fresh state."""
        ident = row.get('id') if isinstance(row, dict) else None
        fields = row.get('identity') if isinstance(row, dict) else None
        prefix, scope, native = ident.split(':', 2) if isinstance(ident, str) and ident.count(':') == 2 else ('', '', '')
        if (prefix != 'simulator' or scope not in KINDS or not _is_uuid(native)
                or not isinstance(fields, dict) or fields.get('id') != native or fields.get('scope') != scope
                or row.get('kind') != KINDS[scope] or row.get('reasons') != []):
            raise SimulatorError('select one identified Simulator resource eligible for deletion')
        if self._tool_identity() != fields.get('tool'):
            raise SimulatorError('the selected simctl changed before deletion')
        prefix = ('runtime',) if scope == 'runtime' else ('--set', 'testing') if scope == 'testing' else ()
        self._run('simctl', *prefix, 'delete', native, timeout=45)
