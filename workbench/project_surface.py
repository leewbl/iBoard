"""Reconcile tracked command registrations and JSX controls with a verification map."""

import re


_COMMAND = re.compile(r"[a-z][a-z0-9-]{0,55}")
_RECEIVER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,50}")
_LABEL_KEY = re.compile(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*")
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_TOKENS = re.compile(
    r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
    r"""|(?P<string>"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`)"""
    r"|[A-Za-z0-9_$]+|\S"
)


def _words(value):
    return (isinstance(value, str) and 1 <= len(value) <= 180
            and all(_COMMAND.fullmatch(word) for word in value.split(" ")))


def validate(contract, check_path, check_text):
    scopes = contract.get("entrypoint_inventory", [])
    if not isinstance(scopes, list) or len(scopes) > 20:
        raise ValueError("entrypoint_inventory needs at most twenty scopes")
    namespaces = set()
    claimed = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"namespace", "sources", "excluded"}:
            raise ValueError("an entrypoint scope needs namespace, sources and excluded")
        namespace = scope["namespace"]
        if not _words(namespace) or namespace in namespaces:
            raise ValueError("entrypoint scope must have a unique namespace")
        namespaces.add(namespace)
        sources = scope["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
            raise ValueError("an entrypoint scope needs one to twelve source bindings")
        seen = set()
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("a source binding must be an object")
            if set(source) == {"path", "receiver"}:
                identity = (source["receiver"],)
                symbols = identity
            elif set(source) == {"kind", "path", "component", "label_key"} and source["kind"] == "jsx-options":
                label_key = source["label_key"]
                if not isinstance(label_key, str) or len(label_key) > 120 or not _LABEL_KEY.fullmatch(label_key):
                    raise ValueError("JSX entrypoint binding requires a literal translation key")
                identity = (source["component"], label_key)
                symbols = identity[:1]
            else:
                raise ValueError("a source binding needs a command receiver or a JSX options selector")
            check_path(source["path"])
            if not all(isinstance(symbol, str) and _RECEIVER.fullmatch(symbol) for symbol in symbols):
                raise ValueError("entrypoint receiver or component must be a source identifier")
            binding = (source["path"], *identity)
            if binding in seen:
                raise ValueError("duplicate entrypoint source binding")
            seen.add(binding)
        excluded = scope["excluded"]
        if not isinstance(excluded, dict) or len(excluded) > 60:
            raise ValueError("excluded must map entrypoint names to specific reasons")
        for command, reason in excluded.items():
            if not _COMMAND.fullmatch(command):
                raise ValueError("excluded entry must be a direct entrypoint name")
            check_text(reason, "entrypoint exclusion reason")
            claimed.add(f"{namespace} {command}")
    for feature in contract["features"]:
        entries = feature.get("entrypoints", [])
        if not isinstance(entries, list) or len(entries) > 25:
            raise ValueError("feature entrypoints must be a bounded list")
        for command in entries:
            if (not _words(command) or command.rpartition(" ")[0] not in namespaces
                    or command in claimed):
                raise ValueError("each mapped entrypoint needs a declared scope and a single owner")
            claimed.add(command)


def _literal(token):
    return token[1:-1] if token[0] in ("'", '"') and "\\" not in token else None


def _matching(tokens, start, brackets=_BRACKETS):
    stack = [brackets[tokens[start][0]]]
    for index in range(start + 1, len(tokens)):
        token = tokens[index][0]
        if token in brackets:
            stack.append(brackets[token])
        elif token in brackets.values():
            if stack.pop() != token:
                break
            if not stack:
                return index
    raise ValueError("unbalanced source delimiters")


def _command_name(value):
    words = value.split(" ", 1) if value else []
    return words[0] if words and _COMMAND.fullmatch(words[0]) else None


def _top_level(tokens, start, stop):
    while start < stop:
        yield start
        if tokens[start][0] in _BRACKETS:
            start = _matching(tokens, start)
        start += 1


def _command_loop_domains(tokens, receiver):
    """Resolve only immutable literal loops, and only direct uses in their body."""
    domains = {}
    for index, token in enumerate(tokens):
        head = [match[0] for match in tokens[index + 1:index + 6]] if token[0] == "for" else []
        if (len(head) != 5 or head[:2] != ["(", "const"] or head[3:] != ["of", "["]
                or not _RECEIVER.fullmatch(head[2])):
            continue
        name = head[2]
        array_end = _matching(tokens, index + 5)
        brace = array_end + 1
        if [t[0] for t in tokens[brace:brace + 2]] == ["as", "const"]:
            brace += 2
        if [t[0] for t in tokens[brace:brace + 2]] != [")", "{"]:
            continue
        values = [_command_name(_literal(part[0][0])) if len(part) == 1 else None
                  for part in _comma_parts(tokens[index + 6:array_end])]
        if not values or None in values or len(set(values)) != len(values):
            continue
        brace += 1
        for offset in _top_level(tokens, brace + 1, _matching(tokens, brace)):
            if tokens[offset][0] == receiver and (offset == brace + 1 or tokens[offset - 1][0] == ";"):
                domains[offset] = (name, values)
    return domains


def _command_access(tokens, offset):
    following = [token[0] for token in tokens[offset + 1:offset + 3]]
    start = offset + 1
    if following[:2] == ["?", "."]:
        start += 2
    elif following[:1] == ["."]:
        start += 1
    elif following[:1] != ["["]:
        return None
    if start == len(tokens):
        return None
    if tokens[start][0] == "[":
        if start + 2 >= len(tokens) or tokens[start + 2][0] != "]":
            return None
        name = _literal(tokens[start + 1][0])
        method, begin = start + 1, start + 3
    else:
        name = tokens[start][0]
        method, begin = start, start + 1
    return (name, method, begin) if name in ("command", "addCommand") else None


def _registrations(text, receiver):
    tokens = [match for match in _TOKENS.finditer(text) if match.lastgroup != "comment"]
    domains = _command_loop_domains(tokens, receiver)
    found, unresolved = [], []
    for offset, token in enumerate(tokens):
        if token[0] != receiver or offset and tokens[offset - 1][0] in (".", "#"):
            continue
        access = _command_access(tokens, offset)
        if access is None:
            continue
        name, method, begin = access
        line = text.count("\n", 0, tokens[method].start()) + 1
        if name == "addCommand":
            unresolved.append((line, "Commander addCommand requires explicit registration review"))
            continue
        if [t[0] for t in tokens[begin:begin + 2]] == ["?", "."]:
            begin += 2
        arguments = [t[0] for t in tokens[begin:begin + 3]]
        names = []
        if len(arguments) == 3 and arguments[0] == "(" and arguments[2] in (")", ","):
            value = arguments[1]
            literal = _command_name(_literal(value))
            if literal:
                names = [literal]
            elif offset in domains and domains[offset][0] == value:
                names = domains[offset][1]
        if names:
            found.extend((name, line) for name in names)
        else:
            unresolved.append((line, "Commander command registration name could not be resolved"))
    return found, unresolved


def _jsx_attributes(tokens, start):
    if start < len(tokens) and tokens[start][0] == "<":
        start = _matching(tokens, start, {**_BRACKETS, "<": ">"}) + 1
    attributes = {"ariaLabel": [], "options": []}
    spread = False
    index = start
    while index < len(tokens):
        token = tokens[index][0]
        if token == ">":
            return attributes, spread
        if token == "{":
            spread |= index == start or tokens[index - 1][0] != "="
            index = _matching(tokens, index) + 1
            continue
        if token in attributes and tokens[index - 1][0] not in (".", "-"):
            if index + 2 < len(tokens) and tokens[index + 1][0] == "=":
                value = index + 2
                end = _matching(tokens, value) if tokens[value][0] == "{" else value
                attributes[token].append(tokens[value:end + 1])
                index = end + 1
                continue
            attributes[token].append([])
        if token in ("<", "}", ";"):
            break
        index += 1
    raise ValueError("incomplete JSX control declaration")


def _comma_parts(tokens):
    start, index = 0, 0
    while index < len(tokens):
        token = tokens[index][0]
        if token == ",":
            yield tokens[start:index]
            start = index + 1
        elif token in _BRACKETS:
            index = _matching(tokens, index)
        index += 1
    if start < len(tokens):
        yield tokens[start:]


def _option_values(tokens):
    tokens = tokens[1:-1] if tokens and tokens[0][0] == "{" else []
    if not tokens or tokens[0][0] != "[" or _matching(tokens, 0) != len(tokens) - 1:
        raise ValueError("JSX control options are not a literal array")
    values = []
    for option in _comma_parts(tokens[1:-1]):
        if not option or option[0][0] != "{" or _matching(option, 0) != len(option) - 1:
            raise ValueError("JSX option is not a literal object")
        named = []
        for field in _comma_parts(option[1:-1]):
            key = field[0][0] if field else ""
            if not key or not (_RECEIVER.fullmatch(key) or _literal(key) is not None) or (
                    len(field) > 1 and field[1][0] != ":"):
                raise ValueError("JSX option contains spread or computed properties")
            if key == "value" or _literal(key) == "value":
                name = _literal(field[2][0]) if len(field) == 3 and field[1][0] == ":" else None
                if not name or not _COMMAND.fullmatch(name):
                    raise ValueError("JSX option value is not a literal entrypoint name")
                named.append((name, field[2].start()))
        if len(named) != 1:
            raise ValueError("JSX option needs one literal value")
        values.append(named[0])
    if not values or len({name for name, _ in values}) != len(values):
        raise ValueError("JSX control options must have distinct literal values")
    return values


def _jsx_options(text, component, label_key):
    tokens = [match for match in _TOKENS.finditer(text) if match.lastgroup != "comment"]
    controls = []
    for index in range(len(tokens) - 1):
        if tokens[index][0] != "<" or tokens[index + 1][0] != component:
            continue
        if index + 2 < len(tokens) and tokens[index + 2][0] in (".", ":"):
            continue
        attributes, spread = _jsx_attributes(tokens, index + 2)
        labels = [tuple(token[0] for token in value) for value in attributes["ariaLabel"]]
        if any(len(label) == 6 and label[:3] == ("{", "t", "(") and label[4:] == (")", "}")
               and _literal(label[3]) == label_key for label in labels):
            if spread or len(labels) != 1 or len(attributes["options"]) != 1:
                raise ValueError("JSX control has a spread or ambiguous label/options declaration")
            controls.append((tokens[index].start(), attributes["options"][0]))
    if len(controls) != 1:
        raise ValueError("expected exactly one JSX control with the declared translation key")
    offset, options = controls[0]
    try:
        values = _option_values(options)
    except ValueError as error:
        line = text.count("\n", 0, offset) + 1
        raise ValueError(f"{error} (control at line {line})") from error
    return [(name, text.count("\n", 0, offset) + 1) for name, offset in values]


def scan(contract, read_blob):
    sources, excluded, errors, kinds = {}, {}, [], {}
    mapped = {command: feature["id"] for feature in contract["features"]
              for command in feature.get("entrypoints", [])}
    for scope in contract.get("entrypoint_inventory", []):
        namespace = scope["namespace"]
        excluded.update({f"{namespace} {name}": reason for name, reason in scope["excluded"].items()})
        for source in scope["sources"]:
            path = source["path"]
            blob, text, error = read_blob(path)
            if error:
                errors.append({"namespace": namespace, "path": path, "error": error})
                continue
            kind = source.get("kind", "command")
            try:
                if kind == "jsx-options":
                    found = _jsx_options(text, source["component"], source["label_key"])
                    unresolved = []
                else:
                    found, unresolved = _registrations(text, source["receiver"])
            except ValueError as problem:
                errors.append({"namespace": namespace, "path": path, "blob": blob, "error": str(problem)})
                continue
            errors.extend({"namespace": namespace, "path": path, "blob": blob, "line": line, "error": problem}
                          for line, problem in unresolved)
            if not found and not unresolved:
                errors.append({"namespace": namespace, "path": path, "blob": blob,
                               "error": "no command registrations found on the declared receiver"})
            for name, line in found:
                key = f"{namespace} {name}"
                if key in kinds and kinds[key] != kind:
                    errors.append({"namespace": namespace, "path": path, "blob": blob, "line": line,
                                   "error": "entrypoint name is shared by command and JSX bindings"})
                    continue
                sources.setdefault(key, []).append({"path": path, "blob": blob, "line": line})
                kinds.setdefault(key, kind)
    entries = []
    for command, locations in sorted(sources.items()):
        record = {"command": command, "locations": locations, "status": "unmapped"}
        if kinds[command] != "command":
            record["kind"] = kinds[command]
        if command in mapped:
            record.update(status="mapped", journey=mapped[command])
        elif command in excluded:
            record.update(status="excluded", reason=excluded[command])
        entries.append(record)
    return {
        "entries": entries,
        "unmapped": [record["command"] for record in entries if record["status"] == "unmapped"],
        "stale": sorted((mapped.keys() | excluded.keys()) - sources.keys()),
        "errors": errors,
        "scope_count": len(contract.get("entrypoint_inventory", [])),
    }
