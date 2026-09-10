"""P14d: a passed gate is the state of a version, not of the company (ADR-0008)."""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.deliverable_reopen import (
    CHANGE_REASON_EVIDENCE,
    CHECKPOINT_KIND,
    DEFAULT_POLICY,
    REOPEN_ITEMS,
    VERDICTS,
    DeliverableReopenConflict,
    DeliverableReopenNotFound,
    DeliverableReopenValidationError,
    GateReopenAuthority,
    approved_reopen,
    consumed_reopen_refs,
    evidence_items,
    load_policy,
    passed_version,
    reopen_assessment,
)
from dalton_core.initial_screen_cli import _target, reopen_revision
from dalton_core.mission_reopen_lane import passed_companies
from dalton_core.mission_stage import evaluate_mission
from dalton_core.mission_deliverable import (
    MissionDeliverableAuthority,
    MissionDeliverableValidationError,
    validate_revision,
)
from dalton_core.store import content_hash
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, OWNER, P14aHarness

SECTION_TITLES = ("一、公司在做什么", "二、行业位置", "三、最近发生了什么",
                  "四、核心 thesis", "五、风险与 anti-thesis",
                  "六、relevance to universe", "七、估值", "八、下一步")


class ReopenHarness(P14aHarness):
    """A Core with one published, passed Initial Screen for ACN."""

    grants = ("deliverable", "stage_record", "observation", "market_event")

    def setUp(self):
        super().setUp()
        self.deliverables = MissionDeliverableAuthority(self.store)
        self.reopens = GateReopenAuthority(self.store)
        self.claim_refs = [
            self.claim(subject=ACN, statement=f"事实 {n}",
                       created_at="2026-09-01T00:00:00+00:00")
            for n in range(30)
        ]

    def sections(self, claim_refs=None):
        refs = list(claim_refs or self.claim_refs[:3])
        return [
            {"title": title,
             "body": f"{title} 的正文，写得够长以便通过结构自评。" * 8,
             "claim_refs": refs, "numbers": [], "gaps": []}
            for title in SECTION_TITLES
        ]

    def publish(self, *, revision=None, sections=None, summary="摘要。"):
        return self.deliverables.publish(
            kind="initial_screen", subject_ref=ACN, mission=self.mission,
            playbook=self.playbook,
            template_ref="playbook:deliverable_templates.initial_screen",
            sections=sections or self.sections(), summary=summary,
            gaps=["估值一节按数字纪律留空"], model_invocation_refs=[],
            actor_ref=AUTOMATION, revision=revision,
        )

    def pass_gate(self, version):
        self.enter_screen(ACN)
        return self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref="initial_screen", status="gate_passed",
            evidence_refs=[version["id"], self.mission["id"]],
            rationale="P10c 出口门自评：四问全部为是，文档非空壳，数字零无源",
            actor_ref=AUTOMATION,
            idempotency_key=f"{self.mission['id']}:{ACN}:initial_screen:{version['id']}",
        )

    def thicken(self, *, lines=120):
        """Ingest one filing's worth of statement lines, after the screen passed.

        Through the real authority: the tables carry a ``dalton_authorized``
        trigger, and a fixture that wrote around it would be testing an
        arrangement the live Core cannot reach.
        """

        self._filed = getattr(self, "_filed", 0) + 1
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref=AUTOMATION,
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(
            authorization=authorization, attempt=self._filed - 1)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], f"sec-financials-run:{self._filed:024d}")
        rows = [{
            "statement": "income", "concept": f"us-gaap:Concept{n}",
            "label": f"Concept {n}", "level": 0, "parent_concept": None,
            "is_breakdown": False, "dimension_axis": None, "dimension_member": None,
            "period_start": "2026-03-01", "period_end": "2026-05-31",
            "value": str(1000 + n), "unit": "USD", "balance": "credit",
        } for n in range(lines)]
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": f"0001467373-26-0000{self._filed:02d}", "form": "10-Q",
                    "filed": "2026-06-25", "report_date": "2026-05-31", "lines": rows,
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")


