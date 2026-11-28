import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zlib

from model.claude_glm import _trace
from workbench import blind_eval, cli, visual_pixels


def _sample_png(red, metadata):
    return (visual_pixels._SIGNATURE
            + visual_pixels._chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + visual_pixels._chunk(b'tEXt', b'Author\0' + metadata)
            + visual_pixels._chunk(b'IDAT', zlib.compress(bytes([0, red, 40, 70])))
            + visual_pixels._chunk(b'IEND', b''))


class BlindEvalTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'), ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'guide.md').write_text('Please keep names compact.\n')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'name.mjs').write_text('export const label = "long";\n')
        before = self.commit()
        (self.repo / 'guide.md').write_text('Please keep names compact and lowercase.\n')
        after = self.commit()
        (self.repo / 'private.mjs').write_text('console.log("READY");\n')
        driver = self.commit()
        self.spec = {
            'schema': 1, 'question': 'Does project guidance affect this edit?',
            'request': 'Read guide.md and update the label in src/name.mjs.',
            'files': ['guide.md', 'src/name.mjs'], 'editable': ['src/name.mjs'],
            'arms': [{'name': 'plain-context', 'revision': before, 'model': 'glm-5.3', 'observe': ['guide.md']},
                     {'name': 'rich-context', 'revision': after, 'model': 'glm-5.3', 'observe': ['guide.md']}],
            'criteria': [{'id': 'behavior', 'basis': 'runtime', 'description': 'Reports the requested name.'},
                         {'id': 'use', 'basis': 'reading', 'description': 'Reads the project conventions.'},
                         {'id': 'shape', 'basis': 'artifact', 'description': 'Keeps the function concise.'}],
            'judge': 'gpt-first', 'auditor': 'gpt-second', 'seconds': 90,
            'check': {'revision': driver, 'file': 'private.mjs', 'image': 'sha256:' + 'b' * 64, 'marker': 'READY'},
        }
        self.barrier = threading.Barrier(2)
        self.witnessed_inputs = []
        self.witnessed_reviews = []
        self.witnessed_images = []
        self.diverge = False

    def commit(self):
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Checkpoint')
        return cli.sha(self.repo, 'HEAD')

    def freeze(self):
        path = self.root / 'manifest.json'
        cli.write(path, self.spec)
        return blind_eval.freeze(path, self.repo, self.root / 'approved.json')

    def edit(self, work, prompt, seconds, **kwargs):
        self.barrier.wait(5)
        self.witnessed_inputs.append((work, prompt))
        self.assertTrue(kwargs['private_workspace'])
        self.assertNotIn('eval', str(work))
        self.assertEqual(sorted(p.name for p in work.iterdir()), ['guide.md', 'src'])
        guide = (work / 'guide.md').read_text()
        value = 'compact' if 'lowercase' in guide else 'LONG'
        (work / 'src' / 'name.mjs').write_text(f'export const label = "{value}";\n')
        return {'exit_code': 0, 'model': kwargs['requested_model'], 'trace_completed': True,
                'trace': [{'type': 'tool_use', 'id': 'read-1', 'tool': 'Read', 'path': 'guide.md'},
                          {'type': 'tool_result', 'id': 'read-1', 'error': value == 'LONG'}],
                'stdout': 'Both guides were read, according to this self-report.'}

    def run_sandbox(self, work, image, command, seconds, **kwargs):
        self.assertNotIn('private.mjs', {p.name for p in work.iterdir()})
        self.assertIn(b'console.log("READY")', kwargs['regression_source'])
        self.assertEqual(kwargs['memory_mb'], 1024 if self.spec['check'].get('screenshot') else 512)
        passed = 'compact' in (work / 'src' / 'name.mjs').read_text()
        result = {'workspace': str(work), 'image': image, 'command': command,
                  'workspace_read_only': kwargs['workspace_read_only'],
                  'container_created': True, 'cleanup_confirmed': True,
                  'regression_sha256': blind_eval._sha(kwargs['regression_source']),
                  'exit_code': 0 if passed else 1, 'stdout': 'READY\n' if passed else ''}
        if path := kwargs.get('screenshot_output'):
            self.assertTrue(kwargs['capture_failure_screenshot'])
            red = (20 if passed else 80) + int(self.diverge and 'replay' in path.name)
            raw = _sample_png(red, b'glm-5.3 ' + path.name.encode())
            path.write_bytes(raw)
            result['screenshot_sha256'] = blind_eval._sha(raw)
        return result

    def review(self, prompt, model, seconds, *, images=()):
        self.witnessed_reviews.append(prompt)
        for forbidden in ('glm-5.3', 'gpt-worker', 'plain-context', 'rich-context', str(self.repo),
                          'private.mjs', 'self-report', 'codex'):
            self.assertNotIn(forbidden, prompt)
        spec = json.loads(prompt.removeprefix(blind_eval.JUDGE).removeprefix(blind_eval.VISUAL_JUDGE))
        if images:
            self.assertEqual([path.name for path in images], ['attachment-1.png', 'attachment-2.png'])
            captured = [path.read_bytes() for path in images]
            self.assertTrue(all(b'glm-5.3' not in raw and b'tEXt' not in raw for raw in captured))
            self.assertNotEqual(*captured)
            self.witnessed_images.append(captured)
        rows = []
        for label, evidence in spec['options'].items():
            file, body = next(iter(evidence['artifact'].items()))
            rows.append({'label': label, 'scores': {
                'behavior': {'why': 'Observed runtime', 'score': 4 if evidence['runtime'] else 0},
                'use': {'why': 'Observed file read', 'score': 4 if evidence['reading'] else 0},
                'shape': {'why': 'Implementation uses an export', 'score': 3, 'file': file, 'quote': body.strip()},
            }})
            if images:
                number = evidence['visual']['attachment']
                self.assertEqual(evidence['visual']['sha256'], blind_eval._sha(captured[number - 1]))
                rows[-1]['scores']['appearance'] = {'attachment': number, 'score': 3, 'why': 'Visible contrast'}
        return {'rows': rows}

    def enable_visual(self):
        self.spec['check']['screenshot'] = True
        self.spec['criteria'].append({'id': 'appearance', 'basis': 'visual', 'description': 'The label is legible.'})

    def run_pilot(self, approval, **kwargs):
        return blind_eval.run(**approval, out=self.root / 'proof', editor=kwargs.get('edit', self.edit),
                              codex_editor=kwargs.get('codex_editor', self.edit_codex),
                              reviewer=kwargs.get('review', self.review), sandbox=self.run_sandbox)

    def edit_codex(self, work, prompt, seconds, *, requested_model, files, editable):
        self.assertEqual(files, self.spec['files'])
        self.assertEqual(editable, self.spec['editable'])
        reply = self.edit(work, prompt, seconds, private_workspace=True, requested_model=requested_model)
        reply['host'] = 'codex'
        reply['trace'].extend((
            {'type': 'tool_use', 'id': 'write-1', 'tool': 'Write', 'path': 'src/name.mjs'},
            {'type': 'tool_result', 'id': 'write-1', 'error': False},
        ))
        return reply

    def test_both_blind_reviews_use_same_material_and_audit_refuses_tampering(self):
        approval = self.freeze()
        result = self.run_pilot(approval)
        self.assertEqual(result['status'], 'recommended', result)
        self.assertEqual(result['promotion'], 'rich-context')
        self.assertEqual([text for _, text in self.witnessed_inputs], [self.spec['request']] * 2)
        self.assertEqual(len({workspace for workspace, _ in self.witnessed_inputs}), 2)
        self.assertTrue(all(not workspace.exists() for workspace, _ in self.witnessed_inputs))
        self.assertEqual(len(self.witnessed_reviews), 2)
        self.assertEqual(*self.witnessed_reviews)
        self.assertEqual(blind_eval.audit(**approval, report=result['report'],
                                         report_sha256=result['sha256'])['promotion'], 'rich-context')
        lane = self.root / 'proof' / result['lanes']['rich-context']['file']
        lane.write_bytes(lane.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            blind_eval.audit(**approval, report=result['report'], report_sha256=result['sha256'])

    def test_freeze_rejects_meta_instructions_and_embedded_model_identity(self):
        request = self.spec['request']
        self.spec['request'] = request + ' This is an evaluation.'
        with self.assertRaisesRegex(blind_eval.EvalError, 'organic'):
            self.freeze()
        self.spec['request'] = request + ' Use the plain-context approach.'
        with self.assertRaisesRegex(blind_eval.EvalError, 'identity leaked'):
            self.freeze()

    def test_visual_reviews_use_the_same_anonymous_pixels_and_audit_refuses_tampering(self):
        self.enable_visual()
        approval = self.freeze()
        result = self.run_pilot(approval)
        self.assertEqual(result['status'], 'recommended', result)
        self.assertEqual(*self.witnessed_images)
        self.assertEqual(*self.witnessed_reviews)
        self.assertEqual(blind_eval.audit(**approval, report=result['report'],
                                         report_sha256=result['sha256'])['promotion'], 'rich-context')
        replay = self.root / 'proof' / 'capture-1-replay.png'
        replay.write_bytes(replay.read_bytes() + b'again')
        with self.assertRaisesRegex(ValueError, 'screenshot digest changed'):
            blind_eval.audit(**approval, report=result['report'], report_sha256=result['sha256'])

    def test_different_replay_pixels_block_before_review(self):
        self.enable_visual()
        self.diverge = True
        result = self.run_pilot(self.freeze())
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('screenshot pixels disagreed', result['error'])
        self.assertEqual(self.witnessed_reviews, [])

    def test_visual_review_must_reference_the_option_attachment(self):
        self.enable_visual()

        def review(prompt, model, seconds, **kwargs):
            result = self.review(prompt, model, seconds, **kwargs)
            score = result['rows'][0]['scores']['appearance']
            score['attachment'] = 3 - score['attachment']
            return result

        result = self.run_pilot(self.freeze(), review=review)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('different screenshot', result['error'])

    def test_visual_criterion_and_screenshot_capture_must_be_declared_together(self):
        self.enable_visual()
        del self.spec['check']['screenshot']
        with self.assertRaisesRegex(blind_eval.EvalError, 'visual criteria require screenshot capture'):
            self.freeze()
        self.spec['check']['screenshot'] = True
        self.spec['criteria'].pop()
        with self.assertRaisesRegex(blind_eval.EvalError, 'capture requires a visual criterion'):
            self.freeze()

    def test_mixed_hosts_share_anonymous_review_packet_and_audit(self):
        self.spec['arms'][1].update(host='codex', model='gpt-worker')
        approval = self.freeze()
        result = self.run_pilot(approval)
        self.assertEqual(result['status'], 'recommended', result)
        self.assertEqual(*self.witnessed_reviews)
        lane = cli.read(self.root / 'proof' / result['lanes']['rich-context']['file'])
        self.assertEqual(lane['host'], 'codex')
        self.assertEqual(lane['model'], 'gpt-worker')
        self.assertEqual(blind_eval.audit(**approval, report=result['report'],
                                         report_sha256=result['sha256'])['promotion'], 'rich-context')

    def test_codex_candidate_with_unobserved_write_is_blocked(self):
        self.spec['arms'][1].update(host='codex', model='gpt-worker')

        def editor(*args, **kwargs):
            reply = self.edit_codex(*args, **kwargs)
            return {**reply, 'trace': reply['trace'][:2]}

        result = self.run_pilot(self.freeze(), codex_editor=editor)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('missing successful file receipts', result['failures']['rich-context'])
        self.assertEqual(self.witnessed_reviews, [])

    def test_model_route_must_match_host_and_be_distinct_from_reviewers(self):
        self.spec['arms'][1].update(host='codex')
        with self.assertRaisesRegex(blind_eval.EvalError, 'compatible host and model'):
            self.freeze()
        self.spec['arms'][1]['model'] = self.spec['auditor']
        with self.assertRaisesRegex(blind_eval.EvalError, 'outside the candidate routes'):
            self.freeze()

    def test_refuses_metadata_in_generated_code_before_any_review(self):
        def edit(work, *args, **kwargs):
            result = self.edit(work, *args, **kwargs)
            path = work / 'src' / 'name.mjs'
            path.write_text(path.read_text() + '// implemented using glm-5.3\n')
            return result

        result = self.run_pilot(self.freeze(), edit=edit)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('provenance', result['error'])
        self.assertEqual(self.witnessed_reviews, [])

    def test_review_disagreement_does_not_recommend_a_variant(self):
        def edit(work, *args, **kwargs):
            result = self.edit(work, *args, **kwargs)
            (work / 'src' / 'name.mjs').write_text('export const label = "compact";\n')
            result['trace'][-1]['error'] = False
            return result

        def review(prompt, model, seconds):
            result = self.review(prompt, model, seconds)
            preferred = 'Option 1' if model == self.spec['judge'] else 'Option 2'
            for row in result['rows']:
                if row['label'] == preferred:
                    row['scores']['shape']['score'] = 4
            return result

        approval = self.freeze()
        result = self.run_pilot(approval, edit=edit, review=review)
        self.assertEqual(result['status'], 'disagreement')
        self.assertIsNone(result['promotion'])
        decision = blind_eval.audit(**approval, report=result['report'], report_sha256=result['sha256'])
        self.assertEqual(decision['status'], 'disagreement')

    def test_trace_is_based_on_matching_tool_results_and_contains_no_input_for_unrelated_paths(self):
        stdout = '\n'.join(json.dumps(event) for event in [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'a', 'name': 'Read', 'input': {'file_path': str(self.repo / 'guide.md')}},
                {'type': 'tool_use', 'id': 'b', 'name': 'Read', 'input': {'file_path': str(self.root / 'secret.md')}},
                {'type': 'tool_use', 'id': 'c', 'name': 'Read', 'input': {'file_path': str(self.repo / 'src/name.mjs')}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'a'},
                {'type': 'tool_result', 'tool_use_id': 'b'},
                {'type': 'tool_result', 'tool_use_id': 'c', 'is_error': True}]}},
        ])
        captured = _trace(stdout, self.repo)['trace']
        self.assertEqual(blind_eval._reads(captured, self.spec['files']), ['guide.md'])
        self.assertNotIn('secret.md', json.dumps(captured))


if __name__ == '__main__':
    unittest.main()
