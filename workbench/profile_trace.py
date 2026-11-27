"""SQLite importers for existing V8 CPU and heap profiles."""

from collections import Counter

from workbench import heap_allocations
from workbench.runtime_forensics import ForensicsError, summarize_heap


def _text(value, limit=2048):
    if not isinstance(value, str) or len(value) > limit or '\0' in value:
        raise ForensicsError('invalid text in V8 profile')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ForensicsError('invalid integer in V8 profile')
    return value


def import_cpu(db, profile, locate):
    nodes, samples, deltas = (profile[key] for key in ('nodes', 'samples', 'timeDeltas'))
    if (not isinstance(nodes, list) or not 0 < len(nodes) <= 200_000 or
            not isinstance(samples, list) or not isinstance(deltas, list)
            or len(samples) != len(deltas) or not 0 < len(samples) <= 2_000_000):
        raise ForensicsError('CPU profile is missing complete, bounded sample data')
    frames, parents = {}, {}
    for raw in nodes:
        frame_id = _integer(raw['id'], 1, 2**31 - 1)
        if frame_id in frames:
            raise ForensicsError('CPU profile has duplicate frame IDs')
        frame = raw['callFrame']
        line = _integer(frame.get('lineNumber', -1), -1, 10**8)
        url, symbol = _text(frame.get('url', '')), _text(frame.get('functionName', ''), 512)
        frames[frame_id] = (symbol or '(anonymous)', url, line + 1 if line >= 0 else None)
        children = raw.get('children', [])
        if not isinstance(children, list) or len(children) > len(nodes):
            raise ForensicsError('invalid CPU frame children')
        for child in children:
            child = _integer(child, 1, 2**31 - 1)
            if child in parents:
                raise ForensicsError('CPU call tree contains more than one parent')
            parents[child] = frame_id
            if len(parents) >= len(nodes):
                raise ForensicsError('CPU call tree contains too many parent edges')
    if parents.keys() - frames.keys():
        raise ForensicsError('CPU call tree refers to an unknown frame')
    children_by_parent = {}
    for child, parent in parents.items():
        children_by_parent.setdefault(parent, []).append(child)
    root = frames.keys() - parents.keys()
    if len(root) != 1:
        raise ForensicsError('CPU call tree must have one root')
    pending = list(root)
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        pending.extend(children_by_parent.get(current, ()))
    if visited != len(frames):
        raise ForensicsError('CPU call tree contains a disconnected cycle')

    db.executescript('''
        CREATE TABLE frames(id INTEGER PRIMARY KEY, parent_id INTEGER, symbol TEXT, url TEXT,
                            file TEXT, line INTEGER, source_status TEXT);
        CREATE TABLE samples(seq INTEGER PRIMARY KEY, frame_id INTEGER, offset_us INTEGER, delta_us INTEGER);
        CREATE TABLE sample_weights(seq INTEGER PRIMARY KEY, leaf_us INTEGER);
    ''')
    locations = {}
    for frame_id, (symbol, url, line) in frames.items():
        file, status = locate(url, line)
        locations[frame_id] = {'symbol': symbol, 'url': url, 'file': file, 'line': line, 'source_status': status}
    db.executemany('INSERT INTO frames VALUES(?,?,?,?,?,?,?)', (
        (frame_id, parents.get(frame_id), item['symbol'], item['url'], item['file'], item['line'], item['source_status'])
        for frame_id, item in locations.items()
    ))

    def observations():
        offset = 0
        for seq, (frame_id, elapsed) in enumerate(zip(samples, deltas), 1):
            if type(frame_id) is not int or frame_id not in frames:
                raise ForensicsError('CPU sample refers to an unknown frame')
            _integer(elapsed, -1_000_000_000, 1_000_000_000)
            offset += elapsed
            if offset < 0:
                raise ForensicsError('CPU sample timestamp precedes the profile start')
            yield seq, frame_id, offset, elapsed

    db.executemany('INSERT INTO samples VALUES(?,?,?,?)', observations())
    db.executescript('''
        CREATE INDEX samples_order ON samples(offset_us, seq);
        INSERT INTO sample_weights
        SELECT seq, COALESCE(LEAD(offset_us) OVER (ORDER BY offset_us, seq) - offset_us, 0)
        FROM samples;
    ''')
    total = db.execute('SELECT SUM(leaf_us) FROM sample_weights').fetchone()[0]
    db.execute('CREATE INDEX samples_frame ON samples(frame_id)')
    rows = db.execute('''
        SELECT frame_id, SUM(leaf_us) AS duration, COUNT(*), MIN(samples.seq)
        FROM samples JOIN sample_weights USING(seq)
        GROUP BY frame_id ORDER BY duration DESC, frame_id LIMIT 20
    ''')
    hottest = []
    for frame_id, duration, count, first in rows:
        cursor = frame_id
        path = []
        depth = 0
        while cursor is not None:
            if len(path) < 30:
                path.append({'frame_id': cursor, **locations[cursor]})
            depth += 1
            cursor = parents.get(cursor)
        hottest.append({'frame_id': frame_id, **locations[frame_id], 'sampled_us': duration,
                        'samples': count, 'first_sample': first, 'share_pct': round(100 * duration / total, 2) if total else None,
                        'call_path': list(reversed(path)), 'omitted_ancestors': depth - len(path)})
    return {'samples': len(samples), 'sampled_us': total, 'top_frames': hottest,
            'duration_semantics': 'Sampled leaf time from each ordered timestamp to the next. '
                                  'The last sample has zero assigned time. Ancestors provide context.'}


