from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from dalton_core.fund_xlsx_template_import import (
    FundXlsxTemplateImportError, NUMBER_STYLE_NAMES, STYLE_NAMES,
    import_fund_xlsx_template_candidate, main,
)


class FundXlsxTemplateImportTests(unittest.TestCase):
    def workbook(self, path: Path) -> None:
        from openpyxl import Workbook
        from openpyxl.comments import Comment
        from openpyxl.workbook.defined_name import DefinedName
        from openpyxl.styles import Border, Font, NamedStyle, PatternFill, Side

        book = Workbook()
        valuation = book.active; valuation.title = "SecretCo Valuation"
        financials = book.create_sheet("SecretCo Financials")
        driver = book.create_sheet("SecretCo Drivers")
        for sheet in (valuation, financials, driver):
            sheet.sheet_view.showGridLines = False
            sheet.sheet_format.defaultRowHeight = 11.25
            for column, width in zip("ABCDEFGHI", (1.875, 2.125, 2.875, 24.375,
                                                    7.125, 7.125, 1.375, 6.375, 1.125)):
                sheet.column_dimensions[column].width = width
        financials.freeze_panes = driver.freeze_panes = "E2"
        driver.column_dimensions["D"].width = 21.625
        driver.column_dimensions["G"].width = 1.125
        driver.column_dimensions["H"].width = 6.625
        driver.column_dimensions["I"].width = 0.875
        valuation.column_dimensions["A"].width = 14.625
        valuation.column_dimensions["B"].width = 14.375
        for column in "CDE": valuation.column_dimensions[column].width = 9.125
        for name in ("model_default", "valuation_default", *STYLE_NAMES):
            style = NamedStyle(name=name, font=Font(name="Arial", size=8, color="FF000000"))
            if name in {"unit_header", "period_header"}:
                style.font = Font(name="Arial", size=8, bold=True, color="FFFFFFFF")
                style.fill = PatternFill("solid", fgColor="FF3366FF")
            if name == "assumption_input":
                side = Side(style="hair", color="FF000000")
                style.border = Border(top=side, bottom=side, left=side, right=side)
            book.add_named_style(style)
        for kind, name in NUMBER_STYLE_NAMES.items():
            style = NamedStyle(name=name, font=Font(name="Arial", size=8))
            style.number_format = {
                "amount": "_(#,##0_);\\(#,##0\\);_(?\\-?_);@",
                "amount_one_decimal": "_(#,##0.0_);\\(#,##0.0\\);_(?\\-?_);@",
                "percentage": "0.0%", "multiple": "#,##0.0\\x",
                "date_month": "mmm-yy",
                "per_share": "_(#,##0.00_);\\(#,##0.00\\);_(?\\-?_);@",
            }[kind]
            book.add_named_style(style)
        financials["A1"] = "SecretCo Holdings"
        financials["B2"] = "=HYPERLINK(\"https://secret.example\",\"secret\")"
        financials["B2"].comment = Comment("private analyst comment", "Alice Analyst")
        financials["B2"].hyperlink = "https://secret.example/internal"
        book.defined_names.add(DefinedName(
            "SecretCompanyForecast", attr_text="'SecretCo Financials'!$B$2"))
        book.save(path)

    def imported(self, path: Path) -> dict:
        return import_fund_xlsx_template_candidate(
            path, sheets={"valuation": "SecretCo Valuation",
                          "financials": "SecretCo Financials",
                          "driver": "SecretCo Drivers"}, template_slug="owner-reference")

    def test_candidate_is_closed_redacted_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "reference.xlsx"
            self.workbook(path)
            first = self.imported(path); second = self.imported(path)
            self.assertEqual(first, second)
            self.assertEqual("candidate_pending_owner_review", first["status"])
            self.assertFalse(first["activation_authorized"])
            self.assertEqual(set(STYLE_NAMES), set(first["style_contract"]["styles"]))
            self.assertEqual(set(NUMBER_STYLE_NAMES),
                             set(first["style_contract"]["number_formats"]))
            payload = (json.dumps(first["style_contract"], indent=2) + "\n").encode()
            with mock.patch("dalton_core.fund_xlsx_template._resource_bytes",
                            return_value=payload), mock.patch(
                                "dalton_core.fund_xlsx_template.STYLE_RESOURCE_SHA256",
                                __import__("hashlib").sha256(payload).hexdigest()):
                from dalton_core.fund_xlsx_template import load_fund_xlsx_template_style
                self.assertEqual(first["style_contract"],
                                 load_fund_xlsx_template_style())
            wire = json.dumps(first)
            for secret in ("SecretCo", "Holdings", "secret.example", "private analyst",
                           "Alice Analyst", "SecretCompanyForecast", "HYPERLINK"):
                self.assertNotIn(secret, wire)

    def test_dry_run_stdout_and_exclusive_candidate_file_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); workbook = root / "reference.xlsx"
            self.workbook(workbook); output = root / "candidate.json"
            args = ["--workbook", str(workbook), "--valuation-sheet", "SecretCo Valuation",
                    "--financials-sheet", "SecretCo Financials", "--driver-sheet",
                    "SecretCo Drivers", "--template-slug", "owner-reference"]
            self.assertEqual(0, main([*args, "--output", str(output)]))
            self.assertEqual(self.imported(workbook), json.loads(output.read_text()))
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            with self.assertRaises(FundXlsxTemplateImportError):
                main([*args, "--output", str(output)])

    def test_symlink_and_external_link_archive_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); workbook = root / "reference.xlsx"
            self.workbook(workbook)
            link = root / "linked.xlsx"; link.symlink_to(workbook)
            with self.assertRaisesRegex(FundXlsxTemplateImportError, "direct regular"):
                self.imported(link)
            with zipfile.ZipFile(workbook, "a") as archive:
                archive.writestr("xl/externalLinks/externalLink1.xml", "<externalLink/>")
            with self.assertRaisesRegex(FundXlsxTemplateImportError, "external"):
                self.imported(workbook)

    def test_ambiguous_role_binding_and_missing_styles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "reference.xlsx"; self.workbook(path)
            with self.assertRaisesRegex(FundXlsxTemplateImportError, "ambiguous"):
                import_fund_xlsx_template_candidate(
                    path, sheets={"valuation": "SecretCo Valuation",
                                  "financials": "SecretCo Financials",
                                  "driver": "SecretCo Financials"}, template_slug="owner-reference")


if __name__ == "__main__": unittest.main()
