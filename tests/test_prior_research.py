"""W3: the fund's own earlier work -- manifest, feed, model, v0, prior_view.

Everything here runs offline against synthetic material. No real prior screen,
memo or model is committed to this repository: the corpus is built into a temp
directory at test time and the workbook is written by openpyxl in the test that
needs it.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from dalton_core import coverage_mission as coverage_mission_module
from dalton_core.claim_index_authority import IMPORTANCE_RANK, IMPORTANCE_TIERS
from dalton_core.claim_index_tagging import (
    SOURCE_TYPE_IMPORTANCE,
    SPEC_IMPORTANCE,
    STALE_AFTER_DAYS,
    STALE_DOWNGRADE,
    STALE_MARK,
    rule_tags,
    stale_importance,
)
from dalton_core.connector_governance import (
    PRIOR_RESEARCH_GET_KIND,
    PRIOR_RESEARCH_LIST_KIND,
    build_governance_record,
)
from dalton_core.coverage_mission import AUTOMATION_WRITE_SCOPES, CoverageMissionAuthority
from dalton_core.document_figure_grade import (
    GRADES,
    GRADE_BY_SPEC,
    INTERNAL_PRIOR,
    NON_FIGURE_GRADES,
    basis_for,
    figure_worthy,
    qualify,
)
from dalton_core.initial_screen import (
    GATE_ITEM_STATUSES,
    PRIOR_REFERENCE_INSTRUCTION,
    assess_exit_gate,
    build_section_prompt,
)
from dalton_core.mission_deliverable import (
    IMPORT_CHANGE_REASON,
    MissionDeliverableAuthority,
    MissionDeliverableConflict,
    MissionDeliverableValidationError,
)
from dalton_core.model_forecast_driver import CHANGE_REASONS
from dalton_core.prior_model_import import (
    ASSUMPTION_KIND,
    PriorModelAuthority,
    PriorModelError,
    decimal_text,
    guess_unit,
    prior_assumption_bands,
    read_workbook,
    workbook_digest,
)
from dalton_core.prior_research_core import (
    DOCUMENT_KINDS,
    EVIDENCE_TIER,
    MANIFEST_NAME,
    ROOT_ENV_VAR,
    PriorResearchError,
    PriorResearchRefusal,
    age_in_months,
    document_ref,
    enumerate_documents,
    parse_manifest,
    prior_research_identity,
    read_document,
)
from dalton_core.prior_screen_import import (
    attach_prior_views,
    build_delta_vs_prior,
    import_prior_screen,
    imported_gate,
    prior_reference,
    prior_view_material,
    split_sections,
)
from dalton_core.research_verification import FIGURE_ADMISSIBLE_GRADES
from dalton_core.store import DaltonStore, canonical_json
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

OWNER = "human:coverage-owner"
ACN = "company:sec-cik:0001467373"
PRIOR_RESEARCH = "source:prior-research"

SCREEN_TEXT = """# 结论
我们认为 Accenture 的 bookings 增长是 mix shift 而不是新的资金池。

