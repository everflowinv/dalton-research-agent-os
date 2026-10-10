"""Synthetic table layouts; no licensed broker text and no paid transport."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dalton_core.store import content_hash, DaltonStore
from dalton_core.street_estimate_table_model import (prepare_request, extract_verified,
    TableModelRefused, EXTRACTOR_REF)
from dalton_core.street_estimate_tables import StreetEstimateFigureStore, _from_model
from dalton_core.street_estimate_table_cli import pending_notes

TEXT = "Example Company\nExample Broker\nHighlights (US$m)\n08/27E\nRevenues\n125\nEPS (US$)\n3.25\n"
MISSION = {"id": "mission:v1", "universe": [{"company_ref": "company:example"}],
           "autonomy": {"may_write": ["consensus_estimate"]}}


def request(text=TEXT):
    return prepare_request(text, company_ref="company:example", document_ref="doc:example",
        house="example-broker", subject_as_named="Example Company",
        source_sha256=hashlib.sha256(text.encode()).hexdigest(), published_on="2026-10-10", fye_month=8)


def response(req):
    return {"schema_version": "0.1", "estimates": [
        {"quote_id": req["quotes"][0]["quote_id"], "metric_ref": "metric:eps-estimate",
         "as_reported_label": "EPS (US$)", "period": "FY2027", "value": "3.25",
         "unit": "currency", "currency": "USD", "scale": None, "superseded": False},
        {"quote_id": req["quotes"][0]["quote_id"], "metric_ref": "metric:revenue-estimate",
         "as_reported_label": "Revenues", "period": "FY2027", "value": "125",
         "unit": "currency", "currency": "USD", "scale": "million", "superseded": False}]}


class Model:
    def __init__(self, answer=None, transform=None):
        self.answer, self.transform, self.calls = answer, transform, []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.answer is not None:
            return {"text": json.dumps(self.answer), "route_decision_ref": "producer", "cost_micros": 0}
        wire = json.loads(kwargs["prompt"].split("\n")[-1])
        req, candidate = wire["request"], wire["candidate"]
        indices = [i for i, row in enumerate(candidate["estimates"]) if not row["superseded"]]
        verdict = {"verdict": "pass", "company_ref": req["company_ref"], "document_ref": req["document_ref"],
            "source_sha256": req["source_sha256"], "candidate_hash": content_hash(candidate),
            "checked_rows": indices, "reason": "Each current estimate maps to the printed annual column.",
            "cell_bindings": [{"row_index": i, "period_as_printed": "08/27E", "period_kind": "fiscal_year",
                "currency_as_printed": "US$", "scale_as_printed": ("US$m" if row["scale"] else None),
                "unit_as_printed": row["as_reported_label"]} for i, row in enumerate(candidate["estimates"]) if not row["superseded"]]}
        if self.transform:
            self.transform(verdict)
        return {"text": json.dumps(verdict), "route_decision_ref": "verifier", "cost_micros": 0}


class VerifiedTableTests(unittest.TestCase):
    def test_actual_cockpit_budget_preserves_smaller_shared_caps_and_rereads_changed_policy(self):
        from dalton_core.street_estimate_table_model import bounded_model_config
        from dalton_core.cockpit_model import CockpitModel
        from tests.test_shared_call_budget_policy import SharedCallBudgetPolicyTests
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            path = state/"shared.json"
            raw = {"routing_policy_ref": "policy:1", "credential_slot_refs": ["slot:1"],
                "model_router_db": str(state/"router.sqlite"), "broker_socket": str(state/"broker.sock"),
                "broker_auth_key": str(state/"key"), "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                "budget_db": str(state/"budget.sqlite"), "budget_policy_ref": "budget:original",
                "shared_call_budget_policy_path": str(path),
                "purpose_call_budgets": {"street_estimate": {"max_input_tokens": 20000, "max_output_tokens": 1000, "timeout_seconds": 60}}}
            hashes = []
            for cost, expected in ((1, .25), (.04, .04), (.8, .25)):
                policy = SharedCallBudgetPolicyTests().policy(default_max_cost_usd=cost, purpose_max_cost_usd={})
                path.write_text(json.dumps(policy))
                projected, binding = bounded_model_config(raw, "street_estimate")
                model = CockpitModel(projected, scheduler_db=state/"scheduler.sqlite")
                self.assertEqual(model.budget_for("street_estimate"), {"max_cost_usd": expected,
                    "max_input_tokens": 20000, "max_output_tokens": 1000, "timeout_seconds": 60})
                self.assertEqual(projected["budget_policy_ref"], "budget:original")
                self.assertEqual(projected["budget_db"], raw["budget_db"])
                self.assertEqual(binding["owner_budget"]["max_cost_usd"], min(.25, cost))
                self.assertEqual(binding["shared_max_cost_usd"], cost)
                self.assertEqual(binding["shared_policy_hash"], policy["content_hash"])
                hashes.append(binding["shared_policy_hash"])
            self.assertEqual(len(set(hashes)), 3)
            for configured, shared in ((.1, 1), (1, .1)):
                policy = SharedCallBudgetPolicyTests().policy(default_max_cost_usd=shared, purpose_max_cost_usd={})
                path.write_text(json.dumps(policy))
                raw["purpose_call_budgets"]["street_estimate"]["max_cost_usd"] = configured
                projected, binding = bounded_model_config(raw, "street_estimate")
                self.assertEqual(CockpitModel(projected, scheduler_db=state/"scheduler.sqlite").budget_for("street_estimate")["max_cost_usd"], .1)

    def execute(self, answer=None, transform=None, families=None, mission=None):
        req = request()
        p, v = Model(answer or response(req)), Model(transform=transform)
        out = extract_verified(req, producer=p, verifier=v, mission=mission or MISSION,
            family_of=lambda r: (families or {"producer": "family:a", "verifier": "family:b"}).get(r))
        return out, p, v

    def test_bound_original_and_scaled_revenue_are_recorded_only_after_two_calls(self):
        out, p, v = self.execute()
        self.assertEqual(out["status"], "model_recorded")
        self.assertEqual([f["value"] for f in out["figures"]], ["3.25", "125000000"])
        self.assertEqual(len(p.calls), 1)
        self.assertEqual(len(v.calls), 1)
        self.assertEqual(v.calls[0]["producer_route_decision_refs"], ["producer"])
        self.assertIn(TEXT, json.loads(p.calls[0]["prompt"].split("\n")[-1])["quotes"][0]["raw_text"])
        self.assertEqual(out["proof"]["source_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())

    def test_same_family_and_unknown_family_fail_closed(self):
        for families in ({"producer": "a", "verifier": "a"}, {"producer": "a"},
                         {"producer": "a", "verifier": "unclassified:b"}):
            with self.subTest(families=families), self.assertRaises(TableModelRefused):
                self.execute(families=families)

    def test_checker_must_bind_every_identity_and_every_cell(self):
        mutations = [lambda v: v.update(document_ref="doc:other"),
            lambda v: v.update(company_ref="company:peer"), lambda v: v.update(source_sha256="0"*64),
            lambda v: v.update(candidate_hash="0"*64), lambda v: v.update(checked_rows=[0]),
            lambda v: v.update(cell_bindings=v["cell_bindings"][:1]), lambda v: v.update(verdict="reject"),
            lambda v: v.update(reason=""), lambda v: v.update(reason=" \t "), lambda v: v.update(extra=True)]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(TableModelRefused):
                self.execute(transform=change)

    def test_wrong_year_currency_scale_and_eps_unit_cannot_pass_a_checker(self):
        changes = [("period_as_printed", "FY2028"), ("period_kind", "quarter"),
                   ("currency_as_printed", "EUR"), ("unit_as_printed", "Revenues")]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(TableModelRefused):
                self.execute(transform=lambda v: v["cell_bindings"][0].update({key: value}))
        with self.assertRaises(TableModelRefused):
            self.execute(transform=lambda v: v["cell_bindings"][1].update(scale_as_printed="US$b"))

    def test_bad_metric_actual_duplicate_and_double_scaled_number_reject_before_verifier(self):
        changes = [lambda r: r[0].update(metric_ref="metric:unknown"),
            lambda r: r[0].update(period="FY2025"), lambda r: r[0].update(scale="million"),
            lambda r: r[1].update(value="125000000"), lambda r: r.append(copy.deepcopy(r[0]))]
        for change in changes:
            req = request(); answer = response(req); change(answer["estimates"])
            p, v = Model(answer), Model()
            with self.subTest(change=change), self.assertRaises(ValueError):
                extract_verified(req, producer=p, verifier=v, mission=MISSION, family_of=lambda r: r)
            self.assertEqual(v.calls, [])

    def test_ungranted_mission_never_calls_either_model(self):
        p, v = Model(response(request())), Model()
        with self.assertRaises(TableModelRefused):
            extract_verified(request(), producer=p, verifier=v, mission={"autonomy": {}}, family_of=lambda r: r)
        self.assertEqual(p.calls + v.calls, [])

    def test_original_hash_and_company_name_are_checked_before_request_exists(self):
        params = dict(company_ref="company:example", document_ref="doc:example", house="broker",
            subject_as_named="Example Company", source_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
            published_on="2026-10-10", fye_month=8)
        for change in ({"source_sha256": "0"*64}, {"subject_as_named": "Peer Name"},
                       {"published_on": "2026-99-40"}, {"fye_month": True}):
            with self.subTest(change=change), self.assertRaises(TableModelRefused):
                prepare_request(TEXT, **{**params, **change})

    def test_long_original_has_exact_bounded_scope_without_changing_full_source_hash(self):
        text = TEXT + "irrelevant disclosure\n"*7000
        req = request(text)
        w = req["source_window"]
        self.assertTrue(w["truncated"])
        self.assertLessEqual(w["end"], 16_000)
        self.assertEqual(req["quotes"][0]["raw_text"], text[w["start"]:w["end"]])
        self.assertEqual(w["sha256"], hashlib.sha256(req["quotes"][0]["raw_text"].encode()).hexdigest())

    def test_quarter_conversion_does_not_read_quarter_number_as_the_year(self):
        item = response(request())["estimates"][0]
        item["period"] = "3Q27E"
        f = _from_model(item, 8)
        self.assertEqual((f["period"], f["period_end"], f["period_kind"]), ("FY2027Q3", "2027-05-31", "quarter"))


class AppendOnlyFallbackTests(unittest.TestCase):
    def _child(self, *, grant=True, excessive=False):
        from dalton_core.street_estimate_table_cli import run_table
        from dalton_core.street_estimate_table_model import MODEL_CONFIG, VERIFIER_CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with DaltonStore(str(state/"core.sqlite")) as core:
                core.connection.execute("CREATE TABLE coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT)")
                core.connection.execute("INSERT INTO coverage_mission_pointer VALUES ('m','m:1')")
                StreetEstimateFigureStore(core)
            for name in (MODEL_CONFIG, VERIFIER_CONFIG):
                (state/name).write_text(json.dumps({"routing_policy_ref": "policy:1", "credential_slot_refs": ["slot:1"],
                    "model_router_db": str(state/"router.sqlite"), "broker_socket": str(state/"broker.sock"),
                    "broker_auth_key": str(state/"key"), "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                    "budget_db": str(state/"budget.sqlite"), "budget_policy_ref": "budget:1"}))
            models = [Model(response(request())), Model()]
            for model in models:
                model.budget_for = lambda purpose: {"max_cost_usd": 1 if excessive else .25,
                    "max_input_tokens": 24_000, "max_output_tokens": 3000}
            made = []
            def factory(config, **kwargs):
                self.assertEqual(config["provider_retry"]["max_same_profile_retries"], 0)
                model = models[len(made)]; made.append(model); return model
            mission = copy.deepcopy(MISSION)
            if not grant:
                mission["autonomy"]["may_write"] = []
            with patch("dalton_core.coverage_mission.CoverageMissionAuthority.mission", return_value=mission), \
                 patch("dalton_core.street_estimate_table_cli.request_for", return_value=request()):
                result = run_table(state_dir=state, company_ref="company:example", document_ref="doc:example",
                    expected_request_hash=content_hash(request()), model_factory=factory,
                    family_of=lambda r: {"producer": "a", "verifier": "b"}.get(r))
            with DaltonStore(str(state/"core.sqlite")) as core:
                rows = core.connection.execute("SELECT record_json FROM street_estimate_figures").fetchall()
            return result, made, models, rows

    def test_child_executes_and_stores_the_same_verified_original_binding(self):
        result, made, models, rows = self._child()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(rows), 2)
        self.assertEqual(result["proof"]["request_hash"], content_hash(request()))
        self.assertEqual(len(models[0].calls) + len(models[1].calls), 2)

    def test_child_refuses_ungranted_or_excessive_budget_before_transport(self):
        for options in ({"grant": False}, {"excessive": True}):
            with self.subTest(options=options):
                result, made, models, rows = self._child(**options)
                self.assertEqual(result["status"], "refused")
                self.assertEqual(models[0].calls + models[1].calls, [])
                self.assertEqual(rows, [])

    def test_only_needs_model_is_reoffered_and_old_scan_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, DaltonStore(str(Path(tmp)/"core.sqlite")) as core:
            core.connection.execute("CREATE TABLE street_estimates (company_ref TEXT, document_ref TEXT, broker TEXT, published_on TEXT, source_manifest_hash TEXT)")
            figures = StreetEstimateFigureStore(core)
            for doc, outcome in (("doc:gap", "needs_model"), ("doc:good", "recorded"), ("doc:none", "none_found")):
                core.connection.execute("INSERT INTO street_estimates VALUES (?,?,?,?,?)", ("company:example", doc, "broker", "2026-10-10", "x"))
                figures.record_document(company_ref="company:example", document_ref=doc, house="broker", published_on="2026-10-10", figures=[], outcome=outcome)
            old = core.connection.execute("SELECT record_json FROM street_estimate_figure_scans WHERE document_ref='doc:gap'").fetchone()[0]
            self.assertEqual([n["document_ref"] for n in pending_notes(core.connection, "company:example")], ["doc:gap"])
            out, _, _ = VerifiedTableTests().execute()
            figures.record_document(company_ref="company:example", document_ref="doc:gap", house="broker", published_on="2026-10-10",
                figures=out["figures"], outcome="model_recorded", extraction_method="model", extractor_ref=EXTRACTOR_REF)
            self.assertEqual(pending_notes(core.connection, "company:example"), [])
            self.assertEqual(core.connection.execute("SELECT record_json FROM street_estimate_figure_scans WHERE document_ref='doc:gap' AND outcome='needs_model'").fetchone()[0], old)
            saved = json.loads(core.connection.execute("SELECT record_json FROM street_estimate_figures LIMIT 1").fetchone()[0])
            self.assertIn("model_proof", saved)

    def test_real_selector_launches_old_gap_once_and_does_not_call_a_model_on_writer_thread(self):
        from dalton_core.mission_consensus_lane import _table_scanner
        with tempfile.TemporaryDirectory() as tmp, DaltonStore(str(Path(tmp)/"core.sqlite")) as core:
            core.connection.execute("CREATE TABLE coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT)")
            core.connection.execute("INSERT INTO coverage_mission_pointer VALUES ('m','m:1')")
            server = SimpleNamespace(state_dir=Path(tmp), store=core)
            launcher = SimpleNamespace(configured=True, configuration_identity=lambda: "fixture-config",
                start=unittest.mock.Mock(return_value={"id": "ticket", "status": "running"}))
            with patch("dalton_core.street_estimate_table_launcher.StreetEstimateTableLauncher", return_value=launcher), \
                 patch("dalton_core.street_estimate_table_cli.pending_notes", return_value=[{"document_ref": "doc:example"}]), \
                 patch("dalton_core.street_estimate_table_cli.request_for", return_value=request()), \
                 patch("dalton_core.financial_period.fiscal_year_end", return_value=None):
                scan = _table_scanner(server)
                universe = [{"company_ref": "company:example", "ticker": "EX"}]
                self.assertEqual(scan(universe)["outcome"], "table_model_launched")
                self.assertIsNone(scan(universe))
                launcher.start.assert_called_once()

    def test_existing_workspace_pair_install_is_idempotent_and_preserves_budget_pins(self):
        from tests.test_document_extraction_setup import _service
        from dalton_core.model_deployment import openclaw_broker_profiles
        from dalton_core.model_router import ModelRouter
        from dalton_core.street_estimate_table_setup import configure
        from dalton_core.street_estimate_table_model import MODEL_CONFIG, VERIFIER_CONFIG
        from dalton_core.model_selection import purpose_policy_bindings
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _service(root)
            with ModelRouter(str(root/"state/model-router.sqlite")) as router:
                for profile in openclaw_broker_profiles(checked_at=datetime.now(timezone.utc)):
                    router.register_profile(profile)
            configure(str(service), producer_tier="cheap", verifier_tier="verifier")
            before = [(root/"state"/name).read_bytes() for name in (MODEL_CONFIG, VERIFIER_CONFIG)]
            again = configure(str(service), producer_tier="cheap", verifier_tier="verifier")
            self.assertTrue(all(not r["model_config_changed"] for r in again["configurations"]))
            self.assertEqual(before, [(root/"state"/name).read_bytes() for name in (MODEL_CONFIG, VERIFIER_CONFIG)])
            for name in (MODEL_CONFIG, VERIFIER_CONFIG):
                wire = json.loads((root/"state"/name).read_text())
                self.assertEqual(wire["budget_policy_ref"], "thesis-impact-day-budget-policy:production:1")
            bindings = purpose_policy_bindings(root/"state")
            self.assertEqual(bindings["street_estimate"]["status"], "configured")
            self.assertTrue(bindings["street_estimate"]["editable"])
            self.assertEqual(bindings["street_estimate_verifier"]["status"], "configured")
