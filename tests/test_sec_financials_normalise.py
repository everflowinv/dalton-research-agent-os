"""P13ag: statement structure and filed facts, joined without inferring anything.

Two views of one filing hold half of a line each. The statement view knows
which concepts are lines, their level and what they roll into, and carries no
unit. The fact view knows the value as filed, its unit and its period, and
carries no hierarchy. The statement defines the lines; the facts fill them.

The cases here are the ones that were wrong when this was first written against
the real EPAM 10-Q: balance-sheet lines vanished entirely because an instant
carries no ``period_end``, and a quarter was indistinguishable from a year to
date because both end on the same day.
"""

from __future__ import annotations

import unittest

from dalton_core.sec_financials_normalise import (
    build_wire,
    canonical_ref,
    fact_period,
    fact_dimension_count,
    normalise_filing,
    normalise_statement,
)


def structure(**overrides):
    base = {
        "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        "label": "Revenues", "level": 1, "abstract": False,
        "parent_concept": "us-gaap_OperatingIncomeLoss", "is_breakdown": False,
        "dimension_axis": None, "dimension_member": None, "balance": "credit",
    }
    base.update(overrides)
    return base


def fact(**overrides):
    base = {
        "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "label": "Revenues", "dimension": None, "member": None,
        "period_start": "2026-04-01", "period_end": "2026-06-30",
        "period_key": "duration_2026-04-01_2026-06-30",
        "unit_ref": "usd", "value": "1414767000", "balance": "credit",
    }
    base.update(overrides)
    return base


class CanonicalRefTests(unittest.TestCase):
    def test_the_two_views_spell_one_thing_two_ways(self):
        # Statement: us-gaap_Revenues / srt_AmericasMember.
        # Facts:     us-gaap:Revenues / srt:AmericasMember.
        self.assertEqual(canonical_ref("us-gaap_Revenues"), "us-gaap:Revenues")
        self.assertEqual(canonical_ref("srt_AmericasMember"), "srt:AmericasMember")
        self.assertEqual(canonical_ref("us-gaap:Revenues"), "us-gaap:Revenues")

    def test_only_the_prefix_separator_moves(self):
        # A local name may contain an underscore; only the first one is the
        # prefix separator, and only when there is no colon already.
        self.assertEqual(canonical_ref("epam_Some_Member"), "epam:Some_Member")
        self.assertEqual(canonical_ref("srt:Already_Colon"), "srt:Already_Colon")

    def test_absent_is_absent(self):
        self.assertIsNone(canonical_ref(None))
        self.assertIsNone(canonical_ref("   "))
        self.assertIsNone(canonical_ref(float("nan")))


class FactPeriodTests(unittest.TestCase):
    def test_a_balance_sheet_instant_carries_its_date_in_the_key(self):
        # This is why the balance sheet was missing: period_end is empty and
        # the date lives in period_key. Reading only period_end dropped every
        # line of the statement and said nothing.
        self.assertEqual(fact_period({"period_key": "instant_2026-06-30"}),
                         (None, "2026-06-30"))

    def test_a_duration_key_gives_both_ends(self):
        self.assertEqual(fact_period({"period_key": "duration_2026-04-01_2026-06-30"}),
                         ("2026-04-01", "2026-06-30"))

    def test_explicit_columns_win_when_present(self):
        self.assertEqual(
            fact_period({"period_start": "2026-01-01", "period_end": "2026-06-30",
                         "period_key": "duration_2026-04-01_2026-06-30"}),
            ("2026-01-01", "2026-06-30"))

    def test_a_period_nobody_stated_is_not_invented(self):
        self.assertEqual(fact_period({"period_key": "nonsense"}), (None, None))
        self.assertEqual(fact_period({}), (None, None))


class DimensionEvidenceTests(unittest.TestCase):
    def test_complete_parser_columns_count_every_nonempty_axis(self):
        row = fact(
            dimension="srt:StatementGeographicalAxis", member="srt:USMember",
            **{"dim_srt_StatementGeographicalAxis": "srt:USMember",
               "dim_us-gaap_ConsolidationItemsAxis": "us-gaap:ParentCompanyMember"},
        )
        self.assertEqual(fact_dimension_count(row), 2)

    def test_projected_axis_alone_is_not_complete_context_proof(self):
        self.assertIsNone(fact_dimension_count(fact(
            dimension="srt:StatementGeographicalAxis", member="srt:USMember")))


