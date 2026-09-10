"""P10c: the mission writes its own Initial Screen, and cannot say an unsourced number."""

from __future__ import annotations

import json
import unittest

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.initial_screen import (
    assess_exit_gate,
    build_claim_context,
    build_section_prompt,
    parse_section_output,
    section_titles,
)
from dalton_core.mission_deliverable import (
    CELL_SOURCE_KINDS,
    MissionDeliverableAuthority,
    MissionDeliverableConflict,
    MissionDeliverableValidationError,
    unsourced_numbers,
    validate_cell_citation,
    value_tokens,
)
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:lumos"


class DeliverableHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        self.params = mission_params(self.state)
        ref = self.params.pop("mission_ref")
        self.mission_ref = ref
        self.mission = self.missions.create_mission(ref, **self.params)
        self.authority = MissionDeliverableAuthority(self.store)
        self.playbook = self.state["playbook"]
        self._seq = 0

    def grant(self, *scopes: str) -> dict:
        params = dict(self.params)
        params["autonomy"] = {**params["autonomy"],
                              "may_write": list(params["autonomy"]["may_write"]) + list(scopes)}
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.mission = self.missions.create_mission(self.mission_ref, **params)
        return self.mission

    def claim(self, *, subject: str = ACN, statement: str = "管理层说需求在改善。", value=None) -> str:
        self._seq += 1
        n = self._seq
        claim = {
            "schema_version": "0.2", "id": f"claim-version:{n:064d}", "claim_ref": f"claim:test:{n}",
            "version": 1, "subject_ref": subject, "metric_or_aspect": "aspect:test", "period": "2026Q2",
            "basis": "fixture", "normalized_statement": statement,
            "claim_kind": "quantitative" if value is not None else "qualitative", "value": value,
            "unit": "USD" if value is not None else None, "currency": None, "scale": None,
            "producer_execution_refs": [], "semantic_review_ref": None, "semantic_review_hash": None,
            "candidate_origin_ref": None, "candidate_origin_hash": None,
            "actor_ref": "system:research-auto-commit", "prior_version_ref": None,
            "created_at": f"2026-09-0{1 + n % 9}T00:00:00+00:00",
        }
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1, json.dumps(claim, sort_keys=True),
                 claim["content_hash"], claim["created_at"]),
            )
        return claim["id"]

    def publish(self, sections, *, actor=AUTOMATION, subject=ACN, summary="摘要。"):
        return self.authority.publish(
            kind="initial_screen", subject_ref=subject,
            mission=self.missions.active_mission(self.mission_ref), playbook=self.playbook,
            template_ref="playbook:deliverable_templates.initial_screen",
            sections=sections, summary=summary, actor_ref=actor,
        )


class NumberDisciplineTests(unittest.TestCase):
    def test_a_period_label_is_not_a_figure(self) -> None:
        self.assertEqual(value_tokens("2026 财年收入创新高"), [])
        self.assertEqual(value_tokens("Q2 FY2026 的势头延续"), [])
        self.assertEqual(value_tokens("收入增长 5.6%"), ["5.6%"])
        self.assertEqual(value_tokens("目标价 $312.50"), ["$312.50"])

    def test_iso_dates_ranges_and_thresholds_are_not_figures(self) -> None:
        """All three read as figures live and emptied most of the first document."""

        self.assertEqual(value_tokens("期间 2026-03-01..2026-05-31 的收入"), [])
        self.assertEqual(value_tokens("Q3 FY26 新签下滑"), [])
        self.assertEqual(value_tokens("book-to-bill 跌破 1 并延续"), [])
        self.assertEqual(value_tokens("覆盖 5 家公司"), [])
        # A measurement is still a measurement.
        self.assertEqual(value_tokens("利润率 14.2%"), ["14.2%"])
        self.assertEqual(value_tokens("新增 250 个席位"), ["250"])
        self.assertEqual(value_tokens("citation C7 和 N1 不是数字"), [])

    def test_only_figures_a_cited_claim_carries_are_allowed(self) -> None:
        numbers = [{"text": "Accenture reported Revenues of USD 18,718,144,000 for 2026Q3, up 5.59%",
                    "claim_version_ref": "claim-version:x"}]
        self.assertEqual(unsourced_numbers("收入 18,718,144,000 美元，同比 5.59%。", numbers), [])
        self.assertEqual(unsourced_numbers("2026 财年利润率约 14.2%。", numbers), ["14.2%"])


