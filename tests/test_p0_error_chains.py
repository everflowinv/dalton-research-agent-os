"""P0 error chains (research artifact quality review 2026-10-09), on synthetic companies.

Three rules that apply to every company in every environment, each exercised
on a company the system has never covered:

1. **Issuer attribution** -- a Claim about financials cut from another
   issuer's tables (the review's MSFT Claims from Oracle's model pages) is
   held at admission and retired by the patrol, and the products citing it
   are marked ``needs_rewrite`` for the next batched redraft.  Here: Acme
   Robotics (subject) and a broker note on Northwind Logistics (front matter
   ``分类: NWND``, ``related_companies: [NWND, ACME]``).
2. **Forecast normalisation** -- a one-off in the base quarter is taken out
   before growth is applied, operating costs cannot outgrow revenue without
   an analyst assumption, a falling share count is bounded by the buyback the
   cash flow funds, revenue guidance bounds the forecast, operating cash flow
   is net income + D&A + working capital when those lines exist and is
   unavailable when a share-of-revenue cash flow contradicts the model's
   losses, and a non-operating line full of securities gains is not
   extrapolated.  Here: Synthetic Widgets Co. (``tests/synthetic_company``).
3. **Return bridge** -- no revenue x trailing-margin proxy; core earnings on
   the model's own tax rate; a return next to a loss-making model carries the
   contradiction.
"""

from __future__ import annotations

import copy
import json
import unittest
from decimal import Decimal
from typing import Any

from dalton_core import product_rewrite_flags
from dalton_core.claim_issuer_attribution import (
    attribute,
    document_primary_issuer,
    issuer_table,
    misattribution_rationale,
)
from dalton_core.claim_retirement import (
    ClaimRetirementConflict,
    retired_claim_version_refs,
)
from dalton_core.claim_review import ClaimReviewDriver
from dalton_core.company_financial_statement_structure import (
    forecast_structure_binding,
    materialize_financial_statement_structure,
)
from dalton_core.document_extraction import DocumentExtractionService, issuer_context_fact
from dalton_core.forecast_coverage_assumptions import (
    OPEX_EXCESS_PP,
    RULE_GROWTH_CAP,
    RULE_GUIDANCE_BOUND,
    RULE_NORMALISED,
    RULE_SHARE_BOUND,
    normalise_base,
)
from dalton_core.model_forecast_driver import (
    CASH_BRIDGE_RULE,
    body_hash,
    build_structured_forecast_model,
    cash_bridge_contradiction,
    validate_forecast_model,
)
from dalton_core.product_rewrite_flags import ProductRewriteFlagAuthority, needs_rewrite
from dalton_core.return_attribution import (
    CORE_EARNINGS_BASIS,
    build_return_bridge,
    model_profit_check,
)
from dalton_core.store import content_hash
from tests.synthetic_company import COMPANY, company
from tests.test_claim_retirement import AUTOMATION, ClaimRetirementHarness

ACME = "company:ticker:acme"
BOLT = "company:ticker:bolt"
ACME_NEEDLES = ["acme", "acme robotics"]
ISSUERS = {ACME: ACME_NEEDLES, BOLT: ["bolt", "bolt freight"]}

SURVEY_PAGE = (
    "--- Page 12 ---\nNorth America Insight\nWhat Do CIOs Say About Automation Vendors?\n"
    "Key Takeaway\nCIOs named Acme Robotics as the vendor expected to gain the largest share of "
    "warehouse automation budgets, with Northwind seen as a share donor.\n"
)
MODEL_PAGE = (
    "--- Page 40 ---\nNorth America Insight\nExhibit 9: Model Changes\nFY24 | FY25 | FY26E | FY27E\n"
    "Freight Revenue\n4,100\n4,350\n4,600\n4,820\nFree Cash Flow\n410\n120\n-80\n-240\n"
    "FCF Margin\n10.0%\n2.8%\n-1.7%\n-5.0%\nShare Repurchases\n300\n150\n0\n0\n"
)
NOTE = (
    "---\ntags: [logistics, automation]\nrelated_companies: [NWND, ACME]\nsource: Example Securities\n"
    "---\n# 券商研报\n\n**日期**: 2026-02-10\n**分类**: NWND (公司)\n"
    "**来源文件**: NORTHWIND_20260210_0101.pdf\n\n--- Page 1 ---\nNorthwind Logistics: funding needs rise.\n"
    + "Filler line about freight networks.\n" * 40 + SURVEY_PAGE
    + "Filler line about lanes.\n" * 40 + MODEL_PAGE + "Filler disclosures.\n" * 20
)
FCF_STATEMENT = ("Free cash flow for the subject company turns negative in the latest periods shown, "
                 "with FCF margin declining from positive to negative.")
SURVEY_STATEMENT = "CIOs expect Acme Robotics to gain the largest share of automation budgets."


