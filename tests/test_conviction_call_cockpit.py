"""P15d: an undecided call is visible in the Cockpit's approvals queue.

The blueprint's acceptance bar for this slice is one sentence -- 一次 call 提案
进入审批 -- and this is where that is either true or not.  The card is
deliberately buttonless: the writer operation that records a decision exists
and is human-governance only, but the Cockpit's own decide path and the three
buttons are integration work, and INT1's rule is that a button which errors
when pressed is worse than no button.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.conviction_call import ConvictionCallAuthority
from tests.test_cockpit_plane import CockpitHarness
from tests.test_conviction_call import ACN, proposal_kwargs


class ConvictionCallApprovalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name))
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        self.authority = ConvictionCallAuthority(self.store)

    def propose(self, **overrides):
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        return self.authority.propose(**proposal_kwargs(
            company_ref=ACN, mission=mission, **overrides))

    def item(self):
        return next(
            (row for row in self.c.plane.approvals()["items"]
             if row["kind"] == "conviction_call"), None)

    def test_a_core_with_no_calls_shows_no_call_cards(self):
        self.assertIsNone(self.item())

    def test_an_undecided_call_appears_in_plain_words(self):
        call = self.propose()
        found = self.item()
        self.assertIsNotNone(found)
        self.assertEqual(found["ref"], call["id"])
        self.assertEqual(found["hash"], call["content_hash"])
        self.assertEqual(found["title"], "是否采纳这条投资 call：做多")
        self.assertEqual(found["who"], "ACN · Accenture")
        # The summary is the one sentence the whole object exists for.
        self.assertEqual(found["summary"], "the lag, not the direction")
        self.assertEqual(found["details"]["时间跨度"], "6–12 个月")
        self.assertEqual(found["details"]["风险回报是否达标"],
                         "达到手册的风险回报标准")
        self.assertTrue(found["details"]["我们的看法"])
        self.assertTrue(found["details"]["市场的看法"])
        self.assertEqual(found["details"]["可观察信号"],
                         ["book-to-bill above 1.1 on the call"])
        # No buttons: nothing on this card can be pressed into an error.
        self.assertEqual(found["actions"], [])
        self.assertFalse(found["needs_rationale"])

    def test_a_decided_call_leaves_the_queue(self):
        call = self.propose()
        self.assertIsNotNone(self.item())
        self.authority.decide(
            proposal_ref=call["id"], proposal_hash=call["content_hash"],
            decision="reject", reason="the lag is already in the price",
            actor_ref="human:owner")
        self.assertIsNone(self.item())


if __name__ == "__main__":
    unittest.main()
