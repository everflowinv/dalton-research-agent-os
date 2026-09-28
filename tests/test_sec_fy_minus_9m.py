"""FY − 9M: a 10-K that reports only its fiscal year still answers its fourth quarter.

The fixture numbers are AMZN's filed revenue (10-K 0001018724-26-000004 and the
three 2025 10-Qs, ``RevenueFromContractWithCustomerExcludingAssessedTax``, in
dollars as the statement lane records them): Q4 2025 = 716,924 − 503,538 =
213,386 million, Q4 2024 = 637,959 − 450,167 = 187,792 million, +13.63%.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.quantitative_claim_promotion import QuantitativeClaimPromotionError
from dalton_core.research_auto_commit import (
    KNOWN_RULE_REFS,
    SEC_FY_MINUS_9M_RULE_REF,
    SEC_STATEMENT_LINE_RULE_REF,
    ResearchAutoCommitRejected,
)
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.sec_fy_minus_9m import (
    DERIVED_BASIS,
    FyMinus9mRefused,
    annual_filing,
    build_fy_minus_9m_candidate,
    derive,
    precision_unit,
    stage_fy_minus_9m_candidate,
)
from dalton_core.store import DaltonStore, GateRejected

# The fixture mission's universe is the US IT services one; the rows are
# AMZN's, filed under a universe company ref (the rule reads rows, not names).
COMPANY = "company:sec-cik:0001467373"
TICKERS = {"company:sec-cik:0001467373": "ACN", "company:sec-cik:0001058290": "CTSH",
           "company:sec-cik:0001352010": "EPAM", "company:sec-cik:001688568": "DXC"}
ACTOR = "automation:coverage-mission"
OWNER = "human:coverage-owner"
REVENUE = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
M = 1_000_000
TENK = "0001018724-26-000004"


def row(start, end, value, *, concept=REVENUE, **extra):
    base = {"statement": "income", "concept": concept, "label": "Total net sales",
            "level": 0, "parent_concept": None, "is_breakdown": False,
            "dimension_axis": None, "dimension_member": None,
            "period_start": start, "period_end": end, "value": str(value),
            "unit": "usd", "balance": "credit"}
    base.update(extra)
    return base


def amzn_filings(unit=M):
    """Accession -> (form, filed, report_date, rows) for fiscal 2025 and its comparatives."""

    u = unit
    return {
        "0001018724-25-000036": ("10-Q", "2025-05-02", "2025-03-31", [
            row("2024-01-01", "2024-03-31", 143313 * u),
            row("2025-01-01", "2025-03-31", 155667 * u)]),
        "0001018724-25-000086": ("10-Q", "2025-08-01", "2025-06-30", [
            row("2024-01-01", "2024-06-30", 291290 * u),
            row("2024-04-01", "2024-06-30", 147977 * u),
            row("2025-01-01", "2025-06-30", 323369 * u),
            row("2025-04-01", "2025-06-30", 167702 * u)]),
        "0001018724-25-000123": ("10-Q", "2025-10-31", "2025-09-30", [
            row("2024-01-01", "2024-09-30", 450167 * u),
            row("2024-07-01", "2024-09-30", 158877 * u),
            row("2025-01-01", "2025-09-30", 503538 * u),
            row("2025-07-01", "2025-09-30", 180169 * u)]),
        TENK: ("10-K", "2026-02-06", "2025-12-31", [
            row("2023-01-01", "2023-12-31", 574785 * u),
            row("2024-01-01", "2024-12-31", 637959 * u),
            row("2025-01-01", "2025-12-31", 716924 * u)]),
    }


class Harness:
    """One Core whose statement lane has ingested the given filings."""

    def __init__(self, filings=None, *, company=COMPANY):
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name)
        self.store = DaltonStore(str(self.path / "core.sqlite"))
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.company = company
        self.authorization = self.missions.authorize_sec_lane(
            company_ref=company, ticker=TICKERS[company], actor_ref=ACTOR,
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        self.attempts = 0
        self.ingest(amzn_filings() if filings is None else filings)
        self.staging = CandidateStagingStore(self.path / "candidate-staging.sqlite")

    def ingest(self, filings):
        for accession, (form, filed, report, rows) in sorted(filings.items()):
            self.attempts += 1
            dispatch = self.missions.queue_statement_dispatch(
                authorization=self.authorization, form=form,
                retry_salt=f"fixture-{self.attempts}")
            self.missions.mark_statement_dispatch_launched(
                dispatch["dispatch_id"], f"sec-financials-run:{self.attempts:024d}")
            self.missions.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"],
                observation={
                    "schema_version": "0.1", "cik": "0001018724", "entity_name": "AMAZON.COM, INC.",
                    "filings": [{"accession": accession, "form": form, "filed": filed,
                                 "report_date": report, "lines": rows}],
                    "source_record_refs": ["raw-sink:" + f"{self.attempts:x}".rjust(64, "a")],
                    "next_cursor": None, "provider_status": 200},
                governance_ref="connector-governance:sec-financial-statements:v2",
                governance_hash="b" * 64)
            self.missions.settle_statement_dispatch(dispatch["dispatch_id"], outcome="succeeded")

    def annual(self, accession=TENK):
        return annual_filing(self.store.connection, company_ref=self.company,
                             accession=accession)["ingest_id"]

    def derive(self, accession=TENK):
        return derive(self.store.connection, self.annual(accession))

    def sign(self, *rules):
        current = self.store.active_policy_version().to_dict()
        body = dict(current["policy"])
        body["research_candidate_auto_commit"] = {
            "enabled": True, "rules": list(rules), "max_records": 20}
        version = int(str(current["id"]).rsplit("-", 1)[1]) + 1
        return self.store.create_policy(
            body, policy_version_id=f"policy-{version}", version_number=version,
            activate=True, policy_ref=current.get("policy_ref", "commit-gate"),
            effective_from="2026-01-01T00:00:00+00:00", effective_until=None,
            actor_ref=OWNER, prior_version_ref=current["id"],
            change_reason="FY-9M test: list the derivation rule")

    def stage(self):
        return stage_fy_minus_9m_candidate(
            self.store.connection, self.staging, ingest_id=self.annual(), actor_ref=ACTOR)

    def commit(self, bundle, **overrides):
        records = {key: bundle[key] for key in (
            "evidence", "claim", "material", "numeric_spec",
            "source_verification", "numeric_verification")}
        records.update(overrides)
        return self.store.commit_policy_candidate(
            **records, idempotency_key="policy-ledger:" + records["claim"]["id"])

    def claims(self):
        return self.store.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE "
            "json_extract(claim_json,'$.metric_or_aspect')='quarterly_revenue_yoy_growth'"
        ).fetchall()

    def close(self):
        self.staging.close()
        self.store.close()
        self._dir.cleanup()


def with_rows(filings, accession, rows):
    form, filed, report, _ = filings[accession]
    filings[accession] = (form, filed, report, rows)
    return filings


class TheRuleIsKnownTests(unittest.TestCase):
    def test_the_rule_is_registered_and_named_for_its_inputs(self):
        self.assertIn(SEC_FY_MINUS_9M_RULE_REF, KNOWN_RULE_REFS)
        self.assertEqual(SEC_FY_MINUS_9M_RULE_REF,
                         "research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1")

    def test_a_new_workspace_signs_it_and_the_owner_script_can(self):
        from dalton_core.workspace_governance_baseline import (
            BASELINE_AUTO_COMMIT_RULES, RULE_CONSUMERS, baseline_policy_body,
        )
        from dalton_core.workspace_health_parity import LANE_POLICY_RULES
        from scripts.sign_auto_commit_rules import SIGNABLE_RULE_REFS

        self.assertIn(SEC_FY_MINUS_9M_RULE_REF, BASELINE_AUTO_COMMIT_RULES)
        self.assertIn("FY - 9M", RULE_CONSUMERS[SEC_FY_MINUS_9M_RULE_REF])
        self.assertIn(SEC_FY_MINUS_9M_RULE_REF, SIGNABLE_RULE_REFS)
        self.assertIn(SEC_FY_MINUS_9M_RULE_REF, LANE_POLICY_RULES["mission_sec_quarters"])
        body, added = baseline_policy_body(
            {"allowed_verdicts": ["pass"], "required_verification": True},
            mission_budget={"max_daily_paid_calls": 1, "max_daily_cost_usd": 1.0,
                            "max_alphaengine_calls_24h": 1})
        self.assertIn(SEC_FY_MINUS_9M_RULE_REF, body["research_candidate_auto_commit"]["rules"])
        self.assertIn(f"research_candidate_auto_commit:{SEC_FY_MINUS_9M_RULE_REF}", added)


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_amzn_fourth_quarter_is_fiscal_year_less_nine_months(self):
        derivation = self.h.derive()
        current, prior = derivation["current"], derivation["prior"]
        self.assertEqual(current["q4"]["period"], "2025-10-01..2025-12-31")
        self.assertEqual(current["q4"]["value"], str(213386 * M))
        self.assertEqual(prior["q4"]["period"], "2024-10-01..2024-12-31")
        self.assertEqual(prior["q4"]["value"], str(187792 * M))
        self.assertEqual(derivation["growth"], "13.63")
        self.assertEqual(derivation["concept"], REVENUE)
        # The filer's own nine-month row, checked against its three quarters.
        self.assertEqual(current["nine_months"]["basis"], "filed_nine_month_row")
        self.assertEqual(current["q4"]["uncertainty"], str(M))
        self.assertEqual(derivation["precision"]["digits"], 2)
        self.assertTrue(any("nine months 503538000000 = sum of quarters" in item
                            for item in derivation["checks"]), derivation["checks"])

    def test_every_component_names_its_filing_and_line(self):
        components = {item["role"]: item for item in self.h.derive()["components"]}
        self.assertEqual(components["current.fiscal_year"]["accession"], TENK)
        self.assertEqual(components["prior.fiscal_year"]["accession"], TENK)
        self.assertEqual(components["current.q1"]["accession"], "0001018724-25-000036")
        self.assertEqual(components["current.nine_months"]["accession"], "0001018724-25-000123")
        # Prior-year figures come from the comparatives beside this year's.
        self.assertEqual(components["prior.nine_months"]["accession"], "0001018724-25-000123")
        for item in components.values():
            self.assertTrue(item["line_id"].startswith(item["ingest_id"] + "#"))
            self.assertEqual(item["concept"], REVENUE)

    def test_a_unit_that_is_not_dollars_is_not_a_component(self):
        filings = amzn_filings()
        form, filed, report, rows = filings[TENK]
        filings[TENK] = (form, filed, report, [{**item, "unit": "eur"} for item in rows])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "no consolidated fiscal-year revenue row"):
            h.derive()

    def test_filed_precision_is_read_off_the_value(self):
        self.assertEqual(precision_unit("716924000000"), 1_000_000)
        self.assertEqual(precision_unit("180000000000"), 1_000_000)  # capped at 10^6
        self.assertEqual(precision_unit("1256431000"), 1_000)
        self.assertEqual(precision_unit("1256431123"), 1)

    def test_quarters_are_summed_when_no_cumulative_row_is_held(self):
        filings = amzn_filings()
        for accession in ("0001018724-25-000086", "0001018724-25-000123"):
            form, filed, report, rows = filings[accession]
            filings[accession] = (form, filed, report, [
                item for item in rows if item["period_start"][5:] != "01-01"])
        h = Harness(filings)
        self.addCleanup(h.close)
        derivation = h.derive()
        self.assertEqual(derivation["current"]["nine_months"]["basis"], "sum_of_quarters")
        self.assertEqual(derivation["current"]["q4"]["value"], str(213386 * M))
        # Four filed roundings instead of two: a wider bound, still two digits.
        self.assertEqual(derivation["current"]["q4"]["uncertainty"], str(2 * M))
        self.assertEqual(derivation["growth"], "13.63")


class FiscalBoundaryTests(unittest.TestCase):
    def test_a_june_fiscal_year_aligns_on_its_own_dates(self):
        # MSFT fiscal 2026: July..June, 10-K 0001193125-26-323660.
        filings = {
            "0001193125-25-256321": ("10-Q", "2025-10-29", "2025-09-30", [
                row("2024-07-01", "2024-09-30", 65585 * M),
                row("2025-07-01", "2025-09-30", 77673 * M)]),
            "0001193125-26-027207": ("10-Q", "2026-01-28", "2025-12-31", [
                row("2024-10-01", "2024-12-31", 69632 * M),
                row("2025-10-01", "2025-12-31", 81273 * M)]),
            "0001193125-26-191507": ("10-Q", "2026-04-29", "2026-03-31", [
                row("2025-01-01", "2025-03-31", 70066 * M),
                row("2026-01-01", "2026-03-31", 82886 * M)]),
            "0001193125-26-323660": ("10-K", "2026-07-29", "2026-06-30", [
                row("2024-07-01", "2025-06-30", 281724 * M),
                row("2025-07-01", "2026-06-30", 329000 * M)]),
        }
        # MSFT's instance names its dollar unit ``U_USD``; the measure is USD.
        for accession, (form, filed, report, rows) in list(filings.items()):
            filings[accession] = (form, filed, report,
                                  [{**item, "unit": "U_USD"} for item in rows])
        h = Harness(filings, company="company:sec-cik:0001058290")
        self.addCleanup(h.close)
        derivation = h.derive("0001193125-26-323660")
        self.assertEqual(derivation["current"]["q4"]["period"], "2026-04-01..2026-06-30")
        self.assertEqual(derivation["current"]["q4"]["value"], str((329000 - 241832) * M))
        self.assertEqual(derivation["prior"]["q4"]["period"], "2025-04-01..2025-06-30")
        self.assertEqual(derivation["prior"]["q4"]["value"], str((281724 - 205283) * M))

    def test_a_53_week_year_aligns_on_its_own_weeks(self):
        # Fiscal 2025: 2024-09-29..2025-09-27 (52 weeks); fiscal 2024 had 53,
        # the extra week in its first quarter.
        filings = {
            "0000000001-25-000010": ("10-Q", "2025-02-01", "2024-12-28", [
                row("2023-09-24", "2023-12-30", 1000 * M),
                row("2024-09-29", "2024-12-28", 1100 * M)]),
            "0000000001-25-000020": ("10-Q", "2025-05-01", "2025-03-29", [
                row("2023-12-31", "2024-03-30", 1000 * M),
                row("2024-12-29", "2025-03-29", 1100 * M)]),
            "0000000001-25-000030": ("10-Q", "2025-08-01", "2025-06-28", [
                row("2024-03-31", "2024-06-29", 1000 * M),
                row("2025-03-30", "2025-06-28", 1100 * M)]),
            "0000000001-25-000040": ("10-K", "2025-11-01", "2025-09-27", [
                row("2023-09-24", "2024-09-28", 4100 * M),   # 53 weeks
                row("2024-09-29", "2025-09-27", 4400 * M)]),  # 52 weeks
        }
        h = Harness(filings, company="company:sec-cik:0001352010")
        self.addCleanup(h.close)
        derivation = h.derive("0000000001-25-000040")
        self.assertEqual(derivation["current"]["q4"]["period"], "2025-06-29..2025-09-27")
        self.assertEqual(derivation["prior"]["q4"]["period"], "2024-06-30..2024-09-28")
        self.assertEqual(derivation["prior"]["q4"]["value"], str(1100 * M))
        self.assertEqual(derivation["current"]["q4"]["value"], str(1100 * M))
        self.assertEqual(derivation["growth"], "0")

    def test_a_gap_between_quarters_refuses(self):
        filings = amzn_filings()
        with_rows(filings, "0001018724-25-000123", [
            row("2024-07-01", "2024-09-30", 158877 * M),
            row("2025-07-02", "2025-09-30", 180169 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "current Q3"):
            h.derive()

    def test_a_fourth_quarter_that_is_not_a_quarter_refuses(self):
        filings = amzn_filings()
        filings[TENK] = ("10-K", "2026-02-06", "2026-01-15", [
            row("2024-01-01", "2024-12-31", 637959 * M),
            row("2025-01-01", "2026-01-15", 716924 * M)])  # a year, but Q4 is 107 days
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "fiscal boundaries do not align"):
            h.derive()

    def test_a_10k_without_its_fiscal_year_row_refuses(self):
        filings = amzn_filings()
        with_rows(filings, TENK, [row("2024-01-01", "2024-12-31", 637959 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "no consolidated fiscal-year revenue row"):
            h.derive()


class RefusalTests(unittest.TestCase):
    def test_a_restated_quarter_refuses(self):
        filings = amzn_filings()
        # The original 2024 Q1 10-Q says something else than the comparative.
        filings["0001018724-24-000083"] = ("10-Q", "2024-05-01", "2024-03-31", [
            row("2024-01-01", "2024-03-31", 143000 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "restated: 2024-01-01..2024-03-31"):
            h.derive()

    def test_an_unrestated_original_is_a_corroboration(self):
        filings = amzn_filings()
        filings["0001018724-24-000083"] = ("10-Q", "2024-05-01", "2024-03-31", [
            row("2024-01-01", "2024-03-31", 143313 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        components = {item["role"]: item for item in h.derive()["components"]}
        self.assertEqual(components["prior.q1"]["reported_by"],
                         ["0001018724-24-000083", "0001018724-25-000036"])
        # The original filing is the one named for its own quarter.
        self.assertEqual(components["prior.q1"]["accession"], "0001018724-24-000083")

    def test_a_concept_change_refuses(self):
        filings = amzn_filings()
        with_rows(filings, "0001018724-25-000086", [
            row("2024-04-01", "2024-06-30", 147977 * M, concept="us-gaap:Revenues"),
            row("2025-04-01", "2025-06-30", 167702 * M, concept="us-gaap:Revenues")])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "concept changed"):
            h.derive()

    def test_a_cumulative_row_that_is_not_its_quarters_refuses(self):
        filings = amzn_filings()
        form, filed, report, rows = filings["0001018724-25-000123"]
        rows = [dict(item) for item in rows]
        rows[2]["value"] = str(503000 * M)  # 2025 nine months
        filings["0001018724-25-000123"] = (form, filed, report, rows)
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "not the sum of its quarters"):
            h.derive()

    def test_a_quarter_not_held_refuses(self):
        filings = amzn_filings()
        del filings["0001018724-25-000036"]
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "no filed current Q1 row"):
            h.derive()

    def test_a_first_quarter_held_only_as_a_comparative_is_not_held(self):
        filings = amzn_filings()
        del filings["0001018724-25-000036"]
        # A 2026 Q1 10-Q repeats 2025 Q1 -- but this year's own Q1 10-Q is absent.
        filings["0001018724-26-000014"] = ("10-Q", "2026-04-30", "2026-03-31", [
            row("2025-01-01", "2025-03-31", 155667 * M),
            row("2026-01-01", "2026-03-31", 181519 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "Q1 .* is not held"):
            h.derive()

    def test_a_10k_that_files_its_own_fourth_quarter_is_not_derived(self):
        filings = amzn_filings()
        form, filed, report, rows = filings[TENK]
        filings[TENK] = (form, filed, report, rows + [
            row("2025-10-01", "2025-12-31", 213386 * M)])
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "files its own fourth quarter"):
            h.derive()

    def test_coarse_filed_precision_states_fewer_digits(self):
        # DXC fiscal 2026, in millions: bound ±0.06 pp, so no decimal survives.
        filings = {
            "0001688568-25-000072": ("10-Q", "2025-08-01", "2025-06-30", [
                row("2024-04-01", "2024-06-30", 3236 * M, concept="us-gaap:Revenues"),
                row("2025-04-01", "2025-06-30", 3159 * M, concept="us-gaap:Revenues")]),
            "0001688568-25-000098": ("10-Q", "2025-11-01", "2025-09-30", [
                row("2024-04-01", "2024-09-30", 6477 * M, concept="us-gaap:Revenues"),
                row("2024-07-01", "2024-09-30", 3241 * M, concept="us-gaap:Revenues"),
                row("2025-04-01", "2025-09-30", 6320 * M, concept="us-gaap:Revenues"),
                row("2025-07-01", "2025-09-30", 3161 * M, concept="us-gaap:Revenues")]),
            "0001688568-26-000005": ("10-Q", "2026-02-01", "2025-12-31", [
                row("2024-04-01", "2024-12-31", 9702 * M, concept="us-gaap:Revenues"),
                row("2024-10-01", "2024-12-31", 3225 * M, concept="us-gaap:Revenues"),
                row("2025-04-01", "2025-12-31", 9514 * M, concept="us-gaap:Revenues"),
                row("2025-10-01", "2025-12-31", 3194 * M, concept="us-gaap:Revenues")]),
            "0001688568-26-000022": ("10-K", "2026-05-20", "2026-03-31", [
                row("2024-04-01", "2025-03-31", 12871 * M, concept="us-gaap:Revenues"),
                row("2025-04-01", "2026-03-31", 12644 * M, concept="us-gaap:Revenues")]),
        }
        h = Harness(filings, company="company:sec-cik:001688568")
        self.addCleanup(h.close)
        derivation = h.derive("0001688568-26-000022")
        self.assertEqual(derivation["current"]["q4"]["period"], "2026-01-01..2026-03-31")
        self.assertEqual(derivation["current"]["q4"]["value"], str(3130 * M))
        self.assertEqual(derivation["prior"]["q4"]["value"], str(3169 * M))
        self.assertEqual(derivation["precision"]["digits"], 0)
        self.assertEqual(derivation["growth"], "-1")
        self.assertEqual(derivation["precision"]["interval_pp"], ["-1.3", "-1.16"])

    def test_precision_too_coarse_for_any_digit_refuses(self):
        filings = amzn_filings(unit=1)  # "USD 155667" of revenue, reported in millions
        for accession, (form, filed, report, rows) in list(filings.items()):
            filings[accession] = (form, filed, report, [
                {**item, "value": str(int(item["value"]) // 1000 * 1_000_000)} for item in rows])
        # Now 155,000,000 etc.: tiny quarters rounded to millions.
        h = Harness(filings)
        self.addCleanup(h.close)
        with self.assertRaisesRegex(FyMinus9mRefused, "wider than half a point|not the sum"):
            h.derive()


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_an_unsigned_rule_leaves_the_derivation_staged(self):
        bundle = self.h.stage()
        self.assertEqual(bundle["write_status"], "fresh")
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF)
        with self.assertRaisesRegex(ResearchAutoCommitRejected, SEC_FY_MINUS_9M_RULE_REF):
            self.h.commit(bundle)
        self.assertEqual(self.h.claims(), [])

    def test_a_signed_rule_admits_a_claim_that_says_it_is_derived(self):
        import json

        bundle = self.h.stage()
        self.h.sign(SEC_STATEMENT_LINE_RULE_REF, SEC_FY_MINUS_9M_RULE_REF)
        result = self.h.commit(bundle)
        self.assertEqual(result["authorization"]["rule_ref"], SEC_FY_MINUS_9M_RULE_REF)
        [claim] = [json.loads(item[0]) for item in self.h.claims()]
        self.assertEqual(claim["basis"], DERIVED_BASIS)
        self.assertEqual(claim["period"], "2025-10-01..2025-12-31")
        self.assertEqual((claim["value"], claim["unit"]), ("13.63", "percent"))
        statement = claim["normalized_statement"]
        self.assertIn("Derived value (FY − 9M", statement)
        self.assertIn("not a filed quarter", statement)
        self.assertIn("USD 716924000000", statement)
        self.assertIn("USD 503538000000", statement)
        self.assertIn("USD 213386000000", statement)
        self.assertIn(TENK, statement)
        payload = bundle["material"]["normalized_payload"]
        self.assertEqual(payload["derivation"]["rule_ref"], SEC_FY_MINUS_9M_RULE_REF)
        self.assertEqual(len(payload["lines"]), len(payload["components"]))
        self.assertEqual(bundle["numeric_spec"]["operator"], "growth_percentage")
        self.assertEqual([item["json_pointer"] for item in bundle["numeric_spec"]["inputs"]],
                         ["/derivation/current/q4/value", "/derivation/prior/q4/value"])
        # Committing again writes nothing new.
        again = self.h.commit(bundle)
        self.assertEqual(again.get("claim_version_ref"), result.get("claim_version_ref"))
        self.assertEqual(len(self.h.claims()), 1)

    def test_an_edited_sentence_is_refused_by_the_replay(self):
        from dalton_core.store import content_hash

        bundle = self.h.stage()
        self.h.sign(SEC_FY_MINUS_9M_RULE_REF)
        claim = dict(bundle["claim"])
        claim["normalized_statement"] = claim["normalized_statement"].replace("13.63", "15.00")
        body = {key: value for key, value in claim.items() if key != "content_hash"}
        claim["content_hash"] = content_hash(body)
        with self.assertRaises((ResearchAutoCommitRejected, GateRejected, ValueError)):
            self.h.commit(bundle, claim=claim)
        self.assertEqual(self.h.claims(), [])

    def test_a_filing_ingested_after_staging_that_restates_refuses_the_replay(self):
        bundle = self.h.stage()
        self.h.sign(SEC_FY_MINUS_9M_RULE_REF)
        self.h.ingest({"0001018724-24-000161": ("10-Q", "2024-11-01", "2024-09-30", [
            row("2024-07-01", "2024-09-30", 150000 * M)])})
        with self.assertRaisesRegex(ResearchAutoCommitRejected, "cannot be replayed|restated"):
            self.h.commit(bundle)

    def test_the_review_inbox_opens_the_candidate_with_its_spec(self):
        from dalton_core.research_review import HumanReviewAuthority

        staged = self.h.stage()
        review = HumanReviewAuthority(self.h.path / "candidate-staging.sqlite")
        self.addCleanup(review.close)
        bundle = review.candidate_authority_bundle(staged["claim"]["id"])
        self.assertEqual(bundle["numeric_spec"]["operator"], "growth_percentage")
        self.assertEqual(bundle["material"]["id"], staged["material"]["id"])

    def test_a_statement_line_candidate_is_not_judged_by_the_fy_rule(self):
        # The router reads the payload kind; a filed line still needs its own rule.
        from tests.test_quantitative_claim_admission import Harness as LineHarness

        line = LineHarness()
        self.addCleanup(line.close)
        bundle = line.stage("revenue")
        line.sign(SEC_FY_MINUS_9M_RULE_REF)
        with self.assertRaisesRegex(ResearchAutoCommitRejected, SEC_STATEMENT_LINE_RULE_REF):
            line.commit(bundle)

    def test_a_human_actor_cannot_stage_the_derivation(self):
        with self.assertRaises(QuantitativeClaimPromotionError):
            build_fy_minus_9m_candidate(self.h.store.connection, ingest_id=self.h.annual(),
                                        actor_ref="human:someone")


class QuarterLaneTests(unittest.TestCase):
    """The quarter lane derives an annual-only 10-K's fourth quarter itself."""

    def coordinator(self, h, *, staging=True):
        from datetime import datetime, timezone

        from dalton_core.mission_sec_quarters import MissionSecQuartersCoordinator, quarterly_filings

        coordinator = MissionSecQuartersCoordinator(
            store=h.store, missions=h.missions, state_dir=h.path,
            checklist=lambda: [{"company_ref": COMPANY, "ticker": "ACN", "items": [
                {"item_ref": "quarterly_financials", "have": 3, "required": 4}]}],
            clock=lambda: datetime(2026, 9, 28, tzinfo=timezone.utc),
            governance_check=lambda: None,
            staging=h.staging if staging else None)
        # Company facts, as a 10-Q run left them: the 10-K carries fiscal years only.
        facts = {"facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": [
                {"form": "10-K", "start": "2025-01-01", "end": "2025-12-31", "accn": TENK,
                 "filed": "2026-02-06", "val": 716924 * M},
                {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "accn": TENK,
                 "filed": "2026-02-06", "val": 637959 * M},
            ]}}}}}
        entries = [{**item, "observation_ref": "sec-company-facts-artifact:" + "f" * 64}
                   for item in quarterly_filings(facts)]
        coordinator._artifact_filings = lambda company_ref: (entries, True)
        return coordinator

    def claims(self, h):
        import json

        return [json.loads(item[0]) for item in h.claims()]

    def test_unsigned_the_fourth_quarter_is_reported_and_the_quarters_still_queue(self):
        h = Harness()
        self.addCleanup(h.close)
        h.sign("research-auto-commit:sec-public-company-facts-growth:v1")
        result = self.coordinator(h).dispatch_once()
        self.assertEqual(result["status"], "queued", result)
        [derived] = result["derived"]
        self.assertEqual((derived["status"], derived["accession"]), ("unsigned", TENK))
        self.assertNotIn(TENK, [item["accession"] for item in result["queued"]])
        self.assertEqual(self.claims(h), [])

    def test_signed_the_fourth_quarter_is_derived_committed_and_held(self):
        h = Harness()
        self.addCleanup(h.close)
        h.sign("research-auto-commit:sec-public-company-facts-growth:v1",
               SEC_FY_MINUS_9M_RULE_REF)
        coordinator = self.coordinator(h)
        result = coordinator.dispatch_once()
        [derived] = result["derived"]
        self.assertEqual(derived["status"], "committed", derived)
        self.assertEqual((derived["period"], derived["value"]),
                         ("2025-10-01..2025-12-31", "13.63"))
        self.assertTrue(derived["ledger_write"])
        # No connector run for the 10-K; the 10-Qs it could not answer are queued.
        self.assertNotIn(TENK, [item["accession"] for item in result["queued"]])
        [claim] = self.claims(h)
        self.assertEqual((claim["basis"], claim["period"]),
                         (DERIVED_BASIS, "2025-10-01..2025-12-31"))
        self.assertIn("2025-10-01..2025-12-31", coordinator._held_periods(COMPANY))

    def test_a_refused_derivation_names_its_reason_and_writes_nothing(self):
        filings = amzn_filings()
        del filings["0001018724-25-000036"]
        h = Harness(filings)
        self.addCleanup(h.close)
        h.sign(SEC_FY_MINUS_9M_RULE_REF)
        result = self.coordinator(h).dispatch_once()
        [derived] = result["derived"]
        self.assertEqual(derived["status"], "refused")
        self.assertIn("no filed current Q1 row", derived["reason"])
        self.assertEqual(self.claims(h), [])

    def test_without_a_staging_store_nothing_is_written(self):
        h = Harness()
        self.addCleanup(h.close)
        h.sign(SEC_FY_MINUS_9M_RULE_REF)
        result = self.coordinator(h, staging=False).dispatch_once()
        [derived] = result["derived"]
        self.assertEqual((derived["status"], derived["value"]), ("unavailable", "13.63"))
        self.assertEqual(self.claims(h), [])


