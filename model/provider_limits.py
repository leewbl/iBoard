"""Recognize failed provider limit responses without interpreting model/source text.

Classification is a passive stop signal, never a retry schedule or cost estimate.
Only failed, non-timeout adapter results and top-level error envelopes qualify.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re


class ProviderLimitError(Exception):
    """A persisted model result requires stopping further provider/workflow calls."""
    def __init__(self, *, report_path, result, signal, phase):
        self.report_path = Path(report_path) if report_path is not None else None
        self.result = deepcopy(result)
        self.signal = dict(signal)
        self.phase = phase
        super().__init__(f'{phase} provider limit; stop without retry; output saved at {self.report_path}')


def raise_http_provider_limit(error, *, secret, phase):
    """Propagate an HTTP 429 even if its body is absent or is not JSON.

    Raw response text follows the adapters' existing credential-redaction rule.
    The task owner attaches the persisted report path before exposing the stop.
    """
    if error.code != 429:
        return
    result = {'exit_code': 1, 'timed_out': False, 'status_code': error.code,
              'stdout': '', 'stderr': ''}
    try:
        result['stdout'] = error.read().decode('utf-8', errors='replace').replace(secret, '[REDACTED]')
    except (OSError, ValueError) as body_error:
        result['body_capture_error'] = type(body_error).__name__
    raise ProviderLimitError(report_path=None, result=result,
                             signal={'kind': 'provider_limit', 'reason': 'http_429'}, phase=phase)


_CODES = frozenset({'insufficient_quota', 'quota_exceeded', 'rate_limit_error',
                    'rate_limit_exceeded', 'usage_limit_reached', 'credit_balance_too_low',
                    'billing_hard_limit_reached', 'insufficient_credits'})
_QUOTA = re.compile(r'\binsufficient[_ ](?:quota|credits?)\b|\bout of credits\b|'
                    r'\bcredit balance\b.{0,40}\b(?:low|exhausted)\b|'
                    r'\b(?:usage|rate)[_ ]limit(?:[_ ](?:reached|exceeded))?\b|'
                    r'\bquota[_ ](?:exceeded|exhausted)\b|\bbilling_hard_limit_reached\b|'
                    r'已达到.{0,60}使用上限|余额不足|额度.{0,20}(?:耗尽|不足)', re.I)
_HTTP_429 = re.compile(r'^429\b|\bHTTP(?:/\d(?:\.\d)?)?\s*[: ]\s*429\b|'
                       r'\b(?:status(?:_code)?|code)\s*[:=]\s*429\b|'
                       r'\brequest\s+rejected\s*\(\s*429\s*\)', re.I)
_ENVELOPE = re.compile(r'^(?:API Error|Provider Error|HTTPError):\s*(.*)\Z', re.I | re.S)


def _structured(error, *, provider_error=False):
    if not isinstance(error, dict):
        return None
    for name in ('status', 'status_code', 'http_status', 'code'):
        if error.get(name) in (429, '429'):
            return {'kind': 'provider_limit', 'reason': 'http_429'}
    for name in ('type', 'code'):
        if isinstance(error.get(name), str) and error[name].lower() in _CODES:
            return {'kind': 'provider_limit', 'reason': 'quota_or_usage_limit'}
    if provider_error and isinstance(error.get('message'), str) and _QUOTA.search(error['message']):
        return {'kind': 'provider_limit', 'reason': 'quota_or_usage_limit'}
    # The nested error is a provider envelope, not arbitrary result/source fields.
    return _structured(error.get('error'), provider_error=True)


def classify_provider_limit(result):
    """Return a small stop signal or None; never alter the adapter result.

    Successful output, timeouts, quoted examples and transient connectivity errors
    are not evidence of a provider limit. No reset time/zone, tokens or cost inferred.
    """
    if (not isinstance(result, dict) or type(result.get('exit_code')) is not int
            or result['exit_code'] == 0 or result.get('timed_out')):
        return None
    signal = _structured(result)
    if signal:
        return signal
    for name in ('stdout', 'stderr'):
        raw = result.get(name)
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict) and isinstance(value.get('error'), dict):
            signal = _structured(value['error'], provider_error=True)
            if signal:
                return signal
        match = _ENVELOPE.fullmatch(text)
        if match:
            message = match.group(1)
            if _HTTP_429.search(message):
                return {'kind': 'provider_limit', 'reason': 'http_429'}
            if _QUOTA.search(message):
                return {'kind': 'provider_limit', 'reason': 'quota_or_usage_limit'}
    return None
