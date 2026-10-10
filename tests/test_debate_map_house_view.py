"""Batch D: a debate map's ``our_position`` comes from the current house view.

Live, all four ACN debates carried ``our_position: none_yet`` while a view was
held.  When a house view exists (the latest Initial Screen's contract stance,
or an admitted thesis), a none_yet reply is filled from it, deterministically.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.debate_map_cli import house_view
from dalton_core.debate_map_draft import (
    HOUSE_STANCE_SIDES,
    build_input_table,
    build_prompt,
    build_verifier_prompt,
    house_position,
    parse_draft,
)
from tests.test_debate_map_draft import DRIVERS, METHOD, SUBJECT, claim_rows, draft_reply

HOUSE = {"ref": "mission-deliverable-version:" + "a" * 32, "stance": "看多",
         "statement": "一致预期低估了服务业务的经营杠杆。", "as_of": "2026-08-15"}
THESIS = {"status": "current", "thesis_version_ref": "thesis-version:t1",
          "statement": "Managed services keep compounding.", "confidence": "medium",
          "driver_refs": ["driver:bookings"]}


def table(*, house=None, thesis=None):
    return build_input_table(
        subject_ref=SUBJECT, subject_kind="company", claim_rows=claim_rows(),
        driver_rows=DRIVERS, thesis=thesis, method=METHOD, previous=None,
        house_view=house,
    )


class HouseViewTests(unittest.TestCase):
    def test_none_yet_is_filled_from_the_house_stance(self):
        parsed = parse_draft(draft_reply(), table(house=HOUSE))
        ours = parsed[0]["ours"]
        self.assertEqual(ours["state"], "held")
        self.assertEqual(ours["side"], "bull")
        self.assertIn("看多", ours["statement"])
        self.assertEqual(ours["refs"], [HOUSE["ref"]])

    def test_the_stance_vocabulary_maps_to_sides(self):
        self.assertEqual(HOUSE_STANCE_SIDES,
                         {"看多": "bull", "看空": "bear", "中性": "neither", "观察": "neither"})
        bear = parse_draft(draft_reply(), table(house={**HOUSE, "stance": "看空"}))
        self.assertEqual(bear[0]["ours"]["side"], "bear")

    def test_a_thesis_alone_is_held_as_neither_with_the_thesis(self):
        parsed = parse_draft(draft_reply(), table(thesis=THESIS))
        ours = parsed[0]["ours"]
        self.assertEqual((ours["state"], ours["side"]), ("held", "neither"))
        self.assertEqual(ours["refs"], ["thesis-version:t1"])
        both = house_position(table(house=HOUSE, thesis=THESIS))
        self.assertEqual(both["side"], "bull")
        self.assertEqual(both["refs"], [HOUSE["ref"], "thesis-version:t1"])

    def test_a_held_reply_is_kept_as_written(self):
        reply = draft_reply(ours={"state": "held", "side": "bear",
                                  "statement": "On this question we lean bear.",
                                  "refs": ["H1"]})
        ours = parse_draft(reply, table(house=HOUSE))[0]["ours"]
        self.assertEqual((ours["side"], ours["refs"]), ("bear", [HOUSE["ref"]]))

    def test_without_a_house_view_nothing_changes(self):
        plain = table()
        self.assertNotIn("house_rows", plain)
        self.assertIsNone(house_position(plain))
        self.assertEqual(parse_draft(draft_reply(), plain)[0]["ours"]["state"], "none_yet")
        self.assertNotIn("OUR HOUSE VIEW", build_prompt(plain))
        # An unknown stance is not a house view.
        self.assertNotIn("house_rows", table(house={**HOUSE, "stance": "buy"}))

    def test_drafter_and_verifier_are_told(self):
        held = table(house=HOUSE)
        prompt = build_prompt(held)
        self.assertIn("OUR HOUSE VIEW", prompt)
        self.assertIn("never none_yet", prompt)
        debates = [{"debate_ref": "debate:x", "status": "open", "question": "q",
                    "driver_refs": ["driver:bookings"], "admission_index": 0,
                    "causal_link_index": 0,
                    "bull_position": {"statement": "b", "claim_refs": ["cv-1"]},
                    "bear_position": {"statement": "b", "claim_refs": ["cv-2"]},
                    "market_position": {"available": False},
                    "our_position": {"state": "held", "side": "bull", "statement": "s",
                                     "refs": [HOUSE["ref"]]},
                    "last_shift_reason": None}]
        self.assertIn("our_position is set from these", build_verifier_prompt(held, debates))


class HouseViewReaderTests(unittest.TestCase):
    def store(self, record):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE mission_deliverable_pointer(deliverable_ref, version_id)")
        connection.execute("CREATE TABLE mission_deliverable_versions(version_id, record_json)")
        if record is not None:
            connection.execute("INSERT INTO mission_deliverable_pointer VALUES(?,?)",
                               ("mission-deliverable:initial_screen:synw", record["id"]))
            connection.execute("INSERT INTO mission_deliverable_versions VALUES(?,?)",
                               (record["id"], json.dumps(record, ensure_ascii=False)))

        class Store:
            pass

        store = Store()
        store.connection = connection
        self.addCleanup(connection.close)
        return store

    def test_reads_the_latest_screens_contract_stance(self):
        record = {"id": "mission-deliverable-version:x", "created_at": "2026-08-15T00:00:00",
                  "summary": "s", "contract": {
                      "stance": "看多", "watch_price": {"status": "given", "value": "85.0"},
                      "worksheet": {"core_judgement": "一致预期低估了经营杠杆。"}}}
        view = house_view(self.store(record), "company:ticker:synw")
        self.assertEqual(view["stance"], "看多")
        self.assertEqual(view["ref"], "mission-deliverable-version:x")
        self.assertIn("85.0", view["statement"])

    def test_a_screen_without_a_contract_stance_is_no_house_view(self):
        self.assertIsNone(house_view(self.store({"id": "v", "summary": "s"}),
                                     "company:ticker:synw"))
        self.assertIsNone(house_view(self.store(None), "company:ticker:synw"))


if __name__ == "__main__":
    unittest.main()
