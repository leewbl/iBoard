"""Import Chrome Trace Event intervals and DevTools CPU samples without opening trace URLs."""

from array import array
from collections import Counter, defaultdict
import heapq
import json
import math


MAX_EVENTS = 1_000_000
MAX_SAMPLES = 2_000_000
MAX_PROFILE_NODES = 500_000
MAX_DEPTH = 512
MAX_TIME_US = 10**15

_SCHEMA = """
CREATE TABLE trace_events (
    ordinal INTEGER PRIMARY KEY, pid INTEGER, tid INTEGER, phase TEXT NOT NULL,
    name TEXT, category TEXT, ts_us REAL, raw_duration_us REAL, thread_ts_us REAL,
    thread_duration_us REAL, scope TEXT, event_id TEXT, id2 TEXT, bind_id TEXT,
    symbol TEXT, path TEXT, line INTEGER
);
CREATE INDEX trace_events_thread_time ON trace_events (pid, tid, ts_us);
CREATE TABLE trace_threads (
    pid INTEGER, tid INTEGER, process_name TEXT, thread_name TEXT, events INTEGER NOT NULL,
    PRIMARY KEY (pid, tid)
);
CREATE TABLE trace_intervals (
    start_ordinal INTEGER PRIMARY KEY REFERENCES trace_events (ordinal),
    end_ordinal INTEGER REFERENCES trace_events (ordinal),
    duration_us REAL NOT NULL
);
CREATE VIEW trace_duration_totals AS
    SELECT e.category, e.name, e.symbol, e.path, e.line, COUNT(*) AS events,
           SUM(i.duration_us) AS total_us, MAX(i.duration_us) AS max_us
    FROM trace_intervals i JOIN trace_events e ON i.start_ordinal = e.ordinal
    GROUP BY e.category, e.name, e.symbol, e.path, e.line;
CREATE TABLE trace_profiles (
    profile_key INTEGER PRIMARY KEY, head_ordinal INTEGER NOT NULL REFERENCES trace_events (ordinal),
    pid INTEGER NOT NULL, tid INTEGER NOT NULL, event_id TEXT NOT NULL,
    header_ts_us REAL, declared_start_us REAL, source TEXT
);
CREATE TABLE trace_profile_frames (
    profile_key INTEGER NOT NULL REFERENCES trace_profiles (profile_key),
    node_id INTEGER NOT NULL, parent_id INTEGER, symbol TEXT, path TEXT, line INTEGER,
    PRIMARY KEY (profile_key, node_id)
);
CREATE TABLE trace_profile_samples (
    profile_key INTEGER NOT NULL REFERENCES trace_profiles (profile_key),
    ordinal INTEGER NOT NULL, chunk_ordinal INTEGER NOT NULL REFERENCES trace_events (ordinal),
    node_id INTEGER NOT NULL, delta_us REAL NOT NULL, offset_us REAL NOT NULL, weight_us REAL NOT NULL,
    PRIMARY KEY (profile_key, ordinal),
    FOREIGN KEY (profile_key, node_id) REFERENCES trace_profile_frames (profile_key, node_id)
);
CREATE INDEX trace_profile_samples_frames ON trace_profile_samples (profile_key, node_id);
CREATE VIEW trace_cpu_frame_totals AS
    SELECT f.symbol, f.path, f.line, COUNT(*) AS samples, SUM(s.weight_us) AS sampled_us
    FROM trace_profile_samples s
    JOIN trace_profile_frames f ON s.profile_key = f.profile_key AND s.node_id = f.node_id
    GROUP BY f.symbol, f.path, f.line;
CREATE TABLE trace_import_gaps (
    reason TEXT PRIMARY KEY, records INTEGER NOT NULL
);
"""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _integer(value):
    return value if type(value) is int and 0 <= value < 2**53 else None


def _text(value):
    if isinstance(value, str) and len(value) <= 4096 and '\x00' not in value:
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return None
        return value
    return None


def _time(value, *, duration=False):
    if type(value) not in (int, float) or not -MAX_TIME_US <= value <= MAX_TIME_US or not math.isfinite(value):
        return None
    return value if not duration or value >= 0 else None


