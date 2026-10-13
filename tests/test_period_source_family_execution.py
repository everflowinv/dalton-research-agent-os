"""Normal owner input/spec/forecast publication and durable source replay."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

from dalton_core.company_model_spec import spec_from_response
from dalton_core.model_forecast_driver import ForecastModelAuthority, ForecastModelConflict
from dalton_core.forecast_execution_statement_scope import PERIOD_RULE_REF
from dalton_core.company_model_state import build_company_model_state
from dalton_core.company_model_spec import build_prompt
from dalton_core.period_source_family import CONTRACT_REF

AXIS = "us-gaap:StatementBusinessSegmentsAxis"
FIXED = {"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}


class PeriodFamilyExecutionTests(unittest.TestCase):
    def fixture(self, *, static=False, legacy_spec=False):
        from tests.test_company_model_forecast import LaneStateTests, REVENUE_CONCEPT
        test = self
        class Case(LaneStateTests):
            use_legacy = legacy_spec
            def file_quarters(self, *args, **kwargs):
                original = self.missions.record_statement_observation
                def add_members(*, observation, **values):
                    observation = copy.deepcopy(observation)
                    for filing in observation["filings"]:
                        leaves = []
                        revenues = [r for r in filing["lines"] if r["concept"] == REVENUE_CONCEPT]
                        for index, row in enumerate(revenues):
                            fixed = {} if static or index < 2 else FIXED
                            row.update(dimensions={}, dimension_count=0)
                            for member, ratio in (("issuer:FirstMember", "0.6"), ("issuer:SecondMember", "0.4")):
                                leaves.append({**row, "value": str(Decimal(row["value"])*Decimal(ratio)),
                                    "is_breakdown": True, "dimension_axis": AXIS, "dimension_member": member,
                                    "dimensions": {**fixed, AXIS: member}, "dimension_count": len(fixed)+1})
                            if fixed:
                                leaves.append({**row, "is_breakdown": True,
                                    "dimension_axis": next(iter(fixed)), "dimension_member": next(iter(fixed.values())),
                                    "dimensions": fixed, "dimension_count": len(fixed)})
                        filing["lines"].extend(leaves)
                        for row in filing["lines"]:
                            row.setdefault("dimensions", {})
                            row.setdefault("dimension_count", len(row["dimensions"]))
                    return original(observation=observation, **values)
                with patch.object(self.missions, "record_statement_observation", side_effect=add_members):
                    result = super().file_quarters(*args, **kwargs)
                from dalton_core.statement_context import StatementContextAuthority, NORMALIZER_REF, normalizer_hash
                from dalton_core.store import content_hash, canonical_json
                import json
                filing = self.missions.statement_filings()[0]
                rows = self.missions.statement_lines(filing["ingest_id"], latest_context=False)
                mapping = {r["line_id"]: {"dimensions": r.get("dimensions") or {},
                           "dimension_count": len(r.get("dimensions") or {})} for r in rows}
                body = {"ingest_id": filing["ingest_id"], "company_ref": filing["company_ref"],
                        "filing_content_hash": filing["content_hash"], "raw_ref": json.loads(filing["source_record_refs_json"])[0],
                        "normalizer_ref": NORMALIZER_REF, "normalizer_hash": normalizer_hash(),
                        "kind": "ordinal_overlay", "mapping": mapping, "mapping_hash": content_hash(mapping),
                        "status": "available"}
                body["version_id"] = "statement-context:" + content_hash(body)[:32]
                body["content_hash"] = content_hash(body)
                authority = StatementContextAuthority(self.store)
                with authority._transaction() as cur:
                    cur.execute("INSERT INTO statement_context_versions VALUES(?,?,?,?,?,?)",
                        (body["version_id"], filing["ingest_id"], "available", canonical_json(body), body["content_hash"], "2026-10-13T00:00:00+00:00"))
                return result

            def record_spec(self, **kwargs):
                if self.use_legacy:
                    with patch("dalton_core.period_source_family.needs_period_choices", return_value=False):
                        return super().record_spec(**kwargs)
                def explicit(state, body, **values):
                    body = copy.deepcopy(body)
                    catalog = state["numeric_context"].get("period_source_family_catalog")
                    if static:
                        test.assertIsNone(catalog)
                        return spec_from_response(state, body, **values)
                    test.assertEqual(catalog["contract_ref"], CONTRACT_REF)
                    choices = [g["family_ref"] for g in catalog["groups"]]
                    body["revenue_drivers"] = [{"ref": ref, "label": ref, "kind": "segment",
                        "basis_concept": REVENUE_CONCEPT, "unit": "USD", "because": "Explicit original complete period family.",
                        "basis_identity": {"statement": "income", "dimensions": {**FIXED, AXIS: member}},
                        "basis_period_family_refs": choices}
                        for ref, member in (("first", "issuer:FirstMember"), ("second", "issuer:SecondMember"))]
                    body["driver_formulas"] = [{"output_ref": "revenue", "operator": "sum",
                        "driver_refs": ["first", "second"], "because": "Every full original partition."}]
                    return spec_from_response(state, body, **values)
                with patch("tests.test_company_model_forecast.spec_from_response", side_effect=explicit):
                    return super().record_spec(**kwargs)
        case = Case("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def test_variable_source_normal_publication_durable_replay_and_no_new_version(self):
        case = self.fixture()
        from tests.test_company_model_forecast import ACN
        state = build_company_model_state(case.missions, ACN, ticker="ACN")
        self.assertEqual(state["numeric_context"]["prompt_bytes"], len(build_prompt(state).encode()))
        before = copy.deepcopy(state)
        result = case.child()
        self.assertEqual(result["forecast_status"], "published", result)
        authority = ForecastModelAuthority(case.store)
        record = authority.model(result["model_version_ref"])
        proof = authority.filing_proof(record["id"])
        self.assertEqual(proof["model_statement_scope"]["rule_ref"], PERIOD_RULE_REF)
        self.assertEqual(authority._filing_proof(record["id"], require_replay=True), proof)
        self.assertEqual(build_company_model_state(case.missions, ACN, ticker="ACN"), before)
        self.assertEqual(case.child()["forecast_status"], "nothing_to_model")

    def test_static_complete_source_keeps_original_prompt_contract_and_scope(self):
        case = self.fixture(static=True)
        from tests.test_company_model_forecast import ACN
        state = build_company_model_state(case.missions, ACN, ticker="ACN")
        self.assertNotIn("period_source_family_catalog", state["numeric_context"])
        self.assertNotIn("basis_period_family_refs", build_prompt(state))
        result = case.child()
        self.assertEqual(result["forecast_status"], "published", result)
        proof = ForecastModelAuthority(case.store).filing_proof(result["model_version_ref"])
        self.assertNotEqual(proof["model_statement_scope"]["rule_ref"], PERIOD_RULE_REF)

    def test_new_saved_scope_cannot_be_relabelled_legacy_or_rehashed_with_other_dimensions(self):
        import json
        from dalton_core.store import content_hash
        case = self.fixture()
        result = case.child()
        authority = ForecastModelAuthority(case.store)
        record = authority.model(result["model_version_ref"])
        original = authority.filing_proof(record["id"])
        case.store.connection.execute("DROP TRIGGER forecast_model_filing_proof_no_update")
        for alter in ("rule", "dimension"):
            proof = copy.deepcopy(original)
            scope = proof["model_statement_scope"]
            if alter == "rule":
                scope["rule_ref"] = "rule:forecast-execution-statement-scope:3"
            else:
                scope["arithmetic_partitions"][0]["fixed_dimensions"] = {"issuer:ForgedAxis": "issuer:ForgedMember"}
            scope["content_hash"] = content_hash({k:v for k,v in scope.items() if k != "content_hash"})
            proof["content_hash"] = content_hash({k:v for k,v in proof.items() if k != "content_hash"})
            case.store.connection.execute("UPDATE forecast_model_filing_proofs SET record_json=?,content_hash=? WHERE model_version_id=?",
                (json.dumps(proof), proof["content_hash"], record["id"]))
            with self.assertRaises(ForecastModelConflict):
                authority.filing_proof(record["id"])

    def test_source_contract_wakes_normal_chooser_once_and_retains_old_spec(self):
        from dalton_core.company_model_cli import choose_company
        from tests.test_company_model_forecast import ACN
        case = self.fixture(legacy_spec=True)
        old = copy.deepcopy(case.missions.latest_company_model_spec(ACN))
        company, current = choose_company(case.missions, case.mission)
        self.assertEqual(company, ACN)
        self.assertNotEqual(current["state_hash"], old["state_hash"])
        self.assertEqual(current["numeric_context"]["period_source_family_catalog"]["contract_ref"], CONTRACT_REF)
        case.use_legacy = False
        new = case.record_spec()
        self.assertNotEqual(new["spec_id"], old["spec_id"])
        self.assertEqual(choose_company(case.missions, case.mission), (None, None))
        row = case.store.connection.execute("SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?",
                                           (old["spec_id"],)).fetchone()
        self.assertEqual(case.missions._model_spec_row(row), old)

    def test_optional_catalog_rejection_preserves_static_state_prompt_and_typed_diagnostic(self):
        from dalton_core.period_source_family import PeriodSourceFamilyError
        from tests.test_company_model_forecast import ACN
        case = self.fixture(static=True)
        original = build_company_model_state(case.missions, ACN, ticker="ACN")
        for reason in ("same complete source identity has conflicting amounts",
                       "complete source catalog exceeds its bounded scope"):
            with patch("dalton_core.period_source_family.catalog", side_effect=PeriodSourceFamilyError(reason)):
                with self.assertLogs("dalton_core.company_model_state", level="WARNING") as logs:
                    replay = build_company_model_state(case.missions, ACN, ticker="ACN")
            self.assertEqual(replay, original)
            self.assertEqual(build_prompt(replay), build_prompt(original))
            self.assertNotIn("period_source_family_catalog", replay["numeric_context"])
            self.assertIn("optional_source_catalog_unavailable", logs.output[0])
            self.assertNotIn("value", logs.output[0])
        with patch("dalton_core.period_source_family.catalog", side_effect=PeriodSourceFamilyError("conflicting source")):
            with patch("logging.Logger.warning", side_effect=OSError("diagnostic sink unavailable")):
                self.assertEqual(build_company_model_state(case.missions, ACN, ticker="ACN"), original)