def _span(text: str, needle: str) -> tuple[int, int]:
    start = text.index(needle)
    return start, start + len(needle)


class IssuerAttributionRuleTests(unittest.TestCase):
    def test_a_table_in_another_issuers_note_is_that_issuers(self) -> None:
        start, end = _span(NOTE, "Free Cash Flow\n410")
        verdict = attribute(text=NOTE, start=start, end=end + 40, statement=FCF_STATEMENT,
                            subject_ref=ACME, issuers=ISSUERS)
        self.assertEqual(verdict["verdict"], "misattributed")
        self.assertEqual(verdict["table_issuer"], "ticker:NWND")
        self.assertEqual(verdict["basis"], "document_primary_issuer")
        self.assertEqual(verdict["primary_issuer"]["basis"], "front_matter_category")
        # Estimate columns read as the subject's actuals are recorded too.
        self.assertTrue(verdict["period"]["estimate_presented_as_actual"])
        self.assertIn("FY27E", verdict["period"]["estimate_markers"])
        self.assertIn("NWND", misattribution_rationale(verdict, "ACME"))

    def test_a_head_listing_related_companies_is_not_a_primary_issuer(self) -> None:
        primary = document_primary_issuer(NOTE, issuers=issuer_table(ISSUERS))
        self.assertEqual(primary["issuer"], "ticker:NWND")
        self.assertIn("northwind", primary["needles"])
        head_only = "related_companies: [NWND, ACME]\nA list of names and nothing else."
        self.assertIsNone(document_primary_issuer(head_only, issuers=issuer_table(ISSUERS)))

    def test_a_cross_company_survey_passage_is_kept(self) -> None:
        start, end = _span(NOTE, "CIOs named Acme Robotics")
        verdict = attribute(text=NOTE, start=start, end=end + 80, statement=SURVEY_STATEMENT,
                            subject_ref=ACME, issuers=ISSUERS)
        self.assertEqual(verdict["verdict"], "not_applicable")  # not about financials
        financial = attribute(text=NOTE, start=start, end=end + 80,
                              statement="Acme Robotics is expected to gain revenue share.",
                              subject_ref=ACME, issuers=ISSUERS)
        self.assertEqual(financial["verdict"], "consistent")
        self.assertEqual(financial["basis"], "subject_named_locally")

    def test_the_subjects_own_document_and_an_undecided_digest(self) -> None:
        own = "Acme Robotics Q2 2026 results\nRevenue 2,100 | Free cash flow -40\n" + "x\n" * 50
        start, end = _span(own, "Revenue 2,100")
        verdict = attribute(text=own, start=start, end=end, statement="Free cash flow turned negative.",
                            subject_ref=ACME, issuers=ISSUERS, title="Acme Robotics Q2 2026 results")
        self.assertEqual(verdict["verdict"], "consistent")
        digest = ("Morning digest\n" + "x\n" * 40 + "Bolt Freight capex rose; free cash flow fell to -80.\n"
                  + "y\n" * 40)
        start, end = _span(digest, "Bolt Freight capex rose")
        verdict = attribute(text=digest, start=start, end=end + 20,
                            statement="Free cash flow fell as capex rose.", subject_ref=ACME,
                            issuers=ISSUERS)
        self.assertEqual(verdict["verdict"], "ambiguous")  # never retired; asked of the model
        self.assertIn("issuers named near the cited passage", issuer_context_fact(verdict))


class _AttributionHarness(ClaimRetirementHarness):
    def setUp(self) -> None:
        super().setUp()
        self.fcf = self.claim(subject=ACME, statement=FCF_STATEMENT, source=NOTE,
                              span=(_span(NOTE, "Free Cash Flow\n410")[0],
                                    _span(NOTE, "Free Cash Flow\n410")[1] + 40),
                              document_ref="company-wiki-doc:sha256:" + "a" * 64)
        start, end = _span(NOTE, "CIOs named Acme Robotics")
        self.survey = self.claim(subject=ACME, statement=SURVEY_STATEMENT, source=NOTE,
                                 span=(start, end + 80),
                                 document_ref="company-wiki-doc:sha256:" + "a" * 64)

    def driver(self) -> ClaimReviewDriver:
        return ClaimReviewDriver(store=self.store, missions=self.missions,
                                 challenges=self.authority, spool=self.spool,
                                 needles={ACME: ACME_NEEDLES})