# 关注点
GenAI bookings 的口径能不能和总 bookings 对上？
"""


def build_corpus(root: Path, *, entries: list[dict[str, Any]] | None = None,
                 company: str = "ACN", as_of: str = "2024-03-28") -> Path:
    """A prior-research corpus with one company folder and a manifest.

    ``as_of`` is a parameter because the coordinator tests have to put the
    document inside one enumeration window: the tick walks the lookback
    fourteen days at a time and spawns a governed child per window, so a
    fixture dated in 2024 would cost a hundred subprocesses to reach.
    """

    folder = root / company
    (folder / "2024").mkdir(parents=True, exist_ok=True)
    (folder / "2024" / "screen.md").write_text(SCREEN_TEXT, encoding="utf-8")
    (folder / "2024" / "undated.md").write_text("没有日期的备忘。\n", encoding="utf-8")
    documents = entries if entries is not None else [
        {"path": "2024/screen.md", "kind": "initial_screen", "as_of": as_of,
         "author": "human:pm", "source_note": "FY24 Q2 电话会之前写的"},
        {"path": "2024/undated.md", "kind": "memo", "author": "human:pm"},
    ]
    (folder / MANIFEST_NAME).write_text(
        json.dumps({"documents": documents}, ensure_ascii=False), encoding="utf-8"
    )
    return root


class ManifestTests(unittest.TestCase):
    def test_an_entry_with_no_as_of_is_refused_with_its_reason(self) -> None:
        entries, refusals = parse_manifest(
            {"documents": [
                {"path": "a.md", "kind": "memo", "as_of": "2024-01-02"},
                {"path": "b.md", "kind": "memo"},
                {"path": "c.md", "kind": "memo", "as_of": ""},
                {"path": "d.md", "kind": "memo", "as_of": "2024-13-40"},
                {"path": "e.md", "kind": "memo", "as_of": "March 2024"},
            ]},
            company="ACN",
        )
        self.assertEqual([item["path"] for item in entries], ["a.md"])
        self.assertEqual([item["relative_path"] for item in refusals],
                         ["b.md", "c.md", "d.md", "e.md"])
        self.assertIn("as_of", refusals[0]["reason"])
        # The reason says why the rule exists, not only that it was broken.
        self.assertIn("aged", refusals[1]["reason"])
        self.assertIn("not a YYYY-MM-DD date", refusals[3]["reason"])

    def test_a_bad_entry_does_not_take_the_folder_with_it(self) -> None:
        entries, refusals = parse_manifest(
            {"documents": [
                {"path": "a.md", "kind": "memo", "as_of": "2024-01-02"},
                {"path": "b.md", "kind": "not_a_kind", "as_of": "2024-01-02"},
                {"path": "c.md", "kind": "notes", "as_of": "2023-01-02"},
            ]},
            company="ACN",
        )
        self.assertEqual([item["path"] for item in entries], ["a.md", "c.md"])
        self.assertEqual(len(refusals), 1)

    def test_a_manifest_that_will_not_parse_is_not_partial(self) -> None:
        with self.assertRaises(PriorResearchError):
            parse_manifest("{not json", company="ACN")
        with self.assertRaises(PriorResearchError):
            parse_manifest({"files": []}, company="ACN")

    def test_the_kinds_are_the_owner_s_five(self) -> None:
        self.assertEqual(
            DOCUMENT_KINDS,
            ("initial_screen", "memo", "notes", "model_excel", "other"),
        )

    def test_a_duplicate_path_is_refused_rather_than_read_twice(self) -> None:
        _, refusals = parse_manifest(
            {"documents": [
                {"path": "a.md", "kind": "memo", "as_of": "2024-01-02"},
                {"path": "a.md", "kind": "notes", "as_of": "2024-02-02"},
            ]},
            company="ACN",
        )
        self.assertEqual(len(refusals), 1)
        self.assertIn("twice", refusals[0]["reason"])

    def test_the_root_is_declared_by_an_environment_variable(self) -> None:
        self.assertEqual(ROOT_ENV_VAR, "DALTON_PRIOR_RESEARCH_DIR")

    def test_age_is_counted_in_whole_months(self) -> None:
        self.assertEqual(age_in_months("2024-03-28", now=date(2026, 9, 10)), 29)
        self.assertEqual(age_in_months("2026-09-01", now=date(2026, 9, 10)), 0)


class CorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = build_corpus(Path(self.temp.name))

    def test_a_window_enumerates_the_dated_and_refuses_the_rest(self) -> None:
        documents, refused, truncated = enumerate_documents(
            self.root, since="2000-01-01", until="2030-01-01"
        )
        self.assertFalse(truncated)
        self.assertEqual(len(documents), 1)
        header = documents[0]
        self.assertEqual(header["kind"], "initial_screen")
        self.assertEqual(header["as_of"], "2024-03-28")
        self.assertEqual(header["as_of_basis"], "manifest_as_of")
        self.assertEqual(header["evidence_tier"], EVIDENCE_TIER)
        self.assertEqual(header["doc_format"], "markdown")
        self.assertEqual(header["company"], "ACN")
        self.assertEqual(len(refused), 1)

    def test_a_document_reads_back_verbatim_under_its_own_id(self) -> None:
        ref = document_ref("ACN", "2024/screen.md")
        header, text = read_document(self.root, ref)
        self.assertEqual(header["document_id"], ref)
        self.assertEqual(text, SCREEN_TEXT)
        with self.assertRaises(PriorResearchError):
            read_document(self.root, "prior-research-doc:sha256:" + "0" * 64)

    def test_a_path_that_leaves_its_company_folder_is_refused(self) -> None:
        build_corpus(self.root, entries=[
            {"path": "../../etc/hosts", "kind": "memo", "as_of": "2024-01-02"},
        ])
        _, refused, _ = enumerate_documents(
            self.root, since="2000-01-01", until="2030-01-01"
        )
        self.assertEqual(len(refused), 1)
        self.assertIn("escapes", refused[0]["reason"])

    def test_an_unreadable_format_is_refused_with_its_suffix(self) -> None:
        (self.root / "ACN" / "notes.rtf").write_text("x", encoding="utf-8")
        build_corpus(self.root, entries=[
            {"path": "notes.rtf", "kind": "notes", "as_of": "2024-01-02"},
        ])
        _, refused, _ = enumerate_documents(
            self.root, since="2000-01-01", until="2030-01-01"
        )
        self.assertIn(".rtf", refused[0]["reason"])


class GovernanceTests(unittest.TestCase):
    def test_the_committed_records_are_proposed_and_match_the_builder(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for kind in (PRIOR_RESEARCH_LIST_KIND, PRIOR_RESEARCH_GET_KIND):
            with self.subTest(kind=kind):
                path = repo / "deploy/connector-governance" / f"{kind}-v1.json"
                committed = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(committed["status"], "proposed")
                built = build_governance_record(
                    kind, approved_by=OWNER, status="proposed",
                    effective_from="2026-09-10T00:00:00+00:00",
                )
                self.assertEqual(committed, json.loads(canonical_json(built)))

    def test_one_approval_covers_one_operation(self) -> None:
        listing = prior_research_identity("list_documents")
        getting = prior_research_identity("get_document")
        self.assertEqual(listing["source_hash"], getting["source_hash"])
        self.assertNotEqual(listing["schema_hash"], getting["schema_hash"])
        self.assertNotEqual(listing["capability_id"], getting["capability_id"])


class ChildTests(unittest.TestCase):
    """The CLI, in process: approval first, artifact always, contract last."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = build_corpus(self.root / "corpus")
        self.state = self.root / "state"
        self.state.mkdir()

    def governance(self, kind: str, *, status: str = "approved") -> Path:
        record = build_governance_record(kind, approved_by=OWNER, status=status)
        path = self.state / f"{kind}-{status}.json"
        path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        return path

    def run_child(self, **overrides: Any) -> dict[str, Any]:
        from dalton_core import prior_research_cli as cli

        argv = ["--state-dir", str(self.state), "--corpus-root", str(self.corpus)]
        for key, value in overrides.items():
            if value is None:
                continue
            argv += ["--" + key.replace("_", "-"), str(value)]
        return cli.run(cli.build_parser().parse_args(argv))

    def test_a_listing_lands_with_its_tier_its_as_of_and_its_refusals(self) -> None:
        summary = self.run_child(
            governance=self.governance(PRIOR_RESEARCH_LIST_KIND),
            operation="list_documents", since="2000-01-01", until="2030-01-01",
        )
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        wire = summary["observation"]
        self.assertEqual(wire["document_count"], 1)
        self.assertEqual(wire["documents"][0]["evidence_tier"], "internal_prior")
        self.assertEqual(wire["documents"][0]["as_of"], "2024-03-28")
        self.assertEqual(wire["refused_count"], 1)
        self.assertIn("as_of", wire["refused"][0]["reason"])

    def test_a_document_acquires_with_a_manifest_dated_by_the_owner(self) -> None:
        summary = self.run_child(
            governance=self.governance(PRIOR_RESEARCH_GET_KIND),
            operation="get_document",
            document_id=document_ref("ACN", "2024/screen.md"),
            summary_dir=self.state,
        )
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        manifest = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_ref"], PRIOR_RESEARCH)
        self.assertEqual(manifest["evidence_tier"], "internal_prior")
        # The document's date, not the day it was read.
        self.assertEqual(manifest["doc_date"], "2024-03-28")
        self.assertEqual(manifest["subject_tickers"], ["ACN"])

    def test_an_unapproved_or_wrong_capability_record_stops_the_run(self) -> None:
        summary = self.run_child(
            governance=self.governance(PRIOR_RESEARCH_LIST_KIND, status="proposed"),
            operation="list_documents", since="2000-01-01", until="2030-01-01",
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

        summary = self.run_child(
            governance=self.governance(PRIOR_RESEARCH_LIST_KIND),
            operation="get_document",
            document_id=document_ref("ACN", "2024/screen.md"),
        )
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])
        self.assertTrue((self.state / "summary.json").is_file())


