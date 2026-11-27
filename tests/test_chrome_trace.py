import sqlite3
import unittest

from workbench import chrome_trace


def event(phase, timestamp, *, name=None, pid=1, tid=7, **fields):
    return {'ph': phase, 'pid': pid, 'tid': tid, 'ts': timestamp, 'name': name, **fields}


def head(pid=1, tid=7, identifier='same'):
    return event('P', 10, name='Profile', pid=pid, tid=tid, id=identifier,
                 args={'data': {'startTime': 12}})


def chunk(timestamp, *, pid=1, tid=99, identifier='same', nodes=(), samples=(), deltas=()):
    return event('P', timestamp, name='ProfileChunk', pid=pid, tid=tid, id=identifier, args={'data': {
        'cpuProfile': {'nodes': list(nodes), 'samples': list(samples)}, 'timeDeltas': list(deltas)}})


def frame(node_id, name, url=None, line=-1, **fields):
    return {'id': node_id, 'callFrame': {'functionName': name, 'url': url, 'lineNumber': line}, **fields}


class ChromeTraceTests(unittest.TestCase):
    def database(self):
        db = sqlite3.connect(':memory:')
        db.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(db.close)
        return db

    def test_imports_queryable_events_and_scoped_duration_intervals(self):
        db = self.database()
        events = [
            event('B', 20, name='outer', cat='app', s='proc', id='001'),
            event('B', 21, name='inner', cat='app'),
            event('E', 35, name='outer', cat='app', s='proc', id='001'),
            event('X', 25, name='FunctionCall', cat='devtools.timeline', dur=3.25, tts=16, tdur=2,
                  args={'data': {'functionName': 'draw', 'url': 'https://example.invalid/app.js', 'lineNumber': 1}}),
            event('E', 27, name='inner', cat='app', tid=8),
            event('E', 22, name='inner', cat='app'),
            event('E', 30, name='outer', cat='app', s='wrong', id='001'),
            event('X', 36, name='bad', dur=-1),
            event('B', 37, name='left-open', cat='app'),
            event('M', 0, name='process_name', tid=0, args={'name': 'Renderer'}),
            event('M', 0, name='thread_name', args={'name': 'Main'}),
            event('b', 40, name='async', id2={'local': '7'}),
            event('e', 50, name='async', id2={'local': '7'}),
            {'name': 'malformed'},
        ]
        result = chrome_trace.import_trace(db, {'traceEvents': events})
        self.assertEqual(result['events'], 13)
        self.assertEqual(result['intervals'], 3)
        self.assertEqual(result['incomplete'], {
            'invalid_duration': 1, 'invalid_event': 1, 'mismatched_end': 1, 'orphan_end': 1, 'unclosed_begin': 1,
        })
        self.assertEqual(db.execute('SELECT * FROM trace_intervals ORDER BY start_ordinal').fetchall(),
                         [(0, 2, 15), (1, 5, 1), (3, 3, 3.25)])
        self.assertEqual(db.execute('SELECT process_name, thread_name FROM trace_threads WHERE tid = 7').fetchone(),
                         ('Renderer', 'Main'))
        self.assertEqual(db.execute('SELECT scope, event_id FROM trace_events WHERE ordinal = 0').fetchone(),
                         ('proc', '"001"'))
        self.assertEqual(db.execute('SELECT id2 FROM trace_events WHERE ordinal = 11').fetchone(),
                         ('{"local": "7"}',))
        self.assertEqual(db.execute('SELECT thread_ts_us, thread_duration_us FROM trace_events WHERE ordinal = 3').fetchone(),
                         (16, 2))
        self.assertEqual(result['duration_groups'][1], {
            'category': 'devtools.timeline', 'name': 'FunctionCall', 'symbol': 'draw',
            'path': 'https://example.invalid/app.js', 'line': 1, 'events': 1, 'total_us': 3.25, 'max_us': 3.25,
        })

    def test_imports_split_cpu_chunks_with_process_and_profile_identity(self):
        db = self.database()
        first = frame(1, '(root)', children=[2])
        leaf = frame(2, 'tick', 'file:///does/not/exist.js', 5)
        events = [
            chunk(25, samples=[2], deltas=[6], nodes=[first]),
            head(),
            chunk(35, samples=[2], deltas=[4], nodes=[leaf]),
            head(pid=2, tid=55),
            chunk(21, pid=2, samples=[1], deltas=[7], nodes=[frame(1, '(idle)')]),
            event('X', 11, name='render', dur=1000, args={'data': {'url': 'https://wrong.invalid/'}}),
        ]
        result = chrome_trace.import_trace(db, events)
        self.assertEqual(result['cpu_profiles'], 2)
        self.assertEqual(result['cpu_samples'], 3)
        self.assertEqual(result['duration_groups'][0]['total_us'], 1000)
        self.assertIsNone(result['duration_groups'][0]['path'])
        self.assertEqual(result['cpu_frames'][0], {
            'symbol': 'tick', 'path': 'file:///does/not/exist.js', 'line': 6, 'samples': 2, 'sampled_us': 4,
        })
        self.assertEqual(result['hot_cpu_paths'][0]['ancestry'], 'root-observed')
        self.assertEqual([frame['symbol'] for frame in result['hot_cpu_paths'][0]['frames']], ['(root)', 'tick'])
        self.assertEqual(db.execute('SELECT pid, tid, declared_start_us FROM trace_profiles ORDER BY pid').fetchall(),
                         [(1, 7, 12), (2, 55, 12)])
        self.assertEqual(db.execute('SELECT node_id, parent_id FROM trace_profile_frames WHERE profile_key = 1').fetchall(),
                         [(1, None), (2, 1)])
        self.assertEqual(db.execute('SELECT chunk_ordinal FROM trace_profile_samples WHERE profile_key = 1 ORDER BY ordinal').fetchall(),
                         [(0,), (2,)])

    def test_reconstructs_native_signed_delta_timestamps_before_attributing_time(self):
        db = self.database()
        result = chrome_trace.import_trace(db, [head(), chunk(
            20, samples=[1, 2, 1], deltas=[10, -4, 8],
            nodes=[frame(1, 'first'), frame(2, 'late-arrival')],
        )])
        self.assertEqual(result['negative_cpu_deltas'], 1)
        self.assertEqual(result['incomplete'], {})
        self.assertEqual(db.execute('SELECT delta_us, offset_us, weight_us FROM trace_profile_samples ORDER BY ordinal').fetchall(),
                         [(10, 10, 4), (-4, 6, 4), (8, 14, 0)])
        self.assertEqual([row['sampled_us'] for row in result['cpu_frames']], [4, 4])

    def test_final_frame_does_not_receive_the_preceding_interval(self):
        db = self.database()
        result = chrome_trace.import_trace(db, [head(), chunk(
            20, samples=[1, 2, 3], deltas=[10, 0, 7],
            nodes=[frame(1, 'same-timestamp-earlier'), frame(2, 'running'), frame(3, 'last')],
        )])
        self.assertEqual({row['symbol']: row['sampled_us'] for row in result['cpu_frames']},
                         {'same-timestamp-earlier': 0, 'running': 7, 'last': 0})

    def test_discloses_profile_gaps_and_never_guesses_missing_samples(self):
        events = [
            head(),
            chunk(20, samples=[1], deltas=[], nodes=[frame(1, 'incomplete')]),
            chunk(20, identifier='orphan', samples=[1], deltas=[10], nodes=[frame(1, 'unattributed')]),
            head(identifier='unknown-node'),
            chunk(20, identifier='unknown-node', samples=[999], deltas=[10]),
            head(identifier='head-only'),
        ]
        result = chrome_trace.import_trace(self.database(), events)
        self.assertEqual(result['cpu_profiles'], 0)
        self.assertEqual(result['cpu_frames'], [])
        self.assertEqual(result['incomplete'], {'incomplete_profile': 2, 'invalid_profile': 2})

    def test_comparison_reads_all_groups_beyond_summary_cutoff(self):
        before, after = self.database(), self.database()
        events = [event('X', i, name=f'group-{i:02d}', dur=100 + i) for i in range(36)]
        a = chrome_trace.import_trace(before, events)
        b = chrome_trace.import_trace(after, [{**events[0], 'dur': 101}, *events[1:]])
        self.assertEqual(a['duration_groups'], b['duration_groups'])
        comparison = chrome_trace.compare_traces(before, after)
        self.assertEqual(comparison['durations']['compared_groups'], 36)
        self.assertEqual(comparison['durations']['largest_changes'], [{
            'category': None, 'name': 'group-00', 'symbol': None, 'path': None, 'line': None,
            'before_events': 1, 'after_events': 1, 'before_total_us': 100,
            'after_total_us': 101, 'delta_total_us': 1,
        }])

    def test_comparison_discloses_if_a_profile_was_dropped_before_comparing(self):
        before, after = self.database(), self.database()
        chrome_trace.import_trace(before, [head(), chunk(
            20, samples=[1, 2], deltas=[5, 7], nodes=[frame(1, 'work'), frame(2, '(idle)')],
        )])
        chrome_trace.import_trace(after, [head(), chunk(20, samples=[1], deltas=[])])
        comparison = chrome_trace.compare_traces(before, after)
        self.assertEqual(comparison['cpu']['coverage'], {
            'status': 'partial', 'before_gaps': {}, 'after_gaps': {'invalid_profile': 1},
        })
        self.assertEqual(comparison['durations']['coverage']['status'], 'complete')
        work = next(row for row in comparison['cpu']['largest_changes'] if row['symbol'] == 'work')
        self.assertEqual((work['before_sampled_us'], work['after_sampled_us']), (7, 0))
        self.assertIn('no imported observations or weight', comparison['zero_semantics'])

    def test_rejects_empty_or_unrecognized_payload(self):
        for payload in ({}, [], {'traceEvents': [{}]}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'Chrome trace'):
                chrome_trace.import_trace(self.database(), payload)
