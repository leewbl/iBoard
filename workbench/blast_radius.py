"""Find source-backed impact hypotheses and verify their decisive facts before delivery."""

import argparse
import json
from pathlib import Path
import re
import time

from model import codex_review
from workbench import cli, delivery, interrogate, investigate
from workbench.automate_me import SHA, _digest, _file, _keep, _read, _require
from workbench.task_lock import locked_task


SOURCE = 'skills/blast-radius/SKILL.md'
ERRORS = (*interrogate.ERRORS, delivery.DeliveryError, investigate.InvestigationError)
FACT = re.compile(r'[a-z][a-z0-9-]{0,30}')
FACT_FIELDS = {'id', 'surface', 'invariant', 'risk', 'likelihood', 'impact', 'source', 'line'}


def _line(text, limit):
    _require(isinstance(text, str) and 1 <= len(text.strip()) <= limit and text.isprintable(),
             'impact descriptions need bounded single-line text')
    return text


def _facts(answer, sources):
    _require(isinstance(answer, dict) and set(answer) == {'summary', 'facts'}, 'invalid impact analysis')
    _line(answer['summary'], 450)
    rows = answer['facts']
    _require(isinstance(rows, list) and 1 <= len(rows) <= 6, 'select one to six important impact facts')
    seen = set()
    for row in rows:
        _require(isinstance(row, dict) and set(row) == FACT_FIELDS and FACT.fullmatch(row['id'])
                 and row['id'] not in seen, 'impact facts need distinct identifiers')
        seen.add(row['id'])
        for name in ('surface', 'invariant', 'risk'):
            _line(row[name], 450)
        _require(row['likelihood'] in ('low', 'medium', 'high', 'unknown')
                 and row['impact'] in ('low', 'medium', 'high'), 'choose explicit qualitative risk levels')
        source = sources.get(row['source'])
        _require(source is not None and type(row['line']) is int and source['start'] <= row['line'] <= source['end'],
                 'risk must cite a captured downstream source line')
    return rows


def _git_context(repo, baseline, candidate, selections):
    interrogate._commit_pair(repo, baseline, candidate)
    _require(1 <= len(selections) <= 16 and len(set(selections)) == len(selections),
             'select distinct downstream source locations')
    _, sources = investigate._collect(repo, candidate, selections, history=False, remaining=lambda: 12)
    diff = delivery.candidate_diff(repo, baseline, candidate).decode('utf-8')
    changed = set(interrogate._git(repo, 'diff', '--name-only', '--no-renames', '-z',
                                    baseline, candidate, '--').decode().rstrip('\0').split('\0'))
    _require(diff.strip() and '\0' not in diff, 'impact analysis needs a readable candidate diff')
    return diff, {key: {**entry, 'changed': entry['path'] in changed} for key, entry in sources.items()}


def _diff_excerpt(diff):
    return diff if len(diff) <= 2200 else diff[:2200] + '\n[diff excerpt shortened]\n'