def import_heap(db, profile, class_name, locate=None):
    # Reuse the live profiler's strong-reference interpretation for the same format.
    allocations = heap_allocations.parse(profile, locate)
    finding = summarize_heap(profile, class_name, allocations=allocations)
    meta = profile['snapshot']['meta']
    nodes, edges, strings = (profile[key] for key in ('nodes', 'edges', 'strings'))
    nf, ef = meta['node_fields'], meta['edge_fields']
    ntype, name, identity, size, degree = (nf.index(k) for k in ('type', 'name', 'id', 'self_size', 'edge_count'))
    trace = nf.index('trace_node_id') if 'trace_node_id' in nf else None
    etype, ename, target = (ef.index(k) for k in ('type', 'name_or_index', 'to_node'))
    node_types, edge_types = meta['node_types'][ntype], meta['edge_types'][etype]
    db.executescript('''
        CREATE TABLE heap_nodes(node_id INTEGER PRIMARY KEY, v8_id INTEGER, kind TEXT, label TEXT,
                                shallow_bytes INTEGER, outgoing INTEGER, trace_node_id INTEGER);
        CREATE TABLE heap_edges(edge_id INTEGER PRIMARY KEY, origin INTEGER, target INTEGER, kind TEXT, label TEXT);
    ''')

    def node_rows():
        for index in range(finding['nodes']):
            at = index * len(nf)
            kind = node_types[_integer(nodes[at + ntype], 0, len(node_types) - 1)]
            label = strings[_integer(nodes[at + name], 0, len(strings) - 1)]
            yield (index, _integer(nodes[at + identity], 0, 2**63 - 1), kind, label,
                   _integer(nodes[at + size], 0, 2**63 - 1), nodes[at + degree],
                   heap_allocations.trace_id(nodes[at + trace], allocations) if trace is not None else None)

    def edge_rows():
        index = 0
        for origin in range(finding['nodes']):
            for _ in range(nodes[origin * len(nf) + degree]):
                at = index * len(ef)
                kind = edge_types[_integer(edges[at + etype], 0, len(edge_types) - 1)]
                address = _integer(edges[at + target], 0, len(nodes) - 1)
                if address % len(nf):
                    raise ForensicsError('heap edge does not point to a node boundary')
                raw = edges[at + ename]
                label = (str(_integer(raw, 0, 2**63 - 1)) if kind in ('element', 'hidden')
                         else strings[_integer(raw, 0, len(strings) - 1)])
                yield index, origin, address // len(nf), kind, label
                index += 1

    db.executemany('INSERT INTO heap_nodes VALUES(?,?,?,?,?,?,?)', node_rows())
    db.executemany('INSERT INTO heap_edges VALUES(?,?,?,?,?)', edge_rows())
    db.executescript('''
        CREATE INDEX heap_nodes_label ON heap_nodes(label);
        CREATE INDEX heap_edges_origin ON heap_edges(origin);
        CREATE INDEX heap_edges_target ON heap_edges(target);
    ''')
    if allocations is not None:
        heap_allocations.write_tables(db, allocations)
    elif class_name is not None:
        observed = db.execute(
            "SELECT COUNT(*) FROM heap_nodes WHERE kind='object' AND label=? AND trace_node_id>0", (class_name,)
        ).fetchone()[0]
        finding['selected_class']['allocation_source'] = ('allocation IDs recorded; source mapping unavailable'
                                                          if observed else 'allocation symbols not captured')
    return finding


def compare_cpu(before, after):
    def totals(db):
        return Counter({(symbol, file, line): duration for symbol, file, line, duration in db.execute('''
            SELECT symbol, file, line, SUM(leaf_us)
            FROM frames JOIN samples ON frames.id=samples.frame_id
            JOIN sample_weights USING(seq)
            WHERE file IS NOT NULL AND line IS NOT NULL
            GROUP BY symbol,file,line
        ''')})

    past, present = totals(before), totals(after)
    keys = sorted(past.keys() | present.keys(),
                  key=lambda key: (-abs(present[key] - past[key]), key))
    return [{'symbol': key[0], 'file': key[1], 'line': key[2],
             'baseline_us': past[key], 'comparison_us': present[key], 'delta_us': present[key] - past[key]}
            for key in keys[:20] if present[key] != past[key]]