class RetroactiveAuditTests(_AttributionHarness):
    def test_a_financial_statement_from_the_subjects_survey_paragraph_is_kept(self) -> None:
        """A note filed and tagged as Northwind's, with a survey paragraph on Acme:
        only the Claims cut from Northwind's tables are retired."""

        start, end = _span(NOTE, "CIOs named Acme Robotics")
        kept = self.claim(subject=ACME, statement="Acme Robotics is expected to gain revenue share "
                          "of warehouse automation budgets.", source=NOTE, span=(start, end + 80),
                          document_ref="company-wiki-doc:sha256:" + "a" * 64)
        self.grant_claim_challenge()
        driver = self.driver()
        principal, _hold = driver._grant()
        summary = driver.audit_issuer_attribution(principal=principal)
        retired = retired_claim_version_refs(self.store.connection)
        self.assertEqual(retired, {self.fcf["ref"]})
        self.assertNotIn(kept["ref"], retired)
        self.assertEqual(summary["outcomes"].get("consistent"), 1)

    def test_the_audit_stops_opening_originals_when_its_seconds_are_spent(self) -> None:
        # 2026-10-10 legacy: ~7 s per original on the writer's store thread;
        # twelve of them overran the 30 s store budget.  Past the budget no
        # further original is opened this tick; the rest wait.
        other = NOTE.replace("Northwind", "Northwind Holdings")
        start, end = _span(other, "CIOs named Acme Robotics")
        self.claim(subject=ACME, statement=SURVEY_STATEMENT, source=other, span=(start, end + 80),
                   document_ref="company-wiki-doc:sha256:" + "b" * 64)
        capped = self.driver().audit_issuer_attribution(principal=None, max_seconds=0)
        self.assertEqual(capped["documents_read"], 1)
        self.assertTrue(capped["out_of_time"])
        self.assertGreaterEqual(capped["remaining"], 1)
        # The next tick picks up the original that waited.
        later = self.driver().audit_issuer_attribution(principal=None, max_seconds=None)
        self.assertEqual((later["documents_read"], later["remaining"]), (1, 0))
        self.assertNotIn("out_of_time", later)

    def test_without_the_grant_the_audit_reports_and_writes_no_challenge(self) -> None:
        summary = self.driver().audit_issuer_attribution(principal=None)
        self.assertEqual([item["claim_version_ref"] for item in summary["would_retire"]],
                         [self.fcf["ref"]])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_retirement_challenges").fetchone()[0], 0)

    def test_the_audit_retires_through_the_authority_with_its_evidence(self) -> None:
        self.grant_claim_challenge()
        driver = self.driver()
        principal, _hold = driver._grant()
        summary = driver.audit_issuer_attribution(principal=principal)
        self.assertEqual([item["claim_version_ref"] for item in summary["retired"]], [self.fcf["ref"]])
        self.assertIn(self.fcf["ref"], retired_claim_version_refs(self.store.connection))
        self.assertNotIn(self.survey["ref"], retired_claim_version_refs(self.store.connection))
        row = self.store.connection.execute(
            "SELECT reason_code, record_json FROM claim_retirement_challenges").fetchone()
        self.assertEqual(row["reason_code"], "issuer_misattribution")
        evidence = json.loads(row["record_json"])["evidence"]
        self.assertEqual(evidence["table_issuer"], "ticker:NWND")
        self.assertEqual(evidence["primary_issuer"]["basis"], "front_matter_category")
        # Bounded and idempotent: an audited Claim is not read again.
        again = driver.audit_issuer_attribution(principal=principal)
        self.assertEqual((again["audited"], again["retired"]), (0, []))

    def test_the_authority_re_runs_the_rule_before_it_retires(self) -> None:
        self.grant_claim_challenge()
        challenge = self.authority.challenge(
            claim_version_ref=self.survey["ref"], claim_version_hash=self.survey["hash"],
            reason_code="issuer_misattribution", rationale="claimed", actor_ref=AUTOMATION)
        start, end = _span(NOTE, "CIOs named Acme Robotics")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                decision="retired", actor_ref=AUTOMATION, rationale="r", source_text=NOTE,
                attribution_inputs={"start": start, "end": end + 80, "issuers": ISSUERS})


