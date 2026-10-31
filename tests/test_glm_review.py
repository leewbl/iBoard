import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model import glm_review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'glm.ini').write_text('test-secret')
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'https://open.bigmodel.cn/api/anthropic',
            'ANTHROPIC_DEFAULT_SONNET_MODEL': 'glm-test'}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_evidence_and_token_redaction(self):
        def opener(request, timeout):
            self.assertEqual(timeout, 12)
            payload = json.loads(request.data)
            self.assertEqual(payload['messages'][0]['content'], 'evidence')
            self.assertEqual(payload['thinking']['type'], 'disabled')
            return io.BytesIO(json.dumps({'stop_reason':'end_turn','content':[{'type':'text','text':'test-secret result'}]}).encode())
        with patch.object(glm_review, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(glm_review.review('evidence', 12, opener=opener), '[REDACTED] result')

    def test_empty_or_truncated_review_is_rejected(self):
        def opener(request, timeout):
            return io.BytesIO(b'{"stop_reason":"max_tokens","content":[]}')
        with patch.object(glm_review, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root):
            with self.assertRaises(RuntimeError):
                glm_review.review('evidence', opener=opener)
