"""Directed-document draft contract 0.2: a promoted statement stands on its own.

Live 2026-09-29/30 (legacy c7556cb9), asked "CTSH 档案「供给与成本」部分引用的
15.8%，在管理层口径里对应哪个指标？", the draft -- told under 0.1 to keep every
quantity out of the statement -- wrote "管理层在 FY2025 业绩电话会上说的这个数字，
是公司全年的调整后营业利润率", and it was promoted.  "这个数字" points at a
question the Ledger does not carry.  These tests pin:

* the rule: a statement must carry a figure its question names (by significant
  digits) and must not point back at the question without the value;
* the draft contract: 0.2 asks for it and refuses a hollow draft as a contract
  failure (bounded retry, never staged); 0.1 is unchanged and stored 0.1 Work
  replays byte for byte and stays 0.1;
* the Ledger gate: the question's own figure is the one number a 0.2 statement
  may carry; any other is still refused;
* the owner replay that lists hollow promoted claims.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.annual_report_qualitative import AnnualReportQualitativeError
from dalton_core.document_inquiry_equivalence import (
    figure_spellings, question_figures, strip_figures, text_figures,
)
from dalton_core.document_research_qualitative import (
    DRAFT_OUTPUT_SCHEMA, deictic_reference, draft_prompt, draft_prompt_v2,
    parse_directed_draft_text, restated_question_figures, self_contained_rejection,
)
from dalton_core.research_auto_commit import document_qualitative_content_rejection
from dalton_core.research_verification import MISSION_DOCUMENT_AUTHORITY_MODE
from dalton_core.store import canonical_json, content_hash

CTSH_QUESTION = "CTSH 档案「供给与成本」部分引用的 15.8%，在管理层口径里对应哪个指标？"
CTSH_HOLLOW = ("管理层在 FY2025 业绩电话会上说的这个数字，是公司全年的调整后营业利润率"
               "（adjusted operating margin，非 GAAP 口径）。这是公司自己的说法，管理层称它"
               "超过了指引，且比上年有所提升。")
CTSH_RESTATED = ("Cognizant 管理层在 FY2025 业绩电话会上所说的 15.8%，是公司全年的调整后营业"
                 "利润率（adjusted operating margin，非 GAAP 口径），管理层称它超过了指引。")
IBM_QUESTION = ("Does the unsupported calendar number '18' refer to the May 18–20, 2026 TMC "
                "conference, and does the source establish an IBM-specific appearance rather "
                "than a future catalyst?")
IBM_HOLLOW = ("The calendar number refers to the TMC conference scheduled for May in Boston, "
              "and the source schedules an IBM fireside chat at that conference.")


def _draft(statement, *, status="answered"):
    return {
        "schema_version": "0.1", "status": status, "answer": statement,
        "candidate": None if status != "answered" else {
            "normalized_statement": statement, "metric_or_aspect": "adjusted operating margin",
            "period": "FY2025", "basis": "earnings call", "cited_match_indexes": [0]},
        "missing": [] if status == "answered" else ["the call transcript"],
    }


class FigureTests(unittest.TestCase):
    def test_figures_in_running_text_by_significant_digits(self):
        self.assertEqual(text_figures(CTSH_QUESTION), {"158"})
        # A figure glued to Chinese text is still a figure (``question_figures``,
        # the equivalence rule's own reading, is left exactly as it was).
        self.assertEqual(text_figures("调整后营业利润率15.8%"), {"158"})
        self.assertEqual(question_figures("调整后营业利润率15.8%"), frozenset())
        self.assertEqual(text_figures("DXC 的 9.7 亿美元"), text_figures("DXC $970 million"))
        self.assertEqual(text_figures("$0.97 billion"), {"97"})
        self.assertEqual(text_figures("5,000 名员工"), text_figures("5000"))
        # Not figures: years, periods, form names, identifiers, small counts.
        self.assertEqual(text_figures("FY2025 的10-K 2024年 Q3 2025 5 份 0001058290"), set())

    def test_spellings_and_stripping(self):
        self.assertEqual(figure_spellings(CTSH_QUESTION), ("15.8%",))
        self.assertEqual(figure_spellings("DXC $970 or 9.7 亿"), ("$970", "9.7"))
        stripped = strip_figures("Cognizant's 15.8% margin, up from $1.2 billion", {"158"})
        self.assertNotIn("15.8", stripped)
        self.assertNotIn("%", stripped)
        self.assertIn("$1.2", stripped)
        self.assertEqual(strip_figures("no figures here", {"158"}), "no figures here")


class RuleTests(unittest.TestCase):
    def test_the_live_hollow_claims_are_refused(self):
        reason = self_contained_rejection(question=CTSH_QUESTION, statement=CTSH_HOLLOW)
        self.assertIn("15.8%", reason)
        self.assertIn("never states it", reason)
        reason = self_contained_rejection(question=IBM_QUESTION, statement=IBM_HOLLOW)
        self.assertIn("18", reason)

    def test_a_restated_figure_passes_in_any_spelling(self):
        self.assertIsNone(self_contained_rejection(question=CTSH_QUESTION,
                                                   statement=CTSH_RESTATED))
        self.assertIsNone(self_contained_rejection(
            question="DXC 档案里的 970 指的是什么？",
            statement="DXC 的 9.7 亿美元是其 FY2025 自由现金流指引。"))
        # With the value present a deictic phrase is harmless.
        self.assertIsNone(self_contained_rejection(
            question=CTSH_QUESTION, statement="这个数字 15.8% 是 Cognizant 的调整后营业利润率。"))

    def test_deictic_references_without_the_value_are_refused(self):
        question = "What does the backlog disclosure say?"
        for statement in ("The figure is disclosed company-wide.",
                          "This figure is the adjusted operating margin.",
                          "The number in question is the company's backlog.",
                          "该数字是公司全年的调整后营业利润率。",
                          "这一比例由管理层在电话会上给出。"):
            with self.subTest(statement=statement):
                self.assertIn("refers to a figure",
                              self_contained_rejection(question=question, statement=statement))

    def test_ordinary_statements_are_not_deictic(self):
        question = "How does the company recognize managed services revenue?"
        for statement in ("Managed services revenue is recognized over time.",
                          "The number of delivery professionals grew in FY2025.",
                          "The value for FY2025 is disclosed in the 10-K.",
                          "该数据中心由公司自建，这值得注意；公司推进数字化转型。"):
            with self.subTest(statement=statement):
                self.assertIsNone(deictic_reference(statement))
                self.assertIsNone(self_contained_rejection(question=question,
                                                           statement=statement))


class ContractTests(unittest.TestCase):
    META = {"retrieval_match_count": 3, "question": CTSH_QUESTION}

    def test_0_1_is_unchanged(self):
        for metadata in (self.META, {**self.META, "draft_contract_version": "0.1"}):
            parsed = parse_directed_draft_text(json.dumps(_draft(CTSH_HOLLOW)),
                                               metadata=metadata)
            self.assertEqual(parsed["candidate"]["normalized_statement"], CTSH_HOLLOW)

    def test_0_2_refuses_a_hollow_statement_as_a_contract_failure(self):
        metadata = {**self.META, "draft_contract_version": "0.2"}
        with self.assertRaisesRegex(AnnualReportQualitativeError, "not self-contained"):
            parse_directed_draft_text(json.dumps(_draft(CTSH_HOLLOW)), metadata=metadata)
        parsed = parse_directed_draft_text(json.dumps(_draft(CTSH_RESTATED)), metadata=metadata)
        self.assertEqual(parsed["candidate"]["normalized_statement"], CTSH_RESTATED)
        # An honest "insufficient evidence" has no statement to hold to it.
        parse_directed_draft_text(json.dumps(_draft("x", status="insufficient_evidence")),
                                  metadata=metadata)
        with self.assertRaisesRegex(AnnualReportQualitativeError, "question"):
            parse_directed_draft_text(json.dumps(_draft(CTSH_RESTATED)), metadata={
                "retrieval_match_count": 3, "draft_contract_version": "0.2"})
        with self.assertRaisesRegex(AnnualReportQualitativeError, "unsupported"):
            parse_directed_draft_text(json.dumps(_draft(CTSH_RESTATED)),
                                      metadata={**self.META, "draft_contract_version": "0.9"})

    def test_the_prompts(self):
        proof = {"matches": []}
        old = json.loads(draft_prompt(question=CTSH_QUESTION, search_proof=proof))
        self.assertEqual(set(old), {"task", "question", "search_proof", "output_schema"})
        self.assertIn("Keep every quantity out of normalized_statement -- no figures",
                      old["task"])
        new = json.loads(draft_prompt_v2(question=CTSH_QUESTION, search_proof=proof))
        self.assertEqual(new["question_figures"], ["15.8%"])
        self.assertEqual(new["output_schema"], DRAFT_OUTPUT_SCHEMA)
        for phrase in ("stand alone", "restate that figure", "name the company",
                       "这个数字", "Apart from the figure the question names"):
            self.assertIn(phrase, new["task"])


def _material(question, *, prompt):
    proof = {"matches": [], "content_hash": "p"}
    return {"provenance_mode": MISSION_DOCUMENT_AUTHORITY_MODE, "normalized_payload": {
        "question": question, "search_proof": proof,
        "draft_proof": {"prompt_hash": content_hash(prompt(question=question,
                                                           search_proof=proof))}}}


class LedgerGateTests(unittest.TestCase):
    CLAIM = {"value": None, "unit": None, "scale": None, "currency": None}

    def test_only_a_0_2_draft_may_carry_its_question_figure(self):
        self.assertEqual(restated_question_figures(
            _material(CTSH_QUESTION, prompt=draft_prompt_v2)), {"158"})
        self.assertEqual(restated_question_figures(
            _material(CTSH_QUESTION, prompt=draft_prompt)), frozenset())
        self.assertEqual(restated_question_figures(None), frozenset())
        self.assertEqual(restated_question_figures(
            {**_material(CTSH_QUESTION, prompt=draft_prompt_v2),
             "provenance_mode": "public_web_core_authority"}), frozenset())

    def test_the_question_figure_passes_and_any_other_is_refused(self):
        claim = {**self.CLAIM, "normalized_statement": CTSH_RESTATED}
        self.assertEqual(document_qualitative_content_rejection(claim),
                         "document qualitative rule admits no numeric statement")
        self.assertIsNone(document_qualitative_content_rejection(
            claim, restated_figures={"158"}))
        other = {**self.CLAIM, "normalized_statement": CTSH_RESTATED + "上年为 14.7%。"}
        self.assertEqual(document_qualitative_content_rejection(
            other, restated_figures={"158"}),
            "document qualitative rule admits no numeric statement")


class ExecutorTests(unittest.TestCase):
    """The contract as the executor asks it, retries it, promotes it and replays it."""

    QUESTION = "What does the 36 month figure in the revenue policy describe?"
    HOLLOW = "This figure is the period over which managed services revenue is recognized."
    RESTATED = ("The company's 36 month figure is the period over which managed services "
                "revenue is recognized over time.")

    def setUp(self):
        from tests import test_mission_document_research as fixtures

        self.fixtures = fixtures
        self.case = fixtures.MissionDocumentResearchTests()
        self.case.addCleanup = self.addCleanup
        self.case.fail = self.fail

    def _wire(self, statement):
        return {"schema_version": "0.1", "status": "answered", "answer": statement,
                "candidate": {"normalized_statement": statement,
                              "metric_or_aspect": "managed services revenue recognition",
                              "period": "current policy", "basis": "reported",
                              "cited_match_indexes": [0]}, "missing": []}

    def _run(self, statements, *, question=None, auto_commit=True):
        fixture, authority, args, _registration, _launcher = self.case._fixture(
            auto_commit=auto_commit, question=question or self.QUESTION)
        self.case._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        replies = list(statements)

        def reply(_work):
            return self._wire(replies.pop(0) if len(replies) > 1 else replies[0])

        draft = self.fixtures.RouteBoundCountingFakeAdapter(reply)
        verifier = self.fixtures.RouteBoundCountingFakeAdapter(
            self.fixtures.passing_verifier_reply)
        executor, _d, _v = self.case._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier)
        return fixture, authority, admission, executor, draft

    def _drive(self, executor, admission, *, until=("complete",), limit=40):
        current = None
        for _ in range(limit):
            current = executor.run_once(admission["id"])
            if current["status"] in until:
                return current
        self.fail(f"never reached {until}: {current}")

    @staticmethod
    def _draft_works(executor, admission):
        rows = executor.scheduler.connection.execute(
            "SELECT work_order_json FROM scheduler_work_orders ORDER BY rowid").fetchall()
        return [work for work in (json.loads(row[0]) for row in rows)
                if work["metadata"].get("mission_document_research_admission_ref")
                == admission["id"]
                and work["metadata"].get("stage") == "qualitative_model_draft"]

    def test_a_hollow_draft_is_retried_and_the_restated_one_is_promoted(self):
        fixture, _authority, admission, executor, draft = self._run(
            [self.HOLLOW, self.RESTATED])
        # Paid Work attempts are capped at one. The hollow reply terminates
        # its Work; the authorized fresh recovery stages the restated answer.
        outcome = self._drive(executor, admission)
        self.assertEqual(outcome["research_status"], "canonical_claim_promoted")
        self.assertEqual(draft.calls, 2)
        works = self._draft_works(executor, admission)
        self.assertEqual(len(works), 2)
        self.assertNotEqual(works[0]["id"], works[1]["id"])
        self.assertNotIn("mission_document_recovery", works[0]["metadata"])
        recovery = works[1]["metadata"]["mission_document_recovery"]
        self.assertEqual(recovery["recovery_number"], 1)
        self.assertTrue(recovery["recovery_link_ref"])
        for work, terminal in zip(works, ("failed", "succeeded")):
            self.assertEqual(work["budget"]["step_max_attempts"], 1)
            history = executor.scheduler.attempt_history(work["id"])
            self.assertEqual([event["state"] for event in history if event["state"] in {
                "retryable", "succeeded", "failed"}], [terminal])
        for work in works:
            self.assertEqual(work["metadata"]["draft_contract_version"], "0.2")
            self.assertEqual(json.loads(work["question"])["question_figures"], ["36"])
        statement = json.loads(fixture.store.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (outcome["claim_version_ref"],)).fetchone()[0])["normalized_statement"]
        self.assertEqual(statement, self.RESTATED)
        # Replay converges without another model call.
        self.assertEqual(executor.run_once(admission["id"]), outcome)
        self.assertEqual(draft.calls, 2)

    def test_a_draft_that_stays_hollow_is_never_staged(self):
        _fixture, _authority, admission, executor, _draft = self._run([self.HOLLOW])
        self._drive(executor, admission, until=("failed",))
        self.assertEqual(executor.run_once(admission["id"])["reason"],
                         "automatic_bounded_contract_retry")
        final = self._drive(executor, admission,
                            until=("stopped", "blocked", "complete", "waiting"))
        self.assertEqual(final.get("reason"), "contract_failed_after_automatic_retry")
        self.assertEqual(executor.staging.connection.execute(
            "SELECT count(*) FROM candidate_claim_versions").fetchone()[0], 0)

    def test_a_0_2_statement_with_another_figure_is_still_refused(self):
        fixture, _authority, admission, executor, _draft = self._run(
            [self.RESTATED + " Contract assets were $4.2 million."])
        outcome = self._drive(executor, admission)
        self.assertEqual(outcome["research_status"], "candidate_rejected")
        self.assertEqual(outcome["rejection_reason"],
                         "document qualitative rule admits no numeric statement")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM claim_versions").fetchone()[0], 0)

    def test_stored_0_1_draft_work_replays_and_stays_0_1(self):
        from dalton_core import mission_document_research_executor as executor_module

        _fixture, _authority, admission, executor, draft = self._run([self.HOLLOW])
        with mock.patch.object(executor_module, "_admission_draft_contract",
                               return_value="0.1"):
            for _ in range(3):
                current = executor.run_once(admission["id"])
                if (current.get("stage") == "qualitative_model_draft"
                        and current["status"] == "succeeded"):
                    break
        [stored] = self._draft_works(executor, admission)
        self.assertNotIn("draft_contract_version", stored["metadata"])
        # Unpatched from here: the stored 0.1 Work alone decides.
        self.assertEqual(executor_module._admission_draft_contract(
            executor.scheduler, admission["id"]), "0.1")
        outcome = self._drive(executor, admission)
        self.assertEqual(outcome["status"], "complete")
        [replayed] = self._draft_works(executor, admission)
        self.assertEqual(canonical_json(replayed), canonical_json(stored))
        self.assertEqual(replayed["question"], draft_prompt(
            question=stored["metadata"]["question"],
            search_proof=stored["metadata"]["retrieval_proof"]))
        self.assertEqual(draft.calls, 1)

    def test_a_question_without_a_figure_is_asked_under_0_2_and_unchanged(self):
        statement = "Managed services revenue is recognized over time."
        _fixture, _authority, admission, executor, _draft = self._run(
            [statement], question="How does the company recognize managed services revenue?")
        outcome = self._drive(executor, admission)
        self.assertEqual(outcome["research_status"], "canonical_claim_promoted")
        [work] = self._draft_works(executor, admission)
        self.assertEqual(work["metadata"]["draft_contract_version"], "0.2")
        self.assertEqual(json.loads(work["question"])["question_figures"], [])


class ReplayScriptTests(unittest.TestCase):
    """``scripts/replay_hollow_claims_2026_09_30.py`` over a synthetic Core."""

    def _core(self, root: Path, rows):
        state = root / "state"
        state.mkdir()
        connection = sqlite3.connect(state / "core.sqlite")
        connection.executescript(
            "CREATE TABLE mission_document_research_admissions (admission_id TEXT PRIMARY KEY,"
            " company_ref TEXT, record_json TEXT);"
            "CREATE TABLE mission_document_research_promotions (promotion_id TEXT PRIMARY KEY,"
            " admission_ref TEXT, outcome_ref TEXT, record_json TEXT, content_hash TEXT,"
            " created_at TEXT);"
            "CREATE TABLE claim_versions (claim_version_id TEXT PRIMARY KEY, claim_json TEXT,"
            " content_hash TEXT, created_at TEXT);"
            "CREATE TABLE claim_retirement_decisions (claim_version_ref TEXT, decision TEXT);"
            "CREATE TABLE claim_retirement_reinstatements (claim_version_ref TEXT);")
        for index, (question, statement, retired) in enumerate(rows):
            admission, claim = f"admission:{index}", f"claim-version:{index}"
            connection.execute("INSERT INTO mission_document_research_admissions VALUES(?,?,?)",
                               (admission, "company:x", json.dumps(
                                   {"planner_inquiry": {"question": question}})))
            connection.execute(
                "INSERT INTO mission_document_research_promotions VALUES(?,?,?,?,?,?)",
                (f"promotion:{index}", admission, f"outcome:{index}",
                 json.dumps({"claim_version_ref": claim}), "h", "2026-09-30"))
            connection.execute("INSERT INTO claim_versions VALUES(?,?,?,?)", (
                claim, json.dumps({"normalized_statement": statement,
                                   "subject_ref": "company:x"}), "h", "2026-09-30"))
            if retired:
                connection.execute(
                    "INSERT INTO claim_retirement_decisions VALUES(?,?)", (claim, "retired"))
        connection.commit()
        connection.close()
        return state

    def test_lists_live_hollow_promotions_only(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / \
            "replay_hollow_claims_2026_09_30.py"
        spec = importlib.util.spec_from_file_location("replay_hollow", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            state = self._core(Path(temp), [
                (CTSH_QUESTION, CTSH_HOLLOW, False),
                (CTSH_QUESTION, CTSH_RESTATED, False),
                (IBM_QUESTION, IBM_HOLLOW, True),
                ("How is revenue recognized?", "Revenue is recognized over time.", False),
            ])
            before = (state / "core.sqlite").read_bytes()
            summary = module.replay(state)
            self.assertEqual((state / "core.sqlite").read_bytes(), before)
        self.assertEqual([item["claim_version_ref"] for item in summary["selected"]],
                         ["claim-version:0"])
        self.assertEqual(summary["already_retired"], 1)
        self.assertEqual(summary["examined"], 3)
        self.assertIn("15.8%", summary["selected"][0]["reason"])
        from dalton_core.claim_admission_cli import selection_hash
        self.assertEqual(summary["selection_sha256"], selection_hash(["claim-version:0"]))


if __name__ == "__main__":
    unittest.main()