class ProductRewriteFlagTests(_AttributionHarness):
    def setUp(self) -> None:
        super().setUp()
        self.store.connection.execute(
            "CREATE TABLE synthetic_dossier_versions (version_id TEXT, dossier_ref TEXT, "
            "version_number INTEGER, company_ref TEXT, record_json TEXT)")
        tables = {"company_dossier": ("synthetic_dossier_versions", "dossier_ref", "company_ref")}
        original = product_rewrite_flags.PRODUCT_TABLES
        product_rewrite_flags.PRODUCT_TABLES = tables
        self.addCleanup(setattr, product_rewrite_flags, "PRODUCT_TABLES", original)
        for number, cites in ((1, [self.fcf["ref"]]), (2, [self.fcf["ref"], self.survey["ref"]])):
            record = {"sections": [
                {"aspect": "management_and_capital_allocation",
                 "slots": [{"text": "FCF turns negative", "refs": cites}]},
                {"aspect": "demand_drivers", "slots": [{"refs": [self.survey["ref"]]}]}]}
            self.store.connection.execute(
                "INSERT INTO synthetic_dossier_versions VALUES(?,?,?,?,?)",
                (f"dossier-version:acme:{number}", "dossier:acme", number, ACME, json.dumps(record)))

    def test_the_current_version_citing_a_retired_claim_needs_rewrite(self) -> None:
        self.grant_claim_challenge()
        driver = self.driver()
        principal, _hold = driver._grant()
        driver.audit_issuer_attribution(principal=principal)
        result = driver.flag_products_for_rewrite(principal=principal)
        self.assertEqual(len(result["flagged"]), 1)
        flags = needs_rewrite(self.store.connection, subject_ref=ACME)
        self.assertEqual(len(flags), 1)
        flag = flags[0]
        self.assertEqual(flag["product_version_ref"], "dossier-version:acme:2")  # current only
        self.assertEqual([item["section"] for item in flag["sections"]],
                         ["management_and_capital_allocation"])
        self.assertEqual(flag["claim_version_refs"], [self.fcf["ref"]])
        self.assertEqual(flag["rewrite_policy"], "batched")
        # Idempotent; and a newer version answers the flag without closing anything.
        self.assertEqual(ProductRewriteFlagAuthority(self.store).flag_all(
            actor_ref=principal)["flagged"], [])
        self.store.connection.execute(
            "INSERT INTO synthetic_dossier_versions VALUES(?,?,?,?,?)",
            ("dossier-version:acme:3", "dossier:acme", 3, ACME, json.dumps({"sections": []})))
        self.assertEqual(needs_rewrite(self.store.connection, subject_ref=ACME), [])

    def test_replacement_material_gates_the_rewrite(self) -> None:
        self.grant_claim_challenge()
        driver = self.driver()
        principal, _hold = driver._grant()
        driver.audit_issuer_attribution(principal=principal)
        driver.flag_products_for_rewrite(principal=principal)
        flag = needs_rewrite(self.store.connection, subject_ref=ACME)[0]
        self.assertEqual(flag["severity"], "immediate")
        self.assertEqual({item["kind"] for item in flag["requires_material"]},
                         {"sec_exhibit", "transcript", "sec_10q", "web_page"})
        self.assertFalse(flag["rewrite_ready"])
        self.assertEqual(flag["rewrite_gate"], "awaiting_material")
        arrivals = [{"company_ref": ACME, "kind": "sec_exhibit", "document_ref": "sec:acme:99.1",
                     "indexed_at": "2999-01-01T00:00:00+00:00"},
                    {"company_ref": "company:ticker:other", "kind": "transcript",
                     "document_ref": "x", "indexed_at": "2999-01-01T00:00:00+00:00"}]
        ready = needs_rewrite(self.store.connection, subject_ref=ACME, arrivals=arrivals)[0]
        self.assertTrue(ready["rewrite_ready"])
        self.assertEqual(ready["arrived_material"], ["sec:acme:99.1"])

    def test_any_retirement_counts_once_a_section_has_too_few_standing_claims(self) -> None:
        self.grant_claim_challenge()
        # A person retires the survey Claim (not an issuer error): the section
        # citing only it falls below the threshold; nothing else is flagged.
        self.authority.retire_by_hand(claim_version_ref=self.survey["ref"],
                                      claim_version_hash=self.survey["hash"],
                                      actor_ref="human:owner", rationale="out of date")
        result = ProductRewriteFlagAuthority(self.store).flag_all(actor_ref=AUTOMATION)
        self.assertEqual(len(result["flagged"]), 1)
        flag = needs_rewrite(self.store.connection, subject_ref=ACME)[0]
        # Batch an: no standing Claim left in the section is ``high`` (below
        # the share and nothing at all stands), above plain ``threshold``.
        self.assertEqual(flag["severity"], "high")
        sections = {item["section"]: item for item in flag["sections"]}
        self.assertEqual(set(sections), {"demand_drivers"})
        self.assertEqual(sections["demand_drivers"]["valid_share"], 0.0)
        # Unchanged state: the next scan reads nothing.
        self.assertEqual(ProductRewriteFlagAuthority(self.store).flag_all(
            actor_ref=AUTOMATION).get("skipped"), "unchanged")


class AdmissionAttributionTests(unittest.TestCase):
    def service(self) -> DocumentExtractionService:
        service = DocumentExtractionService.__new__(DocumentExtractionService)
        service._document_text = lambda context: NOTE
        service._mission_plans = lambda: []
        service.mission_names = lambda mission: {"ACME": ("Acme Robotics",)}

        class _Row(dict):
            pass

        class _Connection:
            def execute(self, *_args, **_kwargs):
                class _Cursor:
                    def fetchone(self_inner):
                        return {"title": "NORTHWIND_20260210_0101.pdf"}
                return _Cursor()

        service.writer = type("W", (), {"store": type("S", (), {"connection": _Connection()})()})()
        return service

    def test_admission_holds_the_table_and_records_attribution_evidence(self) -> None:
        service = self.service()
        mission = {"universe": [{"company_ref": ACME, "ticker": "ACME", "name": "Acme Robotics"}]}
        context = {"content_hash": "c" * 64, "document_ref": "company-wiki-doc:x"}
        check = service.admission_attribution_check(context, None, mission)
        start, end = _span(NOTE, "Free Cash Flow\n410")
        table = {"id": "suggestion:1", "normalized_statement": FCF_STATEMENT,
                 "citation": {"source_start": start, "source_end": end + 40}}
        verdict = check(ACME, table)
        self.assertEqual(verdict["verdict"], "misattributed")
        self.assertEqual(service.attribution_evidence(context, table, ACME), verdict)
        survey = {"id": "suggestion:2", "normalized_statement": SURVEY_STATEMENT,
                  "citation": {"source_start": 0, "source_end": 10}}
        self.assertIsNone(check(ACME, survey))  # not about financials: nothing to decide


