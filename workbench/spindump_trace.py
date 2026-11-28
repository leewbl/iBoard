"""Read sampled macOS call trees without inferring unreported scheduler state."""

import re


_SAMPLE_HEADER = re.compile(
    r"^Analysis of sampling (?P<name>.+?) \(pid (?P<pid>\d{1,12})\)(?: every (?P<interval>[\d.]+) milliseconds?)?\s*$"
)
_SAMPLE_THREAD = re.compile(r"^\s*(\d{1,12})\s+Thread_(\d{1,24})\b(.*)$")
_SAMPLE_FRAME = re.compile(r"^[ \t+!:|]*(?P<count>\d{1,12})\s+(?P<body>\S.*)$")
_SPIN_THREAD = re.compile(r"^\s*Thread\s+(0x[\da-fA-F]{1,24})\b(.*?)\s+(\d{1,12})\s+samples?\b(.*)$")
_SPIN_FRAME = re.compile(r"^[ \t]*(?P<kernel>\*)?\s*(?P<count>\d{1,12})\s+(?P<body>\S.*)$")
_PROCESS = re.compile(r"^Process:\s+(.*?)\s+\[(\d{1,12})\](?:\s+\[unique pid \d+\])?\s*$")
_INTERVAL = re.compile(r"^Steps:\s+\d+\s+\(([\d.]+)\s*ms sampling interval\)\s*$")
_ADDRESS = re.compile(r"\[(0x[\da-fA-F]{1,32})(?:,\s*(?:0x[\da-fA-F]{1,32}|\.\.\.))*\]")
_RANGES = re.compile(r"(?:^|\s)(\d{1,12}(?:-\d{1,12})?(?:,\s*\d{1,12}(?:-\d{1,12})?)*)$")
_STATE = re.compile(
    r"\(((?:running|runnable|suspended|on-core|on-CPU)(?:,[^\r\n]{1,254})?"
    r"|(?:blocked(?: by| on)?|wait(?:ing)?(?: on| for)?)\b[^\r\n]{0,250})\)$",
    re.IGNORECASE,
)
_SOURCE = re.compile(
    r"\((?P<path>[^():]{1,1024}\.[a-zA-Z][a-zA-Z0-9]{0,11}):(?P<line>\d{1,9})"
    r"(?:[:,](?P<column>\d{1,9}))?(?= in |\))"
)
_SAMPLE_SOURCE = re.compile(
    r"(?P<path>[^():]{1,1024}\.[a-zA-Z][a-zA-Z0-9]{0,11}):(?P<line>\d{1,9})"
    r"(?:[:,](?P<column>\d{1,9}))?"
)
_IN_IMAGE = re.compile(r"\(in ([^()]{1,255})\)")
_SPIN_IMAGE = re.compile(r"\(([^()]{1,1024})\s+\+\s+(?:0x[\da-fA-F]+|\d+)\)\s*$")
_FRAME_OFFSET = re.compile(r"\s+\+\s+(?:0x[\da-fA-F]+|\d+)(?=[\s,.]|$)")
_STACK_END = ("Total number in stack", "Sort by top of stack", "Binary Images", "Binary images")
_MAX_FRAMES = 250_000


def _interval(value):
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 < seconds <= 60_000 else None


def _frame(body, *, spin):
    address = _ADDRESS.search(body)
    if address:
        symbol_text = body[:address.start()].strip()
        suffix = body[address.end():].strip()
    elif body.startswith("<truncated backtrace>"):
        symbol_text, suffix = "<truncated backtrace>", body[len("<truncated backtrace>"):].strip()
    else:
        return None

    sample_range = _RANGES.search(suffix) if spin else None
    if sample_range:
        suffix = suffix[:sample_range.start()].strip()
    state = _STATE.fullmatch(suffix) if spin else None
    inline_source = _SOURCE.search(symbol_text)
    trailing_source = _SAMPLE_SOURCE.fullmatch(suffix) if not spin else None
    source = inline_source or trailing_source
    image = _IN_IMAGE.search(symbol_text) or (_SPIN_IMAGE.search(symbol_text) if spin else None)
    module = image.group(1).rsplit(" in ", 1)[-1].strip() if image else None

    symbol = symbol_text[:image.start()].strip() if image else symbol_text
    if inline_source and inline_source.start() < len(symbol):
        symbol = symbol[:inline_source.start()].strip()
    symbol = re.split(r"\s+load address\s+", _FRAME_OFFSET.split(symbol, maxsplit=1)[0], maxsplit=1)[0].strip()
    if not symbol or len(symbol) > 4096:
        return None
    return (
        symbol, module, source['path'].strip() if source and int(source['line']) > 0 else None,
        int(source['line']) if source and int(source['line']) > 0 else None,
        int(source['column']) if source and source['column'] and int(source['column']) > 0 else None,
        address.group(1) if address else None, state.group(1) if state else None,
        sample_range.group(1) if sample_range else None, bool(suffix and not state and not trailing_source),
    )


