import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model import glm_vision


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'glm.ini').write_text('test-secret')
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps(
            {'env': {'ANTHROPIC_BASE_URL': 'https://open.bigmodel.cn/api/anthropic'}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_vision_request_uses_vision_model_and_redacts_secret(self):
        captured = {}
        def open_request(request, timeout):
            captured['body'] = json.loads(request.data)
            return io.BytesIO(json.dumps({'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': 'visible message test-secret'}]}).encode())
        with patch.object(glm_vision, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root):
            result = glm_vision.describe(b'\x89PNG\r\n\x1a\nexample', 30, opener=open_request)
        self.assertEqual(captured['body']['model'], 'glm-5.3-flash')
        self.assertEqual(captured['body']['messages'][0]['content'][0]['source']['media_type'], 'image/png')
        self.assertNotIn('test-secret', result)

    def test_rejects_unrecognized_image_and_incomplete_response(self):
        with self.assertRaises(ValueError):
            glm_vision.image_type(b'not an image')
        def incomplete(request, timeout):
            return io.BytesIO(b'{"stop_reason":"max_tokens","content":[{"type":"text","text":"partial"}]}')
        with patch.object(glm_vision, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                glm_vision.describe(b'\x89PNG\r\n\x1a\nexample', 30, opener=incomplete)
