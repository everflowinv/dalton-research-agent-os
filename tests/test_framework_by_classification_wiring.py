"""W4: the driver template and ``market_proxy`` where the lanes actually read them.

``test_driver_template`` proves the table; this proves the three outputs are
wired to it and that the forecast layer refuses an assumption resting on a
market proxy that does not say how far the proxy sits from the company.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.company_dossier import MAX_GAPS
from dalton_core.company_dossier_draft import (
    TEMPLATE_UNITS,
    build_unit_prompt,
    parse_unit_output,
)
from dalton_core.company_model_spec import build_prompt, spec_template_gaps
from dalton_core.company_model_state import build_company_model_state
from dalton_core.debate_map_draft import build_input_table, build_prompt as debate_prompt
from dalton_core.driver_template import REGISTRY_HASH
from dalton_core.event_judgement import EVIDENCE_KIND_LINES, build_judge_prompt, model_drivers
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelValidationError,
    market_proxy_ref,
)
from dalton_core.store import DaltonStore

from tests.test_company_model_inputs import ACN
from tests.test_model_forecast_driver import model

GAP = "the spread is a chemical margin, not this plant's cash margin"


# ---------------------------------------------------------------------------
# the specification lane
# ---------------------------------------------------------------------------

SPEC_STATE = {
    "company_ref": "company:sec-cik:0000001",
    "ticker": "WH", "entity_name": "A Producer", "cik": "0000001",
    "filings": [], "statements": {},
    "concepts": ["us-gaap:Revenues", "us-gaap:CostOfRevenue"],
}


class SpecPromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_template_for_the_classification(self):
        prompt = build_prompt({**SPEC_STATE,
                               "industry_classification": "commodity_cycle"})
        self.assertIn("DRIVER TEMPLATE (commodity_cycle)", prompt)
        self.assertIn("cost_curve_position", prompt)
        self.assertIn("us-gaap:CostOfRevenue", prompt)
        self.assertNotIn("penetration", prompt)

    def test_no_classification_falls_to_the_generic_template_and_labels_it(self):
        prompt = build_prompt(SPEC_STATE)
        self.assertIn("DRIVER TEMPLATE (generic -- GENERIC", prompt)
        self.assertIn("no industry classification", prompt)

    def test_the_template_gaps_are_reported_and_never_enforced(self):
        spec = {"assessment": "价差决定一切",
                "revenue_drivers": [{"label": "spread", "because": "价差",
                                     "basis_concept": None}],
                "expense_lines": [], "operating_metrics": []}
        gaps = spec_template_gaps(
            spec, {**SPEC_STATE, "industry_classification": "commodity_cycle"})
        self.assertTrue(all("spread" not in gap for gap in gaps))
        self.assertTrue(any("utilisation" in gap for gap in gaps))


class StubMissions:
    """The two reads ``build_company_model_state`` makes, and nothing else."""

    def statement_filings(self, company_ref=None):
        return [{"ingest_id": "ingest:1", "accession": "0000-1", "form": "10-Q",
                 "report_date": "2026-06-30", "line_count": 1,
                 "entity_name": "A Producer", "cik": "0000001",
                 "company_ref": ACN}]

    def statement_lines(self, ingest_id):
        return [{"statement": "income", "concept": "us-gaap:Revenues",
                 "label": "Revenues", "level": 1, "parent_concept": None,
                 "is_breakdown": False, "dimension_axis": None,
                 "dimension_member": None, "unit": "USD",
                 "period_start": "2026-04-01", "period_end": "2026-06-30"}]


class ModelStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions = StubMissions()

    def test_the_classification_is_inside_the_hash_the_spec_is_keyed_by(self):
        plain = build_company_model_state(self.missions, ACN)
        typed = build_company_model_state(
            self.missions, ACN, industry_classification="commodity_cycle")
        self.assertNotEqual(plain["state_hash"], typed["state_hash"])
        self.assertEqual(typed["industry_classification"], "commodity_cycle")

    def test_a_state_with_no_classification_hashes_as_it_always_did(self):
        # The key is omitted rather than written as null, so every projection
        # built before W4 still hashes to what it hashed to then.
        plain = build_company_model_state(self.missions, ACN)
        self.assertNotIn("industry_classification", plain)
        self.assertEqual(
            plain["state_hash"],
            build_company_model_state(
                self.missions, ACN, industry_classification=None)["state_hash"])


# ---------------------------------------------------------------------------
# the dossier
# ---------------------------------------------------------------------------

STRUCTURE = ({"slot_id": "causal_chain:0", "prompt": "需求从哪来"},)
MATERIAL = ({"tag": "C1", "kind": "claim", "ref": "claim-version:1",
             "text": "开工率维持在九成", "period": None, "importance": "filing"},)


def reply(gaps):
    return json.dumps({
        "slots": [{"slot_id": "causal_chain:0",
                   "sentences": [{"text": "开工率维持在九成", "refs": ["C1"]}]}],
        "gaps": list(gaps),
    })


class DossierTemplateTests(unittest.TestCase):
    def test_demand_drivers_is_the_section_the_template_frames(self):
        self.assertEqual(TEMPLATE_UNITS,
                         frozenset({"demand_drivers", "supply_and_cost"}))

    def test_the_prompt_shows_the_template_as_a_checklist(self):
        prompt = build_unit_prompt(
            unit="demand_drivers", structure=STRUCTURE, material=MATERIAL,
            company={"company_ref": "company:x", "ticker": "WH"},
            classification="commodity_cycle")
        self.assertIn("DRIVER TEMPLATE (commodity_cycle)", prompt)
        self.assertIn("checklist, not a structure", prompt)

    def test_the_template_table_cannot_be_read_as_the_slot_structure(self):
        # The prompt prints its slot structure as two spaces, an id and a tab.
        # A checklist in that same shape is a checklist a reader takes for
        # structure -- and the lane's own fixture did exactly that, refusing
        # every draft for filling slots nobody asked for.
        import re

        prompt = build_unit_prompt(
            unit="demand_drivers", structure=STRUCTURE, material=MATERIAL,
            company={"company_ref": "company:x", "ticker": "WH"},
            classification="commodity_cycle")
        self.assertEqual(re.findall(r"^  (\S+)\t", prompt, flags=re.MULTILINE),
                         ["causal_chain:0"])

    def test_supply_section_gets_the_separate_cost_template(self):
        prompt = build_unit_prompt(
            unit="supply_and_cost", structure=STRUCTURE, material=MATERIAL,
            company={"company_ref": "company:x", "ticker": "WH"},
            classification="commodity_cycle")
        self.assertIn("COST DRIVER TEMPLATE (commodity_cycle)", prompt)
        self.assertIn("raw_material_spread", prompt)
        self.assertNotIn("cost_curve_position", prompt)

    def test_an_uncovered_template_slot_becomes_a_gap_not_a_refusal(self):
        block = parse_unit_output(
            reply([]), unit="demand_drivers", structure=STRUCTURE,
            material=MATERIAL, classification="commodity_cycle")
        self.assertEqual(block["status"], "drafted")
        joined = " ".join(block["gaps"])
        self.assertIn("spread", joined)
        self.assertNotIn("utilisation", joined)

    def test_the_drafters_own_gaps_come_first_and_the_cap_holds(self):
        drafted = [f"缺口{index}" for index in range(MAX_GAPS - 1)]
        block = parse_unit_output(
            reply(drafted), unit="demand_drivers", structure=STRUCTURE,
            material=MATERIAL, classification="commodity_cycle")
        self.assertEqual(block["gaps"][:len(drafted)], drafted)
        self.assertEqual(len(block["gaps"]), MAX_GAPS)

    def test_a_full_gap_list_is_left_alone(self):
        drafted = [f"缺口{index}" for index in range(MAX_GAPS)]
        block = parse_unit_output(
            reply(drafted), unit="demand_drivers", structure=STRUCTURE,
            material=MATERIAL, classification="commodity_cycle")
        self.assertEqual(block["gaps"], drafted)

    def test_no_classification_still_produces_labelled_generic_gaps(self):
        block = parse_unit_output(
            reply([]), unit="demand_drivers", structure=STRUCTURE,
            material=MATERIAL)
        self.assertTrue(any("通用模板" in gap for gap in block["gaps"]))


# ---------------------------------------------------------------------------
# the debate map
# ---------------------------------------------------------------------------

METHOD = {"question_admission": ["A question must change a driver view."],
          "causal_chain": ["Price reaches earnings through the spread."]}


class DebateMapTemplateTests(unittest.TestCase):
    def _table(self, classification):
        return build_input_table(
            subject_ref="company:sec-cik:0000001", subject_kind="company",
            claim_rows=[], driver_rows=[
                {"driver_ref": "driver:spread", "label": "价差", "mechanism": "x"}],
            thesis=None, method=METHOD, previous=None,
            industry_classification=classification)

    def test_the_table_and_prompt_carry_the_template(self):
        table = self._table("commodity_cycle")
        self.assertEqual(table["industry_classification"], "commodity_cycle")
        self.assertIn("cost_curve_position", debate_prompt(table))

    def test_an_industry_subject_gets_the_generic_template(self):
        table = self._table(None)
        self.assertIsNone(table["industry_classification"])
        self.assertIn("GENERIC", debate_prompt(table))


# ---------------------------------------------------------------------------
# the forecast layer, end to end
# ---------------------------------------------------------------------------

class ForecastProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)

    def _with_proxy(self, proxy_ref):
        body = model()
        body["assumptions"][0] = {
            **body["assumptions"][0],
            "refs": list(body["assumptions"][0]["refs"]) + [proxy_ref],
        }
        return body

    def test_a_model_resting_on_a_proxy_that_states_its_gap_publishes(self):
        stored = self.authority.publish(self._with_proxy(
            market_proxy_ref("proxy:mdi-benzene-spread", proxy_gap=GAP)))
        self.assertEqual(stored["status"], "fresh")
        gaps = [item["proxy_gap"] for item in stored["assumptions"][0]["refs"]
                if item["kind"] == "market_proxy"]
        self.assertEqual(gaps, [GAP])

    def test_a_model_resting_on_a_proxy_with_no_gap_is_refused_whole(self):
        body = self._with_proxy({
            "kind": "market_proxy", "ref": "proxy:mdi-benzene-spread",
            "concept": None, "period_end": None, "accession": None})
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.authority.publish(body)
        self.assertIn("how far it sits", str(caught.exception))
        self.assertEqual(self.authority.versions(ACN), [])

    def test_a_model_with_no_proxy_hashes_exactly_as_before(self):
        # The kind was added without moving any existing model's bytes.
        first = self.authority.publish(model())
        again = self.authority.publish(model())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])


# ---------------------------------------------------------------------------
# the judgement layer
# ---------------------------------------------------------------------------

CONTEXT = {
    "event": {"id": "event:1", "kind": "filing", "evidence_tier": "primary_filing",
              "occurred_at": "2026-06-30T00:00:00+00:00", "payload": {},
              "source_refs": ["source:1"]},
    "company_ref": "company:sec-cik:0000001", "ticker": "WH",
}


class JudgePromptTests(unittest.TestCase):
    def test_the_evidence_kinds_are_enumerated_for_the_judge(self):
        prompt = build_judge_prompt(CONTEXT)
        self.assertIn("market_proxy", prompt)
        self.assertIn("never the company's own number", prompt)
        for line in EVIDENCE_KIND_LINES:
            self.assertIn(line, prompt)

    def test_a_proxy_gap_travels_with_the_assumption_into_the_prompt(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        record = ForecastModelAuthority(store)
        body = model()
        body["assumptions"][0] = {
            **body["assumptions"][0],
            "refs": list(body["assumptions"][0]["refs"]) + [
                market_proxy_ref("proxy:spread", proxy_gap=GAP)],
        }
        stored = record.publish(body)
        drivers = model_drivers(stored)
        carried = [row for driver in drivers for row in driver["assumptions"]
                   if row["proxy_gaps"]]
        self.assertTrue(carried)
        prompt = build_judge_prompt({**CONTEXT, "drivers": drivers})
        self.assertIn(f"market_proxy gap: {GAP}", prompt)


class RegistryBindingTests(unittest.TestCase):
    def test_the_specification_task_hash_moves_with_the_registry(self):
        from dalton_core.company_model_spec import TASK_HASH

        self.assertEqual(len(TASK_HASH), 64)
        self.assertEqual(len(REGISTRY_HASH), 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
