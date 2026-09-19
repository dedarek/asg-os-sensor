import unittest
from runtime.hook_approval import summarize

class ApprovalTests(unittest.TestCase):
    def test_group_only_owned_callbacks(self):
        items=[{'displayCommand':'asg-run.sh '+str(i),'trustState':'pending_trust'} for i in range(7)]
        items.append({'displayCommand':'user-hook.sh','trustState':'pending_trust'})
        result=summarize({'items':items},'/workspace')
        self.assertEqual(result['status'],'approval_required')
        self.assertEqual(result['event_count'],7)

    def test_approved_and_partial(self):
        items=[{'displayCommand':'asg-run.sh','trustState':'trusted_persistent'}]
        self.assertEqual(summarize({'items':items},'/w')['status'],'approved')
        items.append({'displayCommand':'asg-run.sh','trustState':'pending_trust'})
        self.assertEqual(summarize({'items':items},'/w')['status'],'approval_required')

    def test_no_owned_definitions_is_not_success(self):
        self.assertEqual(summarize({'items':[]},'/w')['status'],'unknown')
