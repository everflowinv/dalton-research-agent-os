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
    CLASSIFICATION_SLOTS, SECTIONS, VARIANT_SLOTS, CompanyDossierAuthority,
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
from dalton_core.store import DaltonStore, content_hash
from dalton_core.writer_server import WriterServerError
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
                 answer_all=False, extra_number=None, break_contract=False,
                 refuse_groups=(), cite_aspect=None):
        self.route = route
        self.verdict = verdict
        self.findings = list(findings)
        self.sentence = sentence
        self.classification = classification or "contract_compounder"
        self.answer_all = answer_all
        self.extra_number = extra_number
        self.break_contract = break_contract
        self.refuse_groups = set(refuse_groups)
        # Which row to cite, by the source column the statements table prints.
        # ``None`` takes whatever came first, which on this fixture is a dossier
        # section body; naming an aspect makes the reply cite that section's
        # Claim instead, which is what a test about a retired Claim needs.
        self.cite_aspect = cite_aspect
        self.prompts: list[str] = []
        self.producer_route_decision_refs = ()

    def call(self, *, purpose, request_id, prompt, mission,
             producer_route_decision_refs=()):
        self.producer_route_decision_refs = tuple(producer_route_decision_refs)
        self.prompts.append(prompt)
        if prompt.startswith("You are an independent verifier"):
            return self._envelope(json.dumps(
                {"verdict": self.verdict, "findings": self.findings}))
        group = next(iter(re.findall(r"^Part: (\w+) --", prompt, flags=re.MULTILINE)),
                     None)
        if self.break_contract or group in self.refuse_groups:
            return self._envelope(json.dumps({"answers": []}))
        refs = re.findall(r"^  (q\d+)\t", prompt, flags=re.MULTILINE)
        tag = None
        if self.cite_aspect is not None:
            tag = next((row[0] for row in re.findall(
                r"^(C\d+)\t[^\t]*\t([^\t]*)\t", prompt, flags=re.MULTILINE)
                if row[1] == self.cite_aspect), None)
        if tag is None:
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
        if "debate_map" not in scopes:
            scopes.append("debate_map")
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
            # Drafted, which is what the eighth, ninth and eleventh questions
            # rest on: "what the market is paying for and where we think it is
            # wrong" already lives here, and a gate that re-derived it from the
            # same Claims would produce a second opinion nobody asked for.
            "variant_view": {
                "status": "drafted", "reason": None, "market_view_available": False,
                "market_view_reason": "没有卖方或共识材料",
                "structure": [slot for slot in VARIANT_SLOTS if slot != "market_view"],
                "slots": [{"slot_id": slot,
                           "sentences": [{"text": f"{slot} 的一句话。",
                                          "refs": [self.claims["c-variant"]]}]}
                          for slot in VARIANT_SLOTS if slot != "market_view"],
                "sources": [{"kind": "claim", "ref": self.claims["c-variant"],
                             "text": "我们与市场的分歧在这里", "period": None}],
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
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
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
            actor_ref=self.mission["autonomy"]["automation_principal"],
            created_at="2026-09-02T00:00:00+00:00",
        )

    def retire_claim(self, claim_version_ref):
        """Retire one Claim the way P10b does, so a citation stops resolving.

        Human judgement rather than a detector: the two deterministic reasons
        re-run their detector against the original, and a fixture that had to
        satisfy a detector would be testing the detector.
        """

        from dalton_core.claim_retirement import ClaimRetirementAuthority

        authority = ClaimRetirementAuthority(self.store)
        claim = self.store.connection.execute(
            "SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
            (claim_version_ref,),
        ).fetchone()
        challenge = authority.challenge(
            claim_version_ref=claim_version_ref,
            claim_version_hash=claim["content_hash"],
            reason_code="human_judgment",
            rationale="fixture: this Claim is retired between the two runs",
            actor_ref=OWNER,
        )
        return authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", rationale="fixture", actor_ref=OWNER,
        )

    def roll_mission_version(self):
        """Publish the next mission version, the way the live mission rolls."""

        from tests.p9a_fixtures import load_mission_manifest

        manifest = load_mission_manifest()
        params = {
            "title": self.mission["title"], "objective": self.mission["objective"],
            "industry_ref": self.mission["industry_ref"],
            "universe": self.mission["universe"],
            "research_questions": self.mission["research_questions"],
            "deliverables": self.mission["deliverables"],
            "source_plan": self.mission["source_plan"],
            "bindings": self.mission["bindings"],
            "autonomy": self.mission["autonomy"],
            "budget": self.mission["budget"],
            "actor_ref": OWNER,
            "version_id": f"{manifest['version_id']}-rolled",
            "prior_version_ref": self.mission["id"],
            "idempotency_key": f"{manifest['idempotency_key']}-rolled",
        }
        rolled = self.missions.create_mission(self.mission["mission_ref"], **params)
        self.mission = rolled
        return rolled

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

    def test_the_variant_view_reaches_the_questions_that_rest_on_it(self):
        # The plan makes the variant view a first-class field, and three of the
        # twelve questions are about the market rather than the company. It is
        # shown as the dossier's own block rather than re-derived, so the gate
        # and the file cannot end up with two opinions about the same week.
        for group in ("market", "thesis"):
            rows, _ = group_material(group=group, dossier=self.dossier,
                                     map_version=self.harness.map_version, numbers=[])
            with self.subTest(group=group):
                self.assertTrue(any(row["importance"] == "variant_view"
                                    for row in rows))

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
    def test_verifier_receives_every_group_draft_route(self):
        verifier = FakeModel(route="route:verify")
        summary = self.harness.run(verifier_model_factory=lambda: verifier)
        self.assertEqual(summary["gate_status"], "submitted")
        self.assertEqual(
            verifier.producer_route_decision_refs,
            ("route:draft", "route:draft", "route:draft", "route:draft"),
        )

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

    def move_the_file(self, name="c-new", aspect="guidance_style"):
        fresh = self.harness.fixture.add_claim(
            name, kind="qualitative", value=None, unit=None,
            statement=f"新的一条定性结论 {name}")["claim_version_id"]
        return self.harness.publish_dossier(extra={aspect: fresh})

    def test_a_redraft_carries_answers_that_cite_the_dossier_version_they_came_from(self):
        # B1: a carried answer cites ``dossier-section:<the version it was
        # written from>:<aspect>``, which is by definition not the version this
        # run read. Resolving against "the rows shown today" made every redraft
        # after a returned gate refuse itself -- after paying for four calls.
        self.decide("return_for_more_work", reason="第五、六问要重答")
        self.move_the_file()
        summary = self.harness.run(
            model_factory=lambda: FakeModel(refuse_groups={"company"}))
        self.assertEqual(summary["gate_status"], "submitted")
        self.assertEqual([row["group"] for row in summary["refused"]], ["company"])
        second = self.gates.gate(summary["version_ref"])
        first = self.gates.gate(self.first["version_ref"])
        carried = {row["question_ref"]: row for row in second["answers"]
                   if row["group"] == "company"}
        held = {row["question_ref"]: row for row in first["answers"]
                if row["group"] == "company"}
        self.assertEqual(carried, held)
        cited = {source["ref"] for row in carried.values()
                 for source in row["sources"]}
        self.assertTrue(any(ref.startswith("dossier-section:") for ref in cited))
        self.assertTrue(any(first["bindings"]["dossier_version_ref"] in ref
                            for ref in cited))
        self.assertEqual(summary["demoted_questions"], [])

    def redraft(self, **kwargs):
        """Return the head, move the file, and run again."""

        head = self.gates.latest(ACN)
        self.gates.decide(
            gate_version_ref=head["id"], gate_version_hash=head["content_hash"],
            decision="return_for_more_work", reason="再来一版", actor_ref=OWNER)
        self.move_the_file(name=f"c-{head['version']}",
                           aspect="guidance_style")
        return self.harness.run(**kwargs)

    def test_a_carried_answer_whose_evidence_is_gone_becomes_an_unknown(self):
        from dalton_core.deep_insight_gate import UNKNOWN_REASONS

        self.assertIn("source_unavailable", UNKNOWN_REASONS)
        # A second version whose answers rest on one company's Claim rather than
        # on the file's own prose, so that retiring the Claim breaks exactly the
        # answers that will be carried forward.
        second = self.redraft(
            model_factory=lambda: FakeModel(cite_aspect="competitive_position"))
        self.assertEqual(second["gate_status"], "submitted")
        self.harness.retire_claim(self.harness.claims["c-compete"])
        # The world moved between the two runs. That is not a bad draft: the
        # carried answer becomes an honest unknown and the lane redrafts its
        # group next time, rather than the chain freezing on it for ever.
        third = self.redraft(
            model_factory=lambda: FakeModel(refuse_groups={"company"}))
        self.assertEqual(third["gate_status"], "submitted")
        record = self.gates.gate(third["version_ref"])
        demoted = [row for row in record["answers"]
                   if row["status"] == "unknown"
                   and row["unknown"]["reason"] == "source_unavailable"]
        self.assertTrue(demoted)
        self.assertEqual(third["demoted_questions"],
                         [row["question_ref"] for row in demoted])
        self.assertTrue(third["demoted_refs"])

    def test_a_freshly_drafted_answer_whose_ref_is_gone_refuses_the_whole_run(self):
        # The dossier still names the Claim; the Ledger has retired it. Anything
        # drafted from it this run is a bad draft, and a bad draft is refused
        # whole rather than quietly demoted.
        self.harness.retire_claim(self.harness.claims["c-business"])
        summary = self.redraft(
            model_factory=lambda: FakeModel(cite_aspect="business_model"))
        self.assertEqual(summary["gate_status"], "unresolvable_refs")
        self.assertEqual(self.gates.latest(ACN)["version"], 1)

    def test_a_returned_gate_is_redrafted_once_the_file_moves(self):
        self.decide("return_for_more_work", reason="第七问没有价格材料")
        self.move_the_file()
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

    def test_a_crash_between_the_two_writes_heals_and_keeps_the_link(self):
        # The stage record goes first so that a crash between the two writes
        # leaves a passed stage the next call heals. A retry that healed the
        # ladder and then recorded a decision unable to name the row it
        # produced would have healed nothing worth having.
        server = self.server()
        mission_ref = self.draft["bindings"]["mission_version_ref"]
        written = self.on_store(server, lambda: self._crash_halfway(
            server, mission_ref))
        result = self.decide(server, "approve")
        self.assertEqual(result["stage_record_ref"], written)
        self.assertEqual(
            [row for row in self.stage_records(server)
             if row == ("deep_insight_gate", "gate_passed")],
            [("deep_insight_gate", "gate_passed")])

    def _crash_halfway(self, server, mission_ref):
        """Everything the operation does before it writes the decision."""

        mission = server.coverage_mission.mission(mission_ref)
        for status in ("entered", "gate_passed"):
            record = server.coverage_mission.record_stage(
                mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                company_ref=ACN, stage_ref="deep_insight_gate", status=status,
                evidence_refs=[self.draft["id"]],
                rationale=("P12d：深度认知门十二问草稿已提交，进入本阶段并由人裁决。"
                           if status == "entered" else "十二问够看了"),
                actor_ref=OWNER,
                idempotency_key=(f"deep-insight-gate:{self.draft['id']}:{status}"
                                 if status == "entered"
                                 else f"deep-insight-gate:{self.draft['id']}:approve"),
            )
        return record["id"]

    def test_a_rolled_mission_version_is_refused_readably_rather_than_crashing(self):
        # The stage ledger is scoped by version and the live mission rolls. A
        # draft published under version N can stop being decidable without
        # anybody touching it, and the owner should not learn that from a
        # traceback after clicking.
        self.harness.fixture = LedgerFixture(str(self.harness.state_dir / "core.sqlite"))
        self.harness.store = self.harness.fixture.store
        self.harness.missions = CoverageMissionAuthority(self.harness.store)
        self.harness.roll_mission_version()
        self.harness.fixture.close()
        server = self.server()
        with self.assertRaises(WriterServerError) as raised:
            self.decide(server, "approve")
        self.assertIn("研究目标版本", str(raised.exception))
        # And returning still works: it writes no stage record, so the ladder
        # has no opinion about it.
        returned = self.decide(server, "return_for_more_work", reason="先放着")
        self.assertIsNone(returned["stage_status"])

    def test_a_rolled_mission_version_shows_as_undecidable_before_the_click(self):
        self.harness.fixture = LedgerFixture(str(self.harness.state_dir / "core.sqlite"))
        self.harness.store = self.harness.fixture.store
        self.harness.missions = CoverageMissionAuthority(self.harness.store)
        self.harness.roll_mission_version()
        self.harness.fixture.close()
        server = self.server()
        listed = self.on_store(
            server, lambda: server._op_deep_insight_gate_submissions({}))["drafts"]
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["decidable"])
        self.assertEqual(listed[0]["undecidable_reason_code"], "mission_version_rolled")
        self.assertIn("研究目标版本", listed[0]["undecidable_reason"])

    def test_the_submissions_view_lists_the_draft_with_its_twelve_answers(self):
        server = self.server()
        listed = self.on_store(
            server, lambda: server._op_deep_insight_gate_submissions({}))["drafts"]
        self.assertEqual([row["version_ref"] for row in listed], [self.draft["id"]])
        self.assertEqual(len(listed[0]["answers"]), 12)
        self.assertTrue(listed[0]["decidable"])
        self.assertIsNone(listed[0]["undecidable_reason"])
        self.decide(server, "approve")
        self.assertEqual(
            self.on_store(
                server, lambda: server._op_deep_insight_gate_submissions({}))["drafts"],
            [])


