"""P15d: an undecided call is visible in the Cockpit's approvals queue.

The blueprint's acceptance bar for this slice is one sentence -- 一次 call 提案
进入审批 -- and this is where that is either true or not.  The card carries the
three decisions ``decide_conviction_call`` accepts, because it always could:
the operation is human-governance only and has taken accept, reject and defer
since P15d.  The card that offered no button and said approval was "等待正式
研究审批流程接入" was describing an integration gap that had already closed,
and it left the owner holding a proposal they could not clear.
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
        # All three verdicts the authority accepts, and a box to say why.
        self.assertEqual([action["decision"] for action in found["actions"]],
                         ["accept", "reject", "defer"])
        self.assertEqual([action["label"] for action in found["actions"]],
                         ["采纳", "驳回", "暂缓"])
        self.assertTrue(found["needs_rationale"])
        self.assertNotIn("审批状态", found["details"])

    def test_a_low_information_call_still_dismisses_in_one_click(self):
        # D3's one-click dismissal survives the three buttons: a call that says
        # "avoid", changes nothing and grades its own confidence low is the
        # machine reporting it has nothing to say, and asking for a written
        # rationale to decline to act on nothing is how a page teaches somebody
        # to stop reading it.  The predicate is patched rather than provoked --
        # see the note in the report about what it reads today.
        from unittest.mock import patch

        from dalton_core.initial_screen_reopen_hygiene import LOW_INFORMATION_NOTE

        self.propose(direction="avoid", confidence="low", decision="NO_CHANGE")
        with patch("dalton_core.cockpit_plane.low_information_call",
                   return_value=True):
            found = self.item()
        self.assertEqual([action["decision"] for action in found["actions"]],
                         ["accept", "reject", "defer"])
        self.assertEqual(found["actions"][1]["label"], "驳回这条提案")
        # Declining to act on nothing costs one click and no writing.
        self.assertFalse(found["needs_rationale"])
        self.assertEqual(found["note"], LOW_INFORMATION_NOTE)

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
