from hashlib import sha256
import json
import unittest

from workbench import source_maps


ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
BASE_URL = 'http://127.0.0.1:18789/assets/bundle.js.map'
SOURCE_A = 'start();\ncontinueWork();\n'
SOURCE_B = 'finish();\nreturn;\n'


def _encode(value):
    bits = abs(value) * 2 + (value < 0)
    digits = []
    while bits >= 32:
        digits.append(ALPHABET[32 + bits % 32])
        bits //= 32
    return ''.join(digits) + ALPHABET[bits]


def _row(*segments):
    return ','.join(''.join(_encode(value) for value in segment) for segment in segments)


def _map(mappings, *, sources=None, contents=None, **extra):
    return {'version': 3, 'sources': ['first.ts', 'second.ts'] if sources is None else sources,
            'sourcesContent': [SOURCE_A, SOURCE_B] if contents is None else contents,
            'mappings': mappings, **extra}


def _section(line, column, document):
    return {'offset': {'line': line, 'column': column}, 'map': document}


class SourceMapTests(unittest.TestCase):
    def setUp(self):
        self.resolutions = []

    def resolve(self, base, root, source, content):
        self.resolutions.append((base, root, source, content))
        if source == 'untracked.ts':
            return None
        return 'src/' + source, sha256(content.encode('utf-8')).hexdigest()

    def decode(self, document):
        return source_maps.decode(json.dumps(document).encode(), BASE_URL, self.resolve)

    def test_uses_columns_on_the_same_line_and_preserves_named_deltas(self):
        first = _row((0, 0, 0, 1, 0), (4, 0, 0, 3, 1), (3,), (2, 1, 0, -4))
        third = _row((2, -1, 1, 2, -1))
        decoded = self.decode(_map(first + ';;' + third, names=['start', 'choose'], sourceRoot='../src'))

        self.assertEqual(decoded.lookup(1, 4), {
            'source_file': 'src/first.ts', 'source_line': 1, 'source_column': 2,
            'source_symbol': 'start', 'source_sha256': sha256(SOURCE_A.encode()).hexdigest(),
        })
        self.assertEqual(decoded.lookup(1, 5)['source_symbol'], 'choose')
        self.assertEqual(decoded.lookup(1, 7)['source_column'], 5)
        self.assertIsNone(decoded.lookup(1, 8))
        self.assertEqual(decoded.lookup(1, 10)['source_file'], 'src/second.ts')
        self.assertIsNone(decoded.lookup(1, 10)['source_symbol'])
        self.assertIsNone(decoded.lookup(2, 25))
        self.assertIsNone(decoded.lookup(3, 1))
        self.assertEqual(decoded.lookup(3, 3)['source_line'], 2)
        self.assertEqual(decoded.lookup(3, 3)['source_symbol'], 'start')
        self.assertIsNone(decoded.lookup(1, None))
        self.assertIsNone(decoded.lookup(1, True))
        self.assertEqual(self.resolutions, [
            (BASE_URL, '../src', 'first.ts', SOURCE_A), (BASE_URL, '../src', 'second.ts', SOURCE_B),
        ])

    def test_missing_or_unverified_content_breaks_the_previous_anchor(self):
        decoded = self.decode(_map(_row((0, 0, 0, 0), (2, 1, 0, 0), (2, 1, 0, 0), (2, 1, 0, 0)),
                                   sources=['first.ts', 'untracked.ts', 'missing.ts', None],
                                   contents=[SOURCE_A, SOURCE_B, None, SOURCE_B]))
        self.assertEqual(decoded.lookup(1, 2)['source_file'], 'src/first.ts')
        for column in range(3, 9):
            self.assertIsNone(decoded.lookup(1, column))
        self.assertEqual([(item[2], item[3]) for item in self.resolutions],
                         [('first.ts', SOURCE_A), ('untracked.ts', SOURCE_B)])

    def test_original_bounds_follow_utf16_and_javascript_line_terminators(self):
        decoded = self.decode(_map(_row((0, 0, 0, 2), (3, 0, 1, -2), (3, 0, 1, 1)),
                                   sources=['first.ts'], contents=['😀x\r\ny\u2028z']))
        self.assertEqual(decoded.lookup(1, 1)['source_column'], 3)
        self.assertEqual(decoded.lookup(1, 4)['source_line'], 2)
        self.assertEqual(decoded.lookup(1, 7)['source_line'], 3)
        with self.assertRaisesRegex(source_maps.SourceMapError, 'source bounds'):
            self.decode(_map(_row((0, 0, 0, 4)), sources=['first.ts'], contents=['😀x']))

    def test_nested_index_boundaries_and_multiline_offsets(self):
        nested = {'version': 3, 'sections': [
            _section(0, 2, _map('ACAA;EACA')),
        ]}
        decoded = self.decode({'version': 3, 'sections': [
            _section(0, 0, _map('AAAA')), _section(0, 4, nested),
        ]})
        self.assertEqual(decoded.lookup(1, 4)['source_file'], 'src/first.ts')
        self.assertIsNone(decoded.lookup(1, 5))
        self.assertIsNone(decoded.lookup(1, 6))
        self.assertEqual(decoded.lookup(1, 7)['source_file'], 'src/second.ts')
        self.assertIsNone(decoded.lookup(2, 2))
        self.assertEqual(decoded.lookup(2, 3)['source_line'], 2)

    def test_indexes_reject_overlap_unordered_sections_and_external_maps(self):
        overlapping = [_section(0, 0, _map('GAAA')), _section(0, 2, _map('AAAA'))]
        unordered = [_section(1, 0, _map('AAAA')), _section(0, 2, _map('AAAA'))]
        external = [{'offset': {'line': 0, 'column': 0}, 'url': 'other.js.map'}]
        for sections in (overlapping, unordered, external):
            with self.subTest(sections=sections), self.assertRaises(source_maps.SourceMapError):
                self.decode({'version': 3, 'sections': sections})

    def test_sorts_unordered_positions_and_accepts_identical_anchors(self):
        decoded = self.decode(_map(_row((3, 0, 0, 1), (-3, 1, 0, -1), (0, 0, 0, 0))))
        self.assertEqual(decoded.lookup(1, 1)['source_file'], 'src/second.ts')
        self.assertEqual(decoded.lookup(1, 4)['source_file'], 'src/first.ts')

    def test_conflicting_anchors_leave_only_the_ambiguous_span_unresolved(self):
        decoded = self.decode(_map(_row((0, 0, 0, 0), (2, 0, 0, 0), (0, 1, 0, 0), (3, 0, 1, 0))))
        self.assertEqual(decoded.lookup(1, 2)['source_file'], 'src/first.ts')
        for column in (3, 4, 5):
            self.assertIsNone(decoded.lookup(1, column))
        self.assertEqual(decoded.lookup(1, 6)['source_file'], 'src/second.ts')

    def test_rejects_truncated_overflowing_and_invalid_segments(self):
        invalid_segments = ('AAAA,g', 'ggggggEAAA', 'AAAA,,A', '!!', 'AACAQ', 'AAGA')
        for segment in invalid_segments:
            with self.subTest(segment=segment), self.assertRaises(source_maps.SourceMapError):
                self.decode(_map(segment))

    def test_rejects_over_limit_and_ambiguous_json(self):
        invalid = (b'', b' ' * (source_maps.MAX_MAP_BYTES + 1), b'{"version":3,"version":3}', b'{"bad":NaN}')
        for raw in invalid:
            with self.subTest(prefix=raw[:100]), self.assertRaises(source_maps.SourceMapError):
                source_maps.decode(raw, BASE_URL, self.resolve)

    def test_http_legacy_prefix_and_utf8_byte_order_mark(self):
        raw = json.dumps(_map('AAAA')).encode()
        for prefix in (b")]}' previously used prefix\r\n", b'\xef\xbb\xbf'):
            with self.subTest(prefix=prefix):
                decoded = source_maps.decode(prefix + raw, BASE_URL, self.resolve)
                self.assertEqual(decoded.lookup(1, 1)['source_file'], 'src/first.ts')


if __name__ == '__main__':
    unittest.main()
