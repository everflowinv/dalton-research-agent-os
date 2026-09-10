"""P12d: the child spends nothing without a grant, and never decides the gate.

The child's shape is the dossier and debate-map children's: check the mission's
``may_write`` before spending anything, derive the work from what the Core holds
rather than draining a queue, and report ``idle`` when there is nothing to do.

What is different, and what these tests are mostly about, is what happens on
either side of ``publish``.

In front of it there are six ways to be refused, and each leaves the chain as it
was: the reply left its contract, question one disagreed with the dossier the
draft was made from, the verifier was not independent or did not pass, a ref
stopped resolving, a hard structural check failed, or ADR-0008 found nothing new.

Behind it there is a person. Publishing opens the checkpoint and stops; the lane
will not draft again for a company whose draft is waiting, because asking the
owner the same question twice is the failure this whole layer is trying to
avoid. Approving writes the ``gate_passed`` stage record through the ladder that
already exists. Returning writes no stage record at all and lets the lane draft
again once the file has moved.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from dalton_core.company_dossier import (
    CLASSIFICATION_SLOTS, SECTIONS, CompanyDossierAuthority,
    causal_chain_hash, policy_hash, validate_policy,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.deep_insight_gate import (
    DEEP_INSIGHT_GATE_RUBRIC, GROUPS, QUESTION_REFS, DeepInsightGateAuthority,
)
from dalton_core.deep_insight_gate_cli import (
    build_parser, deliverable_sections, granted_scope, group_material, run_gate,
    screened_companies,
)
from dalton_core.deep_insight_gate_launcher import DeepInsightGateLauncher, run_digest
from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_deep_insight_lane import (
    LANE, MissionDeepInsightLaneCoordinator, build_launcher, dispatch,
    ledger_signature,
)
from dalton_core.store import content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_claim_index_entries import ACN, LedgerFixture

AUTOMATION = "automation:coverage-mission"
OWNER = "human:coverage-owner"
CHAIN = ["Bookings lead revenue by two to four quarters."]

def policy_document():
    return validate_policy({
        "schema_version": "0.1",
        "policy_ref": "dossier-policy:test:v1",
        "causal_chain_maps": [{
            "constitution_ref": "constitution:us-it-services",
            "causal_chain_hash": causal_chain_hash(CHAIN),
            "sections": ["demand_drivers"],
            "note": "the fixture constitution's single link",
        }],
        "output_rubric_bindings": [{
            "criterion_hash": content_hash("State what changed and its impact."),
            "check": "not_a_restatement", "reason": "",
        }],
    })


class FakeModel:
    """Answers whatever the prompt asked, in the shape the contract wants.

    It reads the question ids out of the prompt rather than being told them, so
    a drafter that stopped printing its questions would fail these tests rather
    than pass them by agreement.
    """

    def __init__(self, *, route="route:draft", verdict="pass", findings=(),
                 sentence="这一问的判断由所引材料支撑。", classification=None,
                 answer_all=False, extra_number=None, break_contract=False):
        self.route = route
        self.verdict = verdict
        self.findings = list(findings)
        self.sentence = sentence
        self.classification = classification or "contract_compounder"
        self.answer_all = answer_all
        self.extra_number = extra_number
        self.break_contract = break_contract
        self.prompts: list[str] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        if prompt.startswith("You are an independent verifier"):
            return self._envelope(json.dumps(
                {"verdict": self.verdict, "findings": self.findings}))
        if self.break_contract:
            return self._envelope(json.dumps({"answers": []}))
        refs = re.findall(r"^  (q\d+)\t", prompt, flags=re.MULTILINE)
        tag = next((candidate for candidate in ("C1", "D1", "N1")
                    if f"\n{candidate}\t" in prompt), None)
        body = self.sentence
        if self.extra_number is not None:
            body = f"{self.sentence}规模约为 {self.extra_number}。"
        answers = []
        for index, ref in enumerate(refs):
            if tag is not None and (index == 0 or self.answer_all):
                answers.append({
                    "question_ref": ref, "status": "answered",
                    "confidence": "medium",
                    "sentences": [{"text": body, "refs": [tag]}], "gaps": [],
                })
            else:
                answers.append({
                    "question_ref": ref, "status": "unknown",
                    "missing": "展示的材料没有回答这一问",
                    "evidence_that_would_answer": "下一份 10-K 的分部披露就能定",
                    "gaps": [],
                })
        payload = {"answers": answers}
        if "q1" in refs:
            payload["classification"] = self.classification
        return self._envelope(json.dumps(payload, ensure_ascii=False))

    def _envelope(self, text):
        return {"text": text, "replayed": False, "cost_micros": 1000,
                "work_order_ref": f"work:cockpit-gate-{len(self.prompts)}",
                "route_decision_ref": self.route}


FAMILIES = {"route:draft": "family-a", "route:verify": "family-b"}


def resolver(ref):
    return FAMILIES.get(ref)


def drafted_section(aspect, ref, *, structure=None, text="这一节的判断由引用支撑。",
                    source_text="合同期限为五年"):
    ids = list(structure or [aspect])
    return {
        "aspect": aspect, "status": "drafted", "reason": None, "structure": ids,
        "slots": [{"slot_id": ids[0], "sentences": [{"text": text, "refs": [ref]}]}]
        + [{"slot_id": item, "unknown": "材料没有回答这一点"} for item in ids[1:]],
        "sources": [{"kind": "claim", "ref": ref, "text": source_text,
                     "period": "2026-05-31"}],
        "gaps": ["这一节还缺一份行业口径的定义"], "profile": None,
    }


def unavailable_section(aspect, reason="no_canonical_claims"):
    return {"aspect": aspect, "status": "unavailable", "reason": reason,
            "structure": [], "slots": [], "sources": [], "gaps": [], "profile": None}


class Harness:
    def __init__(self, *, may_write=None, screened=True, dossier=True, debate=True,
                 classification="contract_compounder"):
        self._dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._dir.name)
        self.fixture = LedgerFixture(str(self.state_dir / "core.sqlite"))
        self.store = self.fixture.store
        state = bootstrap_method_authorities(self.store)
        self.constitution = state["constitution"]
        self.playbook = state["playbook"]
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        scopes = (list(params["autonomy"]["may_write"]) if may_write is None
                  else list(may_write))
        if may_write is None and "deliverable" not in scopes:
            scopes.append("deliverable")
        params["autonomy"] = {**params["autonomy"], "may_write": scopes}
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        if screened:
            self.pass_screen()
        self.claims = {
            name: self.fixture.add_claim(
                name, kind="qualitative", value=None, unit=None,
                statement=f"关于 {name} 的一句定性结论").
            __getitem__("claim_version_id")
            for name in ("c-business", "c-compete", "c-kpi", "c-price", "c-mix",
                         "c-management", "c-class", "c-variant")
        }
        self.dossiers = CompanyDossierAuthority(self.store)
        self.dossier = (self.publish_dossier(classification=classification)
                        if dossier else None)
        self.map_version = self.publish_debate_map() if debate else None
        self.policy_path = self.state_dir / "policy.json"
        self.policy_path.write_text(json.dumps(policy_document()), encoding="utf-8")
        self.model_config = self.state_dir / "model.json"
        self.model_config.write_text("{}", encoding="utf-8")

    def pass_screen(self):
        for status in ("entered", "gate_passed"):
            self.missions.record_stage(
                mission_version_ref=self.mission["id"],
                mission_version_hash=self.mission["content_hash"],
                company_ref=ACN, stage_ref="initial_screen", status=status,
                evidence_refs=[self.mission["id"]], rationale="fixture",
                actor_ref=AUTOMATION, idempotency_key=f"fixture:{status}",
            )

    def dossier_bindings(self):
        return {
            "constitution_version": {"ref": self.constitution["id"],
                                     "hash": self.constitution["content_hash"]},
            "playbook_version": {"ref": self.playbook["id"],
                                 "hash": self.playbook["content_hash"]},
            "mission_version_ref": self.mission["id"],
            "policy_ref": "dossier-policy:test:v1",
            "policy_hash": policy_hash(policy_document()),
            "causal_chain_hash": causal_chain_hash(CHAIN),
            "rubric_ref": "rubric:company-dossier", "rubric_hash": "e" * 64,
        }

    def publish_dossier(self, *, classification="contract_compounder", extra=None):
        refs = {
            "business_model": self.claims["c-business"],
            "competitive_position": self.claims["c-compete"],
            "kpi_dictionary": self.claims["c-kpi"],
            "history_of_price_drivers": self.claims["c-price"],
            "segments_and_mix": self.claims["c-mix"],
            "management_and_capital_allocation": self.claims["c-management"],
        }
        if extra:
            refs.update(extra)
        sections = []
        for aspect in SECTIONS:
            if aspect in refs:
                sections.append(drafted_section(aspect, refs[aspect]))
            elif aspect == "demand_drivers":
                sections.append(unavailable_section(aspect, "no_canonical_claims"))
            elif aspect == "supply_and_cost":
                sections.append(unavailable_section(aspect, "causal_chain_unmapped"))
            else:
                sections.append(unavailable_section(aspect))
        record = {
            "company_ref": ACN,
            "sections": sections,
            "industry_classification": {
                "classification": classification,
                "slots": [{"slot_id": slot,
                           "sentences": [{"text": f"{slot} 的理由。",
                                          "refs": [self.claims["c-class"]]}]}
                          for slot in CLASSIFICATION_SLOTS],
                "sources": [{"kind": "claim", "ref": self.claims["c-class"],
                             "text": "合同期限为五年", "period": None}],
                "gaps": [],
            },
            # Unavailable rather than drafted, and not by choice: a drafted
            # variant view cannot round-trip through P12a's validator today
            # (``validate_variant_view`` drops ``gaps`` on the drafted branch,
            # so the body hash it publishes is not the body it re-reads). The
            # gate handles the absence the way it handles any missing source --
            # the questions that rest on it come back unknown -- and the report
            # names the one-line fix for the integrator.
            "variant_view": {
                "status": "unavailable", "reason": "no_canonical_claims",
                "market_view_available": False, "market_view_reason": None,
                "structure": [], "slots": [], "sources": [],
                "gaps": ["没有共识数字，市场那一侧只能留白"],
            },
            "bindings": self.dossier_bindings(),
            "actor_ref": AUTOMATION,
            "change_reason": "evidence_thicker",
            "evidence_refs": [{"kind": "claim", "ref": self.claims["c-business"],
                               "text": "合同期限为五年", "period": None}],
            "decision": None,
            "drafted_at": {},
        }
        return self.dossiers.publish(record)

    def publish_debate_map(self):
        from dalton_core.debate_map import DebateMapAuthority, evidence_fingerprint

        authority = DebateMapAuthority(self.store)
        return authority.publish_map(
            subject_ref=ACN, subject_kind="company", change_reason="evidence_thicker",
            change_evidence_refs=[self.claims["c-compete"]],
            constitution_ref=self.constitution["id"],
            constitution_hash=self.constitution["content_hash"],
            evidence_fingerprint=evidence_fingerprint([self.claims["c-compete"]]),
            debates=[{
                "debate_ref": "debate:acn:ai-conversion",
                "question": "AI 订单能不能转成收入？",
                "driver_refs": ["driver:d"], "admission_index": 0,
                "causal_link_index": 0,
                "bull_position": {"statement": "订单已经在转",
                                  "claim_refs": [self.claims["c-compete"]]},
                "bear_position": {"statement": "转化率被夸大了",
                                  "claim_refs": [self.claims["c-business"]]},
                "market_position": {"available": False, "lean": None,
                                    "statement": None, "refs": []},
                "our_position": {"state": "none_yet", "side": None,
                                 "statement": None, "refs": []},
                "status": "open", "last_shift_reason": None,
                "first_seen_at": "2026-09-01T00:00:00+00:00",
                "source_independence": {"bull_sources": 1, "bear_sources": 1},
            }],
            rejected_by_constitution=[{
                "candidate_ref": "candidate:acn:fx",
                "question": "汇率会不会吃掉利润？", "driver_refs": [],
                "reasons": ["no_bear_evidence"],
                "observed_at": "2026-09-01T00:00:00+00:00",
            }],
            actor_ref=AUTOMATION, created_at="2026-09-02T00:00:00+00:00",
        )

    def run(self, **kwargs):
        options = {
            "state_dir": self.state_dir,
            "model_config_path": self.model_config,
            "summary_dir": self.state_dir / "summary",
            "policy_path": self.policy_path,
            "model_factory": lambda: FakeModel(),
            "verifier_model_factory": lambda: FakeModel(route="route:verify"),
            "family_resolver": resolver,
        }
        options.update(kwargs)
        return run_gate(**options)

    def gates(self):
        return DeepInsightGateAuthority(self.store)

    def close(self):
        self.fixture.close()
        self._dir.cleanup()


class GrantTests(unittest.TestCase):
    def test_a_mission_that_has_not_granted_deliverable_spends_nothing(self):
        harness = Harness(may_write=["claim", "stage_record", "observation",
                                     "research_question", "dossier"])
        self.addCleanup(harness.close)
        summary = harness.run()
        self.assertEqual((summary["status"], summary["gate_status"]),
                         ("held", "not_authorized"))
        self.assertIsNone(summary["version_ref"])

    def test_the_word_is_deliverable_and_there_is_no_fallback(self):
        self.assertEqual(granted_scope({"autonomy": {"may_write": ["deliverable"]}}),
                         "deliverable")
        self.assertIsNone(granted_scope({"autonomy": {"may_write": ["dossier"]}}))

    def test_a_company_that_has_not_passed_its_screen_has_no_gate_to_answer(self):
        harness = Harness(screened=False)
        self.addCleanup(harness.close)
        self.assertEqual(screened_companies(harness.missions, harness.mission), [])
        self.assertEqual(harness.run()["gate_status"], "no_screened_company")

    def test_a_company_with_no_dossier_is_not_eligible(self):
        harness = Harness(dossier=False)
        self.addCleanup(harness.close)
        summary = harness.run()
        self.assertEqual(summary["gate_status"], "no_eligible_company")
        self.assertEqual(summary["blocked"][ACN], "no_dossier")

    def test_a_run_without_a_verifier_configuration_is_held(self):
        harness = Harness()
        self.addCleanup(harness.close)
        summary = harness.run(verifier_model_factory=None)
        self.assertEqual((summary["status"], summary["gate_status"]),
                         ("held", "no_verifier"))

    def test_an_installation_without_the_policy_is_held_not_crashed(self):
        harness = Harness()
        self.addCleanup(harness.close)
        summary = harness.run(policy_path=harness.state_dir / "absent.json")
        self.assertEqual((summary["status"], summary["gate_status"]),
                         ("held", "no_policy"))


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.dossier = self.harness.dossiers.latest(ACN)

    def test_every_group_is_shown_the_sections_its_questions_rest_on(self):
        for group in GROUPS:
            rows, _notes = group_material(
                group=group, dossier=self.dossier,
                map_version=self.harness.map_version, numbers=[])
            with self.subTest(group=group):
                self.assertTrue(rows, "a group with no material cannot be drafted")

    def test_the_file_is_citable_as_itself_and_so_are_the_debates(self):
        rows, _ = group_material(group="market", dossier=self.dossier,
                                 map_version=self.harness.map_version, numbers=[])
        kinds = {row["kind"] for row in rows}
        self.assertIn("dossier_section", kinds)
        self.assertIn("debate", kinds)
        self.assertTrue(any(row["tag"].startswith("D") for row in rows))

    def test_the_twelfth_question_is_told_what_the_file_already_knows_is_missing(self):
        _rows, notes = group_material(group="thesis", dossier=self.dossier,
                                      map_version=self.harness.map_version, numbers=[])
        self.assertTrue(any("整节缺" in note for note in notes))
        self.assertTrue(any("宪法拒绝的候选争议" in note for note in notes))

    def test_a_dry_run_plans_and_writes_nothing(self):
        summary = self.harness.run(dry_run=True)
        self.assertEqual(summary["gate_status"], "dry_run")
        self.assertEqual(self.harness.gates().versions(ACN), [])


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def test_a_run_submits_twelve_answers_and_opens_the_checkpoint(self):
        summary = self.harness.run()
        self.assertEqual((summary["status"], summary["gate_status"]),
                         ("succeeded", "submitted"))
        self.assertEqual(summary["checkpoint_kind"], "deep_insight_gate")
        draft = self.harness.gates().gate(summary["version_ref"])
        self.assertEqual([row["question_ref"] for row in draft["answers"]],
                         list(QUESTION_REFS))
        self.assertEqual(len(self.harness.gates().undecided()), 1)

    def test_most_questions_come_back_unknown_and_say_what_would_answer_them(self):
        summary = self.harness.run()
        draft = self.harness.gates().gate(summary["version_ref"])
        unknowns = [row for row in draft["answers"] if row["status"] == "unknown"]
        self.assertEqual(summary["unknown"], len(unknowns))
        self.assertGreater(len(unknowns), summary["answered"])
        for row in unknowns:
            self.assertTrue(row["unknown"]["evidence_that_would_answer"].strip())

    def test_the_draft_binds_the_file_and_the_map_it_was_made_from(self):
        summary = self.harness.run()
        draft = self.harness.gates().gate(summary["version_ref"])
        self.assertEqual(draft["bindings"]["dossier_version_ref"],
                         self.harness.dossier["id"])
        self.assertEqual(draft["bindings"]["debate_map_version_ref"],
                         self.harness.map_version["id"])
        self.assertEqual(draft["bindings"]["rubric_hash"],
                         DEEP_INSIGHT_GATE_RUBRIC.content_hash)

    def test_the_questions_answered_are_the_bound_playbooks_own(self):
        summary = self.harness.run()
        draft = self.harness.gates().gate(summary["version_ref"])
        stage = next(item for item in self.harness.playbook["stages"]
                     if item["stage_ref"] == "deep_insight_gate")
        self.assertEqual([row["question"] for row in draft["answers"]],
                         stage["exit_gate"]["questions"])

    def test_the_reader_copy_is_published_as_a_deliverable(self):
        from dalton_core.mission_deliverable import MissionDeliverableAuthority

        summary = self.harness.run()
        self.assertEqual(summary["deliverable_status"]["status"], "fresh")
        published = MissionDeliverableAuthority(self.harness.store).deliverables(
            self.harness.mission["id"], kind="deep_insight_gate")
        self.assertEqual(len(published), 1)
        self.assertEqual(len(published[0]["sections"]), 12)

    def test_a_second_run_on_an_unchanged_file_has_nothing_to_do(self):
        first = self.harness.run()
        self.assertEqual(first["gate_status"], "submitted")
        second = self.harness.run()
        # Blocked on the pending decision before anything else: a lane that
        # drafted here would be asking the owner the same question twice.
        self.assertEqual(second["blocked"][ACN], "awaiting_decision")
        self.assertEqual(second["gate_status"], "no_eligible_company")


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def test_a_reply_that_leaves_its_contract_is_refused_whole(self):
        summary = self.harness.run(model_factory=lambda: FakeModel(break_contract=True))
        self.assertEqual(summary["gate_status"], "nothing_drafted")
        self.assertEqual(len(summary["refused"]), len(GROUPS))
        self.assertEqual(self.harness.gates().versions(ACN), [])

    def test_question_one_disagreeing_with_the_dossier_refuses_the_draft(self):
        summary = self.harness.run(
            model_factory=lambda: FakeModel(classification="turnaround"))
        self.assertEqual(summary["gate_status"], "classification_conflict")
        self.assertIn("contract_compounder", summary["failure_reason"])
        self.assertEqual(self.harness.gates().versions(ACN), [])

    def test_a_verifier_that_rejects_stops_the_submission(self):
        summary = self.harness.run(
            verifier_model_factory=lambda: FakeModel(
                route="route:verify", verdict="reject",
                findings=[{"question_ref": "q1", "code": "unsupported_answer",
                           "detail": "这句话超出了它引用的那一行"}]))
        self.assertEqual(summary["gate_status"], "verification_failed")
        self.assertEqual(self.harness.gates().versions(ACN), [])

    def test_a_verifier_of_the_same_family_is_not_a_verifier(self):
        summary = self.harness.run(
            verifier_model_factory=lambda: FakeModel(route="route:draft"))
        self.assertEqual(summary["gate_status"], "not_independent")
        self.assertFalse(summary["verification"]["independent"])
        self.assertEqual(self.harness.gates().versions(ACN), [])

    def test_an_unresolvable_family_fails_closed_before_paying_for_a_verdict(self):
        summary = self.harness.run(family_resolver=lambda ref: None)
        self.assertEqual(summary["gate_status"], "not_independent")
        self.assertEqual(summary["verification"]["status"], "skipped")

    def test_a_figure_with_no_source_fails_the_hard_check(self):
        summary = self.harness.run(
            model_factory=lambda: FakeModel(extra_number="47.3 亿美元"))
        self.assertEqual(summary["gate_status"], "rubric_refused")
        self.assertIn("numbers_without_refs", summary["failure_reason"])
        self.assertEqual(self.harness.gates().versions(ACN), [])


class RedraftTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.first = self.harness.run()
        self.assertEqual(self.first["gate_status"], "submitted")
        self.gates = self.harness.gates()

    def decide(self, decision, reason="裁决理由"):
        draft = self.gates.gate(self.first["version_ref"])
        return self.gates.decide(
            gate_version_ref=draft["id"], gate_version_hash=draft["content_hash"],
            decision=decision, reason=reason, actor_ref=OWNER)

    def test_an_approved_gate_is_not_redrafted_by_this_lane(self):
        self.decide("approve")
        summary = self.harness.run()
        self.assertEqual(summary["blocked"][ACN], "decided:approve")

    def test_a_rejected_gate_is_not_redrafted_by_this_lane(self):
        self.decide("reject")
        summary = self.harness.run()
        self.assertEqual(summary["blocked"][ACN], "decided:reject")

    def test_a_returned_gate_waits_for_new_evidence(self):
        self.decide("return_for_more_work", reason="第七问没有价格材料")
        summary = self.harness.run()
        # Returned, but the file has not moved: ADR-0008 says a redraft that
        # cites nothing new is not a version, and the lane says so before
        # spending a model call.
        self.assertEqual(summary["blocked"][ACN], "nothing_new")

    def test_a_returned_gate_is_redrafted_once_the_file_moves(self):
        self.decide("return_for_more_work", reason="第七问没有价格材料")
        fresh = self.harness.fixture.add_claim(
            "c-new", kind="qualitative", value=None, unit=None,
            statement="新的一条定性结论")["claim_version_id"]
        self.harness.publish_dossier(extra={"guidance_style": fresh})
        summary = self.harness.run()
        self.assertEqual(summary["gate_status"], "submitted")
        self.assertEqual(self.gates.latest(ACN)["version"], 2)
        self.assertEqual(len(self.gates.undecided()), 1)


class DecisionOperationTests(unittest.TestCase):
    """The writer op: the ladder is the mission's, and only a person walks it."""

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.summary = self.harness.run()
        self.assertEqual(self.summary["gate_status"], "submitted")
        self.draft = self.harness.gates().gate(self.summary["version_ref"])
        self.harness.fixture.close()
        self.socket = str(self.harness.state_dir / "writer.sock")

    def server(self):
        from dalton_core.writer_server import CORE_OPERATIONS, Principal, WriterServer

        principals = {
            "core": Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True),
        }
        server = WriterServer(
            self.harness.state_dir / "core.sqlite", self.socket, principals)
        server.start()
        self.addCleanup(server.stop)
        return server

    @staticmethod
    def on_store(server, call):
        """Run an operation where the writer runs them: its single store thread.

        The writer opens its Core on one thread and answers every request there,
        so a test that called an ``_op_`` from its own thread would be testing a
        connection SQLite refuses to hand over rather than the operation.
        """

        return server._store_executor.submit(call).result(timeout=30)

    def decide(self, server, decision, reason="十二问够看了", actor_ref=OWNER):
        return self.on_store(server, lambda: server._op_decide_deep_insight_gate({
            "gate_version_ref": self.draft["id"],
            "gate_version_hash": self.draft["content_hash"],
            "decision": decision, "reason": reason, "actor_ref": actor_ref,
        }))

    def stage_records(self, server):
        mission_ref = self.draft["bindings"]["mission_version_ref"]
        return self.on_store(server, lambda: [
            (record["stage_ref"], record["status"])
            for record in server.coverage_mission.stage_records(mission_ref, ACN)
        ])

    def test_approving_enters_and_passes_the_stage_through_the_existing_ladder(self):
        server = self.server()
        result = self.decide(server, "approve")
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["stage_status"], "gate_passed")
        self.assertIn(("deep_insight_gate", "entered"), self.stage_records(server))
        self.assertIn(("deep_insight_gate", "gate_passed"), self.stage_records(server))

    def test_returning_writes_no_stage_record_at_all(self):
        server = self.server()
        result = self.decide(server, "return_for_more_work", reason="第七问没有材料")
        self.assertIsNone(result["stage_status"])
        self.assertEqual(
            [row for row in self.stage_records(server)
             if row[0] == "deep_insight_gate"], [])

    def test_rejecting_records_a_failed_gate(self):
        server = self.server()
        self.decide(server, "reject", reason="覆盖价值不够")
        self.assertIn(("deep_insight_gate", "gate_failed"), self.stage_records(server))

    def test_deciding_twice_is_idempotent_rather_than_a_second_ladder_row(self):
        server = self.server()
        self.decide(server, "approve")
        again = self.decide(server, "approve")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(
            [row for row in self.stage_records(server)
             if row == ("deep_insight_gate", "gate_passed")],
            [("deep_insight_gate", "gate_passed")])

    def test_automation_cannot_reach_the_decision_at_all(self):
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS, MISSION_AUTOMATION_OPERATIONS, Principal,
        )

        self.assertIn("decide_deep_insight_gate", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertNotIn("decide_deep_insight_gate", MISSION_AUTOMATION_OPERATIONS)
        server = self.server()
        principal = Principal("coverage-governance", "t",
                              HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER)
        params = {"gate_version_ref": self.draft["id"],
                   "gate_version_hash": self.draft["content_hash"],
                   "decision": "approve", "reason": "r"}
        # An automation actor supplied by the caller is refused outright, and
        # an omitted one is replaced by the authenticated principal's. Either
        # way the ``human:`` rule is enforced by the gate rather than declared
        # by the caller.
        with self.assertRaises(PermissionError):
            server._authorized_params(
                principal, "decide_deep_insight_gate",
                {**params, "actor_ref": AUTOMATION})
        bound = server._authorized_params(
            principal, "decide_deep_insight_gate", params)
        self.assertEqual(bound["actor_ref"], OWNER)

    def test_the_submissions_view_lists_the_draft_with_its_twelve_answers(self):
        server = self.server()
        listed = self.on_store(
            server, lambda: server._op_deep_insight_gate_submissions({}))["drafts"]
        self.assertEqual([row["version_ref"] for row in listed], [self.draft["id"]])
        self.assertEqual(len(listed[0]["answers"]), 12)
        self.decide(server, "approve")
        self.assertEqual(
            self.on_store(
                server, lambda: server._op_deep_insight_gate_submissions({}))["drafts"],
            [])


class CockpitTests(unittest.TestCase):
    def test_the_undecided_gate_is_listed_with_a_plain_title(self):
        from dalton_core import cockpit_plane

        source = Path(cockpit_plane.__file__).read_text(encoding="utf-8")
        self.assertIn("deep_insight_gate_versions", source)
        self.assertIn("深度认知门十二问：是否让这家公司进入完整覆盖", source)
        self.assertIn('"decide_deep_insight_gate"', source)


class LaneTests(unittest.TestCase):
    def test_the_lane_is_registered_once_after_the_two_files_it_reads(self):
        spec = lane_for_operation("dispatch_deep_insight_gate")
        self.assertIsNotNone(spec)
        self.assertIs(spec, LANE)
        self.assertEqual(spec.order, 138)
        self.assertEqual(spec.driver_key, "deep_insight_gate")
        orders = {item.operation: item.order for item in registered_lanes()}
        self.assertLess(orders["dispatch_company_dossier"], spec.order)
        self.assertLess(orders["dispatch_debate_map"], spec.order)

    def test_the_lane_is_absent_without_a_model_configuration(self):
        class Args:
            deep_insight_gate_model_config = None

        self.assertIsNone(build_launcher(Args()))

    def test_the_launch_agent_argv_needs_the_config_and_the_policy(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)

            class Context:
                pass

            context = Context()
            context.state = state
            self.assertEqual(LANE.argv_fragment(context), [])
            (state / "initial-screen-model-config.json").write_text("{}")
            (state / "p12a-dossier-policy-v1.json").write_text("{}")
            argv = LANE.argv_fragment(context)
            self.assertIn("--deep-insight-gate-model-config", argv)
            self.assertNotIn("--deep-insight-gate-verifier-model-config", argv)
            (state / "dossier-verifier-model-config.json").write_text("{}")
            self.assertIn("--deep-insight-gate-verifier-model-config",
                          LANE.argv_fragment(context))

    def test_a_writer_without_the_lane_reports_it_rather_than_raising(self):
        class Server:
            lane_state: dict = {}

            def lane_launcher(self, name):
                return None

        self.assertEqual(dispatch(Server(), {})["status"], "unconfigured")

    def test_the_signature_moves_when_a_draft_or_a_decision_lands(self):
        harness = Harness()
        self.addCleanup(harness.close)
        before = ledger_signature(harness.store.connection)
        summary = harness.run()
        after = ledger_signature(harness.store.connection)
        self.assertNotEqual(before, after)
        draft = harness.gates().gate(summary["version_ref"])
        harness.gates().decide(
            gate_version_ref=draft["id"], gate_version_hash=draft["content_hash"],
            decision="approve", reason="通过", actor_ref=OWNER)
        self.assertNotEqual(after, ledger_signature(harness.store.connection))

    def test_the_coordinator_stays_quiet_on_an_unchanged_signature(self):
        class Launcher:
            def __init__(self):
                self.started = 0

            def start(self, *, signature, company_ref=None):
                self.started += 1
                return {"id": f"ticket-{self.started}", "signature": signature}

            def status(self, ticket_ref):
                return {"status": "succeeded", "signature": self.signature,
                        "summary": {"gate_status": "no_eligible_company"}}

        harness = Harness()
        self.addCleanup(harness.close)
        launcher = Launcher()
        coordinator = MissionDeepInsightLaneCoordinator(
            connection=harness.store.connection, launcher=launcher)
        launched = coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        launcher.signature = launched["signature"]
        settled = coordinator.dispatch_once()
        self.assertEqual(settled["status"], "idle")
        self.assertEqual(launcher.started, 1)

    def test_the_ticket_is_named_by_the_company_and_the_evidence(self):
        self.assertNotEqual(run_digest(ACN, "sig-a"), run_digest(ACN, "sig-b"))
        self.assertEqual(run_digest(ACN, "sig-a"), run_digest(ACN, "sig-a"))

    def test_the_launcher_passes_every_configured_path_to_the_child(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            launcher = DeepInsightGateLauncher(
                state_dir=state, model_config_path=state / "model.json",
                verifier_model_config_path=state / "verify.json",
                policy_path=state / "policy.json")
            command = launcher._command(ticket_dir=state / "ticket")
            self.assertTrue(launcher.configured)
            for flag in ("--model-config", "--verifier-model-config", "--gate-policy"):
                self.assertIn(flag, command)

    def test_the_child_parser_takes_the_arguments_the_launcher_sends(self):
        args = build_parser().parse_args([
            "--state-dir", "/tmp/x", "--model-config", "/tmp/m.json",
            "--verifier-model-config", "/tmp/v.json", "--gate-policy", "/tmp/p.json",
            "--company-ref", ACN, "--quiet",
        ])
        self.assertEqual(args.company_ref, ACN)
        self.assertTrue(args.quiet)


class DeliverableKindTests(unittest.TestCase):
    """The reader's copy needs a kind, and old Cores need to be told about it."""

    def test_a_core_built_before_this_kind_existed_is_widened_not_broken(self):
        from dalton_core.mission_deliverable import (
            DELIVERABLE_KINDS, MissionDeliverableAuthority,
        )
        from dalton_core.store import DaltonStore

        self.assertIn("deep_insight_gate", DELIVERABLE_KINDS)
        with tempfile.TemporaryDirectory() as name:
            store = DaltonStore(str(Path(name) / "core.sqlite"))
            self.addCleanup(store.close)
            # The table exactly as a Core created under P14a's eight-kind CHECK
            # holds it. ``CREATE TABLE IF NOT EXISTS`` would leave it refusing
            # the ninth kind for ever, with an IntegrityError rather than the
            # readable refusal the vocabulary check gives.
            store.connection.executescript(
                """
                CREATE TABLE mission_deliverable_versions (
                    version_id TEXT PRIMARY KEY,
                    deliverable_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 1),
                    prior_version_ref TEXT,
                    mission_version_ref TEXT NOT NULL,
                    mission_version_hash TEXT NOT NULL,
                    playbook_version_ref TEXT NOT NULL,
                    playbook_version_hash TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN (
                        'industry_framework','initial_screen','industry_model',
                        'company_model','forecast_lines','investment_memo',
                        'weekly_brief','event_note'
                    )),
                    subject_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deliverable_ref, version_number)
                );
                """
            )
            MissionDeliverableAuthority(store)
            sql = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='mission_deliverable_versions'").fetchone()[0]
            self.assertIn("'deep_insight_gate'", sql)
            self.assertIn("'event_note'", sql)
            self.assertEqual(
                store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


class DeliverableRenderingTests(unittest.TestCase):
    def test_an_unknown_answer_reads_as_what_is_missing_and_what_would_settle_it(self):
        harness = Harness()
        self.addCleanup(harness.close)
        summary = harness.run()
        record = harness.gates().gate(summary["version_ref"])
        sections = deliverable_sections(record)
        unknown = next(section for section in sections if "未答" in section["body"])
        self.assertIn("能定这一问的证据", unknown["body"])
        self.assertEqual(len(sections), 12)


if __name__ == "__main__":
    unittest.main()
