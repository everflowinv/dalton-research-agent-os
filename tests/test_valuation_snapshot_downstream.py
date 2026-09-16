"""P11c-E: the two readers that could not use a snapshot until there was one.

Both were written against a valuation layer that did not exist yet, and both
guessed its shape wrong in the same direction -- they assumed it would hold a
*target price*. It does not and, under the frozen formula, never will: P11c
computes four trailing multiples and no forward anything.

So this file pins two things down.

The Initial Screen's 估值 section is now drafted from the snapshot, with each
multiple tagged ``N`` like every other figure and citing the snapshot cell
rather than a Claim -- the third computed layer ``mission_deliverable``'s cell
vocabulary was built to admit. The number discipline is unchanged where it
matters: a figure the snapshot does not hold is still refused at publish time.

The consensus bridge's ``target_price`` leg now says what the snapshot
actually holds instead of "the valuation snapshot holds no target price", and
names the priced anchor without bridging it. Putting a market capitalisation
on the ``ours`` side of a comparison with the street's target would
manufacture a variant view out of the fact that the shares have a price.
"""

from __future__ import annotations

import unittest

from dalton_core import consensus_bridge as cb
from dalton_core.deep_insight_gate_cli import valuation_rows
from dalton_core.initial_screen import (
    SECTION_GUIDANCE,
    VALUATION_GAP,
    build_claim_context,
    parse_section_output,
    valuation_section_held,
)
from dalton_core.mission_deliverable import (
    CELL_SOURCE_KINDS,
    MissionDeliverableAuthority,
    MissionDeliverableConflict,
    MissionDeliverableValidationError,
    validate_cell_citation,
    validate_section,
)
from tests.p14a_fixtures import ACN
from tests.test_valuation_snapshot_lane import ValuationLaneHarness

VALUATION_TITLE = "S6 估值（street 预期、框架、event pathway、IRR）"


class GuidanceTests(unittest.TestCase):
    def test_the_valuation_section_has_guidance_now(self) -> None:
        guidance = SECTION_GUIDANCE[6]
        self.assertTrue(guidance.strip())
        # The three things this section gets wrong if nobody says otherwise.
        self.assertIn("目标价", guidance)
        self.assertIn("共识一致等于没有观点", guidance)
        self.assertIn("N 标签", guidance)

    def test_the_gap_text_names_the_reason_that_is_actually_true(self) -> None:
        self.assertIn("估值快照", VALUATION_GAP)
        self.assertIn("valuation_snapshot_versions", VALUATION_GAP)
        # The old text blamed six absent authorities. Five of them are held on
        # this Core now, so saying they are missing would be a false statement
        # about our own ledger.
        for stale in ("汇率", "利率", "估值投影"):
            self.assertNotIn(stale, VALUATION_GAP)

    def test_the_section_is_held_only_when_there_is_nothing_to_write_it_from(self) -> None:
        guidance = SECTION_GUIDANCE[6]
        self.assertTrue(valuation_section_held(VALUATION_TITLE, guidance, []))
        self.assertFalse(
            valuation_section_held(VALUATION_TITLE, guidance, [{"ref": "x"}]))
        # An empty guidance still holds it, which is what every other section
        # with no guidance relies on.
        self.assertTrue(valuation_section_held(VALUATION_TITLE, "", [{"ref": "x"}]))
        # And this is not a rule about valuation, it is a rule about one title.
        self.assertFalse(valuation_section_held("S2 公司概览", "写公司概览", []))