def _shape(lines):
    for line in lines[:100]:
        if match := _SAMPLE_HEADER.fullmatch(line):
            return "macos-sample", match["name"], int(match["pid"]), _interval(match["interval"])
    stackshot = any(re.fullmatch(r"Data Source:\s+Stackshots\s*", line.strip()) for line in lines[:150])
    signature = any(line.strip().startswith(("Heaviest stack for", "Timeline format", "Heavy format"))
                    for line in lines[:500])
    if stackshot and signature:
        match = next((m for line in lines[:150] if (m := _INTERVAL.fullmatch(line.strip()))), None)
        return "macos-spindump", None, None, _interval(match.group(1)) if match else None
    raise ValueError("unrecognized macOS sample or spindump text")


def _parse(lines, kind, name, pid):
    spin = kind == "macos-spindump"
    processes = [] if spin else [(1, pid, name, 1)]
    threads, frames, stack, root_weights = [], [], [], {}
    unreliable_threads = set()
    process, thread, active = (None if spin else 1), None, spin
    boundary = False
    coverage = {"unparsed_frame_rows": 0, "unparsed_thread_headers": 0,
                "unrecognized_annotations": 0, "truncated_backtraces": 0}
    for number, raw in enumerate(lines, 1):
        line = raw.expandtabs(8)
        stripped = line.strip()
        if not spin and stripped == "Call graph:":
            active = True
            continue
        if stripped.startswith(_STACK_END):
            boundary = True
            active, thread = False, None
            continue
        if spin and (match := _PROCESS.fullmatch(stripped)):
            process = len(processes) + 1
            processes.append((process, int(match.group(2)), match.group(1), number))
            active, thread = True, None
            boundary = False
            continue
        if not active or process is None or not stripped:
            continue
        match = (_SPIN_THREAD if spin else _SAMPLE_THREAD).fullmatch(line)
        if match:
            if spin:
                identifier, descriptor, weight, _ = match.groups()
            else:
                weight, identifier, descriptor = match.groups()
            thread = len(threads) + 1
            threads.append((thread, process, identifier, descriptor.strip(), int(weight), number))
            root_weights[thread] = 0
            stack = []
            continue
        if spin and stripped.startswith("Thread "):
            coverage["unparsed_thread_headers"] += 1
            thread = None
            continue
        if thread is None:
            continue
        if spin and stripped.startswith("<") and stripped.endswith(">"):
            # These are thread-level QoS / timeline annotations, never per-frame wait evidence.
            coverage["unrecognized_annotations"] += 1
            continue
        frame = (_SPIN_FRAME if spin else _SAMPLE_FRAME).fullmatch(line) if len(line) < 32_768 else None
        parsed = _frame(frame["body"], spin=spin) if frame else None
        if not parsed or not int(frame["count"]):
            coverage["unparsed_frame_rows"] += 1
            unreliable_threads.add(thread)
            continue
        symbol, image, path, source_line, col, address, state, ranges, unknown_suffix = parsed
        coverage["unrecognized_annotations"] += unknown_suffix
        coverage["truncated_backtraces"] += symbol == "<truncated backtrace>"
        weight = int(frame["count"])
        indent = frame.start("count")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        row_id = len(frames) + 1
        if row_id > _MAX_FRAMES:
            raise ValueError("macOS capture exceeds the frame count limit")
        kernel = int(bool(frame["kernel"])) if spin else 0
        frames.append([row_id, thread, parent, len(stack), weight, weight, symbol, image,
                       path, source_line, col, address, state, ranges, kernel, number])
        if parent:
            frames[parent - 1][5] -= weight
        else:
            root_weights[thread] += weight
        stack.append((indent, row_id))

    if not threads or not frames:
        raise ValueError("macOS capture has no supported thread call trees")
    if not spin and not boundary:
        raise ValueError("macOS sample call graph has no closing section")
    coverage["uncertain_weight_threads"] = len(unreliable_threads := unreliable_threads | {
        row[0] for row in threads if root_weights[row[0]] > row[4] or not root_weights[row[0]]
    } | {row[1] for row in frames if row[5] < 0})
    coverage["recognized_end_section"] = boundary
    for frame in frames:
        if frame[1] in unreliable_threads or frame[6] == "<truncated backtrace>":
            frame[5] = None
    return processes, threads, frames, coverage


