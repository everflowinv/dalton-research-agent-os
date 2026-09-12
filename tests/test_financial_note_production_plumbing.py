from __future__ import annotations

import copy
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, materialize_financial_statement_structure,
)
from dalton_core.company_model_spec import (
    CompanyModelSpecError, build_prompt, spec_from_response,
)
from dalton_core.company_model_state import build_company_model_state
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.financial_note_context import (
    FinancialNoteContextError, FinancialNoteReadContext,
)
from dalton_core.financial_note_evidence import financial_note_evidence_binding
from dalton_core.store import content_hash
from dalton_core.store import DaltonStore
from dalton_core.company_model_forecast import run_company_forecast
from dalton_core.company_model_forecast import model_digest
from dalton_core.model_forecast_driver import ForecastModelAuthority
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_company_financial_statement_structure import (
    note_backed_eps_inputs_and_proposal, presentation_state, typed_note,
)
from tests.test_company_model_cli import _spec_body
from tests.test_financial_note_evidence import FinancialNoteResolverTests


def _context_record(binding):
    body = {
        "authority_ref": binding["ref"],
        "authority_hash": binding["content_hash"],
        "binding": binding,
        "normalized_statement": "Only the exchangeable NCI amount enters the numerator.",
        "registration_ref": "registered-document:test",
        "registration_hash": "1" * 64,
        "source_authority_ref": "acquired:test",
        "source_authority_hash": "2" * 64,
        "source_content_hash": "3" * 64,
        "search_proof_ref": "document-search-proof:test",
        "search_proof_hash": "4" * 64,
        "passages": [{"source_start": 10, "source_end": 20,
                       "excerpt": "exchangeable", "excerpt_sha256": "5" * 64}],
    }
    return {**body, "content_hash": content_hash(body)}


def _context(binding):
    body = {
        "schema_version": "company-model-financial-note-context-0.1",
        "company_ref": binding["company_ref"],
        "config_hash": "6" * 64,
        "config_file_sha256": "7" * 64,
        "records": [_context_record(binding)],
    }
    return {**body, "content_hash": content_hash(body)}


