"""P12a: the child checks the grant before it spends, and refuses to publish alone.

The child's shape is the Claim-index and model-specification children's: check
the mission's ``may_write`` before spending anything, derive the work from the
Ledger rather than draining a queue, and report ``idle`` when there is nothing
to do instead of relaunching itself.

What is different, and what these tests are mostly about, is the gate in front
of ``publish``. A draft becomes a version only if an independent verifier
passed it, Q1's hard checks passed it, the Constitution's ``output_rubric``
found nothing, every ref resolves, and ADR-0008 finds a ref the current version
does not have. Five ways to be refused, and each of them leaves the chain
exactly as it was.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.claim_index_authority import ClaimIndexAuthority
from dalton_core.company_dossier import (
    VARIANT_SLOTS, CompanyDossierAuthority, causal_chain_hash, validate_policy,
)
from dalton_core.company_dossier_cli import (
    build_parser, claim_material, dossier_freshness, granted_scope,
    reconstruct_dossier_input, run_dossier,
    screened_companies, stale_units,
)
from dalton_core.company_dossier_launcher import CompanyDossierLauncher, run_digest
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_dossier_lane import (
    LANE, LAUNCHER_KWARG, MissionDossierLaneCoordinator, build_launcher,
    company_ledger_signature, dispatch, ledger_signature,
)
from dalton_core.store import content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_claim_index_entries import ACN, LedgerFixture, entry_args

AUTOMATION = "automation:coverage-mission"
CHAIN = ["Bookings lead revenue by two to four quarters."]


def policy_document(*, map_chain=True):
    maps = []
    if map_chain:
        maps.append({
            "constitution_ref": "constitution:us-it-services",
            "causal_chain_hash": causal_chain_hash(CHAIN),
            "sections": ["demand_drivers"],
            "note": "the fixture constitution's single link",
        })
    return validate_policy({
        "schema_version": "0.1",
        "policy_ref": "dossier-policy:test:v1",
        "causal_chain_maps": maps,
        "output_rubric_bindings": [{
            "criterion_hash": content_hash("State what changed and its impact."),
            "check": "not_a_restatement", "reason": "",
        }],
    })


class FakeModel:
    """Answers whatever the prompt asked for, in the shape the contract wants.

    It reads the slot ids out of the prompt rather than being told them, so a
    drafter that stopped printing its structure would fail these tests rather
    than pass them by agreement.
    """

    def __init__(self, *, route="route:draft", verdict="pass", findings=(),
                 sentence="这一节的判断由所引材料支撑。", extra_number=None):
        self.route = route
        self.verdict = verdict
        self.findings = list(findings)
        self.sentence = sentence
        self.extra_number = extra_number
        self.prompts: list[str] = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        if prompt.startswith("You are an independent verifier"):
            text = json.dumps({"verdict": self.verdict, "findings": self.findings})
            return self._envelope(text)
        slots = re.findall(r"^  (\S+)\t", prompt, flags=re.MULTILINE)
        tag = "C1" if "\nC1\t" in prompt else "N1"
        body = self.sentence
        if self.extra_number is not None:
            body = f"{self.sentence}规模约为 {self.extra_number}。"
        payload = {
            "slots": [{"slot_id": slots[0],
                       "sentences": [{"text": body, "refs": [tag]}]}]
            + [{"slot_id": slot, "unknown": "材料没有回答这一点"} for slot in slots[1:]],
            "gaps": [],
        }
        if "Part: industry_classification" in prompt:
            payload["classification"] = "contract_compounder"
        return self._envelope(json.dumps(payload, ensure_ascii=False))

    def _envelope(self, text):
        return {"text": text, "replayed": False, "cost_micros": 1000,
                "work_order_ref": f"work:cockpit-dossier-{len(self.prompts)}",
                "route_decision_ref": self.route}


FAMILIES = {"route:draft": "family-a", "route:verify": "family-b"}


def resolver(ref):
    return FAMILIES.get(ref)


class Harness:
    def __init__(self, *, may_write=None, screened=True, index=True, map_chain=True):
        self._dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._dir.name)
        self.fixture = LedgerFixture(str(self.state_dir / "core.sqlite"))
        self.store = self.fixture.store
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        scopes = list(params["autonomy"]["may_write"]) if may_write is None else list(may_write)
        if may_write is None and "dossier" not in scopes:
            scopes.append("dossier")
        params["autonomy"] = {**params["autonomy"], "may_write": scopes}
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        if screened:
            self.pass_screen()
        self.index = ClaimIndexAuthority(self.store) if index else None
        if index:
            self.add_material()
        self.policy_path = self.state_dir / "policy.json"
        self.policy_path.write_text(
            json.dumps(policy_document(map_chain=map_chain)), encoding="utf-8")
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

    def tag(self, ref, aspect, *, statement, metric="demand environment",
            kind="qualitative", value=None, unit=None, group=None,
            period="2026-03-01..2026-05-31", importance="filing"):
        claim = self.fixture.add_claim(
            ref, kind=kind, value=value, unit=unit, metric=metric,
            statement=statement, period=period)
        self.index.record_entry(**entry_args(
            claim, aspect=aspect, metric_or_aspect=metric, period_key=period,
            dedupe_group_key=group or f"qual|{ACN}|{ref}",
            claim_kind=kind, importance=importance,
            importance_basis="document_spec:sec-10q"))
        return claim

    def add_market_view(self):
        """Sell-side material, which is what a variant view is allowed to rest on.

        The fixture had none, so every run left the variant view
        ``unavailable`` -- which is why nothing here ever published a drafted
        one, and why a validator that dropped a field from that shape went
        unnoticed for a whole review cycle.
        """

        return self.tag("m-1", "history_of_price_drivers",
                        statement="卖方在报告里把估值倍数的回落归因于联邦支出。",
                        metric="street view", importance="sell_side")

    def add_guidance_pair(self, period="2026-03-01..2026-05-31", actual=8.0):
        """A guide and the settled number that answered it, both as Claims."""

        self.tag("g-%s" % period, "guidance_style",
                 statement=("管理层指引本季 revenue growth of 5% to 7%。"),
                 metric="revenue growth guidance", period=period)
        self.tag("a-%s" % period, "segments_and_mix",
                 statement=("Revenue growth for the quarter was %s percent." % actual),
                 metric="quarterly_revenue_yoy_growth", kind="quantitative",
                 value=actual, unit="percent", period=period)

    def add_material(self):
        self.tag("d-1", "business_model",
                 statement="公司通过咨询与外包两类合同赚钱。")
        self.tag("d-2", "segments_and_mix",
                 statement="北美占收入的一半以上，且占比在上升。")
        self.tag("d-3", "demand_drivers",
                 statement="管理层说预订量在本季转正。")
        self.tag("d-4", "guidance_style",
                 statement="管理层给出 revenue growth of 5% to 7% 的指引。")

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
        return run_dossier(**options)

    def close(self):
        self.fixture.close()
        self._dir.cleanup()


class GrantTests(unittest.TestCase):
    def test_a_mission_that_has_not_granted_dossier_spends_nothing(self):
        harness = Harness(may_write=["claim", "deliverable", "stage_record",
                                     "observation", "research_question"])
        self.addCleanup(harness.close)
        summary = harness.run()
        self.assertEqual((summary["status"], summary["dossier_status"]),
                         ("held", "not_authorized"))
        self.assertIsNone(summary["version_ref"])

    def test_the_word_is_dossier_and_there_is_no_fallback(self):
        self.assertEqual(granted_scope({"autonomy": {"may_write": ["dossier"]}}),
                         "dossier")
        self.assertIsNone(granted_scope({"autonomy": {"may_write": ["deliverable"]}}))

    def test_a_core_with_no_claim_index_is_held_rather_than_empty(self):
        harness = Harness(index=False)
        self.addCleanup(harness.close)
        summary = harness.run()
        self.assertEqual(summary["dossier_status"], "no_claim_index")

    def test_an_installation_without_the_policy_is_held_not_crashed(self):
        harness = Harness()
        self.addCleanup(harness.close)
        summary = harness.run(policy_path=harness.state_dir / "absent.json")
        self.assertEqual((summary["status"], summary["dossier_status"]),
                         ("held", "no_policy"))

    def test_a_company_that_has_not_passed_its_screen_has_no_file_to_deepen(self):
        harness = Harness(screened=False)
        self.addCleanup(harness.close)
        self.assertEqual(screened_companies(harness.missions, harness.mission), [])
        summary = harness.run()
        self.assertEqual(summary["dossier_status"], "no_screened_company")

    def test_a_screen_that_passed_under_an_earlier_mission_version_still_counts(self):
        # P14-S: the gate is a fact about the mission, not about the version
        # it was recorded under. Read per-version, publishing v14 would stop
        # every dossier in the mission until somebody re-screened.
        harness = Harness()
        self.addCleanup(harness.close)
        self.assertEqual(screened_companies(harness.missions, harness.mission), [ACN])
        params = dict(mission_params(bootstrap_method_authorities(harness.store)))
        params["autonomy"] = dict(harness.mission["autonomy"])
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": harness.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2",
                       "title": "v2"})
        rolled = harness.missions.create_mission(params.pop("mission_ref"), **params)
        self.assertEqual(harness.missions.stage_records(rolled["id"]), [])
        self.assertEqual(screened_companies(harness.missions, rolled), [ACN])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def test_a_dry_run_plans_and_writes_nothing(self):
        summary = self.harness.run(dry_run=True)
        self.assertEqual(summary["dossier_status"], "dry_run")
        self.assertIsNone(summary["version_ref"])
        self.assertEqual(
            CompanyDossierAuthority(self.harness.store).versions(ACN), [])

    def test_the_sections_nobody_can_write_say_which_authority_is_missing(self):
        planned = self.harness.run(dry_run=True)["units_planned"]
        self.assertEqual(planned["catalyst_calendar"]["reason"],
                         "no_catalyst_calendar_authority")
        self.assertEqual(planned["history_of_price_drivers"]["reason"],
                         "no_market_data")
        self.assertEqual(planned["kpi_dictionary"]["reason"], "no_canonical_claims")

    def test_an_unmapped_causal_chain_makes_its_two_sections_unavailable(self):
        harness = Harness(map_chain=False)
        self.addCleanup(harness.close)
        planned = harness.run(dry_run=True)["units_planned"]
        for aspect in ("demand_drivers", "supply_and_cost"):
            self.assertEqual(planned[aspect]["reason"], "causal_chain_unmapped")

    def test_a_mapped_chain_gives_demand_drivers_one_slot_per_link(self):
        planned = self.harness.run(dry_run=True)["units_planned"]
        self.assertEqual(planned["demand_drivers"]["status"], "ready")
        self.assertEqual(planned["demand_drivers"]["slots"], len(CHAIN))

    def test_stale_units_prefer_what_nobody_has_drafted(self):
        plan = {
            "business_model": {"unit": "business_model", "status": "ready",
                               "new_refs": 1, "last_drafted": 3, "stale": True,
                               "structure": []},
            "segments_and_mix": {"unit": "segments_and_mix", "status": "ready",
                                 "new_refs": 1, "last_drafted": None, "stale": True,
                                 "structure": []},
            "kpi_dictionary": {"unit": "kpi_dictionary", "status": "ready",
                               "new_refs": 0, "last_drafted": None, "stale": True,
                               "structure": []},
        }
        self.assertEqual(stale_units(plan, limit=2),
                         ["segments_and_mix", "business_model"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.authority = CompanyDossierAuthority(self.harness.store)

    def test_a_run_publishes_one_version_with_the_rest_unavailable(self):
        summary = self.harness.run()
        self.assertEqual(summary["dossier_status"], "published")
        record = self.authority.latest(ACN)
        self.assertEqual(record["version"], 1)
        drafted = [item["aspect"] for item in record["sections"]
                   if item["status"] == "drafted"]
        self.assertEqual(drafted, sorted(set(drafted)))
        self.assertTrue(drafted)
        unavailable = {item["aspect"]: item["reason"] for item in record["sections"]
                       if item["status"] == "unavailable"}
        self.assertEqual(unavailable["catalyst_calendar"],
                         "no_catalyst_calendar_authority")
        self.assertEqual(record["change_reason"], "evidence_thicker")
        self.assertTrue(record["evidence_refs"])

    def test_a_real_new_version_reconstructs_fresh_then_uncited_input_is_stale(self):
        first = self.harness.run(max_units=12)
        record = self.authority.latest(ACN)
        policy = json.loads(self.harness.policy_path.read_text(encoding="utf-8"))
        self.assertEqual(dossier_freshness(
            self.harness.store.connection, record, self.harness.mission, policy), "fresh")
        self.assertEqual(first["input_freshness"], "fresh")

        self.harness.tag("d-new", "business_model",
                         statement="This uncited row still changes the next producer prompt.")
        self.assertEqual(dossier_freshness(
            self.harness.store.connection, record, self.harness.mission, policy), "stale")

    def test_first_partial_dossier_with_ready_undrafted_units_is_not_fresh(self):
        summary = self.harness.run(max_units=3)
        self.assertEqual(summary["dossier_status"], "published")
        self.assertEqual(summary["input_freshness"], "unknown")

    def test_reconstruction_is_select_only_on_a_read_only_database(self):
        self.harness.run(max_units=3)
        record = self.authority.latest(ACN)
        policy = json.loads(self.harness.policy_path.read_text(encoding="utf-8"))
        connection = sqlite3.connect(
            f"file:{self.harness.state_dir / 'core.sqlite'}?mode=ro", uri=True)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        denied = []
        write_ops = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                     sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                     sqlite3.SQLITE_DROP_TABLE}
        def authorizer(action, arg1, arg2, database, trigger):
            if action in write_ops:
                denied.append((action, arg1))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorizer)
        rebuilt = reconstruct_dossier_input(
            connection, record, self.harness.mission, policy)
        self.assertEqual(set(rebuilt), set(record["input_fingerprints"]))
        self.assertEqual(denied, [])

    def test_the_guidance_section_carries_the_computed_table_not_a_verdict(self):
        summary = self.harness.run(max_units=12)
        self.assertEqual(summary["dossier_status"], "published")
        record = self.authority.latest(ACN)
        section = next(item for item in record["sections"]
                       if item["aspect"] == "guidance_style")
        self.assertIsNotNone(section["profile"])
        self.assertEqual(section["profile"]["classification"], "insufficient_data")
        self.assertEqual(section["profile"]["events"][0]["guide"]["low"], "5")

    def test_the_units_a_bounded_tick_left_out_are_drafted_on_the_next_one(self):
        # Five units have material and a tick drafts three. Against the head of
        # the chain the two left over would look current the moment the first
        # version landed -- nothing has arrived since -- and the file would
        # stop at three sections for ever. Staleness is per unit: a unit
        # nobody has written is stale whatever else was published.
        first = self.harness.run(max_units=3)
        self.assertEqual(first["dossier_status"], "published")
        self.assertEqual(len(first["units_drafted"]), 3)
        second = self.harness.run(max_units=3)
        self.assertEqual(second["dossier_status"], "published")
        self.assertEqual(len(second["units_drafted"]), 2)
        self.assertFalse(set(first["units_drafted"]) & set(second["units_drafted"]))
        policy = json.loads(self.harness.policy_path.read_text(encoding="utf-8"))
        self.assertEqual(dossier_freshness(
            self.harness.store.connection, self.authority.latest(ACN),
            self.harness.mission, policy), "unknown")
        third = self.harness.run(max_units=3)
        self.assertEqual(third["dossier_status"], "nothing_new")
        record = self.authority.latest(ACN)
        self.assertEqual(record["version"], 2)
        self.assertEqual(sorted(record["drafted_at"]),
                         sorted(first["units_drafted"] + second["units_drafted"]))
        self.assertEqual(
            len([item for item in record["sections"] if item["status"] == "drafted"]),
            4)

    def test_a_carried_forward_unit_keeps_the_time_it_was_written(self):
        self.harness.run(max_units=3)
        stamps = self.authority.latest(ACN)["drafted_at"]
        self.harness.run(max_units=3)
        after = self.authority.latest(ACN)["drafted_at"]
        for unit, when in stamps.items():
            self.assertEqual(after[unit], when, unit)

    def test_the_classification_is_one_of_the_gates_five_words(self):
        self.harness.run(max_units=12)
        record = self.authority.latest(ACN)
        self.assertEqual(record["industry_classification"]["classification"],
                         "contract_compounder")
        self.assertTrue(record["industry_classification"]["sources"])

    def test_a_section_is_stale_when_a_claim_arrives_not_when_one_is_uncited(self):
        # Two Claims for one aspect and a drafter that cites one of them. The
        # section has not left the other one undone, and redrafting it every
        # tick would spend a call to publish a duplicate.
        self.harness.tag("d-6", "business_model",
                         statement="另一条关于商业模式的一手材料。")
        first = self.harness.run(max_units=12)
        self.assertEqual(first["dossier_status"], "published")
        planned = self.harness.run(dry_run=True)["units_planned"]
        self.assertGreater(planned["business_model"]["new_refs"], 0)
        self.assertFalse(planned["business_model"]["stale"])
        again = self.harness.run(max_units=12)
        self.assertEqual(again["dossier_status"], "nothing_new")

    def test_a_second_run_with_nothing_new_is_idle(self):
        self.harness.run(max_units=12)
        again = self.harness.run(max_units=12)
        self.assertEqual(again["dossier_status"], "nothing_new")
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_new_claim_gives_the_next_version_something_to_cite(self):
        self.harness.run(max_units=12)
        self.harness.tag("d-5", "competitive_position",
                         statement="公司在两个客户群里替换了原有供应商。")
        again = self.harness.run(max_units=12)
        self.assertEqual(again["dossier_status"], "published")
        self.assertEqual(self.authority.latest(ACN)["version"], 2)
        self.assertGreaterEqual(again["new_refs"], 1)

    def test_an_explicit_revise_request_redrafts_a_part_that_is_not_stale(self):
        self.harness.run(max_units=12)
        self.assertEqual(self.harness.run(max_units=12)["dossier_status"],
                         "nothing_new")
        asked = self.harness.run(max_units=12, revise_units=("business_model",))
        # Nothing new arrived, so the redraft cites what the current version
        # already cites and it is refused before a record is even assembled.
        # The request is honoured; ADR-0008 is not suspended by it.
        self.assertEqual(asked["units_drafted"], ["business_model"])
        self.assertEqual(asked["dossier_status"], "no_new_evidence")
        self.assertIsNone(asked["version_ref"])
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_verifier_on_the_drafters_family_publishes_nothing(self):
        summary = self.harness.run(
            verifier_model_factory=lambda: FakeModel(route="route:draft"))
        self.assertEqual(summary["dossier_status"], "not_independent")
        self.assertFalse(summary["verification"]["independent"])
        self.assertEqual(self.authority.versions(ACN), [])

    def test_an_unresolvable_verifier_family_also_publishes_nothing(self):
        summary = self.harness.run(
            verifier_model_factory=lambda: FakeModel(route="route:unknown"))
        self.assertEqual(summary["dossier_status"], "not_independent")
        self.assertEqual(self.authority.versions(ACN), [])

    def test_a_rejecting_verifier_publishes_nothing(self):
        summary = self.harness.run(
            verifier_model_factory=lambda: FakeModel(
                route="route:verify", verdict="reject",
                findings=[{"unit": "business_model", "code": "unsupported_sentence",
                           "detail": "这句话超出了它引用的材料"}]))
        self.assertEqual(summary["dossier_status"], "verification_failed")
        self.assertEqual(summary["status"], "failed")
        self.assertIn("unsupported_sentence", summary["failure_reason"])
        self.assertEqual(summary["verification"]["verdict"], "reject")
        self.assertEqual(self.authority.versions(ACN), [])

    def test_all_refused_drafts_are_a_failed_run_with_no_formal_write(self):
        class RefusingModel(FakeModel):
            def call(self, **kwargs):
                self.prompts.append(kwargs["prompt"])
                return self._envelope("not json")

        summary = self.harness.run(model_factory=RefusingModel, max_units=2)
        self.assertEqual((summary["status"], summary["dossier_status"]),
                         ("failed", "rubric_refused"))
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertIn("all attempted dossier units", summary["failure_reason"])

    def test_one_refused_unit_does_not_hide_a_published_partial_success(self):
        class FirstRefusedModel(FakeModel):
            def call(self, **kwargs):
                if not self.prompts:
                    self.prompts.append(kwargs["prompt"])
                    return self._envelope("not json")
                return super().call(**kwargs)

        summary = self.harness.run(model_factory=FirstRefusedModel, max_units=12)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["dossier_status"], "published")
        self.assertTrue(summary["refused"])
        self.assertTrue(summary["units_drafted"])

    def test_a_number_the_material_does_not_carry_is_refused_before_publish(self):
        summary = self.harness.run(
            model_factory=lambda: FakeModel(extra_number="45.6 亿美元"))
        self.assertEqual(summary["dossier_status"], "rubric_refused")
        self.assertIn("numbers_without_refs", summary["rubric"]["hard_failed"])
        self.assertEqual(self.authority.versions(ACN), [])

    def test_the_run_writes_a_summary_a_parent_can_read(self):
        self.harness.run()
        summary = json.loads(
            (self.harness.state_dir / "summary" / "summary.json").read_text(
                encoding="utf-8"))
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertEqual(summary["write_scope"], "dossier")
        self.assertTrue(summary["units_drafted"])

    def test_without_a_model_the_run_plans_and_reports_gated(self):
        summary = self.harness.run(model_config_path=None)
        self.assertEqual(summary["dossier_status"], "gated")
        self.assertEqual(self.authority.versions(ACN), [])


class GuidanceThroughTheCoreTests(unittest.TestCase):
    """P12f end to end: the rows the Core hands over must actually settle.

    The first version of this path built its actuals with a null value and a
    span for a period while the guides carried a Claim's own period key, so
    the join could not match and every event was ``unknown``. A profile that
    can never settle an event is a profile that can only ever say
    ``insufficient_data``, which reads exactly like an honest answer.
    """

    QUARTERS = (("2025-03-01..2025-05-31", 8.0), ("2025-06-01..2025-08-31", 9.0),
                ("2025-09-01..2025-11-30", 4.0), ("2025-12-01..2026-02-28", 6.0))

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def profile(self):
        from dalton_core.company_dossier_cli import guidance_material
        from dalton_core.guidance_profile import build_profile

        guides, actuals = guidance_material(self.harness.store, ACN)
        return build_profile(company_ref=ACN, guides=guides, actuals=actuals)

    def test_a_guide_and_a_quantitative_claim_settle_into_beat_miss_inline(self):
        for period, actual in self.QUARTERS:
            self.harness.add_guidance_pair(period=period, actual=actual)
        profile = self.profile()
        settled = [event["deviation"]["verdict"] for event in profile["events"]
                   if event["actual"] is not None]
        self.assertEqual(settled, ["beat", "beat", "miss", "inline"])
        self.assertEqual(profile["settled"], 4)
        self.assertEqual(profile["classification"], "mixed")

    def test_beating_every_quarter_is_read_as_conservative(self):
        for period, _ in self.QUARTERS:
            self.harness.add_guidance_pair(period=period, actual=9.0)
        self.assertEqual(self.profile()["classification"], "conservative")

    def test_a_guide_with_no_settled_number_stays_unknown_rather_than_dropped(self):
        profile = self.profile()
        self.assertEqual([event["deviation"]["verdict"] for event in profile["events"]],
                         ["unknown"])
        self.assertEqual(profile["classification"], "insufficient_data")

    def test_the_published_section_carries_the_settled_table(self):
        for period, actual in self.QUARTERS:
            self.harness.add_guidance_pair(period=period, actual=actual)
        summary = self.harness.run(max_units=12)
        self.assertEqual(summary["dossier_status"], "published")
        record = CompanyDossierAuthority(self.harness.store).latest(ACN)
        section = next(item for item in record["sections"]
                       if item["aspect"] == "guidance_style")
        self.assertEqual(section["profile"]["classification"], "mixed")
        self.assertEqual(section["profile"]["settled"], 4)


class VariantViewLaneTests(unittest.TestCase):
    """The variant view, drafted and published through the whole path."""

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.harness.add_market_view()
        self.authority = CompanyDossierAuthority(self.harness.store)

    def test_sell_side_material_gets_the_variant_view_drafted_and_published(self):
        summary = self.harness.run(max_units=12)
        self.assertEqual(summary["dossier_status"], "published")
        self.assertIn("variant_view", summary["units_drafted"])
        record = self.authority.latest(ACN)
        block = record["variant_view"]
        self.assertEqual(block["status"], "drafted")
        self.assertTrue(block["market_view_available"])
        self.assertEqual([slot["slot_id"] for slot in block["slots"]],
                         list(VARIANT_SLOTS))
        # And it reads back through the same door a reader would use.
        self.assertEqual(self.authority.dossier(record["id"])["variant_view"], block)

    def test_the_variant_view_is_planned_as_ready_once_the_street_is_in_the_ledger(self):
        planned = self.harness.run(dry_run=True)["units_planned"]
        self.assertEqual(planned["variant_view"]["status"], "ready")
        self.assertEqual(planned["variant_view"]["slots"], len(VARIANT_SLOTS))


class GateTests(unittest.TestCase):
    """The five ways a draft fails to become a version, and one that no longer is."""

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.authority = CompanyDossierAuthority(self.harness.store)

    def retire(self, claim_ref):
        from dalton_core.claim_retirement import ClaimRetirementAuthority

        row = self.harness.store.connection.execute(
            "SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
            (claim_ref,),
        ).fetchone()
        authority = ClaimRetirementAuthority(self.harness.store)
        challenge = authority.challenge(
            claim_version_ref=claim_ref, claim_version_hash=row["content_hash"],
            reason_code="human_judgment", rationale="fixture",
            actor_ref="human:coverage-owner")
        authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref="human:coverage-owner",
            rationale="fixture")

    def test_retired_canonical_claim_is_removed_before_prompt_material_is_bounded(self):
        live = self.harness.tag(
            "d-live", "business_model",
            statement="The company retained its live recurring-revenue contract.")
        retired = self.harness.tag(
            "d-retired", "business_model",
            statement="This canonical row was later retired.", importance="filing")
        self.retire(retired["claim_version_id"])

        rows = claim_material(self.harness.store, ACN, "business_model")

        refs = {row["ref"] for row in rows}
        self.assertIn(live["claim_version_id"], refs)
        self.assertNotIn(retired["claim_version_id"], refs)

    def test_retired_rows_cannot_crowd_a_live_row_out_of_the_query_bound(self):
        live = self.harness.tag(
            "z-live", "competitive_position",
            statement="The live row sorts after the retired rows.")
        for number in range(4):
            claim = self.harness.tag(
                f"a-retired-{number}", "competitive_position",
                statement=f"Retired canonical row {number}.")
            self.retire(claim["claim_version_id"])

        rows = claim_material(
            self.harness.store, ACN, "competitive_position", limit=1)

        self.assertEqual([row["ref"] for row in rows],
                         [live["claim_version_id"]])

    def test_all_retired_canonical_claims_are_idle_without_a_model_call(self):
        refs = [row[0] for row in self.harness.store.connection.execute(
            "SELECT claim_version_id FROM claim_versions"
        ).fetchall()]
        for ref in refs:
            self.retire(ref)
        producer = FakeModel()
        verifier = FakeModel(route="route:verify")

        summary = self.harness.run(
            model_factory=lambda: producer,
            verifier_model_factory=lambda: verifier)

        self.assertEqual((summary["status"], summary["dossier_status"]),
                         ("idle", "nothing_new"))
        self.assertEqual(producer.prompts, [])
        self.assertEqual(verifier.prompts, [])
        self.assertEqual(summary["cost_micros"], 0)

    def test_without_a_verifier_configuration_nothing_is_drafted_at_all(self):
        # One configuration routes both calls the same way, so the verdict
        # could never be shown to be independent. Refusing after twelve
        # drafting calls would be the same answer at twelve times the price.
        drafter = FakeModel()
        summary = self.harness.run(model_factory=lambda: drafter,
                                   verifier_model_factory=None)
        self.assertEqual((summary["status"], summary["dossier_status"]),
                         ("held", "no_verifier"))
        self.assertEqual(drafter.prompts, [])
        self.assertEqual(summary["cost_micros"], 0)

    def test_an_unresolvable_drafting_family_skips_the_verifier_call(self):
        verifier = FakeModel(route="route:verify")
        summary = self.harness.run(
            model_factory=lambda: FakeModel(route="route:unknown"),
            verifier_model_factory=lambda: verifier)
        self.assertEqual(summary["dossier_status"], "not_independent")
        self.assertEqual(summary["verification"]["status"], "skipped")
        self.assertEqual(verifier.prompts, [])

    def test_a_run_that_cannot_afford_the_verifier_publishes_nothing(self):
        config = json.loads(self.harness.model_config.read_text(encoding="utf-8"))
        config["purpose_run_budgets"] = {"dossier": {"max_cost_usd": 0.001}}
        self.harness.model_config.write_text(json.dumps(config), encoding="utf-8")
        summary = self.harness.run()
        self.assertEqual(summary["dossier_status"], "unverified")
        self.assertIn("run cost bound", summary["refused"][0]["reason"])
        self.assertEqual(self.authority.versions(ACN), [])

    def test_explicit_max_units_overrides_the_configured_run_default(self):
        config = json.loads(self.harness.model_config.read_text(encoding="utf-8"))
        config["purpose_run_budgets"] = {"dossier": {"max_units": 3}}
        self.harness.model_config.write_text(json.dumps(config), encoding="utf-8")
        summary = self.harness.run(max_units=1)
        self.assertEqual(len(summary["units_drafted"]), 1)

    def test_a_carried_section_whose_claim_was_retired_is_dropped_not_deadlocked(self):
        from dalton_core.claim_retirement import ClaimRetirementAuthority

        self.harness.run(max_units=12)
        record = self.authority.latest(ACN)
        section = next(item for item in record["sections"]
                       if item["aspect"] == "business_model")
        cited = section["sources"][0]["ref"]
        row = self.harness.store.connection.execute(
            "SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
            (cited,)).fetchone()
        retirement = ClaimRetirementAuthority(self.harness.store)
        challenge = retirement.challenge(
            claim_version_ref=cited, claim_version_hash=row["content_hash"],
            reason_code="human_judgment", rationale="fixture",
            actor_ref="human:coverage-owner")
        retirement.decide(challenge_ref=challenge["id"],
                          challenge_hash=challenge["content_hash"],
                          decision="retired", actor_ref="human:coverage-owner",
                          rationale="fixture")
        # Something new to write, so the run has a reason to exist at all.
        self.harness.tag("d-7", "competitive_position",
                         statement="公司在两个客户群里替换了原有供应商。")
        summary = self.harness.run(max_units=12)
        # The chain moves. Refusing the version because a section written last
        # week rests on a Claim retired since would freeze the file for ever on
        # the one section nobody can fix without publishing.
        self.assertEqual(summary["dossier_status"], "published")
        # The classification drew on the same Claim, so it goes too: a ref
        # that stopped resolving is a defect in every part that cites it.
        self.assertEqual(summary["dropped_units"],
                         ["business_model", "industry_classification"])
        after = self.authority.latest(ACN)
        dropped = next(item for item in after["sections"]
                       if item["aspect"] == "business_model")
        self.assertEqual((dropped["status"], dropped["reason"]),
                         ("unavailable", "refused_by_verification"))
        # And the old version still says what it said.
        before = self.authority.versions(ACN)[0]
        kept = next(item for item in before["sections"]
                    if item["aspect"] == "business_model")
        self.assertEqual(kept["status"], "drafted")

    def test_a_freshly_drafted_unresolvable_ref_still_refuses_the_run(self):
        from dalton_core.company_dossier_cli import unresolved_refs

        record = {"sections": [{"aspect": "business_model", "sources": [
            {"kind": "claim", "ref": "claim-version:nope", "text": "x", "period": None}]}]}
        missing = unresolved_refs(self.harness.store.connection, record,
                                  forecast_cells=set())
        self.assertEqual(missing, [{"ref": "claim-version:nope",
                                    "reason": "no such claim version"}])

    def test_a_new_figure_ref_counts_as_new_evidence_for_the_hard_check(self):
        # Q1's check reads Claim refs; a dossier also rests on filed lines and
        # forecast cells. Where the two disagree the broader rule wins, and the
        # summary records that it did.
        from dalton_core.company_dossier_cli import rubric_gate

        prior = {"sections": [{"aspect": "business_model", "status": "drafted",
                               "reason": None, "structure": ["business_model"],
                               "slots": [{"slot_id": "business_model", "sentences": [
                                   {"text": "上一版。", "refs": ["claim-version:a"]}]}],
                               "sources": [{"kind": "claim", "ref": "claim-version:a",
                                            "text": "收入", "period": None}],
                               "gaps": [], "profile": None}],
                 "industry_classification": {"classification": "insufficient_evidence",
                                             "slots": [], "sources": [], "gaps": []},
                 "variant_view": {"status": "unavailable", "sources": []}}
        record = {"sections": [{"aspect": "business_model", "status": "drafted",
                                "reason": None, "structure": ["business_model"],
                                "slots": [{"slot_id": "business_model", "sentences": [
                                    {"text": "这一版换了说法。", "refs": ["claim-version:a"]}]}],
                                "sources": [
                                    {"kind": "claim", "ref": "claim-version:a",
                                     "text": "收入", "period": None},
                                    {"kind": "figure", "ref": "statement-line:new",
                                     "text": "filed", "period": None}],
                                "gaps": [], "profile": None}],
                  "industry_classification": {"classification": "insufficient_evidence",
                                              "slots": [], "sources": [], "gaps": []},
                  "variant_view": {"status": "unavailable", "sources": []},
                  "company_ref": ACN}
        gate = rubric_gate(None, record, prior=prior)
        self.assertIn("new_version_cites_new_refs", gate["summary"]["failed_checks"])
        self.assertEqual(gate["failed"], [])
        self.assertEqual(gate["summary"]["overridden_checks"],
                         ["new_version_cites_new_refs"])


def _no_core():
    """A store stand-in for the tests that patch both row readers."""

    import types

    return types.SimpleNamespace(connection=None)


class FigureQuotaTests(unittest.TestCase):
    def test_filed_lines_do_not_starve_the_forecast_cells(self):
        from unittest.mock import patch

        from dalton_core.company_dossier_cli import number_material

        filed = [{"kind": "figure", "ref": f"statement-line:{n}", "text": "x",
                  "period": "p"} for n in range(60)]
        cells = [{"kind": "forecast_cell", "ref": f"cell:{n}", "text": "y",
                  "period": "p"} for n in range(20)]
        with patch("dalton_core.company_dossier_cli._statement_line_rows",
                   return_value=filed), \
             patch("dalton_core.company_dossier_cli._forecast_cell_rows",
                   return_value=cells):
            rows = number_material(_no_core(), ACN, limit=30)
        kinds = [row["kind"] for row in rows]
        self.assertEqual(len(rows), 30)
        self.assertEqual(kinds.count("forecast_cell"), 10)
        self.assertEqual(kinds.count("figure"), 20)

    def test_the_quota_is_a_floor_and_not_a_ceiling(self):
        from unittest.mock import patch

        from dalton_core.company_dossier_cli import number_material

        filed = [{"kind": "figure", "ref": f"statement-line:{n}", "text": "x",
                  "period": "p"} for n in range(60)]
        with patch("dalton_core.company_dossier_cli._statement_line_rows",
                   return_value=filed), \
             patch("dalton_core.company_dossier_cli._forecast_cell_rows",
                   return_value=[]):
            rows = number_material(_no_core(), ACN, limit=30)
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(row["kind"] == "figure" for row in rows))


class LaneWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_the_lane_is_registered_once_and_findable(self):
        self.assertIs(lane_for_operation("dispatch_company_dossier"), LANE)
        self.assertEqual(LANE.driver_key, "company_dossier")
        self.assertEqual(LANE.init_kwarg, LAUNCHER_KWARG)
        orders = [spec.order for spec in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))
        keys = [spec.driver_key for spec in registered_lanes()]
        self.assertEqual(len(keys), len(set(keys)))

    def context(self):
        state = self.state

        class Context:
            candidate_staging_path = None
            extraction_model_config_path = None

        Context.state = state
        return Context()

    def test_the_lane_is_absent_until_a_drafting_model_is_installed(self):
        class Args:
            db = str(self.state / "core.sqlite")
            company_dossier_model_config = None
            company_dossier_policy = None
            company_dossier_verifier_model_config = None
            scheduler = None

        self.assertIsNone(build_launcher(Args()))
        self.assertEqual(LANE.argv_fragment(self.context()), [])

    def test_the_lane_stays_off_until_the_policy_is_on_disk_too(self):
        from dalton_core.mission_dossier_lane import DOSSIER_MODEL_CONFIG, DOSSIER_POLICY

        (self.state / DOSSIER_MODEL_CONFIG).write_text("{}", encoding="utf-8")
        # A model and no policy: the two constitution-shaped sections have no
        # structure, so the lane would hold every tick. It stays off instead.
        self.assertEqual(LANE.argv_fragment(self.context()), [])
        (self.state / DOSSIER_POLICY).write_text("{}", encoding="utf-8")
        self.assertEqual(
            LANE.argv_fragment(self.context()),
            ["--company-dossier-model-config", str(self.state / DOSSIER_MODEL_CONFIG),
             "--company-dossier-policy", str(self.state / DOSSIER_POLICY)])

    def test_the_argv_carries_the_verifier_configuration_when_it_exists(self):
        from dalton_core.mission_dossier_lane import (
            DOSSIER_MODEL_CONFIG, DOSSIER_POLICY, DOSSIER_VERIFIER_MODEL_CONFIG,
        )

        for name in (DOSSIER_MODEL_CONFIG, DOSSIER_POLICY,
                     DOSSIER_VERIFIER_MODEL_CONFIG):
            (self.state / name).write_text("{}", encoding="utf-8")
        argv = LANE.argv_fragment(self.context())
        self.assertIn("--company-dossier-verifier-model-config", argv)
        self.assertEqual(argv[-1], str(self.state / DOSSIER_VERIFIER_MODEL_CONFIG))

    def test_the_lane_does_not_read_the_extraction_configuration(self):
        # It extracts nothing. Gating on the extraction model meant an
        # installation with one and no dossier policy had this lane on and
        # holding on every tick.
        from dalton_core.mission_dossier_lane import DOSSIER_MODEL_CONFIG, DOSSIER_POLICY

        for name in (DOSSIER_MODEL_CONFIG, DOSSIER_POLICY):
            (self.state / name).write_text("{}", encoding="utf-8")
        context = self.context()
        context.extraction_model_config_path = None
        self.assertTrue(LANE.argv_fragment(context))

    def test_the_writer_without_this_lane_says_so_rather_than_failing(self):
        class Server:
            lane_state: dict = {}

            def lane_launcher(self, kwarg):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("company-dossier", result["reason"])

    def test_the_child_parser_takes_what_the_launcher_passes(self):
        state = self.state.resolve()
        launcher = CompanyDossierLauncher(
            state_dir=state, model_config_path=state / "m.json",
            verifier_model_config_path=state / "v.json",
            policy_path=state / "p.json")
        command = launcher._command(ticket_dir=state, company_ref=ACN)
        args = build_parser().parse_args(command[3:])
        self.assertEqual(args.company_ref, ACN)
        self.assertEqual(args.model_config, state / "m.json")
        self.assertEqual(args.verifier_model_config, state / "v.json")
        self.assertEqual(args.dossier_policy, state / "p.json")

    def test_bad_model_config_does_not_break_controller_construction(self):
        path = self.state / "bad-model.json"
        path.write_text("{", encoding="utf-8")
        launcher = CompanyDossierLauncher(state_dir=self.state,
                                          model_config_path=path)
        connection = sqlite3.connect(":memory:")
        try:
            coordinator = MissionDossierLaneCoordinator(
                connection=connection, launcher=launcher)
            self.assertEqual(coordinator.budget.probe_interval_seconds, 1800)
        finally:
            connection.close()

    def test_the_ticket_is_named_by_the_evidence_the_run_is_about(self):
        first = run_digest(ACN, "signature-a")
        self.assertEqual(first, run_digest(ACN, "signature-a"))
        self.assertNotEqual(first, run_digest(ACN, "signature-b"))
        self.assertRegex(first, r"^[0-9a-f]{24}$")


class CoordinatorTests(unittest.TestCase):
    class Launcher:
        def __init__(self, ticket_status="succeeded", summary=None,
                     capacity_cooldown=None):
            self.started: list[str] = []
            self.ticket_status = ticket_status
            self.summary = summary or {"dossier_status": "nothing_new"}
            self.capacity_cooldown = capacity_cooldown
            self.started_companies: list[str | None] = []
            self.reentries: set[tuple[str | None, str]] = set()
            self.reentry_checks: list[tuple[str | None, str]] = []

        def controlled_reentry(self, *, signature, company_ref, mission):
            key = (company_ref, signature)
            self.reentry_checks.append(key)
            return ":operator-recovery:" + "a" * 16 if key in self.reentries else None

        def capacity_probe_interval_seconds(self):
            return self.capacity_cooldown

        def start(self, *, signature, company_ref=None,
                  controlled_reentry=None):
            self.started.append(signature)
            self.started_companies.append(company_ref)
            return {"id": f"company-dossier-run:{run_digest(company_ref, signature)}",
                    "signature": signature, "company_ref": company_ref,
                    "controlled_reentry": controlled_reentry}

        def status(self, ticket_ref):
            return {"id": ticket_ref, "status": self.ticket_status,
                    "signature": self.started[-1],
                    "company_ref": self.started_companies[-1],
                    "summary": self.summary}

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection

    def test_it_launches_once_and_settles_on_the_next_tick(self):
        launcher = self.Launcher()
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        second = coordinator.dispatch_once()
        self.assertEqual(second["settled"]["dossier_status"], "nothing_new")
        self.assertEqual(len(launcher.started), 1)

    def test_it_stays_quiet_until_the_ledger_moves(self):
        launcher = self.Launcher()
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        coordinator.dispatch_once()
        coordinator.dispatch_once()
        third = coordinator.dispatch_once()
        self.assertEqual(third["status"], "idle")
        self.harness.tag("d-9", "business_model", statement="又一条新的一手材料。")
        fourth = coordinator.dispatch_once()
        self.assertEqual(fourth["status"], "launched")
        self.assertEqual(len(launcher.started), 2)

    def test_three_transient_failures_hold_that_signature(self):
        launcher = self.Launcher(ticket_status="failed",
                                 summary={"failure_reason": "boom"})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        for _ in range(3):
            coordinator.dispatch_once()
        held = coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "boom")

    def test_a_contract_refusal_is_terminal_for_the_unchanged_signature(self):
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "rubric_refused",
                     "failure_reason": "demand_drivers.gaps must be at most 6 short strings"})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        held = coordinator.dispatch_once()
        self.assertEqual(held["status"], "terminal")
        self.assertEqual(len(launcher.started), 1)

    def test_one_company_content_refusal_does_not_starve_the_next_company(self):
        second_company = "company:sec-cik:0000000002"
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "rubric_refused",
                     "failure_reason": "unsupported sentence"},
        )
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN, second_company],
        )
        self.assertEqual(coordinator.dispatch_once()["company_ref"], ACN)
        second = coordinator.dispatch_once()
        self.assertEqual(second["status"], "launched")
        self.assertEqual(second["company_ref"], second_company)
        self.assertEqual(launcher.started_companies, [ACN, second_company])
        self.assertIn(ACN, second["held"])

    def test_only_the_exact_held_company_signature_with_authority_reenters(self):
        second_company = "company:sec-cik:0000000002"
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "rubric_refused",
                     "failure_reason": "host transport failed"},
        )
        mission = {"id": "coverage-mission-version:test:1"}
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN, second_company], mission=lambda: mission,
        )
        first = coordinator.dispatch_once()
        first_key = (ACN, first["signature"])
        second = coordinator.dispatch_once()
        second_key = (second_company, second["signature"])
        self.assertEqual(second["company_ref"], second_company)
        launcher.reentries.add(first_key)
        recovered = coordinator.dispatch_once()
        self.assertEqual(recovered["company_ref"], ACN)
        self.assertEqual((ACN, recovered["signature"]), first_key)
        self.assertNotEqual(first_key, second_key)

    def test_company_signature_ignores_another_company_claim(self):
        second_company = "company:sec-cik:0000000002"
        acn_before = company_ledger_signature(self.connection, ACN)
        other_before = company_ledger_signature(self.connection, second_company)
        claim = self.harness.fixture.add_claim(
            "other-company-claim", subject_ref=second_company,
            statement="另一家公司的新材料。",
        )
        self.harness.index.record_entry(**entry_args(
            claim, subject_ref=second_company,
            dedupe_group_key=f"qual|{second_company}|other-company-claim",
        ))
        self.assertEqual(company_ledger_signature(self.connection, ACN), acn_before)
        self.assertNotEqual(
            company_ledger_signature(self.connection, second_company), other_before)

    def test_selected_statement_and_forecast_material_are_company_scoped(self):
        second_company = "company:sec-cik:0000000002"

        def filed(_connection, company_ref, *, limit):
            return ([{"kind": "figure", "ref": "statement-line:acn", "text": "filed",
                      "period": "2026Q1"}] if company_ref == ACN else [])

        def forecast(_connection, company_ref, *, limit):
            return ([{"kind": "forecast_cell", "ref": "forecast-cell:acn",
                      "text": "forecast", "period": "2026Q2"}]
                    if company_ref == ACN else [])

        with patch("dalton_core.company_dossier_cli._statement_line_rows",
                   side_effect=filed), patch(
                       "dalton_core.company_dossier_cli._forecast_cell_rows",
                       side_effect=forecast):
            acn_with_inputs = company_ledger_signature(self.connection, ACN)
            other_with_inputs = company_ledger_signature(self.connection, second_company)
        with patch("dalton_core.company_dossier_cli._statement_line_rows",
                   return_value=[]), patch(
                       "dalton_core.company_dossier_cli._forecast_cell_rows",
                       return_value=[]):
            self.assertNotEqual(
                company_ledger_signature(self.connection, ACN), acn_with_inputs)
            self.assertEqual(
                company_ledger_signature(self.connection, second_company),
                other_with_inputs,
            )

    def test_document_figure_and_claim_retirement_change_only_their_company(self):
        from dalton_core.claim_retirement import ClaimRetirementAuthority

        second_company = "company:sec-cik:0000000002"
        other_before = company_ledger_signature(self.connection, second_company)
        before = company_ledger_signature(self.connection, ACN)
        self.harness.missions.record_document_figures(
            company_ref=ACN, review_ref="mission-document-review:dossier-signature",
            document_ref="sec:filing:0001467373-26-000001",
            source_manifest_hash="0" * 64, source_grade="company-filed-document",
            figures=[{"quote_id": "quote:0:100:" + "a" * 16,
                      "metric_ref": "metric:revenue", "subject_as_named": "Accenture",
                      "as_reported_label": "Revenue", "value": "17.7",
                      "unit": "currency", "currency": "USD", "period": "FY2026Q3",
                      "basis": "gaap-reported", "scale": "billion",
                      "citation_text": "Revenue was 17.7 billion."}],
            observed_by=AUTOMATION,
        )
        after_figure = company_ledger_signature(self.connection, ACN)
        self.assertNotEqual(after_figure, before)
        self.assertEqual(company_ledger_signature(self.connection, second_company), other_before)

        claim_ref = self.harness.tag(
            "retire-signature", "guidance_style",
            statement="Management guided revenue growth to 5 percent.",
        )["claim_version_id"]
        indexed = company_ledger_signature(self.connection, ACN)
        claim_hash = self.connection.execute(
            "SELECT content_hash FROM claim_versions WHERE claim_version_id=?",
            (claim_ref,),
        ).fetchone()["content_hash"]
        retirement = ClaimRetirementAuthority(self.harness.store)
        challenge = retirement.challenge(
            claim_version_ref=claim_ref, claim_version_hash=claim_hash,
            reason_code="human_judgment", rationale="fixture",
            actor_ref="human:coverage-owner",
        )
        retirement.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref="human:coverage-owner",
            rationale="fixture",
        )
        self.assertNotEqual(company_ledger_signature(self.connection, ACN), indexed)

    def test_a_failed_verifier_transport_is_not_mislabeled_as_content(self):
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "verification_failed",
                     "failure_reason": "TransportError: verifier socket unavailable"})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        # Settlement classifies the transport as a dependency and grants its
        # one governed probe; it is not a permanent content terminal.
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(launcher.started), 2)

    def test_an_executed_verifier_rejection_is_terminal_content(self):
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "verification_failed",
                     "failure_reason": "verifier rejected unsupported_sentence",
                     "verification": {"status": "verified", "verdict": "reject"}})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(coordinator.dispatch_once()["status"], "terminal")
        self.assertEqual(len(launcher.started), 1)

    def test_a_draft_contract_change_moves_the_lane_signature_once(self):
        before = ledger_signature(self.connection)
        with patch("dalton_core.company_dossier_draft.draft_contract_fingerprint",
                   return_value="f" * 64):
            after = ledger_signature(self.connection)
        self.assertNotEqual(before, after)

    def test_a_verifier_prompt_contract_change_releases_the_company_hold(self):
        before = company_ledger_signature(self.connection, ACN)
        with patch(
            "dalton_core.company_dossier_draft.verifier_prompt_contract_fingerprint",
            return_value="e" * 64,
        ):
            after = company_ledger_signature(self.connection, ACN)
        self.assertNotEqual(before, after)

    def test_a_reviewed_verifier_contract_gets_a_new_ticket_after_cached_reject(self):
        launcher = self.Launcher(
            ticket_status="failed",
            summary={"dossier_status": "verification_failed",
                     "failure_reason": "date absent from the cited source",
                     "verification": {"status": "verified", "verdict": "reject"}},
        )
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher, companies=lambda: [ACN])
        first = coordinator.dispatch_once()
        held = coordinator.dispatch_once()
        self.assertEqual(held["status"], "terminal")

        with patch(
            "dalton_core.company_dossier_draft.verifier_prompt_contract_fingerprint",
            return_value="e" * 64,
        ):
            retried = coordinator.dispatch_once()
        self.assertEqual(retried["status"], "launched")
        self.assertNotEqual(retried["signature"], first["signature"])
        self.assertNotEqual(retried["ticket_ref"], first["ticket_ref"])
        self.assertEqual(launcher.started_companies, [ACN, ACN])

    def test_configured_model_capacity_cooldown_controls_lane_probe(self):
        now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
        launcher = self.Launcher(
            ticket_status="failed", capacity_cooldown=60,
            summary={"failure_reason": "capacity_busy: broker busy"})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            failure_clock=lambda: now[0])
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        # Settlement parks it, and the dependency budget grants one free probe.
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(coordinator.dispatch_once()["status"], "parked")
        now[0] += timedelta(seconds=59)
        self.assertEqual(coordinator.dispatch_once()["status"], "parked")
        now[0] += timedelta(seconds=1)
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")

    def test_the_signature_moves_when_a_dossier_version_lands(self):
        before = ledger_signature(self.connection)
        self.harness.run()
        self.assertNotEqual(ledger_signature(self.connection), before)

    def test_importing_this_module_does_not_pull_in_the_writer(self):
        import subprocess
        import sys
        import textwrap

        import dalton_core

        root = Path(dalton_core.__file__).resolve().parents[1]
        script = textwrap.dedent("""
            import sys
            import dalton_core.mission_dossier_lane  # noqa: F401
            print(",".join(sorted(
                module for module in sys.modules
                if module in ("dalton_core.writer_server",
                              "dalton_core.bounded_planner_driver",
                              "dalton_core.macos_launchagent")
            )))
        """)
        finished = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}, timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(finished.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
