"""Reconcile tracked literal command registrations with a project verification map."""

import re


_COMMAND = re.compile(r"[a-z][a-z0-9-]{0,55}")
_RECEIVER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,50}")
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
        raise ValueError("entrypoint_inventory needs at most twenty command scopes")
    namespaces = set()
    claimed = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"namespace", "sources", "excluded"}:
            raise ValueError("an entrypoint scope needs namespace, sources and excluded")
        namespace = scope["namespace"]
        if not _words(namespace) or namespace in namespaces:
            raise ValueError("entrypoint scope must have a unique command namespace")
        namespaces.add(namespace)
        sources = scope["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
            raise ValueError("an entrypoint scope needs one to twelve source bindings")
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"path", "receiver"}:
                raise ValueError("a source binding needs path and receiver")
            check_path(source["path"])
            receiver = source["receiver"]
            if not isinstance(receiver, str) or not _RECEIVER.fullmatch(receiver):
                raise ValueError("entrypoint receiver must be a source identifier")
            binding = (source["path"], receiver)
            if binding in seen:
                raise ValueError("duplicate entrypoint source binding")
            seen.add(binding)
        excluded = scope["excluded"]
        if not isinstance(excluded, dict) or len(excluded) > 60:
            raise ValueError("excluded must map direct subcommands to specific reasons")
        for command, reason in excluded.items():
            if not _COMMAND.fullmatch(command):
                raise ValueError("excluded entry must be a direct subcommand")
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


def _registrations(text, receiver):
    tokens = [match for match in _TOKENS.finditer(text) if match.lastgroup != "comment"]
    for offset, token in enumerate(tokens):
        if token[0] != receiver or offset and tokens[offset - 1][0] in (".", "#"):
            continue
        following = tokens[offset + 1:offset + 6]
        if len(following) != 5:
            continue
        dot, method, begin, literal, end = following
        if (dot[0], method[0], begin[0], end[0]) != (".", "command", "(", ")"):
            continue
        if literal[0][0] not in ("'", '"') or not _COMMAND.fullmatch(literal[0][1:-1]):
            continue
        yield literal[0][1:-1], text.count("\n", 0, method.start()) + 1


def scan(contract, read_blob):
    sources, excluded, errors = {}, {}, []
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
            found = list(_registrations(text, source["receiver"]))
            if not found:
                errors.append({"namespace": namespace, "path": path, "error": "no literal command registrations found"})
            for name, line in found:
                key = f"{namespace} {name}"
                sources.setdefault(key, []).append({"path": path, "blob": blob, "line": line})
    entries = []
    for command, locations in sorted(sources.items()):
        record = {"command": command, "locations": locations, "status": "unmapped"}
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