class NormaliseTests(unittest.TestCase):
    def lines(self, **kwargs):
        return normalise_statement(statement="income", **kwargs)

    def test_a_line_takes_structure_from_one_view_and_the_figure_from_the_other(self):
        result = self.lines(structure=[structure()], facts=[fact()])
        [line] = result["lines"]
        self.assertEqual(line["level"], 1)
        self.assertEqual(line["parent_concept"], "us-gaap:OperatingIncomeLoss")
        self.assertEqual(line["value"], "1414767000")
        self.assertEqual(line["unit"], "usd")
        self.assertEqual((line["period_start"], line["period_end"]),
                         ("2026-04-01", "2026-06-30"))

    def test_a_quarter_and_a_year_to_date_are_two_lines(self):
        # Both end 2026-06-30. Collapsing them would report a half-year as a
        # quarter, which is the failure this field exists to prevent.
        result = self.lines(structure=[structure()], facts=[
            fact(),
            fact(period_start="2026-01-01", period_key="duration_2026-01-01_2026-06-30",
                 value="2814828000"),
        ])
        self.assertEqual(len(result["lines"]), 2)
        self.assertEqual({line["period_start"] for line in result["lines"]},
                         {"2026-04-01", "2026-01-01"})

    def test_the_same_filed_figure_presented_twice_is_one_line(self):
        result = self.lines(structure=[structure()], facts=[fact(), fact()])
        self.assertEqual(len(result["lines"]), 1)
        self.assertEqual(result["dropped"]["duplicate presentation of one filed figure"], 1)

    def test_a_figure_with_no_stated_unit_is_dropped_not_assumed(self):
        result = self.lines(structure=[structure()], facts=[fact(unit_ref=None)])
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["dropped"]["fact has no unit"], 1)

    def test_a_value_that_is_not_a_number_is_dropped_not_coerced(self):
        for bad in ("1,414,767", "n/a", "(243911)", ""):
            with self.subTest(value=bad):
                result = self.lines(structure=[structure()], facts=[fact(value=bad)])
                self.assertEqual(result["lines"], [])

    def test_a_negative_figure_survives(self):
        result = self.lines(structure=[structure()], facts=[fact(value="-243911000")])
        self.assertEqual(result["lines"][0]["value"], "-243911000")

    def test_a_header_row_is_not_a_line(self):
        # "Operating expenses:" has no figure of its own.
        result = self.lines(
            structure=[structure(abstract=True, label="Operating expenses:")],
            facts=[fact()])
        self.assertEqual(result["lines"], [])

    def test_a_fact_the_statement_does_not_present_is_not_a_line(self):
        # Emitting it would mean inventing a level and a parent for it.
        result = self.lines(structure=[], facts=[fact()])
        self.assertEqual(result["lines"], [])

    def test_a_statement_line_with_no_fact_is_reported(self):
        result = self.lines(structure=[structure()], facts=[])
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["dropped"]["statement line has no fact in this filing"], 1)

    def test_a_breakdown_joins_on_its_axis_and_member(self):
        result = self.lines(
            structure=[structure(is_breakdown=True, level=2, label="Americas",
                                 dimension_axis="srt:StatementGeographicalAxis",
                                 dimension_member="srt_AmericasMember")],
            facts=[fact(dimension="srt:StatementGeographicalAxis",
                        member="srt:AmericasMember", value="1498358000")],
        )
        [line] = result["lines"]
        self.assertTrue(line["is_breakdown"])
        self.assertEqual(line["dimension_member"], "srt:AmericasMember")
        self.assertIsNone(line["dimension_count"])
        self.assertEqual(line["value"], "1498358000")

    def test_single_complete_dimension_reaches_the_line(self):
        result = self.lines(
            structure=[structure(is_breakdown=True,
                                 dimension_axis="srt:StatementGeographicalAxis",
                                 dimension_member="srt:AmericasMember")],
            facts=[fact(dimension="srt:StatementGeographicalAxis",
                        member="srt:AmericasMember",
                        **{"dim_srt_StatementGeographicalAxis": "srt:AmericasMember"})],
        )
        self.assertEqual(result["lines"][0]["dimension_count"], 1)

    def test_a_breakdown_does_not_take_the_consolidated_figure(self):
        # The undimensioned fact belongs to the undimensioned line.
        result = self.lines(
            structure=[structure(is_breakdown=True, level=2,
                                 dimension_axis="srt:StatementGeographicalAxis",
                                 dimension_member="srt_AmericasMember")],
            facts=[fact()],
        )
        self.assertEqual(result["lines"], [])

    def test_an_unknown_statement_is_refused(self):
        with self.assertRaises(ValueError):
            normalise_statement(statement="equity", structure=[], facts=[])


class WireTests(unittest.TestCase):
    def test_the_wire_carries_the_filing_and_not_the_drops(self):
        # What could not be used belongs in the run summary, where someone
        # asking "why is this line missing" will look -- not in the observation.
        filing = normalise_filing(
            accession="0001352010-26-000046", form="10-Q", filed="2026-08-06",
            report_date="2026-06-30",
            statements={"income": {"structure": [structure()], "facts": [fact()]}},
        )
        wire = build_wire(cik="1352010", entity_name="EPAM SYSTEMS, INC.",
                          filings=[filing], source_record_refs=["raw-sink:" + "a" * 64])
        self.assertEqual(wire["cik"], "0001352010")
        self.assertEqual(wire["filings"][0]["accession"], "0001352010-26-000046")
        self.assertNotIn("dropped", wire["filings"][0])
        self.assertEqual(len(wire["filings"][0]["lines"]), 1)

    def test_the_wire_matches_the_frozen_output_contract(self):
        from dalton_core.authority_resolver import _schema_matches
        from dalton_core.connector_inventory import load_packaged_connector_inventory
        from dalton_core.sec_financials_core import OPERATION

        template = load_packaged_connector_inventory()["templates"]["sec-financials"]
        ref = f"schema:connector-inventory:sec-financials:{OPERATION}:output:0.1"
        schema = next(d["document"] for d in template["schema_documents"]
                      if d["schema_ref"] == ref)
        filing = normalise_filing(
            accession="0001352010-26-000046", form="10-Q", filed="2026-08-06",
            report_date="2026-06-30",
            statements={
                "income": {"structure": [structure()], "facts": [fact()]},
                "balance": {
                    "structure": [structure(concept="us-gaap_AssetsCurrent",
                                            label="Total current assets",
                                            parent_concept=None, balance="debit")],
                    "facts": [fact(concept="us-gaap:AssetsCurrent",
                                   period_start=None, period_end=None,
                                   period_key="instant_2026-06-30",
                                   value="2215989000")],
                },
            },
        )
        wire = build_wire(cik="1352010", entity_name="EPAM SYSTEMS, INC.",
                          filings=[filing], source_record_refs=[])
        _schema_matches(wire, schema, "output")
        statements = {line["statement"] for line in wire["filings"][0]["lines"]}
        self.assertEqual(statements, {"income", "balance"})


if __name__ == "__main__":
    unittest.main()
