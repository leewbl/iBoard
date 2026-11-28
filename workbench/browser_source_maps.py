"""Bind Chromium's captured source maps to the exact source bytes in Git."""

import base64
from collections import defaultdict
import hashlib
import json
import re
import subprocess
from urllib.parse import unquote, urljoin, urlsplit

from runner.sandbox import BROWSER_MAPS_LIMIT
from workbench import source_maps
from workbench.runtime_forensics import ForensicsError


MAP_LIMIT = source_maps.MAX_MAP_BYTES
MAP_COUNT = 100
SHA256 = re.compile(r'[a-f0-9]{64}')


def _git_sources(repo, revision):
    index = subprocess.check_output(
        ['git', '-C', str(repo), 'ls-tree', '-r', '-z', '--full-tree', revision], timeout=30)
    by_blob = defaultdict(set)
    for entry in index.split(b'\0'):
        if not entry:
            continue
        header, path = entry.split(b'\t', 1)
        mode, kind, digest = header.split()
        path = path.decode('utf-8', 'replace')
        if (kind == b'blob' and mode in (b'100644', b'100755') and len(digest) == 40
                and path.isprintable() and '\ufffd' not in path and len(path) <= 2048):
            by_blob[digest.decode('ascii')].add(path)

    def resolve(base_url, source_root, source, content):
        raw = content.encode('utf-8')
        digest = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
        candidates = by_blob.get(digest, set())
        if len(candidates) == 1:
            return next(iter(candidates)), hashlib.sha256(raw).hexdigest()
        if candidates:
            try:
                root = urljoin(base_url, source_root.rstrip('/') + '/') if source_root else base_url
                declared = unquote(urlsplit(urljoin(root, source)).path).lstrip('/')
            except ValueError:
                return None
            if declared in candidates:
                return declared, hashlib.sha256(raw).hexdigest()
        return None

    return resolve


def _map_url(url, port):
    if not isinstance(url, str) or not 0 < len(url) <= 4096 or not url.isprintable():
        return False
    if url == 'inline':
        return True
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme == 'http' and parsed.netloc == f'127.0.0.1:{port}'
            and bool(parsed.path) and not parsed.query and not parsed.fragment)


def bind(repo, revision, port, observed, path, expected_sha):
    """Return scripts with usable source maps and a receipt for every collected map."""
    with path.open('rb') as stream:
        raw = stream.read(BROWSER_MAPS_LIMIT + 1)
    digest = hashlib.sha256(raw).hexdigest()
    if not 0 < len(raw) <= BROWSER_MAPS_LIMIT or digest != expected_sha:
        raise ForensicsError('browser source map evidence changed after collection')
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or manifest['schema'] != 1
            or not isinstance(manifest.get('maps'), list) or len(manifest['maps']) > MAP_COUNT
            or type(manifest.get('omitted')) is not int or not 0 <= manifest['omitted'] <= 1000):
        raise ForensicsError('invalid browser source map evidence')

    rows = []
    by_script = defaultdict(list)
    for entry in manifest['maps']:
        keys = ('url_sha256', 'script_sha256', 'map_url', 'map_sha256')
        if not isinstance(entry, dict) or not {*keys, 'content_base64'} <= entry.keys():
            raise ForensicsError('incomplete browser source map evidence')
        url_sha, script_sha, url, map_sha = (entry[key] for key in keys)
        if (any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in (url_sha, script_sha, map_sha))
                or not _map_url(url, port) or not isinstance(entry['content_base64'], str)
                or len(entry['content_base64']) > 4 * ((MAP_LIMIT + 2) // 3)):
            raise ForensicsError('invalid browser source map identity')
        raw_map = base64.b64decode(entry['content_base64'], validate=True)
        if not 0 < len(raw_map) <= MAP_LIMIT or hashlib.sha256(raw_map).hexdigest() != map_sha:
            raise ForensicsError('source map bytes do not match the recorded digest')
        row = {key:entry[key] for key in keys}
        rows.append(row)
        by_script[url_sha].append((row, raw_map))

    mappings = {}
    resolve = None
    for url_sha, records in by_script.items():
        scripts = observed.get(url_sha, set())
        if len(records) != 1 or len(scripts) != 1:
            for row, _ in records:
                row['status'] = 'script-identity-ambiguous-or-unobserved'
            continue
        script_url, script_sha = next(iter(scripts))
        row, raw_map = records[0]
        if row['script_sha256'] != script_sha or not _map_url(script_url, port) or script_url == 'inline':
            row['status'] = 'script-identity-mismatch'
            continue
        if resolve is None:
            resolve = _git_sources(repo, revision)
        try:
            source_map = source_maps.decode(raw_map, script_url if row['map_url'] == 'inline' else row['map_url'], resolve)
        except (source_maps.SourceMapError, UnicodeError, RecursionError):
            row['status'] = 'malformed-or-unsupported-map'
            continue
        row['status'] = 'map-bound-to-script'
        mappings[url_sha] = source_map, row

    return mappings, {'path':str(path), 'sha256':digest, 'omitted':manifest['omitted'], 'maps':rows}
