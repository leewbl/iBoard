import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from workbench import cli, profile_trace, trace_forensics


def cpu_profile(elapsed):
    return {'nodes': [
        {'id': 1, 'children': [2], 'callFrame': {'functionName': '(root)'}},
        {'id': 2, 'callFrame': {'functionName': 'busy', 'url': 'file:///app/app.mjs', 'lineNumber': 0}},
    ], 'samples': [2, 2], 'timeDeltas': [1000, elapsed]}


class TraceForensicsTests(unittest.TestCase):
    def test_cpu_import_binds_captures_and_reopens_the_exact_source_call_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            cli.git(repo, 'init', '-q')
            cli.git(repo, 'config', 'user.name', 'Trace Test')
            cli.git(repo, 'config', 'user.email', 'trace-test@example.invalid')
            (repo / 'app.mjs').write_text('function busy() {}\n')
            cli.git(repo, 'add', 'app.mjs')
            cli.git(repo, 'commit', '-qm', 'tracked symbol')
            revision = cli.sha(repo, 'HEAD')
            inputs = []
            for role, elapsed in (('before', 7000), ('after', 3000)):
                path = root / (role + '.json.gz')
                path.write_bytes(gzip.compress(json.dumps(cpu_profile(elapsed)).encode()))
                inputs.append((path, cli.digest(path)))
            (before, bsha), (after, asha) = inputs
            out = root / 'audit'
            with contextlib.redirect_stdout(io.StringIO()):
                trace_forensics.main([
                    '--input', str(before), '--sha256', bsha, '--compare', str(after), '--compare-sha256', asha,
                    '--repo', str(repo), '--revision', revision, '--compare-revision', revision, '--out', str(out),
                ])
            result = cli.read(out / 'result.json')
            self.assertEqual(result['evidence_strength'], 'paired-observation')
            trial = result['trials']['baseline']
            top = trial['finding']['top_frames'][0]
            self.assertEqual(top['source_status'], 'verified-git-source')
            self.assertEqual([f['symbol'] for f in top['call_path']], ['(root)', 'busy'])
            self.assertEqual(result['comparison']['source_frame_deltas'][0]['delta_us'], -4000)
            snapshot = Path(trial['artifact']['snapshot'])
            self.assertEqual(cli.digest(snapshot), bsha)
            self.assertEqual(snapshot.read_bytes(), before.read_bytes())
            with contextlib.closing(sqlite3.connect(trial['database']['path'])) as db:
                self.assertEqual(db.execute('SELECT SUM(delta_us) FROM samples').fetchone(), (8000,))
                self.assertEqual(db.execute('SELECT SUM(leaf_us) FROM sample_weights').fetchone(), (7000,))
                self.assertEqual(db.execute('SELECT sha256 FROM capture_identity').fetchone(), (bsha,))
            self.assertEqual(cli.git(repo, 'status', '--porcelain'), '')

    def test_digest_mismatch_stops_before_output_and_keeps_original(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'trace.cpuprofile'
            source = json.dumps(cpu_profile(1000)).encode()
            path.write_bytes(source)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                trace_forensics.main(['--input', str(path), '--sha256', '0' * 64, '--out', str(root / 'audit')])
            self.assertEqual(error.exception.code, 1)
            self.assertFalse((root / 'audit').exists())
            self.assertEqual(cli.digest(path), hashlib.sha256(source).hexdigest())

    def test_rewrapping_one_capture_does_not_create_an_independent_pair(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.dumps(cpu_profile(1000)).encode()
            (root / 'original.json').write_bytes(raw)
            (root / 'copy.json.gz').write_bytes(gzip.compress(raw))
            out = root / 'audit'
            with contextlib.redirect_stdout(io.StringIO()):
                trace_forensics.main(['--input', str(root/'original.json'), '--compare', str(root/'copy.json.gz'),
                                      '--out', str(out)])
            result = cli.read(out/'result.json')
            self.assertEqual(result['evidence_strength'], 'identical-artifact')
            a, b = (record['artifact'] for record in result['trials'].values())
            self.assertNotEqual(a['sha256'], b['sha256'])
            self.assertEqual(a['content_sha256'], b['content_sha256'])

    def test_browser_frame_locations_keep_the_origin_without_credentials(self):
        locate = trace_forensics._source_locator(None, None, None)
        a, status = locate('https://user:secret@a.example:443/x.js?v=1#fragment', 8)
        b, _ = locate('https://b.example/x.js?v=2', 8)
        self.assertEqual(a, 'https://a.example/x.js')
        self.assertNotEqual(a, b)
        self.assertEqual(status, 'reported-browser-location')

    def test_cpu_weights_the_frame_after_its_ordered_timestamp_without_tail_estimate(self):
        profile = cpu_profile(1000)
        profile['nodes'][0]['children'].append(3)
        profile['nodes'].append({'id': 3, 'callFrame': {'functionName': 'lastFrame'}})
        profile['samples'], profile['timeDeltas'] = [2, 3, 2], [10, 20, -10]
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            report = profile_trace.import_cpu(db, profile, lambda *_: (None, 'unmapped'))
            self.assertEqual(db.execute('SELECT delta_us FROM samples ORDER BY seq').fetchall(),
                             [(10,), (20,), (-10,)])
            self.assertEqual(report['sampled_us'], 20)
            self.assertEqual([(item['symbol'], item['sampled_us']) for item in report['top_frames']],
                             [('busy', 20), ('lastFrame', 0)])

    def test_heap_import_retains_edges_and_reports_missing_source_symbols(self):
        profile = {'snapshot': {'node_count': 2, 'edge_count': 1, 'meta': {
            'node_fields': ['type', 'name', 'id', 'self_size', 'edge_count'],
            'edge_fields': ['type', 'name_or_index', 'to_node'],
            'node_types': [['synthetic', 'object'], 'string', 'number', 'number', 'number'],
            'edge_types': [['property'], 'string_or_number', 'node'],
        }}, 'nodes': [0, 0, 1, 0, 1, 1, 1, 2, 32, 0], 'edges': [0, 2, 5],
                   'strings': ['', 'Held', 'owner']}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'sample.heapsnapshot'
            path.write_text(json.dumps(profile))
            out = root / 'audit'
            with contextlib.redirect_stdout(io.StringIO()):
                trace_forensics.main(['--input', str(path), '--class-name', 'Held', '--out', str(out)])
            result = cli.read(out / 'result.json')
            self.assertEqual(result['evidence_strength'], 'single-capture-hypothesis')
            record = result['trials']['baseline']
            selected = record['finding']['selected_class']
            self.assertEqual(selected['strong_root_path'][0]['edge'], 'owner')
            self.assertEqual(selected['allocation_source'], 'allocation symbols not captured')
            with contextlib.closing(sqlite3.connect(record['database']['path'])) as db:
                self.assertEqual(db.execute('SELECT origin,target,label FROM heap_edges').fetchone(), (0, 1, 'owner'))