class AuthorityTests(DeliverableHarness):
    def test_an_unsourced_figure_fails_the_publish(self) -> None:
        self.grant("deliverable")
        ref = self.claim(statement="Accenture reported Revenues of USD 18,718,144,000 for 2026Q3.", value=1.87e10)
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.publish([{"title": "S1", "body": "利润率提升到 14.2%。", "claim_refs": [], "numbers": [], "gaps": []}])
        self.assertIn("14.2%", str(caught.exception))
        self.assertIn("缺来源", str(caught.exception))
        # The same figure is fine once the Claim that carries it is cited.
        record = self.publish([{
            "title": "S1", "body": "收入 18,718,144,000 美元。", "claim_refs": [ref],
            "numbers": [{"text": "Accenture reported Revenues of USD 18,718,144,000 for 2026Q3.",
                         "claim_version_ref": ref}], "gaps": [],
        }])
        self.assertEqual((record["status"], record["version"]), ("fresh", 1))

    def test_a_retired_or_unknown_claim_cannot_be_cited(self) -> None:
        self.grant("deliverable")
        ref = self.claim()
        with self.assertRaises(MissionDeliverableConflict):
            self.publish([{"title": "S1", "body": "文本。", "claim_refs": ["claim-version:missing"],
                           "numbers": [], "gaps": []}])
        from dalton_core.claim_retirement import ClaimRetirementAuthority

        retirements = ClaimRetirementAuthority(self.store)
        challenge = retirements.challenge(
            claim_version_ref=ref, claim_version_hash=json.loads(self.store.connection.execute(
                "SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (ref,)
            ).fetchone()["claim_json"])["content_hash"],
            reason_code="human_judgment", rationale="wrong", actor_ref=OWNER)
        retirements.decide(challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                           decision="retired", actor_ref=OWNER, rationale="retire it")
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.publish([{"title": "S1", "body": "文本。", "claim_refs": [ref], "numbers": [], "gaps": []}])
        self.assertIn("retired", str(caught.exception))

    def test_automation_needs_the_grant_and_a_document_needs_a_written_section(self) -> None:
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.publish([{"title": "S1", "body": "文本。", "claim_refs": [], "numbers": [], "gaps": []}])
        self.assertIn("deliverable", str(caught.exception))
        # A person may always publish; an all-empty document is still refused.
        with self.assertRaises(MissionDeliverableConflict):
            self.publish([{"title": "S6 估值", "body": "", "claim_refs": [], "numbers": [],
                           "gaps": ["市场数据未接入"]}], actor=OWNER)
        record = self.publish([{"title": "S1", "body": "文本。", "claim_refs": [], "numbers": [], "gaps": []}],
                              actor=OWNER)
        self.assertEqual(record["actor_ref"], OWNER)

    def test_versions_chain_and_an_identical_document_is_a_duplicate(self) -> None:
        self.grant("deliverable")
        sections = [{"title": "S1", "body": "第一版。", "claim_refs": [], "numbers": [], "gaps": []}]
        first = self.publish(sections)
        again = self.publish(sections)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["version"], 1)
        second = self.publish([{"title": "S1", "body": "第二版，多写了一句。", "claim_refs": [],
                                "numbers": [], "gaps": []}])
        self.assertEqual((second["status"], second["version"], second["prior_version_ref"]),
                         ("fresh", 2, first["id"]))
        latest = self.authority.latest(first["deliverable_ref"])
        self.assertEqual(latest["id"], second["id"])
        self.assertEqual([d["subject_ref"] for d in self.authority.deliverables(self.mission["id"])], [ACN])
        # The document binds the authorities it was written under.
        self.assertEqual(latest["mission_version_hash"], self.mission["content_hash"])
        self.assertEqual(latest["playbook_version_hash"], self.playbook["content_hash"])

    def test_a_subject_outside_the_universe_is_refused(self) -> None:
        self.grant("deliverable")
        with self.assertRaises(MissionDeliverableConflict):
            self.publish([{"title": "S1", "body": "x", "claim_refs": [], "numbers": [], "gaps": []}],
                         subject="company:sec-cik:9999999999")
        with self.assertRaises(MissionDeliverableValidationError):
            self.authority.publish(
                kind="not_a_kind", subject_ref=ACN, mission=self.mission, playbook=self.playbook,
                template_ref="t", sections=[], summary="s", actor_ref=OWNER)