def analyze(*, repo, baseline, candidate, intent_file, source, model, out, timeout=180, reviewer=codex_review.review):
    repo = Path(repo).expanduser().resolve(strict=True)
    intent = _read(intent_file, 8000).decode('utf-8')
    _require(intent.strip(), 'supply the original task request')
    diff, inputs = _git_context(repo, baseline, candidate, source)
    prompt = ('Find one or two decisive facts the change relies on in downstream behavior. '
              'Look past changed symbols into public responses, storage, default configuration, and downstream callers. '
              'Use only the supplied data and no tools. Every risk is a hypothesis until tested. '
              'Cite a captured source ID and its exact line. Give likelihood low, medium, high, or unknown and '
              'impact low, medium, or high. Include neither executable steps nor attack techniques. '
              'Return exactly this JSON shape, filling in the source and line from the input: '
              '{"summary":"one sentence","facts":[{"surface":"caller","invariant":"expected behavior",'
              '"risk":"consequence","likelihood":"medium","impact":"high","source":"S1","line":1}]}. '
              'Describe the condition that an ordinary product check '
              'must observe. Input data follows.\n' + json.dumps({'request': intent, 'diff': diff, 'sources': inputs},
                                                                  ensure_ascii=False))
    _require(len(prompt.encode()) <= 85_000 and type(timeout) is int and 1 <= timeout <= 300,
             'select a narrower source range and a 1–300 second analysis limit')
    deadline = time.monotonic() + timeout
    attempts = []
    for prefix in ('', 'The earlier response had invalid fields or line numbers. Use the exact JSON keys and a captured line.\n'):
        current = prefix + prompt
        remaining = int(deadline - time.monotonic() + 0.9)
        _require(remaining >= 1, 'impact analysis time expired')
        answer = reviewer(current, model, remaining)
        attempts.append(_digest(current.encode()))
        try:
            _require(isinstance(answer, dict) and set(answer) == {'summary', 'facts'}
                     and isinstance(answer['facts'], list) and all(
                         isinstance(row, dict) and set(row) == FACT_FIELDS - {'id'} for row in answer['facts']),
                     'impact analysis has missing or extra fields')
            rows = [{**row, 'id': f'fact-{number}'} for number, row in enumerate(answer['facts'], 1)]
            _facts({'summary': answer['summary'], 'facts': rows}, inputs)
        except (ValueError, TypeError) as error:
            if prefix:
                raise ValueError(f'impact analysis did not return cited facts: {error}') from error
        else:
            break
    packet = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'impact-analysis', 'baseline': baseline,
              'candidate': candidate, 'intent': intent, 'intent_sha256': _digest(intent.encode()),
              'selections': list(source), 'diff_sha256': _digest(diff.encode()), 'diff_excerpt': _diff_excerpt(diff),
              'sources': inputs, 'summary': answer['summary'], 'facts': rows,
              'review_model': model, 'prompt_sha256': attempts[-1], 'analysis_attempts': attempts}
    return {**_keep(out, packet), 'facts': len(rows)}


def _reference(path, sha256):
    _require(isinstance(sha256, str) and SHA.fullmatch(sha256), 'evidence SHA-256 is required')
    return {'path': str(_file(path)), 'sha256': sha256}


def _load(ref, limit):
    _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'} and _reference(**ref) == ref,
             'evidence location changed')
    return json.loads(_read(ref['path'], limit, ref['sha256']))


def _analysis(repo, ref):
    row = _load(ref, 180_000)
    _require(row['schema'] == cli.SCHEMA and row['source'] == SOURCE and row['kind'] == 'impact-analysis',
             'choose an impact analysis')
    diff, sources = _git_context(repo, row['baseline'], row['candidate'], row['selections'])
    _require(row['sources'] == sources and row['diff_sha256'] == _digest(diff.encode())
             and row['diff_excerpt'] == _diff_excerpt(diff)
             and row['intent_sha256'] == _digest(row['intent'].encode()), 'impact analysis source or request changed')
    _facts({key: row[key] for key in ('summary', 'facts')}, sources)
    return row


def _scope(analysis, selected, directory):
    _require(isinstance(selected, dict) and set(selected) == {'checks', 'required', 'interrogate'},
             'supply check bindings, required fact IDs, and Interrogate settings')
    ids = {row['id'] for row in analysis['facts']}
    checks, required = selected['checks'], selected['required']
    _require(isinstance(checks, dict) and set(checks) <= ids and
             all(isinstance(probe, str) and FACT.fullmatch(probe) for probe in checks.values()) and
             len(set(checks.values())) == len(checks) and isinstance(required, list) and
             1 <= len(required) <= 3 and len(set(required)) == len(required) and set(required) <= ids,
             'pick distinct approved probes and one to three required facts')
    config = selected['interrogate']
    if checks:
        _require(isinstance(config, dict), 'provide Interrogate settings for the selected checks')
        config = interrogate.settings({**config, 'strict_evidence': True}, directory=directory)
        _require(set(checks.values()) == {row['id'] for row in config['probes']},
                 'map each declared probe to exactly one impact fact')
    else:
        _require(config is None, 'an analysis without approved checks has no execution settings')
    return {'checks': checks, 'required': required, 'interrogate': config}