class ImportanceTests(unittest.TestCase):
    def test_internal_prior_sits_between_management_and_the_sell_side(self) -> None:
        self.assertEqual(
            IMPORTANCE_TIERS,
            ("filing", "management_statement", "internal_prior", "sell_side",
             "news", "other"),
        )
        self.assertLess(IMPORTANCE_RANK["management_statement"],
                        IMPORTANCE_RANK["internal_prior"])
        self.assertLess(IMPORTANCE_RANK["internal_prior"], IMPORTANCE_RANK["sell_side"])

    def test_the_feed_spec_carries_the_tier(self) -> None:
        self.assertEqual(SPEC_IMPORTANCE["prior-research"], "internal_prior")
        # Not through the source-type fallback: several libraries share that
        # word and only the spec says which one this is.
        self.assertNotIn("authenticated_library", SOURCE_TYPE_IMPORTANCE)

    def test_a_prior_view_is_downgraded_exactly_at_the_threshold(self) -> None:
        self.assertEqual(STALE_AFTER_DAYS, 180)
        as_of = "2026-01-01"
        edge = date(2026, 1, 1) + __import__("datetime").timedelta(days=STALE_AFTER_DAYS)
        fresh, basis = stale_importance(
            "internal_prior", "discovery_spec:prior-research", as_of=as_of,
            as_of_basis="manifest_as_of",
            now=edge - __import__("datetime").timedelta(days=1),
        )
        self.assertEqual(fresh, "internal_prior")
        self.assertNotIn(STALE_MARK, basis)
        stale, basis = stale_importance(
            "internal_prior", "discovery_spec:prior-research", as_of=as_of,
            as_of_basis="manifest_as_of", now=edge,
        )
        self.assertEqual(stale, STALE_DOWNGRADE["internal_prior"])
        self.assertIn(STALE_MARK, basis)
        # The reason keeps the tier it came from, so the downgrade is legible.
        self.assertIn("internal_prior", basis)

    def test_only_the_prior_tier_ages(self) -> None:
        # A 10-K from 2019 is still the company publishing that number for that
        # period; it does not become less filed.
        for tier in ("filing", "management_statement", "sell_side", "news"):
            with self.subTest(tier=tier):
                self.assertEqual(
                    stale_importance(tier, "b", as_of="2019-01-01",
                                     as_of_basis="period_end", now=date(2026, 9, 10))[0],
                    tier,
                )
        self.assertEqual(list(STALE_DOWNGRADE), ["internal_prior"])

    def test_an_undated_prior_claim_is_not_aged(self) -> None:
        self.assertEqual(
            stale_importance("internal_prior", "b", as_of=None,
                             as_of_basis="unknown", now=date(2026, 9, 10))[0],
            "internal_prior",
        )

    def test_the_rule_tagger_reads_the_threshold(self) -> None:
        tags = rule_tags(
            {"claim_kind": "qualitative", "subject_ref": ACN,
             "normalized_statement": "s", "period": "2024-03-28"},
            {"importance": "internal_prior",
             "importance_basis": "discovery_spec:prior-research"},
            now=date(2026, 9, 10),
        )
        self.assertEqual(tags["importance"], "sell_side")
        self.assertIn(STALE_MARK, tags["importance_basis"])


class GradeTests(unittest.TestCase):
    def test_the_prior_grade_exists_and_is_never_a_figure(self) -> None:
        self.assertEqual(INTERNAL_PRIOR, "internal-prior-document")
        self.assertEqual(NON_FIGURE_GRADES, (INTERNAL_PRIOR,))
        self.assertNotIn(INTERNAL_PRIOR, GRADES)
        self.assertNotIn(INTERNAL_PRIOR, GRADE_BY_SPEC.values())
        self.assertNotIn(INTERNAL_PRIOR, FIGURE_ADMISSIBLE_GRADES)
        self.assertFalse(figure_worthy("prior-research"))

    def test_it_still_tells_a_reader_what_it_is(self) -> None:
        self.assertEqual(basis_for(INTERNAL_PRIOR), "internal-prior-document")
        self.assertIn("earlier work", qualify("Revenue was 64.1bn", INTERNAL_PRIOR))


class PriorModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workbook = self.root / "model.xlsx"
        self.build_workbook(self.workbook)
        self.store = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = PriorModelAuthority(self.store)

    @staticmethod
    def build_workbook(path: Path, *, growth: float = 0.055) -> None:
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "Drivers"
        sheet["A1"], sheet["B1"] = "Assumption", "FY25"
        sheet["A2"], sheet["B2"] = "Organic revenue growth %", growth
        sheet["B2"].number_format = "0.0%"
        sheet["A3"], sheet["B3"] = "Revenue ($mn)", 64100
        sheet["A4"], sheet["B4"] = "Implied revenue", "=B3*(1+B2)"
        book.save(path)

    def test_a_workbook_keeps_its_formulas_verbatim_and_its_values_as_text(self) -> None:
        rows = read_workbook(self.workbook)
        by_cell = {row["cell"]: row for row in rows}
        self.assertEqual(by_cell["B2"]["value"], "0.055")
        self.assertIsInstance(by_cell["B2"]["value"], str)
        self.assertEqual(by_cell["B2"]["unit"], "percent")
        self.assertEqual(by_cell["B2"]["unit_basis"], "number_format")
        self.assertEqual(by_cell["B4"]["formula"], "=B3*(1+B2)")
        for row in rows:
            with self.subTest(cell=row["cell"]):
                self.assertNotIsInstance(row["value"], float)

    def test_no_float_survives_into_the_record(self) -> None:
        published = self.publish()
        wire = json.loads(canonical_json(published))

        def walk(node: Any) -> None:
            if isinstance(node, float):
                self.fail("a float reached the PriorModelVersion record")
            if isinstance(node, dict):
                for item in node.values():
                    walk(item)
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(wire)

    def test_every_imported_cell_is_prior_human_and_nothing_else(self) -> None:
        published = self.publish()
        self.assertTrue(all(row["kind"] == ASSUMPTION_KIND
                            for row in published["assumptions"]))
        with self.assertRaises(PriorModelError):
            self.authority.publish(
                company_ref=ACN, source_document_ref="prior-research-doc:sha256:" + "b" * 64,
                as_of="2024-03-28", workbook_sha256="c" * 64,
                assumptions=[{"sheet": "S", "cell": "A1", "value": "1",
                              "kind": "actual", "unit_basis": "unknown"}],
                actor_ref=OWNER,
            )

    def publish(self, *, as_of: str = "2024-03-28") -> dict[str, Any]:
        return self.authority.publish(
            company_ref=ACN,
            source_document_ref="prior-research-doc:sha256:" + "a" * 64,
            as_of=as_of, workbook_sha256=workbook_digest(self.workbook),
            assumptions=read_workbook(self.workbook), actor_ref=OWNER,
        )

    def test_the_chain_reads_back_and_an_unchanged_workbook_is_a_duplicate(self) -> None:
        first = self.publish()
        self.assertEqual((first["status"], first["version"]), ("fresh", 1))
        self.assertEqual(self.publish()["status"], "duplicate")
        stored = self.authority.model(first["id"])
        self.assertEqual(stored["content_hash"], first["content_hash"])

    def test_a_band_is_a_range_with_its_rows_and_its_unit(self) -> None:
        self.publish()
        band = prior_assumption_bands(
            self.store.connection, ACN, "organic revenue growth"
        )
        self.assertEqual(band["count"], 1)
        self.assertEqual(band["unit"], "percent")
        self.assertEqual((band["low"], band["high"]), ("0.055", "0.055"))
        self.assertEqual(band["matched_on"], "contains")
        self.assertEqual(band["rows"][0]["formula"], "")
        self.assertEqual(band["rows"][0]["kind"], ASSUMPTION_KIND)

    def test_a_label_nothing_carries_is_an_empty_band_with_a_reason(self) -> None:
        self.publish()
        band = prior_assumption_bands(self.store.connection, ACN, "attrition")
        self.assertEqual(band["count"], 0)
        self.assertIsNone(band["low"])
        self.assertIn("no prior assumption", band["reason"])

    def test_a_band_across_units_refuses_rather_than_averaging(self) -> None:
        self.publish()
        band = prior_assumption_bands(self.store.connection, ACN, "revenue")
        self.assertIsNone(band["low"])
        self.assertIn("unit", band["reason"])
        narrowed = prior_assumption_bands(
            self.store.connection, ACN, "revenue", unit="currency"
        )
        self.assertEqual(narrowed["low"], "64100")

    def test_the_prior_model_is_not_reachable_from_the_number_paths(self) -> None:
        import ast

        package = Path(__file__).resolve().parents[1] / "src" / "dalton_core"
        # The chokepoints: statement lines, forecast actuals, figure admission.
        for name in (
            "model_forecast", "model_forecast_driver", "forecast_reconciliation",
            "coverage_mission", "claim_index_figures", "research_verification",
            "statement_snapshot",
        ):
            with self.subTest(module=name):
                tree = ast.parse((package / f"{name}.py").read_text(encoding="utf-8"))
                names: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        names.append(node.module)
                    if isinstance(node, ast.Import):
                        names += [alias.name for alias in node.names]
                self.assertFalse(
                    [item for item in names if "prior_model" in item
                     or "prior_research" in item or "prior_screen" in item],
                    f"{name} imports a prior-research module; the arrow is one-way",
                )

    def test_decimals_are_exact_text_and_a_boolean_is_not_a_number(self) -> None:
        self.assertEqual(decimal_text(0.1), "0.1")
        self.assertEqual(decimal_text(5.60), "5.6")
        self.assertEqual(decimal_text(0), "0")
        with self.assertRaises(PriorModelError):
            decimal_text(True)
        with self.assertRaises(PriorModelError):
            decimal_text(float("nan"))

    def test_a_unit_it_cannot_be_sure_of_is_no_unit(self) -> None:
        self.assertEqual(guess_unit(label="", number_format="General"), (None, "unknown"))
        self.assertEqual(guess_unit(label="Gross margin", number_format="General"),
                         ("percent", "label"))


