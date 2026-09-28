"""The legacy IT-services refusals of 2026-09-27, and what was and was not a rule bug.

Live, ``/Volumes/EveSSD/Dalton/legacy-state/dalton-core/company-model-spec-runs``:

* ``41245f70d8ee229a0da3961e`` IBM (09:12Z, $0.48 repair under the
  ``:retired-rule:95310fa7…`` recovery epoch): ``diluted EPS cannot be
  aggregated from quarterly EPS；缺口：…income报表里找不到「营业利润」``.
* ``c65b634d4cc51bbdb09b649a`` DXC (09:36Z): the same rule, ``缺口 … 「稀释加权平均股数」``,
  and no repair at all.
* ``1a4fab24801920d91c8f9839`` ACN (09:20Z): ``EPS numerator must use the
  company-specific diluted EPS numerator role``.

Replayed against the held filings (``coverage_mission_statement_lines``), the
model answers were wrong in ways the rules are right to refuse -- IBM's pretax
bridge was revenue less "Total expense and other (income)" (17,162 - 7,428 is
not 2,479); ACN's numerator was parent net income, which misses filed diluted
EPS in 9 of 22 periods because Accenture adds back the redeemable
noncontrolling interest of Accenture Canada Holdings; DXC divided by a share
line no DXC statement carries.  What *was* wrong on this side is below, each
with the filed evidence it rests on.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dalton_core import company_model_cli as cli
from dalton_core import company_model_spec as model_spec
from dalton_core import economic_invariants as ei
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError,
    replay_historical_structure,
)
from dalton_core.company_model_series import (
    DUPLICATE_FACT_POLICY_REF,
    quarterly_series,
)
from dalton_core.company_model_spec import (
    REPAIRABLE_STRUCTURE_RULES,
    REPAIRABLE_STRUCTURE_RULES_REF,
    CompanyModelSpecError,
    financial_line_gaps,
    structure_error_code,
)
from dalton_core.store import content_hash

IBM = "company:sec-cik:0000051143"
DXC = "company:sec-cik:001688568"
SHARES = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
EPS_RULE = "diluted EPS cannot be aggregated from quarterly EPS"


def _row(start, end, value, *, accession, filed, concept=SHARES, unit="shares"):
    return {"concept": concept, "period_start": start, "period_end": end,
            "value": value, "accession": accession, "filed": filed,
            "filing_form": "10-Q", "unit": unit, "is_breakdown": False,
            "dimension_axis": None}


# ---------------------------------------------------------------------------
# 1. IBM's diluted shares, filed twice per period at two precisions
# ---------------------------------------------------------------------------


class ConsistentDuplicateFactTests(unittest.TestCase):
    """Accession 0000051143-26-000078 files 2026-04-01..06-30 diluted shares as
    953,300,000 (face, in millions) and 953,263,534 (EPS note).  Both were one
    fact; the series called the quarter ambiguous and dropped it, so every IBM
    quarter was missing and no diluted-EPS divide could ever be tested."""

    def test_the_precise_fact_is_the_value_when_both_agree_after_rounding(self):
        series = quarterly_series([
            _row("2026-04-01", "2026-06-30", "953300000",
                 accession="0000051143-26-000078", filed="2026-07-23"),
            _row("2026-04-01", "2026-06-30", "953263534",
                 accession="0000051143-26-000078", filed="2026-07-23"),
            # The 10-Q for 2025 files "948.0" million beside 947,961,917.
            _row("2025-04-01", "2025-06-30", "948000000.0",
                 accession="0000051143-25-000052", filed="2025-07-24"),
            _row("2025-04-01", "2025-06-30", "947961917",
                 accession="0000051143-25-000052", filed="2025-07-24"),
        ])
        self.assertEqual(
            [(item["period_end"], item["value"]) for item in series["quarters"]],
            [("2025-06-30", "947961917"), ("2026-06-30", "953263534")])
        self.assertEqual(series["ambiguous_periods"], [])

    def test_values_that_do_not_agree_after_rounding_stay_ambiguous(self):
        for coarse in ("953400000", "953200000"):
            with self.subTest(coarse=coarse):
                series = quarterly_series([
                    _row("2026-04-01", "2026-06-30", coarse,
                         accession="0000051143-26-000078", filed="2026-07-23"),
                    _row("2026-04-01", "2026-06-30", "953263534",
                         accession="0000051143-26-000078", filed="2026-07-23"),
                ])
                self.assertEqual(series["quarters"], [])
                self.assertEqual(len(series["ambiguous_periods"]), 1)

    def test_two_facts_at_the_same_precision_are_a_conflict(self):
        series = quarterly_series([
            _row("2026-04-01", "2026-06-30", "953263534",
                 accession="0000051143-26-000078", filed="2026-07-23"),
            _row("2026-04-01", "2026-06-30", "953263535",
                 accession="0000051143-26-000078", filed="2026-07-23"),
        ])
        self.assertEqual(series["quarters"], [])
        self.assertEqual(len(series["ambiguous_periods"]), 1)

    def test_legacy_replay_keeps_its_byte_exact_projection(self):
        rows = [
            _row("2026-04-01", "2026-06-30", "953300000",
                 accession="0000051143-26-000078", filed="2026-07-23"),
            _row("2026-04-01", "2026-06-30", "953263534",
                 accession="0000051143-26-000078", filed="2026-07-23"),
        ]
        series = quarterly_series(rows, legacy_replay=True)
        self.assertEqual(series["quarters"][0]["value"], "953263534")
        self.assertNotIn("ambiguous_periods", series)

    def test_the_policy_is_part_of_the_validation_contract(self):
        self.assertEqual(
            cli.model_spec_validation_contract()["series_duplicate_facts"],
            DUPLICATE_FACT_POLICY_REF)


# ---------------------------------------------------------------------------
# 2. The refusal note that sent the reader after a line IBM does not file
# ---------------------------------------------------------------------------


def _statements(income):
    return {
        "income": income,
        "balance": [{"label": "Total assets"}, {"label": "Total liabilities"},
                    {"label": "Total equity"}],
        "cash": [{"label": "Net cash from operating activities"},
                 {"label": "Payments for plant, rental machines and other property"}],
    }


IBM_INCOME = [
    {"label": "Revenue", "concept": "us-gaap:Revenues"},
    {"label": "Cost", "concept": "us-gaap:CostOfRevenue"},
    {"label": "Gross profit", "concept": "us-gaap:GrossProfit"},
    {"label": "Total expense and other (income)",
     "concept": "ibm:ExpenseAndIncomeOther"},
    {"label": "Income from continuing operations before income taxes",
     "concept": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                "ExtraordinaryItemsNoncontrollingInterest"},
    {"label": "Net income", "concept": "us-gaap:NetIncomeLoss"},
    {"label": "Assuming dilution (in shares)", "concept": SHARES},
]


class OperatingIncomeGapTests(unittest.TestCase):
    def gaps(self, income, company_ref=IBM):
        return [item["line"] for item in financial_line_gaps(
            {"company_ref": company_ref, "statements": _statements(income)})
            if item["statement"] == "income"]

    def test_a_company_presenting_pretax_income_without_an_operating_subtotal(self):
        # IBM, all nine held filings: no us-gaap:OperatingIncomeLoss; gross
        # profit less "Total expense and other (income)" is pretax income
        # (Q2 2026: 9,907 - 7,428 = 2,479).
        self.assertEqual(self.gaps(IBM_INCOME), [])

    def test_the_exact_operating_income_concept_answers_under_any_label(self):
        income = [row for row in IBM_INCOME if "BeforeIncomeTaxes" not in row["concept"]]
        income.append({"label": "Income from operations",
                       "concept": "us-gaap:OperatingIncomeLoss"})
        self.assertEqual(self.gaps(income), [])

    def test_neither_line_is_still_a_gap(self):
        income = [row for row in IBM_INCOME if "BeforeIncomeTaxes" not in row["concept"]]
        self.assertEqual(self.gaps(income), ["operating income"])

    def test_a_label_saying_pretax_does_not_answer_without_the_exact_concept(self):
        income = [row for row in IBM_INCOME if "BeforeIncomeTaxes" not in row["concept"]]
        income.append({"label": "Income before income taxes",
                       "concept": "ibm:SomethingElse"})
        self.assertEqual(self.gaps(income), ["operating income"])

    def test_dxc_really_files_no_share_count_on_its_statements(self):
        # DXC, all nine held filings: EarningsPerShareDiluted on the income
        # statement, no weighted-average share concept anywhere.  That gap is
        # real and stays reported.
        income = [
            {"label": "Revenues", "concept": "us-gaap:Revenues"},
            {"label": "Income before income taxes",
             "concept": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                        "ExtraordinaryItemsNoncontrollingInterest"},
            {"label": "Net income", "concept": "us-gaap:ProfitLoss"},
            {"label": "Diluted (in dollars per share)",
             "concept": "us-gaap:EarningsPerShareDiluted"},
        ]
        self.assertEqual(self.gaps(income, DXC), ["weighted average"])


# ---------------------------------------------------------------------------
# 3. The annual-semantics enum on the diluted EPS line is wiring
# ---------------------------------------------------------------------------


class DilutedEpsAggregationRuleTests(unittest.TestCase):
    def test_it_is_repairable_and_versioned(self):
        self.assertIn(EPS_RULE, REPAIRABLE_STRUCTURE_RULES)
        self.assertEqual(REPAIRABLE_STRUCTURE_RULES_REF,
                         "rule:company-model-spec-repairable-structure:0.4")
        self.assertIn(EPS_RULE, cli.REPAIR_CONTRACT["eligible_structure_rules"])
        live = (f"financial_statement_structure is invalid: {EPS_RULE}；缺口："
                f"{DXC} 的income报表里找不到「稀释加权平均股数」")
        self.assertEqual(structure_error_code(live), "structure")

    def test_the_share_line_positivity_and_ties_are_still_semantic(self):
        for message in ("diluted weighted-average shares must be positive",
                        "formula diluted-eps does not tie to filed history",
                        "divide formula names an unknown line"):
            self.assertEqual(structure_error_code(message), "semantic", message)


class AccentureNumeratorEvidenceTests(unittest.TestCase):
    """The numerator rule is right for ACN, and here is the filed proof.

    Accession 0001467373-25-000222, 2025-09-01..11-30: net income attributable
    to Accenture plc 2,211,561,000; noncontrolling interest in Accenture Canada
    Holdings (redeemable) 2,083,000; diluted shares 626,043,040; diluted EPS
    3.54.  Parent net income alone is 3.53."""

    END = "2025-11-30"
    START = "2025-09-01"

    def inputs(self):
        def line(concept, value):
            return {"concept": concept, "cells": {
                self.END: {"period_start": self.START, "value": value}}}
        return {"filed_lines": [
            line("us-gaap:NetIncomeLoss", "2211561000"),
            line("us-gaap:NoncontrollingInterestInNetIncomeLossOther"
                 "NoncontrollingInterestsRedeemable", "2083000"),
            line(SHARES, "626043040"),
            line("us-gaap:EarningsPerShareDiluted", "3.54"),
        ]}

    def structure(self, terms, tie):
        def line(ref, role, concept, kind="filed", unit="usd"):
            return {"ref": ref, "role": role, "kind": kind, "concept": concept,
                    "unit": unit, "forecast_method": (
                        "formula" if kind == "derived" else "unavailable"),
                    "forecast_base_ref": None, "annual_forecast_method": None}
        return {
            "schema_version": "0.5", "content_hash": "0" * 64,
            "lines": [
                line("parent", "parent_net_income", "us-gaap:NetIncomeLoss"),
                line("redeemable", "dilutive_securities_adjustment",
                     "us-gaap:NoncontrollingInterestInNetIncomeLossOther"
                     "NoncontrollingInterestsRedeemable"),
                line("shares", "diluted_weighted_average_shares", SHARES,
                     unit="shares"),
                line("numerator", "diluted_eps_numerator", None, "derived"),
                line("eps", "diluted_eps", None, "derived", unit="usd_per_share"),
            ],
            "formulas": [
                {"output_ref": "numerator", "operator": "sum", "terms": [
                    {"line_ref": ref, "coefficient": "1"} for ref in terms],
                 "tie_out_concept": tie, "evidence_refs": ["x"]},
                {"output_ref": "eps", "operator": "divide",
                 "numerator_ref": "numerator", "denominator_ref": "shares",
                 "tie_out_concept": "us-gaap:EarningsPerShareDiluted",
                 "evidence_refs": ["x"]},
            ],
        }

    def test_parent_net_income_wrapped_as_the_numerator_does_not_tie(self):
        with self.assertRaisesRegex(FinancialStatementStructureError,
                                    "formula eps does not tie"):
            replay_historical_structure(
                self.structure(["parent"], "us-gaap:NetIncomeLoss"), self.inputs())

    def test_the_company_numerator_is_what_the_filed_eps_divides(self):
        numerator = Decimal("2211561000") + Decimal("2083000")
        self.assertEqual(
            (numerator / Decimal("626043040")).quantize(Decimal("0.01")),
            Decimal("3.54"))
        self.assertEqual(
            (Decimal("2211561000") / Decimal("626043040")).quantize(Decimal("0.01")),
            Decimal("3.53"))


# ---------------------------------------------------------------------------
# 4. A rule-version change re-validates held answers and buys at most one
# ---------------------------------------------------------------------------


def _spec_text(**structure_changes):
    from tests.test_company_model_spec import _spec
    body = _spec()
    structure = body["financial_statement_structure"]
    for key, value in structure_changes.items():
        structure[key] = value
    return json.dumps(body)


def _eps_misdeclared():
    """A structure refused on the diluted-EPS enum and nothing else."""

    from tests.test_company_model_spec import _statement_structure
    lines = copy.deepcopy(_statement_structure()["lines"])
    lines.append({
        "ref": "eps", "role": "diluted_eps", "label": "eps", "kind": "derived",
        "concept": None, "statement": "income", "unit": "usd_per_share",
        "period_kind": "duration", "annual_semantics": "not_applicable",
        "forecast_method": "formula", "forecast_base_ref": None,
    })
    return _spec_text(lines=lines)


def _derived_claims_concept():
    from tests.test_company_model_spec import _statement_structure
    lines = copy.deepcopy(_statement_structure()["lines"])
    for line in lines:
        if line["ref"] == "operating":
            line["concept"] = "us-gaap:OperatingIncomeLoss"
    return _spec_text(lines=lines)


def _scheduler_db(path: Path, answers: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE scheduler_work_orders (work_order_id TEXT PRIMARY KEY,"
        " work_order_json TEXT, work_order_hash TEXT, created_at TEXT);"
        "CREATE TABLE scheduler_formal_results (work_order_id TEXT UNIQUE,"
        " result_envelope_id TEXT, result_envelope_json TEXT,"
        " result_envelope_hash TEXT, terminal_state TEXT, created_at TEXT);")
    for number, answer in enumerate(answers):
        work_id = f"work:cockpit-model_spec-{number:032d}"
        work = {"id": work_id, "metadata": {
            "purpose": "model_spec", "structured_output_repair": answer["binding"]}}
        envelope = {"id": f"result:{number}", "work_order_ref": work_id,
                    "status": answer.get("status", "succeeded"),
                    "invocation_ref": f"invocation:{number}",
                    "metadata": {"route_decision_ref": f"route:{number}"},
                    "outputs": {"text": answer["text"]}}
        connection.execute(
            "INSERT INTO scheduler_work_orders VALUES (?,?,?,?)",
            (work_id, json.dumps(work), content_hash(work), f"2026-09-27T0{number}"))
        connection.execute(
            "INSERT INTO scheduler_formal_results VALUES (?,?,?,?,?,?)",
            (work_id, envelope["id"], json.dumps(envelope), content_hash(envelope),
             "succeeded", f"2026-09-27T0{number}"))
    connection.commit()
    connection.close()


def _binding(parent_text, message, code="structure", *, contract="e" * 64,
             number=1, state_hash="a" * 64):
    return {
        "schema_version": "company-model-spec-repair-binding-0.1",
        "state_hash": state_hash, "task_hash": model_spec.TASK_HASH,
        "parent_text_sha256": hashlib.sha256(parent_text.encode()).hexdigest(),
        "validation_error": {"code": code, "message": message},
        "repair_contract_hash": contract, "repair_number": number,
        "repair_config": {"max_attempts": 1},
    }


class PriorContractReplayTests(unittest.TestCase):
    """IBM's shape: an original refused on a wiring rule, a repair bought for
    it under the previous contract, and that repair refused on a rule this
    contract newly lists."""

    def setUp(self):
        from tests.test_company_model_spec import STATE
        self.state = STATE
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "scheduler.sqlite"
        self.original = _derived_claims_concept()
        self.held_repair = _eps_misdeclared()
        self.fixed = _spec_text()
        _scheduler_db(self.db, [{
            "binding": _binding(
                self.original, "financial_statement_structure is invalid: "
                "a derived line cannot claim a filed concept"),
            "text": self.held_repair,
        }])

    def model(self, answers):
        db = self.db

        class Model:
            scheduler_db = str(db)

            def __init__(inner):
                inner.calls = []

            def call(inner, **kwargs):
                inner.calls.append(kwargs)
                text = answers.pop(0)
                return {"text": text, "work_order_ref": "work:new",
                        "work_order_hash": "1" * 64,
                        "result_envelope_ref": "result:new",
                        "result_envelope_hash": "2" * 64,
                        "invocation_ref": "invocation:new",
                        "route_decision_ref": "route:new",
                        "cost_micros": 480_000, "replayed": False}
        return Model()

    def original_call(self):
        return {"text": self.original, "work_order_ref": "work:root",
                "work_order_hash": "3" * 64, "result_envelope_ref": "result:root",
                "result_envelope_hash": "4" * 64, "invocation_ref": "invocation:root",
                "route_decision_ref": "route:root", "cost_micros": 0,
                "replayed": True}

    def run_repair(self, model, attempts=None):
        return cli._validated_spec_with_repair(
            model=model, state=self.state,
            mission={"autonomy": {"automation_principal": "automation:test"}},
            original_call=self.original_call(), repair_config={"max_attempts": 1},
            decided_by="automation:test",
            repair_attempts=[] if attempts is None else attempts)

    def test_the_held_answer_is_replayed_free_and_one_new_question_is_asked(self):
        model = self.model([self.fixed])
        attempts = []
        spec, _repairs, accepted = self.run_repair(model, attempts)
        self.assertEqual(accepted["work_order_ref"], "work:new")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual([(item["replayed"], item["cost_micros"]) for item in attempts],
                         [(True, 0), (False, 480_000)])
        binding = model.calls[0]["_structured_output_repair"]
        # Asked of the held answer, about the rule it actually broke, one
        # level below it, under this contract.
        self.assertEqual(binding["repair_number"], 2)
        self.assertEqual(binding["repair_parent"]["work_order_ref"],
                         f"work:cockpit-model_spec-{0:032d}")
        self.assertEqual(binding["repair_contract_hash"], cli.REPAIR_CONTRACT_HASH)
        self.assertIn(EPS_RULE, binding["validation_error"]["message"])
        self.assertEqual(spec["company_ref"], self.state["company_ref"])

    def test_the_new_contract_gets_one_call_not_two(self):
        model = self.model([self.held_repair, self.fixed])
        with self.assertRaisesRegex(CompanyModelSpecError, EPS_RULE):
            self.run_repair(model)
        self.assertEqual(len(model.calls), 1)

    def test_an_answer_that_changed_nothing_is_not_bought_again(self):
        _scheduler_db(Path(self.directory.name) / "same.sqlite", [{
            "binding": _binding(
                self.original, "financial_statement_structure is invalid: "
                "a derived line cannot claim a filed concept"),
            "text": self.original,
        }])
        model = self.model([self.fixed])
        model.scheduler_db = str(Path(self.directory.name) / "same.sqlite")
        with self.assertRaisesRegex(CompanyModelSpecError, "cannot claim"):
            self.run_repair(model)
        self.assertEqual(model.calls, [])

    def test_only_the_exact_question_matches(self):
        message = ("financial_statement_structure is invalid: a derived line "
                   "cannot claim a filed concept")
        for label, binding in (
            ("other disclosure", _binding(self.original, message, state_hash="9" * 64)),
            ("other parent", _binding("{}", message)),
            ("other error", _binding(self.original, message + " (other)")),
            ("this contract", _binding(self.original, message,
                                       contract=cli.REPAIR_CONTRACT_HASH)),
        ):
            with self.subTest(label):
                db = Path(self.directory.name) / f"{label}.sqlite"
                _scheduler_db(db, [{"binding": binding, "text": self.held_repair}])
                self.assertIsNone(cli._prior_contract_repair(
                    db, state_hash="a" * 64, parent_text=self.original,
                    validation_error={"code": "structure", "message": message},
                    exclude=set()))

    def test_a_drifted_envelope_is_no_answer(self):
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE scheduler_formal_results SET result_envelope_hash='x'")
        connection.commit()
        connection.close()
        self.assertIsNone(cli._prior_contract_repair(
            self.db, state_hash="a" * 64, parent_text=self.original,
            validation_error={"code": "structure", "message": (
                "financial_statement_structure is invalid: a derived line "
                "cannot claim a filed concept")},
            exclude=set()))

    def test_without_a_scheduler_the_repair_is_exactly_as_before(self):
        model = self.model([self.fixed])
        model.scheduler_db = None
        spec, attempts, accepted = self.run_repair(model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["_structured_output_repair"]["repair_number"], 1)
        self.assertEqual(accepted["work_order_ref"], "work:new")


class ValidationContractTests(unittest.TestCase):
    def test_the_lane_key_moves_with_every_rule_version(self):
        from dalton_core.company_model_launcher import CompanyModelSpecLauncher
        from dalton_core.mission_model_spec_lane import business_key

        contract = cli.model_spec_validation_contract()
        self.assertEqual(contract["repairable_structure_rules"],
                         REPAIRABLE_STRUCTURE_RULES_REF)
        self.assertEqual(contract["repair_replay"], cli.REPAIR_REPLAY_POLICY_REF)
        launcher = object.__new__(CompanyModelSpecLauncher)
        self.assertEqual(launcher.financial_validation_contract_hash(),
                         cli.model_spec_validation_contract_hash())

        def key():
            return business_key(
                company_ref=IBM, state_hash="a" * 64, task_hash="b" * 64,
                repair_policy_hash="c" * 64,
                validation_hash=cli.model_spec_validation_contract_hash())

        before = key()
        with patch.object(cli, "REPAIRABLE_STRUCTURE_RULES_REF", "rule:x:9"):
            self.assertNotEqual(key(), before)
        with patch("dalton_core.company_model_series.DUPLICATE_FACT_POLICY_REF", "x"):
            self.assertNotEqual(key(), before)


# ---------------------------------------------------------------------------
# 5. The same questions through the real Cockpit and Scheduler
# ---------------------------------------------------------------------------


from tests import test_cockpit_model_fallback as _fallback  # noqa: E402

ChainAdapter = _fallback.ChainAdapter


class RuleVersionThroughTheSchedulerTests(unittest.TestCase):
    # The fallback suite's router, budget and mission fixtures, borrowed
    # without importing (and so re-running) its TestCase.
    setUp = _fallback.CockpitChainTests.setUp
    _model = _fallback.CockpitChainTests._model

    def test_a_rule_change_replays_the_paid_repair_and_asks_once(self):
        from dalton_core.company_model_cli import (
            _validated_spec_with_repair, model_spec_request_id,
            model_spec_request_identity,
        )
        from tests.test_company_model_spec import STATE

        outputs = [_derived_claims_concept(), _eps_misdeclared(), _spec_text()]

        class SequenceAdapter(ChainAdapter):
            def execute(inner, work, route, profile):
                invocation, envelope = super().execute(work, route, profile)
                return invocation, replace(
                    envelope, outputs={"text": outputs.pop(0)})

        adapter = SequenceAdapter({})
        model = self._model(adapter, policy_version_ref=self.pinned_policy)
        config = {"max_attempts": 1}
        first = model.call(
            purpose="model_spec",
            request_id=model_spec_request_id(STATE["state_hash"], repair_config=config),
            prompt="decide model structure", mission=self.mission,
            _model_spec_request_identity=model_spec_request_identity(
                STATE["state_hash"], repair_config=config))

        def run():
            return _validated_spec_with_repair(
                model=model, state=STATE, mission=self.mission,
                original_call=first, repair_config=config,
                decided_by="automation:test")

        # Before: the EPS enum was not on the list, so the paid repair was
        # refused whole.
        old_rules = tuple(rule for rule in REPAIRABLE_STRUCTURE_RULES if rule != EPS_RULE)
        old_contract = {**cli.REPAIR_CONTRACT, "ref": "contract:old",
                        "eligible_structure_rules": list(old_rules)}
        with patch.object(model_spec, "REPAIRABLE_STRUCTURE_RULES", old_rules), \
                patch.object(cli, "REPAIR_CONTRACT", old_contract), \
                patch.object(cli, "REPAIR_CONTRACT_HASH", content_hash(old_contract)), \
                patch.object(cli, "REPAIR_CONTRACT_REF", "contract:old"):
            with self.assertRaisesRegex(CompanyModelSpecError, EPS_RULE):
                run()
        self.assertEqual(len(adapter.served), 2)

        # After: the held repair is replayed and one new repair is admitted by
        # the Cockpit's own ancestry proof.
        spec, repairs, accepted = run()
        self.assertEqual(len(adapter.served), 3)
        self.assertEqual([item["replayed"] for item in repairs], [True, False])
        self.assertEqual(spec["state_hash"], STATE["state_hash"])

        # And again: everything replays; nothing more is bought.
        again, repairs_again, accepted_again = run()
        self.assertEqual(len(adapter.served), 3)
        self.assertEqual(accepted_again["work_order_ref"], accepted["work_order_ref"])
        self.assertTrue(all(item["replayed"] for item in repairs_again))
        self.assertEqual(again["content_hash"], spec["content_hash"])


# ---------------------------------------------------------------------------
# 6. META's geographic revenue (ws-7d forecast, forecast-economic-invariants:7)
# ---------------------------------------------------------------------------


def _geo_rows(total, members):
    rows = [{"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "period_start": "2023-01-01", "period_end": "2023-12-31",
             "value": total, "is_breakdown": False, "dimension_axis": None,
             "dimension_member": None}]
    for member, value in members:
        rows.append({**rows[0], "value": value, "is_breakdown": True,
                     "dimension_axis": "srt:StatementGeographicalAxis",
                     "dimension_member": member, "dimension_count": 1})
    return rows


META_2023 = [
    ("meta:USCanadaMember", "52888000000"), ("srt:EuropeMember", "31210000000"),
    ("srt:AsiaPacificMember", "36154000000"), ("meta:RestOfWorldMember", "14650000000"),
    ("country:US", "49780000000"),
]


class GeographicCountryMemberTests(unittest.TestCase):
    def verdict(self, rows):
        return ei._segment_sum(ei.segment_groups(rows))

    def test_meta_s_united_states_inside_us_and_canada_is_not_a_sixth_region(self):
        # Accession 0001628280-26-003942: four regions add to 134,902; the
        # fifth member, country:US 49,780, is inside "United States & Canada".
        verdict = self.verdict(_geo_rows("134902000000", META_2023))
        self.assertEqual(verdict.status, ei.PASS)
        self.assertEqual(verdict.checked, 1)

    def test_regions_that_do_not_add_up_still_fail(self):
        members = list(META_2023)
        members[1] = ("srt:EuropeMember", "31000000000")
        verdict = self.verdict(_geo_rows("134902000000", members))
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("5 segments add to", verdict.findings[0])

    def test_a_country_larger_than_every_region_cannot_be_nested(self):
        members = [("srt:EuropeMember", "60"), ("srt:AsiaPacificMember", "40"),
                   ("country:US", "70")]
        self.assertEqual(self.verdict(_geo_rows("100", members)).status, ei.FAIL)

    def test_united_states_plus_international_is_still_a_checked_partition(self):
        members = [("country:US", "60"), ("meta:InternationalMember", "40")]
        self.assertEqual(self.verdict(_geo_rows("100", members)).status, ei.PASS)
        members = [("country:US", "60"), ("meta:InternationalMember", "30")]
        self.assertEqual(self.verdict(_geo_rows("100", members)).status, ei.FAIL)

    def test_only_geographic_axes_read_countries_this_way(self):
        rows = _geo_rows("134902000000", META_2023)
        for row in rows[1:]:
            row["dimension_axis"] = "srt:ProductOrServiceAxis"
        self.assertEqual(self.verdict(rows).status, ei.FAIL)

    def test_the_contract_version_moved(self):
        self.assertEqual(ei.FORECAST_INVARIANT_CONTRACT_REF,
                         "forecast-economic-invariants:9")
        self.assertEqual(
            ei.FORECAST_INVARIANT_CONTRACT["segment_sum"]["geographic_country_members"],
            "partition-or-nested-in-region:v1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
