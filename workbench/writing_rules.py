"""Check protected literals and observable pstack writing patterns."""

from collections import Counter
import re


FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
INLINE = re.compile(r'(?s)(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|(?:[^\s()]+|\([^()\n]*\))+)', re.M)
REFERENCE = re.compile(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|\S+)', re.M)
SHORT_REFERENCE = re.compile(r'!?\[[^\]\n]+\]\[([^\]\n]*)\]')
HTML = re.compile(r'</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*?)?\s*/?>')
URL = re.compile(r'https?://[^\s<>`"\]\)]+')
PATH = re.compile(r'(?<![\w:/-])(?:\.?\.?/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+\b')
FLAG = re.compile(r'(?<![\w-])--[a-zA-Z][a-zA-Z0-9-]*\b')
NUMBER = re.compile(r'(?<![\w.])[-+~]?\d+(?:[.,]\d+)*(?:%|[ \t]*(?:ms|GB|MB|GiB|MiB|KiB|kB|Hz)\b)?(?!\w|[.,]\d)')
CJK_QUANTITY = re.compile(r'[零〇一二两三四五六七八九十百千万亿]+(?:份|次|毫秒|秒|分钟|小时|天|周|个月|个|项)')
LIST_MARKER = re.compile(r'^[ \t]{0,3}\d{1,9}[.)][ \t]+', re.M)
HEADING = re.compile(r'^(#{1,6})[ \t]+\S', re.M)

# These IDs stay aligned with the pinned unslop prompt. The editor and reviewer
# assess the contextual rules where a mechanical check would misread the prose.
UNSLOP = {
    'U3': 'Drop trailing -ing phrases that add no fact.',
    'U5': 'Name the source for attributions or remove unsupported generalizations.',
    'U7': 'Replace inflated stock vocabulary with common words.',
    'U8': 'Prefer is or has to stock descriptions such as serves as.',
    'U9': 'State the point directly without not just X, but Y.',
    'U10': 'Avoid forced groups of three.',
    'U11': 'Use one name for the same thing.',
    'U12': 'Use from X to Y only on a real scale.',
    'U13': 'Use sentences or commas instead of dashes and substitute parentheses.',
    'U14': 'Use a colon before an example or list.',
    'U15': 'Use emphasis sparingly.',
    'U16': 'Remove bold labels that repeat the sentence.',
    'U17': 'Use sentence case headings.',
    'U18': 'Remove decorative emoji.',
    'U19': 'Use straight quotes where appropriate for the language.',
    'U20': 'Remove chatbot phrases.',
    'U22': 'Start directly without praise or agreement.',
    'U23': 'Cut filler.',
    'U24': 'Remove redundant hedges while retaining uncertainty.',
    'U25': 'End with a concrete fact or plan when needed.',
    'U26': 'Replace abstract metaphor nouns with the actual mechanism when they are filler.',
    'U27': 'Name the behavior or measurement.',
    'U28': 'Give each sentence one idea.',
    'U29': 'Name the actor when it matters.',
    'U30': 'Prefer specific verbs and quantities to empty adverbs.',
    'U31': 'Use the plain word.',
    'U32': 'State what happens literally.',
    'U33': 'Use complete sentences and explain unexplained abbreviations or arrows.',
}
TECHNICAL = {
    'T1': 'Pick one Diátaxis structure and link to separate material.',
    'T2': 'Use present tense and speak to the reader. Give the condition before the action.',
    'T3': 'Write one action per sentence. Split long instructions when they obscure the sequence.',
    'T4': 'Use exact commands, flags, files and symbols. Use meaningful link labels.',
    'T5': 'Use sentence-case headings and parallel list items. Number procedures in order.',
    'T6': 'Remove ambiguous pronouns, conjunctions and noun chains. Keep terms consistent.',
    'T7': 'Keep repository conventions, indent new indented snippets with tabs, and show how to reproduce counts and tree claims.',
    'T8': 'For PRs and commits, lead with the reason and link to evidence. Keep a PR briefing under a minute.',
    'T9': 'Use separate sentences instead of semicolons or em dashes. Avoid slashes as connectors.',
}
STRUCTURES = {
    'tutorial': 'Say what the learner will build. Give a visible result for each step and link to explanations.',
    'how-to': 'Name the task. Give the steps, prerequisites, conditions, and forks a competent reader needs.',
    'reference': 'Organize facts for lookup in the same order as the documented interface.',
    'explanation': 'Explain a bounded topic with context, decisions, constraints, and alternatives.',
}
PATTERNS = {
    'U7': r'\b(?:additionally|crucial|delve|foster|pivotal|showcase|testament|underscore|vibrant)\b',
    'U8': r'\b(?:serves as|stands as|boasts)\b',
    'U9': r'\bnot (?:just|only)\b.{1,100}\bbut\b',
    'U13': r'[—–]|\s-\s',
    'U19': r'[“”‘’]',
    'U20': r'\b(?:i hope this helps|let me know if)\b',
    'U23': r'\b(?:in order to|it is important to note that|it is worth noting that)\b|值得注意的是|不难看出',
    'U31': r'\b(?:utilize|utilizes|utilizing|leverage|leveraging)\b',
}