def _identity(value):
    if _integer(value) is not None or _text(value) is not None:
        return json.dumps(value, ensure_ascii=True)
    return None


def _id2(value):
    if isinstance(value, dict) and value and set(value) <= {'local', 'global'}:
        if all(_identity(part) is not None for part in value.values()):
            return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return None


def _call_frame(value, *, first_line=0):
    frame = _mapping(value)
    line = _integer(frame.get('lineNumber'))
    path = _text(frame.get('url')) or None
    return (_text(frame.get('functionName')) or None, path,
            line + 1 - first_line if path and line is not None and line >= first_line else None)


def _explicit_frame(event, data):
    # FunctionCall has call-frame fields directly on data. Other events can
    # provide an explicit callFrame; an arbitrary event URL is not a frame.
    value = event.get('callFrame') or data.get('callFrame') or _mapping(event.get('args')).get('callFrame')
    if value is None and event.get('name') == 'FunctionCall':
        # Blink serializes a 1-based SourceLocation here. Profiler CallFrame
        # records use zero-based source coordinates.
        return _call_frame(data, first_line=1)
    return _call_frame(value)


def _import_intervals(db, problems):
    """Paired synchronous intervals follow timestamp order per thread."""
    db.execute("""
        INSERT INTO trace_intervals
        SELECT ordinal, ordinal, raw_duration_us FROM trace_events
        WHERE phase = 'X' AND raw_duration_us IS NOT NULL AND ts_us IS NOT NULL
    """)
    problems['invalid_duration'] = db.execute(
        "SELECT COUNT(*) FROM trace_events WHERE phase = 'X' AND (raw_duration_us IS NULL OR ts_us IS NULL)"
    ).fetchone()[0]
    paired = []
    stack = []
    thread = None
    events = db.execute("""
        SELECT ordinal, pid, tid, phase, name, category, ts_us, scope, event_id, id2
        FROM trace_events WHERE phase IN ('B', 'E') ORDER BY pid, tid, ts_us, ordinal
    """)
    for ordinal, pid, tid, phase, name, category, timestamp, scope, event_id, id2 in events:
        if (pid, tid) != thread:
            problems['unclosed_begin'] += len(stack)
            stack.clear()
            thread = pid, tid
        if pid is None or tid is None or timestamp is None:
            problems['invalid_boundary'] += 1
        elif phase == 'B':
            if len(stack) >= MAX_DEPTH:
                raise ValueError('Chrome trace synchronous nesting exceeds the limit')
            stack.append((ordinal, timestamp, (name, category, scope, event_id, id2)))
        elif not stack:
            problems['orphan_end'] += 1
        else:
            start, began, identity = stack[-1]
            end_identity = name, category, scope, event_id, id2
            if any(before and after and before != after for before, after in zip(identity, end_identity)):
                # A labeled end contradicting the thread's top begin cannot
                # legitimately close that begin or reach into its ancestors.
                problems['mismatched_end'] += 1
                continue
            stack.pop()
            paired.append((start, ordinal, timestamp - began))
            if len(paired) == 1000:
                db.executemany('INSERT INTO trace_intervals VALUES (?, ?, ?)', paired)
                paired.clear()
    problems['unclosed_begin'] += len(stack)
    db.executemany('INSERT INTO trace_intervals VALUES (?, ?, ?)', paired)