def _intent(row, scope):
    facts = []
    for fact in row['facts']:
        if fact['id'] in scope['checks']:
            source = row['sources'][fact['source']]
            facts.append({'probe': scope['checks'][fact['id']], 'surface': fact['surface'],
                          'invariant': fact['invariant'], 'risk': fact['risk'],
                          'line_text': source['text'].splitlines()[fact['line'] - source['start']],
                          'source_file_changed': source['changed'],
                          'source': f"{source['path']}:{fact['line']}"})
    packet = ('Original request:\n' + row['intent'] + '\nVerified candidate diff excerpt:\n' + row['diff_excerpt']
              + '\nSelected impact facts:\n' + json.dumps(facts, ensure_ascii=False))
    _require(len(packet.encode()) <= 8000, 'select less downstream text for the independent reviewers')
    return packet


def _regressions(repo, row, scope, execution, retained):
    """Treat repeatable mismatches as observations and leave other failures unproven."""
    root = Path(execution['path']).parent
    config = scope['interrogate']
    if (retained.get('baseline'), retained.get('candidate'), retained.get('request_sha256')) != (
            row['baseline'], row['candidate'], _digest(_intent(row, scope).encode())):
        return set()
    confirmed, containers = set(), set()
    sources = interrogate._sources(repo, row['baseline'], row['candidate'], config)
    for probe in config['probes']:
        ident = probe['id']
        if ident not in retained.get('findings', []):
            continue
        try:
            compared = []
            for side in interrogate.SIDES:
                run = retained['probes'][ident][side]
                raw = interrogate._load(root, run['evidence'], f'{ident}-{side}.json', 16000000)
                _require((raw['baseline'], raw['candidate'], raw['request_sha256'], raw['probe'], raw['side'],
                          raw['source_sha256']) == (row['baseline'], row['candidate'], retained['request_sha256'],
                          ident, side, sources[ident]['sha256']), 'reproduction identity changed')
                value = raw['execution']
                outcome = interrogate._outcome(value, probe, config, retained['image'], root / f'probe-{side}', containers)
                _require(outcome == run['outcome'] == 'mismatch', 'real execution mismatch was not confirmed')
                compared.append(interrogate._excerpt(value, outcome, probe['marker'], strict=True))
            if compared[0] == compared[1]:
                confirmed.add(ident)
        except ERRORS:
            continue
    return confirmed


def _outcome(repo, analysis, scope, execution, *, audit=interrogate.validate_receipt):
    observations = False
    reason = None
    regressions = set()
    if execution:
        retained = _load(execution, 800000)
        try:
            intent = _intent(analysis, scope)
            _require(retained.get('intent') == intent, 'paired review was for another impact request')
            audit(execution['path'], retained, repo=repo, baseline=analysis['baseline'], candidate=analysis['candidate'],
                  request_sha256=_digest(intent.encode()), config=scope['interrogate'])
            observations = True
        except ERRORS as error:
            cause = retained.get('reason')
            reason = (f'{error}. Runner: {cause}' if isinstance(cause, str) and cause else str(error))[:900]
            regressions = _regressions(repo, analysis, scope, execution, retained)
    facts = {}
    for row in analysis['facts']:
        proof = scope['checks'].get(row['id'])
        excerpt = analysis['sources'][row['source']]
        clear = observations and proof is not None
        status = 'proven' if clear else 'regression-observed' if proof in regressions else 'unproven'
        facts[row['id']] = {'surface': row['surface'], 'invariant': row['invariant'], 'risk': row['risk'],
            'likelihood': row['likelihood'], 'impact': row['impact'], 'path': excerpt['path'], 'line': row['line'],
            'outside_diff': not excerpt['changed'], 'probe': proof, 'status': status,
            'proof_level': 'source-anchor' if status == 'unproven' else 'executed-real-code'}
    clear = all(facts[key]['status'] == 'proven' for key in scope['required']) and reason is None
    return ('clear' if clear else 'blocked'), facts, reason


