import unittest

from model.provider_limits import classify_provider_limit


ACTUAL_QUOTA = ('API Error: Request rejected (429) · [1308]'
                '[已达到 5 小时的使用上限。您的限额将在 2026-11-02 19:15:46 重置。][request-id]')


class ProviderLimitTests(unittest.TestCase):
    def test_actual_stdout_quota_with_benign_model_warning(self):
        result = {'exit_code': 1, 'timed_out': False, 'stdout': ACTUAL_QUOTA,
                  'stderr': 'Warning: unrecognized model glm-test\n'}
        original = dict(result)
        self.assertEqual(classify_provider_limit(result)['reason'], 'http_429')
        self.assertEqual(result, original)

    def test_known_failed_provider_error_envelopes(self):
        cases = [
            {'status_code': 429}, {'status': 429}, {'error': {'status': 429}},
            {'stdout': '{"error":{"type":"insufficient_quota","message":"no credits"}}'},
            {'stdout': '{"error":{"message":"usage limit reached"}}'},
            {'stderr': 'API Error: credit balance is too low'},
            {'stdout': 'API Error: out of credits'},
            {'stdout': 'API Error: usage limit reached'},
            {'stdout': 'API Error: HTTP 429 Too Many Requests'},
            {'stdout': 'API Error: 429 {"error":{"type":"rate_limit_error"}}'},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertIsNotNone(classify_provider_limit(
                    {'exit_code': 1, 'timed_out': False, **fields}))

    def test_success_quoted_source_and_transient_failures_are_not_limits(self):
        cases = [
            {'exit_code': 0, 'stdout': ACTUAL_QUOTA},
            {'exit_code': 0, 'status_code': 429},
            {'exit_code': 1, 'stdout': 'Source example:\n' + ACTUAL_QUOTA},
            {'exit_code': 1, 'stdout': '```text\n' + ACTUAL_QUOTA + '\n```'},
            {'exit_code': 1, 'stdout': 'The test fixture contains insufficient_quota and HTTP 429.'},
            {'exit_code': 1, 'stdout': 'Error: source snippet says HTTP 429'},
            {'exit_code': 1, 'stdout': 'Error: source quotes usage limit reached'},
            {'exit_code': 1, 'stdout': 'API Error: HTTP 503 Service Unavailable'},
            {'exit_code': 1, 'stderr': 'Connection reset by peer'},
            {'exit_code': 1, 'stderr': 'Warning: unrecognized model glm-test'},
            {'exit_code': None, 'timed_out': True, 'stdout': ACTUAL_QUOTA},
        ]
        for result in cases:
            with self.subTest(result=result):
                self.assertIsNone(classify_provider_limit(result))
