import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'model/specimen_files.py'


class SpecimenFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'edit.txt').write_text('old')
        (self.workspace / 'read.txt').write_text('read only')
        (self.workspace / 'link.txt').symlink_to(self.workspace / 'read.txt')
        self.trace = self.root / 'trace.jsonl'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'workspace': str(self.workspace), 'files': ['edit.txt', 'read.txt', 'link.txt'],
                                           'editable': ['edit.txt'], 'trace': str(self.trace)}))

    def run_server(self, requests):
        return subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), str(self.config)],
                              input=''.join(json.dumps(row) + '\n' for row in requests),
                              text=True, capture_output=True, timeout=5)

    @staticmethod
    def call(ident, name, args):
        return {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                'params': {'name': name, 'arguments': args}}

    def test_negotiation_read_write_and_refusals(self):
        req = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
               {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
               {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
               {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'},
               self.call(4, 'read_file', {'path': 'read.txt'}),
               self.call('5', 'write_file', {'path': 'edit.txt', 'text': 'changed'}),
               self.call(6, 'write_file', {'path': 'read.txt', 'text': 'no'}),
               self.call(7, 'read_file', {'path': '../config.json'}),
               self.call(8, 'read_file', {'path': 'link.txt'}),
               self.call(9, 'write_file', {'path': 'edit.txt', 'text': 'x' * 12001})]
        run = self.run_server(req)
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = {r['id']: r['result'] for r in map(json.loads, run.stdout.splitlines())}
        self.assertEqual(rows[1]['protocolVersion'], '2025-03-26')
        self.assertEqual({x['name'] for x in rows[2]['tools']}, {'read_file', 'write_file'})
        self.assertEqual(rows[3], {})
        self.assertEqual(rows[4]['content'][0]['text'], 'read only')
        self.assertFalse(rows['5']['isError'])
        self.assertTrue(all(rows[i]['isError'] for i in (6, 7, 8, 9)))
        self.assertEqual((self.workspace / 'edit.txt').read_text(), 'changed')
        receipts = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([r['id'] for r in receipts], ['4', '5', '6', '7', '8', '9'])
        self.assertEqual([r['ok'] for r in receipts], [True, True, False, False, False, False])
        self.assertEqual(receipts[2]['path'], '')
        self.assertEqual(receipts[3]['path'], '')
        self.assertNotIn('changed', self.trace.read_text())
        self.assertEqual(self.run_server([]).returncode, 1)  # exclusive receipt creation

    def test_call_bound_and_nested_symlink_rejection(self):
        (self.workspace / 'nested').symlink_to(self.root, target_is_directory=True)
        config = json.loads(self.config.read_text())
        config['files'].append('nested/config.json')
        self.config.write_text(json.dumps(config))
        req = [self.call(0, 'read_file', {'path': 'nested/config.json'})]
        req.extend(self.call(i, 'read_file', {'path': 'read.txt'}) for i in range(1, 41))
        run = self.run_server(req)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(len(self.trace.read_text().splitlines()), 40)
        self.assertTrue(json.loads(run.stdout.splitlines()[0])['result']['isError'])
        self.assertTrue(json.loads(run.stdout.splitlines()[-1])['result']['isError'])


if __name__ == '__main__':
    unittest.main()