class DraftingTests(DeliverableHarness):
    def test_the_context_tags_claims_and_figures_and_the_prompt_carries_both(self) -> None:
        qualitative = self.claim(statement="管理层说需求在改善。")
        figure = self.claim(statement="Accenture reported Revenues of USD 18,718,144,000 for 2026Q3.", value=1.87e10)
        claims = [
            {"ref": qualitative, "statement": "管理层说需求在改善。", "period": "2026Q2", "value": None,
             "aspect": "aspect:demand", "created_at": "2026-09-01"},
            {"ref": figure, "statement": "Accenture reported Revenues of USD 18,718,144,000 for 2026Q3.",
             "period": "2026Q3", "value": 1.87e10, "aspect": "metric:revenue", "created_at": "2026-09-02"},
        ]
        context = build_claim_context(claims)
        self.assertEqual([c["tag"] for c in context["claims"]], ["C1"])
        self.assertEqual([n["tag"] for n in context["numbers"]], ["N1"])
        self.assertIn("18,718,144,000", context["numbers"][0]["figures"][0].replace("USD ", ""))
        prompt = build_section_prompt(
            title="S1 公司概览", guidance="写公司概览", company={"ticker": "ACN", "company_ref": ACN},
            mission=self.mission, context=context,
            checklist=[{"label": "最新年报（10-K）正文", "status": "not_planned"}],
        )
        self.assertIn("C1", prompt)
        self.assertIn("N1", prompt)
        self.assertIn("最新年报", prompt)
        self.assertIn("缺来源", prompt)

    def test_an_invented_tag_is_dropped_and_reported(self) -> None:
        context = {"claims": [{"tag": "C1", "ref": "claim-version:1", "statement": "s", "period": "p"}],
                   "numbers": [{"tag": "N1", "ref": "claim-version:2", "statement": "USD 5", "period": "p"}]}
        section = parse_section_output(
            '{"body":"正文。","claims":["C1","C9"],"numbers":["N1","N7"],"gaps":[]}',
            context=context, title="S1")
        self.assertEqual(section["claim_refs"], ["claim-version:1"])
        self.assertEqual([n["claim_version_ref"] for n in section["numbers"]], ["claim-version:2"])
        self.assertTrue(any("C9" in gap for gap in section["gaps"]))
        empty = parse_section_output("not json at all", context=context, title="S1")
        self.assertEqual((empty["body"], empty["claim_refs"]), ("", []))

    def test_the_gate_reads_the_source_base_and_what_was_actually_written(self) -> None:
        titles = section_titles(self.playbook)
        self.assertEqual(len(titles), 8)
        thin = [{"title": title, "body": "", "claim_refs": [], "numbers": [], "gaps": []} for title in titles]
        incomplete = {"items": [{"label": "过去 4 个季度的电话会纪要", "status": "missing"},
                                {"label": "最新年报（10-K）正文", "status": "not_planned"}]}
        gate = assess_exit_gate(playbook=self.playbook, checklist_entry=incomplete, sections=thin)
        self.assertFalse(gate["passed"])
        self.assertIn("电话会纪要", gate["rationale"])
        self.assertEqual([answer["check"] for answer in gate["answers"]],
                         ["source_base", "number_provenance", "key_driver", "street_and_risk"])
        self.assertTrue(all(answer["question"] for answer in gate["answers"]))
        # A full source base and three written sections pass every check.
        full = [dict(section) for section in thin]
        for index in (3, 4, 5):
            full[index] = {**full[index], "body": "写满这一节。" * 40,
                           "claim_refs": ["a", "b", "c"]}
        passing = assess_exit_gate(
            playbook=self.playbook,
            checklist_entry={"items": [{"label": "x", "status": "complete"}]}, sections=full)
        self.assertTrue(passing["passed"])
        self.assertIn("四问全部为是", passing["rationale"])