class PolicyTests(unittest.TestCase):
    def test_the_defaults_are_the_five_items_the_owner_named(self):
        self.assertEqual(REOPEN_ITEMS, ("statements", "verified_figures", "market_data",
                                        "consensus", "claims_per_section"))
        self.assertEqual(load_policy(None), dict(DEFAULT_POLICY))

    def test_a_policy_file_with_an_unknown_key_is_a_typo_not_a_default(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps({"min_statement_lines": 1}), encoding="utf-8")
            self.assertEqual(load_policy(path)["min_statement_lines"], 1)
            path.write_text(json.dumps({"min_statements": 1}), encoding="utf-8")
            with self.assertRaisesRegex(DeliverableReopenValidationError, "unknown keys"):
                load_policy(path)
            path.write_text(json.dumps({"min_statement_lines": -1}), encoding="utf-8")
            with self.assertRaisesRegex(DeliverableReopenValidationError, "non-negative"):
                load_policy(path)


class AssessmentTests(ReopenHarness):
    def test_a_company_that_never_passed_is_not_an_error(self):
        assessment = reopen_assessment(self.store.connection, company_ref=CTSH)
        self.assertEqual(assessment["status"], "not_passed")
        self.assertIn("过闸", assessment["reason"])

    def test_the_baseline_is_the_evidence_base_as_of_the_passed_version(self):
        version = self.publish()
        self.pass_gate(version)
        before = evidence_items(
            self.store.connection, company_ref=ACN, section_count=8,
            as_of=version["created_at"],
        )
        self.thicken(lines=250)
        after = evidence_items(
            self.store.connection, company_ref=ACN, section_count=8, as_of=None,
        )
        was = {item["item_ref"]: item for item in before}
        now = {item["item_ref"]: item for item in after}
        self.assertEqual((was["statements"]["value"], was["statements"]["mark"]), (0, "缺"))
        self.assertEqual((now["statements"]["value"], now["statements"]["mark"]), (250, "有"))
        # The Claims were there before the screen and are still there: not a flip.
        self.assertEqual(was["claims_per_section"]["mark"], "有")
        self.assertEqual(now["claims_per_section"]["mark"], "有")

    def test_an_item_that_flips_proposes_and_one_that_does_not_exist_says_so(self):
        version = self.publish()
        self.pass_gate(version)
        quiet = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(quiet["status"], "no_flip")
        self.assertEqual(quiet["flipped"], [])

        self.thicken(lines=250)
        assessment = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(assessment["status"], "reopen_proposed")
        self.assertEqual(assessment["flipped"], ["statements"])
        self.assertEqual(assessment["change_reason"], CHANGE_REASON_EVIDENCE)
        self.assertEqual(assessment["passed_version_ref"], version["id"])
        self.assertTrue(assessment["evidence_refs"])
        consensus = next(e for e in assessment["diff"] if e["item_ref"] == "consensus")
        self.assertFalse(consensus["flipped"])
        self.assertIn("P11b", consensus["note"])

    def test_the_diff_is_measured_from_the_version_the_gate_cited_not_the_newest(self):
        first = self.publish()
        self.pass_gate(first)
        self.thicken(lines=250)
        # A later version exists; the baseline must stay the one that passed.
        later = self.publish(sections=self.sections(self.claim_refs[3:6]))
        self.assertEqual(later["prior_version_ref"], first["id"])
        found = passed_version(self.store.connection, company_ref=ACN)
        self.assertEqual(found["version_id"], first["id"])
        self.assertEqual(reopen_assessment(self.store.connection,
                                           company_ref=ACN)["flipped"], ["statements"])

    def test_a_gate_that_passed_under_an_earlier_mission_version_is_still_passed(self):
        # P14-S. The live mission rolled v7 -> v13 in two days. Read off the
        # active version, the four Initial Screens that passed under v13 come
        # back as never-screened under v14, and the selection rule -- which
        # skips a company whose gate has passed -- re-drafts all four.
        version = self.publish()
        self.pass_gate(version)
        first = self.mission["id"]
        self.grant("forecast_line")
        self.assertNotEqual(self.mission["id"], first)
        self.assertEqual(self.missions.stage_records(self.mission["id"]), [],
                         "the new version carries no stage records of its own")

        stage_state = self.missions.stage_state_by_company(self.mission_ref)
        self.assertEqual(stage_state[ACN]["initial_screen"], ["entered", "gate_passed"])
        rows = evaluate_mission(self.store.connection, self.mission,
                                planned_specs=set(), stage_state=stage_state)
        acn = next(row for row in rows if row["company_ref"] == ACN)
        self.assertEqual((acn["stage"], acn["stage_status"]), ("initial_screen", "gate_passed"))
        _target_entry, skipped = _target(
            mission=self.mission, stage_rows=rows, deliverables={},
            claims={ACN: [{"created_at": "2026-09-01T00:00:00+00:00"}]}, reopens={},
        )
        self.assertIn({"company_ref": ACN, "reason": "initial screen already passed"},
                      skipped)
        # And the diff baseline is still the version the gate cited.
        self.assertEqual(passed_version(self.store.connection,
                                        company_ref=ACN)["version_id"], version["id"])

    def test_a_pass_a_reopen_already_superseded_is_not_a_pass(self):
        # A ``gate_failed`` after a ``gate_passed`` is how a reopened gate
        # reads. ``record_stage`` will not write it -- reopening a passed gate
        # is a human checkpoint with no automatic writer (ADR-0008) -- so the
        # row goes in the way the authority would write it, and the readers
        # that must respect it are the ones under test: without the fold, the
        # weekly lane offers an already-open gate for reopening every week.
        version = self.publish()
        self.pass_gate(version)
        self.thicken(lines=250)
        self.assertEqual(passed_companies(self.store.connection, self.mission), [ACN])
        with self.missions._transaction() as cur:
            latest = cur.execute(
                "SELECT MAX(created_at) FROM coverage_mission_stage_records"
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO coverage_mission_stage_records(record_id,mission_version_ref,"
                "company_ref,stage_ref,status,actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("mission-stage-record:reopen:acn", self.mission["id"], ACN,
                 "initial_screen", "gate_failed", OWNER, '{"rationale":"reopened"}',
                 "e" * 64, latest + "1"),
            )
        self.assertIsNone(passed_version(self.store.connection, company_ref=ACN))
        self.assertEqual(
            reopen_assessment(self.store.connection, company_ref=ACN)["status"],
            "not_passed",
        )
        self.assertEqual(passed_companies(self.store.connection, self.mission), [])

    def test_the_same_evidence_base_always_hashes_the_same(self):
        version = self.publish()
        self.pass_gate(version)
        self.thicken(lines=250)
        first = reopen_assessment(self.store.connection, company_ref=ACN)
        second = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertEqual(first["assessment_hash"], second["assessment_hash"])
        self.thicken(lines=140)
        third = reopen_assessment(self.store.connection, company_ref=ACN)
        self.assertNotEqual(first["assessment_hash"], third["assessment_hash"])


