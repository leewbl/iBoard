"""Parse and migrate the pinned pstack model-rule format as inert data."""
import re

DEFAULTS = {
    'feature, refactoring': 'claude-opus-5-5-xhigh-fast',
    'bug-fix': 'claude-opus-5-5-xhigh-fast',
    'perf-issue': 'claude-opus-5-5-xhigh-fast',
    'hillclimb': 'claude-opus-5-5-xhigh-fast',
    'judgment and prose': 'claude-opus-5-5-xhigh-fast',
    'arena runners': ['claude-fable-5-2-high', 'claude-opus-5-5-high-fast', 'gemini-4-neon-xhigh'],
    'arena cross-judge pool': ['claude-opus-5-5-high-fast', 'gemini-4-neon-xhigh'],
    'swarm workers': 'claude-opus-5-5-high-fast',
    'interrogate reviewers': ['claude-fable-5-2-high', 'claude-opus-5-5-high-fast', 'gemini-4-neon-xhigh'],
}
PANELS = {'arena runners', 'arena cross-judge pool', 'interrogate reviewers'}
V1_SCALARS = {'feature, refactoring', 'bug-fix', 'perf-issue', 'hillclimb', 'swarm workers'}
SLUG = re.compile(r'[a-z0-9][a-z0-9.-]{0,127}\Z')
ROLE = re.compile(r'[a-z][a-z0-9 ,_-]{0,99}\Z')
MAX_BYTES = 32_768


def parse(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('invalid model rule size')
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError as error:
        raise ValueError('model rule is not UTF-8') from error
    if not lines or lines[0] != '---' or len(lines) > 500:
        raise ValueError('missing model rule frontmatter')
    try:
        end = lines.index('---', 1)
    except ValueError as error:
        raise ValueError('missing model rule frontmatter end') from error
    metadata = {}
    for line in lines[1:end]:
        match = re.fullmatch(r'(description|alwaysApply):\s*(\S(?:.*\S)?)', line)
        if not match or match[1] in metadata or any(ord(c) < 32 for c in match[2]):
            raise ValueError('invalid model rule frontmatter')
        metadata[match[1]] = match[2]
    if set(metadata) != {'description', 'alwaysApply'} or metadata['alwaysApply'] != 'true':
        raise ValueError('invalid model rule frontmatter')
    roles, changes = {}, []
    version = budget = None
    for line in lines[end + 1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            mark = re.match(r'^#\s*(defaults|budget)\b', line)
            if not mark:
                continue
            if mark[1] == 'defaults':
                match = re.fullmatch(r'#\s*defaults:\s*([0-9]+)', line)
                if not match or version is not None or int(match[1]) not in (2, 3):
                    raise ValueError('invalid or duplicate defaults marker')
                version = int(match[1])
            else:
                match = re.fullmatch(r'#\s*budget:\s*(large|medium|small|unlimited)(?:\s*\((xhigh|high|medium)\))?', line)
                if not match or budget is not None:
                    raise ValueError('invalid or duplicate budget marker')
                budget = match[1]
                expected = {'large': 'xhigh', 'medium': 'high', 'small': 'medium', 'unlimited': 'xhigh'}
                if match[2] and match[2] != expected[budget]:
                    raise ValueError('budget effort conflicts')
            continue
        key, sep, value = line.partition(':')
        key = key.strip()
        if not sep or not ROLE.fullmatch(key) or key in roles:
            raise ValueError('invalid or duplicate role')
        values = [item.strip() for item in value.split(',')]
        if not 1 <= len(values) <= 20 or any(not SLUG.fullmatch(item) for item in values):
            raise ValueError('invalid model choice')
        if key not in DEFAULTS:
            changes.append({'role': key, 'before': values[0] if len(values) == 1 else values,
                            'after': None, 'reason': 'retired or unknown role'})
            roles[key] = None
        elif key in PANELS:
            roles[key] = values
        elif len(values) == 1:
            roles[key] = values[0]
        else:
            raise ValueError('single-model role has multiple choices')
    version = version or 1
    if budget is None:
        budget = 'large'
        changes.append({'role': 'budget', 'before': None, 'after': budget, 'reason': 'missing budget default'})
    elif budget == 'unlimited':
        if version == 3:
            raise ValueError('unlimited is unsupported in current defaults')
        changes.append({'role': 'budget', 'before': budget, 'after': 'large', 'reason': 'legacy budget'})
        budget = 'large'
    for key, default in DEFAULTS.items():
        if key not in roles:
            roles[key] = default.copy() if isinstance(default, list) else default
            changes.append({'role': key, 'before': None, 'after': roles[key], 'reason': 'missing role default'})
            continue
        before = roles[key]
        after = before.copy() if isinstance(before, list) else before
        reasons = []
        if isinstance(after, list):
            replaced = ['gemini-4-neon-xhigh' if item == 'gemini-4-neon-high' or
                        (item.startswith('gemini-4-') and not item.startswith('gemini-4-neon-')) else item
                        for item in after]
            if replaced != after:
                reasons.append('legacy Gemini panel entry')
            after = replaced
            if version < 3 and key == 'arena cross-judge pool' and len(after) == 3 and all(
                    sum(value.startswith(prefix) for value in after) == 1
                    for prefix in ('claude-fable-', 'claude-opus-', 'gemini-')):
                after = default.copy()
                reasons.append('legacy cross-judge panel')
            if version == 1:
                if len(after) == 2 and sum(v.startswith('claude-opus-') for v in after) == 1 and sum(
                        v.startswith('grok-') for v in after) == 1:
                    after = default.copy()
                    reasons.append('legacy Opus and Grok panel')
                else:
                    replaced = ['claude-opus-5-5-high-fast' if value.startswith('grok-') else value for value in after]
                    if replaced != after:
                        reasons.append('legacy Grok panel entry')
                    after = replaced
        elif version == 1 and key in V1_SCALARS and after.startswith('grok-'):
            after = default
            reasons.append('legacy Grok role default')
        if before != after:
            roles[key] = after
            changes.append({'role': key, 'before': before, 'after': after, 'reason': ', '.join(reasons)})
    return {'defaults_version': version, 'budget': budget,
            'roles': {key: roles[key] for key in DEFAULTS}, 'changes': changes}
