"""Validate and render progressively disclosed, evidence-linked teaching diagrams."""

from html import escape
import re
from textwrap import wrap


class LessonError(ValueError):
    pass


PROMPT = (' Also return lesson as {"summary":"one or two plain sentences","steps":[{"text":"what a reader sees '
          'and why it happens", "refs":["how:1"], "nodes":[{"id":"n1","label":"First part"},'
          '{"id":"n2","label":"Second part"}], "edge":{"from":"n1","to":"n2","label":"short action"}}]}. '
          'The example step is the FIRST figure. Do not add a separate node-only step before it. '
          'Use the audience and their question to '
          'choose what to show. Use an empty steps array if a figure adds no value. For a flow with more than two '
          'parts, start with exactly two nodes and one edge. Redraw that view and add just one connected node '
          'with its edge, or one edge between existing nodes, per later step. Keep the flows small and put long '
          'function names in the explanation or evidence when a plain node label is enough. Include a '
          'concrete event or input. Each arrow must point to the actual target of that action or condition. '
          'Pick refs from the named lesson_refs entries. Use the specific findings whose text and citations '
          'support that step. Keep the confidence of each '
          'historical finding and put uncertain reasons in conditional terms.')


def _line(text, size):
    return isinstance(text, str) and 1 <= len(text.strip()) <= size and text.isprintable()


def finding_refs(prior):
    return {f'{stage}:{index}': finding for stage in ('how', 'why')
            for index, finding in enumerate(prior[stage]['findings'], 1)}


