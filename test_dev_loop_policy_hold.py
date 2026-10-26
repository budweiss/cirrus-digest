"""S497 P9: policy holds become Cowork-session tickets, never build-queue work."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dev_loop

# The real 10-23 alopecia-brief diagnosis shape (journal tail Skywarden quotes).
POLICY = ("Unit: alopecia-brief.service\n\nalopecia_brief.py exited 1: "
          "llm_providers.ProviderError: capability selection unavailable or requirements unmet; "
          "ProviderError: foundation route requires review or deferral. Restart failed again.")
CODE = ("Unit: cirrus-hoaleads.service\n\nhoa_leads.py exited 1: Traceback ... "
        "KeyError: 'parcel_id' at line 412. Restart failed again; deterministic.")


class PolicyHold(unittest.TestCase):
    def create(self, detail):
        with tempfile.TemporaryDirectory() as tmp, patch.object(dev_loop, '_notify_ticket') as notify:
            t = dev_loop.ticket_create('skywarden', ['cumulus'], 'unit has a diagnosed operational failure',
                                       detail, origin='skywarden', project_dir=tmp)
            saved = json.loads(dev_loop._ticket_path(Path(tmp)).read_text().splitlines()[-1])
            return t, saved, notify

    def test_policy_hold_goes_to_a_session_not_the_build_queue(self):
        t, saved, notify = self.create(POLICY)
        self.assertEqual((t['status'], saved['status']), ('session', 'session'))
        self.assertIn('policy hold, not a code defect', t['tier_reason'])
        notify.assert_called_once()                      # Cowork is told it needs a session

    def test_a_real_code_failure_still_queues_for_the_builder(self):
        t, _, notify = self.create(CODE)
        self.assertEqual(t['status'], 'queued'); notify.assert_not_called()

    def test_markers_are_exact_phrases(self):
        self.assertEqual(dev_loop.policy_hold('QUALIFICATION EXPIRED; research admission stopped'),
                         'qualification expired')
        self.assertIsNone(dev_loop.policy_hold('approval_id KeyError in approvals.py'))
        self.assertIsNone(dev_loop.policy_hold(''))


if __name__ == '__main__':
    unittest.main()
