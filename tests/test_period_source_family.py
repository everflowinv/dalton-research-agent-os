import copy
import unittest

from dalton_core.period_source_family import (
    PeriodSourceFamilyError, SOURCE_FIELDS, catalog, preflight,
    driver_bindings, verify_driver_bindings, identity_for_row, validate_sum_bindings,
    needs_period_choices,
)
from dalton_core.store import content_hash

AXIS = "us-gaap:StatementBusinessSegmentsAxis"
FIXED = {"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}


def fixture(company="company:ticker:one"):
    filings, rows = [], []
    for index, (end, fixed) in enumerate((("2024-03-31", {}), ("2024-06-30", FIXED))):
        ingest = company + ":ingest:" + str(index)
        filings.append({"ingest_id": ingest, "company_ref": company,
                        "accession": company + ":accession:" + str(index), "content_hash": "a" * 64})
        facts = [(100, {}), (60, {**fixed, AXIS: "issuer:FirstMember"}),
                 (40, {**fixed, AXIS: "issuer:SecondMember"})]
        if fixed:
            facts.append((100, fixed))
        for ordinal, (value, dims) in enumerate(facts):
            rows.append({"line_id": ingest + ":" + str(ordinal), "ingest_id": ingest,
                         "statement_context_ref": ingest + ":context", "statement": "income",
                         "concept": "us-gaap:Revenues", "period_start": "2024-01-01",
                         "period_end": end, "unit": "USD", "value": str(value),
                         "dimension_count": len(dims), "dimensions": dims})
    return filings, rows


def requirements(groups):
    return [{**{k: g[k] for k in SOURCE_FIELDS}, "axis": g["axis"], "members": g["members"]}
            for g in groups]


class PeriodSourceFamilyTests(unittest.TestCase):
    def test_source_only_wake_requires_complete_variable_not_static_history(self):
        filings, rows = fixture()
        self.assertTrue(needs_period_choices(catalog("company:ticker:one", filings, rows)))
        static = copy.deepcopy(rows)
        for row in static:
            row["dimensions"].pop("srt:ConsolidationItemsAxis", None)
            row["dimension_count"] = len(row["dimensions"])
        self.assertFalse(needs_period_choices(catalog("company:ticker:one", filings, static)))
        incomplete = [r for r in rows if r["line_id"] != rows[1]["line_id"]]
        self.assertFalse(needs_period_choices(catalog("company:ticker:one", filings, incomplete)))

    def bound_drivers(self, company="company:ticker:one"):
        filings, rows = fixture(company)
        c = catalog(company, filings, rows)
        drivers = []
        for member in ("issuer:FirstMember", "issuer:SecondMember"):
            d = {"ref": member, "basis_concept": "us-gaap:Revenues",
                 "basis_identity": {"statement": "income", "dimensions": {**FIXED, AXIS: member}},
                 "basis_period_family_refs": [g["family_ref"] for g in c["groups"]]}
            d["basis_period_family_bindings"] = driver_bindings({"period_source_family_catalog": c}, d)
            drivers.append(d)
        return filings, rows, c, drivers

    def test_saved_bindings_rebuild_all_original_scopes_for_two_companies(self):
        for company in ("company:ticker:one", "company:ticker:two"):
            filings, rows, c, drivers = self.bound_drivers(company)
            validate_sum_bindings(drivers, [{"operator": "sum", "driver_refs": [d["ref"] for d in drivers]}])
            for d in drivers:
                proof = d["basis_period_family_bindings"]
                self.assertEqual(proof, verify_driver_bindings(proof, driver=d, company_ref=company,
                                                              filings=filings, rows=rows))
                executed = {"statement_identity": d["basis_identity"], "statement_period_family_bindings": proof}
                self.assertEqual(identity_for_row(executed, rows[1])["dimensions"], {AXIS: d["ref"]})
                self.assertEqual(identity_for_row(executed, rows[4])["dimensions"], {**FIXED, AXIS: d["ref"]})

    def test_rehashed_saved_proof_cannot_omit_old_operand_or_change_original(self):
        filings, rows, c, drivers = self.bound_drivers()
        d = copy.deepcopy(drivers[0])
        d["basis_period_family_refs"] = d["basis_period_family_refs"][1:]
        p = d["basis_period_family_bindings"]
        p["bindings"] = p["bindings"][1:]
        p["content_hash"] = content_hash({k:v for k,v in p.items() if k != "content_hash"})
        with self.assertRaisesRegex(PeriodSourceFamilyError, "every admitted original duration"):
            verify_driver_bindings(p, driver=d, company_ref=c["company_ref"], filings=filings, rows=rows)
        changed = copy.deepcopy(rows); changed[1]["value"] = "59"
        with self.assertRaises(PeriodSourceFamilyError):
            verify_driver_bindings(drivers[0]["basis_period_family_bindings"], driver=drivers[0],
                company_ref=c["company_ref"], filings=filings, rows=changed)

    def test_full_sum_requires_same_complete_family_and_no_single_leaf_shortcut(self):
        filings, rows, c, drivers = self.bound_drivers()
        with self.assertRaisesRegex(PeriodSourceFamilyError, "every member"):
            validate_sum_bindings(drivers, [{"operator": "sum", "driver_refs": [drivers[0]["ref"]]}])
        with self.assertRaisesRegex(PeriodSourceFamilyError, "complete revenue sum"):
            validate_sum_bindings(drivers, [])

    def test_legacy_identity_unchanged_and_malformed_or_duplicate_proof_rejected(self):
        identity = {"statement": "income", "dimensions": {AXIS: "issuer:FirstMember"}}
        self.assertEqual(identity_for_row({"statement_identity": identity}, {}), identity)
        _, _, _, drivers = self.bound_drivers()
        for proof in ({}, [], {**drivers[0]["basis_period_family_bindings"], "bindings": []}):
            with self.assertRaises(PeriodSourceFamilyError):
                identity_for_row({"statement_identity": identity, "statement_period_family_bindings": proof}, {})
    def test_two_companies_explicit_period_families_keep_full_original_dimensions(self):
        for company in ("company:ticker:one", "company:ticker:two"):
            with self.subTest(company=company):
                filings, rows = fixture(company)
                original = copy.deepcopy(rows)
                c = catalog(company, filings, rows)
                self.assertEqual(len(c["groups"]), 2)
                proof = preflight(c, [g["family_ref"] for g in c["groups"]], requirements(c["groups"]),
                                  company_ref=company, filings=filings, rows=rows)
                self.assertEqual(proof["selected_families"][1]["fixed_dimensions"], FIXED)
                self.assertEqual(rows, original)

    def test_cumulative_operand_cannot_be_omitted_to_shrink_history(self):
        filings, rows = fixture()
        c = catalog("company:ticker:one", filings, rows)
        with self.assertRaisesRegex(PeriodSourceFamilyError, "historical operand periods missing"):
            preflight(c, [c["groups"][1]["family_ref"]], requirements(c["groups"]),
                      company_ref="company:ticker:one", filings=filings, rows=rows)

    def test_incomplete_family_cannot_borrow_a_peer_from_another_fixed_family(self):
        filings, rows = fixture()
        rows[1]["dimensions"] = {**FIXED, AXIS: "issuer:FirstMember"}
        rows[1]["dimension_count"] = 2
        c = catalog("company:ticker:one", filings, rows)
        self.assertEqual(len(c["groups"]), 1)
        self.assertTrue(c["gaps"])
        self.assertEqual(c["groups"][0]["period_end"], "2024-06-30")

    def test_duplicate_equal_identity_retains_origins_and_conflict_refuses(self):
        filings, rows = fixture()
        duplicate = {**rows[1], "line_id": "duplicate-origin"}
        rows.append(duplicate)
        c = catalog("company:ticker:one", filings, rows)
        self.assertEqual(len(c["groups"][0]["member_sources"][0]["origin_refs"]), 2)
        duplicate["value"] = "61"
        with self.assertRaisesRegex(PeriodSourceFamilyError, "conflicting amounts"):
            catalog("company:ticker:one", filings, rows)

    def test_unit_mismatch_and_foreign_filing_never_choose_by_equal_value(self):
        filings, rows = fixture()
        rows[2]["unit"] = "EUR"
        c = catalog("company:ticker:one", filings, rows)
        self.assertEqual(len(c["groups"]), 1)
        filings[0]["company_ref"] = "company:ticker:other"
        with self.assertRaisesRegex(PeriodSourceFamilyError, "foreign"):
            catalog("company:ticker:one", filings, rows)

    def test_new_dimension_does_not_silently_disappear(self):
        filings, rows = fixture()
        rows[1]["dimensions"]["issuer:CustomerAxis"] = "issuer:BigCustomer"
        rows[1]["dimension_count"] = 2
        c = catalog("company:ticker:one", filings, rows)
        self.assertEqual(len(c["groups"]), 1)

    def test_rehashed_catalog_tamper_still_rebuilds_full_source_authority(self):
        filings, rows = fixture()
        c = catalog("company:ticker:one", filings, rows)
        c["groups"][0]["filing_hash"] = "b" * 64
        c["content_hash"] = content_hash({k: v for k, v in c.items() if k != "content_hash"})
        with self.assertRaisesRegex(PeriodSourceFamilyError, "differs from complete original"):
            preflight(c, [g["family_ref"] for g in c["groups"]], requirements(c["groups"]),
                      company_ref="company:ticker:one", filings=filings, rows=rows)

    def test_foreign_or_repeated_selection_and_changed_history_members_refuse(self):
        filings, rows = fixture()
        c = catalog("company:ticker:one", filings, rows)
        selected = [g["family_ref"] for g in c["groups"]]
        for selection, needed in ((selected + selected[:1], requirements(c["groups"])),
                                  (["period-source-family:foreign"], requirements(c["groups"])),
                                  (selected, [{**r, "members": ["issuer:FirstMember", "issuer:ThirdMember"]}
                                              for r in requirements(c["groups"])])):
            with self.assertRaises(PeriodSourceFamilyError):
                preflight(c, selection, needed, company_ref="company:ticker:one", filings=filings, rows=rows)


if __name__ == "__main__":
    unittest.main()
