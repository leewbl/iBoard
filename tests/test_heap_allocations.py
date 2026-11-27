import contextlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from workbench import cli, heap_allocations, profile_trace, trace_forensics
from workbench.runtime_forensics import summarize_heap


ENTRY = 'file:///app/entry.mjs'
ALLOCATOR = '/app/leaks.mjs'
FOREIGN = 'file:///external/foreign.mjs'
UNLOCATED = '/app/dynamic.mjs'


def snapshot():
    strings = ['', 'Owner', 'Held', 'Decoy', 'owner', 'first', 'second', 'foreign',
               'untraced', 'decoy', 'noPosition', '(root)', 'serve', ENTRY, 'createHeld',
               ALLOCATOR, 'outsider', FOREIGN, 'missingPosition', UNLOCATED]
    meta = {
        'node_fields': ['type', 'name', 'id', 'self_size', 'edge_count', 'trace_node_id'],
        'edge_fields': ['type', 'name_or_index', 'to_node'],
        'node_types': [['synthetic', 'object'], 'string', 'number', 'number', 'number', 'number'],
        'edge_types': [['property'], 'string_or_number', 'node'],
        'trace_function_info_fields': ['function_id', 'name', 'script_name', 'script_id', 'line', 'column'],
        'trace_node_fields': ['id', 'function_info_index', 'count', 'size', 'children'],
    }
    return {
        'snapshot': {'node_count': 8, 'edge_count': 7, 'trace_function_count': 5, 'meta': meta},
        'nodes': [
            0, 0, 1, 0, 1, 0,
            1, 1, 5, 16, 6, 0,
            1, 2, 9, 24, 0, 4,
            1, 2, 11, 40, 0, 4,
            1, 2, 13, 72, 0, 5,
            1, 2, 15, 56, 0, 0,
            1, 2, 17, 80, 0, 6,
            1, 3, 19, 144, 0, 4,
        ],
        'edges': [
            0, 4, 6,
            0, 5, 12,
            0, 6, 18,
            0, 7, 24,
            0, 8, 30,
            0, 10, 36,
            0, 9, 42,
        ],
        'strings': strings,
        'trace_function_infos': [
            0, 11, 0, 0, 0, 0,
            45, 12, 13, 7, 4, 2,
            47, 14, 15, 8, 12, 5,
            49, 16, 17, 9, 9, 1,
            51, 18, 19, 10, 0, 0,
        ],
        'trace_tree': [
            1, 0, 0, 0, [
                2, 1, 0, 0, [
                    4, 2, 10000, 800000, [],
                ],
                5, 3, 7000, 700000, [],
                6, 4, 9000, 900000, [],
            ],
        ],
    }


def locate(url, line):
    if url == ENTRY and line == 4:
        return 'entry.mjs', 'verified-git-source'
    if url == ALLOCATOR and line == 12:
        return 'leaks.mjs', 'verified-git-source'
    return url, 'outside-capture-source-root'


