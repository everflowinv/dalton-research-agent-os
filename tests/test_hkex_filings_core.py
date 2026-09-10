"""W4: the identity, the governance records, the quota and the URLs.

Everything here is offline and everything is about the *shape* of what would be
asked for -- which host, which approval, which category code -- rather than
about any answer. The answers are ``test_hkex_filings_adapter``'s job.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from dalton_core.connector_governance import ConnectorGovernance
from dalton_core.connector_inventory import (
    PROFILE_DEFINITIONS,
    load_packaged_connector_inventory,
)
from dalton_core.connector_quota_policy import governed_daily_quota
from dalton_core.hkex_filings_core import (
    ANNOUNCEMENTS_INDEX_OPERATION,
    CAPABILITY_BY_OPERATION,
    COMPANY_REF_PREFIX,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    ENDPOINT_FRAGILITY,
    FIGURE_GRADE_BY_OPERATION,
    HKEX_EVIDENCE_TIER,
    HKEX_GRADE,
    HOSTS_BY_OPERATION,
    KIND_BY_OPERATION,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    OPERATIONS,
    SOURCE_REF,
    TIER_ONE_ALL,
    TIER_ONE_CODES,
    HkexFilingsError,
    build_hkex_filings_governance_record,
    company_ref,
    di_all_form_list_url,
    di_corp_list_url,
    di_form_detail_url,
    hkex_adapter_hash,
    hkex_identity,
    hkex_output_schema,
    hkex_schema_hash,
    hkex_source_hash,
    invocation_ref,
    normalise_ticker,
    share_buyback_report_url,
    stock_prefix_url,
    ticker_for_company_ref,
    title_search_url,
    yahoo_ticker,
)

REPO = Path(__file__).resolve().parents[1]


class IdentityTests(unittest.TestCase):
    def test_four_operations_four_schema_hashes_one_source(self) -> None:
        # Four approvals, not one: reading what a company bought back is not
        # permission to read who its directors are.
        hashes = {hkex_schema_hash(operation) for operation in OPERATIONS}
        self.assertEqual(len(hashes), 4)
        sources = {hkex_identity(operation)["source_hash"] for operation in OPERATIONS}
        self.assertEqual(sources, {hkex_source_hash()})

    def test_the_adapter_hash_names_the_reader_and_its_hosts(self) -> None:
        # Rewriting the reader, or letting it reach a different host, is a
        # different capability rather than a maintenance detail.
        first = hkex_adapter_hash(NEXT_DAY_DISCLOSURE_OPERATION)
        self.assertNotEqual(first, hkex_adapter_hash(ANNOUNCEMENTS_INDEX_OPERATION))
        self.assertEqual(first, hkex_adapter_hash(NEXT_DAY_DISCLOSURE_OPERATION))

    def test_an_unknown_operation_is_refused_by_name(self) -> None:
        with self.assertRaises(HkexFilingsError) as caught:
            hkex_schema_hash("share_buybacks")
        self.assertIn("share_buybacks", str(caught.exception))

    def test_the_grade_keeps_these_out_of_every_figure_path(self) -> None:
        from dalton_core.document_figure_grade import GRADE_BY_SPEC
        from dalton_core.research_verification import FIGURE_ADMISSIBLE_GRADES

        self.assertNotIn(HKEX_GRADE, set(GRADE_BY_SPEC.values()))
        self.assertNotIn(HKEX_GRADE, FIGURE_ADMISSIBLE_GRADES)
        self.assertEqual(FIGURE_GRADE_BY_OPERATION, {})

    def test_no_statement_line_can_come_from_here(self) -> None:
        from dalton_core.statement_snapshot import _FORMS

        self.assertNotIn("Next Day Disclosure Return", _FORMS)
        self.assertNotIn(SOURCE_REF, _FORMS)

    def test_the_evidence_tier_is_a_frozen_vocabulary_word(self) -> None:
        from dalton_core.research_event import EVIDENCE_TIERS

        self.assertIn(HKEX_EVIDENCE_TIER, EVIDENCE_TIERS)


class CompanyRefTests(unittest.TestCase):
    def test_the_hong_kong_scheme_round_trips(self) -> None:
        self.assertEqual(company_ref("700"), "company:hk-secucode:00700.HK")
        self.assertEqual(company_ref("00700"), "company:hk-secucode:00700.HK")
        self.assertEqual(ticker_for_company_ref("company:hk-secucode:00700.HK"), "00700")

    def test_a_sec_company_ref_is_not_a_hong_kong_one(self) -> None:
        # The whole point of the scheme: this lane must never pick up a US
        # name and go looking for it in Hong Kong.
        self.assertIsNone(ticker_for_company_ref("company:sec-cik:1467373"))
        self.assertIsNone(ticker_for_company_ref("company:hk-secucode:700.HK"))
        self.assertIsNone(ticker_for_company_ref(None))

    def test_the_three_spellings_of_one_company(self) -> None:
        # 700 in the Exchange's report, 00700 on HKEXnews, 0700.HK on Yahoo.
        self.assertEqual(normalise_ticker("711"), "00711")
        self.assertEqual(normalise_ticker("1"), "00001")
        self.assertEqual(yahoo_ticker("00700"), "0700.HK")
        with self.assertRaises(HkexFilingsError):
            normalise_ticker("00700.HK")

    def test_the_mission_universe_is_not_widened_here(self) -> None:
        # This slice introduces a ref *scheme*. Admitting a Hong Kong name to
        # coverage is an owner decision, and nothing in this repository's
        # committed missions has made it.
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO / "deploy").rglob("*.json")
        )
        self.assertNotIn(COMPANY_REF_PREFIX, text)


class PriceAuthorityTests(unittest.TestCase):
    def test_the_price_authority_accepts_only_four_digit_hk_symbols(self) -> None:
        from dalton_core.market_price import _TICKER_RE
        for ticker in (yahoo_ticker("00700"), "ACN", "BRK.B"):
            self.assertIsNotNone(_TICKER_RE.fullmatch(ticker))
        for ticker in ("700.HK", "00700.HK", "0700", "1234.US", "1ABC"):
            self.assertIsNone(_TICKER_RE.fullmatch(ticker))


class UrlTests(unittest.TestCase):
    def test_the_buyback_report_url_is_the_one_path_that_serves_it(self) -> None:
        # www.hkexnews.hk and www1 both redirect this path to a lowercase one
        # that 404s; only www3 serves the workbook.
        self.assertEqual(
            share_buyback_report_url("2026-09-09"),
            "https://www3.hkexnews.hk/reports/sharerepur/documents/SRRPT20260909.xls",
        )

    def test_the_title_search_refuses_the_category_that_answers_zero(self) -> None:
        # `t1code=-1` is accepted upstream, answers HTTP 200 and reports zero
        # records for an issuer that filed two dozen documents. It cannot be
        # composed here at all.
        self.assertNotIn("-1", TIER_ONE_CODES)
        with self.assertRaises(HkexFilingsError):
            title_search_url(stock_id=7609, since="2026-08-01", until="2026-09-10",
                             tier_one="-1")
        url = title_search_url(stock_id=7609, since="2026-08-01", until="2026-09-10")
        self.assertIn("t1code=-2", url)
        self.assertIn("t2Gcode=-2", url)
        self.assertIn("from=20260801", url)

    def test_a_stock_id_is_the_integer_the_prefix_endpoint_gave(self) -> None:
        for bad in (0, -1, "7609", True):
            with self.assertRaises(HkexFilingsError):
                title_search_url(stock_id=bad, since="2026-08-01", until="2026-09-10")

    def test_the_di_urls_use_the_databases_own_date_shape(self) -> None:
        # dd/mm/yyyy on the DI database, yyyymmdd on HKEXnews. Two shapes for
        # one date is exactly the kind of thing that works until the day of the
        # month passes twelve.
        url = di_corp_list_url(ticker="00700", since="2026-01-08", until="2026-09-10")
        self.assertIn("scsd=08%2F01%2F2026", url)
        self.assertIn("sced=10%2F09%2F2026", url)

    def test_a_form_list_needs_the_sid_the_corp_list_gave(self) -> None:
        for bad in (0, -3, "6893", True):
            with self.assertRaises(HkexFilingsError):
                di_all_form_list_url(sid=bad, corporation_name="X", ticker="00700",
                                     since="2026-01-01", until="2026-09-10")

    def test_a_form_path_that_is_not_a_form_path_is_refused(self) -> None:
        for bad in ("../secrets.aspx?fn=X", "http://elsewhere/NSForm3A.aspx?fn=X",
                    "NSForm3A.aspx?fn=X&extra=1"):
            with self.assertRaises(HkexFilingsError):
                di_form_detail_url(form_path=bad, sid=6893, corporation_name="X",
                                   ticker="00700", since="2026-01-01",
                                   until="2026-09-10")
        good = di_form_detail_url(
            form_path="NSForm3A.aspx?fn=DA20260819E00389", sid=6893,
            corporation_name="Tencent Holdings Ltd.", ticker="00700",
            since="2026-01-01", until="2026-09-10",
        )
        self.assertTrue(good.startswith("https://di.hkex.com.hk/di/NSForm3A.aspx"))

    def test_an_announcement_path_may_not_traverse(self) -> None:
        from dalton_core.hkex_filings_core import announcement_document_url

        with self.assertRaises(HkexFilingsError):
            announcement_document_url("/listedco/../../etc/passwd")
        with self.assertRaises(HkexFilingsError):
            announcement_document_url("https://elsewhere/doc.pdf")
        self.assertEqual(
            announcement_document_url("/listedco/listconews/sehk/2026/0909/x.pdf"),
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0909/x.pdf",
        )

    def test_a_stock_prefix_url_carries_the_padded_code(self) -> None:
        self.assertIn("name=00700", stock_prefix_url("700"))


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = load_packaged_connector_inventory()
        self.profile = self.inventory["templates"]["hkex-filings"]

    def test_the_profile_declares_exactly_the_hosts_the_operations_reach(self) -> None:
        declared = set(self.profile["transport"]["allowed_hosts"])
        reached = {host for hosts in HOSTS_BY_OPERATION.values() for host in hosts}
        self.assertEqual(declared, reached)

    def test_it_is_public_https_with_no_credential_anywhere(self) -> None:
        self.assertEqual(self.profile["transport"]["kind"], "public_https")
        self.assertEqual(self.profile["transport"]["host_policy"], "literal_allowlist")
        self.assertEqual(self.profile["auth_boundary"]["mode"], "none")
        serialized = json.dumps(self.inventory, ensure_ascii=False).lower()
        self.assertNotIn("cookie:", serialized)
        self.assertNotIn("viewstate", serialized)

    def test_the_refused_routes_are_named_with_their_reasons(self) -> None:
        forbidden = set(self.profile["route_restrictions"]["forbidden_target_refs"])
        self.assertIn("route:hkexnews-title-search-tier-one-minus-one", forbidden)
        self.assertIn("route:hkexnews-jsf-titlesearch-xhtml", forbidden)
        self.assertIn("route:third-party-buyback-transcription", forbidden)
        self.assertIn("route:arbitrary-attachment-url", forbidden)

    def test_no_operation_declares_a_fallback(self) -> None:
        # Each of these three surfaces is the only publisher of what it
        # publishes; there is nothing to fall back to that is the same fact.
        self.assertEqual(self.profile["route_restrictions"]["fallback_routes"], [])

    def test_only_the_daily_tape_claims_enumeration(self) -> None:
        ceiling = {
            item["operation"]: item["completeness_ceiling"]
            for item in self.profile["operations"]
        }
        self.assertEqual(ceiling[NEXT_DAY_DISCLOSURE_OPERATION], "enumerated")
        self.assertEqual(ceiling[ANNOUNCEMENTS_INDEX_OPERATION], "partial")
        self.assertEqual(ceiling[DISCLOSURE_OF_INTERESTS_OPERATION], "partial")
        self.assertEqual(ceiling[MONTHLY_RETURNS_OPERATION], "partial")

    def test_readiness_fabricates_no_runner_authority(self) -> None:
        readiness = self.profile["readiness"]
        self.assertEqual(readiness["level"], "inventory_connected")
        self.assertFalse(readiness["lease_eligible"])
        self.assertFalse(readiness["live_execution_allowed"])

    def test_the_monthly_return_contract_says_it_has_no_figures(self) -> None:
        schema = hkex_output_schema(MONTHLY_RETURNS_OPERATION)
        self.assertEqual(
            schema["properties"]["figures_available"]["enum"], [False]
        )

    def test_every_verbatim_figure_is_a_string_in_the_contract(self) -> None:
        # A float would lose the trailing zero of `438.20` and silently
        # renormalise `100,442,863.00`.
        schema = hkex_output_schema(NEXT_DAY_DISCLOSURE_OPERATION)
        row = schema["properties"]["rows"]["items"]["properties"]
        for field in ("shares_repurchased", "highest_price", "lowest_price",
                      "aggregate_price_paid", "mandate_to_date_shares"):
            self.assertEqual(row[field]["type"], ["string", "null"], field)

    def test_the_packaged_inventory_matches_the_frozen_definitions(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "build_connector_inventory.py"),
             "--check"],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_slug_is_in_the_definitions_exactly_once(self) -> None:
        slugs = [item["slug"] for item in PROFILE_DEFINITIONS]
        self.assertEqual(slugs.count("hkex-filings"), 1)


class GovernanceTests(unittest.TestCase):
    def test_the_committed_records_are_what_the_builder_makes(self) -> None:
        for operation in OPERATIONS:
            kind = KIND_BY_OPERATION[operation]
            path = REPO / "deploy" / "connector-governance" / f"{kind}-v1.json"
            committed = json.loads(path.read_text(encoding="utf-8"))
            built = build_hkex_filings_governance_record(
                operation=operation, approved_by="human:lumos"
            )
            self.assertEqual(committed, built, kind)

    def test_every_committed_record_is_proposed(self) -> None:
        for operation in OPERATIONS:
            kind = KIND_BY_OPERATION[operation]
            path = REPO / "deploy" / "connector-governance" / f"{kind}-v1.json"
            record = ConnectorGovernance.load(path)
            self.assertFalse(record.approved, kind)

    def test_one_operations_record_cannot_cover_another(self) -> None:
        buyback = build_hkex_filings_governance_record(
            operation=NEXT_DAY_DISCLOSURE_OPERATION, approved_by="human:lumos"
        )
        di = build_hkex_filings_governance_record(
            operation=DISCLOSURE_OF_INTERESTS_OPERATION, approved_by="human:lumos"
        )
        self.assertNotEqual(buyback["expected_schema_hash"], di["expected_schema_hash"])
        self.assertNotEqual(buyback["capability_id"], di["capability_id"])
        self.assertEqual(buyback["expected_source_hash"], di["expected_source_hash"])

    def test_a_record_needs_a_human_approver(self) -> None:
        with self.assertRaises(HkexFilingsError):
            build_hkex_filings_governance_record(
                operation=NEXT_DAY_DISCLOSURE_OPERATION,
                approved_by="automation:coverage-mission",
            )

    def test_the_records_are_registered_governance_kinds(self) -> None:
        from dalton_core.connector_governance import governance_kind_for_capability

        for operation in OPERATIONS:
            self.assertEqual(
                governance_kind_for_capability(CAPABILITY_BY_OPERATION[operation]),
                KIND_BY_OPERATION[operation],
            )


class QuotaTests(unittest.TestCase):
    def test_every_operation_has_a_governed_ceiling(self) -> None:
        for operation in OPERATIONS:
            quota = governed_daily_quota("hkex-filings", operation)
            self.assertGreaterEqual(quota["daily_unit_limit"], 1)
            self.assertGreaterEqual(quota["max_physical_calls_per_unit"], 1)

    def test_the_daily_tape_is_one_document_and_one_call(self) -> None:
        quota = governed_daily_quota("hkex-filings", NEXT_DAY_DISCLOSURE_OPERATION)
        self.assertEqual(quota["quota_unit"], "document")
        self.assertEqual(quota["max_physical_calls_per_unit"], 1)

    def test_the_di_ceiling_is_the_adapters_own_ceiling_plus_its_two_lists(self) -> None:
        # Pinned to the product rather than left near it: the run is a
        # corporation lookup, a notice list and one page per notice, and the
        # ceiling should be the truth about that rather than a hope.
        from dalton_core.hkex_filings_adapter import MAX_DI_NOTICES

        quota = governed_daily_quota("hkex-filings", DISCLOSURE_OF_INTERESTS_OPERATION)
        self.assertEqual(quota["max_physical_calls_per_unit"], MAX_DI_NOTICES + 2)

    def test_the_search_operations_pay_for_the_id_lookup(self) -> None:
        # The stock code has to be resolved to HKEXnews' internal id first and
        # there is no other route from one to the other.
        for operation in (MONTHLY_RETURNS_OPERATION, ANNOUNCEMENTS_INDEX_OPERATION):
            quota = governed_daily_quota("hkex-filings", operation)
            self.assertEqual(quota["max_physical_calls_per_unit"], 2, operation)


class FragilityTests(unittest.TestCase):
    def test_every_route_this_connector_uses_has_a_written_fragility(self) -> None:
        # The reason lives where the call is, not in a report somebody would
        # have to know to look for.
        routes = {entry["route"] for entry in ENDPOINT_FRAGILITY}
        self.assertTrue(any("SRRPT" in route for route in routes))
        self.assertTrue(any("titleSearchServlet" in route for route in routes))
        self.assertTrue(any("prefix.do" in route for route in routes))
        self.assertTrue(any("NSAllFormList" in route for route in routes))
        for entry in ENDPOINT_FRAGILITY:
            self.assertTrue(entry["observed"].strip(), entry["route"])
            self.assertGreater(len(entry["fragility"]), 80, entry["route"])


class EventVocabularyTests(unittest.TestCase):
    def test_buyback_disclosure_is_a_declared_kind_with_the_agreed_fields(self) -> None:
        # Coordinated verbatim with the US buy-back slice landing beside this
        # one: one company's repurchases must read the same whichever market
        # disclosed them.
        from dalton_core.research_event import (
            DEFAULT_TIER_BY_KIND, EVENT_KINDS, PAYLOAD_FIELDS,
        )

        self.assertIn("buyback_disclosure", EVENT_KINDS)
        self.assertTrue({
            "accession", "filing_date", "market", "shares_purchased",
            "average_price_paid", "cumulative_shares", "cumulative_basis", "cluster_key",
        }.issubset(PAYLOAD_FIELDS["buyback_disclosure"]))
        self.assertNotIn("cumulative_shares_ytd", PAYLOAD_FIELDS["buyback_disclosure"])
        self.assertEqual(DEFAULT_TIER_BY_KIND["buyback_disclosure"], "primary_filing")

    def test_every_kind_still_has_a_payload_contract(self) -> None:
        from dalton_core.research_event import EVENT_KINDS, PAYLOAD_FIELDS

        self.assertEqual(set(PAYLOAD_FIELDS), set(EVENT_KINDS))

    def test_the_notes_hash_joined_the_two_kinds_a_di_notice_becomes(self) -> None:
        from dalton_core.research_event import PAYLOAD_FIELDS

        self.assertIn("notes_text_hash", PAYLOAD_FIELDS["insider_transaction"])
        self.assertIn("notes_text_hash", PAYLOAD_FIELDS["ownership_change"])
        # And nowhere else: a 13F holding has no such text.
        self.assertNotIn("notes_text_hash", PAYLOAD_FIELDS["holdings_change"])


class CapabilityMapTests(unittest.TestCase):
    def test_the_brain_can_be_told_where_hong_kong_disclosure_lives(self) -> None:
        from dalton_core.source_capability_map import capability, sources_for

        entry = capability("hkex-filings")
        self.assertEqual(entry["markets"], ("HK",))
        self.assertEqual(entry["evidence_tier"], "primary_filing")
        self.assertFalse(entry["generic"])
        self.assertIn("hkex-filings", sources_for("filing"))

    def test_the_map_still_builds_and_hashes(self) -> None:
        from dalton_core.source_capability_map import build_map

        self.assertEqual(len(build_map()["content_hash"]), 64)


class InvocationTests(unittest.TestCase):
    def test_the_same_read_twice_is_one_invocation(self) -> None:
        first = invocation_ref(
            operation=NEXT_DAY_DISCLOSURE_OPERATION, governance_ref="g",
            governance_hash="h", parameters={"hk_ticker": "00700"},
            artifact_hash="a" * 64,
        )
        second = invocation_ref(
            operation=NEXT_DAY_DISCLOSURE_OPERATION, governance_ref="g",
            governance_hash="h", parameters={"hk_ticker": "00700"},
            artifact_hash="a" * 64,
        )
        self.assertEqual(first, second)

    def test_a_different_answer_is_a_different_invocation(self) -> None:
        base = dict(operation=NEXT_DAY_DISCLOSURE_OPERATION, governance_ref="g",
                    governance_hash="h", parameters={"hk_ticker": "00700"})
        self.assertNotEqual(
            invocation_ref(**base, artifact_hash="a" * 64),
            invocation_ref(**base, artifact_hash="b" * 64),
        )

    def test_it_names_the_connector(self) -> None:
        self.assertTrue(
            invocation_ref(
                operation=ANNOUNCEMENTS_INDEX_OPERATION, governance_ref="g",
                governance_hash="h", parameters={}, artifact_hash="a" * 64,
            ).startswith("connector-invocation:hkex-filings:")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
