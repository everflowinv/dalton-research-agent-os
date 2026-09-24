"""Retired Claims leave every downstream product, and published ones reopen.

Live 2026-09-24: ACN's dossier v20 cited 28 Claims retired within twenty
minutes of it, and ``event-judgement:ddd002385401`` was generated at 11:40 on a
Claim retired at 10:49.  Every reader goes through
``claim_retirement.retired_claim_version_refs`` (retired less reinstated); this
file covers what sits downstream of it: the debate map's filter, the canonical
stand-in, judgement selection, the dossier's reopen and the display notice.
"""

from __future__ import annotations

import json
import re
import sqlite3
import types
import unittest
from unittest.mock import patch

from dalton_core.company_dossier import CompanyDossierAuthority, retired_withdrawals
from dalton_core.company_dossier_cli import stale_units
from dalton_core.company_research_view import query_company_research
from tests.test_claim_index_entries import ACN
from tests.test_dossier_lane import FakeModel, Harness

A = "claim-version:" + "a" * 64
B = "claim-version:" + "b" * 64
C = "claim-version:" + "c" * 64


def bare_core() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE claim_versions (claim_version_id TEXT PRIMARY KEY);
        CREATE TABLE claim_retirement_decisions (
            decision_id TEXT PRIMARY KEY, claim_version_ref TEXT, challenge_ref TEXT,
            decision TEXT, created_at TEXT);
    """)
    for ref in (A, B, C):
        connection.execute("INSERT INTO claim_versions VALUES (?)", (ref,))
    connection.execute("INSERT INTO claim_retirement_decisions VALUES "
                       "('d1', ?, 'c1', 'retired', '2026-09-24T10:49:43+00:00')", (A,))
    connection.execute("INSERT INTO claim_retirement_decisions VALUES "
                       "('d2', ?, 'c2', 'kept', '2026-09-24T10:50:00+00:00')", (B,))
    return connection


class CockpitJudgementMarkingTests(unittest.TestCase):
    def test_a_judgement_citing_a_retired_claim_is_marked_not_rewritten(self):
        from dalton_core.cockpit_plane import CockpitPlane

        core = bare_core()
        self.addCleanup(core.close)
        core.execute("CREATE TABLE event_judgements (judgement_id TEXT, company_ref TEXT, "
                     "record_json TEXT, created_at TEXT)")
        record = {"event_ref": "research-event:1", "event_kind": "claim",
                  "decision": "NO_CHANGE", "action": "no_change", "because": "维持",
                  "citations": ["research-event:1", A, B], "effect": {}}
        core.execute("INSERT INTO event_judgements VALUES (?,?,?,?)",
                     ("event-judgement:ddd0", ACN, json.dumps(record), "2026-09-24T11:40:46"))
        plane = types.SimpleNamespace(_rows=CockpitPlane._rows)
        item = CockpitPlane._judgements(plane, core)["by_ref"]["event-judgement:ddd0"]
        self.assertEqual(item["retired_citations"], [A])
        self.assertIn("1 条结论已退役", item["retired_citation_label"])
        self.assertEqual(item["citations"], record["citations"])

    def test_the_notice_names_the_count_and_is_absent_for_zero(self):
        from dalton_core.cockpit_research_library import retired_citation_notice

        self.assertIsNone(retired_citation_notice(0))
        self.assertIn("3 条结论已退役", retired_citation_notice(3))


class LedgerHarnessCase(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def retire(self, ref):
        from dalton_core.claim_retirement import ClaimRetirementAuthority

        row = self.harness.store.connection.execute(
            "SELECT content_hash FROM claim_versions WHERE claim_version_id=?", (ref,),
        ).fetchone()
        authority = ClaimRetirementAuthority(self.harness.store)
        challenge = authority.challenge(
            claim_version_ref=ref, claim_version_hash=row["content_hash"],
            reason_code="human_judgment", rationale="fixture",
            actor_ref="human:coverage-owner")
        authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref="human:coverage-owner", rationale="fixture")


class CanonicalStandInTests(LedgerHarnessCase):
    def test_a_retired_canonical_copy_does_not_take_its_live_duplicate_with_it(self):
        first = self.harness.tag("dup-1", "business_model", group="dup|revenue",
                                 statement="咨询收入占一半。")
        second = self.harness.tag("dup-2", "business_model", group="dup|revenue",
                                  statement="咨询收入约占一半。")
        refs = lambda: [row["claim_version_ref"] for row in query_company_research(  # noqa: E731
            self.harness.store, company_ref=ACN, index_aspect="business_model",
            exclude_retired=True)]
        pair = {first["claim_version_id"], second["claim_version_id"]}
        shown = pair & set(refs())
        self.assertEqual(len(shown), 1)
        canonical = shown.pop()
        duplicate = (pair - {canonical}).pop()
        self.retire(canonical)
        after = refs()
        self.assertNotIn(canonical, after)
        # Without the stand-in the fact vanished: the canonical copy was retired
        # and ``canonical_only`` dropped every live duplicate.
        self.assertIn(duplicate, after)

    def test_debate_map_evidence_excludes_retired_claims(self):
        from dalton_core.debate_map_draft import subject_claim_refs, subject_claim_rows

        claim = self.harness.tag("debate-1", "demand_drivers", statement="订单转正。")
        self.assertIn(claim["claim_version_id"], subject_claim_refs(self.harness.store, ACN))
        self.retire(claim["claim_version_id"])
        self.assertNotIn(claim["claim_version_id"],
                         subject_claim_refs(self.harness.store, ACN))
        self.assertNotIn(claim["claim_version_id"],
                         [row["claim_version_ref"] for row in
                          subject_claim_rows(self.harness.store, ACN)])

    def test_a_retirement_moves_the_debate_map_change_key(self):
        from dalton_core.mission_debate_map_lane import subject_change_keys

        claim = self.harness.tag("debate-2", "demand_drivers", statement="订单转正。")
        before = subject_change_keys(self.harness.store.connection, [ACN])[ACN]
        self.retire(claim["claim_version_id"])
        after = subject_change_keys(self.harness.store.connection, [ACN])[ACN]
        self.assertNotEqual(before, after)


class CitingEveryTag(FakeModel):
    """A drafter that cites every Claim it was shown, not just the first."""

    def call(self, *, purpose, request_id, prompt, mission):
        reply = super().call(purpose=purpose, request_id=request_id, prompt=prompt,
                             mission=mission)
        if prompt.startswith("You are an independent verifier"):
            return reply
        tags = re.findall(r"^(C\d+)\t", prompt, flags=re.MULTILINE)
        if not tags:
            return reply
        payload = json.loads(reply["text"])
        for slot in payload["slots"]:
            for sentence in slot.get("sentences") or []:
                sentence["refs"] = tags
        return {**reply, "text": json.dumps(payload, ensure_ascii=False)}


class DossierReopenTests(LedgerHarnessCase):
    def setUp(self):
        super().setUp()
        self.harness.tag("d-1b", "business_model",
                         statement="咨询合同的续约率高于外包合同。")
        self.authority = CompanyDossierAuthority(self.harness.store)
        summary = self.harness.run(max_units=12, model_factory=lambda: CitingEveryTag())
        self.assertIn(summary["dossier_status"], ("published", "partial_published"))
        self.first = self.authority.latest(ACN)
        section = self.section(self.first, "business_model")
        self.cited = sorted(row["ref"] for row in section["sources"])
        self.assertEqual(len(self.cited), 2)

    @staticmethod
    def section(record, aspect):
        return next(item for item in record["sections"] if item["aspect"] == aspect)

    def test_nothing_new_and_nothing_retired_stays_idle(self):
        summary = self.harness.run(max_units=12, model_factory=lambda: CitingEveryTag())
        self.assertEqual(summary["dossier_status"], "nothing_new")

    def test_a_unit_citing_a_retired_claim_is_redrafted_with_nothing_else_new(self):
        retired = self.cited[0]
        self.retire(retired)
        planned = self.harness.run(dry_run=True)
        self.assertIn("business_model", planned["units_drafted"])
        # A different sentence: the Constitution's output rubric refuses a
        # unit that restates its prior body word for word.
        drafter = CitingEveryTag(sentence="续约率更高的咨询合同支撑了这一判断。")
        summary = self.harness.run(max_units=12, model_factory=lambda: drafter)
        self.assertIn(summary["dossier_status"], ("published", "partial_published"),
                      summary.get("failure_reason"))
        self.assertIn(retired, summary["retired_withdrawals"])
        second = self.authority.latest(ACN)
        self.assertEqual(second["version"], self.first["version"] + 1)
        refs = [row["ref"] for row in self.section(second, "business_model")["sources"]]
        self.assertNotIn(retired, refs)
        self.assertIn(self.cited[1], refs)
        # The redraft was not shown the retired sentence as something to keep.
        business_prompts = [p for p in drafter.prompts if "Part: business_model" in p]
        self.assertTrue(business_prompts)
        # The one prior sentence cited the retired Claim, so it is left out.
        self.assertNotIn("这一节的判断由所引材料支撑。", business_prompts[0])
        # And the old version still says what it said.
        self.assertIn(retired, [row["ref"] for row in
                                self.section(self.first, "business_model")["sources"]])
        # Corrected: the next tick has nothing to do.
        again = self.harness.run(max_units=12, model_factory=lambda: CitingEveryTag())
        self.assertEqual(again["dossier_status"], "nothing_new")

    def test_withdrawals_count_only_claims_retired_now(self):
        connection = self.harness.store.connection
        dropped = {"sections": []}
        self.assertEqual(retired_withdrawals(connection, dropped, self.first), [])
        self.retire(self.cited[0])
        self.assertEqual([row["ref"] for row in
                          retired_withdrawals(connection, dropped, self.first)],
                         [self.cited[0]])
        # A reinstated Claim is live again: dropping it is not a correction.
        with patch("dalton_core.claim_retirement.reinstated_claim_version_refs",
                   return_value={self.cited[0]}):
            self.assertEqual(retired_withdrawals(connection, dropped, self.first), [])

    def test_corrections_go_first_but_stay_inside_the_unit_bound(self):
        plan = {
            unit: {"unit": unit, "status": "ready", "stale": stale, "new_refs": new,
                   "retired_refs": retired, "last_drafted": "2026-09-01"}
            for unit, stale, new, retired in (
                ("business_model", False, 0, [A]),
                ("segments_and_mix", True, 5, []),
                ("demand_drivers", False, 0, [B]),
                ("supply_and_cost", False, 0, []),
            )
        }
        self.assertEqual(stale_units(plan, limit=2), ["business_model", "demand_drivers"])
        self.assertEqual(stale_units(plan, limit=3),
                         ["business_model", "demand_drivers", "segments_and_mix"])

    def test_the_library_marks_a_published_dossier_resting_on_a_retired_claim(self):
        from dalton_core.cockpit_research_library import research_library

        connection = self.harness.store.connection
        mission = self.harness.mission
        before = research_library(connection, mission, ACN)
        dossier = next(p for p in before["products"] if p["kind"] == "dossier")
        self.assertNotIn("retired_citation_count", dossier)
        self.retire(self.cited[0])
        after = research_library(connection, mission, ACN)
        dossier = next(p for p in after["products"] if p["kind"] == "dossier")
        self.assertGreaterEqual(dossier["retired_citation_count"], 1)
        self.assertTrue(any("已退役" in gap for gap in dossier["display_gaps"]))
        marked = [section for section in dossier["sections"]
                  if section.get("retired_sources")]
        self.assertTrue(marked)
        self.assertTrue(all(self.cited[0] in section["retired_sources"] for section in marked))
        # The unlocalised projection the publication worker hashes is untouched.
        raw = research_library(connection, mission, ACN, localize=False)
        raw_dossier = next(p for p in raw["products"] if p["kind"] == "dossier")
        self.assertNotIn("retired_citation_count", raw_dossier)


class EventJudgementSelectionTests(unittest.TestCase):
    def test_an_event_whose_claim_was_retired_is_not_judged(self):
        from dalton_core.event_judgement import EventJudgementAuthority
        from dalton_core.event_judgement_cli import unjudged_event_groups
        from dalton_core.research_event import ResearchEventAuthority, record_event
        from tests.p14a_fixtures import AUTOMATION, P14aHarness

        class Case(P14aHarness):
            def runTest(self):  # pragma: no cover - driven below
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        events = ResearchEventAuthority(case.store)
        judgements = EventJudgementAuthority(case.store)
        refs = {}
        for name, occurred in (("live", "2026-09-24T09:00:00+00:00"),
                               ("dead", "2026-09-24T09:30:00+00:00")):
            ref = "claim-version:" + (name * 32)[:64]
            refs[name] = ref
            record_event(
                events, company_ref=ACN, kind="claim", occurred_at=occurred,
                source_refs=[ref],
                payload={"claim_ref": f"claim:{name}", "claim_version_ref": ref,
                         "metric_ref": None, "period": "recent quarter",
                         "source_ref": None, "statement": f"{name} statement"},
                mission=case.mission, actor_ref=AUTOMATION)
        with patch("dalton_core.claim_retirement.retired_claim_version_refs",
                   return_value={refs["dead"]}):
            groups = unjudged_event_groups(events, judgements, company_ref=ACN, limit=10)
        selected = [event["source_refs"][0] for group in groups for event in group]
        self.assertEqual(selected, [refs["live"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