class HeapAllocationTests(unittest.TestCase):
    def test_tracked_instances_use_snapshot_counts_and_the_same_representative(self):
        profile = snapshot()
        result = summarize_heap(profile, 'Held', allocations=heap_allocations.parse(profile, locate))
        selected = result['selected_class']
        self.assertEqual((selected['count'], selected['shallow_bytes']), (5, 272))
        self.assertEqual([edge['edge'] for edge in selected['strong_root_path']], ['owner', 'first'])

        allocation = result['allocation']
        self.assertEqual(allocation['class_name'], 'Held')
        self.assertEqual(
            (allocation['snapshot_objects'], allocation['traced_objects'], allocation['verified_source_objects']),
            (5, 4, 2),
        )
        sites = {site['trace_node_id']: site for site in allocation['sites']}
        self.assertEqual({key: (site['objects'], site['shallow_bytes']) for key, site in sites.items()},
                         {4: (2, 64), 5: (1, 72), 6: (1, 80)})
        stack = sites[4]['stack']
        self.assertEqual([frame['symbol'] for frame in stack], ['(root)', 'serve', 'createHeld'])
        self.assertEqual(
            [(frame['source_file'], frame['line'], frame['column'], frame['source_status']) for frame in stack[1:]],
            [('entry.mjs', 4, 2, 'verified-git-source'), ('leaks.mjs', 12, 5, 'verified-git-source')],
        )
        self.assertNotEqual(stack[0]['source_status'], 'verified-git-source')
        self.assertNotEqual(sites[5]['stack'][-1]['source_status'], 'verified-git-source')
        self.assertNotEqual(sites[6]['stack'][-1]['source_status'], 'verified-git-source')
        self.assertFalse(sites[6]['stack'][-1]['line'])

        representative = allocation['representative']
        self.assertEqual((representative['heap_node'], representative['trace_node_id']), (2, 4))
        self.assertEqual(representative['stack'], stack)

    def test_untracked_snapshot_keeps_retainer_and_missing_source_description(self):
        profile = snapshot()
        profile['snapshot']['trace_function_count'] = 0
        profile['trace_function_infos'] = []
        profile['trace_tree'] = []
        profile['nodes'][5::6] = [0] * profile['snapshot']['node_count']
        graph = heap_allocations.parse(profile, locate)
        self.assertIsNone(graph)
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            result = profile_trace.import_heap(db, profile, 'Held', locate)
            self.assertEqual(db.execute("SELECT count(*) FROM heap_nodes WHERE label='Held'").fetchone(), (5,))
        self.assertNotIn('allocation', result)
        self.assertEqual(result['selected_class']['allocation_source'], 'allocation symbols not captured')
        self.assertEqual(result['selected_class']['strong_root_path'][-1]['edge'], 'first')

    def test_malformed_allocation_graph_cannot_attach_source_to_objects(self):
        invalid_index = snapshot()
        invalid_index['trace_tree'][4][4][1] = 5
        duplicate_id = snapshot()
        duplicate_id['trace_tree'][4][5] = 4
        incomplete_functions = snapshot()
        incomplete_functions['trace_function_infos'].pop()
        for description, profile in (('invalid function index', invalid_index), ('duplicate trace ID', duplicate_id),
                                     ('incomplete function row', incomplete_functions)):
            with self.subTest(description=description), self.assertRaises(ValueError):
                heap_allocations.parse(profile, locate)

    def test_positive_object_reference_must_point_to_a_known_trace(self):
        profile = snapshot()
        profile['nodes'][2 * 6 + 5] = 9999
        with self.assertRaises(ValueError):
            graph = heap_allocations.parse(profile, locate)
            summarize_heap(profile, 'Held', allocations=graph)

    def test_offline_import_links_snapshot_objects_to_trace_and_pinned_git_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            cli.git(repo, 'init', '-q')
            cli.git(repo, 'config', 'user.name', 'Allocation Test')
            cli.git(repo, 'config', 'user.email', 'allocation-test@example.invalid')
            (repo / 'entry.mjs').write_text('\n' * 3 + 'function serve() {}\n')
            (repo / 'leaks.mjs').write_text('\n' * 11 + 'function createHeld() {}\n')
            cli.git(repo, 'add', 'entry.mjs', 'leaks.mjs')
            cli.git(repo, 'commit', '-qm', 'tracked functions')
            revision = cli.sha(repo, 'HEAD')
            # A worktree edit cannot supply the source behind a pinned revision.
            (repo / 'leaks.mjs').write_text('function currentOnly() {}\n')
            capture = root / 'sample.heapsnapshot'
            capture.write_text(json.dumps(snapshot()))
            sha256 = cli.digest(capture)
            out = root / 'audit'
            with contextlib.redirect_stdout(io.StringIO()):
                trace_forensics.main([
                    '--input', str(capture), '--sha256', sha256, '--class-name', 'Held',
                    '--repo', str(repo), '--revision', revision, '--out', str(out),
                ])
            trial = cli.read(out / 'result.json')['trials']['baseline']
            self.assertEqual(trial['source_revision'], revision)
            self.assertEqual(trial['artifact']['sha256'], sha256)
            same_stack = trial['finding']['allocation']['representative']['stack']
            self.assertEqual((same_stack[-1]['source_file'], same_stack[-1]['line'],
                              same_stack[-1]['source_status']), ('leaks.mjs', 12, 'verified-git-source'))
            with contextlib.closing(sqlite3.connect(trial['database']['path'])) as db:
                self.assertEqual(db.execute('''
                    SELECT nodes.node_id, traces.trace_node_id, traces.parent_id,
                           functions.function_index, traces.recorded_allocations, traces.recorded_bytes
                    FROM heap_nodes AS nodes JOIN heap_allocation_traces AS traces USING(trace_node_id)
                    JOIN heap_allocation_functions AS functions USING(function_index)
                    WHERE nodes.label='Held' ORDER BY nodes.node_id
                ''').fetchall(), [
                    (2, 4, 2, 2, 10000, 800000),
                    (3, 4, 2, 2, 10000, 800000),
                    (4, 5, 1, 3, 7000, 700000),
                    (6, 6, 1, 4, 9000, 900000),
                ])
                self.assertEqual(db.execute('SELECT sha256 FROM capture_identity').fetchone(), (sha256,))