class FinancialNoteContextFactoryTests(unittest.TestCase):
    def test_empty_promotions_do_not_open_config_registry_or_side_databases(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE mission_document_research_admissions("
            "admission_id TEXT,company_ref TEXT,record_json TEXT,content_hash TEXT);"
            "CREATE TABLE mission_document_research_promotions("
            "admission_ref TEXT);"
        )
        store = type("Store", (), {"connection": connection})()
        context = FinancialNoteReadContext(store=store, state_dir="/not/opened")
        with patch(
            "dalton_core.financial_note_context._read_config",
            side_effect=AssertionError("empty path opened configuration"),
        ):
            self.assertIsNone(context.projection("company:test"))
        connection.close()

    def test_real_promoted_authority_projects_and_replays_without_manual_binding(self):
        helper = FinancialNoteResolverTests(
            "test_exact_typed_promoted_note_resolves_without_numeric_invention"
        )
        self.addCleanup(helper.doCleanups)
        fixture, executor, admission, _target = helper._completed_typed()
        config = {"test": "already-validated"}
        config_file_hash = "8" * 64
        context = FinancialNoteReadContext(store=fixture.store, state_dir=fixture.state)
        context._config = config
        context._config_file_sha256 = config_file_hash
        context._registry = executor.registry
        context._router = fixture.router.connection
        context._staging = executor.staging.connection
        with patch(
            "dalton_core.financial_note_context._read_config",
            return_value=(config, config_file_hash),
        ):
            projected = context.projection(admission["company_ref"])
            self.assertEqual(len(projected["records"]), 1)
            binding = projected["records"][0]["binding"]
            self.assertEqual(binding["ref"], "financial-note-evidence:" + admission["id"])
            self.assertEqual(
                context.resolver(binding["ref"], expected_context=projected), binding,
            )
            substituted = copy.deepcopy(projected)
            substituted["records"][0]["binding"]["periods"][0]["period_end"] = (
                "2025-08-30"
            )
            record = substituted["records"][0]
            record["content_hash"] = content_hash({
                key: value for key, value in record.items() if key != "content_hash"
            })
            substituted["content_hash"] = content_hash({
                key: value for key, value in substituted.items() if key != "content_hash"
            })
            with self.assertRaisesRegex(
                FinancialNoteContextError, "authority differs",
            ):
                context.resolver(binding["ref"], expected_context=substituted)
        # These connections belong to the fixture; context must not close them.
        context._router = context._staging = None

    def test_config_drift_is_refused_before_an_authority_can_be_reused(self):
        binding = typed_note()
        context = FinancialNoteReadContext(
            store=type("Store", (), {"connection": sqlite3.connect(":memory:")})(),
            state_dir="/unused",
        )
        context._config = {"version": 1}
        context._config_file_sha256 = "a" * 64
        with patch(
            "dalton_core.financial_note_context._read_config",
            return_value=({"version": 2}, "b" * 64),
        ), self.assertRaisesRegex(FinancialNoteContextError, "configuration changed"):
            context.resolver(binding["ref"], expected_context=_context(binding))
        context.store.connection.close()


class FinancialNoteModelPlumbingTests(unittest.TestCase):
    def _filed_mission(self, inputs):
        import tempfile
        from pathlib import Path

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = DaltonStore(str(Path(temporary.name) / "core.sqlite"))
        self.addCleanup(store.close)
        authority = CoverageMissionAuthority(store)
        params = mission_params(bootstrap_method_authorities(store))
        params["universe"] = [{
            "company_ref": "company:test", "ticker": "TEST",
            "coverage_tier": "A", "bootstrap_priority": "P0",
        }]
        mission = authority.create_mission(params.pop("mission_ref"), **params)
        authorization = authority.authorize_sec_lane(
            company_ref="company:test", ticker="TEST",
            actor_ref=mission["autonomy"]["automation_principal"],
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
        )
        dispatch = authority.queue_statement_dispatch(
            authorization=authorization, form="10-K",
        )
        authority.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "f" * 24,
        )
        rows = []
        for line in inputs["filed_lines"]:
            for period_end, cell in sorted(line["cells"].items()):
                rows.append({
                    "statement": "income", "concept": line["concept"],
                    "label": line["label"], "level": 0, "parent_concept": None,
                    "is_breakdown": False, "dimension_axis": None,
                    "dimension_member": None,
                    "period_start": cell.get("period_start"),
                    "period_end": period_end, "value": cell["value"],
                    "unit": cell["unit"], "balance": None,
                })
        authority.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0000000001",
                "entity_name": "Test issuer", "filings": [{
                    "accession": "0000000001-26-000001", "form": "10-K",
                    "filed": "2025-02-01", "report_date": "2024-12-31",
                    "lines": rows,
                }],
                "source_record_refs": ["raw-sink:" + "a" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="governance:test", governance_hash="b" * 64,
        )
        authority.settle_statement_dispatch(dispatch["dispatch_id"], outcome="succeeded")
        return store, authority, mission

    def test_note_context_is_hashed_and_counted_in_the_whole_prompt(self):
        helper = FinancialNoteResolverTests(
            "test_exact_typed_promoted_note_resolves_without_numeric_invention"
        )
        self.addCleanup(helper.doCleanups)
        fixture, executor, admission, _target = helper._completed_typed()
        full = __import__(
            "dalton_core.financial_note_evidence", fromlist=["resolve_financial_note_evidence"]
        ).resolve_financial_note_evidence(
            core_connection=fixture.store.connection,
            router_connection=fixture.router.connection,
            staging_connection=executor.staging.connection,
            registry=executor.registry,
            admission_ref=admission["id"],
        )
        binding = financial_note_evidence_binding(full)
        note_context = _context_record(binding)
        body = {
            "schema_version": "company-model-financial-note-context-0.1",
            "company_ref": admission["company_ref"], "config_hash": "6" * 64,
            "config_file_sha256": "7" * 64, "records": [note_context],
        }
        note_context = {**body, "content_hash": content_hash(body)}
        missions = CoverageMissionAuthority(fixture.store)
        state = build_company_model_state(
            missions, admission["company_ref"], financial_note_context=note_context,
        )
        self.assertEqual(state["financial_note_context"]["records"][0]["binding"], binding)
        self.assertEqual(
            state["numeric_context"]["prompt_bytes"],
            len(build_prompt(state).encode("utf-8")),
        )
        self.assertIn("exchangeable", build_prompt(state))

    def test_forecast_digest_binds_exact_note_prompt_and_configuration(self):
        binding = typed_note()
        first = _context(binding)
        second = copy.deepcopy(first)
        second["config_file_sha256"] = "8" * 64
        second["content_hash"] = content_hash({
            key: value for key, value in second.items() if key != "content_hash"
        })
        spec = {
            "schema_version": "0.3", "company_ref": "company:test",
            "spec_id": "company-model-spec:test", "content_hash": "9" * 64,
            "financial_statement_structure": {"note_evidence": [binding]},
        }
        structure = {"schema_version": "financial-statement-structure-0.3"}
        replay = {"ready_for_forecast": True}
        with patch(
            "dalton_core.company_model_forecast."
            "materialize_financial_statement_structure",
            return_value=(structure, replay),
        ), patch(
            "dalton_core.company_model_forecast.forecast_structure_binding",
            return_value={"content_hash": "a" * 64},
        ), patch(
            "dalton_core.company_model_forecast.structure_formula_hash",
            return_value="b" * 64,
        ):
            first_digest = model_digest(
                spec, {}, financial_note_context=first,
                note_evidence_resolver=lambda _ref: binding,
            )
            second_digest = model_digest(
                spec, {}, financial_note_context=second,
                note_evidence_resolver=lambda _ref: binding,
            )
        self.assertNotEqual(first_digest, second_digest)

    def test_parse_persisted_definition_uses_only_cited_binding_and_annual_stays_unavailable(self):
        inputs, proposal = note_backed_eps_inputs_and_proposal()
        binding = typed_note()
        next(item for item in inputs["filed_lines"]
             if item["concept"] == "revenue")["concept"] = "us-gaap:Revenues"
        next(item for item in proposal["lines"]
             if item["concept"] == "revenue")["concept"] = "us-gaap:Revenues"
        state = presentation_state(inputs)
        state.update({
            "ticker": "TEST", "entity_name": "Test", "cik": "1",
            "concepts": sorted(line["concept"] for line in inputs["filed_lines"]),
            "numeric_context": {
                "filing_authorities": [{
                    "ingest_id": binding["statement_ingest_ref"],
                    "accession": binding["accession"], "form": binding["form"],
                    "content_hash": binding["statement_filing_hash"],
                }],
            },
            "financial_note_context": _context(binding),
        })
        other_binding = {
            **binding, "ref": "financial-note-evidence:unreferenced",
            "content_hash": "8" * 64,
        }
        held_context = state["financial_note_context"]
        held_context["records"].append(_context_record(other_binding))
        held_context["records"].sort(key=lambda item: item["authority_ref"])
        held_context["content_hash"] = content_hash({
            key: value for key, value in held_context.items() if key != "content_hash"
        })
        state["state_hash"] = content_hash({
            key: value for key, value in state.items() if key != "state_hash"
        })
        response = _spec_body()
        response["revenue_anchor_concept"] = "us-gaap:Revenues"
        response["expense_lines"] = [{
            "ref": "cost", "label": "Cost", "basis_concept": "cost",
            "behaviour": "variable_with_revenue", "driver_ref": "heads",
            "because": "This filed cost follows activity.",
        }, {
            "ref": "opex", "label": "Operating expense", "basis_concept": "opex",
            "behaviour": "fixed", "driver_ref": None,
            "because": "This filed expense has a separate cost base.",
        }]
        response["financial_statement_structure"] = {
            key: copy.deepcopy(proposal[key])
            for key in ("schema_version", "lines", "formulas")
        }
        calls = []

        def resolver(ref):
            calls.append(ref)
            return binding if ref == binding["ref"] else None

        spec = spec_from_response(
            state, response, decided_by="automation:test",
            note_evidence_resolver=resolver,
        )
        with self.assertRaisesRegex(
            CompanyModelSpecError, "configuration changed after the model call",
        ):
            spec_from_response(
                state, response, decided_by="automation:test",
                note_evidence_resolver=lambda _ref: (_ for _ in ()).throw(
                    FinancialNoteContextError(
                        "configuration changed after the model call"
                    )
                ),
            )
        spec["spec_id"] = "company-model-spec:test"
        self.assertEqual(spec["financial_statement_structure"]["note_evidence"], [binding])
        self.assertEqual(set(calls), {binding["ref"]})
        structure, replay = materialize_financial_statement_structure(
            spec, inputs, note_evidence_resolver=resolver,
        )
        self.assertEqual(structure["note_evidence"], [binding])
        self.assertFalse(replay["ready_for_forecast"])
        numerator = next(item for item in replay["formulas"]
                         if item["output_ref"] == "eps-numerator")
        self.assertEqual(numerator["status"], "unavailable")

    def test_actual_mission_parse_persist_and_forecast_replay_the_same_note_binding(self):
        inputs, proposal = note_backed_eps_inputs_and_proposal()
        next(item for item in inputs["filed_lines"]
             if item["concept"] == "revenue")["concept"] = "us-gaap:Revenues"
        next(item for item in proposal["lines"]
             if item["concept"] == "revenue")["concept"] = "us-gaap:Revenues"
        store, missions, mission = self._filed_mission(inputs)
        filing = missions.statement_filings("company:test")[0]
        binding = typed_note()
        binding.update({
            "statement_ingest_ref": filing["ingest_id"],
            "statement_filing_hash": filing["content_hash"],
        })
        state = build_company_model_state(
            missions, "company:test", ticker="TEST",
            financial_note_context=_context(binding),
        )
        response = _spec_body()
        response["revenue_anchor_concept"] = "us-gaap:Revenues"
        response["expense_lines"] = [{
            "ref": "cost", "label": "Cost", "basis_concept": "cost",
            "behaviour": "variable_with_revenue", "driver_ref": "heads",
            "because": "The company's filed cost follows revenue.",
        }, {
            "ref": "opex", "label": "Operating expense", "basis_concept": "opex",
            "behaviour": "fixed", "driver_ref": None,
            "because": "The company presents this operating cost separately.",
        }]
        response["financial_statement_structure"] = {
            key: copy.deepcopy(proposal[key])
            for key in ("schema_version", "lines", "formulas")
        }
        resolver = lambda ref: binding if ref == binding["ref"] else None
        spec = spec_from_response(
            state, response, decided_by="automation:test",
            note_evidence_resolver=resolver,
        )
        stored, proof = missions.record_validated_company_model_spec(
            spec, mission_version_ref=mission["id"],
            note_evidence_resolver=resolver,
        )
        self.assertEqual(
            stored["financial_statement_structure"]["note_evidence"], [binding],
        )
        self.assertFalse(proof["financial_replay"]["ready_for_forecast"])
        models = ForecastModelAuthority(store)
        with self.assertRaisesRegex(
            FinancialStatementStructureError, "not ready for forecast",
        ):
            run_company_forecast(
                missions, stored, models=models,
                mission_version_ref=mission["id"],
                note_evidence_resolver=resolver,
            )


if __name__ == "__main__":
    unittest.main()