class DeliverableV0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(str(Path(self.temp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.playbook = self.state["playbook"]
        missions = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        ref = params.pop("mission_ref")
        params["autonomy"] = {
            **params["autonomy"],
            "may_write": sorted(set(params["autonomy"]["may_write"]) | {"deliverable"}),
        }
        self.mission = missions.create_mission(ref, **params)
        self.company = self.mission["universe"][0]["company_ref"]
        self.authority = MissionDeliverableAuthority(self.store)
        self.document = {
            "document_id": document_ref("ACN", "2024/screen.md"),
            "as_of": "2024-03-28", "author": "human:pm",
            "source_note": "FY24 Q2 电话会之前写的",
        }

    def import_screen(self) -> dict[str, Any]:
        return import_prior_screen(
            self.authority, mission=self.mission, playbook=self.playbook,
            company_ref=self.company, document=self.document, text=SCREEN_TEXT,
            actor_ref=OWNER,
        )

    def test_imported_prior_is_in_the_vocabulary_and_only_on_a_v0(self) -> None:
        self.assertIn(IMPORT_CHANGE_REASON, CHANGE_REASONS)
        self.assertEqual(CHANGE_REASONS[:5], (
            "filing_actual", "driver_event", "assumption_review",
            "evidence_thicker", "human_revision"))
        with self.assertRaises(MissionDeliverableValidationError):
            self.authority.publish(
                kind="initial_screen", subject_ref=self.company,
                mission=self.mission, playbook=self.playbook,
                template_ref="t", sections=[{"title": "T", "body": "b"}],
                summary="s", actor_ref=OWNER,
                revision={"change_reason": IMPORT_CHANGE_REASON,
                          "evidence_refs": ["x"]},
            )

    def test_a_v0_needs_the_import_reason(self) -> None:
        with self.assertRaises(MissionDeliverableValidationError):
            self.authority.publish(
                kind="initial_screen", subject_ref=self.company,
                mission=self.mission, playbook=self.playbook,
                template_ref="t", sections=[{"title": "T", "body": "b"}],
                summary="s", actor_ref=OWNER, as_version_zero=True,
                revision={"change_reason": "evidence_thicker", "evidence_refs": ["x"]},
            )

    def test_a_prior_screen_becomes_v0_and_dalton_writes_v1(self) -> None:
        v0 = self.import_screen()
        self.assertEqual((v0["status"], v0["version"]), ("fresh", 0))
        self.assertIsNone(v0["prior_version_ref"])
        self.assertEqual(v0["revision"]["change_reason"], IMPORT_CHANGE_REASON)
        self.assertIn(self.document["document_id"], v0["revision"]["evidence_refs"])
        self.assertEqual([item["title"] for item in v0["sections"]], ["结论", "关注点"])

        v1 = self.authority.publish(
            kind="initial_screen", subject_ref=self.company, mission=self.mission,
            playbook=self.playbook, template_ref="t",
            sections=[{"title": "结论", "body": "本版重新判断了 bookings 的口径。"}],
            summary="Dalton 自己写的第一版", actor_ref=OWNER,
        )
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v1["prior_version_ref"], v0["id"])

    def test_a_chain_can_only_start_once(self) -> None:
        self.import_screen()
        with self.assertRaises(MissionDeliverableConflict):
            import_prior_screen(
                self.authority, mission=self.mission, playbook=self.playbook,
                company_ref=self.company,
                document={**self.document,
                          "document_id": "prior-research-doc:sha256:" + "f" * 64},
                text="# 另一个\n别的东西。\n", actor_ref=OWNER,
            )

    def test_re_importing_the_same_document_is_a_duplicate(self) -> None:
        # The lane re-offers the same screen every tick for as long as the
        # file is on disk, so the second import has to be a no-op rather than
        # a conflict that reads like a fault.
        first = self.import_screen()
        again = self.import_screen()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["version"], 0)

        # And it stays a duplicate once Dalton's own v1 is the chain head,
        # which is the case the pointer-based duplicate check cannot see.
        self.authority.publish(
            kind="initial_screen", subject_ref=self.company, mission=self.mission,
            playbook=self.playbook, template_ref="t",
            sections=[{"title": "结论", "body": "本版重新判断了 bookings 的口径。"}],
            summary="Dalton 自己写的第一版", actor_ref=OWNER,
        )
        third = self.import_screen()
        self.assertEqual(third["status"], "duplicate")
        self.assertEqual(third["version"], 0)

    def test_a_version_zero_may_not_claim_it_passed(self) -> None:
        with self.assertRaises(MissionDeliverableValidationError) as caught:
            self.authority.publish(
                kind="initial_screen", subject_ref=self.company,
                mission=self.mission, playbook=self.playbook,
                template_ref="t", sections=[{"title": "T", "body": "b"}],
                summary="s", actor_ref=OWNER, as_version_zero=True,
                revision={"change_reason": IMPORT_CHANGE_REASON,
                          "evidence_refs": ["d:1"]},
                gate={"passed": True, "answers": []},
            )
        self.assertIn("cannot carry a passed gate", str(caught.exception))

    def test_the_v0_gate_marks_every_item_imported(self) -> None:
        v0 = self.import_screen()
        gate = v0["gate"]
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["imported"])
        self.assertEqual({item["status"] for item in gate["answers"]}, {"imported"})
        self.assertEqual(len(gate["answers"]), 4)
        for item in gate["answers"]:
            self.assertIn(item["status"], GATE_ITEM_STATUSES)
            self.assertNotIn("answer", item)

    def test_a_prior_figure_is_a_recorded_gap_not_a_refusal(self) -> None:
        v0 = import_prior_screen(
            self.authority, mission=self.mission, playbook=self.playbook,
            company_ref=self.company, document=self.document,
            text="# 结论\n收入 64,100 百万美元，我们当时这样算的。\n", actor_ref=OWNER,
        )
        gaps = [gap for section in v0["sections"] for gap in section["gaps"]]
        self.assertTrue(any("未在本系统重新核对" in gap for gap in gaps), gaps)

    def test_an_imported_v0_still_counts_as_no_screen_yet(self) -> None:
        from dalton_core.initial_screen_cli import _target

        v0 = self.import_screen()
        entry = {
            "company_ref": self.company, "stage": "initial_screen",
            "stage_status": "entered", "items": [], "ticker": "ACN",
        }
        target, skipped = _target(
            mission=self.mission, stage_rows=[entry],
            deliverables={self.company: v0},
            claims={self.company: [{"created_at": "2020-01-01T00:00:00+00:00"}]},
        )
        self.assertIsNotNone(target, skipped)

    def test_a_real_v1_does_stop_the_next_draft(self) -> None:
        from dalton_core.initial_screen_cli import _target

        v1 = {"version": 1, "created_at": "2026-09-10T00:00:00+00:00"}
        entry = {
            "company_ref": self.company, "stage": "initial_screen",
            "stage_status": "entered", "items": [], "ticker": "ACN",
        }
        target, skipped = _target(
            mission=self.mission, stage_rows=[entry],
            deliverables={self.company: v1},
            claims={self.company: [{"created_at": "2020-01-01T00:00:00+00:00"}]},
        )
        self.assertIsNone(target)
        self.assertIn("nothing new", skipped[0]["reason"])

    def test_the_drafting_context_carries_the_prior_block_and_the_instruction(self) -> None:
        v0 = self.import_screen()
        block = prior_reference(v0, now=date(2026, 9, 10))
        self.assertEqual(block["as_of"], "2024-03-28")
        self.assertEqual(block["age_months"], 29)
        self.assertTrue(block["imported"])
        prompt = build_section_prompt(
            title="结论", guidance="g", company={"ticker": "ACN", "company_ref": self.company},
            mission={"title": "t", "objective": "o", "research_questions": []},
            context={"claims": [], "numbers": [], "series": []},
            prior_reference=block,
        )
        self.assertIn("上一版（内部，2024-03", prompt)
        self.assertIn("29 个月", prompt)
        self.assertIn(PRIOR_REFERENCE_INSTRUCTION, prompt)
        self.assertIn("仍然成立", prompt)
        self.assertIn("不要照抄", prompt)
        self.assertIn("【结论】", prompt)

    def test_no_prior_version_means_no_block_at_all(self) -> None:
        self.assertIsNone(prior_reference(None))
        prompt = build_section_prompt(
            title="结论", guidance="g", company={"ticker": "ACN", "company_ref": self.company},
            mission={"title": "t", "objective": "o", "research_questions": []},
            context={"claims": [], "numbers": [], "series": []},
        )
        self.assertNotIn("上一版", prompt)

    def test_the_exit_gate_reports_the_delta_and_never_fails_on_it(self) -> None:
        v0 = self.import_screen()
        block = prior_reference(v0, now=date(2026, 9, 10))
        delta = build_delta_vs_prior(
            prior=block,
            filings=[{"ref": "0001", "form": "10-K", "filed_at": "2025-10-10"},
                     {"ref": "0000", "form": "10-Q", "filed_at": "2023-01-10"}],
            shifted_debates=[{"debate_ref": "d:1", "question": "q", "status": "shifting",
                              "last_shift_reason": "r"}],
            now=date(2026, 9, 10),
        )
        self.assertEqual(delta["new_filing_count"], 1)
        self.assertEqual(delta["debates_shifted_count"], 1)
        self.assertEqual(delta["prior_as_of"], "2024-03-28")
        self.assertFalse(delta["price_move"]["available"])
        sections = [
            {"title": f"T{index}", "body": "x" * 300,
             "claim_refs": ["a", "b", "c"], "numbers": [], "gaps": []}
            for index in range(6)
        ]
        gate = assess_exit_gate(
            playbook=self.playbook,
            checklist_entry={"items": [{"label": "L", "status": "complete"}]},
            sections=sections, delta_vs_prior=delta,
        )
        self.assertEqual(gate["delta_vs_prior"]["new_filing_count"], 1)
        # Reported, never a trigger: it is not one of the four answers.
        self.assertEqual(len(gate["answers"]), 4)
        self.assertTrue(gate["passed"])
        self.assertEqual({item["status"] for item in gate["answers"]}, {"passed"})

    def test_a_gate_with_no_prior_says_so_rather_than_guessing(self) -> None:
        self.assertIsNone(build_delta_vs_prior(prior=None))
        gate = assess_exit_gate(
            playbook=self.playbook,
            checklist_entry={"items": []}, sections=[{"title": "T", "body": "b",
                                                      "claim_refs": [], "numbers": [],
                                                      "gaps": []}],
        )
        self.assertIsNone(gate["delta_vs_prior"])

    def test_the_imported_gate_names_the_document_it_did_not_check(self) -> None:
        gate = imported_gate(playbook=self.playbook, document_ref="d:1",
                             as_of="2024-03-28")
        self.assertIn("d:1", gate["answers"][0]["basis"])
        self.assertIn("2024-03-28", gate["answers"][0]["basis"])

    def test_a_screen_with_no_headings_arrives_as_one_section(self) -> None:
        sections = split_sections("一段没有标题的旧笔记。\n还有第二行。\n")
        self.assertEqual(len(sections), 1)
        self.assertIn("旧笔记", sections[0]["body"])


class PriorViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(str(Path(self.temp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)

    def test_a_debate_map_reads_back_without_the_field(self) -> None:
        from dalton_core.debate_map import _debate

        base = self.debate()
        self.assertNotIn("prior_view", _debate(base, "debate"))

    def test_a_dated_prior_view_is_accepted_and_an_undated_one_is_not(self) -> None:
        from dalton_core.debate_map import DebateMapValidationError, _debate, cited_refs

        view = {"as_of": "2024-03-28", "source_kind": "initial_screen",
                "statement": "我们当时认为 bookings 是 mix shift。",
                "refs": ["mission-deliverable-version:abc"]}
        wire = _debate({**self.debate(), "prior_view": view}, "debate")
        self.assertEqual(wire["prior_view"]["as_of"], "2024-03-28")
        self.assertIn("mission-deliverable-version:abc",
                      cited_refs({"debates": [wire]}))
        for broken in ({**view, "as_of": "March 2024"},
                       {**view, "source_kind": "tweet"},
                       {**view, "refs": []}):
            with self.subTest(broken=broken):
                with self.assertRaises(DebateMapValidationError):
                    _debate({**self.debate(), "prior_view": broken}, "debate")

    def test_the_field_is_attached_when_there_is_material_and_absent_otherwise(self) -> None:
        debates = [self.debate(), {**self.debate(), "debate_ref": "debate:2"}]
        self.assertTrue(all("prior_view" not in item
                            for item in attach_prior_views(debates, [])))
        material = [{"as_of": "2023-01-01", "source_kind": "memo",
                     "statement": "旧的", "refs": ["claim-version:1"]},
                    {"as_of": "2024-03-28", "source_kind": "initial_screen",
                     "statement": "新一点的", "refs": ["mission-deliverable-version:abc"]}]
        attached = attach_prior_views(debates, material)
        self.assertTrue(all("prior_view" in item for item in attached))
        self.assertEqual(attached[0]["prior_view"]["as_of"], "2024-03-28")

    def test_no_prior_material_on_this_core_is_an_empty_list(self) -> None:
        self.assertEqual(prior_view_material(self.store, ACN), [])

    def test_the_reflection_accepts_a_dated_prior_view_and_refuses_a_stray_ref(self) -> None:
        from dalton_core.event_judgement import (
            EventJudgementValidationError,
            PRIOR_VIEW_VERDICTS,
            validate_reflection_output,
        )

        context = {
            "event": {"id": "event:1", "source_refs": [], "payload": {}},
            "theses": [{"ref": "thesis:1", "statement": "s"}],
            "drivers": [], "claims": [], "market_view": [],
            "prior_view": [{"as_of": "2024-03-28", "source_kind": "initial_screen",
                            "statement": "我们当时的看法",
                            "refs": ["mission-deliverable-version:abc"]}],
        }
        body = self.reflection()
        ok = validate_reflection_output({
            **body,
            "prior_view": {"as_of": "2024-03-28", "still_holds": "changed",
                           "summary": "口径已经变了",
                           "refs": ["mission-deliverable-version:abc"]},
        }, context)
        self.assertEqual(ok["prior_view"]["still_holds"], "changed")
        self.assertIn("changed", PRIOR_VIEW_VERDICTS)
        # Absent is fine.
        self.assertNotIn("prior_view", validate_reflection_output(body, context))
        for broken in (
            {"as_of": "2024-03-28", "still_holds": "changed", "summary": "s",
             "refs": ["invented:1"]},
            {"as_of": "yesterday", "still_holds": "changed", "summary": "s",
             "refs": ["mission-deliverable-version:abc"]},
            {"as_of": "2024-03-28", "still_holds": "maybe", "summary": "s",
             "refs": ["mission-deliverable-version:abc"]},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(EventJudgementValidationError):
                    validate_reflection_output({**body, "prior_view": broken}, context)

    @staticmethod
    def reflection() -> dict[str, Any]:
        return {
            "thesis_refs": ["thesis:1"],
            "what_we_expected": "e", "what_happened": "h", "why": "w",
            "citations": ["event:1"], "missed_debates": [],
            "followup_tracking": [], "followup_research": [],
            "market_view_vs_ours": {"available": False, "our_direction": "flat",
                                    "summary": "nothing held", "refs": []},
            "convergence_pathway": "p",
        }

    @staticmethod
    def debate() -> dict[str, Any]:
        position = {"statement": "s", "claim_refs": ["claim-version:1"]}
        return {
            "debate_ref": "debate:1", "question": "q?",
            "driver_refs": ["driver:d"], "admission_index": 0,
            "causal_link_index": 0,
            "bull_position": dict(position), "bear_position": dict(position),
            "market_position": {"available": False, "lean": None, "statement": None,
                                "refs": []},
            "our_position": {"state": "none_yet", "side": None, "statement": None,
                             "refs": []},
            "status": "open", "last_shift_reason": None,
            "first_seen_at": "2026-09-10T00:00:00+00:00",
            "source_independence": {"bull_sources": 1, "bear_sources": 1},
        }


class CoordinatorTests(unittest.TestCase):
    """The tick, against the real mission authority and a real runner.

    ``coverage_mission.DISCOVERY_SOURCES`` does not carry this feed's row yet
    -- the branch that added the feed may not touch that file -- so the row is
    patched in exactly as S1 and S2 do, which also pins what the integrator
    will install.
    """

    def setUp(self) -> None:
        from dalton_core.connector import ConnectorStore
        from dalton_core.connector_governance import ConnectorGovernance
        from dalton_core.mission_feed_lane import FEED_DISCOVERY_SOURCES
        from dalton_core.observability import ObservabilityStore
        from dalton_core.prior_research_launcher import PriorResearchFeedLauncher
        from dalton_core.raw_spool import RawSpool

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        # Inside one fourteen-day enumeration window: every window is a
        # governed child process, so a fixture dated two years back would make
        # this test a hundred subprocesses long.
        self.as_of = (date.today() - timedelta(days=3)).isoformat()
        self.since = (date.today() - timedelta(days=6)).isoformat()
        self.corpus = build_corpus(self.root / "corpus", as_of=self.as_of)
        repo = Path(__file__).resolve().parents[1]
        patch_path = mock.patch.dict("os.environ", {"PYTHONPATH": str(repo / "src")})
        patch_path.start()
        self.addCleanup(patch_path.stop)

        self.core = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(self.core.close)
        self.connectors = ConnectorStore(self.core)
        self.observability = ObservabilityStore(self.core)
        self.spool = RawSpool(str(self.root / "spool"), max_total_bytes=1_000_000_000)
        self.bootstrap = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        patch = mock.patch.object(
            coverage_mission_module, "DISCOVERY_SOURCES",
            MappingProxyType({
                **dict(coverage_mission_module.DISCOVERY_SOURCES),
                **dict(FEED_DISCOVERY_SOURCES),
            }),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.mission = self.publish_mission()
        self.governance = {
            operation: self.write_governance(kind)
            for operation, kind in (("list_documents", PRIOR_RESEARCH_LIST_KIND),
                                    ("get_document", PRIOR_RESEARCH_GET_KIND))
        }
        self.launcher = PriorResearchFeedLauncher(
            corpus_root=self.corpus, state_dir=self.state,
            governance_paths=self.governance, spool_dir=self.root / "spool",
        )
        self.addCleanup(self.launcher.close)
        self.ConnectorGovernance = ConnectorGovernance

    def write_governance(self, kind: str) -> Path:
        record = build_governance_record(kind, approved_by=OWNER, status="approved")
        path = self.root / f"{kind}-approved.json"
        path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        return path

    def publish_mission(self) -> dict[str, Any]:
        params = mission_params(self.bootstrap)
        params["source_plan"] = list(params["source_plan"]) + [{
            "source_ref": PRIOR_RESEARCH,
            "role": "this fund's own earlier work on these companies",
            "status": "connected",
        }]
        params["autonomy"]["may_write"] = sorted(
            set(params["autonomy"]["may_write"]) | {"source_discovery", "observation"}
        )
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def coordinator(self) -> Any:
        from dalton_core.mission_feed_lane import (
            FeedDiscoveryCoordinator,
            build_feed_runner,
            load_feed_discovery_plan,
        )

        runners = {
            name: build_feed_runner(
                launcher=self.launcher, operation=operation,
                governance=self.ConnectorGovernance.load(self.governance[operation]),
                store=self.core, connectors=self.connectors,
                observability=self.observability, spool=self.spool,
                source_ref=PRIOR_RESEARCH,
            )
            for name, operation in (("enumerator", "list_documents"),
                                    ("runner", "get_document"))
        }
        repo = Path(__file__).resolve().parents[1]
        return FeedDiscoveryCoordinator(
            missions=self.missions, launcher=self.launcher,
            source_ref=PRIOR_RESEARCH,
            plan=load_feed_discovery_plan(
                repo / "deploy/phase9/p9-us-it-services-feeds-v2.json"
            ),
            **runners,
        )

    def test_a_tick_reads_a_prior_document_into_an_internal_prior_discovery(self) -> None:
        result = self.coordinator().dispatch_once(
            universe=[{"company_ref": ACN, "ticker": "ACN"}], since=self.since
        )
        self.assertEqual(result["status"], "dispatched", result)
        read = result["read"]
        self.assertEqual(read["read"], 1)
        self.assertEqual(read["company"], 1)
        outcome = read["outcomes"][0]
        self.assertEqual(outcome["outcome"], "company")
        self.assertEqual(outcome["document_ref"],
                         document_ref("ACN", "2024/screen.md"))

        queued = self.missions.discovered_documents(self.mission["id"], limit=50)
        self.assertEqual({row["document_ref"] for row in queued},
                         {document_ref("ACN", "2024/screen.md")})
        self.assertEqual({row["status"] for row in queued}, {"acquired"})

        specs = {
            row["spec_ref"] for row in self.core.connection.execute(
                "SELECT spec_ref FROM coverage_mission_source_discoveries"
            ).fetchall()
        }
        self.assertEqual(specs, {"prior-research"})
        # The whole point of the spec: it is the key the claim index reads the
        # tier off, and it has to be a key that table actually holds.
        self.assertEqual(SPEC_IMPORTANCE["prior-research"], "internal_prior")

        # Second tick: the same document is held, not discovered again.
        again = self.coordinator().dispatch_once(
            universe=[{"company_ref": ACN, "ticker": "ACN"}], since=self.since
        )
        self.assertEqual(again["read"]["read"], 0)
        self.assertEqual(again["read"]["already_held"], 1)

    def test_a_folder_outside_the_universe_is_dropped_not_attributed(self) -> None:
        build_corpus(self.corpus, company="ZZZZ", as_of=self.as_of)
        result = self.coordinator().dispatch_once(
            universe=[{"company_ref": ACN, "ticker": "ACN"}], since=self.since
        )
        outcomes = {item["document_ref"]: item for item in result["read"]["outcomes"]}
        stray = outcomes[document_ref("ZZZZ", "2024/screen.md")]
        self.assertEqual(stray["outcome"], "dropped")
        self.assertEqual(stray["records"], [])

    def test_the_triage_reads_a_screen_before_a_memo(self) -> None:
        from dalton_core.mission_feed_lane import (
            attribute_prior_documents,
            triage_prior_documents,
        )

        documents = [
            {"document_id": "d:notes", "company": "ZZZZ", "kind": "notes"},
            {"document_id": "d:screen", "company": "ZZZZ", "kind": "initial_screen"},
            {"document_id": "d:memo", "company": "ZZZZ", "kind": "memo"},
            {"document_id": "d:acn", "company": "ACN", "kind": "memo"},
        ]
        triaged = triage_prior_documents(documents, [{"company_ref": ACN, "ticker": "ACN"}],
                                         {"industry_keywords": [], "peer_names": [],
                                          "companies": {}})
        self.assertEqual(triaged["read_queue"],
                         ["d:acn", "d:screen", "d:memo", "d:notes"])
        self.assertEqual(triaged["header_company"], {ACN: ["d:acn"]})
        attributed = attribute_prior_documents(
            documents, [{"company_ref": ACN, "ticker": "ACN"}]
        )
        self.assertEqual(attributed["by_company"], {ACN: ["d:acn"]})
        self.assertEqual(attributed["unattributed"],
                         ["d:memo", "d:notes", "d:screen"])


class LaneTests(unittest.TestCase):
    def test_the_lane_is_registered_at_order_thirty_two(self) -> None:
        from dalton_core.lane_registry import LANE_MODULES, registered_lanes

        self.assertIn("dalton_core.mission_prior_research_lane", LANE_MODULES)
        spec = next(item for item in registered_lanes()
                    if item.operation == "dispatch_prior_research")
        self.assertEqual(spec.order, 32)
        self.assertEqual(spec.driver_key, "prior_research")
        self.assertTrue(all(other.order != 32 for other in registered_lanes()
                            if other.operation != "dispatch_prior_research"))

    def test_the_grants_it_needs_are_words_a_mission_can_hold(self) -> None:
        from dalton_core.mission_prior_research_lane import (
            IMPORT_WRITE_SCOPE,
            WRITE_SCOPES,
            may_import_prior_screen,
            may_read_prior_research,
        )

        for word in (*WRITE_SCOPES, IMPORT_WRITE_SCOPE):
            with self.subTest(word=word):
                self.assertIn(word, AUTOMATION_WRITE_SCOPES)
        reading = {"autonomy": {"may_write": list(WRITE_SCOPES)}}
        self.assertTrue(may_read_prior_research(reading))
        self.assertFalse(may_import_prior_screen(reading))
        importing = {"autonomy": {"may_write": [*WRITE_SCOPES, IMPORT_WRITE_SCOPE]}}
        self.assertTrue(may_import_prior_screen(importing))
        self.assertFalse(may_read_prior_research({"autonomy": {"may_write": []}}))

    def test_it_is_in_the_budget_pool_and_the_cockpit_label_map(self) -> None:
        from dalton_core.budget_pools import LANE_POOLS
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        self.assertEqual(LANE_POOLS["dispatch_prior_research"], "coverage")
        self.assertIn("prior_research", REGISTRY_LANE_LABELS)

    def test_the_schema_is_bootstrapped_and_rehearsed(self) -> None:
        from scripts.rehearse_deploy import CORE_MIGRATIONS, INSTALL_SEEDS

        from dalton_core.bootstrap import SCHEMA_DATABASES

        self.assertIn("prior_model_schema.sql", dict(SCHEMA_DATABASES))
        self.assertIn("prior_model_schema.sql",
                      {item.schema for item in CORE_MIGRATIONS})
        gated = {
            spec.repo: spec.gate for spec in INSTALL_SEEDS
            if "prior-research" in spec.repo
        }
        self.assertEqual(set(gated.values()), {"prior-research"})
        self.assertEqual(len(gated), 2)

    def test_install_seeds_the_lane_all_or_nothing(self) -> None:
        script = (Path(__file__).resolve().parents[1]
                  / "deploy/macos/install.sh").read_text(encoding="utf-8")
        block = script.split("prior_research_dir=", 1)[1].split("# S3 / INT2", 1)[0]
        for name in ("prior-research-list-documents", "prior-research-get-document",
                     '"$feeds_dir/prior-research"'):
            with self.subTest(name=name):
                self.assertIn(name, block)
        self.assertIn("seed_feed_plan", block)
        self.assertIn("DALTON_PRIOR_RESEARCH_DIR", script)

    def test_the_lane_refuses_until_the_authority_knows_the_source(self) -> None:
        from dalton_core.mission_prior_research_lane import LAUNCHER_KWARG, dispatch

        class Server:
            lane_state: dict[str, Any] = {}

            def lane_launcher(self, kwarg: str) -> Any:
                assert kwarg == LAUNCHER_KWARG
                return object()

        outcome = dispatch(Server(), {})
        self.assertEqual(outcome["status"], "unconfigured")
        if PRIOR_RESEARCH not in coverage_mission_module.DISCOVERY_SOURCES:
            self.assertIn("DISCOVERY_SOURCES", outcome["reason"])

    def test_the_feed_plan_names_this_source(self) -> None:
        from dalton_core.mission_feed_lane import load_feed_discovery_plan

        repo = Path(__file__).resolve().parents[1]
        plan = load_feed_discovery_plan(
            repo / "deploy/phase9/p9-us-it-services-feeds-v2.json"
        )
        self.assertIn(PRIOR_RESEARCH, plan["source_refs"])


if __name__ == "__main__":
    unittest.main()
