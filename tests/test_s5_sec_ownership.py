"""S5: the four SEC ownership operations -- contract, grade, parsing, child.

Everything here is offline.  The filings are synthetic and carry no real
person's name: the shapes are the public filing shapes, the people are
``SYNTHETIC TESTPERSON A`` and friends, and no test in this file reaches SEC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.connector_governance import (
    GOVERNANCE_KIND_REGISTRY,
    ConnectorGovernance,
    build_governance_record,
)
from dalton_core.connector_inventory import (
    build_connector_inventory,
    load_packaged_connector_inventory,
)
from dalton_core.connector_quota_policy import governed_daily_quota
from dalton_core.sec_ownership_adapter import (
    SecOwnershipParseError,
    compare_holdings,
    decide_value_unit,
    parse_beneficial_ownership,
    parse_form13f,
    parse_form144,
    parse_form4,
    quarter_of,
)
from dalton_core.sec_ownership_cli import run as run_child
from dalton_core.sec_ownership_core import (
    ALL_OWNERSHIP_FORMS,
    BENEFICIAL_OWNERSHIP_OPERATION,
    FORM13F_OPERATION,
    FORM144_OPERATION,
    FORM4_OPERATION,
    FORMS_BY_OPERATION,
    KIND_BY_OPERATION,
    OPERATIONS,
    OWNERSHIP_EVIDENCE_TIER,
    OWNERSHIP_GRADE,
    SecOwnershipError,
    invocation_ref,
    ownership_filings,
    ownership_identity,
    ownership_output_schema,
    ownership_schema_hash,
    primary_document_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec-ownership"
ACN = "company:sec-cik:0001467373"
ACN_CIK = "0001467373"
ACN_FORM4 = "0001467373-26-000045"
HOLDER = "0001900002"

# The two operations that existed before S5, pinned by the hashes the packaged
# template carried on main at ``ebd2ea8``. Adding four operations to a profile
# must not move an approval that is already live, and this is the assertion
# that says so rather than the intention.
FROZEN_OPERATION_HASHES = {
    "list_filings": (
        "8fbd0650bfcb845f8bddded017551834d39174e9c74f785d1538e657c359cde4",
        "d832b00d9df34a53a22d54470112af1d9a0646ed1057d747a75ace4aa2f2d979",
    ),
    "list_official_attachments": (
        "097e2b5cda735a0ffba6ba7a2eeb0064e9914c2688c21adde50b0384e1d9aa8a",
        "2d6209614d453c6f07b491b781140964200b82f501b1c2fa0f75bc241851560e",
    ),
    "get_official_attachment": (
        "d5930fbd0923de7045d18e55e1bcf3302c5eeecd27bbadbbd5c167dd03bfd4d4",
        "2d6209614d453c6f07b491b781140964200b82f501b1c2fa0f75bc241851560e",
    ),
    # The reason the new operations take ``filing_accession`` and not
    # ``accession``: these three already take an ``accession`` as free text,
    # and narrowing that field name would have moved all three of these hashes.
    "read_item": (
        "8c3c5af49c82e39cb70f54b0c2f5d3806416acf81880ee4c604982bb0e40e666",
        "2d6209614d453c6f07b491b781140964200b82f501b1c2fa0f75bc241851560e",
    ),
    "get_company_facts": (
        "9391711e60bd62794d66623d67d3788b4f22f0e90d4d3e38aa610eb7d00ac232",
        "7e1dbb47227a27d5326685e1f304e43b7f018dd6d5118257cc73e2d5a2ed147c",
    ),
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def digest(name: str) -> str:
    return hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = load_packaged_connector_inventory()["templates"]["sec"]
        self.operations = {
            item["operation"]: item for item in self.template["operations"]
        }

    def test_the_four_operations_are_on_the_sec_profile(self) -> None:
        for operation in OPERATIONS:
            self.assertIn(operation, self.operations, operation)

    def test_the_packaged_inventory_matches_the_definitions(self) -> None:
        # What ``scripts/build_connector_inventory.py --check`` asserts, as a
        # test, so a definition edited without a regeneration fails here first.
        built = build_connector_inventory()
        self.assertEqual(built["templates"]["sec"], self.template)
        self.assertIn("ir-page-watch", built["templates"])

    def test_the_output_contracts_before_s5_did_not_move(self) -> None:
        for name, (_input_hash, output_hash) in FROZEN_OPERATION_HASHES.items():
            self.assertEqual(
                self.operations[name]["output_schema_hash"], output_hash, name
            )

    def test_the_input_contracts_before_s5_did_not_move(self) -> None:
        # The reason ``filing_accession`` is not called ``accession``: three
        # frozen operations already take an ``accession`` as free text, and
        # narrowing that name would have moved approvals nobody asked to move.
        for name, (input_hash, _output_hash) in FROZEN_OPERATION_HASHES.items():
            self.assertEqual(
                self.operations[name]["input_schema_hash"], input_hash, name
            )

    def test_no_ownership_operation_can_be_handed_a_url(self) -> None:
        for operation in OPERATIONS:
            document = next(
                item["document"] for item in self.template["schema_documents"]
                if item["schema_ref"] == self.operations[operation]["input_schema_ref"]
            )
            self.assertNotIn("url", document["properties"], operation)
            self.assertEqual(
                document["properties"]["filing_accession"]["pattern"],
                "^[0-9]{10}-[0-9]{2}-[0-9]{6}$",
            )
        self.assertIn(
            "route:arbitrary-attachment-url",
            self.template["route_restrictions"]["forbidden_target_refs"],
        )

    def test_each_operation_has_its_own_schema_hash(self) -> None:
        hashes = {operation: ownership_schema_hash(operation) for operation in OPERATIONS}
        self.assertEqual(len(set(hashes.values())), len(OPERATIONS), hashes)

    def test_the_source_hash_is_the_one_the_filings_lane_already_uses(self) -> None:
        from dalton_core.research_plan_executor import sec_connector_identity

        facts = sec_connector_identity(self.template, "get_company_facts")
        self.assertEqual(
            ownership_identity(FORM4_OPERATION)["source_hash"], facts["source_hash"]
        )

    def test_adding_operations_did_not_widen_the_research_plan_identity(self) -> None:
        from dalton_core.research_plan_executor import sec_connector_identity

        # Both narrow identities still see exactly the two operations they
        # were signed against. ``list_filings`` narrows further to its own
        # (P10n), and neither notices that the profile now carries four more.
        facts = sec_connector_identity(self.template, "get_company_facts")
        self.assertEqual(
            facts["allowed_operations"], ["list_filings", "get_company_facts"]
        )
        index = sec_connector_identity(self.template, "list_filings")
        self.assertEqual(index["allowed_operations"], ["list_filings"])

    def test_every_operation_is_a_registered_governance_kind(self) -> None:
        for operation, kind in KIND_BY_OPERATION.items():
            self.assertIn(kind, GOVERNANCE_KIND_REGISTRY, operation)
            record = build_governance_record(kind, approved_by="human:lumos")
            self.assertEqual(record["status"], "proposed")
            self.assertEqual(
                record["expected_schema_hash"], ownership_schema_hash(operation)
            )

    def test_the_shipped_records_are_proposed_and_load(self) -> None:
        root = Path(__file__).resolve().parents[1] / "deploy" / "connector-governance"
        for kind in KIND_BY_OPERATION.values():
            record = ConnectorGovernance.load(root / f"{kind}-v1.json")
            self.assertEqual(record.wire["status"], "proposed")
            self.assertFalse(record.approved)

    def test_every_operation_has_a_governed_quota(self) -> None:
        for operation in OPERATIONS:
            quota = governed_daily_quota("sec", operation)
            self.assertEqual(quota["quota_unit"], "document")
            self.assertGreaterEqual(quota["daily_unit_limit"], 1)

    def test_a_document_url_is_derived_from_the_accession_alone(self) -> None:
        self.assertEqual(
            primary_document_url(ACN_CIK, ACN_FORM4),
            "https://www.sec.gov/Archives/edgar/data/1467373/"
            "000146737326000045/primary_doc.xml",
        )
        with self.assertRaises(SecOwnershipError):
            primary_document_url(ACN_CIK, "https://example.com/evil.xml")

    def test_the_invocation_name_moves_when_the_bytes_do(self) -> None:
        kwargs = {
            "operation": FORM4_OPERATION, "governance_ref": "g:1",
            "governance_hash": "a" * 64, "parameters": {"filing_accession": ACN_FORM4},
        }
        self.assertNotEqual(
            invocation_ref(artifact_hash="b" * 64, **kwargs),
            invocation_ref(artifact_hash="c" * 64, **kwargs),
        )


class GradeExclusionTests(unittest.TestCase):
    """These filings are primary, regulatory, and never a statement figure."""

    def test_the_grade_is_not_one_a_figure_may_be_read_under(self) -> None:
        from dalton_core.document_figure_grade import GRADE_BY_SPEC, GRADES

        self.assertNotIn(OWNERSHIP_GRADE, GRADES)
        self.assertNotIn(OWNERSHIP_GRADE, set(GRADE_BY_SPEC.values()))

    def test_a_verified_figure_may_not_carry_it(self) -> None:
        from dalton_core.research_verification import FIGURE_ADMISSIBLE_GRADES

        self.assertNotIn(OWNERSHIP_GRADE, FIGURE_ADMISSIBLE_GRADES)

    def test_no_ownership_form_can_produce_a_statement_line(self) -> None:
        from dalton_core.statement_snapshot import _FORMS

        self.assertEqual(ALL_OWNERSHIP_FORMS & _FORMS, frozenset())
        self.assertEqual(_FORMS, frozenset({"10-Q", "10-K"}))

    def test_the_operations_are_not_figure_worthy_document_kinds(self) -> None:
        from dalton_core.document_figure_grade import figure_worthy, grade_for

        for operation in OPERATIONS:
            self.assertIsNone(grade_for(operation), operation)
            self.assertFalse(figure_worthy(operation), operation)

    def test_the_tier_says_well_attested_and_the_grade_still_excludes(self) -> None:
        # The distinction the slice exists to draw: a Form 4 is as well
        # attested as a fact gets, and that is not a licence to read it as a
        # number about the business.
        from dalton_core.research_event import EVIDENCE_TIERS
        from dalton_core.research_verification import FIGURE_ADMISSIBLE_GRADES

        self.assertEqual(EVIDENCE_TIERS[0], OWNERSHIP_EVIDENCE_TIER)
        self.assertNotIn(OWNERSHIP_GRADE, FIGURE_ADMISSIBLE_GRADES)

    def test_nothing_in_the_forecast_path_imports_this_connector(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "dalton_core"
        for name in (
            "model_forecast.py", "forecast_reconciliation.py",
            "statement_snapshot.py", "company_model_inputs.py",
        ):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("sec_ownership", text, name)


class NumberTests(unittest.TestCase):
    def test_a_figure_is_the_text_the_filing_contained(self) -> None:
        wire = parse_form4(
            fixture("form4-sale.xml"), accession=ACN_FORM4,
            artifact_hash=digest("form4-sale.xml"),
        )
        exercised = next(
            row for row in wire["transactions"]
            if row["table"] == "non_derivative" and row["transaction_code"] == "M"
        )
        # Four decimal places, kept. A float would make this "1200.0" and
        # throw away what the plan actually reported.
        self.assertEqual(exercised["shares"], "1200.0000")

    def test_a_thousands_separator_is_typography_and_a_zero_pad_is_not_a_number(self) -> None:
        wire = parse_form4(
            fixture("form4-sale.xml"), accession=ACN_FORM4,
            artifact_hash=digest("form4-sale.xml"),
        )
        sale = next(row for row in wire["transactions"] if row["transaction_code"] == "S")
        self.assertEqual(sale["shares"], "4250")
        self.assertEqual(sale["shares_owned_following"], "18600")
        self.assertEqual(sale["price_per_share"], "318.4200")

    def test_a_figure_the_contract_cannot_describe_is_refused_not_zeroed(self) -> None:
        broken = fixture("form4-sale.xml").replace(
            "<value>4,250</value>", "<value>approximately 4,250</value>"
        )
        with self.assertRaises(SecOwnershipParseError) as caught:
            parse_form4(broken, accession=ACN_FORM4, artifact_hash="a" * 64)
        self.assertIn("approximately", str(caught.exception))

    def test_a_document_type_declaration_is_refused(self) -> None:
        evil = (
            '<?xml version="1.0"?>\n<!DOCTYPE lol [<!ENTITY a "b">]>\n'
            "<ownershipDocument><documentType>4</documentType></ownershipDocument>"
        )
        with self.assertRaises(SecOwnershipParseError) as caught:
            parse_form4(evil, accession=ACN_FORM4, artifact_hash="a" * 64)
        self.assertIn("DOCTYPE", str(caught.exception))


class Form4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.hash = digest("form4-sale.xml")
        self.wire = parse_form4(
            fixture("form4-sale.xml"), accession=ACN_FORM4,
            artifact_hash=self.hash, source_record_refs=["raw-sink:x"],
        )

    def test_the_wire_satisfies_the_frozen_contract(self) -> None:
        _schema_matches(self.wire, ownership_output_schema(FORM4_OPERATION), "output")

    def test_the_role_is_four_booleans_and_a_derived_word(self) -> None:
        owner = self.wire["reporting_owners"][0]
        self.assertTrue(owner["is_officer"])
        self.assertFalse(owner["is_director"])
        self.assertEqual(owner["officer_title"], "Chief Financial Officer")
        self.assertEqual(owner["role"], "officer:Chief Financial Officer")

    def test_both_tables_are_read_and_labelled(self) -> None:
        self.assertEqual(
            [row["table"] for row in self.wire["transactions"]],
            ["non_derivative", "non_derivative", "derivative"],
        )

    def test_a_footnoted_price_keeps_its_footnote_reference(self) -> None:
        sale = next(
            row for row in self.wire["transactions"] if row["transaction_code"] == "S"
        )
        self.assertEqual(sale["footnote_refs"], ["F1"])
        self.assertIn("weighted average", self.wire["footnotes"][0])

    def test_every_row_is_bound_to_the_accession_and_the_bytes(self) -> None:
        other = parse_form4(
            fixture("form4-sale.xml"), accession=ACN_FORM4, artifact_hash="0" * 64,
        )
        self.assertNotEqual(
            [row["record_hash"] for row in self.wire["transactions"]],
            [row["record_hash"] for row in other["transactions"]],
        )
        again = parse_form4(
            fixture("form4-sale.xml"), accession=ACN_FORM4, artifact_hash=self.hash,
        )
        self.assertEqual(
            [row["record_hash"] for row in self.wire["transactions"]],
            [row["record_hash"] for row in again["transactions"]],
        )

    def test_a_form_that_is_not_an_ownership_document_is_refused(self) -> None:
        with self.assertRaises(SecOwnershipParseError):
            parse_form4(
                "<informationTable/>", accession=ACN_FORM4, artifact_hash="a" * 64
            )


class BeneficialOwnershipTests(unittest.TestCase):
    def test_an_amended_13g_carries_its_amendment_number(self) -> None:
        wire = parse_beneficial_ownership(
            fixture("sc13g-amendment.xml"), accession="0001900002-26-000012",
            artifact_hash=digest("sc13g-amendment.xml"), form_type="SC 13G/A",
            source_record_refs=["raw-sink:y"],
        )
        _schema_matches(
            wire, ownership_output_schema(BENEFICIAL_OWNERSHIP_OPERATION), "output"
        )
        self.assertEqual(wire["form_type"], "SC 13G")
        self.assertTrue(wire["is_amendment"])
        # "007" is a padded field, not amendment seven hundred and seven.
        self.assertEqual(wire["amendment_no"], "7")
        self.assertEqual(wire["cusip"], "G1151C101")
        self.assertEqual(wire["event_date"], "2026-06-30")

    def test_two_filers_are_two_rows_with_their_own_powers(self) -> None:
        wire = parse_beneficial_ownership(
            fixture("sc13g-amendment.xml"), accession="0001900002-26-000012",
            artifact_hash=digest("sc13g-amendment.xml"), form_type="SC 13G/A",
        )
        people = wire["reporting_persons"]
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0]["sole_voting_power"], "38120455")
        self.assertEqual(people[1]["sole_voting_power"], "0")
        self.assertEqual(people[1]["shared_voting_power"], "38120455")
        self.assertEqual({person["percent_of_class"] for person in people}, {"6.7"})

    def test_the_flat_older_shape_still_reads(self) -> None:
        wire = parse_beneficial_ownership(
            fixture("sc13d-flat.xml"), accession="0001900004-26-000003",
            artifact_hash=digest("sc13d-flat.xml"), form_type="SC 13D",
        )
        _schema_matches(
            wire, ownership_output_schema(BENEFICIAL_OWNERSHIP_OPERATION), "output"
        )
        self.assertEqual(wire["form_type"], "SC 13D")
        self.assertFalse(wire["is_amendment"])
        # An amendment number the filing did not carry is absent, not zero.
        self.assertIsNone(wire["amendment_no"])
        self.assertEqual(wire["reporting_persons"][0]["aggregate_shares"], "9600000")

    def test_the_purpose_is_hashed_rather_than_copied(self) -> None:
        wire = parse_beneficial_ownership(
            fixture("sc13d-flat.xml"), accession="0001900004-26-000003",
            artifact_hash=digest("sc13d-flat.xml"), form_type="SC 13D",
        )
        self.assertEqual(len(wire["purpose_text_hash"]), 64)
        self.assertGreater(wire["purpose_text_chars"], 100)
        self.assertNotIn("undervalued", json.dumps(wire))

    def test_a_purpose_that_changed_gives_a_different_hash(self) -> None:
        base = dict(
            accession="0001900004-26-000003",
            artifact_hash=digest("sc13d-flat.xml"), form_type="SC 13D",
        )
        one = parse_beneficial_ownership(
            fixture("sc13d-flat.xml"), purpose_text="we intend to be passive", **base
        )
        two = parse_beneficial_ownership(
            fixture("sc13d-flat.xml"),
            purpose_text="we intend to seek board representation", **base
        )
        self.assertNotEqual(one["purpose_text_hash"], two["purpose_text_hash"])

    def test_a_form_type_that_is_not_a_13d_or_13g_is_refused(self) -> None:
        with self.assertRaises(SecOwnershipParseError):
            parse_beneficial_ownership(
                fixture("sc13d-flat.xml"), accession="0001900004-26-000003",
                artifact_hash="a" * 64, form_type="SC 13E3",
            )


class Form144Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = parse_form144(
            fixture("form144.xml"), accession="0001058290-26-000077",
            artifact_hash=digest("form144.xml"), source_record_refs=["raw-sink:a"],
        )

    def test_the_wire_satisfies_the_frozen_contract(self) -> None:
        _schema_matches(self.wire, ownership_output_schema(FORM144_OPERATION), "output")

    def test_seller_shares_value_and_planned_date(self) -> None:
        notice = self.wire["notices"][0]
        self.assertEqual(notice["seller_name"], "SYNTHETIC TESTPERSON B")
        self.assertEqual(notice["relationship_to_issuer"], "Officer")
        self.assertEqual(notice["shares_to_be_sold"], "25000")
        self.assertEqual(notice["aggregate_market_value"], "2145000.00")
        self.assertEqual(notice["approx_sale_date"], "2026-09-05")

    def test_prior_sales_in_the_window_are_reported(self) -> None:
        self.assertTrue(self.wire["securities_sold_past_3_months"])

    def test_a_144_with_no_named_seller_is_refused(self) -> None:
        with self.assertRaises(SecOwnershipParseError):
            parse_form144(
                "<edgarSubmission><formData/></edgarSubmission>",
                accession="0001058290-26-000077", artifact_hash="a" * 64,
            )


class Form13FTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = parse_form13f(
            fixture("form13f-table-2026q2.xml"), accession="0001900002-26-000090",
            artifact_hash=digest("form13f-table-2026q2.xml"), holder_cik=HOLDER,
            primary_text=fixture("form13f-cover-2026q2.xml"),
            source_record_refs=["raw-sink:b"],
        )
        self.prior = parse_form13f(
            fixture("form13f-table-2026q1.xml"), accession="0001900002-26-000030",
            artifact_hash=digest("form13f-table-2026q1.xml"), holder_cik=HOLDER,
            primary_text=fixture("form13f-cover-2026q1.xml"),
        )

    def test_the_wire_satisfies_the_frozen_contract(self) -> None:
        _schema_matches(
            self.current, ownership_output_schema(FORM13F_OPERATION), "output"
        )

    def test_holder_quarter_cusip_shares_and_value(self) -> None:
        self.assertEqual(self.current["holder_name"], "SYNTHETIC ASSET MANAGEMENT LLC")
        self.assertEqual(self.current["holder_cik"], HOLDER)
        self.assertEqual(self.current["quarter"], "2026Q2")
        acn = next(
            row for row in self.current["holdings"] if row["cusip"] == "G1151C101"
        )
        self.assertEqual(acn["shares_or_principal_amount"], "3000000")
        self.assertEqual(acn["value_usd"], "955000000")
        self.assertEqual(acn["shares_or_principal_type"], "SH")

    def test_a_period_after_the_2023_rule_is_whole_dollars(self) -> None:
        self.assertEqual(self.current["value_unit"], "usd")
        self.assertEqual(self.current["value_unit_basis"], "post_2023_rule")

    def test_a_period_before_it_is_thousands_and_is_scaled(self) -> None:
        old = parse_form13f(
            fixture("form13f-table-2022q4.xml"), accession="0001900002-23-000002",
            artifact_hash=digest("form13f-table-2022q4.xml"), holder_cik=HOLDER,
            primary_text=fixture("form13f-cover-2022q4.xml"),
        )
        self.assertEqual(old["quarter"], "2022Q4")
        self.assertEqual(old["value_unit"], "thousands")
        self.assertEqual(old["value_unit_basis"], "pre_2023_rule")
        row = old["holdings"][0]
        # Both. The digits as filed, and the dollars they mean.
        self.assertEqual(row["value_as_filed"], "720000")
        self.assertEqual(row["value_usd"], "720000000")

    def test_the_ratio_overrules_the_rule_and_says_that_it_did(self) -> None:
        # A filer who kept reporting thousands after the rule changed. The
        # period says dollars; a value-to-share ratio of 0.27 says otherwise,
        # and being wrong by a factor of a thousand is the worst thing this
        # parser could do.
        holdings = [
            {"shares_or_principal_amount": "2700000", "value_as_filed": "720000"},
        ]
        self.assertEqual(
            decide_value_unit(holdings, "2026-06-30"),
            ("thousands", "ratio_heuristic"),
        )

    def test_a_book_with_no_shares_falls_back_to_the_period_rule(self) -> None:
        self.assertEqual(decide_value_unit([], "2026-06-30"), ("usd", "post_2023_rule"))
        self.assertEqual(
            decide_value_unit([], "2022-12-31"), ("thousands", "pre_2023_rule")
        )

    def test_a_quarter_is_derived_from_the_period_end(self) -> None:
        self.assertEqual(quarter_of("2026-06-30"), "2026Q2")
        self.assertEqual(quarter_of("2026-01-31"), "2026Q1")
        self.assertIsNone(quarter_of(None))

    def test_a_put_and_the_name_itself_are_different_positions(self) -> None:
        epam = [row for row in self.current["holdings"] if row["cusip"] == "29414B104"]
        self.assertEqual(len(epam), 1)
        self.assertEqual(epam[0]["put_call"], "Put")

    def test_change_against_the_prior_quarter(self) -> None:
        found = compare_holdings(self.current, self.prior)
        self.assertEqual(found["status"], "compared")
        self.assertEqual(found["prior_quarter"], "2026Q1")
        actions = {
            (row["cusip"], row["put_call"]): (row["action"], row["share_change"])
            for row in found["changes"]
        }
        self.assertEqual(actions[("G1151C101", None)], ("add", "500000"))
        self.assertEqual(actions[("192446102", None)], ("unchanged", "0"))
        self.assertEqual(actions[("29414B104", "Put")], ("new", "900000"))
        self.assertEqual(actions[("459200101", None)], ("exit", "-700000"))

    def test_an_absent_prior_quarter_is_named_and_not_read_as_a_new_book(self) -> None:
        found = compare_holdings(self.current, None)
        self.assertEqual(found["status"], "prior_absent")
        self.assertEqual(found["changes"], [])
        self.assertEqual(found["first_reading_count"], 3)
        self.assertIn("first reading", found["reason"])

    def test_a_cusip_that_is_not_one_is_refused(self) -> None:
        broken = fixture("form13f-table-2026q2.xml").replace(
            "<cusip>G1151C101</cusip>", "<cusip>NOPE</cusip>"
        )
        with self.assertRaises(SecOwnershipParseError):
            parse_form13f(
                broken, accession="0001900002-26-000090", artifact_hash="a" * 64,
                holder_cik=HOLDER,
            )


class IndexReaderTests(unittest.TestCase):
    """Choosing what to read is a read of local bytes, with no SEC call in it."""

    PAYLOAD = {
        "filings": {"recent": {
            "accessionNumber": [
                "0001467373-26-000045", "0001467373-26-000044",
                "0001900002-26-000012", "0001467373-26-000040",
            ],
            "form": ["4", "10-Q", "SC 13G/A", "144"],
            "filingDate": ["2026-08-14", "2026-07-01", "2026-02-10", "2026-09-01"],
            "reportDate": ["2026-08-12", "2026-05-31", "", ""],
            "primaryDocument": ["doc4.xml", "acn.htm", "primary_doc.xml", "p.xml"],
        }},
    }

    def test_only_ownership_forms_are_chosen_and_each_names_its_operation(self) -> None:
        found = ownership_filings(self.PAYLOAD, since="2026-01-01")
        self.assertEqual(
            [(row["form"], row["operation"]) for row in found],
            [
                ("144", FORM144_OPERATION),
                ("4", FORM4_OPERATION),
                ("SC 13G/A", BENEFICIAL_OWNERSHIP_OPERATION),
            ],
        )

    def test_the_window_excludes_what_is_older_than_it(self) -> None:
        found = ownership_filings(self.PAYLOAD, since="2026-08-01")
        self.assertEqual({row["accession"] for row in found}, {
            "0001467373-26-000045", "0001467373-26-000040",
        })

    def test_one_operation_can_be_asked_for_alone(self) -> None:
        found = ownership_filings(
            self.PAYLOAD, forms=FORMS_BY_OPERATION[FORM4_OPERATION], since="2026-01-01"
        )
        self.assertEqual([row["form"] for row in found], ["4"])

    def test_an_index_this_reader_cannot_describe_is_refused(self) -> None:
        with self.assertRaises(SecOwnershipError):
            ownership_filings({"filings": {}})


class ChildTests(unittest.TestCase):
    """Approval first, artifact always, contract last -- offline throughout."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.governance: dict[str, Path] = {}
        for operation, kind in KIND_BY_OPERATION.items():
            record = build_governance_record(
                kind, approved_by="human:test-owner", status="approved"
            )
            path = self.state / f"{kind}.json"
            path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            self.governance[operation] = path

    def args(self, operation: str, **overrides: object) -> argparse.Namespace:
        values = {
            "state_dir": str(self.state),
            "governance": str(self.governance[operation]),
            "operation": operation,
            "company_ref": ACN,
            "accession": ACN_FORM4,
            "form_type": "4",
            "issuer": ACN_CIK,
            "holder_cik": None,
            "quarter": None,
            "filed_at": "2026-08-14",
            "company_cusips": None,
            "cover_file": None,
            "prior_file": None,
            "prior_cover_file": None,
            "prior_accession": None,
            "summary_dir": str(self.state / operation),
            "actor_ref": "core:test",
            "user_agent": "test",
            "allow_network": False,
            "fixture_file": str(FIXTURES / "form4-sale.xml"),
            "quiet": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_a_form4_run_produces_one_event_per_transaction(self) -> None:
        summary = run_child(self.args(FORM4_OPERATION))
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["event_count"], 3)
        kinds = {event["kind"] for event in summary["events"]}
        self.assertEqual(kinds, {"insider_transaction"})
        payload = summary["events"][0]["payload"]
        self.assertEqual(payload["owner_name"], "SYNTHETIC TESTPERSON A")
        self.assertEqual(payload["transaction_code"], "S")
        self.assertEqual(payload["transaction_meaning"], "open-market sale")
        self.assertEqual(payload["accession"], ACN_FORM4)
        self.assertEqual(payload["artifact_hash"], summary["artifact"]["content_hash"])

    def test_the_bytes_are_spooled_before_anything_is_read_out_of_them(self) -> None:
        summary = run_child(self.args(FORM4_OPERATION))
        self.assertEqual(
            summary["artifact"]["content_hash"], digest("form4-sale.xml")
        )

    def test_a_parse_failure_still_leaves_the_artifact(self) -> None:
        broken = self.state / "broken.xml"
        broken.write_text("<informationTable/>", encoding="utf-8")
        summary = run_child(
            self.args(FORM4_OPERATION, fixture_file=str(broken),
                      summary_dir=str(self.state / "broken"))
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIsNotNone(summary["artifact"])
        self.assertIn("ownershipDocument", summary["failure_reason"])

    def test_an_unapproved_record_is_refused_before_the_spool(self) -> None:
        path = self.state / "proposed.json"
        path.write_text(
            json.dumps(
                build_governance_record(
                    KIND_BY_OPERATION[FORM4_OPERATION], approved_by="human:test-owner"
                ),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        summary = run_child(
            self.args(FORM4_OPERATION, governance=str(path),
                      summary_dir=str(self.state / "unapproved"))
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_one_operations_approval_cannot_be_spent_on_another(self) -> None:
        summary = run_child(
            self.args(
                FORM4_OPERATION,
                governance=str(self.governance[FORM13F_OPERATION]),
                summary_dir=str(self.state / "crossed"),
            )
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])

    def test_a_form_the_operation_does_not_read_is_refused(self) -> None:
        summary = run_child(
            self.args(FORM4_OPERATION, form_type="10-K",
                      summary_dir=str(self.state / "wrongform"))
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("is not read by", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_neither_mode_and_both_modes_are_both_refused(self) -> None:
        for overrides in (
            {"fixture_file": None, "allow_network": False},
            {"allow_network": True},
        ):
            summary = run_child(
                self.args(FORM4_OPERATION, summary_dir=str(self.state / "mode"),
                          **overrides)
            )
            self.assertEqual(summary["status"], "failed")
            self.assertIn("exactly one of", summary["failure_reason"])

    def test_a_144_event_says_the_sale_is_planned(self) -> None:
        summary = run_child(
            self.args(
                FORM144_OPERATION, accession="0001058290-26-000077",
                form_type="144", issuer="0001058290",
                fixture_file=str(FIXTURES / "form144.xml"),
                summary_dir=str(self.state / "f144"),
            )
        )
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        payload = summary["events"][0]["payload"]
        self.assertEqual(payload["form"], "144")
        self.assertIn("planned sale", payload["person_type"])
        self.assertEqual(payload["aggregate_shares"], "25000")

    def test_a_13g_amendment_run_carries_the_amendment_number(self) -> None:
        summary = run_child(
            self.args(
                BENEFICIAL_OWNERSHIP_OPERATION, accession="0001900002-26-000012",
                form_type="SC 13G/A",
                fixture_file=str(FIXTURES / "sc13g-amendment.xml"),
                summary_dir=str(self.state / "13g"),
            )
        )
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["event_count"], 2)
        payload = summary["events"][0]["payload"]
        self.assertEqual(payload["form"], "SC 13G/A")
        self.assertEqual(payload["amendment_no"], "7")
        self.assertEqual(payload["percent_of_class"], "6.7")

    def thirteen_f(self, **overrides: object) -> argparse.Namespace:
        values = {
            "accession": "0001900002-26-000090",
            "form_type": "13F-HR",
            "issuer": None,
            "holder_cik": HOLDER,
            "quarter": "2026Q2",
            "company_cusips": "G1151C101,192446102",
            "cover_file": str(FIXTURES / "form13f-cover-2026q2.xml"),
            "fixture_file": str(FIXTURES / "form13f-table-2026q2.xml"),
            "summary_dir": str(self.state / "13f"),
        }
        values.update(overrides)
        return self.args(FORM13F_OPERATION, **values)

    def test_a_13f_run_emits_only_the_covered_names_that_moved(self) -> None:
        summary = run_child(self.thirteen_f(
            prior_file=str(FIXTURES / "form13f-table-2026q1.xml"),
            prior_cover_file=str(FIXTURES / "form13f-cover-2026q1.xml"),
            prior_accession="0001900002-26-000030",
        ))
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["comparison_status"], "compared")
        self.assertEqual(summary["prior_quarter"], "2026Q1")
        # CTSH is unchanged and IBM is not covered here; only ACN's add is an
        # event.
        self.assertEqual(summary["event_count"], 1)
        payload = summary["events"][0]["payload"]
        self.assertEqual(payload["cusip"], "G1151C101")
        self.assertEqual(payload["action"], "add")
        self.assertEqual(payload["share_change"], "500000")
        self.assertEqual(payload["value_unit_basis"], "post_2023_rule")

    def test_a_13f_with_no_prior_quarter_says_first_reading(self) -> None:
        summary = run_child(self.thirteen_f(summary_dir=str(self.state / "13f-first")))
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["comparison_status"], "prior_absent")
        self.assertIsNone(summary["prior_quarter"])
        actions = {event["payload"]["action"] for event in summary["events"]}
        self.assertEqual(actions, {"first_reading"})
        self.assertEqual(summary["event_count"], 2)



class SpoolReaderTests(unittest.TestCase):
    """The whole queue comes from bytes already on disk, with no SEC call."""

    SUBMISSIONS = {
        "cik": "1467373",
        "filings": {"recent": {
            "accessionNumber": [
                "0001467373-26-000045", "0001467373-26-000044",
                "0001467373-26-000041",
            ],
            "form": ["4", "10-Q", "144"],
            "filingDate": ["2026-08-14", "2026-07-01", "2026-09-01"],
            "reportDate": ["2026-08-12", "2026-05-31", ""],
            "primaryDocument": ["doc4.xml", "acn.htm", "p.xml"],
        }},
    }

    def setUp(self) -> None:
        from dalton_core.store import DaltonStore

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        # The three columns the reader joins over. C1's own test builds the
        # same three for the same reason: what is under test is the join and
        # the spool read, not the connector plane.
        self.store.connection.executescript(
            "CREATE TABLE connector_call_specs("
            "call_spec_id TEXT PRIMARY KEY, operation TEXT, record_json TEXT);"
            "CREATE TABLE connector_invocations("
            "connector_invocation_id TEXT PRIMARY KEY, call_spec_ref TEXT);"
            "CREATE TABLE connector_source_envelopes("
            "source_envelope_id TEXT PRIMARY KEY, connector_invocation_ref TEXT,"
            "raw_response_hash TEXT, status TEXT);"
        )

    def seed(self, issuer: str = "0001467373") -> str:
        body = json.dumps(self.SUBMISSIONS)
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        objects = self.state / "connector-spool" / "connector-spool" / "objects"
        (objects / sha[:2]).mkdir(parents=True, exist_ok=True)
        (objects / sha[:2] / sha).write_text(body, encoding="utf-8")
        connection = self.store.connection
        connection.execute(
            "INSERT INTO connector_call_specs VALUES(?,?,?)",
            ("spec:1", "list_filings",
             json.dumps({"parameters": {"issuer": issuer, "form": "10-K"}})),
        )
        connection.execute(
            "INSERT INTO connector_invocations VALUES(?,?)", ("inv:1", "spec:1")
        )
        connection.execute(
            "INSERT INTO connector_source_envelopes VALUES(?,?,?,?)",
            ("envelope:1", "inv:1", sha, "complete"),
        )
        return sha

    def test_the_ownership_filings_come_out_of_the_spooled_index(self) -> None:
        from dalton_core.sec_ownership_core import ownership_filings_for_issuer

        sha = self.seed()
        found = ownership_filings_for_issuer(
            self.store.connection, self.state, issuer="0001467373",
            today="2026-09-09", lookback_days=400,
        )
        self.assertEqual(found["status"], "read")
        self.assertEqual(found["artifact_hash"], sha)
        self.assertEqual(
            [(row["form"], row["operation"]) for row in found["filings"]],
            [("144", FORM144_OPERATION), ("4", FORM4_OPERATION)],
        )

    def test_a_company_whose_index_has_not_been_read_says_so(self) -> None:
        from dalton_core.sec_ownership_core import ownership_filings_for_issuer

        found = ownership_filings_for_issuer(
            self.store.connection, self.state, issuer="0000051143",
            today="2026-09-09",
        )
        self.assertEqual(found["status"], "unavailable")
        self.assertEqual(found["filings"], [])
        self.assertIn("discovery lane has not run", found["reason"])

    def test_bytes_that_do_not_hash_to_their_ref_are_refused(self) -> None:
        from dalton_core.sec_ownership_core import ownership_filings_for_issuer

        sha = self.seed()
        objects = self.state / "connector-spool" / "connector-spool" / "objects"
        (objects / sha[:2] / sha).write_text("{}", encoding="utf-8")
        found = ownership_filings_for_issuer(
            self.store.connection, self.state, issuer="0001467373",
            today="2026-09-09",
        )
        self.assertEqual(found["status"], "tampered")
        self.assertEqual(found["filings"], [])


if __name__ == "__main__":
    unittest.main()