class ProposalTests(ReopenHarness):
    def setUp(self):
        super().setUp()
        self.grant(checkpoints=("gate_reopen",))
        self.version = self.publish()
        self.pass_gate(self.version)
        self.thicken(lines=250)
        self.assessment = reopen_assessment(self.store.connection, company_ref=ACN)

    def propose(self):
        return self.reopens.propose(assessment=self.assessment, mission=self.mission,
                                    actor_ref=AUTOMATION)

    def test_one_proposal_per_company_and_assessment_hash(self):
        first = self.propose()
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["checkpoint_kind"], CHECKPOINT_KIND)
        self.assertEqual(first["flipped"], ["statements"])
        again = self.propose()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(self.reopens.holds_assessment(
            company_ref=ACN, assessment_hash=self.assessment["assessment_hash"]))
        self.assertEqual(len(self.reopens.proposals(ACN)), 1)

    def test_an_assessment_with_no_flip_cannot_be_proposed(self):
        quiet = {**self.assessment, "status": "no_flip", "flipped": []}
        with self.assertRaisesRegex(DeliverableReopenConflict, "flipped"):
            self.reopens.propose(assessment=quiet, mission=self.mission,
                                 actor_ref=AUTOMATION)

    def test_a_proposal_must_name_its_evidence(self):
        bare = {**self.assessment, "evidence_refs": []}
        with self.assertRaisesRegex(DeliverableReopenConflict, "ADR-0008"):
            self.reopens.propose(assessment=bare, mission=self.mission,
                                 actor_ref=AUTOMATION)

    def test_an_automation_actor_must_be_the_mission_principal(self):
        with self.assertRaisesRegex(DeliverableReopenConflict, "mission principal"):
            self.reopens.propose(assessment=self.assessment, mission=self.mission,
                                 actor_ref="automation:somebody-else")

    def test_only_a_person_decides_and_the_decision_is_bound_to_the_proposal(self):
        proposal = self.propose()
        self.assertEqual([p["id"] for p in self.reopens.undecided()], [proposal["id"]])
        with self.assertRaisesRegex(DeliverableReopenValidationError, "human checkpoint"):
            self.reopens.decide(proposal_ref=proposal["id"],
                                proposal_hash=proposal["content_hash"],
                                verdict="approve", reason="go", actor_ref=AUTOMATION)
        with self.assertRaisesRegex(DeliverableReopenConflict, "hash binding"):
            self.reopens.decide(proposal_ref=proposal["id"], proposal_hash="0" * 64,
                                verdict="approve", reason="go", actor_ref=OWNER)
        with self.assertRaisesRegex(DeliverableReopenValidationError, "verdict"):
            self.reopens.decide(proposal_ref=proposal["id"],
                                proposal_hash=proposal["content_hash"],
                                verdict="maybe", reason="go", actor_ref=OWNER)
        with self.assertRaises(DeliverableReopenNotFound):
            self.reopens.decide(proposal_ref="gate-reopen-proposal:nope",
                                proposal_hash="a" * 64, verdict="approve",
                                reason="go", actor_ref=OWNER)
        self.assertEqual(set(VERDICTS), {"approve", "decline"})

    def test_a_second_answer_is_refused_and_the_same_answer_is_a_duplicate(self):
        proposal = self.propose()
        args = dict(proposal_ref=proposal["id"], proposal_hash=proposal["content_hash"],
                    verdict="decline", reason="十条报表行不改变这份文件。", actor_ref=OWNER)
        first = self.reopens.decide(**args)
        self.assertEqual(self.reopens.decide(**args)["status"], "duplicate")
        with self.assertRaisesRegex(DeliverableReopenConflict, "already decline"):
            self.reopens.decide(**{**args, "verdict": "approve", "reason": "改主意了。"})
        self.assertEqual(self.reopens.decision_for(proposal["id"])["id"], first["id"])
        self.assertEqual(self.reopens.undecided(), [])

    def test_an_approval_is_a_permission_that_one_version_spends(self):
        proposal = self.propose()
        self.assertIsNone(approved_reopen(self.store.connection, ACN))
        self.reopens.decide(proposal_ref=proposal["id"],
                            proposal_hash=proposal["content_hash"], verdict="approve",
                            reason="报表行进来了，值得重写。", actor_ref=OWNER)
        permission = approved_reopen(self.store.connection, ACN)
        self.assertEqual(permission["proposal_ref"], proposal["id"])
        self.assertEqual(permission["proposal"]["flipped"], ["statements"])

        reissued = self.publish(sections=self.sections(self.claim_refs[3:9]),
                                revision=reopen_revision(permission), summary="新的摘要。")
        self.assertEqual(reissued["status"], "fresh")
        self.assertEqual(reissued["version"], 2)
        self.assertEqual(reissued["prior_version_ref"], self.version["id"])
        self.assertEqual(reissued["revision"]["change_reason"], CHANGE_REASON_EVIDENCE)
        self.assertEqual(reissued["revision"]["reopen_ref"], proposal["id"])
        self.assertIn(proposal["id"], consumed_reopen_refs(self.store.connection))
        self.assertIsNone(approved_reopen(self.store.connection, ACN))

        # The version that passed, and the stage record that says it passed,
        # are exactly as they were.
        row = self.store.connection.execute(
            "SELECT record_json, content_hash FROM mission_deliverable_versions "
            "WHERE version_id=?", (self.version["id"],),
        ).fetchone()
        self.assertEqual(row["content_hash"], self.version["content_hash"])
        self.assertIsNone(json.loads(row["record_json"])["revision"])
        statuses = [r["status"] for r in self.missions.stage_records(self.mission["id"])
                    if r["company_ref"] == ACN]
        self.assertIn("gate_passed", statuses)

    def test_the_proposal_and_the_decision_cannot_be_edited(self):
        proposal = self.propose()
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store._transaction() as cur:
                cur.execute("UPDATE gate_reopen_proposals SET flipped_count=9 "
                            "WHERE proposal_id=?", (proposal["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store._transaction() as cur:
                cur.execute("DELETE FROM gate_reopen_proposals WHERE proposal_id=?",
                            (proposal["id"],))


class RevisionFieldTests(ReopenHarness):
    def test_a_change_reason_outside_adr_0008_s_vocabulary_is_refused(self):
        with self.assertRaisesRegex(MissionDeliverableValidationError, "change_reason"):
            validate_revision({"change_reason": "because I felt like it",
                               "evidence_refs": ["r"]})
        with self.assertRaisesRegex(MissionDeliverableValidationError, "unknown fields"):
            validate_revision({"change_reason": "human_revision", "evidence_refs": ["r"],
                               "mood": "confident"})

    def test_a_reason_is_not_a_change(self):
        first = self.publish()
        again = self.publish(revision={"change_reason": "human_revision",
                                       "evidence_refs": ["evidence:whatever"]})
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])

    def test_an_ordinary_redraft_still_needs_no_reason(self):
        first = self.publish()
        second = self.publish(sections=self.sections(self.claim_refs[3:6]))
        self.assertEqual(second["version"], 2)
        self.assertIsNone(second["revision"])
        self.assertEqual(second["prior_version_ref"], first["id"])


class SelectionRuleTests(ReopenHarness):
    def entry(self, status):
        return {
            "company_ref": ACN, "ticker": "ACN", "stage": "initial_screen",
            "stage_status": status,
            "items": [{"label": "过去 4 个季度的财报数字", "status": "complete"}],
        }

    def claims_map(self):
        return {ACN: [{"ref": self.claim_refs[0],
                       "created_at": "2026-09-01T00:00:00+00:00"}]}

    def test_a_passed_gate_is_skipped_until_a_person_approves_a_reopen(self):
        target, skipped = _target(
            mission=self.mission, stage_rows=[self.entry("gate_passed")],
            deliverables={}, claims=self.claims_map(),
        )
        self.assertIsNone(target)
        self.assertEqual(skipped[0]["reason"], "initial screen already passed")

        permission = {"id": "gate-reopen-decision:x", "proposal_ref": "gate-reopen-proposal:x",
                      "proposal": {"change_reason": CHANGE_REASON_EVIDENCE,
                                   "evidence_refs": ["statement-ingest:1"],
                                   "passed_version_ref": "mission-deliverable-version:1"}}
        target, skipped = _target(
            mission=self.mission, stage_rows=[self.entry("gate_passed")],
            deliverables={}, claims=self.claims_map(),
            reopens={ACN: permission},
        )
        self.assertIsNotNone(target)
        self.assertEqual(target["reopen"], permission)
        self.assertEqual(skipped, [])

    def test_an_approval_also_defeats_the_nothing_new_rule(self):
        # The finding that produced the reopen is that evidence arrived which
        # is not a Claim of this company's, so "nothing new" would be wrong.
        deliverables = {ACN: {"created_at": "2026-09-30T00:00:00+00:00"}}
        target, skipped = _target(
            mission=self.mission, stage_rows=[self.entry("entered")],
            deliverables=deliverables, claims=self.claims_map(),
        )
        self.assertIsNone(target)
        self.assertEqual(skipped[0]["reason"], "nothing new since the last version")
        target, _ = _target(
            mission=self.mission, stage_rows=[self.entry("entered")],
            deliverables=deliverables, claims=self.claims_map(),
            reopens={ACN: {"id": "d", "proposal_ref": "p",
                           "proposal": {"change_reason": CHANGE_REASON_EVIDENCE,
                                        "evidence_refs": ["r"],
                                        "passed_version_ref": "v"}}},
        )
        self.assertIsNotNone(target)

    def test_the_revision_block_names_the_approval_the_proposal_and_the_old_version(self):
        block = reopen_revision({
            "id": "gate-reopen-decision:d", "proposal_ref": "gate-reopen-proposal:p",
            "proposal": {"change_reason": CHANGE_REASON_EVIDENCE,
                         "evidence_refs": ["statement-ingest:1", "figure:2"],
                         "passed_version_ref": "mission-deliverable-version:old"},
        })
        self.assertEqual(block["change_reason"], CHANGE_REASON_EVIDENCE)
        self.assertEqual(block["reopen_ref"], "gate-reopen-proposal:p")
        self.assertEqual(block["evidence_refs"][:3],
                         ["gate-reopen-decision:d", "gate-reopen-proposal:p",
                          "mission-deliverable-version:old"])
        self.assertIsNone(reopen_revision(None))


if __name__ == "__main__":
    unittest.main()
