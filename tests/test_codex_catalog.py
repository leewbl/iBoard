import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model import codex_catalog


SERVER = '''#!/usr/bin/env python3
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        assert request['params']['clientInfo'] == {'name':'iboard','title':'iBoard','version':'1.0.0'}
        print(json.dumps({'id':request['id'],'result':{}}), flush=True)
    elif method == 'model/list':
        mode = os.environ.get('CATALOG_FIXTURE')
        if mode == 'timeout': time.sleep(5)
        if mode == 'oversize':
            print('x' * 1000100, flush=True)
            continue
        cursor = request['params'].get('cursor')
        assert request['params']['includeHidden'] is False and request['params']['limit'] == 50
        model = {'model':'gpt-first' if not cursor else 'gpt-second', 'hidden':False,
                 'isDefault':not bool(cursor), 'supportedReasoningEfforts':[{'reasoningEffort':'high'},
                 {'reasoningEffort':'medium'}, {'reasoningEffort':'unsupported'}]}
        rows = [model, {'model':'not-gpt', 'hidden':False, 'isDefault':False, 'supportedReasoningEfforts':[]},
                {'model':'gpt-hidden', 'hidden':True, 'isDefault':False, 'supportedReasoningEfforts':[]}]
        next_cursor = 'page-two' if not cursor or mode == 'cycle' else None
        if mode == 'malformed': rows = [{'model': 'gpt-broken'}]
        print(json.dumps({'id':request['id'],'result':{'data':rows,'nextCursor':next_cursor}}), flush=True)
'''


class CodexCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'codex'
        path.write_text(SERVER)
        path.chmod(0o700)
        self.env = patch.dict(os.environ, {'PATH': self.temp.name + os.pathsep + os.environ['PATH']})
        self.env.start()
        self.addCleanup(self.env.stop)
        # The production environment is deliberately allowlisted.
        self.allow = patch.object(codex_catalog.codex_review, 'ENV', (*codex_catalog.codex_review.ENV, 'CATALOG_FIXTURE'))
        self.allow.start()
        self.addCleanup(self.allow.stop)

    def test_paginated_catalog_preserves_order_and_supported_efforts(self):
        with patch.dict(os.environ, {'CATALOG_FIXTURE': 'ok'}):
            self.assertEqual(codex_catalog.list_models(), [
                {'model':'gpt-first', 'efforts':['medium','high'], 'default':True},
                {'model':'gpt-second', 'efforts':['medium','high'], 'default':False}])

    def test_rejects_bad_pages_cycles_oversize_and_timeout(self):
        for mode in ('malformed', 'cycle', 'oversize', 'timeout'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'CATALOG_FIXTURE': mode}):
                with self.assertRaises(codex_catalog.CatalogError):
                    codex_catalog.list_models(timeout=0.3 if mode == 'timeout' else 5)


if __name__ == '__main__':
    unittest.main()
