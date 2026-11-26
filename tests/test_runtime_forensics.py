import unittest

from workbench import runtime_forensics as forensics


class RuntimeForensicsTests(unittest.TestCase):
    def test_cpu_aggregates_only_source_verified_leaf_frames(self):
        data = {'nodes': [
            {'id': 1, 'callFrame': {'functionName': 'hotLoop', 'url': 'file:///app/src/work.mjs', 'lineNumber': 2}},
            {'id': 2, 'callFrame': {'functionName': 'hotLoop', 'url': 'file:///app/src/work.mjs', 'lineNumber': 2}},
            {'id': 3, 'callFrame': {'functionName': 'foreign', 'url': 'file:///other/work.mjs', 'lineNumber': 0}},
            {'id': 4, 'callFrame': {'functionName': 'untracked', 'url': 'file:///app/missing.mjs', 'lineNumber': 0}},
        ], 'samples': [1, 2, 3, 4], 'timeDeltas': [1000, 3000, 1000, 1000]}
        report = forensics.summarize_cpu(data, lambda path: 8 if path == 'src/work.mjs' else 0)
        self.assertEqual(report['sampled_ms'], 6)
        self.assertEqual(report['hot_frames'], [{'symbol': 'hotLoop', 'path': 'src/work.mjs', 'line': 3,
                                                'sampled_ms': 4, 'share_pct': 66.67}])
        data['samples'][0] = 99
        with self.assertRaisesRegex(forensics.ForensicsError, 'unknown frame'):
            forensics.summarize_cpu(data, lambda _: 0)

    def test_heap_ignores_weak_and_shortcut_edges_during_root_walk(self):
        # Synthetic root -> owner -> Held, with weak and shortcut alternatives.
        data = {'snapshot': {'node_count': 4, 'edge_count': 4, 'meta': {
            'node_fields': ['type', 'name', 'self_size', 'edge_count'],
            'edge_fields': ['type', 'name_or_index', 'to_node'],
            'node_types': [['synthetic', 'object'], 'string', 'number', 'number'],
            'edge_types': [['weak', 'shortcut', 'property'], 'string_or_number', 'node'],
        }}, 'nodes': [0, 0, 0, 3, 1, 1, 16, 1, 1, 2, 32, 0, 1, 2, 16, 0],
                'edges': [0, 3, 12, 1, 4, 8, 2, 5, 4, 2, 6, 8],
                'strings': ['', 'Owner', 'Held', 'weakOnly', 'shortcut', 'subject', 'held']}
        result = forensics.summarize_heap(data, 'Held')['selected_class']
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['shallow_bytes'], 48)
        self.assertEqual([part['edge'] for part in result['strong_root_path']], ['subject', 'held'])
        self.assertIsNone(forensics.summarize_heap(data, 'Absent')['selected_class']['strong_root_path'])
        data['edges'][8] = 99
        with self.assertRaisesRegex(forensics.ForensicsError, 'invalid destination'):
            forensics.summarize_heap(data, 'Held')
