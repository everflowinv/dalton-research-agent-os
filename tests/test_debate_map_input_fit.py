"""WP-C1-3: fit the table, hold on an unchanged one, repair the shape once.

The live evidence, from
``~/Library/Application Support/Dalton/state/dalton-core/debate-map-runs/``
(2026-09-12 .. 09-16, 144 runs, 3 fresh maps):

* prompt size -- ``prompt_bytes`` min 11,134, median 29,346, max 31,206, against
  an antigravity provider input bound of 30,000; ``map_status`` was
  ``model_unavailable`` on 63 of the 144.
* wasted redraws -- 42 ``map_status: "duplicate"`` runs, e.g.
  ``23823e87119b5b5da85a696c`` (09-16T14:57Z, version 7, 30,748 bytes),
  ``04fefe1e79f6ee16d96ebd08`` (09-16T13:17Z, version 7, 30,748 bytes),
  ``4614e7443f1f9da1301fca34`` (09-16T13:08Z, version 7, 30,800 bytes),
  ``e3b241a0c12a5b1f1758450f`` (09-16T12:35Z, version 7, 30,800 bytes) -- the
  same subject, the same version, a drafting call and a verifying call each.
* broken shapes -- ``37c8749b40e77d1fa064e054``
  (``debates[1].bear.statement is longer than 600 characters``),
  ``5223298a8f60b81bb297b6d6`` (``debates[0].market.lean is not a lean``),
  ``84cd45b7358f3328f446ade1`` (``the drafter did not return one JSON object``).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.debate_map_draft import (
    MAX_STATEMENT_OUT_CHARS,
    TARGET_PROMPT_BYTES,
    build_input_table,
    build_prompt,
    draft_contract,
    draft_debate_map,
    drafted_from_identical_input,
    fit_input_table,
    parse_draft,
)
from dalton_core.draft_contract_repair import check_contract
from dalton_core.store import canonical_json, content_hash
from tests.test_debate_map_draft import (
    DRIVERS, METHOD, MISSION, NOW, PASS, SUBJECT, FakeModel, draft_reply,
)


def many_claims(count=200):
    rows = []
    for index in range(count):
        rows.append({
            "claim_version_ref": f"cv-{index:03d}",
            "subject_ref": SUBJECT,
            "index_aspect": "demand_drivers" if index % 2 else "supply_and_cost",
            "importance": "sell_side",
            "document_title": f"Broker {index}: a note about bookings and demand",
            "as_of": f"2026-0{1 + index % 8}-0{1 + index % 9}",
            "normalized_statement": (
                f"Row {index}: bookings, pricing and utilisation all moved this "
                "quarter and the brokers disagree about which of them leads the "
                "others into next year's revenue."),
        })
    return rows


def previous_map(cited):
    return {
        "id": "debate-map-version:prev",
        "version": 7,
        "debates": [{
            "debate_ref": "debate:prev-one",
            "question": "Does utilisation lead margin?",
            "status": "open",
            "driver_refs": ["driver:bookings"],
            "first_seen_at": NOW,
            "bull_position": {"statement": "yes", "claim_refs": list(cited)},
            "bear_position": {"statement": "no", "claim_refs": list(cited)},
        }],
        "drafted_by": {"kind": "model", "work_order_ref": "work:cockpit-debate_map-1",
                       "invocation_ref": "inv", "route_decision_ref": "route",
                       "model_family": "family-a"},
    }


class PromptFitTests(unittest.TestCase):
    BOUND = 12_000

    def fit(self, rows, previous=None, max_bytes=None):
        return fit_input_table(
            subject_ref=SUBJECT, subject_kind="company", claim_rows=rows,
            driver_rows=DRIVERS, thesis=None, method=METHOD, previous=previous,
            max_bytes=self.BOUND if max_bytes is None else max_bytes,
        )

    def test_the_widest_table_the_builder_can_make_is_under_the_target(self):
        # The live lane sent 29,346 bytes at the median against a 30,000-byte
        # provider bound.  Nothing the builder can produce may reach that now.
        table = build_input_table(
            subject_ref=SUBJECT, subject_kind="company", claim_rows=many_claims(),
            driver_rows=DRIVERS, thesis=None, method=METHOD,
            previous=previous_map([f"cv-{n:03d}" for n in range(12)]))
        self.assertLessEqual(
            len(build_prompt(table).encode("utf-8")), TARGET_PROMPT_BYTES)
        self.assertLess(TARGET_PROMPT_BYTES, 30_000)

    def test_a_table_over_the_bound_really_does_overshoot_before_fitting(self):
        table = build_input_table(
            subject_ref=SUBJECT, subject_kind="company", claim_rows=many_claims(),
            driver_rows=DRIVERS, thesis=None, method=METHOD, previous=None)
        self.assertGreater(len(build_prompt(table).encode("utf-8")), self.BOUND)

    def test_a_large_subject_is_fitted_under_the_target(self):
        table, report = self.fit(many_claims())
        self.assertTrue(report["fits"])
        self.assertLessEqual(
            len(build_prompt(table).encode("utf-8")), self.BOUND)
        self.assertEqual(report["prompt_bytes"],
                         len(build_prompt(table).encode("utf-8")))
        self.assertLess(report["claim_rows_shown"], report["claim_rows_available"])

    def test_a_small_subject_is_not_trimmed_at_all(self):
        table, report = self.fit(many_claims(4), max_bytes=TARGET_PROMPT_BYTES)
        self.assertTrue(report["fits"])
        self.assertEqual(report["claim_rows_shown"], 4)
        self.assertEqual(report["max_statement_chars"], 320)

    def test_the_rows_the_current_map_cites_are_never_dropped(self):
        # The refs a map already stands on. Dropping them is how a redraw
        # loses its own evidence and is then refused as a duplicate.
        cited = ["cv-190", "cv-191", "cv-192", "cv-193"]
        table, report = self.fit(many_claims(), previous=previous_map(cited))
        shown = {row["claim_version_ref"] for row in table["claims"]}
        self.assertTrue(set(cited) <= shown)
        self.assertEqual(report["pinned_refs_kept"], len(cited))
        self.assertLessEqual(
            len(build_prompt(table).encode("utf-8")), self.BOUND)

    def test_a_pinned_row_keeps_its_place_in_contested_order(self):
        # Pinning decides what survives, not what is shown first: the top of a
        # bounded table is where a debate is, and reordering it would move the
        # argument to the bottom.
        table, _ = self.fit(many_claims(), previous=previous_map(["cv-190"]))
        ids = [row["row_id"] for row in table["claims"]]
        self.assertEqual(ids, [f"C{index + 1}" for index in range(len(ids))])
        citable = table["citable"]
        for row in table["claims"]:
            self.assertEqual(citable[row["row_id"]], row["claim_version_ref"])

    def test_a_table_that_cannot_be_fitted_says_so_rather_than_lying(self):
        table, report = fit_input_table(
            subject_ref=SUBJECT, subject_kind="company", claim_rows=many_claims(),
            driver_rows=DRIVERS, thesis=None, method=METHOD, previous=None,
            max_bytes=10)
        self.assertFalse(report["fits"])
        self.assertGreater(report["prompt_bytes"], 10)


class HeldOnUnchangedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scheduler = Path(self.temp.name) / "scheduler.sqlite"
        connection = sqlite3.connect(self.scheduler)
        connection.execute(
            "CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,"
            "work_order_json TEXT,work_order_hash TEXT)")
        connection.commit()
        self.connection = connection
        self.addCleanup(connection.close)

    def record(self, work_ref, question):
        work = {"id": work_ref, "question": question}
        self.connection.execute(
            "INSERT INTO scheduler_work_orders VALUES(?,?,?)",
            (work_ref, canonical_json(work), content_hash(work)))
        self.connection.commit()

    def test_an_identical_prompt_is_recognised(self):
        previous = previous_map(["cv-1"])
        prompt = "THE TABLE AS IT WAS SHOWN"
        self.record(previous["drafted_by"]["work_order_ref"], prompt)
        self.assertTrue(drafted_from_identical_input(
            self.scheduler, previous, prompt))

    def test_one_extra_row_is_not_an_identical_prompt(self):
        previous = previous_map(["cv-1"])
        self.record(previous["drafted_by"]["work_order_ref"], "THE TABLE")
        self.assertFalse(drafted_from_identical_input(
            self.scheduler, previous, "THE TABLE\nC61\tone more row"))

    def test_it_fails_open_on_everything_it_cannot_read(self):
        # Not knowing is a reason to draft, never a reason to stay silent.
        previous = previous_map(["cv-1"])
        self.assertFalse(drafted_from_identical_input(
            self.scheduler, previous, "anything"))          # no work order row
        self.assertFalse(drafted_from_identical_input(
            Path(self.temp.name) / "missing.sqlite", previous, "anything"))
        self.assertFalse(drafted_from_identical_input(self.scheduler, None, "x"))
        self.assertFalse(drafted_from_identical_input(
            self.scheduler, {"drafted_by": None}, "x"))
        self.assertFalse(drafted_from_identical_input(None, previous, "x"))


class DraftContractTests(unittest.TestCase):
    def contract(self):
        return draft_contract(build_input_table(
            subject_ref=SUBJECT, subject_kind="company",
            claim_rows=many_claims(4), driver_rows=DRIVERS, thesis=None,
            method=METHOD, previous=previous_map(["cv-000"])))

    def test_an_overlong_bear_statement_is_named_with_its_count(self):
        reply = json.loads(draft_reply(
            bear={"statement": "x" * 900, "claim_refs": ["C1"]}))
        found = check_contract(reply, self.contract())
        self.assertEqual([item.rule for item in found], ["max_chars"])
        self.assertIn("debates[0].bear.statement", found[0].path)
        self.assertIn("900 characters", found[0].detail)
        self.assertIn(str(MAX_STATEMENT_OUT_CHARS), found[0].detail)

    def test_a_lean_that_is_not_a_lean_names_the_three_leans(self):
        reply = json.loads(draft_reply(
            market={"available": True, "lean": "bullish",
                    "statement": "s", "refs": ["C1"]}))
        found = [item for item in check_contract(reply, self.contract())
                 if item.rule == "enum"]
        self.assertTrue(found)

    def test_a_debate_ref_from_nowhere_names_both_allowed_sets(self):
        # The live ``debates[N].debate_ref '...' is neither a previous debate
        # nor a new-<n> id`` refusal, made repairable.
        reply = json.loads(draft_reply(debate_ref="debate:something-i-made-up"))
        found = [item for item in check_contract(reply, self.contract())
                 if item.path.endswith("debate_ref")]
        self.assertEqual(len(found), 1)
        self.assertIn("debate:prev-one", found[0].detail)
        self.assertIn("new-1", found[0].detail)

    def test_the_previous_debate_ref_and_a_new_n_are_both_accepted(self):
        for ref in ("debate:prev-one", "new-1", "new-12"):
            reply = json.loads(draft_reply(debate_ref=ref))
            self.assertEqual(
                [item for item in check_contract(reply, self.contract())
                 if item.path.endswith("debate_ref")], [])

    def test_the_prompt_states_the_two_closed_sets_and_the_hard_bound(self):
        prompt = build_prompt(build_input_table(
            subject_ref=SUBJECT, subject_kind="company", claim_rows=many_claims(3),
            driver_rows=DRIVERS, thesis=None, method=METHOD, previous=None))
        self.assertIn("character for character", prompt)
        self.assertIn("new-1, new-2, new-3", prompt)
        self.assertIn("count them, spaces included", prompt)
        self.assertIn("refuses the whole reply", prompt)


class DraftRepairTests(unittest.TestCase):
    def run_draft(self, model, previous=None, **kwargs):
        table = build_input_table(
            subject_ref=SUBJECT, subject_kind="company",
            claim_rows=many_claims(6), driver_rows=DRIVERS, thesis=None,
            method=METHOD, previous=previous)
        return draft_debate_map(
            table=table, method=METHOD, model=model, mission=MISSION,
            created_at=NOW, previous=previous, family_of=model.family_of, **kwargs)

    def test_an_overlong_statement_is_repaired_once_and_then_verified(self):
        long_reply = draft_reply(
            bull={"statement": "x" * 900, "claim_refs": ["C1"]})
        # The repair stays on the drafting family; only the verifier is a
        # different one, which is what D2 requires and nothing more.
        model = FakeModel([long_reply, draft_reply(), PASS],
                          families=("family-a", "family-a", "family-b"))
        result = self.run_draft(model)
        self.assertEqual(result["status"], "verified")
        self.assertEqual([call["purpose"] for call in model.calls],
                         ["debate_map", "debate_map", "debate_map_verifier"])
        self.assertEqual(result["contract_repair"]["status"], "repaired")
        self.assertEqual(result["contract_repair"]["repair_attempts"], 1)
        self.assertIn("bull.statement", model.calls[1]["prompt"])

    def test_the_repair_prompt_carries_the_ids_and_not_the_whole_table(self):
        model = FakeModel([draft_reply(debate_ref="debate:invented"),
                           draft_reply(), PASS],
                          families=("family-a", "family-a", "family-b"))
        self.run_draft(model)
        repair = model.calls[1]["prompt"]
        self.assertIn("CITABLE ROW IDS", repair)
        self.assertIn("DRIVER REFS", repair)
        self.assertNotIn("QUESTION ADMISSION", repair)
        self.assertLess(len(repair.encode("utf-8")),
                        len(model.calls[0]["prompt"].encode("utf-8")))

    def test_a_reply_that_is_not_json_is_repaired_once_and_then_refused(self):
        model = FakeModel(["I think the debates are:", "still prose", PASS])
        result = self.run_draft(model)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("after one repair", result["reason"])

    def test_a_repair_that_will_not_fit_the_budget_is_never_made(self):
        model = FakeModel([draft_reply(debate_ref="debate:invented"), PASS])
        result = self.run_draft(model, budget_remaining_micros=1_200,
                                repair_reserve_micros=1_000)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(model.calls), 1)
        self.assertIn("run cost bound reached", result["reason"])

    def test_the_repair_is_paid_for_out_of_the_run(self):
        model = FakeModel([draft_reply(debate_ref="debate:invented"),
                           draft_reply(), PASS],
                          families=("family-a", "family-a", "family-b"))
        result = self.run_draft(model)
        self.assertEqual(result["cost_micros"], 3_000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RunHeldOnUnchangedInputTests(unittest.TestCase):
    """The child holds before it builds a model, not after it has paid.

    The 42 live duplicates each bought a drafting call and a verifying call to
    redraw a map from a table the current version had already been drawn from.
    """

    def setUp(self):
        from tests import test_debate_map_lane as lane

        self.lane = lane
        self.harness = lane.ChildHarness()
        self.addCleanup(self.harness.close)
        self.harness.add_claims()
        rows = lane.subject_claim_rows(self.harness.store, lane.ACN)
        from dalton_core.debate_map import DebateMapAuthority, evidence_fingerprint

        DebateMapAuthority(self.harness.store).publish_map(
            mission_version_ref=self.harness.mission["id"],
            mission_version_hash=self.harness.mission["content_hash"],
            subject_ref=lane.ACN, subject_kind="company",
            change_reason="evidence_thicker",
            change_evidence_refs=[rows[0]["claim_version_ref"]],
            constitution_ref="constitution-version:x:1", constitution_hash="a" * 64,
            evidence_fingerprint=evidence_fingerprint(
                row["claim_version_ref"] for row in rows),
            debates=[{
                "debate_ref": "debate:x", "question": "q?",
                "driver_refs": [lane.DRIVER],
                "admission_index": 0, "causal_link_index": 0,
                "bull_position": {"statement": "up",
                                  "claim_refs": [rows[0]["claim_version_ref"]]},
                "bear_position": {"statement": "down",
                                  "claim_refs": [rows[1]["claim_version_ref"]]},
                "market_position": {"available": False, "lean": None,
                                    "statement": None, "refs": []},
                "our_position": {"state": "none_yet", "side": None,
                                 "statement": None, "refs": []},
                "status": "candidate", "last_shift_reason": None,
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "source_independence": {"bull_sources": 1, "bear_sources": 1},
            }],
            drafted_by={"kind": "model", "work_order_ref": "work:cockpit-debate_map-1",
                        "invocation_ref": "inv", "route_decision_ref": "route-0",
                        "model_family": "family-a"},
            verified_by={"kind": "model", "work_order_ref": "work:v",
                         "invocation_ref": "inv-v", "route_decision_ref": "route-1",
                         "model_family": "family-b"},
            actor_ref=self.harness.mission["autonomy"]["automation_principal"],
            created_at="2026-09-09T00:00:00+00:00",
        )

    def test_it_holds_without_building_a_model(self):
        from unittest import mock

        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        with mock.patch("dalton_core.debate_map_cli.drafted_from_identical_input",
                        return_value=True) as recognised, \
                mock.patch("dalton_core.debate_map_cli.CockpitModel") as model:
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["map_status"], "input_unchanged")
        self.assertEqual(summary["cost_micros"], 0)
        self.assertIn("同一张图", summary["failure_reason"])
        self.assertIsNotNone(summary["version_ref"])
        model.assert_not_called()
        # It was asked about the prompt it was about to send, not about a
        # fingerprint standing in for one.
        prompt = recognised.call_args.args[2]
        self.assertIsInstance(prompt, str)
        self.assertIn("OUTPUT CONTRACT", prompt)
        self.assertIn(self.lane.ACN, prompt)

    def test_a_moved_table_is_drafted_rather_than_held(self):
        from unittest import mock

        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        with mock.patch("dalton_core.debate_map_cli.drafted_from_identical_input",
                        return_value=False), \
                mock.patch("dalton_core.debate_map_cli.CockpitModel") as model:
            model.side_effect = RuntimeError("the run went on to build a model")
            with self.assertRaisesRegex(RuntimeError, "went on to build a model"):
                self.harness.run(dry_run=False, model_config_path=config)

    def test_the_prompt_the_child_measures_is_the_one_it_compares(self):
        from unittest import mock

        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        with mock.patch("dalton_core.debate_map_cli.drafted_from_identical_input",
                        return_value=True) as recognised:
            summary = self.harness.run(dry_run=False, model_config_path=config)
        prompt = recognised.call_args.args[2]
        self.assertEqual(summary["prompt_bytes"], len(prompt.encode("utf-8")))
        self.assertLessEqual(summary["prompt_bytes"], TARGET_PROMPT_BYTES)
        self.assertTrue(summary["prompt_fit"]["fits"])
