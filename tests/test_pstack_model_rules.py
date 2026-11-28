import unittest
from workbench import pstack_model_rules as rules

HEADER = '---\ndescription: pstack per-role model choices (overrides skill defaults)\nalwaysApply: true\n---\n'


class ModelRulesTests(unittest.TestCase):
    def parse(self, body):
        return rules.parse((HEADER + body).encode())

    def test_v1_migrates_known_values_and_reports_retired(self):
        result = self.parse('# budget: unlimited\nfeature, refactoring: grok-old\njudgment and prose: grok-custom\n'
                            'arena runners: grok-old, claude-opus-old\n'
                            'interrogate reviewers: auto, grok-old, gemini-4-other\n'
                            'hardest tasks: claude-old\n')
        self.assertEqual(result['defaults_version'], 1)
        self.assertEqual(result['budget'], 'large')
        self.assertEqual(result['roles']['feature, refactoring'], rules.DEFAULTS['feature, refactoring'])
        self.assertEqual(result['roles']['judgment and prose'], 'grok-custom')
        self.assertEqual(result['roles']['arena runners'], rules.DEFAULTS['arena runners'])
        self.assertEqual(result['roles']['interrogate reviewers'], ['auto', 'claude-opus-5-5-high-fast', 'gemini-4-neon-xhigh'])
        self.assertTrue(any(row['role'] == 'hardest tasks' and row['after'] is None for row in result['changes']))
        self.assertTrue(any(row['role'] == 'bug-fix' and row['before'] is None for row in result['changes']))

    def test_v2_resets_old_cross_panel_only_and_keeps_custom(self):
        result = self.parse('# defaults: 2\n# budget: medium (high)\n'
                            'arena cross-judge pool: gemini-old, claude-fable-old, claude-opus-old\n'
                            'bug-fix: grok-custom\n')
        self.assertEqual(result['roles']['arena cross-judge pool'], rules.DEFAULTS['arena cross-judge pool'])
        self.assertEqual(result['roles']['bug-fix'], 'grok-custom')
        self.assertEqual(result['budget'], 'medium')

    def test_current_panel_preserves_order_duplicates_and_aliases(self):
        result = self.parse('# defaults: 3\n# budget: large (xhigh)\n'
                            'arena runners: auto, gemini-4-neon-high, auto, gemini-4-neon-medium\n')
        self.assertEqual(result['roles']['arena runners'],
                         ['auto', 'gemini-4-neon-xhigh', 'auto', 'gemini-4-neon-medium'])

    def test_rejects_duplicate_malformed_and_unsupported(self):
        invalid = ['# defaults: 4\n', '# defaults: 2\n# defaults: 3\n', '# budget: large (medium)\n',
                   '# budget: unlimited\n# defaults: 3\n', 'bug-fix: x\nbug-fix: y\n',
                   'bug-fix: x, y\n', 'arena runners: x,\n', 'evil$: x\n',
                   '# budget: large\n# budget: small\n']
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)
        with self.assertRaises(ValueError):
            rules.parse(b'---\nalwaysApply: true\nalwaysApply: true\n---\n')
        with self.assertRaises(ValueError):
            rules.parse(b'\xff')


if __name__ == '__main__':
    unittest.main()