class OwnerStepsTests(unittest.TestCase):
    """The read-only preview and the signing script the owner runs for legacy and ws-7d."""

    SCRIPT = Path(__file__).resolve().parents[1] / "docs/ops/sign-fy-minus-9m-rule-2026-09-28.sh"

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def active_policy(self):
        return self.h.store.connection.execute(
            "SELECT policy_version_id FROM governance_policy_pointer").fetchone()[0]

    def run_script(self, *args, release_py=None):
        import os
        import subprocess
        import sys

        env = {**os.environ, "ONLY": "legacy", "LEGACY_STATE": str(self.h.path),
               "DALTON_PY": sys.executable, "LEGACY_RELEASE_PY": release_py or sys.executable,
               "ACTOR": "human:canary-owner"}
        return subprocess.run(["zsh", str(self.SCRIPT), *args], env=env, text=True,
                              capture_output=True, timeout=300)

    def test_the_preview_is_read_only_and_cross_checks_the_year(self):
        import sqlite3

        from scripts.preview_fy_minus_9m import preview

        connection = sqlite3.connect(f"file:{self.h.path / 'core.sqlite'}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        [row] = preview(connection)
        self.assertEqual((row["status"], row["period"], row["growth_percent"]),
                         ("derivable", "2025-10-01..2025-12-31", "13.63"))
        self.assertEqual(row["cross_check"]["current"]["filed_quarters_plus_q4_minus_fy"], "0")
        self.assertEqual(row["cross_check"]["prior"]["filed_quarters_plus_q4_minus_fy"], "0")
        self.assertIsNone(row["already_held"])

    def test_the_default_run_is_a_dry_run_that_writes_nothing(self):
        before = self.active_policy()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would-publish", result.stdout)
        self.assertIn("同比 13.63%", result.stdout)
        self.assertIn("dry-run 到此为止", result.stdout)
        self.assertEqual(self.active_policy(), before)

    def test_apply_refuses_while_the_running_release_does_not_know_the_rule(self):
        before = self.active_policy()
        result = self.run_script("apply", release_py="/usr/bin/false")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("运行中的 release 不认识", result.stderr)
        self.assertEqual(self.active_policy(), before)

    def test_rehearse_publishes_on_a_copy_only(self):
        before = self.active_policy()
        result = self.run_script("rehearse")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"added={SEC_FY_MINUS_9M_RULE_REF}", result.stdout)
        self.assertIn("mission_binds_new_constitution=True", result.stdout)
        self.assertEqual(self.active_policy(), before)

    def test_an_unknown_mode_is_refused(self):
        result = self.run_script("publish")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
