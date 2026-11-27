"""Resolve generated JavaScript positions from bounded, captured ECMA-426 source maps."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import json
import re
from typing import NamedTuple


MAX_MAP_BYTES = 8 << 20
MAX_MAPPINGS = 500_000
MAX_INDEX_DEPTH = 8
MAX_POSITION = 2**31 - 1
LINE_BREAKS = re.compile(r'\r\n|[\r\n\u2028\u2029]')
BASE64_DIGITS = {char: i for i, char in enumerate(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/')}
AMBIGUOUS = object()


class SourceMapError(ValueError):
    """The captured map cannot safely support source attribution."""


class _Location(NamedTuple):
    source_file: str
    source_line: int
    source_column: int
    source_symbol: str | None
    source_sha256: str


class _Anchor(NamedTuple):
    column: int
    original: _Location | None
    boundary: bool = False


class SourceMap:
    """A 1-based view of verified anchors grouped by generated line."""

    def __init__(self, lines):
        self._lines = {line: (tuple(anchor.column for anchor in anchors),
                              tuple(anchor.original for anchor in anchors))
                       for line, anchors in lines.items()}

    def lookup(self, line, column):
        """Return the original position of the closest eligible anchor on this line."""
        if type(line) is not int or type(column) is not int or line < 1 or column < 1:
            return None
        columns, originals = self._lines.get(line - 1, ((), ()))
        index = bisect_right(columns, column - 1) - 1
        if index >= 0 and (original := originals[index]) is not None:
            return original._asdict()
        return None


def _json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise SourceMapError('source map JSON contains duplicate keys')
    return result


def _reject_constant(value):
    raise SourceMapError(f'source map JSON contains invalid number {value}')


def _deltas(segment):
    """Decode the sign and continuation bits of each 32-bit Base64 VLQ."""
    values = []
    shift = number = 0
    for char in segment:
        digit = BASE64_DIGITS.get(char)
        if digit is None or shift > 30:
            raise SourceMapError('invalid source map VLQ')
        number |= (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        if number >= 2**32:
            raise SourceMapError('source map VLQ exceeds 32 bits')
        value = number >> 1
        if number & 1:
            value = -value if value else -(2**31)
        values.append(value)
        shift = number = 0
        if len(values) > 5:
            raise SourceMapError('source map segment has too many fields')
    if shift or len(values) not in (1, 4, 5):
        raise SourceMapError('incomplete source map segment')
    return values


def _position(value):
    if type(value) is not int or not 0 <= value <= MAX_POSITION:
        raise SourceMapError('source map position is outside its coordinate range')
    return value


def _offset(start, line, column):
    return _position(start[0] + line), _position(column + (start[1] if line == 0 else 0))


def _string_array(value, *, optional_items=False):
    if not isinstance(value, list) or any(
            not isinstance(item, str) and not (optional_items and item is None) for item in value):
        raise SourceMapError('invalid source map string table')
    return value


def _resolve_sources(document, base_url, resolve_source):
    root = document.get('sourceRoot')
    if root is not None and not isinstance(root, str):
        raise SourceMapError('sourceRoot must be a string when present')
    sources = _string_array(document.get('sources'), optional_items=True)
    contents = _string_array(document.get('sourcesContent', []), optional_items=True)
    resolved = []
    for index, source in enumerate(sources):
        content = contents[index] if index < len(contents) else None
        try:
            proof = resolve_source(base_url, root, source, content) if source is not None and content is not None else None
            if proof is None:
                resolved.append(None)
                continue
            path, digest = proof
            widths = [len(line.encode('utf-16-le')) // 2 for line in LINE_BREAKS.split(content)]
        except UnicodeError as exc:
            raise SourceMapError('original source must contain valid Unicode') from exc
        resolved.append((path, digest, widths))
    return resolved


def _regular_anchors(document, base_url, resolve_source):
    mappings = document.get('mappings')
    if not isinstance(mappings, str):
        raise SourceMapError('source map mappings must be a string')
    sources = _resolve_sources(document, base_url, resolve_source)
    names = _string_array(document.get('names', []))
    source_index = original_line = original_column = name_index = 0
    for line_number, line in enumerate(mappings.split(';')):
        if not line:
            continue
        column = 0
        segments = {}
        for segment in line.split(','):
            fields = _deltas(segment)
            column = _position(column + fields[0])
            original = None
            if len(fields) >= 4:
                source_index += fields[1]
                if not 0 <= source_index < len(sources):
                    raise SourceMapError('source map source index is outside its table')
                original_line = _position(original_line + fields[2])
                original_column = _position(original_column + fields[3])
                symbol = None
                if len(fields) == 5:
                    name_index += fields[4]
                    if not 0 <= name_index < len(names):
                        raise SourceMapError('source map name index is outside its table')
                    symbol = names[name_index]
                original = (source_index, original_line, original_column, symbol)
            # ECMA-426 allows multiple original records for a generated offset.
            # Conflicting records act as a barrier for single-location citations.
            segments[column] = AMBIGUOUS if column in segments and segments[column] != original else original
        for column, original in sorted(segments.items()):
            location = None
            if original is not None and original is not AMBIGUOUS:
                index, source_line, offset, symbol = original
                if sources[index] is not None:
                    path, digest, widths = sources[index]
                    if source_line >= len(widths) or offset > widths[source_line]:
                        raise SourceMapError('source map original position exceeds verified source bounds')
                    symbol = symbol if symbol and symbol.isprintable() and len(symbol) <= 240 else None
                    location = _Location(path, source_line + 1, offset + 1, symbol, digest)
            yield line_number, column, location


def _sections(document, start):
    sections = document['sections']
    if not isinstance(sections, list) or any(key in document for key in ('sources', 'mappings', 'sourcesContent')):
        raise SourceMapError('invalid source map index')
    entries = []
    for section in sections:
        if (not isinstance(section, dict) or 'url' in section or not isinstance(section.get('map'), dict)
                or not isinstance(section.get('offset'), dict)):
            raise SourceMapError('source map index sections must embed a map and position')
        offset = section['offset']
        position = _offset(start, _position(offset.get('line')), _position(offset.get('column')))
        if entries and position <= entries[-1][0]:
            raise SourceMapError('source map index section positions must strictly increase')
        entries.append((position, section['map']))
    return entries


def _anchors(document, base_url, resolve_source, start=(0, 0), stop=None, depth=0):
    if not isinstance(document, dict) or type(document.get('version')) is not int or document['version'] != 3:
        raise SourceMapError('expected a version 3 source map')
    if 'sections' not in document:
        for line, column, original in _regular_anchors(document, base_url, resolve_source):
            position = _offset(start, line, column)
            if stop is not None and position >= stop:
                raise SourceMapError('source map index sections overlap')
            yield position, _Anchor(position[1], original)
        return
    if depth >= MAX_INDEX_DEPTH:
        raise SourceMapError('source map index nesting exceeds its limit')
    sections = _sections(document, start)
    for index, (position, child) in enumerate(sections):
        if stop is not None and position >= stop:
            raise SourceMapError('source map index sections overlap')
        end = sections[index + 1][0] if index + 1 < len(sections) else stop
        # The previous section cannot describe positions before this section's first anchor.
        yield position, _Anchor(position[1], None, boundary=True)
        yield from _anchors(child, base_url, resolve_source, position, end, depth + 1)


def decode(raw, base_url, resolve_source):
    """Decode a capture and use the resolver to prove embedded sources against Git.

    ``resolve_source(base_url, source_root, source, content)`` returns the exact
    tracked path and source SHA-256, or ``None`` when proof is unavailable.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MAP_BYTES:
        raise SourceMapError('source map exceeds its byte limit or is empty')
    if base_url.startswith(('http://', 'https://')) and raw.startswith(b")]}'"):
        # HTTP servers may send the legacy anti-XSSI prefix described in ECMA-426.
        raw = re.split(br'[\r\n]', raw, maxsplit=1)[-1]
    try:
        document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_json_object,
                              parse_constant=_reject_constant)
    except SourceMapError:
        raise
    except (ValueError, RecursionError) as exc:
        raise SourceMapError('invalid source map JSON') from exc

    lines = defaultdict(list)
    for count, ((line, column), anchor) in enumerate(_anchors(document, base_url, resolve_source), 1):
        if count > MAX_MAPPINGS:
            raise SourceMapError('source map exceeds its mapping count limit')
        row = lines[line]
        if row and row[-1].column == column and row[-1].boundary:
            row[-1] = anchor
        else:
            row.append(anchor)
    return SourceMap(lines)
