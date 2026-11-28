"""Join live heap objects to the V8 allocation stacks recorded in a snapshot."""

from collections import Counter


MAX_FUNCTIONS = 250_000
MAX_TRACE_NODES = 500_000
MAX_STACK_DEPTH = 128


class AllocationError(ValueError):
    pass


def _number(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AllocationError('heap allocation metadata contains an invalid integer')
    return value


def _layout(meta, field, required):
    names = meta.get(field)
    if (not isinstance(names, list) or not 1 <= len(names) <= 24 or not all(isinstance(n, str) for n in names)
            or len(set(names)) != len(names) or not set(required) <= set(names)):
        raise AllocationError(f'invalid V8 metadata for {field}')
    return len(names), tuple(names.index(name) for name in required)


def _string(strings, index):
    value = strings[_number(index, 0, len(strings) - 1)]
    if not isinstance(value, str):
        raise AllocationError('allocation frame references a non-string table entry')
    return value


def parse(profile, locate=None):
    """Decode V8's function table and nested flat trace tree, preserving parent IDs."""
    snapshot = profile['snapshot']
    count = _number(snapshot.get('trace_function_count', 0), maximum=MAX_FUNCTIONS)
    functions = profile.get('trace_function_infos', [])
    tree = profile.get('trace_tree', [])
    if not count:
        if functions or tree:
            raise AllocationError('allocation trace data has no declared function count')
        return None
    meta = snapshot['meta']
    if 'trace_node_id' not in meta['node_fields']:
        raise AllocationError('tracked heap objects do not have allocation trace IDs')
    strings = profile['strings']
    width, layout = _layout(meta, 'trace_function_info_fields',
                            ('function_id', 'name', 'script_name', 'script_id', 'line', 'column'))
    if not isinstance(functions, list) or len(functions) != count * width:
        raise AllocationError('allocation function count does not match the function table')

    frames = []
    for offset in range(0, len(functions), width):
        function, symbol, script, script_id, line, column = (functions[offset + index] for index in layout)
        name = _string(strings, symbol)[:240]
        path = _string(strings, script)
        path = path if path and len(path) <= 2048 and path.isprintable() else None
        line = _number(line, -1, 2**31 - 1)
        column = _number(column, -1, 2**31 - 1)
        line = line if line > 0 and path else None
        if path and line and locate is not None:
            file, status = locate(path, line)
        elif path and line:
            file, status = path, 'reported-location'
        else:
            file, status = None, 'location-unavailable'
        frames.append({'function_id':_number(function), 'script_id':_number(script_id, -1),
                       'symbol':name or '(anonymous)', 'path':path, 'line':line,
                       'column':column if line and column > 0 else None,
                       'source_file':file, 'source_status':status})

    width, layout = _layout(meta, 'trace_node_fields', ('id', 'function_info_index', 'count', 'size', 'children'))
    if not isinstance(tree, list) or len(tree) != width:
        raise AllocationError('allocation trace must have one root row')
    nodes, verified = {}, set()
    # Visit child rows iteratively while keeping only the active stack.
    active = [(iter(range(0, len(tree), width)), tree, None)]
    while active:
        offsets, rows, parent = active[-1]
        offset = next(offsets, None)
        if offset is None:
            active.pop()
            continue
        node, frame, calls, size, children = (rows[offset + index] for index in layout)
        node = _number(node, 1, 2**31 - 1)
        frame = _number(frame, 0, len(frames) - 1)
        if node in nodes or len(nodes) >= MAX_TRACE_NODES:
            raise AllocationError('allocation trace IDs are duplicated or exceed the limit')
        if not isinstance(children, list) or len(children) % width:
            raise AllocationError('allocation trace contains incomplete child rows')
        nodes[node] = parent, frame, _number(calls), _number(size)
        if parent in verified or frames[frame]['source_status'] == 'verified-git-source':
            verified.add(node)
        if children:
            if len(active) >= MAX_STACK_DEPTH:
                raise AllocationError('allocation stack depth exceeds the limit')
            active.append((iter(range(0, len(children), width)), children, node))

    return {'frames':frames, 'nodes':nodes, 'verified':verified}


def _stack(graph, node):
    frames = []
    while node is not None:
        node, frame, _, _ = graph['nodes'][node]
        frames.append(graph['frames'][frame])
    return list(reversed(frames))


def trace_id(value, graph):
    node = _number(value, maximum=2**31 - 1)
    if node and graph is not None and node not in graph['nodes']:
        raise AllocationError('heap object references an unknown allocation trace ID')
    return node


def summarize(profile, graph, class_name, representative):
    """Count matching object rows and sum their shallow sizes, using trace IDs as the join."""
    meta = profile['snapshot']['meta']
    nodes, strings = profile['nodes'], profile['strings']
    width, (kind, name, size, trace) = _layout(meta, 'node_fields', ('type', 'name', 'self_size', 'trace_node_id'))
    object_kind = meta['node_types'][kind].index('object')
    total = 0
    counts, sizes = Counter(), Counter()
    for offset in range(0, len(nodes), width):
        node = trace_id(nodes[offset + trace], graph)
        if nodes[offset + kind] != object_kind:
            continue
        if class_name is not None and _string(strings, nodes[offset + name]) != class_name:
            continue
        total += 1
        if node:
            counts[node] += 1
            sizes[node] += _number(nodes[offset + size])
    example = None
    if representative is not None:
        node = trace_id(nodes[representative * width + trace], graph)
        example = {'heap_node':representative, 'trace_node_id':node or None,
                   'stack':_stack(graph, node) if node else None}
    return {'class_name':class_name, 'snapshot_objects':total, 'traced_objects':sum(counts.values()),
            'verified_source_objects':sum(counts[node] for node in graph['verified']),
            'sites':[{'trace_node_id':node, 'objects':count, 'shallow_bytes':sizes[node], 'stack':_stack(graph, node)}
                     for node, count in counts.most_common(15)], 'representative':example}


def write_tables(db, graph):
    db.executescript('''
        CREATE TABLE heap_allocation_functions(function_index INTEGER PRIMARY KEY, function_id INTEGER,
            script_id INTEGER, symbol TEXT, path TEXT, line INTEGER, column INTEGER, source_file TEXT, source_status TEXT);
        CREATE TABLE heap_allocation_traces(trace_node_id INTEGER PRIMARY KEY, parent_id INTEGER,
            function_index INTEGER REFERENCES heap_allocation_functions(function_index),
            recorded_allocations INTEGER, recorded_bytes INTEGER);
        CREATE INDEX heap_nodes_trace_id ON heap_nodes(trace_node_id);
    ''')
    keys = ('function_id', 'script_id', 'symbol', 'path', 'line', 'column', 'source_file', 'source_status')
    db.executemany('INSERT INTO heap_allocation_functions VALUES(?,?,?,?,?,?,?,?,?)',
                   ((i, *(frame[key] for key in keys)) for i, frame in enumerate(graph['frames'])))
    db.executemany('INSERT INTO heap_allocation_traces VALUES(?,?,?,?,?)',
                   ((node, *row) for node, row in graph['nodes'].items()))