class LaneTests(DeliverableHarness):
    def test_the_child_holds_before_spending_when_the_mission_does_not_grant_it(self) -> None:
        import argparse
        import tempfile
        from pathlib import Path as _Path

        from dalton_core import initial_screen_cli

        self.claim()
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = _Path(temp.name)
        # The child reads a state directory; give it this Core under that name.
        self.store.connection.commit()
        backup = root / "core.sqlite"
        import sqlite3

        destination = sqlite3.connect(str(backup))
        self.store.connection.backup(destination)
        destination.close()
        args = argparse.Namespace(
            state_dir=str(root), model_config=None, summary_dir=str(root),
            scheduler_db=None, timeout_seconds=30, dry_run=True, quiet=True,
        )
        summary = initial_screen_cli.run(args)
        self.assertEqual(summary["status"], "held")
        self.assertIn("deliverable", summary["failure_reason"])
        self.assertEqual(summary["sections"], [])


class ComputedCellCitationTests(DeliverableHarness):
    """P12e / P14f: a figure may cite a computed cell instead of a Claim.

    Two layers now produce figures that are arithmetic over things the Core
    already holds -- the industry framework's comparison cells and the
    forecast model's cells -- and neither has a Claim behind it. Minting one
    would be manufacturing a quotation for a calculation. What does not change
    is the rule that matters: a figure this Core cannot re-derive is refused.
    """

    ACCESSION = "0001467373-26-000032"

    def filing(self, accession: str | None = None) -> str:
        """One held 10-Q, through the authority that owns the table."""

        accession = accession or self.ACCESSION
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref=AUTOMATION,
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(
            authorization=authorization, attempt=0)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "0" * 24)
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": accession, "form": "10-Q", "filed": "2026-07-01",
                    "report_date": "2026-05-31",
                    "lines": [{
                        "statement": "income", "concept": "us-gaap:Revenues",
                        "label": "Revenues", "level": 0, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-03-01",
                        "period_end": "2026-05-31", "value": "18718144000",
                        "unit": "USD", "balance": "credit",
                    }],
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")
        return accession

    def cell_number(self, accession: str, *, text="ACN 2026Q2 revenue 18718144000"):
        return {"text": text,
                "cell": {"kind": "statement_accession",
                         "ref": "comparison-cell:acn:revenue:2026Q2",
                         "accession": accession},
                "period": "2026Q2"}

    def test_the_shape_is_closed_per_kind(self) -> None:
        self.assertEqual(CELL_SOURCE_KINDS,
                         ("statement_accession", "forecast_cell"))
        with self.assertRaises(MissionDeliverableValidationError):
            validate_cell_citation({"kind": "vibes", "ref": "r"})
        with self.assertRaises(MissionDeliverableValidationError):
            # a statement cell carrying a forecast's field
            validate_cell_citation({"kind": "statement_accession", "ref": "r",
                                    "version_ref": "v"})
        self.assertEqual(
            validate_cell_citation({"kind": "forecast_cell", "ref": "r@2026-06-30:estimate",
                                    "version_ref": "forecast-model-version:x"})["kind"],
            "forecast_cell")

    def test_a_figure_backed_by_a_held_filing_publishes(self) -> None:
        self.grant("deliverable")
        accession = self.filing()
        record = self.publish([{
            "title": "S1", "body": "本季收入 18718144000。", "claim_refs": [],
            "numbers": [self.cell_number(accession)], "gaps": [],
        }])
        entry = record["sections"][0]["numbers"][0]
        self.assertEqual(entry["cell"]["accession"], accession)
        self.assertNotIn("claim_version_ref", entry)

    def test_a_dangling_cell_ref_is_refused(self) -> None:
        self.grant("deliverable")
        with self.assertRaises(MissionDeliverableConflict) as caught:
            self.publish([{
                "title": "S1", "body": "本季收入 18718144000。", "claim_refs": [],
                "numbers": [self.cell_number("0000000000-00-000000")], "gaps": [],
            }])
        self.assertIn("does not resolve", str(caught.exception))

    def test_a_dangling_forecast_cell_is_refused(self) -> None:
        self.grant("deliverable")
        with self.assertRaises(MissionDeliverableConflict):
            self.publish([{
                "title": "S1", "body": "预测收入 19000000000。", "claim_refs": [],
                "numbers": [{
                    "text": "revenue 2026-12-31 estimate 19000000000",
                    "cell": {"kind": "forecast_cell",
                             "ref": "result:revenue@2026-12-31:estimate",
                             "version_ref": "forecast-model-version:nope"},
                    "period": "2026Q4"}],
                "gaps": [],
            }])

    def test_a_number_citing_both_a_claim_and_a_cell_is_refused(self) -> None:
        self.grant("deliverable")
        accession = self.filing()
        ref = self.claim(statement="Accenture reported Revenues of 18718144000.",
                         value=1.87e10)
        with self.assertRaises(MissionDeliverableValidationError) as caught:
            self.publish([{
                "title": "S1", "body": "本季收入 18718144000。", "claim_refs": [],
                "numbers": [{**self.cell_number(accession), "claim_version_ref": ref}],
                "gaps": [],
            }])
        self.assertIn("exactly one", str(caught.exception))

    def test_a_number_citing_neither_is_still_refused(self) -> None:
        self.grant("deliverable")
        with self.assertRaises(MissionDeliverableValidationError):
            self.publish([{
                "title": "S1", "body": "本季收入 18718144000。", "claim_refs": [],
                "numbers": [{"text": "revenue 18718144000", "period": "2026Q2"}],
                "gaps": [],
            }])

    def test_a_resolvable_cell_makes_its_figure_sourced(self) -> None:
        numbers = [self.cell_number("x", text="ACN 2026Q2 revenue 18718144000")]
        self.assertEqual(unsourced_numbers("本季收入 18718144000。", numbers), [])
        self.assertEqual(unsourced_numbers("利润率 14.2%。", numbers), ["14.2%"])


