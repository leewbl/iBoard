import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import pstack_help


class PstackHelpTests(unittest.TestCase):
    def lookup(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            pstack_help.main(args)
        return json.loads(output.getvalue())

    def test_named_scenarios_point_to_distinct_verification_lanes(self):
        expected = {
            'pr-status': 'pr_watch --mode check',
            'verify-pr': 'verify_pr',
            'review-pr': 'interrogate',
            'ship-stack': 'stack_ship plan',
            'inspect-task': 'pickup',
            'resume-task': 'resume',
            'generate-verify': 'project_verify create',
            'maintain-verify': 'project_verify maintain',
        }
        for name, command in expected.items():
            with self.subTest(name=name):
                result = self.lookup('scenario', name)
                self.assertEqual(result['command'], 'python3 -m workbench.' + command)
                self.assertEqual(result['status'], 'native-entry')
                self.assertTrue(result['expected_evidence'])
                self.assertIn('/blob/' + result['pstack_commit'] + '/pstack/', result['pstack_source'])

    def test_every_advertised_route_has_a_pinned_source_and_local_files(self):
        catalog = pstack_help._load_catalog()
        listed = self.lookup('list')
        self.assertEqual(listed['pstack_commit'], catalog['commit'])
        for name in listed['scenarios']:
            with self.subTest(name=name):
                route = pstack_help._scenario(name, catalog)
                self.assertEqual(route['status'], 'native-entry')
                self.assertTrue(route['command'])
                self.assertTrue(route['local_guide'])

    def test_catalog_only_and_prompt_guidance_have_no_runnable_route(self):
        for name, expected_status in (
            ('/pstack:create-formal-verification-skill', 'catalog-only'),
            ('autopilot-full', 'catalog-only'),
            ('setup-pstack', 'catalog-only'),
            ('principle-prove-it-works', 'guidance-only'),
        ):
            with self.subTest(name=name):
                result = self.lookup('show', name)
                self.assertEqual(result['status'], expected_status)
                self.assertEqual(result['native_routes'], [])
        self.assertIn('final-review/*', self.lookup('show', 'principle-prove-it-works')['guidance_stages'])

    def test_local_entry_is_withheld_when_implementation_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / 'skills' / 'babysit' / 'SKILL.md'
            guide.parent.mkdir(parents=True)
            guide.write_text('Stale local guide', encoding='utf-8')
            with patch.object(pstack_help, 'ROOT', root):
                result = self.lookup('scenario', 'pr-status')
        self.assertEqual(result['status'], 'missing-local-files')
        self.assertIsNone(result['command'])
        self.assertIsNone(result['local_guide'])

    def test_name_and_path_match_and_unknown_entries_give_recovery_hint(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as exited:
                self.lookup('show', 'not-an-available-skill')
        self.assertEqual(exited.exception.code, 2)
        self.assertIn('exact source path', error.getvalue())
        result = self.lookup('show', 'skills/poteto-mode/playbooks/hillclimb.md')
        self.assertEqual(result, self.lookup('show', 'hillclimb'))
        self.assertEqual(result['native_routes'][0]['scenario'], 'improve-performance')

    def test_catalog_version_mismatch_fails_without_rendering_a_route(self):
        with patch.object(pstack_help, 'PSTACK_COMMIT', 'different'):
            with self.assertRaisesRegex(ValueError, 'different pstack commits'):
                pstack_help._load_catalog()


if __name__ == '__main__':
    unittest.main()