def validate(raw, prior):
    if (not isinstance(raw, dict) or set(raw) != {'summary', 'steps'} or not _line(raw['summary'], 420)
            or not isinstance(raw['steps'], list) or len(raw['steps']) > 7):
        raise LessonError('lesson needs a short summary and up to seven progressive steps')
    allowed = finding_refs(prior)
    nodes, pairs, steps = {}, set(), []
    for index, step in enumerate(raw['steps']):
        if (not isinstance(step, dict) or set(step) != {'text', 'refs', 'nodes', 'edge'}
                or not _line(step['text'], 540) or not isinstance(step['refs'], list)
                or not 1 <= len(step['refs']) <= 5
                or not all(isinstance(ref, str) and ref in allowed for ref in step['refs'])
                or len(set(step['refs'])) != len(step['refs'])):
            raise LessonError('each lesson step needs a short explanation and existing how or why references')
        added = step['nodes']
        if not isinstance(added, list) or len(added) > 8:
            raise LessonError('supply diagram nodes as a bounded list')
        frame = {}
        for node in added:
            if (not isinstance(node, dict) or set(node) != {'id', 'label'} or not _line(node['label'], 80)
                    or not isinstance(node['id'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,19}', node['id'])
                    or node['id'] in frame):
                raise LessonError('diagram nodes need a unique ID and label of up to 80 characters')
            frame[node['id']] = node['label']
        if nodes.keys() & frame.keys():
            if list(frame.items())[:len(nodes)] != list(nodes.items()):
                raise LessonError('a repeated frame must begin with all earlier nodes unchanged and in order')
            added = added[len(nodes):]
        names = {node['id'] for node in added}
        if (index == 0 and len(names) != 2) or (index > 0 and len(names) > 1):
            raise LessonError('start with two nodes and introduce at most one new node per later step')
        edge = step['edge']
        if (not isinstance(edge, dict) or set(edge) != {'from', 'to', 'label'} or not _line(edge['label'], 65)
                or not all(isinstance(edge[key], str) for key in ('from', 'to'))):
            raise LessonError('each step adds one named connection')
        pair = (edge['from'], edge['to'])
        if pair[0] == pair[1] or pair in pairs or not names <= set(pair) <= nodes.keys() | names:
            raise LessonError('connect the new node or add a new edge between already visible nodes')
        nodes.update((node['id'], node['label']) for node in added)
        pairs.add(pair)
        steps.append({**step, 'nodes': added})
    return {**raw, 'steps': steps}


STYLE = '''
:root{color-scheme:light dark;--paper:#f1f5fb;--panel:#fff;--ink:#14263e;--sub:#47576d;
--line:#c5d0df;--accent:#1d56b5;--node:#e8f1ff;--curve:#2557a5}
@media(prefers-color-scheme:dark){:root{--paper:#111826;--panel:#1a2537;--ink:#e7eefb;
--sub:#b8c6db;--line:#455772;--accent:#a4c7ff;--node:#233c61;--curve:#99bfff}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:820px;margin:auto;padding:2rem 1rem 4rem}
header{margin-bottom:2rem}h1{font-size:clamp(1.5rem,5vw,2.5rem);line-height:1.22;margin:0 0 .8rem}
h2{font-size:1.18rem;margin:1.8rem 0 .7rem}h3{font-size:1rem;margin:.8rem 0 .4rem}
p{margin:.75rem 0}code{font-size:.8em;overflow-wrap:anywhere}
.muted,.cite{color:var(--sub)}.cite{font-size:.85rem;overflow-wrap:anywhere}.badge{color:var(--accent);font-weight:650}
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:.7rem 0;overflow:hidden}
summary{cursor:pointer;padding:.8rem 1rem;font-weight:650;overflow-wrap:anywhere}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:-3px}details[open]>summary{border-bottom:1px solid var(--line)}
.step{padding:.1rem 1rem 1.1rem}.map{display:block;width:min(100%,420px);height:auto;margin:.8rem auto;
font-family:inherit;font-size:15px}.node{fill:var(--node);stroke:var(--curve);stroke-width:1.25}.flow{stroke:var(--curve);
stroke-width:1.5;fill:none}.label{fill:var(--ink);font-size:15px;font-weight:600}.index{fill:var(--panel);stroke:var(--curve)}
.number{fill:var(--ink);font-size:11px;font-weight:650}.edge-key{font-size:.88rem;color:var(--sub);padding-left:1.5rem}
ul{padding-left:1.3rem}li+li{margin-top:.5rem}small{display:block}.page-summary{font-size:1.12rem;max-width:65ch}
'''


def _diagram(nodes, edges, index, title):
    top = 12
    positions = {}
    for order, (key, label) in enumerate(nodes.items()):
        lines = wrap(label, 13 if re.search(r'[\u3000-\u9fff]', label) else 20)
        half = max(23, len(lines) * 7 + 9)
        positions[key] = (order, top + half, half, lines)
        top += 2 * half + 27
    height = top - 15
    marker = f'arrow-{index}'
    svg = [f'<svg class="map" viewBox="0 0 400 {height}" role="img" aria-label="{escape(title)}">',
           f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
           'markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
           'style="fill:var(--curve)"/></marker></defs>']
    bends = 0
    for order, edge in enumerate(edges, 1):
        start_order, start, start_half, _ = positions[edge['from']]
        end_order, end, end_half, _ = positions[edge['to']]
        if end_order - start_order == 1:
            x, y = 143, (start + start_half + end - end_half) // 2
            path = f'M {x} {start + start_half} V {end - end_half}'
        else:
            x, y = 276 + 16 * bends, (start + end) // 2
            bends += 1
            path = f'M 246 {start} H {x} V {end} H 248'
        svg.append(f'<path class="flow" d="{path}" marker-end="url(#{marker})"/>')
        svg.append(f'<circle class="index" cx="{x + 12}" cy="{y}" r="8"/>'
                   f'<text class="number" x="{x + 12}" y="{y + 4}" text-anchor="middle">{order}</text>')
    for key, (_, y, half, lines) in positions.items():
        svg.append(f'<g data-node="{escape(key)}"><rect class="node" x="39" y="{y - half}" '
                   f'width="207" height="{2 * half}" rx="8"/>')
        for line, row in enumerate(lines):
            offset = 5 + (line * 14) - (len(lines) - 1) * 7
            svg.append(f'<text class="label" x="143" y="{y + offset}" text-anchor="middle">{escape(row)}</text>')
        svg.append('</g>')
    return ''.join(svg) + '</svg>'


def _finding(finding, evidence, basis):
    sources = []
    for cite in finding['citations']:
        item = evidence[cite['id']]
        if item['kind'] == 'source':
            sources.append(f"{item['path']}:{cite['line']} · {item['commit'][:9]}")
        else:
            sources.append(item.get('url') or item['commit'])
    citation = ' · '.join(sources)
    return (f'<li><span class="badge">{basis[finding["basis"]]}</span> {escape(finding["text"])}'
            f'<small class="cite">{escape(citation)}</small></li>')


def render(report):
    prior = report['stages']
    final = prior['teach']
    lesson = validate(final['lesson'], prior)
    evidence = report['evidence']
    zh = bool(re.search(r'[\u4e00-\u9fff]', lesson['summary']))
    basis = {'observed': '有记录' if zh else 'Observed', 'inferred': '推断' if zh else 'Inferred'}
    steps_title, details_title, limits_title = ('运行过程', '解释与依据', '仍待确认') if zh else (
        'Follow the flow', 'Explanation and evidence', 'Open questions')
    step_label, evidence_label = ('第 {} 步', '依据') if zh else ('Step {}', 'Evidence')
    refs = finding_refs(prior)
    question = escape(report['question'])
    title = re.split(r'(?<=[。！？])|(?<=[.!?])\s+', report['question'].strip(), maxsplit=1)[0]
    title = escape(title if len(title) <= 88 else title[:87].rstrip() + '…')
    body = [f'<!doctype html><html lang="{"zh" if zh else "en"}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{question}</title><style>{STYLE}</style></head><body><main>'
            f'<header><h1>{title}</h1><p class="page-summary">{escape(lesson["summary"])}</p>'
            f'<code class="muted">{escape(report["commit"])}</code></header>']
    if lesson['steps']:
        body.append(f'<h2>{steps_title}</h2>')
    nodes, edges = {}, []
    for index, step in enumerate(lesson['steps'], 1):
        nodes.update({node['id']:node['label'] for node in step['nodes']})
        edges.append(step['edge'])
        step_title = step_label.format(index)
        body.append(f'<details{" open" if index == 1 else ""} data-step="{index}"><summary>'
                    f'{step_title} · {escape(step["edge"]["label"])}</summary><div class="step">'
                    f'<p>{escape(step["text"])}</p>{_diagram(nodes, edges, index, step_title)}<ol class="edge-key">')
        body.extend(f'<li>{escape(nodes[edge["from"]])} → {escape(nodes[edge["to"]])} · {escape(edge["label"])}</li>'
                    for edge in edges)
        body.append(f'</ol><h3>{evidence_label}</h3><ul>')
        body.extend(_finding(refs[ref], evidence, basis) for ref in step['refs'])
        body.append('</ul></div></details>')
    if final['findings']:
        body.append(f'<h2>{details_title}</h2><ul>')
        body.extend(_finding(finding, evidence, basis) for finding in final['findings'])
        body.append('</ul>')
    if prior['why']['findings']:
        history_title = '原始设计溯源' if zh else 'Original design history'
        body.append(f'<details><summary>{history_title}</summary><div class="step"><ul>')
        body.extend(_finding(finding, evidence, basis) for finding in prior['why']['findings'])
        body.append('</ul></div></details>')
    if any(prior[name]['unknowns'] for name in ('how', 'why', 'teach')):
        body.append(f'<h2>{limits_title}</h2>')
        labels = ('源码调查', '设计溯源', '综合说明') if zh else ('Code inspection', 'Design history', 'Synthesis')
        for name, label in zip(('how', 'why', 'teach'), labels):
            if prior[name]['unknowns']:
                body.append(f'<h3>{label}</h3><ul>')
                body.extend(f'<li>{escape(text)}</li>' for text in dict.fromkeys(prior[name]['unknowns']))
                body.append('</ul>')
    return ''.join(body) + '</main></body></html>\n'