# -- forecast normalisation, on Synthetic Widgets Co. --------------------------


def build(data: dict[str, Any], sources: dict[str, Any] | None = None) -> dict[str, Any]:
    structure, replay = materialize_financial_statement_structure(data["spec"], data["table"])
    binding = forecast_structure_binding(structure, replay, data["table"])
    return build_structured_forecast_model(
        data["spec"], data["table"], structure=structure, replay=replay, binding=binding,
        actor_ref="automation:test", assumption_sources=sources)


def stored(body: dict[str, Any]) -> dict[str, Any]:
    record = {key: value for key, value in body.items() if key != "source_version_ref"}
    record.update({"id": "forecast-model-version:synw:1", "created_at": "2026-10-09T00:00:00+00:00",
                   "version": 1, "prior_version_ref": None})
    record["body_hash"] = body_hash(record)
    record["content_hash"] = content_hash(record)
    return validate_forecast_model(record)


def assumptions_for(body: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    return sorted((item for item in body["assumptions"] if item["driver_ref"] == f"concept:{concept}"),
                  key=lambda item: item["period"]["end"])


def cells(body: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    return next(item for item in body["results"] if item["ref"] == ref)["cells"]


def losing_judgement(data: dict[str, Any]) -> dict[str, Any]:
    """An analyst assumption (exempt from the growth cap) that sends R&D up 45% a quarter."""

    ends = [item["end"] for item in build(copy.deepcopy(data))["forecast_periods"]]
    driver = "concept:us-gaap:ResearchAndDevelopmentExpense"
    return {"judgement": [
        {"driver_ref": driver, "period_end": end, "measure": "quarterly_growth", "value": "0.45",
         "because": "owner: an aggressive investment cycle",
         "refs": [{"kind": "human_decision", "ref": "assumption:rnd", "concept": None,
                   "period_end": end, "accession": None}]} for end in ends]}


def one_offs(*concepts: str) -> dict[str, Any]:
    return {"one_offs": {"concepts": list(concepts), "items": []}}


class OneOffNormalisationTests(unittest.TestCase):
    def test_the_spike_the_spec_calls_one_off_is_taken_out_of_the_base(self) -> None:
        data = company(gna_spike="2500000000")
        flagged = build(data, one_offs("us-gaap:GeneralAndAdministrativeExpense"))
        plain = build(company(gna_spike="2500000000"))
        gna = assumptions_for(flagged, "us-gaap:GeneralAndAdministrativeExpense")
        self.assertEqual(gna[0]["provenance"]["rule_ref"], RULE_NORMALISED)
        self.assertIn("base normalised", gna[0]["because"])
        self.assertLess(Decimal(gna[0]["value"]), Decimal("0.02"))  # the underlying ~1% a quarter
        compounding = assumptions_for(plain, "us-gaap:GeneralAndAdministrativeExpense")
        self.assertGreater(Decimal(compounding[0]["value"]), Decimal("0.05"))
        flagged_first = Decimal(cells(flagged, "result:operating_expense:g-and-a")[0]["value"])
        self.assertLess(flagged_first, Decimal("1300000000"))
        stored(flagged)

    def test_an_explicit_one_off_amount_is_subtracted_from_its_quarter(self) -> None:
        values = {"2026-03-31": Decimal(100), "2026-06-30": Decimal(400)}
        out, records = normalise_base(values, flagged=False, explicit=[
            {"period_end": "2026-06-30", "amount": "240", "ref": None, "because": "legal charge"}])
        self.assertEqual(out["2026-06-30"], Decimal(160))
        self.assertEqual(records[0]["basis"], "explicit_one_off")

    def test_a_recurring_line_the_spec_calls_one_off_is_never_held_at_zero(self) -> None:
        data = company(gna_spike="2500000000")
        structure, _replay = materialize_financial_statement_structure(data["spec"], data["table"])
        gna = next(line for line in structure["lines"] if line["ref"] == "g-and-a")
        self.assertNotEqual(gna["forecast_method"], "zero_one_off")


class GrowthInvariantTests(unittest.TestCase):
    def test_opex_cannot_outgrow_revenue_beyond_the_allowance_for_long(self) -> None:
        body = build(company(rnd_growth="0.06"))
        revenue = assumptions_for(body, "us-gaap:Revenues")
        rnd = assumptions_for(body, "us-gaap:ResearchAndDevelopmentExpense")
        rules = [item["provenance"]["rule_ref"] for item in rnd]
        self.assertNotIn(RULE_GROWTH_CAP, rules[:2])
        self.assertTrue(all(rule == RULE_GROWTH_CAP for rule in rules[2:]))
        for spend, sales in zip(rnd[2:], revenue[2:]):
            self.assertLessEqual(Decimal(spend["value"]),
                                 Decimal(sales["value"]) + OPEX_EXCESS_PP + Decimal("0.000001"))
        stored(body)

    def test_an_analyst_assumption_overrides_the_cap(self) -> None:
        base = build(company(rnd_growth="0.06"))
        driver = "concept:us-gaap:ResearchAndDevelopmentExpense"
        ends = [item["end"] for item in base["forecast_periods"]]
        sources = {"judgement": [{"driver_ref": driver, "period_end": ends[3],
                                  "measure": "quarterly_growth", "value": "0.09",
                                  "because": "owner: hiring plan", "refs": [
                                      {"kind": "human_decision", "ref": "assumption:x",
                                       "concept": None, "period_end": ends[3], "accession": None}]}]}
        body = build(company(rnd_growth="0.06"), sources)
        rnd = {item["period"]["end"]: item for item in assumptions_for(
            body, "us-gaap:ResearchAndDevelopmentExpense")}
        self.assertEqual(Decimal(rnd[ends[3]]["value"]), Decimal("0.09"))


class ShareCountBoundTests(unittest.TestCase):
    def test_a_shrink_the_buyback_cannot_fund_is_bounded(self) -> None:
        body = build(company(share_shrink="0.03", recent_buyback="10000000"))
        shares = assumptions_for(body, "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")
        self.assertEqual(shares[0]["provenance"]["rule_ref"], RULE_SHARE_BOUND)
        self.assertGreater(Decimal(shares[0]["value"]), Decimal("-0.03"))

    def test_no_buyback_line_holds_the_count_flat(self) -> None:
        data = company(share_shrink="0.03")
        data["table"]["filed_lines"] = [line for line in data["table"]["filed_lines"]
                                        if line["concept"] != "us-gaap:PaymentsForRepurchaseOfCommonStock"]
        body = build(data)
        shares = assumptions_for(body, "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")
        self.assertEqual(Decimal(shares[0]["value"]), Decimal(0))
        self.assertIn("not backed by any buyback line", shares[0]["because"])


class GuidanceBoundTests(unittest.TestCase):
    def test_revenue_guidance_bounds_the_first_forecast_quarter(self) -> None:
        base = build(company())
        first = base["forecast_periods"][0]["end"]
        trailing = Decimal(cells(base, "result:revenue")[0]["value"])
        high = (trailing * Decimal("0.98")).quantize(Decimal(1))
        bound = {first: {"low": str(high - 500_000_000), "high": str(high),
                         "ref": {"kind": "figure", "ref": "guidance:synw:q3", "concept": None,
                                 "period_end": first, "accession": None},
                         "because": "company guidance for the quarter"}}
        body = build(company(), {"revenue_bounds": bound})
        revenue = assumptions_for(body, "us-gaap:Revenues")
        self.assertEqual(revenue[0]["provenance"]["rule_ref"], RULE_GUIDANCE_BOUND)
        self.assertEqual(Decimal(cells(body, "result:revenue")[0]["value"]).quantize(Decimal(1)), high)
        stored(body)


class CashBridgeTests(unittest.TestCase):
    def test_operating_cash_flow_is_derived_from_earnings_when_the_lines_exist(self) -> None:
        data = company()
        asset = copy.deepcopy(next(l for l in data["table"]["filed_lines"] if l["concept"] == "us-gaap:DepreciationDepletionAndAmortization"))
        asset.update(concept="us-gaap:PropertyPlantAndEquipmentNet", statement="balance", label="Fixed assets", period_basis="instant")
        for cell in asset["cells"].values(): cell.update(period_start=None, value="5000000000")
        data["table"]["filed_lines"].append(asset)
        body = build(data)
        ocf = next(item for item in body["results"] if item["ref"] == "result:operating_cash_flow")
        self.assertIn("net_income[k] + depreciation_amortization[k]", ocf["formula"])
        self.assertIn("other_operating_items", ocf["formula"])
        self.assertTrue(all(cell["status"] == "computed" for cell in ocf["cells"]))
        stored(body)

    def test_a_cash_flow_rising_through_deep_losses_is_unavailable(self) -> None:
        income = [
            {"ref": "result:revenue", "role": "revenue", "cells": [
                {"period": {"end": end}, "status": "computed", "value": "100"} for end in ("q1", "q2")]},
            {"ref": "result:operating_income", "role": "operating_income", "cells": [
                {"period": {"end": "q1"}, "status": "computed", "value": "5"},
                {"period": {"end": "q2"}, "status": "computed", "value": "-30"}]},
        ]
        conflict = cash_bridge_contradiction(
            income, [{"end": "q1"}, {"end": "q2"}],
            {"q1": Decimal(50), "q2": Decimal(55)})
        self.assertEqual(conflict["period_end"], "q2")
        self.assertIn(CASH_BRIDGE_RULE, conflict["reason"])

    def test_without_d_and_a_a_loss_making_model_states_the_contradiction(self) -> None:
        data = company()
        data["table"]["filed_lines"] = [
            line for line in data["table"]["filed_lines"]
            if line["concept"] != "us-gaap:DepreciationDepletionAndAmortization"]
        body = build(data, losing_judgement(data))
        ocf = cells(body, "result:operating_cash_flow")
        fcf = cells(body, "result:free_cash_flow")
        operating = cells(body, "result:operating_income")
        loss = [cell["period"]["end"] for cell in operating
                if cell["status"] == "computed" and Decimal(cell["value"]) < 0]
        self.assertTrue(loss)
        unavailable = [cell for cell in ocf if cell["status"] != "computed"]
        self.assertTrue(unavailable)
        self.assertIn(CASH_BRIDGE_RULE, unavailable[0]["reason"])
        self.assertTrue(any(CASH_BRIDGE_RULE in str(cell.get("reason")) for cell in fcf))
        stored(body)


class NonOperatingGainTests(unittest.TestCase):
    def test_securities_gains_are_not_extrapolated(self) -> None:
        body = build(company(nonoperating="3000000000"))
        nonop = assumptions_for(body, "us-gaap:NonoperatingIncomeExpense")
        self.assertTrue(all(Decimal(item["value"]) == 0 for item in nonop))
        small = build(company())
        self.assertTrue(all(Decimal(item["value"]) > 0 for item in assumptions_for(
            small, "us-gaap:NonoperatingIncomeExpense")))


class SensitivityMagnitudeTests(unittest.TestCase):
    def test_a_what_if_far_beyond_the_companys_scale_refuses_the_projection(self) -> None:
        from dalton_core.forecast_sensitivity import (
            MAGNITUDE_RULE_REF, SensitivityUnavailable, build_projection, magnitude_breaches)

        model = build(company())
        model["id"] = "forecast-model-version:synw:1"
        sane = [{"label": "R&D", "what_if": [{"scenario": "peak", "lines": [
            {"ref": "result:operating_income", "cells": [
                {"period_end": "2026-09-30", "status": "computed", "value": "-9000000000"}]}]}]}]
        self.assertEqual(magnitude_breaches(model, sane), [])
        absurd = [{"label": "R&D", "what_if": [{"scenario": "peak", "lines": [
            {"ref": "result:operating_income", "cells": [
                {"period_end": "2026-09-30", "status": "computed", "value": "-292100000000"}]}]}]}]
        found = magnitude_breaches(model, absurd)
        self.assertEqual(len(found), 1)
        self.assertIn("-292100000000", found[0])
        try:
            build_projection(model)
        except SensitivityUnavailable as exc:  # a sane model never trips the gate
            self.assertNotIn(MAGNITUDE_RULE_REF, str(exc))


# -- the return bridge -----------------------------------------------------------


def series(net: str = "36000000000", revenue: str = "52000000000") -> dict[str, Any]:
    return {"ttm": [{"end": "2026-06-30", "rule": "annual",
                     "net_income_to_common": {"value": net}, "diluted_eps": {"value": "72"},
                     "revenue": {"value": revenue}, "diluted_shares": {"value": "500000000"},
                     "dps": None}]}


SNAPSHOT = {"id": "valuation-snapshot-version:synw:1", "metrics": [],
            "price": {"close": "1000", "bar_date": "2026-07-15", "version_ref": "price:synw"}}


def model_record(data: dict[str, Any] | None = None,
                 sources: dict[str, Any] | None = None) -> dict[str, Any]:
    body = build(data or company(), sources)
    body["id"] = "forecast-model-version:synw:1"
    return body


class DriverLegTests(unittest.TestCase):
    def test_no_net_income_means_no_return_and_no_proxy(self) -> None:
        model = model_record()
        for result in model["results"]:
            if result.get("role") in ("net_income", "parent_net_income"):
                result["cells"] = [{**cell, "status": "unavailable", "value": None,
                                    "reason": "test"} for cell in result["cells"]]
        bridge = build_return_bridge(COMPANY, series=series(), series_ref=None, snapshot=SNAPSHOT,
                                     price=None, model=model, consensus=None)
        leg = next(item for item in bridge["legs"] if item["leg"] == "expected_driver_model")
        self.assertEqual(leg["status"], "unavailable")
        self.assertEqual(leg["reason_code"], "model_net_income_unavailable")
        self.assertNotIn("trailing_net_margin", leg)

    def test_material_nonoperating_gains_require_normalised_core_tax_evidence(self) -> None:
        model = model_record(company(nonoperating="3000000000"))
        bridge = build_return_bridge(COMPANY, series=series(net="90000000000"), series_ref=None,
                                     snapshot=SNAPSHOT, price=None, model=model, consensus=None)
        leg = next(item for item in bridge["legs"] if item["leg"] == "expected_driver_model")
        self.assertEqual(leg["status"], "unavailable")
        self.assertEqual(leg["reason_code"], "normalised_core_tax_authority_required")
        self.assertIn("营业外", leg["reason"])
        self.assertIn("core_operating_income_forward", leg)
        self.assertNotIn("core_earnings_forward", leg)
        self.assertEqual(bridge["model_consistency"]["status"], "ok")

    def test_a_loss_making_model_puts_the_contradiction_beside_every_expected_return(self) -> None:
        model = model_record(company(), losing_judgement(company()))
        check = model_profit_check(model)
        self.assertEqual(check["status"], "loss_making")
        bridge = build_return_bridge(COMPANY, series=series(), series_ref=None, snapshot=SNAPSHOT,
                                     price=None, model=model, consensus=None)
        leg = next(item for item in bridge["legs"] if item["leg"] == "expected_driver_model")
        if leg["status"] == "available":
            self.assertIn("矛盾提示", leg["contradiction"])
        else:
            self.assertIn(leg["reason_code"], {"model_core_earnings_negative",
                                               "trailing_core_not_positive"})
        self.assertIn("矛盾提示", bridge["model_consistency"]["notice"])


class TargetLegTests(unittest.TestCase):
    def test_the_consensus_leg_is_a_target_implied_return_on_twelve_months(self) -> None:
        consensus = {"id": "consensus-estimate-version:synw", "content_hash": "c" * 64,
                     "fiscal_calendar": {"last_reported_period_end": "2026-06-30"},
                     "eps_estimates": [{"label": "FY2026", "period_kind": "fiscal_year",
                                        "period_end": "2026-12-31", "vendor_period": "0y",
                                        "avg": "80", "year_ago": "72"}],
                     "price_target": {"mean": "1100"},
                     "report_consensus": {"mean": "1050", "estimate_refs": ["street-estimate:s"]}}
        series_with_dps = series()
        series_with_dps["ttm"][0]["dps"] = {"value": "20"}
        bridge = build_return_bridge(COMPANY, series=series_with_dps, series_ref=None,
                                     snapshot=SNAPSHOT, price=None, model=None, consensus=consensus)
        legs = {item["leg"]: item for item in bridge["legs"]}
        for name, target in (("expected_consensus", "10.00"), ("expected_sell_side", "5.00")):
            leg = legs[name]
            self.assertIn("目标价隐含回报", leg["label"])
            self.assertEqual(leg["window"], {"start": "2026-07-15", "end": "2027-07-15"})
            self.assertEqual(leg["target_implied_percent"], target)
            self.assertEqual(leg["dividend_yield_percent"], "2.00")
            self.assertIn("总回报 = 目标价隐含", leg["notes"][0])


class OutlookGuidanceTests(unittest.TestCase):
    def test_guidance_ranges_are_read_from_the_outlook_records_by_name(self) -> None:
        import sqlite3
        import sys
        import types

        from dalton_core.forecast_assumption_sources import revenue_guidance_bounds

        module = types.ModuleType("dalton_core.earnings_release_outlook")
        module.latest_outlook = lambda connection, company_ref, state_dir=None: {
            "accession": "0009999999-26-000009", "items": [
                {"kind": "guidance", "metric": "revenue", "period_kind": "quarter",
                 "period_end": "2026-09-30", "low": "12.4", "high": "12.9", "scale": "billion",
                 "unit": "currency", "period": "Q3 2026"},
                {"kind": "reported", "metric": "revenue", "period_kind": "quarter",
                 "period_end": "2026-06-30", "value": "12.5"}]}
        previous = sys.modules.get("dalton_core.earnings_release_outlook")
        sys.modules["dalton_core.earnings_release_outlook"] = module
        try:
            bounds, notes = revenue_guidance_bounds(sqlite3.connect(":memory:"), COMPANY)
        finally:
            if previous is None:
                sys.modules.pop("dalton_core.earnings_release_outlook", None)
            else:
                sys.modules["dalton_core.earnings_release_outlook"] = previous
        self.assertEqual(bounds["2026-09-30"]["low"], "12400000000.0")
        self.assertEqual(bounds["2026-09-30"]["high"], "12900000000.0")
        self.assertIn("latest_outlook", notes[0])


if __name__ == "__main__":
    unittest.main()