class CockpitTests(unittest.TestCase):
    """The approvals page, run for real against a Core that holds a draft."""

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.summary = self.harness.run()
        self.assertEqual(self.summary["gate_status"], "submitted")
        self.draft = self.harness.gates().gate(self.summary["version_ref"])
        self.harness.fixture.close()

    def plane(self):
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane

        state = self.harness.state_dir
        config = CockpitConfig(
            core_db=state / "core.sqlite", state_dir=state,
            heartbeat_path=state / "heartbeat.json",
            scheduler_db=state / "scheduler.sqlite",
            journal_path=state / "cockpit.sqlite",
        )
        plane = CockpitPlane(config, writer_socket=state / "w.sock",
                             token_config=state / "t.json")
        self.addCleanup(plane.close)
        return plane

    def gate_item(self, plane):
        items = plane.approvals()["items"]
        return next(item for item in items if item["kind"] == "deep_insight_gate")

    def test_the_undecided_gate_is_listed_with_a_plain_title(self):
        item = self.gate_item(self.plane())
        self.assertEqual(item["title"], "深度认知门十二问：是否让这家公司进入完整覆盖")
        self.assertEqual(item["ref"], self.draft["id"])
        self.assertEqual(item["hash"], self.draft["content_hash"])
        self.assertEqual([action["decision"] for action in item["actions"]],
                         ["approve", "return_for_more_work", "reject"])
        self.assertTrue(item["needs_rationale"])

    def test_every_detail_value_is_something_the_page_can_render(self):
        # The details renderer joins an array with 、 and stringifies an object,
        # so a list of answer objects would arrive as twelve "[object Object]"
        # run together. Every value here is a string.
        item = self.gate_item(self.plane())
        for name, value in item["details"].items():
            with self.subTest(detail=name):
                self.assertIsInstance(value, (str, type(None)))
        for ref in QUESTION_REFS:
            self.assertIn(ref, item["details"])
        answered = [row for row in self.draft["answers"]
                    if row["status"] == "answered"]
        self.assertIn("把握", item["details"][answered[0]["question_ref"]])
        unknown = next(row for row in self.draft["answers"]
                       if row["status"] == "unknown")
        self.assertIn("能定它的证据", item["details"][unknown["question_ref"]])

    def test_a_rolled_mission_version_leaves_the_item_with_no_buttons(self):
        from dalton_core.deep_insight_gate import UNDECIDABLE_REASONS

        self.harness.fixture = LedgerFixture(str(self.harness.state_dir / "core.sqlite"))
        self.harness.store = self.harness.fixture.store
        self.harness.missions = CoverageMissionAuthority(self.harness.store)
        self.harness.roll_mission_version()
        self.harness.fixture.close()
        item = self.gate_item(self.plane())
        # Still on the page -- it is still what the owner has to deal with --
        # and saying why, because a button that goes nowhere is worse than an
        # item that says so.
        self.assertEqual(item["actions"], [])
        self.assertFalse(item["needs_rationale"])
        self.assertIn(UNDECIDABLE_REASONS["mission_version_rolled"], item["note"])

    def test_a_decided_gate_leaves_the_page(self):
        plane = self.plane()
        self.assertTrue(any(item["kind"] == "deep_insight_gate"
                            for item in plane.approvals()["items"]))
        gates = DeepInsightGateAuthority(DaltonStore(
            str(self.harness.state_dir / "core.sqlite")))
        self.addCleanup(gates.store.close)
        gates.decide(gate_version_ref=self.draft["id"],
                     gate_version_hash=self.draft["content_hash"],
                     decision="approve", reason="十二问够看了", actor_ref=OWNER)
        self.assertFalse(any(item["kind"] == "deep_insight_gate"
                             for item in plane.approvals()["items"]))


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
            # Still off: without a separate verifier configuration the child
            # holds every tick without drafting, and a lane switched on to
            # report the same refusal for ever is worse than one that is off.
            self.assertEqual(LANE.argv_fragment(context), [])
            (state / "dossier-verifier-model-config.json").write_text("{}")
            argv = LANE.argv_fragment(context)
            for flag in ("--deep-insight-gate-model-config",
                         "--deep-insight-gate-policy",
                         "--deep-insight-gate-verifier-model-config"):
                self.assertIn(flag, argv)

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
                        "summary": {"gate_status": self.gate_status}}

        harness = Harness()
        self.addCleanup(harness.close)
        for gate_status in ("no_eligible_company",):
            with self.subTest(gate_status=gate_status):
                launcher = Launcher()
                launcher.gate_status = gate_status
                coordinator = MissionDeepInsightLaneCoordinator(
                    connection=harness.store.connection, launcher=launcher)
                launched = coordinator.dispatch_once()
                self.assertEqual(launched["status"], "launched")
                launcher.signature = launched["signature"]
                settled = coordinator.dispatch_once()
                # A refusal is as good a reason to stay quiet as an idle tick:
                # the same evidence will be refused the same way, and relaunching
                # would re-pay for four model calls to learn it again.
                self.assertEqual(settled["status"], "idle")
                self.assertEqual(launcher.started, 1)

    def test_content_refusals_are_terminal_for_the_unchanged_signature(self):
        class Launcher:
            def __init__(self, gate_status):
                self.gate_status = gate_status
                self.started = 0

            def start(self, *, signature, company_ref=None):
                self.started += 1
                self.signature = signature
                return {"id": f"ticket-{self.started}", "signature": signature}

            def status(self, ticket_ref):
                return {"status": "succeeded", "signature": self.signature,
                        "summary": {"gate_status": self.gate_status}}

        harness = Harness()
        self.addCleanup(harness.close)
        for gate_status in ("classification_conflict", "verification_failed",
                            "rubric_refused"):
            with self.subTest(gate_status=gate_status):
                launcher = Launcher(gate_status)
                coordinator = MissionDeepInsightLaneCoordinator(
                    connection=harness.store.connection, launcher=launcher)
                coordinator.dispatch_once()
                self.assertEqual(coordinator.dispatch_once()["status"], "terminal")
                self.assertEqual(launcher.started, 1)

    def test_a_submission_is_the_one_reason_to_look_again(self):
        from dalton_core.mission_deep_insight_lane import RELAUNCH_STATUSES

        self.assertEqual(RELAUNCH_STATUSES, frozenset({"submitted", "duplicate"}))

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
