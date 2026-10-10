"""2026-10-06 (owner decision A): AlphaEngine query forms are owned by code.

Every environment renders the same forms (``ALPHAENGINE_QUERY_FORMS``); an
approved plan file governs the budget, each spec's lookback and intervals and
the company names.  A 0.6 plan that still carries its own forms keeps its
hash, its selector and its approval -- the forms are just not rendered -- and
the rendered plan carries its own content hash, so the first search after a
form-set change is a new search exactly once.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from dalton_core.alphaengine_query_forms import (
    ALPHAENGINE_QUERY_FORMS,
    ALPHAENGINE_QUERY_FORMS_HASH,
    code_query_variants,
    effective_discovery_plan,
    load_effective_discovery_plan,
    query_form_report,
    query_forms_ledger_note,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.macos_launchagent import ALPHAENGINE_PLAN_SELECTOR, _alphaengine_discovery_plan
from dalton_core.mission_source_discovery import (
    AlphaEngineSearchLauncher,
    DiscoveryPlanError,
    MissionSourceDiscoveryCoordinator,
    build_discovery_parameters,
    build_discovery_plan,
    discovery_coordinator_state,
    load_discovery_plan,
    validate_discovery_plan,
)
from dalton_core.store import content_hash
from dalton_core.workspace_health_parity import Environment, alphaengine_query_form_rows
from tests import test_mission_source_discovery as base
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

TRANSCRIPTS, SELL_SIDE = "earnings-call-transcripts", "sell-side-reports"


def variants(*templates: str) -> list[dict]:
    return [{"query_template": template, "filters": {}} for template in templates]


#: The legacy plan the legacy Core runs today, field for field.
LEGACY_V2_COMPANIES = {
    "company:sec-cik:0000051143": {"aliases": ["IBM"], "name": "International Business Machines",
                                   "ticker": "IBM"},
    "company:sec-cik:0001058290": {"aliases": ["Cognizant"],
                                   "name": "Cognizant Technology Solutions", "ticker": "CTSH"},
    "company:sec-cik:0001352010": {"aliases": ["EPAM"], "name": "EPAM Systems", "ticker": "EPAM"},
    "company:sec-cik:0001467373": {"aliases": ["Accenture"], "name": "Accenture plc",
                                   "ticker": "ACN"},
    "company:sec-cik:001688568": {"aliases": ["DXC"], "name": "DXC Technology", "ticker": "DXC"},
}
LEGACY_V2_SPECS = [
    {"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes", "lookback_days": 400,
     "query_variants": variants("{name} {quarter} earnings call transcript",
                                "{ticker} {quarter} results webcast transcript",
                                "{name} quarterly earnings conference call"),
     "rediscovery_interval_days": 7, "retry_interval_days": 1},
    {"spec_ref": SELL_SIDE, "document_type": "sell_side_report", "lookback_days": 180,
     "query_variants": variants("{name} equity research report", "{ticker} analyst report"),
     "rediscovery_interval_days": 7, "retry_interval_days": 1},
]
LEGACY_V2_HASH = "5a9d4b1c4cbe67210bade644cef4695e0f1939f0e333c0157377977d4b82f9ee"


def legacy_v2() -> dict:
    return build_discovery_plan(
        plan_id="discovery-plan:us-it-services:alphaengine:2-candidate",
        created_at="2026-09-11T00:00:00.000000+00:00",
        mission_ref="coverage-mission:us-it-services", source_ref="source:alphaengine",
        max_calls_24h=130, companies=LEGACY_V2_COMPANIES, specs=LEGACY_V2_SPECS)


def ws7d_v3_like(companies: dict, *, ticker_only: bool = False) -> dict:
    """ws-7d's v3 shape: forms equal to the code's (v1 shape when ticker_only)."""

    specs = [{**{k: v for k, v in spec.items() if k != "query_variants"},
              "query_variants": code_query_variants(spec["spec_ref"], spec["document_type"])}
             for spec in LEGACY_V2_SPECS]
    if ticker_only:
        companies = {ref: {"name": c["ticker"], "ticker": c["ticker"], "aliases": []}
                     for ref, c in companies.items()}
    return build_discovery_plan(
        plan_id="discovery-plan:ws:alphaengine:3", created_at="2026-10-03T19:01:15.430735+00:00",
        mission_ref="coverage-mission:ws", source_ref="source:alphaengine",
        max_calls_24h=50, companies=companies, specs=specs)


def write_selected(state: Path, plan: dict, filename: str) -> Path:
    plans = state / "discovery-plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / filename).write_text(json.dumps(plan))
    body = {"schema_version": "alphaengine-discovery-plan-selection-0.1",
            "id": "alphaengine-plan-selection:test", "status": "approved",
            "source_ref": "source:alphaengine", "plan_ref": plan["id"],
            "plan_hash": plan["content_hash"], "plan_path": filename}
    (plans / ALPHAENGINE_PLAN_SELECTOR).write_text(
        json.dumps({**body, "content_hash": content_hash(body)}))
    return plans / filename


class RenderingTests(unittest.TestCase):
    def test_the_legacy_plan_still_validates_and_renders_the_code_forms(self) -> None:
        approved = legacy_v2()
        # The approved file and its selector are untouched: same hash as live.
        self.assertEqual(approved["content_hash"], LEGACY_V2_HASH)
        rendered = effective_discovery_plan(approved)
        self.assertNotEqual(rendered["content_hash"], LEGACY_V2_HASH)
        self.assertEqual(rendered["id"], approved["id"])
        self.assertEqual(rendered["budget"], {"max_calls_24h": 130})
        self.assertEqual(rendered["companies"], approved["companies"])
        for spec, old in zip(rendered["specs"], approved["specs"]):
            self.assertEqual(spec["query_variants"],
                             code_query_variants(spec["spec_ref"], spec["document_type"]))
            for key in ("lookback_days", "rediscovery_interval_days", "retry_interval_days",
                        "document_type"):
                self.assertEqual(spec[key], old[key])
        query = build_discovery_parameters(
            rendered, spec_ref=TRANSCRIPTS, company_ref="company:sec-cik:0001467373",
            as_of=date(2026, 10, 6), missing_periods=["FY2026-Q4"])["query"]
        self.assertEqual(query, "Accenture plc Q4 2026")
        query = build_discovery_parameters(
            rendered, spec_ref=SELL_SIDE, company_ref="company:sec-cik:0001467373",
            as_of=date(2026, 10, 6))["query"]
        self.assertEqual(query, "ACN analyst research report")
        # Idempotent: the coordinator, the launcher and the child may each render.
        self.assertEqual(effective_discovery_plan(rendered), rendered)

    def test_a_plan_whose_forms_are_the_codes_renders_to_itself(self) -> None:
        v3 = ws7d_v3_like(LEGACY_V2_COMPANIES)
        self.assertEqual(effective_discovery_plan(v3)["content_hash"], v3["content_hash"])
        report = query_form_report(v3)
        self.assertEqual(report["rendered_from"], "code")
        self.assertEqual([s["plan_forms_differ"] for s in report["specs"]], [False, False])
        self.assertNotIn("ignored", query_forms_ledger_note(report)["query_forms_note"])

    def test_ticker_only_names_are_resolved_from_the_name_table(self) -> None:
        companies = {"company:ticker:googl": {"name": "Alphabet", "ticker": "GOOGL",
                                              "aliases": ["Alphabet"]},
                     "company:ticker:zzzz": {"name": "ZZZZ", "ticker": "ZZZZ", "aliases": []}}
        v1 = ws7d_v3_like(companies, ticker_only=True)
        rendered = effective_discovery_plan(v1)
        self.assertEqual(rendered["companies"]["company:ticker:googl"],
                         {"name": "Alphabet", "ticker": "GOOGL", "aliases": ["Alphabet"]})
        # No name anywhere in code: rendered as the ticker, and reported.
        self.assertEqual(rendered["companies"]["company:ticker:zzzz"]["name"], "ZZZZ")
        report = query_form_report(v1)
        self.assertEqual(report["names_filled"], {"company:ticker:googl": "Alphabet"})
        self.assertEqual(report["names_unresolved"], ["company:ticker:zzzz"])
        query = build_discovery_parameters(
            rendered, spec_ref=TRANSCRIPTS, company_ref="company:ticker:googl",
            as_of=date(2026, 10, 6), missing_periods=["FY2025-Q3"])["query"]
        self.assertEqual(query, "Alphabet Q3 2025")

    def test_a_spec_the_code_has_no_forms_for_keeps_its_own(self) -> None:
        def plan(spec_ref: str, document_type: str) -> dict:
            return build_discovery_plan(
                plan_id="discovery-plan:x:alphaengine:1", created_at="2026-10-06T00:00:00.000000+00:00",
                mission_ref="coverage-mission:x", source_ref="source:alphaengine",
                max_calls_24h=10, companies={"company:ticker:googl": {
                    "name": "Alphabet", "ticker": "GOOGL", "aliases": ["Alphabet"]}},
                specs=[{"spec_ref": spec_ref, "document_type": document_type,
                        "query_variants": variants("{name} news"), "lookback_days": 90,
                        "rediscovery_interval_days": 7, "retry_interval_days": 1}])

        news = plan("company-news", "news")
        report = query_form_report(news)
        self.assertEqual(report["specs"][0]["rendered_from"], "plan")
        self.assertEqual(effective_discovery_plan(news), news)
        # Named differently but of a document type the code has forms for.
        minutes = plan("other-minutes", "meeting_minutes")
        self.assertEqual(query_form_report(minutes)["specs"][0]["rendered_from"], "code")
        self.assertEqual(effective_discovery_plan(minutes)["specs"][0]["query_variants"],
                         code_query_variants(TRANSCRIPTS, "meeting_minutes"))

    def test_a_bare_search_terms_plan_cannot_render_from_code(self) -> None:
        plan = build_discovery_plan(
            plan_id="discovery-plan:us-it-services:alphaengine:1",
            created_at="2026-09-01T00:00:00.000000+00:00", mission_ref="coverage-mission:us-it-services",
            companies={"company:sec-cik:0001467373": "Accenture"},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_template": "{terms} earnings call transcript", "lookback_days": 400,
                    "rediscovery_interval_days": 7, "retry_interval_days": 1}])
        self.assertEqual(effective_discovery_plan(plan), plan)
        self.assertEqual(query_form_report(plan)["rendered_from"], "plan")


class GovernedOnlySchemaTests(unittest.TestCase):
    def test_the_generator_writes_governed_fields_only(self) -> None:
        from dalton_core.workspace_mission_setup import (
            alphaengine_plan_companies, alphaengine_plan_specs,
        )

        plan = build_discovery_plan(
            plan_id="discovery-plan:ws-new:alphaengine:1",
            created_at="1970-01-01T00:00:00.000000+00:00", mission_ref="coverage-mission:ws-new",
            companies=alphaengine_plan_companies({"company:ticker:msft": "MSFT"}),
            max_calls_24h=50, specs=alphaengine_plan_specs())
        self.assertEqual(plan["schema_version"], "0.7")
        self.assertTrue(all("query_variants" not in spec for spec in plan["specs"]))
        rendered = effective_discovery_plan(plan)
        self.assertEqual(rendered["schema_version"], "0.6")
        self.assertEqual({spec["spec_ref"] for spec in rendered["specs"]},
                         set(ALPHAENGINE_QUERY_FORMS))
        # A governed-only plan handed straight to the builder is rendered too.
        self.assertEqual(
            build_discovery_parameters(plan, spec_ref=TRANSCRIPTS, company_ref="company:ticker:msft",
                                       as_of=date(2026, 10, 6),
                                       missing_periods=["FY2026-Q4"])["query"],
            "Microsoft Q4 2026")

    def test_a_governed_only_plan_must_name_specs_the_code_has_forms_for(self) -> None:
        base_plan = {"schema_version": "0.7", "id": "discovery-plan:x:alphaengine:1",
                     "created_at": "2026-10-06T00:00:00.000000+00:00",
                     "mission_ref": "coverage-mission:x", "source_ref": "source:alphaengine",
                     "budget": {"max_calls_24h": 5},
                     "companies": {"company:ticker:msft": {"name": "Microsoft", "ticker": "MSFT",
                                                           "aliases": []}},
                     "specs": [{"spec_ref": "made-up", "document_type": "sell_side_report",
                                "lookback_days": 1, "rediscovery_interval_days": 1,
                                "retry_interval_days": 1}]}
        # sell_side_report matches one code spec by document type: accepted.
        validate_discovery_plan({**base_plan, "content_hash": content_hash(base_plan)})
        base_plan["specs"][0]["query_variants"] = variants("{name}")
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan({**base_plan, "content_hash": content_hash(base_plan)})


class SelectorAndLoadersTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"

    def test_an_old_style_selection_resolves_and_every_loader_renders_the_same_plan(self) -> None:
        from dalton_core.writer_server import _rendered_alphaengine_plan

        path = write_selected(self.state, legacy_v2(), "us-it-services-alphaengine-v2.json")
        # The owner's selector binding still validates the approved file.
        self.assertEqual(_alphaengine_discovery_plan(self.state), path.resolve())
        writer_plan, note = _rendered_alphaengine_plan(path)
        launcher = AlphaEngineSearchLauncher(state_dir=self.state, governance_path=path,
                                             plan_path=path)
        child_plan = effective_discovery_plan(load_discovery_plan(path))
        self.assertEqual(writer_plan, launcher.load_plan())
        self.assertEqual(writer_plan, child_plan)
        self.assertEqual(writer_plan, load_effective_discovery_plan(path))
        self.assertEqual(note["query_forms"], "code")
        self.assertEqual(note["query_forms_hash"], writer_plan["content_hash"])
        self.assertIn("ignored for earnings-call-transcripts,sell-side-reports",
                      note["query_forms_note"])

    def test_the_approval_hash_still_binds_every_field_of_the_file(self) -> None:
        plan = legacy_v2()
        path = write_selected(self.state, plan, "v2.json")
        for change in ("budget", "lookback", "forms"):
            changed = json.loads(json.dumps(plan))
            if change == "budget":
                changed["budget"]["max_calls_24h"] += 1
            elif change == "lookback":
                changed["specs"][0]["lookback_days"] += 1
            else:
                changed["specs"][1]["query_variants"].reverse()
            changed["content_hash"] = content_hash(
                {k: v for k, v in changed.items() if k != "content_hash"})
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "ref, hash, and source"):
                _alphaengine_discovery_plan(self.state)
        path.write_text(json.dumps(plan))
        self.assertEqual(_alphaengine_discovery_plan(self.state), path.resolve())


class ParityRowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"
        self.state.mkdir()

    def rows(self) -> dict:
        env = Environment("t", self.state)
        try:
            return {row["check"]: row for row in alphaengine_query_form_rows(env)}
        finally:
            env.close()

    def test_legacy_forms_are_information_and_governed_fields_are_shown(self) -> None:
        write_selected(self.state, legacy_v2(), "us-it-services-alphaengine-v2.json")
        rows = self.rows()
        row = rows[f"alphaengine.query_forms.{TRANSCRIPTS}"]
        self.assertEqual(row["status"], "info")
        self.assertIn("differ and are ignored", row["detail"])
        self.assertIn("lookback_days=400", row["detail"])
        self.assertIn("rediscovery_interval_days=7", row["detail"])
        self.assertIn("max_calls_24h=130", row["detail"])
        self.assertIn(LEGACY_V2_HASH[:12], row["detail"])
        self.assertEqual(rows[f"alphaengine.query_forms.{SELL_SIDE}"]["status"], "info")
        self.assertNotIn("alphaengine.query_forms.names", rows)

    def test_code_forms_are_ok_and_unnamed_companies_warn(self) -> None:
        write_selected(self.state, ws7d_v3_like(LEGACY_V2_COMPANIES), "v3.json")
        rows = self.rows()
        self.assertEqual({row["status"] for row in rows.values()}, {"ok"})
        write_selected(self.state, ws7d_v3_like(
            {"company:ticker:zzzz": {"name": "ZZZZ", "ticker": "ZZZZ", "aliases": []}},
            ticker_only=True), "v1.json")
        names = self.rows()["alphaengine.query_forms.names"]
        self.assertEqual(names["status"], "warn")
        self.assertIn("owner must publish", names["fix"])

    def test_a_plan_that_cannot_render_from_code_warns(self) -> None:
        plans = self.state / "discovery-plans"
        plans.mkdir()
        plan = build_discovery_plan(
            plan_id="discovery-plan:us-it-services:alphaengine:1",
            created_at="2026-09-01T00:00:00.000000+00:00", mission_ref="coverage-mission:us-it-services",
            companies={"company:sec-cik:0001467373": "Accenture"},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_template": "{terms} earnings call transcript", "lookback_days": 400,
                    "rediscovery_interval_days": 7, "retry_interval_days": 1}])
        (plans / "us-it-services-alphaengine-v1.json").write_text(json.dumps(plan))
        row = self.rows()[f"alphaengine.query_forms.{TRANSCRIPTS}"]
        self.assertEqual(row["status"], "warn")

    def test_no_plan_yet_is_pending(self) -> None:
        rows = self.rows()
        self.assertEqual(rows["alphaengine.query_forms"]["status"], "pending")
        self.assertIn(ALPHAENGINE_QUERY_FORMS_HASH[:12], rows["alphaengine.query_forms"]["detail"])


class Selection:
    def currently_consumed(self, **_kwargs):
        return []

    def current_selections(self, **_kwargs):
        return {}


class FormSetChangeCadenceTests(unittest.TestCase):
    """A form-set change fires each (company, spec) once; no change fires nothing."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clock = base.Clock()
        self.h = base.SearchHarness(Path(temp.name), [
            {"doc_id": base.NEW_DOC.split(":")[1]}], clock=self.clock)
        self.addCleanup(self.h.close)
        state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        params = mission_params(state)
        v1 = self.missions.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": v1["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.company = self.mission["universe"][0]["company_ref"]

    def approved(self, forms: list[dict]) -> dict:
        return build_discovery_plan(
            plan_id="discovery-plan:us-it-services:alphaengine:2-candidate",
            created_at=base.NOW.isoformat(timespec="microseconds"),
            mission_ref="coverage-mission:us-it-services", source_ref="source:alphaengine",
            max_calls_24h=30,
            companies={self.company: {"name": "Accenture plc", "ticker": "ACN",
                                      "aliases": ["Accenture"]}},
            specs=[{"spec_ref": TRANSCRIPTS, "document_type": "meeting_minutes",
                    "query_variants": forms, "lookback_days": 400,
                    "rediscovery_interval_days": 7, "retry_interval_days": 1}])

    def coordinator(self, plan: dict, **kwargs):
        search = base.FakeSearchLauncher(self.h, self.missions, plan)
        return MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=plan,
            search_launcher=search, acquisition_launcher=None, clock=self.clock,
            selection_launcher=Selection(), **kwargs), search

    def search_once(self, plan: dict) -> None:
        coordinator, _ = self.coordinator(plan)
        result = coordinator.launch_discovery()
        self.assertEqual(result["status"], "launched", result)
        coordinator.settle_dispatches()

    def test_a_changed_first_form_is_searched_once_on_deploy(self) -> None:
        approved = self.approved(LEGACY_V2_SPECS[0]["query_variants"])
        # Before the deploy the coordinator ran the file's own forms.
        self.search_once(approved)
        self.clock.advance(hours=3)
        before, _ = self.coordinator(approved)
        self.assertEqual(before.launch_discovery()["status"], "idle")
        # After: the same approved file, rendered with the code's forms.
        rendered = effective_discovery_plan(approved)
        note = query_forms_ledger_note(query_form_report(approved))
        after, search = self.coordinator(rendered, query_forms=note)
        launched = after.launch_discovery()
        self.assertEqual(launched["status"], "launched", launched)
        self.assertEqual((search.starts[-1]["variant_index"], search.starts[-1]["cursor"]),
                         (0, None))
        dispatch = self.missions.discovery_dispatches(
            self.mission["id"], company_ref=self.company, spec_ref=TRANSCRIPTS, limit=1,
            across_versions=True)[0]
        # The dispatch records the rendered plan, i.e. the form set it used.
        self.assertEqual(dispatch["discovery_plan_hash"], rendered["content_hash"])
        self.assertEqual(dispatch["discovery_plan_ref"], approved["id"])
        after.settle_dispatches()
        # Once: the rendered plan's own cadence holds from here.
        again = after.launch_discovery()
        self.assertEqual(again["status"], "idle", again)
        self.assertIn("rediscovered", again["skipped"][0]["reason"])

    def test_an_unchanged_form_set_searches_nothing_new(self) -> None:
        approved = self.approved(code_query_variants(TRANSCRIPTS, "meeting_minutes"))
        self.search_once(approved)
        self.clock.advance(hours=3)
        rendered = effective_discovery_plan(approved)
        self.assertEqual(rendered["content_hash"], approved["content_hash"])
        coordinator, search = self.coordinator(rendered)
        self.assertEqual(coordinator.launch_discovery()["status"], "idle")
        self.assertEqual(search.starts, [])

    def test_the_first_tick_ledger_says_which_forms_once(self) -> None:
        approved = self.approved(LEGACY_V2_SPECS[0]["query_variants"])
        note = query_forms_ledger_note(query_form_report(approved))
        coordinator, _ = self.coordinator(effective_discovery_plan(approved), query_forms=note)
        first = discovery_coordinator_state(coordinator.dispatch_once())
        self.assertEqual(first["query_forms"], "code")
        self.assertIn("query_variants ignored for earnings-call-transcripts",
                      first["query_forms_note"])
        coordinator.settle_dispatches()
        second = discovery_coordinator_state(coordinator.dispatch_once())
        self.assertNotIn("query_forms", second)


if __name__ == "__main__":
    unittest.main()