def run(*, repo, analysis, sha256, probes, out, runner=interrogate.run):
    repo = Path(repo).expanduser().resolve(strict=True)
    ref = _reference(analysis, sha256)
    row = _analysis(repo, ref)
    checks = _file(probes)
    scope = _scope(row, json.loads(_read(checks, 100000)), checks.parent)
    directory = Path(out).expanduser().resolve()
    _require(not directory.is_relative_to(repo), 'keep impact evidence outside the source checkout')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    execution = None
    if scope['checks']:
        prompt = _intent(row, scope)
        receipt = runner(repo=repo, baseline=row['baseline'], candidate=row['candidate'],
                         request_sha256=_digest(prompt.encode()), intent=prompt,
                         config=scope['interrogate'], out=directory / 'interrogate')
        execution = _reference(receipt['report'], receipt['report_sha256'])
    status, facts, reason = _outcome(repo, row, scope, execution)
    report = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'blast-radius', 'analysis': ref,
              'baseline': row['baseline'], 'candidate': row['candidate'], 'intent_sha256': row['intent_sha256'],
              'scope': scope, 'execution': execution, 'status': status, 'facts': facts, 'reason': reason}
    return {**_keep(directory / 'report.json', report), 'status': status, 'facts': facts, 'reason': reason}


def require(data, head, request_path=None):
    try:
        bind = data['blast_radius']
        ref = bind['report']
        original = bind['request']
        _require(set(original) == {'path', 'sha256'} and _reference(**original) == original
                 and (request_path is None or _file(request_path) == Path(original['path'])), 'task request path changed')
        _read(original['path'], 8000, original['sha256'])
        report = _load(ref, 180000)
        row = _analysis(data['repo'], report['analysis'])
        _require(report['kind'] == 'blast-radius' and report['schema'] == cli.SCHEMA and report['source'] == SOURCE
                 and (row['baseline'], row['candidate'], row['intent_sha256']) ==
                 (data['base_commit'], head, original['sha256'])
                 and (report['baseline'], report['candidate'], report['intent_sha256']) ==
                 (row['baseline'], row['candidate'], row['intent_sha256']), 'impact evidence is for another exact task')
        scope = _scope(row, report['scope'], Path(ref['path']).parent)
        status, facts, reason = _outcome(data['repo'], row, scope, report['execution'])
        _require(status == report['status'] == 'clear' and facts == report['facts'] and reason == report['reason'],
                 'required blast-radius facts are unproven or the evidence changed')
        return report
    except ERRORS as error:
        raise cli.WorkbenchError(f'blast-radius evidence: {error}') from error


@locked_task('task_path')
def attach(task_path, report, sha256):
    task = _file(task_path)
    data = cli.read(task)
    request = _file(task.parent / 'issue.txt')
    data['blast_radius'] = {'report': _reference(report, sha256),
                            'request': _reference(request, cli.digest(request))}
    head = cli.sha(data['repo'], 'HEAD')
    require(data, head, request)
    cli.write(task, data)
    return {'head': head, 'blast_radius': data['blast_radius']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for label in ('analyze', 'run', 'attach'):
        stage = sub.add_parser(label)
        names = {'analyze': ('repo', 'baseline', 'candidate', 'intent-file', 'model', 'out'),
                 'run': ('repo', 'analysis', 'sha256', 'probes', 'out'), 'attach': ('report', 'sha256')}
        for name in names[label]:
            stage.add_argument('--' + name, required=True)
        if label == 'attach':
            stage.add_argument('--task', dest='task_path', required=True)
        if label == 'analyze':
            stage.add_argument('--source', action='append', required=True)
            stage.add_argument('--timeout', type=int, default=180)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'analyze': analyze, 'run': run, 'attach': attach}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(action == 'run' and result['status'] != 'clear')


if __name__ == '__main__':
    raise SystemExit(main())
