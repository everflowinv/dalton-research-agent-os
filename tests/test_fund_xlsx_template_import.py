from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from dalton_core.fund_xlsx_template_import import (
    FundXlsxTemplateImportError, NUMBER_STYLE_NAMES, STYLE_NAMES,
    import_fund_xlsx_template_candidate, main, _source_bytes,
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


class SourceWorkbookStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "source.xlsx"
        self.path.write_bytes(b"original source bytes")

    def reading_action(self, action):
        """Run a real file operation after the real descriptor has been read."""
        fdopen = os.fdopen
        class Reader:
            def __init__(self, handle): self.handle = handle
            def __enter__(self): self.handle.__enter__(); return self
            def __exit__(self, *args): return self.handle.__exit__(*args)
            def fileno(self): return self.handle.fileno()
            def read(self, *args):
                data = self.handle.read(*args)
                action()
                return data
        return mock.patch("dalton_core.fund_xlsx_template_import.os.fdopen",
                          side_effect=lambda fd, *args, **kw: Reader(fdopen(fd, *args, **kw)))

    def test_a_real_read_that_only_updates_atime_is_accepted(self):
        os.utime(self.path, ns=(1_000_000_000, 2_000_000_000))
        before = self.path.lstat()
        self.assertEqual(_source_bytes(self.path), b"original source bytes")
        after = self.path.lstat()
        for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"):
            self.assertEqual(getattr(before, field), getattr(after, field), field)
        if before.st_atime_ns == after.st_atime_ns:
            self.skipTest("filesystem does not update atime on reads")
        self.assertNotEqual(before, after)  # the old full-stat comparison refused this

    def test_same_size_mutation_with_restored_mtime_is_refused(self):
        held = self.path.stat()
        def mutate():
            self.path.write_bytes(b"mutated! source bytes")
            os.utime(self.path, ns=(held.st_atime_ns, held.st_mtime_ns))
        with self.reading_action(mutate), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)

    def test_same_content_inode_replacement_during_read_is_refused(self):
        replacement = self.path.with_name("replacement.xlsx")
        replacement.write_bytes(b"original source bytes")
        with self.reading_action(lambda: os.replace(replacement, self.path)), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)

    def test_symlink_replacement_during_read_is_refused(self):
        target = self.path.with_name("other.xlsx")
        target.write_bytes(b"original source bytes")
        def replace():
            self.path.unlink(); self.path.symlink_to(target)
        with self.reading_action(replace), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)

    def test_inode_replacement_before_open_is_refused_by_fd_identity(self):
        replacement = self.path.with_name("replacement.xlsx")
        replacement.write_bytes(b"original source bytes")
        open_fd = os.open
        def replace_and_open(*args, **kw):
            os.replace(replacement, self.path)
            return open_fd(*args, **kw)
        with mock.patch("dalton_core.fund_xlsx_template_import.os.open", side_effect=replace_and_open), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)

    def test_symlink_replacement_before_open_is_not_followed(self):
        target = self.path.with_name("other.xlsx")
        target.write_bytes(b"original source bytes")
        open_fd = os.open
        def replace_and_open(*args, **kw):
            self.path.unlink(); self.path.symlink_to(target)
            return open_fd(*args, **kw)
        with mock.patch("dalton_core.fund_xlsx_template_import.os.open", side_effect=replace_and_open), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)

    def test_permission_change_during_read_is_refused(self):
        mode = self.path.stat().st_mode & 0o777
        with self.reading_action(lambda: self.path.chmod(mode ^ 0o040)), self.assertRaisesRegex(FundXlsxTemplateImportError, "changed while read"):
            _source_bytes(self.path)


if __name__ == "__main__": unittest.main()
