import hashlib
import unittest

from workbench import prompt_profiles, pstack_agents, pstack_help
from workbench.pstack_principles import PRINCIPLES


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class PstackGuidanceTests(unittest.TestCase):
    def test_all_pinned_principles_reach_a_real_stage_with_individual_provenance(self):
        catalog = pstack_help._load_catalog()
        expected = {entry['source'] for entry in catalog['entries']
                    if entry['source'].startswith('skills/principle-')}
        self.assertEqual(len(expected), 24)
        self.assertEqual(set(PRINCIPLES), expected)
        for source, (phases, text) in PRINCIPLES.items():
            with self.subTest(source=source):
                for phase in phases:
                    guidance, receipt = prompt_profiles.guidance(phase, 'bug')
                    self.assertIn(text, guidance)
                    self.assertIn({'sources': [source], 'sha256': digest(text)}, receipt['sections'])
                    self.assertEqual(receipt['guidance_sha256'], digest(guidance))
                lookup = pstack_help._show(source, catalog)
                if source == 'skills/principle-attack-the-premise/SKILL.md':
                    self.assertEqual(lookup['status'], 'native-entry')
                    self.assertEqual([route['scenario'] for route in lookup['native_routes']],
                                     ['diagnose-repeated-failure'])
                    self.assertIn('premise-diagnosis/*', lookup['guidance_stages'])
                else:
                    self.assertEqual(lookup['status'], 'guidance-only')
                    self.assertEqual(lookup['native_routes'], [])
                self.assertTrue(set(phases) <= {stage.split('/')[0] for stage in lookup['guidance_stages']})

    def test_roles_apply_only_to_their_execution_paths(self):
        catalog = pstack_help._load_catalog()
        expected = {row['source'] for row in catalog['entries'] if row['role'] == 'agent-prompt'}
        _, ordinary = prompt_profiles.guidance('implementation', 'feature')
        _, delegated = prompt_profiles.guidance('implementation', 'feature', delegated=True)
        _, benny = prompt_profiles.guidance('repair-2', 'bug', benny=True)
        actual = (set(delegated['sources']) | set(benny['sources'])) & expected
        self.assertFalse(expected & set(ordinary['sources']))
        self.assertEqual(actual, {pstack_agents.POTETO_AGENT, pstack_agents.WORKER, pstack_agents.BENNY_OPERATOR})
        self.assertEqual(benny['phase'], 'repair')
        for role in ('benny-triage', 'comment-review', 'swarm-investigation'):
            text, receipt = prompt_profiles.specialist(role)
            self.assertEqual(receipt['guidance_sha256'], digest(text))
            actual.update(receipt['sources'])
            for source in receipt['sources']:
                self.assertIn(role + '/*', pstack_help._show(source, catalog)['guidance_stages'])
        self.assertEqual(actual, expected)