def _import_profiles(db, headers, chunks, problems):
    node_count = sample_count = reordered = 0
    for key, parts in chunks.items():
        if len(headers[key]) != 1:
            problems['incomplete_profile'] += 1
            continue
        head, pid, tid, header_ts, header = headers[key][0]
        if tid is None:
            problems['incomplete_profile'] += 1
            continue
        frames = {}
        parent_links = {}
        samples = []
        invalid = False
        for ordinal, data in parts:
            profile = _mapping(data.get('cpuProfile'))
            nodes = profile.get('nodes', [])
            ids, deltas = profile.get('samples', []), data.get('timeDeltas', [])
            if not isinstance(nodes, list) or not isinstance(ids, list) or not isinstance(deltas, list) or len(ids) != len(deltas):
                invalid = True
                break
            node_count += len(nodes)
            sample_count += len(ids)
            if node_count > MAX_PROFILE_NODES or sample_count > MAX_SAMPLES:
                raise ValueError('Chrome trace CPU profile counts exceed the limits')
            for node in nodes:
                node = _mapping(node)
                node_id, parent_id = _integer(node.get('id')), _integer(node.get('parent'))
                frame = _call_frame(node.get('callFrame'))
                if (node_id is None or (node_id in frames and frames[node_id] != frame) or
                        (parent_id is not None and node_id in parent_links and parent_links[node_id] != parent_id)):
                    invalid = True
                    break
                frames[node_id] = frame
                if parent_id is not None:
                    parent_links[node_id] = parent_id
                children = node.get('children', [])
                if not isinstance(children, list) or len(children) > MAX_PROFILE_NODES:
                    invalid = True
                    break
                for child in children:
                    child = _integer(child)
                    if child is None or (child in parent_links and parent_links[child] != node_id):
                        invalid = True
                        break
                    parent_links[child] = node_id
                if invalid:
                    break
            if invalid:
                break
            for node_id, delta in zip(ids, deltas):
                node_id, delta = _integer(node_id), _time(delta)
                if node_id is None or delta is None:
                    invalid = True
                    break
                samples.append((ordinal, node_id, delta))
            if invalid:
                break
        if invalid or any(node_id not in frames for _, node_id, _ in samples):
            problems['invalid_profile'] += 1
            continue
        if not samples:
            problems['incomplete_profile'] += 1
            continue
        offset = 0
        offsets = array('d')
        for _, _, delta in samples:
            offset += delta
            offsets.append(offset)
        if any(abs(offset) > MAX_TIME_US for offset in offsets):
            problems['invalid_profile'] += 1
            continue
        # Native Chromium deltas can be signed when samples arrive out of
        # time order. Keep original deltas and order, then attribute elapsed
        # intervals after reconstructing and ordering the sample offsets.
        negatives = sum(delta < 0 for _, _, delta in samples)
        reordered += negatives
        weights = array('d', [0]) * len(samples)
        order = sorted(range(len(samples)), key=offsets.__getitem__) if negatives else range(len(samples))
        previous = None
        for index in order:
            if previous is not None:
                # A sampled frame starts at its observed timestamp. Do not
                # attribute the gap before the capture or extrapolate its tail.
                weights[previous] = offsets[index] - offsets[previous]
            previous = index
        db.execute('INSERT INTO trace_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (head, head, pid, tid, key[1], header_ts, _time(header.get('startTime')), _text(header.get('source'))))
        db.executemany('INSERT INTO trace_profile_frames VALUES (?, ?, ?, ?, ?, ?)',
                       ((head, node_id, parent_links.get(node_id), *frame) for node_id, frame in frames.items()))
        db.executemany('INSERT INTO trace_profile_samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ((head, sample, *values, offsets[sample], weights[sample])
                        for sample, values in enumerate(samples)))
    problems['incomplete_profile'] += len(headers.keys() - chunks.keys())
    return reordered


def _hot_cpu_paths(db):
    hotspots = db.execute("""
        SELECT f.profile_key, f.node_id, p.pid, p.tid, COUNT(*), SUM(s.weight_us)
        FROM trace_profile_samples s
        JOIN trace_profile_frames f ON s.profile_key = f.profile_key AND s.node_id = f.node_id
        JOIN trace_profiles p ON f.profile_key = p.profile_key
        WHERE f.path IS NOT NULL AND f.line IS NOT NULL
        GROUP BY f.profile_key, f.node_id
        ORDER BY SUM(s.weight_us) DESC, f.profile_key, f.node_id LIMIT 10
    """)
    for profile, node, pid, tid, samples, sampled in hotspots:
        chain, seen = [], set()
        status = 'truncated'
        for _ in range(64):
            if node in seen:
                status = 'cycle'
                break
            seen.add(node)
            frame = db.execute("""
                SELECT parent_id, symbol, path, line FROM trace_profile_frames
                WHERE profile_key = ? AND node_id = ?
            """, (profile, node)).fetchone()
            if frame is None:
                status = 'missing-parent'
                break
            parent, symbol, path, line = frame
            chain.append({'node_id': node, 'symbol': symbol, 'path': path, 'line': line})
            if parent is None:
                status = 'root-observed' if symbol == '(root)' else 'no-recorded-parent'
                break
            node = parent
        yield {'profile_key': profile, 'pid': pid, 'tid': tid, 'samples': samples, 'sampled_us': sampled,
               'ancestry': status, 'frames': list(reversed(chain))}


