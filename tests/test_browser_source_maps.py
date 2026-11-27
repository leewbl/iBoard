import base64
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from runner.sandbox import SandboxError, run_sandbox
from workbench import browser_forensics, browser_source_maps, cli, trace_forensics
from workbench.runtime_forensics import ForensicsError


class BrowserSourceMapTests(unittest.TestCase):
    port = 18789
    script = 'http://127.0.0.1:18789/assets/bundle.js'
    script_sha = sha256(b'compiled script').hexdigest()
    source = 'function original() {\n  return 42\n}\n'

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'src').mkdir()
        (self.repo / 'src/feature.ts').write_text(self.source)
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Browser Forensics Test')
        cli.git(self.repo, 'config', 'user.email', 'browser-test@example.invalid')
        cli.git(self.repo, 'add', 'src/feature.ts')
        cli.git(self.repo, 'commit', '-qm', 'feature source')
        self.revision = cli.sha(self.repo, 'HEAD')
        self.ident = sha256(self.script.encode()).hexdigest()
        self.observed = {self.ident:{(self.script, self.script_sha)}}

    def sidecar(self, content=None, **fields):
        source_map = {'version':3, 'sources':['../src/feature.ts'], 'names':['original'],
                      'sourcesContent':[self.source if content is None else content], 'mappings':'EAACA,E'}
        raw = json.dumps(source_map).encode()
        record = {'url_sha256':self.ident, 'script_sha256':self.script_sha, 'map_url':self.script + '.map',
                  'map_sha256':sha256(raw).hexdigest(), 'content_base64':base64.b64encode(raw).decode(), **fields}
        path = self.root / 'browser-source-maps.json'
        data = json.dumps({'schema':1, 'maps':[record], 'omitted':0}).encode()
        path.write_bytes(data)
        return path, sha256(data).hexdigest()

    def bind(self, content=None, **fields):
        return browser_source_maps.bind(self.repo, self.revision, self.port, self.observed,
                                        *self.sidecar(content, **fields))

    def test_binds_only_byte_matching_source_in_selected_git_revision(self):
        (self.repo / 'src/feature.ts').write_text('working tree diverged')
        mappings, receipt = self.bind()
        self.assertEqual(receipt['maps'][0]['status'], 'map-bound-to-script')
        mapper, _ = mappings[self.ident]
        self.assertEqual(mapper.lookup(1, 3), {'source_file':'src/feature.ts', 'source_line':1,
                                             'source_column':2, 'source_symbol':'original',
                                             'source_sha256':sha256(self.source.encode()).hexdigest()})
        for line, column in ((1,2), (1,5), (2,3), (1,None)):
            self.assertIsNone(mapper.lookup(line,column))
        stale, _ = self.bind(content='function original() { return 99 }')
        self.assertIsNone(stale[self.ident][0].lookup(1,3))

    def test_mismatch_or_corruption_never_produces_a_map_binding(self):
        mappings, receipt = self.bind(script_sha256='a' * 64)
        self.assertFalse(mappings)
        self.assertEqual(receipt['maps'][0]['status'], 'script-identity-mismatch')
        with self.assertRaisesRegex(ForensicsError, 'recorded digest'):
            self.bind(map_sha256='a' * 64)
        with self.assertRaisesRegex(ForensicsError, 'invalid browser source map identity'):
            self.bind(map_url='http://127.0.0.1:18888/foreign.js.map')

    def test_writes_exact_generated_to_original_position_and_withholds_cross_thread_url(self):
        mappings, receipt = self.bind()
        captured = self.root / 'trace.cdptrace'
        for label, threads in (('one',[7]), ('shared',[7,8])):
            events = [{'ph':'X', 'name':'FunctionCall', 'cat':'devtools.timeline', 'pid':6, 'tid':tid,
                       'ts':100, 'dur':50, 'args':{'data':{'url':self.script,'lineNumber':1,'columnNumber':3}}}
                      for tid in threads]
            captured.write_text(json.dumps({'traceEvents':events}))
            imported = trace_forensics._import(self.root / label, captured, trace_forensics._load(captured, None),
                                               None, None, Path('/app'), None)
            browser_forensics._label_sources(imported, {}, {'pid':6,'tid':7}, mappings)
            location = imported['finding']['duration_groups'][0]
            database = Path(imported['database']['path'])
            self.assertEqual(imported['database']['sha256'], cli.digest(database))
            with sqlite3.connect(database) as db:
                rows = db.execute('SELECT generated_line,generated_column,source_file,source_line,source_column,map_sha256 '
                                  'FROM trace_source_locations').fetchall()
            if label == 'one':
                self.assertEqual(location['source_status'], 'verified-git-source-map')
                self.assertEqual(rows, [(1,3,'src/feature.ts',1,2,receipt['maps'][0]['map_sha256'])])
            else:
                self.assertEqual(location['source_status'], 'reported-browser-location')
                self.assertEqual(rows, [])

    def test_sandbox_sidecar_requires_scoped_browser_trace(self):
        with self.assertRaisesRegex(SandboxError, 'separate JSON output'):
            run_sandbox(self.repo, 'local:test', ['node', '/workspace/node-profile.mjs'], 10,
                        source_root='/app', workspace_read_only=True, profile_output=self.root / 'cpu.cpuprofile',
                        browser_maps_output=self.root / 'maps.json', find_executable=lambda _: 'docker',
                        invoke=lambda *_a, **_kw: self.fail('unscoped sidecar reached Docker'))
