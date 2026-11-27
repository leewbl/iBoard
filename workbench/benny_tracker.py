"""GitHub Issues adapter for Benny's report handoff."""

import json
import subprocess
from urllib.parse import quote

from workbench import benny_triage, delivery


def issue_url(repo, number):
    return f'https://github.com/{repo}/issues/{number}'


def _issue(row, repo, number=None):
    if (not isinstance(row, dict) or 'pull_request' in row or type(row.get('number')) is not int
            or row['number'] < 1 or (number is not None and row['number'] != number)
            or str(row.get('html_url', '')).casefold() != issue_url(repo, row['number']).casefold()
            or row.get('state') not in ('open', 'closed')):
        raise delivery.DeliveryError('GitHub did not confirm the expected issue identity')
    return row


def owns(row, actor, marker):
    return (isinstance(row.get('user'), dict) and row['user'].get('login') == actor
            and marker in str(row.get('body', '')).splitlines())


class Tracker(benny_triage.Tracker):
    def _call(self, command, *args):
        return super()._call(command, '--hostname', 'github.com', *args)

    def _write(self, repo, endpoint, method, value):
        try:
            process = subprocess.run(['gh', 'api', '--hostname', 'github.com', '--method', method,
                                      f'repos/{repo}/{endpoint}', '--input', '-'], input=json.dumps(value),
                                     capture_output=True, text=True, timeout=self._bounded(35), check=False)
            if process.returncode:
                raise delivery.DeliveryError('GitHub did not acknowledge the tracker write')
            return json.loads(process.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise delivery.DeliveryError('GitHub tracker write outcome is unknown') from error

    def identity(self):
        row = self._call('api', '--method', 'GET', 'user')
        actor = row.get('login') if isinstance(row, dict) else None
        if not isinstance(actor, str) or not actor:
            raise delivery.DeliveryError('GitHub tracker writer identity is unavailable')
        return actor

    def preflight(self, repo, labels, *, create):
        data = self._call('api', '--method', 'GET', f'repos/{repo}')
        if (not isinstance(data, dict) or str(data.get('full_name', '')).casefold() != repo.casefold()
                or data.get('has_issues') is not True or data.get('archived') or data.get('disabled')):
            raise delivery.DeliveryError('GitHub Issues is unavailable for the selected tracker')
        permissions = data.get('permissions')
        if create and (not isinstance(permissions, dict) or not any(
                permissions.get(role) is True for role in ('triage', 'push', 'maintain', 'admin'))):
            raise delivery.DeliveryError('GitHub cannot confirm permission to compensate by closing a newly created issue')
        for label in labels:
            row = self._call('api', '--method', 'GET', f'repos/{repo}/labels/{quote(label, safe="")}')
            if not isinstance(row, dict) or row.get('name') != label:
                raise delivery.DeliveryError('GitHub did not resolve the configured issue label')

    def source_linked(self, repo, source):
        evidence, searches = benny_triage._search(self, repo, [source], source)
        links = [item['url'] for item in evidence.values() if item['item_type'] == 'issue' and item['source_linked']]
        if not links and not all(row['complete'] for row in searches):
            raise delivery.DeliveryError('the current GitHub source-link check was incomplete')
        return links

    def read(self, repo, number):
        return _issue(self._call('api', '--method', 'GET', f'repos/{repo}/issues/{number}'), repo, number)

    def recent(self, repo, actor, marker):
        # In recovery prefer the repository's current issue list over indexed search.
        rows = self._call('api', '--method', 'GET', 'repos/' + repo + '/issues?state=all&sort=created&direction=desc&per_page=50')
        if not isinstance(rows, list) or len(rows) > 50:
            raise delivery.DeliveryError('GitHub issue recovery list was invalid')
        found = [_issue(row, repo) for row in rows if isinstance(row, dict) and 'pull_request' not in row
                 and owns(row, actor, marker)]
        if len(found) > 1:
            raise delivery.DeliveryError('multiple issues matched the frozen run marker')
        return found[0] if found else None

    def comments(self, repo, number):
        endpoint = f'repos/{repo}/issues/{number}/comments?per_page=50'
        rows = self._call('api', '--method', 'GET', endpoint)
        if not isinstance(rows, list) or len(rows) >= 50 or any(not isinstance(row, dict) for row in rows):
            raise delivery.DeliveryError('GitHub comment history was incomplete')
        return rows

    def create(self, repo, title, body, labels):
        return _issue(self._write(repo, 'issues', 'POST', {'title': title, 'body': body, 'labels': labels}), repo)

    def comment(self, repo, number, body):
        return self._write(repo, f'issues/{number}/comments', 'POST', {'body': body})

    def close(self, repo, number):
        return _issue(self._write(repo, f'issues/{number}', 'PATCH', {'state': 'closed', 'state_reason': 'not_planned'}),
                      repo, number)