def import_trace(db, payload):
    """Import one decoded trace, returning concise duration and sample aggregates."""
    events = payload.get('traceEvents') if isinstance(payload, dict) else payload
    if not isinstance(events, list) or not 0 < len(events) <= MAX_EVENTS:
        raise ValueError('Chrome trace needs a nonempty, bounded traceEvents list')
    problems = Counter()
    headers, chunks = defaultdict(list), defaultdict(list)
    process_names, thread_names, thread_events = {}, {}, Counter()

    def event_rows():
        for ordinal, event in enumerate(events):
            phase = _text(_mapping(event).get('ph'))
            if phase is None or len(phase) != 1:
                problems['invalid_event'] += 1
                continue
            pid, tid = (_integer(event.get(key)) for key in ('pid', 'tid'))
            name, category = (_text(event.get(key)) for key in ('name', 'cat'))
            timestamp = _time(event.get('ts'))
            args = _mapping(event.get('args'))
            data = _mapping(args.get('data'))
            event_id = _identity(event.get('id'))
            if pid is not None and tid is not None:
                thread_events[pid, tid] += 1
            if phase == 'M':
                label = _text(args.get('name'))
                if pid is not None and label is not None:
                    if name == 'process_name':
                        process_names[pid] = label
                    elif name == 'thread_name' and tid is not None:
                        thread_names[pid, tid] = label
            if phase == 'P' and name in ('Profile', 'ProfileChunk'):
                if pid is None or event_id is None:
                    problems['invalid_profile_event'] += 1
                elif name == 'Profile':
                    headers[pid, event_id].append((ordinal, pid, tid, timestamp, data))
                else:
                    chunks[pid, event_id].append((ordinal, data))
            yield (ordinal, pid, tid, phase, name, category, timestamp, _time(event.get('dur'), duration=True),
                   _time(event.get('tts')), _time(event.get('tdur'), duration=True), _text(event.get('s')),
                   event_id, _id2(event.get('id2')), _identity(event.get('bind_id')), *_explicit_frame(event, data))

    db.executescript(_SCHEMA)
    with db:
        db.executemany('INSERT INTO trace_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', event_rows())
        if problems['invalid_event'] == len(events):
            raise ValueError('Chrome trace contains no recognizable events')
        db.executemany('INSERT INTO trace_threads VALUES (?, ?, ?, ?, ?)',
                       ((pid, tid, process_names.get(pid), thread_names.get((pid, tid)), count)
                        for (pid, tid), count in thread_events.items()))
        _import_intervals(db, problems)
        reordered = _import_profiles(db, headers, chunks, problems)
        db.executemany('INSERT INTO trace_import_gaps VALUES (?, ?)',
                       ((reason, count) for reason, count in problems.items() if count))

    duration_groups = [
        dict(zip(('category', 'name', 'symbol', 'path', 'line', 'events', 'total_us', 'max_us'), row))
        for row in db.execute("""
            SELECT * FROM trace_duration_totals
            ORDER BY total_us DESC, category, name, symbol, path, line LIMIT 25
        """)
    ]
    cpu_frames = [
        dict(zip(('symbol', 'path', 'line', 'samples', 'sampled_us'), row))
        for row in db.execute("""
            SELECT * FROM trace_cpu_frame_totals ORDER BY sampled_us DESC, symbol, path, line LIMIT 25
        """)
    ]
    return {
        'events': len(events) - problems['invalid_event'],
        'threads': len(thread_events),
        'intervals': db.execute('SELECT COUNT(*) FROM trace_intervals').fetchone()[0],
        'cpu_profiles': db.execute('SELECT COUNT(*) FROM trace_profiles').fetchone()[0],
        'cpu_samples': db.execute('SELECT COUNT(*) FROM trace_profile_samples').fetchone()[0],
        'negative_cpu_deltas': reordered,
        'duration_groups': duration_groups,
        'cpu_frames': cpu_frames,
        'hot_cpu_paths': list(_hot_cpu_paths(db)),
        'incomplete': {name: count for name, count in sorted(problems.items()) if count},
        'duration_semantics': 'Inclusive X and synchronous B/E microseconds. Intervals can overlap or nest.',
        'cpu_semantics': 'Elapsed microseconds from each ordered V8 leaf sample to the next timestamp. '
                         'The final sample has zero weight. Original signed deltas are available in SQLite. '
                         'Profiles can overlap. Separate from event duration.',
        'source_semantics': 'Locations come from explicit trace call frames. URLs were not opened or symbolicated.',
    }


