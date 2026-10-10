"""Forecast coverage (structure 0.6): methods, subtotal repair, EPS, sources, UI.

The fixture is Accenture as held on 2026-10-09: the drafted 0.5 structure that
computed 7 of 19 model lines (a filed "total operating expenses" used as a
formula term, pretax/interest/other income left unavailable, no EPS), the
filed inputs through FY26 Q3 and the FY26 Q4 8-K preliminary actuals.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError,
    forecast_structure_binding,
    materialize_financial_statement_structure,
    presentation_rounding_ties,
    validate_structure_proposal,
)
from dalton_core.company_model_forecast import pending_action
from dalton_core.forecast_assumption_sources import held_consensus_targets, prior_judgements
from dalton_core.forecast_coverage import (
    complete_methods_by_role,
    complete_structure_definition,
    forecast_computability,
    method_label,
)
from dalton_core.forecast_coverage_assumptions import (
    RULE_CONSENSUS,
    RULE_JUDGEMENT,
    RULE_ONE_OFF,
    RULE_PRELIMINARY,
)
from dalton_core.model_forecast_driver import (
    COVERAGE_GENERATOR_REF,
    STRUCTURE_GENERATOR_REF,
    body_hash,
    build_structured_forecast_model,
    revise_assumptions,
    validate_forecast_model,
)
from dalton_core.store import content_hash

FIXTURE = Path(__file__).parent / "fixtures" / "forecast_coverage" / "acn_fy26q3.json"


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def materialize(data: dict):
    structure, replay = materialize_financial_statement_structure(data["spec"], data["table"])
    binding = forecast_structure_binding(structure, replay, data["table"])
    return structure, replay, binding


def build(data: dict, sources: dict | None = None) -> dict:
    structure, replay, binding = materialize(data)
    return build_structured_forecast_model(
        data["spec"], data["table"], structure=structure, replay=replay,
        binding=binding, actor_ref="automation:test", assumption_sources=sources)


def stored(body: dict) -> dict:
    record = {key: value for key, value in body.items() if key != "source_version_ref"}
    record.update({"id": "forecast-model-version:1467373:1", "created_at": "2026-10-09T00:00:00+00:00",
                   "version": 1, "prior_version_ref": None})
    record["body_hash"] = body_hash(record)
    record["content_hash"] = content_hash(record)
    return validate_forecast_model(record)


class CoverageRuleTests(unittest.TestCase):
    def test_debt_basis_selects_current_exact_concept_without_requiring_eight_instants(self):
        data=load()
        old=next(l for l in data["table"]["filed_lines"] if l["concept"]=="us-gaap:LongTermDebtAndCapitalLeaseObligations")
        old["concept"]="us-gaap:LongTermDebtNoncurrent"
        ends=sorted(old["cells"])
        current=copy.deepcopy(old)
        current["concept"]="us-gaap:LongTermDebtAndCapitalLeaseObligations"
        current["cells"]={end:current["cells"][end] for end in ends[-4:]}
        old["cells"]={end:old["cells"][end] for end in ends[:-4]}
        data["table"]["filed_lines"].append(current)
        completed=complete_structure_definition(data["spec"]["financial_statement_structure"],company_spec=data["spec"],financial_inputs=data["table"])
        debt=next(l for l in completed["lines"] if l["role"]=="debt_noncurrent")
        interest=next(l for l in completed["lines"] if l["role"]=="interest_expense")
        self.assertEqual(debt["concept"],current["concept"])
        self.assertEqual(debt["forecast_method"],"hold_last")
        self.assertEqual(interest["forecast_method"],"rate_times_balance")
        self.assertEqual(interest["forecast_base_ref"],debt["ref"])

    def test_every_line_with_eight_quarters_gets_a_method(self):
        data = load()
        drafted = data["spec"]["financial_statement_structure"]
        unavailable = [line["ref"] for line in drafted["lines"]
                       if line["forecast_method"] == "unavailable"]
        self.assertIn("interest-income", unavailable)
        self.assertIn("business-optimization", unavailable)
        completed = complete_structure_definition(
            drafted, company_spec=data["spec"], financial_inputs=data["table"])
        self.assertEqual(completed["schema_version"], "0.6")
        coverage = {item["line_ref"]: item for item in completed["coverage"]}
        lines = {line["ref"]: line for line in completed["lines"]}
        for ref, item in coverage.items():
            line = lines[ref]
            if line["kind"] == "filed" and (item["history_quarters"] or 0) >= 8:
                self.assertNotEqual(item["method"], "unavailable", ref)
            if item["method"] == "unavailable":
                self.assertTrue(item["reason"], ref)
        # spec behaviour one_off -> zero, even with seven quarters of history
        self.assertEqual(lines["business-optimization"]["forecast_method"], "zero_one_off")
        self.assertEqual(coverage["business-optimization"]["history_quarters"], 7)
        self.assertIn("一次性", coverage["business-optimization"]["reason"])
        # other income is carried at its trailing mean, flagged 延续
        self.assertEqual(lines["other-nonoperating"]["forecast_method"], "carry_forward_mean")
        self.assertIn("延续", coverage["other-nonoperating"]["reason"])
        # interest is a rate on a balance once the balance sheet is present
        self.assertEqual(lines["interest-income"]["forecast_method"], "rate_times_balance")
        self.assertEqual(lines["interest-expense"]["forecast_method"], "rate_times_balance")
        self.assertEqual(lines[lines["interest-expense"]["forecast_base_ref"]]["statement"],
                         "balance")
        # a drafted judgement is kept, not overruled
        self.assertEqual(lines["g-and-a"]["forecast_method"], "quarterly_growth")

    def test_short_history_is_unavailable_with_a_written_reason(self):
        data = load()
        table = copy.deepcopy(data["table"])
        other = next(item for item in table["filed_lines"]
                     if item["concept"] == "us-gaap:OtherNonoperatingIncomeExpense")
        for end in sorted(other["cells"])[:3]:
            del other["cells"][end]
        completed = complete_structure_definition(
            data["spec"]["financial_statement_structure"], company_spec=data["spec"],
            financial_inputs=table)
        coverage = {item["line_ref"]: item for item in completed["coverage"]}
        self.assertEqual(coverage["other-nonoperating"]["method"], "unavailable")
        self.assertIn("少于 8", coverage["other-nonoperating"]["reason"])
        blocked = forecast_computability(completed["lines"], completed["formulas"])
        self.assertIsNotNone(blocked["pretax-income"])

    def test_method_labels_for_the_table(self):
        self.assertEqual(method_label("share_of_line", base_role="revenue"), "占收入比")
        self.assertEqual(method_label("quarterly_growth"), "驱动")
        self.assertEqual(method_label("formula"), "公式")
        self.assertEqual(method_label("carry_forward_mean"), "延续")
        self.assertEqual(method_label("zero_one_off"), "一次性归零")
        self.assertEqual(method_label("unavailable"), "不可预测")


class StructureRepairTests(unittest.TestCase):
    def test_acn_subtotal_pretax_and_eps_are_repaired_on_history(self):
        data = load()
        structure, replay, _binding = materialize(data)
        self.assertEqual(structure["schema_version"], "0.6")
        lines = {line["ref"]: line for line in structure["lines"]}
        formulas = {item["output_ref"]: item for item in structure["formulas"]}
        # the filed "Total operating expenses" became a tied sum of components
        total = lines["total-operating-expenses"]
        self.assertEqual((total["kind"], total["role"]), ("derived", "company_presented_subtotal"))
        self.assertEqual(
            [(term["line_ref"], term["coefficient"]) for term in formulas["total-operating-expenses"]["terms"]],
            [("cost-of-services", "1"), ("sales-marketing", "1"), ("g-and-a", "1"),
             ("business-optimization", "1")])
        self.assertEqual(formulas["total-operating-expenses"]["tie_out_concept"], "us-gaap:CostsAndExpenses")
        # pretax = operating income + every non-operating item, tied
        self.assertEqual(lines["pretax-income"]["kind"], "derived")
        self.assertEqual(
            {term["line_ref"]: term["coefficient"] for term in formulas["pretax-income"]["terms"]},
            {"operating-income": "1", "interest-income": "1", "interest-expense": "-1",
             "other-nonoperating": "1"})
        # EPS: attributable plus the exchangeable-share NCI, proven by filed EPS
        numerator = formulas["diluted-eps-numerator"]
        self.assertIsNone(numerator["tie_out_concept"])
        self.assertEqual({term["line_ref"] for term in numerator["terms"]},
                         {"parent-net-income", "nci-canada"})
        self.assertEqual(formulas["diluted-eps"]["tie_out_concept"], "us-gaap:EarningsPerShareDiluted")
        reports = {item["output_ref"]: item for item in replay["formulas"]}
        self.assertTrue(all(item["status"] == "validated" for item in reports.values()))
        self.assertIn("filed diluted EPS", reports["diluted-eps-numerator"]["reason"])
        self.assertTrue(replay["ready_for_forecast"])
        self.assertEqual({ref for ref, reason in replay["forecast_computability"].items() if reason}, set())
        kinds = {item["kind"] for item in structure["auto_repairs"]}
        self.assertTrue({"subtotal_derived", "pretax_formula", "eps_chain", "line_added"} <= kinds)
        # first-version cash flow and balance sheet leaves
        roles = {line["role"]: line for line in structure["lines"]}
        for role in ("share_repurchase", "dividends_paid", "acquisitions",
                     "cash_and_equivalents", "debt_noncurrent", "accounts_payable"):
            self.assertIn(role, roles)
        self.assertEqual(roles["cash_and_equivalents"]["forecast_method"], "roll_forward")
        self.assertEqual(roles["debt_noncurrent"]["forecast_method"], "hold_last")

    def test_a_wrong_filed_eps_leaves_no_untied_eps_chain(self):
        data = load()
        table = copy.deepcopy(data["table"])
        eps = next(item for item in table["filed_lines"]
                   if item["concept"] == "us-gaap:EarningsPerShareDiluted")
        last = sorted(eps["cells"])[-1]
        eps["cells"][last]["value"] = "9.99"
        completed = complete_structure_definition(
            data["spec"]["financial_statement_structure"], company_spec=data["spec"],
            financial_inputs=table)
        self.assertNotIn("diluted_eps", {line["role"] for line in completed["lines"]})

    def test_presentation_rounding_tie_is_bounded_by_filed_precision(self):
        leaves = [Decimal("4981000000"), Decimal("2311000000"), Decimal("-166000000"),
                  Decimal("-185000000"), Decimal("486000000")]
        self.assertTrue(presentation_rounding_ties(Decimal("7427000000"), Decimal("7428000000"), leaves))
        self.assertFalse(presentation_rounding_ties(Decimal("7420000000"), Decimal("7428000000"), leaves))
        exact = [Decimal("4981123"), Decimal("2311456")]
        self.assertFalse(presentation_rounding_ties(Decimal("7292579"), Decimal("7292580"), exact))

    def test_proposal_completes_methods_and_refuses_an_uncomputable_chain(self):
        data = load()
        drafted = copy.deepcopy(data["spec"]["financial_statement_structure"])
        drafted["schema_version"] = "0.6"
        completed = complete_methods_by_role(drafted)
        methods = {line["ref"]: line["forecast_method"] for line in completed["lines"]}
        self.assertEqual(methods["other-nonoperating"], "carry_forward_mean")
        self.assertEqual(methods["pretax-income"], "unavailable")  # filed total: repaired later
        state = {
            "company_ref": data["spec"]["company_ref"],
            "filings": [{"accession": "0001467373-26-000031"}],
            "statements": {"income": [
                {"concept": line["concept"], "unit": line["unit"], "period_kind": "duration"}
                for line in drafted["lines"] if line["kind"] == "filed"
            ] + [{"concept": item["tie_out_concept"], "unit": "usd", "period_kind": "duration"}
                 for item in drafted["formulas"]]},
        }
        for formula in drafted["formulas"]:
            formula["evidence_refs"] = ["0001467373-26-000031"]
        normalized = validate_structure_proposal(
            drafted, state, revenue_anchor_concept=data["spec"]["revenue_anchor_concept"],
            expense_lines=data["spec"]["expense_lines"])
        self.assertEqual(
            {line["ref"]: line["forecast_method"] for line in normalized["lines"]}["interest-income"],
            "carry_forward_mean")
        broken = copy.deepcopy(drafted)
        broken["lines"].append({
            "ref": "made-up-eps", "role": "diluted_eps", "label": "EPS", "kind": "derived",
            "concept": None, "statement": "income", "unit": "usd_per_share",
            "period_kind": "duration", "annual_semantics": "direct_annual",
            "forecast_method": "formula", "forecast_base_ref": None, "annual_forecast_method": None,
        })
        broken["lines"].append({
            "ref": "made-up-shares", "role": "diluted_weighted_average_shares", "label": "Shares",
            "kind": "filed", "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
            "statement": "income", "unit": "shares", "period_kind": "duration",
            "annual_semantics": "direct_annual", "forecast_method": "unavailable",
            "forecast_base_ref": None, "annual_forecast_method": "unavailable",
        })
        broken["lines"].append({
            "ref": "made-up-numerator", "role": "diluted_eps_numerator", "label": "Numerator",
            "kind": "derived", "concept": None, "statement": "income", "unit": "usd",
            "period_kind": "duration", "annual_semantics": "sum_quarters",
            "forecast_method": "formula", "forecast_base_ref": None, "annual_forecast_method": None,
        })
        broken["formulas"].append({
            "output_ref": "made-up-numerator", "operator": "sum",
            "terms": [{"line_ref": "parent-net-income", "coefficient": "1"}],
            "tie_out_concept": "us-gaap:NetIncomeLoss", "evidence_refs": ["0001467373-26-000031"]})
        broken["formulas"].append({
            "output_ref": "made-up-eps", "operator": "divide",
            "numerator_ref": "made-up-numerator", "denominator_ref": "made-up-shares",
            "tie_out_concept": "us-gaap:EarningsPerShareDiluted",
            "evidence_refs": ["0001467373-26-000031"]})
        state["statements"]["income"] += [
            {"concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
             "unit": "shares", "period_kind": "duration"},
            {"concept": "us-gaap:EarningsPerShareDiluted", "unit": "usd_per_share",
             "period_kind": "duration"},
            {"concept": "us-gaap:NetIncomeLoss", "unit": "usd", "period_kind": "duration"},
        ]
        # the share line gets quarterly_growth from the completion, so the
        # chain is computable; only an unforecastable non-total term refuses
        validate_structure_proposal(
            broken, state, revenue_anchor_concept=data["spec"]["revenue_anchor_concept"],
            expense_lines=data["spec"]["expense_lines"])
        # The backstop itself: a derived line whose term has no method and is
        # not a repairable filed total is refused with the blocking term named.
        from dalton_core.company_financial_statement_structure import _refuse_uncomputable_chain

        lines = [
            {"ref": "revenue", "role": "revenue", "kind": "filed", "concept": "r",
             "forecast_method": "quarterly_growth", "forecast_base_ref": None},
            {"ref": "other", "role": "other_operating_income_expense", "kind": "filed",
             "concept": "o", "forecast_method": "unavailable", "forecast_base_ref": None},
            {"ref": "operating-income", "role": "operating_income", "kind": "derived",
             "concept": None, "forecast_method": "formula", "forecast_base_ref": None},
        ]
        formulas = [{"output_ref": "operating-income", "operator": "sum",
                     "terms": [{"line_ref": "revenue", "coefficient": "1"},
                               {"line_ref": "other", "coefficient": "1"}]}]
        with self.assertRaisesRegex(FinancialStatementStructureError, "other 没有预测方法"):
            _refuse_uncomputable_chain(lines, formulas)
        lines[1]["forecast_method"] = "carry_forward_mean"
        _refuse_uncomputable_chain(lines, formulas)


class ChainComputabilityTests(unittest.TestCase):
    def test_income_statement_completes_to_attributable_net_income_and_eps(self):
        data = load()
        body = build(data)
        self.assertEqual(body["generator_ref"], COVERAGE_GENERATOR_REF)
        results = {item["ref"]: item for item in body["results"]}
        for ref in ("result:revenue", "result:company_presented_subtotal",
                    "result:operating_income", "result:interest_income",
                    "result:interest_expense", "result:other_nonoperating_income_expense",
                    "result:pretax_income", "result:income_tax_expense", "result:net_income",
                    "result:parent_net_income", "result:diluted_weighted_average_shares",
                    "result:diluted_eps", "result:free_cash_flow",
                    "result:cash_and_equivalents", "result:net_cash_change"):
            self.assertEqual(results[ref]["status"], "computed", ref)
        self.assertTrue(all(item["status"] == "computed" for item in body["results"]
                            if item["ref"] not in {"result:free_cash_flow_company", "result:free_cash_flow_company_ex_one_offs", "result:capital_expenditure_company"}))
        # the cash roll: cash[k] = cash[k-1] + net change
        cash = [Decimal(cell["value"]) for cell in results["result:cash_and_equivalents"]["cells"]]
        change = [Decimal(cell["value"]) for cell in results["result:net_cash_change"]["cells"]]
        self.assertEqual(cash[1] - cash[0], change[1])
        # EPS = numerator / shares
        eps = Decimal(results["result:diluted_eps"]["cells"][0]["value"])
        numerator = Decimal(results["result:diluted_eps_numerator"]["cells"][0]["value"])
        shares = Decimal(results["result:diluted_weighted_average_shares"]["cells"][0]["value"])
        self.assertEqual(eps.quantize(Decimal("0.0001")), (numerator / shares).quantize(Decimal("0.0001")))
        # every assumption names its source; the one-off is held at zero
        rules = {item["provenance"]["rule_ref"] for item in body["assumptions"]}
        self.assertIn(RULE_ONE_OFF, rules)
        one_off = [item for item in body["assumptions"]
                   if item["provenance"]["rule_ref"] == RULE_ONE_OFF]
        self.assertTrue(all(Decimal(item["value"]) == 0 for item in one_off))
        record = stored(body)
        self.assertEqual(record["generator_ref"], COVERAGE_GENERATOR_REF)
        from dalton_core.economic_invariants import evaluate_forecast_model
        report = evaluate_forecast_model(record)
        self.assertEqual(report.reasons, ())

    def test_revision_recomputes_through_the_same_chain(self):
        body = build(load())
        record = stored(body)
        revenue_driver = next(item["ref"] for item in record["drivers"] if item["role"] == "revenue")
        first = record["forecast_periods"][0]["end"]
        revised = revise_assumptions(
            record, [{"driver": revenue_driver, "period": first, "value": "0.05",
                      "because": "owner judgement for the test quarter"}],
            change_reason="human_revision",
            evidence_refs=[{"kind": "human_decision", "ref": "decision:test", "concept": None,
                            "period_end": None, "accession": None}],
            actor_ref="human:owner")
        results = {item["ref"]: item for item in revised["results"]}
        self.assertEqual(results["result:diluted_eps"]["status"], "computed")
        self.assertEqual(results["result:cash_and_equivalents"]["status"], "computed")


class SourcePriorityTests(unittest.TestCase):
    def test_preliminary_then_consensus_then_judgement(self):
        data = load()
        structure, replay, binding = materialize(data)
        base = build(data)
        revenue_driver = next(item["ref"] for item in base["drivers"] if item["role"] == "revenue")
        ends = [item["end"] for item in base["forecast_periods"]]
        sources = {
            "preliminary": data["preliminary"],
            "revenue_targets": {ends[1]: {
                "source": "consensus", "value": "19500000000",
                "ref": {"kind": "figure", "ref": "consensus-estimate-version:test", "concept": None,
                        "period_end": ends[1], "accession": None},
                "because": "street consensus revenue for the test quarter"}},
            "judgement": [{"driver_ref": revenue_driver, "period_end": ends[2],
                           "measure": "quarterly_growth", "value": "0.02",
                           "because": "carried owner judgement",
                           "refs": [{"kind": "human_decision", "ref": "assumption:x", "concept": None,
                                     "period_end": ends[2], "accession": None}]}],
        }
        body = build_structured_forecast_model(
            data["spec"], data["table"], structure=structure, replay=replay, binding=binding,
            actor_ref="automation:test", assumption_sources=sources)
        revenue = {item["period"]["end"]: item for item in body["assumptions"]
                   if item["driver_ref"] == revenue_driver}
        self.assertEqual(revenue[ends[0]]["provenance"]["rule_ref"], RULE_PRELIMINARY)
        self.assertEqual(revenue[ends[1]]["provenance"]["rule_ref"], RULE_CONSENSUS)
        self.assertEqual(revenue[ends[2]]["provenance"]["rule_ref"], RULE_JUDGEMENT)
        cells = {cell["period"]["end"]: Decimal(cell["value"]) for cell in next(
            item for item in body["results"] if item["ref"] == "result:revenue")["cells"]}
        self.assertNotIn(ends[0], cells)
        self.assertEqual(revenue[ends[0]]["measure"], "reported_actual")
        self.assertEqual(Decimal(revenue[ends[0]]["value"]), Decimal("18679110000"))
        self.assertEqual(cells[ends[1]].quantize(Decimal(1)), Decimal("19500000000"))
        self.assertEqual((cells[ends[2]] / cells[ends[1]] - 1).quantize(Decimal("0.0001")),
                         Decimal("0.0200"))
        tax = [item for item in body["assumptions"]
               if item["period"]["end"] == ends[0] and item["provenance"]["rule_ref"] == RULE_PRELIMINARY]
        self.assertGreaterEqual(len(tax), 2)  # revenue, cost share, tax rate, shares
        stored(body)

    def test_consensus_with_a_shifted_fiscal_mapping_is_not_used(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE consensus_estimate_versions (consensus_ref TEXT, "
                           "version_number INTEGER, record_json TEXT)")
        from dalton_core.consensus_estimate import consensus_ref_for
        company = "company:sec-cik:0001467373"
        record = {"id": "consensus-estimate-version:t", "as_of": "2026-10-09", "revenue_estimates": [
            # labelled FY2026Q4 but year_ago is FY2026Q1's revenue: shifted
            {"label": "FY2026Q4", "period_kind": "quarter", "period_end": "2026-08-31",
             "avg": "19411241500", "year_ago": "18742125000", "number_of_analysts": 19},
            {"label": "FY2027Q1", "period_kind": "quarter", "period_end": "2026-11-30",
             "avg": "19000000000", "year_ago": "18742125000", "number_of_analysts": 18},
        ]}
        connection.execute("INSERT INTO consensus_estimate_versions VALUES (?,?,?)",
                           (consensus_ref_for(company), 1, json.dumps(record)))
        history = {"2025-08-31": Decimal("17596260000"), "2025-11-30": Decimal("18742125000")}
        targets, notes = held_consensus_targets(connection, company, history)
        connection.close()
        self.assertEqual(set(targets), {"2026-11-30"})
        self.assertTrue(any("财期映射" in note for note in notes))

    def test_prior_judgements_are_human_or_event_decided_only(self):
        prior = {"id": "v1", "forecast_periods": [{"end": "2026-11-30"}], "assumptions": [
            {"driver_ref": "d", "period": {"end": "2026-11-30"}, "measure": "quarterly_growth",
             "value": "0.01", "kind": "human", "because": "owner", "ref": "a1",
             "superseded_by": None, "provenance": {"rule_ref": None, "decided_by": "human:o"}},
            {"driver_ref": "e", "period": {"end": "2026-11-30"}, "measure": "quarterly_growth",
             "value": "0.01", "kind": "estimate", "because": "trailing", "ref": "a2",
             "superseded_by": None,
             "provenance": {"rule_ref": "rule:trailing-carry-forward:1", "decided_by": "automation:x"}},
        ]}
        self.assertEqual([item["driver_ref"] for item in prior_judgements(prior)], ["d"])


class DeployTriggerTests(unittest.TestCase):
    def test_a_pre_coverage_structured_model_is_rebuilt_once(self):
        data = load()
        prior = {"schema_version": "0.4", "spec_ref": data["spec"]["spec_id"],
                 "generator_ref": STRUCTURE_GENERATOR_REF, "forecast_periods": []}
        self.assertEqual(pending_action(prior, data["spec"], data["table"]), "rules_upgrade")
        prior = build(data)
        self.assertIsNone(pending_action(prior, data["spec"], data["table"]))


class ModelPayloadTests(unittest.TestCase):
    def test_method_column_statement_completeness_and_preliminary_column(self):
        from dalton_core.cockpit_v2.charts_api import _model_from_record
        from dalton_core.return_attribution import amount_scale

        data = load()
        record = stored(build(data, {"preliminary": data["preliminary"]}))
        payload = _model_from_record({"notes": []}, record, amount_scale,
                                     preliminary=data["preliminary"])
        kinds = [column["kind"] for column in payload["columns"]]
        self.assertEqual(kinds.count("preliminary"), 1)
        self.assertEqual(payload["data_as_of"], "2026-08-31")
        self.assertEqual(payload["boundary_index"], kinds.index("preliminary") + 1)
        self.assertEqual(record["forecast_periods"][0]["end"], "2026-11-30")
        self.assertEqual(record["forecast_policy"]["latest_actual_end"], "2026-08-31")
        rows = {row["key"]: row for row in payload["rows"]}
        revenue = next(row for row in payload["rows"] if row["role"] == "revenue")
        self.assertEqual(revenue["method"]["label"], "驱动")
        eps = next(row for row in payload["rows"] if row["role"] == "diluted_eps")
        self.assertTrue(any(cell["status"] == "ok" for cell in eps["cells"][:8]),
                        "derived EPS shows filed-formula history")
        labels = {row["method"]["label"] for row in payload["rows"]}
        self.assertTrue({"驱动", "占收入比", "公式", "延续", "一次性归零"} <= labels)
        by = payload["completeness"]["by_statement"]
        self.assertEqual(set(by), {"income", "cash", "balance"})
        self.assertTrue(any(item["computed"] < item["total"] for item in by.values()),
                        "missing preliminary bases stay explicit instead of skipping a quarter")
        self.assertTrue(payload["completeness"]["label"].startswith("利润表 "))
        self.assertIn("现金流", payload["completeness"]["label"])
        self.assertIn("资产负债表", payload["completeness"]["label"])
        operating = next(row for row in payload["rows"] if row["role"] == "operating_income")
        self.assertGreaterEqual(sum(1 for cell in operating["cells"]
                                    if cell["basis"] == "derived_formula"), 7)
        self.assertTrue(rows)

    def test_legacy_model_derived_rows_show_history(self):
        from dalton_core.cockpit_v2.charts_api import _legacy_formula_history

        drivers = [
            {"role": "revenue", "history": [{"period_start": "2026-04-01", "period_end": "2026-06-30", "value": "100"}]},
            {"role": "cost_of_revenue", "history": [{"period_start": "2026-04-01", "period_end": "2026-06-30", "value": "40"}]},
            {"role": "operating_expense", "history": [{"period_start": "2026-04-01", "period_end": "2026-06-30", "value": "30"}]},
            {"role": "operating_cash_flow", "history": [{"period_start": "2026-04-01", "period_end": "2026-06-30", "value": "25"}]},
            {"role": "capital_expenditure", "history": [{"period_start": "2026-04-01", "period_end": "2026-06-30", "value": "5"}]},
        ]
        derived = _legacy_formula_history(drivers)
        self.assertEqual(derived["gross-profit"]["2026-06-30"]["value"], "60")
        self.assertEqual(derived["operating-income"]["2026-06-30"]["value"], "30")
        self.assertEqual(derived["free-cash-flow"]["2026-06-30"]["value"], "20")

    def test_model_table_renders_the_method_column(self):
        source = (Path(__file__).parents[1] / "src" / "dalton_core" / "cockpit_v2" / "charts"
                  / "model.mjs").read_text(encoding="utf-8")
        self.assertIn('text: "方法"', source)
        self.assertIn("export function methodCell(row)", source)
        self.assertIn("初步实际（8-K）", source)
        self.assertIn("预测完整度", source)




class ActualizeTests(unittest.TestCase):
    def test_a_filed_quarter_actualizes_a_coverage_model_in_place(self):
        from dalton_core.model_forecast_driver import actualize_model, structure_topology_changed

        data = load()
        record = stored(build(data))
        table = copy.deepcopy(data["table"])
        last, oldest = table["periods"][-1], table["periods"][0]
        new_end, new_start = "2026-08-31", "2026-06-01"
        for line in table["filed_lines"]:
            cells = line.get("cells") or {}
            if last in cells:
                held = dict(cells[last])
                if held.get("period_start"):
                    held["period_start"] = new_start
                cells[new_end] = held
            cells.pop(oldest, None)
        table["periods"] = table["periods"][1:] + [new_end]
        structure, replay = materialize_financial_statement_structure(data["spec"], table)
        binding = forecast_structure_binding(structure, replay, table)
        self.assertFalse(structure_topology_changed(record, structure))
        body = actualize_model(record, table, structure=structure, replay=replay,
                               binding=binding, actor_ref="automation:test")
        results = {item["ref"]: item for item in body["results"]}
        revenue_actual = [cell for cell in results["result:revenue"]["cells"]
                          if cell["kind"] == "actual"]
        self.assertEqual([cell["period"]["end"] for cell in revenue_actual], [new_end])
        self.assertIn(new_end, [item["end"] for item in body["realised_periods"]])
        self.assertNotIn(new_end, [item["end"] for item in body["forecast_periods"]])
        self.assertEqual(results["result:diluted_eps"]["status"], "computed")
        self.assertIn("result:net_cash_change", results)
        cash = [cell for cell in results["result:cash_and_equivalents"]["cells"]
                if cell["kind"] == "actual"]
        self.assertEqual(len(cash), 1)


if __name__ == "__main__":
    unittest.main()
