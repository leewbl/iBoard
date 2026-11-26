import unittest

from workbench import stack_verify, verify_pr


def member(number, head_branch, base_branch, head, base, proof, *, state='OPEN'):
    return {'number':number,'state':state, 'head_branch':head_branch, 'base_branch':base_branch,
            'head':head, 'base':base, 'cross_repository':False,
            'proof':{'status':proof,'reason':'missing-evidence'} if proof != 'verified' else {'status':proof}}


class StackEvidenceTests(unittest.TestCase):
    def test_verified_tip_waits_behind_unverified_parent(self):
        rows = [member(1,'root','main','a','base','verified'), member(2,'mid','root','b','a','unproven'),
                member(3,'tip','mid','c','b','verified')]
        self.assertEqual(stack_verify._prefix(rows, 'main'), ([1], {'pr':2,'reason':'missing-evidence'}))
        rows[1]['proof'] = {'status':'verified'}
        self.assertEqual(stack_verify._prefix(rows, 'main'), ([1,2,3], None))
        rows[1]['base'] = 'old'
        self.assertEqual(stack_verify._prefix(rows, 'main'), ([1], {'pr':2,'reason':'parent-revision-unavailable-or-changed'}))

    def test_pr_merged_into_parent_is_not_credited_as_landed_on_trunk(self):
        rows = [member(1,'root','main','a','base','already-merged',state='MERGED'),
                member(2,'mid','root','b','a','verified',state='MERGED')]
        self.assertEqual(stack_verify._prefix(rows, 'main'), ([], {'pr':2,'reason':'merged-into-parent-rather-than-default-branch'}))

    def test_duplicate_or_missing_acceptance_contract_is_rejected(self):
        for value in ([], [{'id':'one','text':'image verifies'},{'id':'one','text':'different'}]):
            with self.subTest(value=value), self.assertRaises(verify_pr.VerifyError):
                verify_pr._criteria(value)
