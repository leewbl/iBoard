"""Reconcile tracked command registrations and JSX controls with a verification map."""

import re


_COMMAND = re.compile(r"[a-z][a-z0-9-]{0,55}")
_RECEIVER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,50}")
_LABEL_KEY = re.compile(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*")
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_COMMAND_MODIFIERS = {"description", "option", "addOption", "requiredOption", "argument", "addHelpText", "action"}
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
            elif (source.get("kind") == "commander-factory"
                  and set(source) - {"receiver_calls"} == {"kind", "path", "owner", "receiver", "factory", "wrapper"}):
                identity = (source["owner"], source["factory"], source["receiver"])
                calls = source.get("receiver_calls", [])
                if (not isinstance(calls, list) or len(calls) > 6
                        or any(not isinstance(call, str) for call in calls) or len(set(calls)) != len(calls)):
                    raise ValueError("factory setup helpers need distinct source identifiers")
                symbols = (*identity, source["wrapper"], *calls)
                if isinstance(source["factory"], str) and source["factory"] in (source["receiver"], source["wrapper"]):
                    raise ValueError("factory, command receiver and wrapper must be separate symbols")
            elif set(source) == {"kind", "path", "component", "label_key"} and source["kind"] == "jsx-options":
                label_key = source["label_key"]
                if not isinstance(label_key, str) or len(label_key) > 120 or not _LABEL_KEY.fullmatch(label_key):
                    raise ValueError("JSX entrypoint binding requires a literal translation key")
                identity = (source["component"], label_key)
                symbols = identity[:1]
            else:
                raise ValueError("a source binding needs a command receiver, a factory or a JSX options selector")
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


def _command_loop_domains(tokens, receiver, *, factory_owners=None):
    """Resolve only immutable literal loops, and only direct uses in their body."""
    domains = {}
    for index, token in enumerate(tokens):
        if factory_owners is not None and index not in factory_owners:
            continue
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
        stop = _matching(tokens, brace)
        if factory_owners is not None and any(t[0] in {"break", "continue", "return", "throw"}
                                              for t in tokens[brace+1:stop]):
            continue
        for offset in _top_level(tokens, brace + 1, stop):
            if tokens[offset][0] != receiver:
                continue
            prefix = [t[0] for t in tokens[offset-3:offset]]
            assigned = (factory_owners is not None and len(prefix) == 3 and prefix[0] == "const"
                        and _RECEIVER.fullmatch(prefix[1]) and prefix[2] == "="
                        and (offset == brace + 4 or tokens[offset-4][0] == ";"))
            if assigned or offset == brace + 1 or tokens[offset - 1][0] == ";":
                domains[offset] = (name, values, stop)
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


def _strict_parameters(tokens, start, end):
    names = []
    for part in _comma_parts(tokens[start:end]):
        if (not part or not _RECEIVER.fullmatch(part[0][0]) or
                len(part) > 1 and (part[1][0] != ":" or any(t[0] == "=" for t in part))):
            raise ValueError("factory and owner require simple named parameters")
        names.append(part[0][0])
    if len(names) != len(set(names)):
        raise ValueError("factory or owner parameter is ambiguous")
    return names


def _owner_scope(tokens, owner):
    matches = [i for i in range(len(tokens) - 2)
               if [t[0] for t in tokens[i:i+3]] == ["function", owner, "("]]
    if len(matches) != 1:
        raise ValueError("expected one function declaration for the Commander factory owner")
    first = matches[0] + 2
    end = _matching(tokens, first)
    params = _strict_parameters(tokens, first + 1, end)
    body = end + 1
    if body + 1 < len(tokens) and tokens[body][0] == ":" and _RECEIVER.fullmatch(tokens[body + 1][0]):
        body += 2
    if body >= len(tokens) or tokens[body][0] != "{":
        raise ValueError("expected a named factory owner with a function body")
    return params, body, _matching(tokens, body)


def _fluent_chain(tokens, start, stop, *, aliases=False):
    """Read known Commander modifiers, retaining literal aliases only at a callsite."""
    entries = []
    while start < stop:
        if start + 2 >= stop or tokens[start][0] != "." or tokens[start + 2][0] != "(":
            raise ValueError("unsupported Commander factory chain")
        method = tokens[start + 1][0]
        end = _matching(tokens, start + 2)
        if end >= stop:
            raise ValueError("incomplete Commander factory chain")
        if aliases and method == "alias":
            name = _command_name(_literal(tokens[start + 3][0])) if end == start + 4 else None
            if not name:
                raise ValueError("Commander factory alias requires one literal name")
            entries.append((name, tokens[start + 1].start()))
        elif method not in _COMMAND_MODIFIERS:
            raise ValueError(f"unsupported Commander factory modifier {method}")
        start = end + 1
    return entries


def _factory_definition(tokens, declaration, stop, receiver, wrapper):
    offset = declaration + 2
    if [t[0] for t in tokens[offset:offset+2]] != ["=", "("]:
        raise ValueError("expected a const Commander factory with an arrow function")
    params_end = _matching(tokens, offset + 1)
    parameters = _strict_parameters(tokens, offset + 2, params_end)
    if not parameters or [t[0] for t in tokens[params_end+1:params_end+5]] != ["=", ">", wrapper, "("]:
        raise ValueError("Commander factory must immediately wrap the registered command")
    wrapper_start = params_end + 4
    wrapper_end = _matching(tokens, wrapper_start)
    receiver_offset = wrapper_start + 1
    end = next((i for i in _top_level(tokens, wrapper_end + 1, stop) if tokens[i][0] == ";"), stop)
    inner = [t[0] for t in tokens[receiver_offset:receiver_offset+6]]
    if inner != [receiver, ".", "command", "(", parameters[0], ")"]:
        raise ValueError("Commander factory must register its first parameter on the declared receiver")
    _fluent_chain(tokens, receiver_offset + 6, wrapper_end)
    _fluent_chain(tokens, wrapper_end + 1, end)
    return receiver_offset


def _check_factory_result(tokens, call, end, boundary):
    prefix = [t[0] for t in tokens[call-3:call]]
    if len(prefix) != 3 or prefix[0] != "const" or prefix[2] != "=":
        return
    receiver = prefix[1]
    for i in range(end + 1, boundary):
        if tokens[i][0] != receiver:
            continue
        access = [t[0] for t in tokens[i+1:i+4]]
        if len(access) != 3 or access[0] != "." or access[1] not in _COMMAND_MODIFIERS or access[2] != "(":
            raise ValueError("unsupported use of the assigned Commander factory result")


def _factory_registrations(text, source):
    tokens = [match for match in _TOKENS.finditer(text) if match.lastgroup != "comment"]
    receiver, factory = source["receiver"], source["factory"]
    params, body, stop = _owner_scope(tokens, source["owner"])
    direct = set(_top_level(tokens, body + 1, stop))
    loops = _command_loop_domains(tokens, factory, factory_owners=direct)
    declarations = [i for i in direct if tokens[i][0] == "const" and tokens[i+1][0] == factory]
    if len(declarations) != 1 or factory in params:
        raise ValueError("expected exactly one owner-local const Commander factory")
    declaration = declarations[0]
    registration = _factory_definition(tokens, declaration, stop, receiver, source["wrapper"])
    line = lambda offset: text.count("\n", 0, offset) + 1
    registration_line = line(tokens[registration + 2].start())
    receiver_bindings = [
        i for i in range(body + 2, stop) if tokens[i][0] == receiver
        and tokens[i-1][0] in ("const", "let", "var")
    ]
    if len(receiver_bindings) + (receiver in params) != 1 or any(
        i - 1 not in direct or i >= declaration or tokens[i-1][0] != "const" for i in receiver_bindings
    ):
        raise ValueError("Commander factory receiver must be one owner-local const or parameter")
    found, unresolved, seen = [], [], set()
    for i in range(body + 1, stop):
        token = tokens[i][0]
        if i in direct and token in {"if", "try", "switch", "while", "do", "return", "throw"}:
            unresolved.append((line(tokens[i].start()), "unsupported control flow in Commander factory owner"))
        if token == receiver and i != registration and i not in receiver_bindings:
            known_setup = (i - 2 in direct and i >= body + 3
                           and tokens[i-2][0] in source.get("receiver_calls", ())
                           and tokens[i-1][0] == "(" and [t[0] for t in tokens[i+1:i+3]] == [")", ";"]
                           and tokens[i-3][0] in ("{", "}", ";"))
            if not known_setup:
                unresolved.append((line(tokens[i].start()), "unexpected use of Commander factory receiver"))
        if token != factory or i == declaration + 1:
            continue
        try:
            domain = loops.get(i)
            direct_call = i in direct and tokens[i-1][0] in ("{", "}", ";")
            if (not (direct_call or domain) or i <= declaration or tokens[i+1][0] != "("):
                raise ValueError("Commander factory must be called directly in its owner")
            end = _matching(tokens, i + 1)
            names = []
            if i + 3 <= end and tokens[i+3][0] in (",", ")"):
                arg = tokens[i+2][0]
                literal = _command_name(_literal(arg))
                if literal:
                    names = [literal]
                elif domain and arg == domain[0]:
                    names = domain[1]
            if not names:
                raise ValueError("Commander factory requires a literal command name")
            boundary = domain[2] if domain else stop
            end_chain = next((j for j in _top_level(tokens, end + 1, boundary) if tokens[j][0] == ";"), boundary)
            aliases = _fluent_chain(tokens, end + 1, end_chain, aliases=len(names) == 1)
            _check_factory_result(tokens, i, end_chain, boundary)
            entries = [(value, tokens[i].start()) for value in names] + aliases
            for name, offset in entries:
                if name in seen:
                    unresolved.append((line(offset), "duplicate Commander factory command or alias"))
                seen.add(name)
                found.append((name, line(offset), registration_line))
        except ValueError as problem:
            unresolved.append((line(tokens[i].start()), str(problem)))
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
                elif kind == "commander-factory":
                    found, unresolved = _factory_registrations(text, source)
                    kind = "command"
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
            for name, line, *registration in found:
                key = f"{namespace} {name}"
                if key in kinds and kinds[key] != kind:
                    errors.append({"namespace": namespace, "path": path, "blob": blob, "line": line,
                                   "error": "entrypoint name is shared by command and JSX bindings"})
                    continue
                location = {"path": path, "blob": blob, "line": line}
                if registration:
                    location["registration_line"] = registration[0]
                sources.setdefault(key, []).append(location)
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
