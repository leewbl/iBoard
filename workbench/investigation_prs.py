"""Find bounded GitHub pull request associations for selected commits."""

import re


MAX_COMMITS = 6
PAGE_SIZE = 5
MAX_PULLS = 2
SLUG = r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}'
PULL_URL = re.compile(rf'https://github\.com/({SLUG})/pull/([1-9][0-9]{{0,8}})')
COMMIT = re.compile(r'[0-9a-fA-F]{40}')
MERGED_AT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')


class LookupUnavailable(Exception):
    """A requested association page could not be fetched within the lookup budget."""


def _pull(repository, row):
    if not isinstance(row, dict):
        raise ValueError('invalid pull request association')
    number = row.get('number')
    url = row.get('html_url')
    match = PULL_URL.fullmatch(url) if isinstance(url, str) else None
    base = row.get('base')
    repo = base.get('repo') if isinstance(base, dict) else None
    name = repo.get('full_name') if isinstance(repo, dict) else None
    merged_at = row.get('merged_at')
    merged = row.get('merged', False)
    if (type(number) is not int or not match or int(match[2]) != number
            or not isinstance(name, str) or not re.fullmatch(SLUG, name)
            or match[1].lower() != repository or name.lower() != repository
            or type(merged) is not bool
            or (merged_at is not None and (not isinstance(merged_at, str) or not MERGED_AT.fullmatch(merged_at)))):
        raise ValueError('invalid pull request association')
    return merged or merged_at is not None, number, url


def discover(repository, commits, get):
    """Return up to two exact PR associations and the result of each bounded lookup."""
    if (not isinstance(repository, str) or not re.fullmatch(SLUG, repository)
            or any(part in ('.', '..') for part in repository.split('/'))):
        raise ValueError('select a GitHub repository as owner/repo')
    repo_key = repository.lower()
    unique_commits = {}
    for commit in commits:
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            raise ValueError('pull request discovery requires an exact 40-hex commit')
        unique_commits[commit.lower()] = None

    candidates, lookups, seen_pulls = [], [], set()
    for commit in list(unique_commits)[:MAX_COMMITS]:
        lookup = {'commit':commit}
        lookups.append(lookup)
        try:
            rows = get(f'repos/{repository}/commits/{commit}/pulls?per_page={PAGE_SIZE}&page=1')
        except LookupUnavailable:
            lookup['status'] = 'unavailable'
            continue
        try:
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise ValueError('invalid pull request association page')
            pulls = sorted((_pull(repo_key, row) for row in rows), key=lambda item: (not item[0], item[1], item[2]))
        except ValueError:
            lookup['status'] = 'invalid-response'
            continue

        lookup.update(status='found' if pulls else 'none', returned=len(pulls), page_limit_reached=len(pulls) == PAGE_SIZE)
        omitted = 0
        for _, number, url in pulls:
            if number in seen_pulls:
                continue
            seen_pulls.add(number)
            if len(candidates) < MAX_PULLS:
                candidates.append({'url':url, 'commit':commit})
            else:
                omitted += 1
        if omitted:
            lookup['omitted_candidates'] = omitted

    if len(unique_commits) > MAX_COMMITS:
        lookups[-1]['omitted_commit_lookups'] = len(unique_commits) - MAX_COMMITS
    return candidates, lookups