class LauncherTests(unittest.TestCase):
    def test_a_ticket_left_running_by_a_restart_is_adopted_not_stuck(self) -> None:
        """Live, a ticket stuck at "running" locked the lane out for good."""

        import json as _json
        import tempfile
        from pathlib import Path as _Path

        from dalton_core.initial_screen_launcher import InitialScreenLauncher

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = _Path(temp.name)
        config = root / "model.json"; config.write_text("{}", encoding="utf-8")
        launcher = InitialScreenLauncher(state_dir=root, model_config_path=config)
        ticket_dir = launcher.tickets_dir / ("a" * 24)
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "ticket.json").write_text(_json.dumps({
            "schema_version": "0.1", "id": "initial-screen:" + "a" * 24, "status": "running",
            "pid": 999_999, "started_at": "2026-09-07T00:00:00+00:00", "exit_code": None,
            "completed_at": None,
        }), encoding="utf-8")
        (ticket_dir / "summary.json").write_text(_json.dumps({
            "status": "succeeded", "drafted": {"ticker": "ACN"},
        }), encoding="utf-8")
        record = launcher.status("initial-screen:" + "a" * 24)
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(record["adopted_from_summary"])
        # Without a usable summary the ticket settles as orphaned, not running.
        other = launcher.tickets_dir / ("b" * 24)
        other.mkdir(parents=True)
        (other / "ticket.json").write_text(_json.dumps({
            "schema_version": "0.1", "id": "initial-screen:" + "b" * 24, "status": "running",
            "pid": 999_998, "started_at": "2026-09-07T00:00:00+00:00", "exit_code": None,
            "completed_at": None,
        }), encoding="utf-8")
        self.assertEqual(launcher.status("initial-screen:" + "b" * 24)["status"], "orphaned")

    def test_a_real_claim_statement_fits_in_a_cited_figure(self) -> None:
        """P13ac: the bound was narrower than what the system produces.

        A number's text is the Claim's own normalized_statement, and the Claim
        contract puts no ceiling on that. The SEC lane writes 205-character
        statements, so every EPAM figure was refused and the Initial Screen
        could not be published at all -- permanently, since Claims are
        append-only and cannot be shortened afterwards.
        """

        from dalton_core.mission_deliverable import (
            MAX_NUMBER_TEXT,
            MissionDeliverableValidationError,
            validate_section,
        )

        statement = (
            "EPAM SYSTEMS, INC. reported Revenue from Contract with Customer, "
            "Excluding Assessed Tax of 1,459,000,000 USD for the quarter ended "
            "2025-06-30, an increase of 18.04 percent against the same quarter "
            "of the prior year as reported in the same filing."
        )
        self.assertGreater(len(statement), 200)
        section = validate_section({
            "title": "S1", "body": "Revenue grew 18.04 percent.",
            "numbers": [{"text": statement,
                         "claim_version_ref": "claim-version:x", "period": "2025Q2"}],
        })
        self.assertEqual(section["numbers"][0]["text"], statement)
        # Still bounded: one statement may not become a document.
        with self.assertRaises(MissionDeliverableValidationError):
            validate_section({
                "title": "S1", "body": "b",
                "numbers": [{"text": "x" * (MAX_NUMBER_TEXT + 1),
                             "claim_version_ref": "claim-version:x"}],
            })

    def test_a_long_statement_still_sources_the_figures_in_the_body(self) -> None:
        # Eliding instead of widening would have dropped the figure, because
        # these statements put it last, and then reported the body citing it as
        # unsourced.
        from dalton_core.mission_deliverable import unsourced_numbers

        statement = "EPAM " + ("very long preamble " * 12) + "revenue was 18.04 percent"
        self.assertGreater(len(statement), 200)
        self.assertEqual(
            unsourced_numbers("Revenue grew 18.04 percent.",
                              [{"text": statement, "claim_version_ref": "claim-version:x"}]),
            [],
        )

    def test_a_hold_says_whether_the_last_run_broke_or_had_nothing_to_write(self) -> None:
        """P13aa: those are different facts and read as the same one.

        Live, eight Initial Screen sections had been failing on a scheduler
        conflict for two days while the hold reported that the ledger simply
        had not moved -- which sends the reader to the ledger, where nothing is
        wrong. The hold itself is right either way; the reason has to say which
        case it is.
        """

        import json as _json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path as _Path

        from dalton_core.initial_screen_launcher import (
            InitialScreenCoordinator,
            InitialScreenLauncher,
        )

        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = _Path(temp.name)
        config = root / "model.json"; config.write_text("{}", encoding="utf-8")
        launcher = InitialScreenLauncher(state_dir=root, model_config_path=config)
        now = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)

        def ticket(name: str, summary_status: str) -> str:
            ref = "initial-screen:" + name * 24
            directory = launcher.tickets_dir / (name * 24)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "ticket.json").write_text(_json.dumps({
                "schema_version": "0.1", "id": ref, "status": summary_status,
                "pid": 1, "started_at": "2026-09-09T09:00:00+00:00",
                "exit_code": 0, "completed_at": "2026-09-09T09:01:00+00:00",
            }), encoding="utf-8")
            (directory / "summary.json").write_text(_json.dumps({
                "status": summary_status, "drafted": {"ticker": "EPAM"},
                "failure_reason": None if summary_status == "succeeded" else "没有任何一节写出来",
            }), encoding="utf-8")
            return ref

        class _Store:
            def __init__(self, connection): self.connection = connection

        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        coordinator = InitialScreenCoordinator(
            store=_Store(connection), launcher=launcher, clock=lambda: now)
        signature = coordinator._signature()
        for name, status, expected in (
            ("c", "failed", "上一轮跑失败了"),
            ("d", "succeeded", "账本也没有变化"),
        ):
            ref = ticket(name, status)
            (root / "initial-screens").mkdir(exist_ok=True)
            coordinator._latest_path().write_text(_json.dumps({
                "ticket_ref": ref, "started_at": "2026-09-09T09:00:00+00:00",
                "idle_signature": signature, "idle_at": "2026-09-09T09:30:00+00:00",
            }), encoding="utf-8")
            result = coordinator.dispatch_once()
            self.assertEqual(result["status"], "held", result)
            self.assertIn(expected, result["reason"])


if __name__ == "__main__":
    unittest.main()
