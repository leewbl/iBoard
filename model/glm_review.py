"""Bounded, tool-free GLM review of explicitly supplied evidence."""
from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlparse

from model.claude_glm import ROOT


def review(prompt: str, timeout: int = 60, *, opener=urllib.request.urlopen):
    if not prompt.strip() or len(prompt) > 100_000 or not 1 <= timeout <= 300:
        raise ValueError('invalid review prompt or timeout')
    key = (ROOT / 'glm.ini').read_text(encoding='utf-8').strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError('invalid GLM key file')
    env = json.loads((Path.home() / '.claude/settings.json').read_text(encoding='utf-8'))['env']
    base = env['ANTHROPIC_BASE_URL'].rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or parsed.hostname not in {'open.bigmodel.cn', 'api.z.ai'} or parsed.username or parsed.password:
        raise ValueError('unsupported GLM endpoint')
    model = env['ANTHROPIC_DEFAULT_SONNET_MODEL'].split('[', 1)[0]
    body = json.dumps({'model': model, 'max_tokens': 2048, 'thinking': {'type': 'disabled'},
                       'messages': [{'role': 'user', 'content': prompt}]}).encode()
    request = urllib.request.Request(base + '/v1/messages', data=body,
              headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    try:
        with opener(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'GLM returned HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f'GLM request failed: {type(error).__name__}') from None
    text = '\n'.join(item.get('text', '') for item in result.get('content', []) if item.get('type') == 'text')
    if not text.strip() or result.get('stop_reason') != 'end_turn':
        raise RuntimeError('GLM review was empty or incomplete')
    return text.replace(key, '[REDACTED]')
