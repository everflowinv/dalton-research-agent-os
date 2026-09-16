"""D4: one list, four sentences per item, and nothing invented.

The list is built from what is already on disk: an undecided gate draft, a
governance file whose ``status`` is still ``proposed``, a source the mission's
own plan calls not-connected, a lane holding for an authorisation, a provider
that has been refusing work all day, a research environment with no goal in it.

Three properties matter more than the contents.

It is **read-only**: every database is opened read-only and nothing is written
anywhere, which is what makes it safe to point at a live installation.

It is **deterministic**: the same state produces the same list in the same
order.  A to-do list that reshuffles is one the owner stops trusting.

And it is **honest about what it read**.  The governance status comes from the
file, not from a lane's remembered complaint -- several of the records those
complaints name have since been approved, and a list that tells the owner to do
something they did last week is a list they stop opening.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.needs_human import (
    KINDS,
    URGENCY,
    collect,
    governance_records,
    held_lanes,
    provider_failures,
    workspaces_without_mission,
)
from dalton_core.needs_human_cli import build_parser, main, render

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def clock():
    return NOW


class ShapeTests(unittest.TestCase):
    def test_every_kind_has_an_urgency_and_the_bands_are_ordered(self):
        self.assertEqual(set(KINDS), set(URGENCY))
        self.assertLess(URGENCY["gate_decision"], URGENCY["governance_record"])
        self.assertLess(URGENCY["controlled_recovery"], URGENCY["source_not_connected"])
        self.assertLess(URGENCY["source_not_connected"], URGENCY["reopen_proposal"])

    def test_an_empty_installation_says_so_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as name:
            result = collect(state_dir=Path(name), clock=clock)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["headline"], "目前没有需要你处理的事。")
        self.assertEqual(result["as_of"], "2026-09-16T12:00:00+00:00")


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, name, status):
        (self.dir / f"{name}.json").write_text(json.dumps({
            "id": f"connector-governance:{name}:v1",
            "capability_id": f"capability:dalton:connector:{name}",
            "status": status, "effective_from": "2026-08-26T00:00:00+00:00",
            "allowed_permissions": {"risk_class": "low"},
        }), encoding="utf-8")

    def test_an_approved_record_is_not_a_to_do(self):
        self.write("sales-notes-list-notes", "approved")
        self.assertEqual(governance_records(self.dir), [])

    def test_a_proposed_record_names_the_connector_and_the_page(self):
        self.write("sec-form144-notices", "proposed")
        items = governance_records(self.dir)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("sec-form144-notices", item["title"])
        self.assertIn("sec-form144-notices", item["action"])
        self.assertEqual(item["where"], "来源")
        self.assertTrue(item["why_blocked"])
        self.assertTrue(item["consequence"])

    def test_an_unrecognised_status_is_shown_rather_than_assumed_fine(self):
        self.write("something-new", "quarantined")
        self.assertEqual(len(governance_records(self.dir)), 1)

    def test_the_list_is_the_file_not_a_lanes_memory(self):
        # The live correction: several records a lane's held reason still calls
        # unapproved have since been approved.  The file is the authority.
        self.write("company-wiki-list-documents", "approved")
        self.write("sales-notes-list-notes", "approved")
        self.write("sec-form144-notices", "proposed")
        names = [item["detail"]["file"] for item in governance_records(self.dir)]
        self.assertEqual(names, ["sec-form144-notices.json"])


class LaneTests(unittest.TestCase):
    def test_a_lane_holding_for_an_authorisation_is_listed_in_its_own_words(self):
        heartbeat = {"bounded_planner": {
            "last_completed_at": "2026-09-16T11:00:00+00:00",
            "last_result": {
                "mission_document_research": {
                    "status": "recovery_required", "held": 10,
                    "reason": "no unstarted document research admission",
                    "last": {"recovery": {
                        "action": "recovery_required",
                        "reason": "controlled_reentry_unavailable:ticket identity changed"}},
                },
                "deep_insight_gate": {"status": "idle"},
            }}}
        items = held_lanes(heartbeat)
        self.assertEqual(len(items), 1)
        self.assertIn("mission_document_research", items[0]["title"])
        # The lane's own sentence, not a translation of it: this module cannot
        # know every lane's recovery story and a wrong instruction is worse
        # than the lane's own words.
        self.assertIn("controlled_reentry_unavailable", items[0]["why_blocked"])

    def test_an_idle_lane_is_not_a_to_do(self):
        self.assertEqual(held_lanes({"bounded_planner": {"last_result": {
            "deep_insight_gate": {"status": "idle"}}}}), [])

    def test_no_heartbeat_is_not_an_error(self):
        self.assertEqual(held_lanes(None), [])
        self.assertEqual(held_lanes({}), [])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "scheduler.sqlite"
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT,"
            "result_envelope_json TEXT, outcome TEXT, created_at TEXT)")
        self.connection = connection

    def add(self, count, *, code="RATE_LIMITED", profile="profile:gpt-6-astra",
            at="2026-09-16T10:00:00+00:00"):
        for index in range(count):
            envelope = {
                "error": {"code": code, "message": "provider returned retryable HTTP 429"},
                "metadata": {
                    "profile_version_ref": "model-profile-version:broker-gpt-6-astra-abc:3",
                    "provider_retry_state": {"excluded_profile_ids": [profile]}},
            }
            self.connection.execute(
                "INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?)",
                (f"result:{code}:{index}", json.dumps(envelope), "retryable", at))
        self.connection.commit()

    def test_a_busy_afternoon_is_not_a_decision(self):
        self.add(5)
        self.assertEqual(provider_failures(self.path, now=NOW), [])

    def test_a_days_worth_of_refusals_is(self):
        self.add(120)
        items = provider_failures(self.path, now=NOW)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("gpt-6-astra", item["title"])
        self.assertEqual(item["detail"]["failures"], 120)
        # The owner's two moves, named: this is a subscription limit rather
        # than a bug, and "wait" and "use another key" are the only two things
        # that change it.
        self.assertIn("额度", item["action"])
        self.assertEqual(item["where"], "模型")

    def test_a_failure_that_is_not_a_refusal_is_somebody_elses_problem(self):
        self.add(120, code="BUDGET_REFUSED")
        self.assertEqual(provider_failures(self.path, now=NOW), [])

    def test_failures_outside_the_window_do_not_count(self):
        self.add(120, at="2026-09-10T10:00:00+00:00")
        self.assertEqual(provider_failures(self.path, now=NOW), [])

    def test_one_model_is_one_line_however_it_is_named(self):
        self.add(60)
        self.add(60, profile="model-profile-version:broker-gpt-6-astra-abc:3")
        items = provider_failures(self.path, now=NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"]["failures"], 120)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def core(self, name, *, with_mission):
        path = self.root / f"{name}.sqlite"
        connection = sqlite3.connect(path)
        if with_mission:
            connection.execute(
                "CREATE TABLE coverage_mission_pointer(mission_ref TEXT, "
                "mission_version_id TEXT)")
            connection.execute(
                "INSERT INTO coverage_mission_pointer VALUES('m','v')")
        connection.commit()
        connection.close()
        return path

    def test_an_environment_with_no_goal_is_the_most_urgent_thing_there_is(self):
        items = workspaces_without_mission([
            {"slug": "ws-a", "name": "美国 Hyperscaler 研究",
             "core_db": self.core("a", with_mission=False)},
            {"slug": "ws-b", "name": "已经在跑的环境",
             "core_db": self.core("b", with_mission=True)},
        ])
        self.assertEqual(len(items), 1)
        self.assertIn("美国 Hyperscaler 研究", items[0]["title"])
        self.assertEqual(items[0]["urgency"], 1)
        self.assertIn("你要研究什么", items[0]["action"])

    def test_an_unreadable_environment_is_skipped_rather_than_guessed_at(self):
        self.assertEqual(workspaces_without_mission(
            [{"slug": "ws-x", "core_db": self.root / "absent.sqlite"}]), [])


class GateAndOrderTests(unittest.TestCase):
    """The whole list, against a Core with a real undecided gate draft."""

    def setUp(self):
        from tests.test_deep_insight_gate_lane import FakeModel, Harness

        self.harness = Harness(submission_standard={
            "max_unknown": 12, "min_evidence_refs": 1,
            "require_question_one_classified": False,
            "require_classification_agrees": False,
        })
        self.addCleanup(self.harness.close)
        summary = self.harness.run(model_factory=lambda: FakeModel(answer_all=True))
        self.assertEqual(summary["gate_status"], "submitted")
        # The store stays open, as it is in the live service: the read-only
        # reader refuses a WAL database whose sidecars are absent rather than
        # creating them, which is the behaviour that makes "read-only" true.

    def collect(self, **kwargs):
        return collect(core_db=self.harness.state_dir / "core.sqlite",
                       state_dir=self.harness.state_dir, clock=clock, **kwargs)

    def test_an_undecided_gate_draft_is_listed_with_its_quality_summary(self):
        result = self.collect()
        gates = [item for item in result["items"] if item["kind"] == "gate_decision"]
        self.assertEqual(len(gates), 1)
        item = gates[0]
        self.assertIn("深度认知评审", item["title"])
        self.assertEqual(item["detail"]["answered"], 12)
        self.assertIn("提交标准", item["detail"]["quality_summary"])
        self.assertTrue(item["detail"]["submittable"])
        self.assertEqual(item["where"], "待办")
        self.assertIn("六个研究阶段", item["why_blocked"])

    def test_the_same_state_produces_the_same_list_twice(self):
        self.assertEqual(json.dumps(self.collect(), ensure_ascii=False, sort_keys=True),
                         json.dumps(self.collect(), ensure_ascii=False, sort_keys=True))

    def test_the_headline_names_the_most_urgent_item(self):
        result = self.collect()
        self.assertIn("深度认知评审", result["headline"])

    def test_nothing_is_written_by_reading(self):
        # No new files, and the Core itself untouched.  The WAL sidecars belong
        # to the writer that is holding the database open and move on their own.
        def listing():
            return sorted(path.name for path in self.harness.state_dir.iterdir())

        core = self.harness.state_dir / "core.sqlite"
        before, stamp = listing(), core.stat().st_mtime_ns
        self.collect()
        self.assertEqual(listing(), before)
        self.assertEqual(core.stat().st_mtime_ns, stamp)

    def test_the_cli_renders_the_four_sentences(self):
        parser = build_parser()
        args = parser.parse_args(["--state-dir", str(self.harness.state_dir)])
        self.assertEqual(args.state_dir, self.harness.state_dir)
        text = render(self.collect())
        self.assertIn("为什么卡住：", text)
        self.assertIn("你要做的：", text)
        self.assertIn("在哪里做：", text)
        self.assertIn("不做的后果：", text)

    def test_the_cli_exits_zero_and_can_filter(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--state-dir", str(self.harness.state_dir),
                         "--kind", "gate_decision"])
        self.assertEqual(code, 0)
        self.assertIn("深度认知评审", buffer.getvalue())


class HeldDraftTests(unittest.TestCase):
    """A draft the standard held back appears, and says it needs nothing."""

    def setUp(self):
        from tests.test_deep_insight_gate_lane import Harness

        self.harness = Harness(submission_standard=None)
        self.addCleanup(self.harness.close)
        self.assertEqual(self.harness.run()["gate_status"], "auto_returned")

    def test_the_owner_is_told_why_the_queue_is_empty(self):
        result = collect(core_db=self.harness.state_dir / "core.sqlite",
                         state_dir=self.harness.state_dir, clock=clock)
        held = [item for item in result["items"]
                if item["kind"] == "gate_auto_returned"]
        self.assertEqual(len(held), 1)
        self.assertFalse(held[0]["actionable"])
        self.assertTrue(held[0]["detail"]["question_gaps"])
        # It is not counted as something to do: there is nothing to press.
        self.assertNotIn(held[0], [item for item in result["items"]
                                   if item["actionable"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
