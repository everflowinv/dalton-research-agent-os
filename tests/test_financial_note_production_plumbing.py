from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, materialize_financial_statement_structure,
)
from dalton_core.company_model_spec import (
    CompanyModelSpecError, build_prompt, spec_from_response,
)
from dalton_core.company_model_cli import run_model_spec
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
    def _completed_typed_with_model_lines(self):
        """One promoted note whose exact 10-K also carries model operands."""

        original = FinancialNoteResolverTests._replace_statement_filing

        def expanded(fixture):
            ingest, _old_hash = original(fixture)
            connection = fixture.store.connection
            filing = connection.execute(
                "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
                (ingest,),
            ).fetchone()
            additions = [
                ("us-gaap:Revenues", "Revenue", "8000000000", "usd"),
                ("cost", "Cost", "4800000000", "usd"),
                ("opex", "Operating expense", "1600000000", "usd"),
                ("operating", "Operating income", "1600000000", "usd"),
                ("interest_income", "Interest income", "10", "usd"),
                ("interest_expense", "Interest expense", "20", "usd"),
                ("pretax", "Pretax income", "1599999990", "usd"),
                ("tax", "Income tax expense", "1599998885", "usd"),
                ("net", "Net income", "1105", "usd"),
                ("nci", "Noncontrolling interest", "1005", "usd"),
                ("parent", "Net income attributable to parent", "100", "usd"),
                ("canada-nci", "Exchangeable NCI adjustment", "5", "usd"),
                ("other-nci", "Other NCI", "7", "usd"),
            ]
            existing = connection.execute(
                "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? "
                "ORDER BY ordinal", (ingest,),
            ).fetchall()
            rows = [{
                "statement": row["statement"], "concept": row["concept"],
                "label": row["label"], "level": row["level"],
                "parent_concept": row["parent_concept"],
                "is_breakdown": bool(row["is_breakdown"]),
                "dimension_axis": row["dimension_axis"],
                "dimension_member": row["dimension_member"],
                "dimension_count": row["dimension_count"],
                "period_start": row["period_start"], "period_end": row["period_end"],
                "value": row["value"], "unit": row["unit"], "balance": row["balance"],
            } for row in existing]
            next(row for row in rows if row["concept"] ==
                 "us-gaap:EarningsPerShareDiluted")["unit"] = "USDPerShare"
            for concept, label, value, unit in additions:
                rows.append({
                    "statement": "income", "concept": concept, "label": label,
                    "level": 0, "parent_concept": None, "is_breakdown": False,
                    "dimension_axis": None, "dimension_member": None,
                    "dimension_count": 0, "period_start": "2024-09-01",
                    "period_end": "2025-08-31", "value": value,
                    "unit": unit, "balance": None,
                })
            body = {
                "company_ref": filing["company_ref"], "cik": filing["cik"],
                "entity_name": filing["entity_name"], "accession": filing["accession"],
                "form": filing["form"], "filed": filing["filed"],
                "report_date": filing["report_date"], "line_count": len(rows),
                "source_record_refs": json.loads(filing["source_record_refs_json"]),
                "governance_ref": filing["governance_ref"],
                "governance_hash": filing["governance_hash"],
            }
            identity = {
                "company_ref": filing["company_ref"], "cik": filing["cik"],
                "accession": filing["accession"], "form": filing["form"],
                "line_count": len(rows),
            }
            new_ingest = "statement-ingest:" + content_hash(identity)[:32]
            hash_rows = [{key: value for key, value in row.items()
                          if key != "dimension_count"} for row in rows]
            filing_hash = content_hash({
                **body, "statement_lines_hash": content_hash(hash_rows),
            })
            authority = CoverageMissionAuthority(fixture.store)
            connection.execute(
                "DROP TRIGGER coverage_mission_statement_lines_no_delete"
            )
            connection.execute(
                "DROP TRIGGER coverage_mission_statement_filings_no_delete"
            )
            with authority._transaction() as cursor:
                cursor.execute(
                    "DELETE FROM coverage_mission_statement_lines WHERE ingest_id=?", (ingest,),
                )
                cursor.execute(
                    "DELETE FROM coverage_mission_statement_filings WHERE ingest_id=?", (ingest,),
                )
                cursor.execute(
                    "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_ingest, filing["dispatch_id"], filing["company_ref"], filing["cik"],
                     filing["entity_name"], filing["accession"], filing["form"],
                     filing["filed"], filing["report_date"], len(rows),
                     filing["source_record_refs_json"], filing["governance_ref"],
                     filing["governance_hash"], filing["recorded_at"], filing_hash),
                )
                for ordinal, line in enumerate(rows):
                    cursor.execute(
                        "INSERT INTO coverage_mission_statement_lines("
                        "line_id,ingest_id,statement,ordinal,concept,label,level,parent_concept,"
                        "is_breakdown,dimension_axis,dimension_member,dimension_count,period_start,"
                        "period_end,value,unit,balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{new_ingest}#{ordinal}", new_ingest, line["statement"], ordinal,
                         line["concept"], line["label"], line["level"],
                         line["parent_concept"], int(line["is_breakdown"]),
                         line["dimension_axis"], line["dimension_member"],
                         line["dimension_count"], line["period_start"], line["period_end"],
                         line["value"], line["unit"], line["balance"]),
                    )
            return new_ingest, filing_hash

        helper = FinancialNoteResolverTests(
            "test_exact_typed_promoted_note_resolves_without_numeric_invention"
        )
        self.addCleanup(helper.doCleanups)
        with patch.object(
            FinancialNoteResolverTests, "_replace_statement_filing",
            staticmethod(expanded),
        ):
            return helper._completed_typed()

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

    def test_offline_promoted_note_runs_through_cli_persistence_and_actual_calculation(self):
        fixture, executor, admission, _target = self._completed_typed_with_model_lines()
        full = __import__(
            "dalton_core.financial_note_evidence",
            fromlist=["resolve_financial_note_evidence"],
        ).resolve_financial_note_evidence(
            core_connection=fixture.store.connection,
            router_connection=fixture.router.connection,
            staging_connection=executor.staging.connection,
            registry=executor.registry,
            admission_ref=admission["id"],
        )
        binding = financial_note_evidence_binding(full)
        context = _context(binding)
        inputs, proposal = note_backed_eps_inputs_and_proposal()
        next(item for item in proposal["lines"]
             if item["concept"] == "revenue")["concept"] = "us-gaap:Revenues"
        next(item for item in proposal["lines"]
             if item["concept"] == "shares")["concept"] = (
                 "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
             )
        next(item for item in proposal["formulas"]
             if item["output_ref"] == "eps")["tie_out_concept"] = (
                 "us-gaap:EarningsPerShareDiluted"
             )
        next(item for item in proposal["lines"]
             if item["ref"] == "eps")["unit"] = "usd_per_share"
        note_formula = next(item for item in proposal["formulas"]
                            if item["output_ref"] == "eps-numerator")
        note_formula["evidence_refs"] = [binding["accession"], binding["ref"]]
        for formula in proposal["formulas"]:
            formula["evidence_refs"] = [
                binding["accession"] if ref == "0000000001-26-000001" else ref
                for ref in formula["evidence_refs"]
            ]
        response = _spec_body()
        response["revenue_anchor_concept"] = "us-gaap:Revenues"
        response["expense_lines"] = [{
            "ref": "cost", "label": "Cost", "basis_concept": "cost",
            "behaviour": "variable_with_revenue", "driver_ref": "heads",
            "because": "The issuer's filed cost follows revenue.",
        }, {
            "ref": "opex", "label": "Operating expense", "basis_concept": "opex",
            "behaviour": "fixed", "driver_ref": None,
            "because": "The issuer files this operating cost separately.",
        }]
        response["financial_statement_structure"] = {
            key: copy.deepcopy(proposal[key])
            for key in ("schema_version", "lines", "formulas")
        }

        class NoteContext:
            def projection(self, company_ref):
                self.assert_company = company_ref
                return context

            def resolver(self, ref, *, expected_context=None):
                if expected_context != context or ref != binding["ref"]:
                    raise FinancialNoteContextError("foreign note replay")
                return binding

            def close(self):
                return None

        class FakeModel:
            def __init__(self, config, **_kwargs):
                self.config = config

            def call(self, *, prompt, **_kwargs):
                self.__class__.prompt = prompt
                return {
                    "text": json.dumps(response), "replayed": False,
                    "cost_micros": 0, "work_order_ref": "work:test",
                    "work_order_hash": "1" * 64,
                    "result_envelope_ref": "result:test",
                    "result_envelope_hash": "2" * 64,
                    "invocation_ref": "invocation:test",
                    "route_decision_ref": "route:test",
                }

        state_dir = Path(fixture.store.connection.execute(
            "PRAGMA database_list"
        ).fetchone()[2]).parent
        with tempfile.TemporaryDirectory() as output:
            model_config = Path(output) / "model.json"
            model_config.write_text(json.dumps({
                "structured_output_repair": {"max_attempts": 0},
            }), encoding="utf-8")
            with patch(
                "dalton_core.financial_note_context.FinancialNoteReadContext",
                return_value=NoteContext(),
            ), patch("dalton_core.company_model_cli.CockpitModel", FakeModel):
                summary = run_model_spec(
                    state_dir=state_dir, model_config_path=model_config,
                    summary_dir=Path(output) / "summary", scheduler_db=None,
                    company_ref=admission["company_ref"],
                )
        self.assertEqual(summary["spec_status"], "fresh", summary)
        self.assertIn(binding["ref"], FakeModel.prompt)
        stored = CoverageMissionAuthority(fixture.store).latest_company_model_spec(
            admission["company_ref"]
        )
        self.assertEqual(
            stored["financial_statement_structure"]["note_evidence"], [binding],
        )
        actuals = summary["pre_persistence_validation"]
        self.assertEqual(actuals, "unavailable")
        structure, replay = materialize_financial_statement_structure(
            stored,
            __import__("dalton_core.company_model_inputs", fromlist=[
                "build_model_inputs"
            ]).build_model_inputs(
                CoverageMissionAuthority(fixture.store), stored,
            ),
            note_evidence_resolver=lambda ref: binding if ref == binding["ref"] else None,
        )
        numerator = next(item for item in replay["formulas"]
                         if item["output_ref"] == "eps-numerator")
        annual = next(item for item in replay["note_formula_periods"]
                      if item["output_ref"] == "eps-numerator")
        self.assertEqual(annual["periods"][0]["value"], "105", annual)
        self.assertEqual(annual["periods"][0]["status"], "validated")
        self.assertEqual(numerator["status"], "unavailable")
        self.assertEqual(numerator["tested_periods"], [])
        self.assertFalse(replay["ready_for_forecast"])


if __name__ == "__main__":
    unittest.main()