def _compare_groups(before, after, view, keys, count_name, time_name):
    columns = ', '.join(keys)
    query = f'SELECT {columns}, {count_name}, {time_name} FROM {view} ORDER BY {columns}'
    first, second = iter(before.execute(query)), iter(after.execute(query))

    def key(row):
        return tuple((value is not None, value) for value in row[:-2])

    compared = 0

    def changes():
        nonlocal compared
        a, b = next(first, None), next(second, None)
        while a is not None or b is not None:
            if b is None or (a is not None and key(a) < key(b)):
                identity, before_count, before_us = a[:-2], a[-2], a[-1]
                after_count = after_us = 0
                a = next(first, None)
            elif a is None or key(b) < key(a):
                identity, after_count, after_us = b[:-2], b[-2], b[-1]
                before_count = before_us = 0
                b = next(second, None)
            else:
                identity, before_count, before_us = a[:-2], a[-2], a[-1]
                after_count, after_us = b[-2:]
                a, b = next(first, None), next(second, None)
            compared += 1
            if before_count != after_count or before_us != after_us:
                yield {
                    **dict(zip(keys, identity)),
                    f'before_{count_name}': before_count, f'after_{count_name}': after_count,
                    f'before_{time_name}': before_us, f'after_{time_name}': after_us,
                    f'delta_{time_name}': after_us - before_us,
                }

    result = heapq.nsmallest(
        25, changes(),
        key=lambda row: (-abs(row[f'delta_{time_name}']), tuple((row[field] is not None, row[field]) for field in keys)),
    )
    return {'compared_groups': compared, 'largest_changes': result}


def compare_traces(before_db, after_db):
    """Compare full aggregate views so omission from a top-N summary is harmless."""
    before_gaps, after_gaps = (dict(db.execute('SELECT reason, records FROM trace_import_gaps'))
                               for db in (before_db, after_db))

    def coverage(reasons):
        before = {reason: count for reason, count in before_gaps.items() if reason in reasons}
        after = {reason: count for reason, count in after_gaps.items() if reason in reasons}
        return {'status': 'partial' if before or after else 'complete',
                'before_gaps': before, 'after_gaps': after}

    durations = _compare_groups(before_db, after_db, 'trace_duration_totals',
                                ('category', 'name', 'symbol', 'path', 'line'), 'events', 'total_us')
    durations['coverage'] = coverage({'invalid_event', 'invalid_duration', 'invalid_boundary',
                                      'orphan_end', 'mismatched_end', 'unclosed_begin'})
    cpu = _compare_groups(before_db, after_db, 'trace_cpu_frame_totals',
                          ('symbol', 'path', 'line'), 'samples', 'sampled_us')
    cpu['coverage'] = coverage({'invalid_event', 'incomplete_profile', 'invalid_profile', 'invalid_profile_event'})
    return {
        'durations': durations,
        'cpu': cpu,
        'semantics': 'Exact event or frame identities, independent of process and thread IDs. '
                     'Coverage describes the supported record types.',
        'zero_semantics': 'Zero means no imported observations or weight for the group. '
                          'With partial coverage it does not establish that execution was absent.',
    }