def import_trace(db, text):
    """Import macOS thread trees and summarize explicitly observed terminal samples."""
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("macOS capture must be a text report")
    lines = text.splitlines()
    kind, name, pid, interval = _shape(lines)
    processes, threads, frames, coverage = _parse(lines, kind, name, pid)
    db.executescript("""
        CREATE TABLE spin_processes(id INTEGER PRIMARY KEY, pid INTEGER, name TEXT, report_line INTEGER);
        CREATE TABLE spin_threads(id INTEGER PRIMARY KEY, process_id INTEGER REFERENCES spin_processes(id),
          thread_id TEXT, descriptor TEXT, samples INTEGER, report_line INTEGER);
        CREATE TABLE spin_frames(id INTEGER PRIMARY KEY, thread_id INTEGER REFERENCES spin_threads(id),
          parent_id INTEGER REFERENCES spin_frames(id), depth INTEGER, samples INTEGER, leaf_samples INTEGER,
          symbol TEXT, image TEXT, path TEXT, line INTEGER, column INTEGER, address TEXT,
          observed_state TEXT, sample_range TEXT, kernel INTEGER, report_line INTEGER);
        CREATE INDEX spin_frames_thread ON spin_frames(thread_id);
        CREATE INDEX spin_frames_parent ON spin_frames(parent_id);
    """)
    db.executemany("INSERT INTO spin_processes VALUES (?, ?, ?, ?)", processes)
    db.executemany("INSERT INTO spin_threads VALUES (?, ?, ?, ?, ?, ?)", threads)
    db.executemany("INSERT INTO spin_frames VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", frames)
    sources = sum(frame[9] is not None and frame[6] != "???" for frame in frames)
    rows = db.execute("""
        SELECT p.pid, p.name AS process, t.thread_id, t.id AS thread_tree_id,
          f.symbol, f.image, f.path, f.line, f.observed_state,
          SUM(f.leaf_samples) AS samples, MIN(f.report_line) AS report_line
        FROM spin_frames AS f JOIN spin_threads AS t ON f.thread_id = t.id
        JOIN spin_processes AS p ON p.id = t.process_id
        WHERE f.leaf_samples > 0
        GROUP BY t.id, f.symbol, f.image, f.path, f.line, f.observed_state
        ORDER BY samples DESC, t.id, report_line LIMIT 20
    """)
    top = [dict(zip((column[0] for column in rows.description), row)) for row in rows]
    return {
        "format": kind, "processes": len(processes), "threads": len({(row[1], row[2]) for row in threads}),
        "thread_trees": len(threads), "frames": len(frames),
        "thread_samples": sum(row[4] for row in threads), "sampling_interval_ms": interval,
        "source_mapped_frames": sources, "top_frames": top, "coverage": coverage,
        "weight_semantics": "Observed sample counts; leaf counts subtract immediate child counts within each tree.",
        "state_semantics": "Only explicit spindump frame annotations supply state. Sample frames supply no scheduler state.",
    }


def compare_traces(before, after, baseline, comparison):
    """Compare all attributable samples without matching process or thread IDs across runs."""
    interval = baseline["sampling_interval_ms"]
    if (baseline["format"] != comparison["format"]
            or interval is None or interval != comparison["sampling_interval_ms"]):
        raise ValueError("paired macOS captures need the same format and reported sampling interval")

    def read(db):
        weighted = db.execute("""
            SELECT p.name, f.image, f.symbol, f.path, f.line, f.observed_state, SUM(f.leaf_samples)
            FROM spin_frames AS f JOIN spin_threads AS t ON f.thread_id = t.id
            JOIN spin_processes AS p ON p.id = t.process_id
            WHERE f.leaf_samples > 0
            GROUP BY p.name, f.image, f.symbol, f.path, f.line, f.observed_state
        """)
        counts, totals = {}, {}
        for process, image, symbol, path, line, state, weight in weighted:
            totals[process] = totals.get(process, 0) + weight
            if symbol != "???":
                counts[(process, image, symbol, path, line, state)] = weight
        return counts, totals

    left, left_totals = read(before)
    right, right_totals = read(after)
    changes = []
    for key in left.keys() | right.keys():
        process, image, symbol, path, line, state = key
        a, b = left.get(key, 0), right.get(key, 0)
        a_total, b_total = left_totals.get(process, 0), right_totals.get(process, 0)
        a_share = round(100 * a / a_total, 3) if a_total else None
        b_share = round(100 * b / b_total, 3) if b_total else None
        difference = round(b_share - a_share, 3) if a_share is not None and b_share is not None else None
        if a != b or difference:
            changes.append({
                "process": process, "image": image, "symbol": symbol, "path": path,
                "line": line, "observed_state": state, "baseline_samples": a, "comparison_samples": b,
                "delta_samples": b - a, "baseline_share_pct": a_share, "comparison_share_pct": b_share,
                "delta_share_points": difference,
                "share_denominator": "Attributable leaf samples in the named process across imported call trees.",
            })
    return sorted(changes, key=lambda item: (
        -abs(item["delta_share_points"] or 0), -abs(item["delta_samples"]),
        item["process"], item["symbol"], item["image"] or "", item["path"] or "",
        item["line"] or 0, item["observed_state"] or "",
    ))[:30]