def markdown(text):
    """Separate prose from code while retaining source line numbers."""
    code, prose, block, active = [], [], [], None
    for line in text.splitlines(keepends=True):
        match = FENCE.match(line)
        if active:
            block.append(line)
            prose.append('\n')
            if match and match[1][0] == active[0] and len(match[1]) >= len(active) and not match[2].strip():
                code.append(''.join(block))
                active, block = None, []
        elif match:
            active, block = match[1], [line]
            prose.append('\n')
        elif line.startswith(('\t', '    ')) and line.strip():
            code.append(line)
            prose.append('\n')
        else:
            prose.append(line)
    return ''.join(prose), code, active is None


def _prose(text):
    prose, _, _ = markdown(text)
    def blank(match):
        return re.sub(r'[^\r\n]', ' ', match[0])
    prose = INLINE.sub(blank, prose)
    prose = LINK.sub(lambda match: match[0].split('](', 1)[0] + '\n' * match[0].count('\n'), prose)
    return re.sub(r'&(?:[A-Za-z]+|#\d+|#x[\da-fA-F]+);', ' ', URL.sub(blank, HTML.sub(blank, prose)))


def style(text):
    prose = _prose(text)
    return {rule: [line for line, source in enumerate(prose.splitlines(), 1) if re.search(pattern, source, re.I)]
            for rule, pattern in PATTERNS.items()}


def literals(text):
    prose, fences, closed = markdown(text)
    inline = [match[0] for match in INLINE.finditer(prose)]
    plain = INLINE.sub(' ', prose)
    destinations = [(match[0].startswith('!'), match[1]) for match in LINK.finditer(plain)]
    references = [(label.casefold(), target) for label, target in REFERENCE.findall(plain)]
    uses = [(match[0].startswith('!'), (match[1] or match[0]).casefold())
            for match in SHORT_REFERENCE.finditer(plain)]
    urls = {url.rstrip('.,;:') for url in URL.findall(plain)}
    values = LIST_MARKER.sub('', plain)
    return {
        'balanced_code': closed, 'code blocks and indents': fences, 'inline code': Counter(inline),
        'link destinations': Counter(destinations), 'link definitions': Counter(references),
        'reference link uses': Counter(uses), 'HTML tags': Counter(HTML.findall(plain)),
        'URLs': urls, 'file paths': set(PATH.findall(plain)),
        'flags': set(FLAG.findall(plain)),
        'numbers': {' '.join(value.split()) for value in NUMBER.findall(values)} | set(CJK_QUANTITY.findall(values)),
    }


def problems(original, candidate, *, preserve, article=False):
    before, after = literals(original), literals(candidate)
    problems = []
    if not after['balanced_code']:
        problems.append('unclosed code fence')
    if preserve:
        for key in before.keys() - {'balanced_code'}:
            if before[key] != after[key]:
                problems.append(f'changed {key}')
    body, _, _ = markdown(candidate)
    levels = [len(match[1]) for match in HEADING.finditer(body)]
    if article and (not levels or levels[0] != 1 or levels.count(1) != 1
                    or any(right > left + 1 for left, right in zip(levels, levels[1:]))):
        problems.append('an article needs one H1 and no skipped heading levels')
    if len(re.findall(r'[;；]', _prose(candidate))) > len(re.findall(r'[;；]', _prose(original))):
        problems.append('introduced a semicolon in prose')
    placeholders = re.compile(r'(?im)^\s*(?:TODO|TBD|\[INSERT\b)')
    if len(placeholders.findall(body)) > len(placeholders.findall(markdown(original)[0])):
        problems.append('introduced a placeholder')
    return sorted(problems)
