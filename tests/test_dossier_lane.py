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
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_index_authority import ClaimIndexAuthority
from dalton_core.company_dossier import (
    CompanyDossierAuthority, causal_chain_hash, validate_policy,
)
from dalton_core.company_dossier_cli import (
    build_parser, granted_scope, run_dossier, screened_companies, stale_units,
)
from dalton_core.company_dossier_launcher import CompanyDossierLauncher, run_digest
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_dossier_lane import (
    LANE, LAUNCHER_KWARG, MissionDossierLaneCoordinator, build_launcher,
    dispatch, ledger_signature,
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
            kind="qualitative", value=None, unit=None, group=None):
        claim = self.fixture.add_claim(
            ref, kind=kind, value=value, unit=unit, metric=metric,
            statement=statement)
        self.index.record_entry(**entry_args(
            claim, aspect=aspect, metric_or_aspect=metric,
            dedupe_group_key=group or f"qual|{ACN}|{ref}",
            claim_kind=kind, importance="filing",
            importance_basis="document_spec:sec-10q"))
        return claim

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

    def test_a_company_that_has_not_passed_its_screen_has_no_file_to_deepen(self):
        harness = Harness(screened=False)
        self.addCleanup(harness.close)
        self.assertEqual(screened_companies(harness.missions, harness.mission), [])
        summary = harness.run()
        self.assertEqual(summary["dossier_status"], "no_screened_company")


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

    def test_the_guidance_section_carries_the_computed_table_not_a_verdict(self):
        summary = self.harness.run(max_units=12)
        self.assertEqual(summary["dossier_status"], "published")
        record = self.authority.latest(ACN)
        section = next(item for item in record["sections"]
                       if item["aspect"] == "guidance_style")
        self.assertIsNotNone(section["profile"])
        self.assertEqual(section["profile"]["classification"], "insufficient_data")
        self.assertEqual(section["profile"]["events"][0]["guide"]["low"], "5")

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
        # already cites and it is refused -- by the rubric's
        # ``new_version_cites_new_refs``, which is ADR-0008's rule stated as a
        # quality check and which fires one step before the authority's own.
        # The request is honoured; the rule is not suspended.
        self.assertEqual(asked["units_drafted"], ["business_model"])
        self.assertEqual(asked["dossier_status"], "rubric_refused")
        self.assertIn("new_version_cites_new_refs", asked["rubric"]["hard_failed"])
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
        self.assertEqual(summary["verification"]["verdict"], "reject")
        self.assertEqual(self.authority.versions(ACN), [])

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

    def test_the_lane_is_absent_until_a_drafting_model_is_installed(self):
        class Args:
            db = str(self.state / "core.sqlite")
            company_dossier_model_config = None
            company_dossier_policy = None
            scheduler = None

        self.assertIsNone(build_launcher(Args()))

        class Context:
            state = self.state
            extraction_model_config_path = None
            candidate_staging_path = None

        self.assertEqual(LANE.argv_fragment(Context()), [])

    def test_the_argv_fragment_appears_once_the_configuration_is_on_disk(self):
        from dalton_core.mission_dossier_lane import DOSSIER_MODEL_CONFIG

        config = self.state / DOSSIER_MODEL_CONFIG
        config.write_text("{}", encoding="utf-8")

        class Context:
            state = self.state
            extraction_model_config_path = "/somewhere/extraction.json"
            candidate_staging_path = None

        self.assertEqual(LANE.argv_fragment(Context()),
                         ["--company-dossier-model-config", str(config)])

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
            policy_path=state / "p.json")
        command = launcher._command(ticket_dir=state, company_ref=ACN)
        args = build_parser().parse_args(command[3:])
        self.assertEqual(args.company_ref, ACN)
        self.assertEqual(args.model_config, state / "m.json")
        self.assertEqual(args.dossier_policy, state / "p.json")

    def test_the_ticket_is_named_by_the_evidence_the_run_is_about(self):
        first = run_digest(ACN, "signature-a")
        self.assertEqual(first, run_digest(ACN, "signature-a"))
        self.assertNotEqual(first, run_digest(ACN, "signature-b"))
        self.assertRegex(first, r"^[0-9a-f]{24}$")


class CoordinatorTests(unittest.TestCase):
    class Launcher:
        def __init__(self, ticket_status="succeeded", summary=None):
            self.started: list[str] = []
            self.ticket_status = ticket_status
            self.summary = summary or {"dossier_status": "nothing_new"}

        def start(self, *, signature, company_ref=None):
            self.started.append(signature)
            return {"id": f"company-dossier-run:{run_digest(company_ref, signature)}",
                    "signature": signature}

        def status(self, ticket_ref):
            return {"id": ticket_ref, "status": self.ticket_status,
                    "signature": self.started[-1], "summary": self.summary}

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

    def test_a_failed_run_is_held_rather_than_retried_every_tick(self):
        launcher = self.Launcher(ticket_status="failed",
                                 summary={"failure_reason": "boom"})
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher)
        coordinator.dispatch_once()
        coordinator.dispatch_once()
        held = coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "boom")

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