class ScreenCitationTests(ValuationLaneHarness):
    """A published snapshot, all the way to what the deliverable authority accepts."""

    def setUp(self) -> None:
        super().setUp()
        self.publish_prices()
        self.ingest_filings()
        self.run_child()
        self.snapshot = self.snapshots.latest_version(ACN)
        self.rows = valuation_rows(self.store, ACN)
        self.context = build_claim_context([], valuation=self.rows)
        self.deliverables = MissionDeliverableAuthority(self.store)

    def section(self, body: str, tags: list[str]) -> dict:
        import json

        return parse_section_output(
            json.dumps({"body": body, "claims": [], "numbers": tags, "gaps": []}),
            context=self.context, title=VALUATION_TITLE,
        )

    def check(self, section: dict) -> dict:
        return validate_section(
            section, live_claim_refs=set(),
            resolve_cell=self.deliverables.cell_resolver())

    def test_the_multiples_arrive_as_n_tags_that_cite_the_snapshot(self) -> None:
        # Every metric in this fixture is available, so all four are citable.
        self.assertEqual(len(self.rows), 4)
        numbers = self.context["numbers"]
        self.assertEqual([item["tag"] for item in numbers],
                         ["N1", "N2", "N3", "N4"])
        for item in numbers:
            self.assertEqual(item["aspect"], "valuation")
            self.assertEqual(item["cell"]["kind"], "valuation_metric")
            self.assertEqual(item["cell"]["version_ref"], self.snapshot["id"])
            self.assertEqual(item["period"], self.snapshot["as_of"])
        # The figures the drafter may write are the ones on the snapshot:
        # N3 is EV/EBITDA, computed at 6,500 / 1,450 in the harness.
        self.assertIn("4.4828", numbers[2]["figures"])

    def test_a_cited_multiple_survives_the_deliverable_authority(self) -> None:
        checked = self.check(self.section("EV/EBITDA 为 4.4828 倍。", ["N3"]))
        entry = checked["numbers"][0]
        self.assertNotIn("claim_version_ref", entry)
        self.assertEqual(entry["cell"]["kind"], "valuation_metric")
        self.assertEqual(entry["cell"]["version_ref"], self.snapshot["id"])

    def test_a_figure_the_snapshot_does_not_hold_is_still_refused(self) -> None:
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.check(self.section("EV/EBITDA 为 99.9 倍。", ["N3"]))
        self.assertIn("99.9", str(caught.exception))

    def test_a_number_may_not_cite_a_claim_and_a_cell_at_once(self) -> None:
        section = self.section("EV/EBITDA 为 4.4828 倍。", ["N3"])
        section["numbers"][0]["claim_version_ref"] = "claim-version:1"
        with self.assertRaises(MissionDeliverableValidationError):
            self.check(section)

    def test_a_cell_whose_snapshot_this_core_does_not_hold_is_refused(self) -> None:
        section = self.section("EV/EBITDA 为 4.4828 倍。", ["N3"])
        section["numbers"][0]["cell"] = {
            "kind": "valuation_metric",
            "ref": "valuation-metric:valuation-snapshot-version:nope:trailing_pe",
            "version_ref": "valuation-snapshot-version:nope",
        }
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.check(section)
        self.assertIn("does not resolve", str(caught.exception))

    def test_a_metric_the_snapshot_published_as_unavailable_is_not_citable(self) -> None:
        resolve = self.deliverables.cell_resolver()
        self.assertTrue(resolve({
            "kind": "valuation_metric", "version_ref": self.snapshot["id"],
            "ref": f"valuation-metric:{self.snapshot['id']}:trailing_pe"}))
        # Every metric is available in this fixture, so unavailability is
        # exercised against a name the snapshot simply does not carry.
        self.assertFalse(resolve({
            "kind": "valuation_metric", "version_ref": self.snapshot["id"],
            "ref": f"valuation-metric:{self.snapshot['id']}:target_price"}))

    def test_the_cell_vocabulary_admits_the_third_computed_layer(self) -> None:
        self.assertIn("valuation_metric", CELL_SOURCE_KINDS)
        with self.assertRaises(MissionDeliverableValidationError):
            # ``accession`` belongs to the filing kind, not to this one.
            validate_cell_citation({"kind": "valuation_metric", "ref": "r",
                                    "accession": "0001467373-26-000001"})


class ScreenWithoutASnapshotTests(ValuationLaneHarness):
    def test_an_unpriced_company_still_falls_back_to_the_gap(self) -> None:
        rows = valuation_rows(self.store, ACN)
        self.assertEqual(rows, [])
        self.assertTrue(
            valuation_section_held(VALUATION_TITLE, SECTION_GUIDANCE[6], rows))
        context = build_claim_context([], valuation=rows)
        self.assertEqual(context["numbers"], [])


class BridgeTargetPriceTests(unittest.TestCase):
    """The leg that was unreachable, and the reason that was wrong."""

    SNAPSHOT = {
        "id": "valuation-snapshot-version:abc",
        "snapshot_ref": "valuation-snapshot:company:x",
        "formula_version": "valuation-formula:p11c:0.2",
        "as_of": "2026-09-15", "currency": "USD",
        "price": {"close": "193.4"},
        "basis": {"market_cap": "118349603880.6", "enterprise_value": None},
        "metrics": [
            {"metric": "trailing_pe", "status": "available", "value": "15.193"},
            {"metric": "ev_to_ebitda", "status": "unavailable", "value": None},
        ],
    }

    def test_a_real_snapshot_holds_no_forward_target(self) -> None:
        self.assertIsNone(cb._valuation_target(self.SNAPSHOT))

    def test_the_live_status_word_is_read_not_only_the_dead_one(self) -> None:
        # The old check demanded ``status == "computed"``, which no snapshot
        # has ever written, so this leg could not fire even for a snapshot
        # that did hold a target.
        held = dict(self.SNAPSHOT, metrics=[
            {"metric": "target_price", "status": "available",
             "value": "220", "unit": "USD"}])
        row = cb._valuation_target(held)
        self.assertEqual(row["value"], "220")
        self.assertEqual(row["ref"], "valuation-snapshot-version:abc")

    def test_the_ref_falls_back_to_the_chain_not_to_a_key_that_never_existed(self) -> None:
        held = {k: v for k, v in self.SNAPSHOT.items() if k != "id"}
        held["metrics"] = [{"metric": "target_price", "status": "available",
                            "value": "220", "unit": "USD"}]
        row = cb._valuation_target(held)
        self.assertEqual(row["ref"], "valuation-snapshot:company:x")
        self.assertNotIn("None", row["ref"])

    def test_the_reason_names_the_anchor_without_bridging_it(self) -> None:
        reason = cb._valuation_anchor(self.SNAPSHOT)
        self.assertIn("no forward target price", reason)
        self.assertIn("valuation-formula:p11c:0.2", reason)
        self.assertIn("193.4", reason)
        self.assertIn("118349603880.6", reason)
        self.assertIn("trailing_pe", reason)
        self.assertIn("valuation-snapshot-version:abc", reason)
        self.assertIn("not a target", reason)
        # Unavailable metrics are not offered as anchors, and an absent
        # enterprise value is not printed as the word None.
        self.assertNotIn("ev_to_ebitda", reason)
        self.assertNotIn("None", reason)

    def test_a_snapshot_with_nothing_priced_still_gives_one_sentence(self) -> None:
        reason = cb._valuation_anchor({"metrics": []})
        self.assertIn("no forward target price", reason)
        self.assertNotIn("what it does hold", reason)
