import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from test_benny import CHANNEL, Fixture, LINK, ROOT, RULES, TRIAGE, reply
from test_runner import tiny_png
from workbench import benny, benny_triage, cli, prompt_profiles


OBSERVED = 'Renaming one local item clears its label.'
EXPECTED = 'Renaming should preserve the new label.'
TRACKER = 'owner/product'


def issue(number, body, *, state='open', pull=False):
    data = {'number': number, 'title': 'Rename label outcome', 'body': body, 'state': state,
            'html_url': f'https://github.com/{TRACKER}/{"pull" if pull else "issues"}/{number}'}
    if pull:
        data['pull_request'] = {'merged_at': None}
    return data


def search_result(*items, incomplete=False):
    return {'items': list(items), 'total_count': len(items), 'incomplete_results': incomplete}


class BennyTriageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo/'labels.py').write_text('def rename_item(label):\n    return label\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Store the selected label')
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.__exit__)
        self.fixture.__enter__()
        self.fixture.root['text'] = OBSERVED + '\n' + EXPECTED
        self.reply = {'category': 'bug', 'title': 'Rename clears the label', 'summary': OBSERVED,
                      'facts': dict.fromkeys(benny_triage.FACTS), 'match': None, 'question': None,
                      'trace': {'basis': 'code-path', 'finding': 'The rename function returns its input.',
                                'citations': [{'id': 'S1', 'quote': 'return label'}]}}
        self.reply['facts'].update(observed={'id': 'R1', 'quote': OBSERVED},
                                   expected={'id': 'R1', 'quote': EXPECTED})
        self.model = Mock(side_effect=lambda *_a, **_k: {'exit_code': 0, 'model': 'glm-fixture',
                                                        'provider': 'zhipu', 'stdout': json.dumps({'status': 'ready', **self.reply})})
        self.tracker = Mock()
        self.tracker.search.return_value = search_result()

    def freeze(self, name):
        return benny.freeze(rules=RULES, slack=self.fixture.slack, channel=CHANNEL, message_ts=ROOT,
                            out=self.root/name)

    def triage(self, name, *, frozen=None, **overrides):
        frozen = frozen or self.freeze('source-' + name)
        args = dict(slack=self.fixture.slack, receipt=frozen['receipt'], sha256=frozen['sha256'], out=self.root/name,
                    repo=self.repo, revision='HEAD', selections=('labels.py:1-2',), tracker_repo=TRACKER,
                    terms=('rename label',), search_source=True, tracker=self.tracker, model=self.model)
        return benny_triage.triage(**(args | overrides))

    def test_cited_bug_can_propose_one_local_draft_after_tracker_check(self):
        result = self.triage('draft')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['proposal']['marker'], '[benny:bug]')
        rec = result['recommendation']
        self.assertEqual(rec['action'], 'draft-new-issue')
        self.assertEqual(rec['fields']['source'], LINK)
        self.assertEqual(rec['fields']['reporter_quote'], OBSERVED)
        self.assertEqual(rec['fields']['environment'], 'unknown')
        self.assertEqual(result['evidence_sha256'], cli.digest(self.root/'draft/evidence.json'))
        self.assertEqual(self.tracker.search.call_count, 2)
        self.assertTrue(self.model.call_args.kwargs['private_workspace'])
        guidance, receipt = prompt_profiles.specialist('benny-triage')
        self.assertTrue(self.model.call_args.args[1].startswith(guidance + '\n\n'))
        self.assertEqual(result['model']['workflow_guidance'], receipt)
        self.assertFalse(any(method == 'chat.postMessage' for _, method, _ in self.fixture.calls))

    def test_partial_search_or_unread_media_withholds_issue_draft(self):
        self.tracker.search.return_value = search_result(incomplete=True)
        self.assertEqual(self.triage('partial')['recommendation']['action'], 'tracker-inconclusive')
        self.tracker.search.return_value = search_result()
        self.assertEqual(self.triage('no-source', search_source=False)['recommendation']['action'], 'tracker-inconclusive')
        self.fixture.root['files'] = [{'id': 'F123ATTACH'}]
        result = self.triage('unread')
        self.assertEqual(result['recommendation']['action'], 'needs-evidence')
        self.assertEqual(result['proposal']['unread_media_count'], 1)

    def test_existing_source_link_preempts_model_and_duplicate_signal_is_exact(self):
        self.tracker.search.return_value = search_result(issue(8, 'Origin: ' + LINK))
        self.assertEqual(self.triage('linked')['status'], 'source-already-linked')
        self.model.assert_not_called()
        self.tracker.search.return_value = search_result(issue(8, OBSERVED))
        self.reply['match'] = {'id': 'T1', 'kind': 'confident-duplicate', 'report': {'id': 'R1', 'quote': OBSERVED},
                               'tracker': {'id': 'T1', 'quote': OBSERVED}}
        result = self.triage('matched')
        self.assertEqual(result['recommendation']['action'], 'confident-duplicate')
        self.assertTrue(result['proposal']['marker'].endswith('/issues/8'))
        self.tracker.search.return_value = search_result(issue(8, OBSERVED, state='closed'))
        self.assertEqual(self.triage('old')['recommendation']['action'], 'related-history')
        self.tracker.search.return_value = search_result(issue(8, 'Another label never appears.'))
        self.reply['match']['tracker']['quote'] = 'Another label never appears.'
        self.assertEqual(self.triage('false-match')['status'], 'blocked')

    def test_forged_source_quote_or_inferred_destination_is_rejected(self):
        self.reply['trace']['citations'][0]['quote'] = 'return invented'
        self.assertEqual(self.triage('wrong-quote')['status'], 'blocked')
        self.reply['trace']['citations'][0]['quote'] = 'return label'
        self.reply['category'] = 'reroute'
        self.assertEqual(self.triage('unknown-owner')['status'], 'blocked')
        mapping = self.root/'routing.json'
        mapping.write_text(json.dumps([{'path': 'labels.py', 'area': 'Labels', 'destination': 'Local label maintainers'}]))
        result = self.triage('known-owner', routing_map=mapping)
        self.assertEqual(result['proposal']['marker'], '[benny:other]')
        self.assertEqual(result['recommendation']['destination'], 'Local label maintainers')

    def test_existing_artifact_and_ambiguous_requirements_have_different_outcomes(self):
        self.tracker.search.return_value = search_result(issue(9, 'Keep the requested label after rename.', pull=True))
        self.reply['match'] = {'id': 'T1', 'kind': 'existing-fix', 'report': {'id': 'R1', 'quote': OBSERVED},
                               'tracker': {'id': 'T1', 'quote': 'Keep the requested label after rename.'}}
        self.assertEqual(self.triage('existing')['recommendation']['action'], 'existing-fix')
        self.reply.update(category='unclear', match=None, question='Does this happen on a second item too?')
        result = self.triage('question')
        self.assertEqual(result['proposal']['marker'], '[benny:other]')
        self.assertEqual(result['proposal']['question'], self.reply['question'])

    def test_prior_verdict_and_drift_cannot_emit_a_proposal(self):
        self.fixture.pages[0].append(reply(1, '[benny:other]', user=TRIAGE))
        self.assertEqual(self.triage('already-triaged')['status'], 'prior-verdict')
        self.model.assert_not_called()
        self.fixture.pages[0].pop()

        def model(*args, **kwargs):
            self.fixture.pages[0].append(reply(1, 'The steps now differ.'))
            return self.model(*args, **kwargs)

        result = self.triage('racing', model=model)
        self.assertEqual(result['status'], 'source-changed')
        self.assertIsNone(result['proposal'])

    def test_markdown_in_a_json_quote_is_literal_data(self):
        quote = 'A documented path\n```sh\nopenclaw example\n```'
        encoded = json.dumps({'status': 'ready', 'quote': quote})
        self.assertEqual(benny_triage._answer(f'```json\n{encoded}\n```'), {'quote': quote})
        with self.assertRaises(ValueError):
            benny_triage._answer(f'```json\n{encoded}\n```\n```json\n{encoded}\n```')
        self.reply['trace']['citations'][0]['quote'] = 'def rename_item(label):\n    return label'
        self.assertEqual(self.triage('multiline-source')['status'], 'ready')

    def test_invalid_format_can_repair_once_but_provider_failure_stops(self):
        accepted = {'exit_code': 0, 'stdout': json.dumps({'status': 'ready', **self.reply})}
        model = Mock(side_effect=[{'exit_code': 0, 'stdout': 'not JSON'}, accepted])
        result = self.triage('format-repair', model=model)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(result['model_attempts']), 2)
        self.assertIn('validation_error', result['model_attempts'][0])
        provider = Mock(return_value={'exit_code': 9, 'stdout': ''})
        result = self.triage('provider-failure', model=provider)
        self.assertEqual(result['status'], 'blocked')
        provider.assert_called_once()

    def test_retained_log_can_support_observed_behavior_but_picture_notes_stay_separate(self):
        self.fixture.root['files'] = [{'id': 'F123LOG'}, {'id': 'F123PHOTO'}]
        self.fixture.attach('F123LOG', (OBSERVED + '\n').encode(), 'text/plain')
        self.fixture.attach('F123PHOTO', tiny_png(), 'image/png')
        self.reply['facts']['observed'] = {'id': 'A1.1', 'quote': OBSERVED}
        self.reply['media'] = [{'id': 'V2', 'quote': 'One red pixel is visible.'}]
        result = self.triage('attachment', inspect_media=True, vision=lambda *_: 'One red pixel is visible.')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['recommendation']['action'], 'draft-new-issue')
        packet = cli.read(self.root/'attachment/evidence.json')
        self.assertEqual(packet['unread_media_count'], 0)
        self.assertEqual(packet['evidence']['V2']['scope'], 'screenshot')
        attached = result['recommendation']['attachment_refs'][1]
        self.assertEqual(attached['path'], 'attachments/F123PHOTO.png')
        self.assertEqual(attached['sha256'], cli.digest(self.root/'attachment'/attached['path']))
        self.reply['facts']['observed'] = dict(self.reply['media'][0])
        self.assertEqual(self.triage('invalid-media-fact', inspect_media=True,
                                    vision=lambda *_: 'One red pixel is visible.')['status'], 'blocked')

    def test_changed_attachment_invalidates_local_proposal(self):
        self.fixture.root['files'] = [{'id': 'F123LOG'}]
        self.fixture.attach('F123LOG', (OBSERVED + '\n').encode(), 'text/plain')

        def model(*args, **kwargs):
            (self.root/'tampered/attachments/F123LOG.txt').write_text('different evidence')
            return self.model(*args, **kwargs)

        result = self.triage('tampered', read_attachments=True, model=model)
        self.assertEqual(result['status'], 'source-changed')
        self.assertIsNone(result['proposal'])

    def test_vision_provider_failure_keeps_evidence_without_calling_triage_model(self):
        self.fixture.root['files'] = [{'id': 'F123PHOTO'}]
        self.fixture.attach('F123PHOTO', tiny_png(), 'image/png')
        result = self.triage('media-quota', inspect_media=True, vision=Mock(side_effect=RuntimeError('provider quota')))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('media model failed', result['reason'])
        self.assertTrue((self.root/'media-quota/attachments/F123PHOTO.png').is_file())
        self.model.assert_not_called()
