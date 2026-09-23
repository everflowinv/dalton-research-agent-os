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
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.raw_spool import RawSpool

from dalton_core.needs_human import (
    KINDS,
    LEGACY_ENVIRONMENT,
    URGENCY,
    collect,
    governance_records,
    held_lanes,
    provider_failures,
    workspaces_in_scope,
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

    def test_a_spool_near_its_bound_is_an_actionable_warning(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            spool = RawSpool(
                state / "connector-spool", max_total_bytes=100,
                archive_after_seconds=60,
            )
            sink = spool.open_sink("raw-sink:" + "a" * 64, max_response_bytes=91)
            sink.write(b"x" * 91)
            sink.finalize()
            with patch.dict("os.environ", {"DALTON_RAW_SPOOL_MAX_TOTAL_BYTES": "100"}):
                result = collect(state_dir=state, clock=clock)
        warning = next(item for item in result["items"]
                       if item["kind"] == "raw_spool_capacity")
        self.assertEqual(warning["detail"]["used_bytes"], 91)
        self.assertEqual(warning["detail"]["percent"], 91)
        self.assertFalse(warning["detail"]["over_ceiling"])
        self.assertEqual(warning["detail"]["ceiling_source"], "environment")

    def test_a_configured_ceiling_is_the_one_the_owner_is_shown(self):
        """The 2026-09-23 regression: the item quoted a ceiling nothing used.

        The configured ceiling is written beside the spool, so a process that
        launchd did not start -- an owner running a CLI, this probe -- reports
        the same bound a write would be held to instead of the built-in 1 GB.
        """

        from dalton_core.raw_spool import publish_capacity_policy

        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            spool = RawSpool(
                state / "connector-spool", max_total_bytes=100,
                archive_after_seconds=60,
            )
            sink = spool.open_sink("raw-sink:" + "a" * 64, max_response_bytes=91)
            sink.write(b"x" * 91)
            sink.finalize()
            publish_capacity_policy(
                state / "connector-spool", max_total_bytes=10_000,
                archive_after_seconds=604_800,
            )
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("DALTON_RAW_SPOOL_MAX_TOTAL_BYTES", None)
                os.environ.pop("DALTON_RAW_SPOOL_ARCHIVE_AFTER_SECONDS", None)
                # Well under the configured ceiling: no errand at all.
                self.assertEqual(
                    [item for item in collect(state_dir=state, clock=clock)["items"]
                     if item["kind"] == "raw_spool_capacity"], [],
                )
                publish_capacity_policy(
                    state / "connector-spool", max_total_bytes=50,
                    archive_after_seconds=604_800,
                )
                result = collect(state_dir=state, clock=clock)
        warning = next(item for item in result["items"]
                       if item["kind"] == "raw_spool_capacity")
        # Above the ceiling is said as such, with the overflow, not as "182%".
        self.assertTrue(warning["detail"]["over_ceiling"])
        self.assertEqual(warning["detail"]["max_total_bytes"], 50)
        self.assertEqual(warning["detail"]["overflow_bytes"], 41)
        self.assertEqual(warning["detail"]["ceiling_source"], "configured")
        self.assertIn("超过上限", warning["title"])
        # And it says what is refused and what is not.
        self.assertIn("已经落盘对象的读取", warning["why_blocked"])

    def test_a_configured_ceiling_bounds_a_writer_without_the_environment(self):
        from dalton_core.raw_spool import RawSpoolCapacityError, publish_capacity_policy

        with tempfile.TemporaryDirectory() as name:
            spool_dir = Path(name) / "connector-spool"
            publish_capacity_policy(spool_dir, max_total_bytes=64)
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("DALTON_RAW_SPOOL_MAX_TOTAL_BYTES", None)
                os.environ.pop("DALTON_RAW_SPOOL_ARCHIVE_AFTER_SECONDS", None)
                # The caller still passes the historical built-in default; the
                # configured ceiling is the one actually enforced.
                spool = RawSpool(spool_dir, max_total_bytes=1_000_000_000)
                with self.assertRaises(RawSpoolCapacityError):
                    spool.open_sink("raw-sink:" + "b" * 64, max_response_bytes=65)
                # An explicit environment override still wins over both.
                os.environ["DALTON_RAW_SPOOL_MAX_TOTAL_BYTES"] = "4000"
                relaxed = RawSpool(spool_dir, max_total_bytes=1_000_000_000)
                sink = relaxed.open_sink(
                    "raw-sink:" + "c" * 64, max_response_bytes=65
                )
                sink.write(b"y" * 65)
                sink.finalize()


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

    def test_an_escalated_unproved_send_says_the_retry_already_happened(self):
        heartbeat = {"bounded_planner": {
            "last_completed_at": "2026-09-18T11:00:00+00:00",
            "last_result": {
                "mission_document_research": {
                    "status": "recovery_required", "held": 7,
                    "reason": "no unstarted document research admission",
                    "last": {"recovery": {
                        "action": "recovery_required",
                        "reason": "unproved_send_failed_after_automatic_retry"}},
                },
            }}}
        items = held_lanes(heartbeat)
        self.assertEqual(len(items), 1)
        self.assertIn("自动重试过一次", items[0]["title"])
        # The owner must not be asked to buy the same call again without being
        # told what has already been bought, or what the worst case is.
        self.assertIn("自动重试过一次", items[0]["why_blocked"])
        self.assertIn("两次调用", items[0]["why_blocked"])

    def test_an_escalated_rebinding_says_the_rebinding_already_happened(self):
        heartbeat = {"bounded_planner": {
            "last_completed_at": "2026-09-18T11:00:00+00:00",
            "last_result": {
                "mission_document_research": {
                    "status": "recovery_required", "held": 10,
                    "reason": "no unstarted document research admission",
                    "last": {"recovery": {
                        "action": "recovery_required",
                        "reason": "reentry_failed_after_automatic_rebind:boom"}},
                },
            }}}
        items = held_lanes(heartbeat)
        self.assertIn("自动重试过一次", items[0]["title"])
        self.assertIn("自动改绑", items[0]["why_blocked"])

    def _ledger(self, root, holds):
        directory = Path(root) / "mission-document-research-runs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "holds.json").write_text(
            json.dumps({"schema_version": "0.1", "holds": holds}), encoding="utf-8")
        return directory / "holds.json"

    def test_an_escalated_hold_is_listed_even_when_the_tick_resumed_another(self):
        """The live gap: the ledger says wait, the tick said resumed."""

        with tempfile.TemporaryDirectory() as name:
            path = self._ledger(name, {
                "mission-document-research-admission:" + "a" * 32: {
                    "admission_hash": "b" * 64, "ticket_ref": None,
                    "reason": ("reentry_failed_after_automatic_rebind:"
                               "controlled reentry was already attempted"),
                    "disposition": "recovery_required", "retry_at": None,
                },
                "mission-document-research-admission:" + "c" * 32: {
                    "admission_hash": "d" * 64, "ticket_ref": None,
                    "reason": "send_state_unproved",
                    "disposition": "recovery_required", "retry_at": None,
                },
            })
            heartbeat = {"bounded_planner": {
                "last_completed_at": "2026-09-18T16:00:00+00:00",
                "last_result": {"mission_document_research": {
                    "status": "resumed",
                    "ticket_ref": "mission-document-research:" + "e" * 24,
                    "admission_ref": "mission-document-research-admission:" + "f" * 32,
                }}}}
            items = held_lanes(heartbeat, state_dir=Path(name))
        # One per escalated admission, plus the lane's aggregate.  The lane
        # having resumed some other admission this tick changes nothing.
        refs = [item["ref"] for item in items]
        self.assertEqual(refs, [
            "lane:mission_document_research:"
            "mission-document-research-admission:" + "a" * 32,
            "lane:mission_document_research",
        ])
        first = items[0]
        self.assertEqual(first["detail"]["admission_ref"],
                         "mission-document-research-admission:" + "a" * 32)
        self.assertIn("自动改绑", first["why_blocked"])
        self.assertIn("document_recovery_cli", first["action"])
        self.assertEqual(items[1]["detail"]["held"], 2)
        self.assertEqual(items[1]["detail"]["escalated"], 1)
        self.assertEqual(items[1]["detail"]["holds_path"], str(path))
        self.assertIn("resumed", items[1]["why_blocked"])

    def test_a_ledger_without_escalations_still_says_how_many_are_held(self):
        with tempfile.TemporaryDirectory() as name:
            self._ledger(name, {
                "mission-document-research-admission:" + "a" * 32: {
                    "admission_hash": "b" * 64, "ticket_ref": None,
                    "reason": "send_state_unproved",
                    "disposition": "recovery_required", "retry_at": None,
                },
                "mission-document-research-admission:" + "c" * 32: {
                    "admission_hash": "d" * 64, "ticket_ref": None,
                    "reason": "existing_lease",
                    "disposition": "recovery_wait", "retry_at": "2026-09-18T17:00:00+00:00",
                },
            })
            items = held_lanes(None, state_dir=Path(name))
        self.assertEqual([item["ref"] for item in items],
                         ["lane:mission_document_research"])
        # A timed wait is the lane's own business and is not counted.
        self.assertEqual(items[0]["detail"]["held"], 1)
        self.assertEqual(items[0]["detail"]["escalated"], 0)

    def test_a_lane_with_a_ledger_is_not_also_read_from_its_tick_result(self):
        with tempfile.TemporaryDirectory() as name:
            self._ledger(name, {
                "mission-document-research-admission:" + "a" * 32: {
                    "admission_hash": "b" * 64, "ticket_ref": None,
                    "reason": "send_state_unproved",
                    "disposition": "recovery_required", "retry_at": None,
                },
            })
            heartbeat = {"bounded_planner": {
                "last_completed_at": "2026-09-18T16:00:00+00:00",
                "last_result": {"mission_document_research": {
                    "status": "recovery_required", "held": 1,
                    "reason": "no unstarted document research admission"}}}}
            items = held_lanes(heartbeat, state_dir=Path(name))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ref"], "lane:mission_document_research")

    def test_an_idle_lane_is_not_a_to_do(self):
        self.assertEqual(held_lanes({"bounded_planner": {"last_result": {
            "deep_insight_gate": {"status": "idle"}}}}), [])

    def test_no_heartbeat_is_not_an_error(self):
        self.assertEqual(held_lanes(None), [])
        self.assertEqual(held_lanes({}), [])


class DocumentRecoveryCliTests(unittest.TestCase):
    """The owner's door: the escalation has to lead somewhere pressable."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.admission = "mission-document-research-admission:" + "a" * 32
        directory = self.state / "mission-document-research-runs"
        directory.mkdir(parents=True)
        (directory / "holds.json").write_text(json.dumps({
            "schema_version": "0.1",
            "holds": {
                self.admission: {
                    "admission_hash": "b" * 64, "ticket_ref": None,
                    "reason": ("reentry_failed_after_automatic_rebind:"
                               "controlled reentry was already attempted"),
                    "disposition": "recovery_required", "retry_at": None,
                },
                "mission-document-research-admission:" + "c" * 32: {
                    "admission_hash": "d" * 64, "ticket_ref": None,
                    "reason": "send_state_unproved",
                    "disposition": "recovery_required", "retry_at": None,
                },
            },
        }), encoding="utf-8")

    def test_holds_lists_the_ledger_and_writes_nothing(self):
        from dalton_core.document_recovery_cli import holds

        value = holds(self.state)["mission_document_research"]
        self.assertEqual(value["held"], 2)
        self.assertEqual(value["waiting_on_owner"], 1)
        self.assertEqual(value["escalated"][0]["admission_ref"], self.admission)
        self.assertEqual(value["other"][0]["reason"], "send_state_unproved")

    def test_a_dry_run_names_the_operation_and_calls_no_writer(self):
        from dalton_core import document_recovery_cli

        code = document_recovery_cli.main([
            "authorize-reentry", "--state-dir", str(self.state),
            "--admission-ref", self.admission])
        self.assertEqual(code, 0)

    def test_an_admission_the_lane_has_not_escalated_is_refused(self):
        from dalton_core import document_recovery_cli

        code = document_recovery_cli.main([
            "authorize-reentry", "--state-dir", str(self.state),
            "--admission-ref", "mission-document-research-admission:" + "c" * 32,
            "--apply", "--actor", "human:lumos"])
        # Not a refusal of the owner: a hold the lane has not given up on is
        # one it is still working through by itself.
        self.assertEqual(code, 1)

    def test_apply_goes_through_the_writers_ephemeral_human_principal(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        with mock.patch("dalton_core.governance_cli.ephemeral_call") as call:
            call.return_value = {"status": "granted"}
            code = document_recovery_cli.main([
                "authorize-reentry", "--state-dir", str(self.state),
                "--admission-ref", self.admission,
                "--apply", "--actor", "human:lumos"])
        self.assertEqual(code, 0)
        self.assertEqual(call.call_count, 1)
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["actor_ref"], "human:lumos")
        self.assertEqual(kwargs["operation"], "authorize_mission_document_reentry")
        self.assertEqual(kwargs["params"], {
            "admission_ref": self.admission, "actor_ref": "human:lumos"})
        self.assertEqual(call.call_args.args[0], self.state / "writer-tokens.json")

    def test_the_writer_exposes_it_as_a_human_only_operation(self):
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS, OPERATION_FIELDS,
            WriterServer,
        )

        name = "authorize_mission_document_reentry"
        self.assertIn(name, HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS[name],
                         frozenset({"admission_ref", "actor_ref"}))
        self.assertEqual(OPERATION_ACTOR_FIELDS[name], "actor_ref")
        self.assertTrue(hasattr(WriterServer, "_op_" + name))

    def test_the_lane_handler_says_so_when_the_lane_is_not_installed(self):
        from dalton_core.mission_document_research_lane import authorize_reentry

        class _Server:
            def lane_launcher(self, _kwarg):
                return None

        self.assertEqual(
            authorize_reentry(_Server(), {"admission_ref": self.admission,
                                          "actor_ref": "human:lumos"}),
            {"status": "unconfigured", "reason": "document research lane is absent"},
        )


class EscalatedRecoveryDoorTests(unittest.TestCase):
    """The two doors the retry escalations lead to, and the pile-clearing one."""

    PAID = "mission-document-research-admission:" + "e" * 32
    UNPROVED = "mission-document-research-admission:" + "f" * 32
    PAID_TWO = "mission-document-research-admission:" + "1" * 32
    REENTRY = "mission-document-research-admission:" + "a" * 32

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.runs = self.state / "mission-document-research-runs"
        self.runs.mkdir(parents=True)
        holds = {}
        for admission, reason, digest, started in (
            (self.UNPROVED, "unproved_send_failed_after_automatic_retry",
             "unproved00", "2026-09-18T08:00:00+00:00"),
            (self.PAID, "contract_failed_after_automatic_retry",
             "paid000000", "2026-09-18T09:00:00+00:00"),
            (self.PAID_TWO, "contract_failed_after_automatic_retry",
             "paid222222", "2026-09-18T09:30:00+00:00"),
            (self.REENTRY, "reentry_failed_after_automatic_rebind:"
             "controlled reentry was already attempted", None, None),
        ):
            holds[admission] = {
                "admission_hash": "b" * 64,
                "ticket_ref": (None if digest is None
                               else "mission-document-research:" + digest),
                "reason": reason, "disposition": "recovery_required",
                "retry_at": None,
            }
            if digest is not None:
                (self.runs / digest).mkdir()
                (self.runs / digest / "ticket.json").write_text(
                    json.dumps({"id": "mission-document-research:" + digest,
                                "started_at": started}), encoding="utf-8")
        (self.runs / "holds.json").write_text(
            json.dumps({"schema_version": "0.1", "holds": holds}),
            encoding="utf-8")

    def test_escalated_holds_come_back_oldest_run_first(self):
        from dalton_core.document_recovery_cli import escalated_holds

        items = escalated_holds(self.state)
        self.assertEqual([item["admission_ref"] for item in items],
                         [self.UNPROVED, self.PAID, self.PAID_TWO, self.REENTRY])
        self.assertEqual(
            [item["operation"] for item in items],
            ["authorize_mission_document_unproved_recovery",
             "authorize_mission_document_paid_recovery",
             "authorize_mission_document_paid_recovery",
             "authorize_mission_document_reentry"])

    def test_a_dry_run_calls_no_writer(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        with mock.patch("dalton_core.governance_cli.ephemeral_call") as call:
            code = document_recovery_cli.main([
                "authorize-paid", "--state-dir", str(self.state),
                "--admission-ref", self.PAID, "--max-cost-usd", "0.5"])
        self.assertEqual(code, 0)
        self.assertEqual(call.call_count, 0)

    def test_the_wrong_door_for_this_reason_is_refused_before_it_costs_anything(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        with mock.patch("dalton_core.governance_cli.ephemeral_call") as call:
            code = document_recovery_cli.main([
                "authorize-paid", "--state-dir", str(self.state),
                "--admission-ref", self.UNPROVED, "--apply",
                "--actor", "human:lumos"])
        self.assertEqual(code, 1)
        self.assertEqual(call.call_count, 0)

    def test_apply_sends_the_cap_through_the_ephemeral_human_principal(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        with mock.patch("dalton_core.governance_cli.ephemeral_call") as call:
            call.return_value = {"status": "admitted", "max_cost_usd": 0.4}
            code = document_recovery_cli.main([
                "authorize-unproved", "--state-dir", str(self.state),
                "--admission-ref", self.UNPROVED, "--max-cost-usd", "0.5",
                "--apply", "--actor", "human:lumos"])
        self.assertEqual(code, 0)
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["operation"],
                         "authorize_mission_document_unproved_recovery")
        self.assertEqual(kwargs["actor_ref"], "human:lumos")
        self.assertEqual(kwargs["params"], {
            "admission_ref": self.UNPROVED, "actor_ref": "human:lumos",
            "max_cost_usd": 0.5})

    def test_apply_without_a_human_actor_is_refused(self):
        from dalton_core import document_recovery_cli

        with self.assertRaises(SystemExit):
            document_recovery_cli.main([
                "authorize-paid", "--state-dir", str(self.state),
                "--admission-ref", self.PAID, "--apply",
                "--actor", "automation:document-research"])

    def test_all_escalated_walks_oldest_first_and_stops_at_the_total_cap(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        seen = []

        def _call(_tokens, _socket, *, actor_ref, operation, params):
            seen.append((operation, params.get("max_cost_usd")))
            if operation == "authorize_mission_document_reentry":
                return {"status": "granted"}
            return {"status": "admitted", "max_cost_usd": 0.4}

        with mock.patch("dalton_core.governance_cli.ephemeral_call",
                        side_effect=_call):
            code = document_recovery_cli.authorize_all_escalated(
                self.state, actor="human:lumos", apply=True,
                max_total_cost_usd=0.5)
        self.assertEqual(code, 0)
        # Oldest first, each one capped by what is left, and the walk ends the
        # moment the next paid door would go over the total.
        self.assertEqual(seen, [
            ("authorize_mission_document_unproved_recovery", 0.5),
            ("authorize_mission_document_paid_recovery", 0.1),
        ])

    def test_all_escalated_dry_run_calls_no_writer(self):
        from unittest import mock

        from dalton_core import document_recovery_cli

        with mock.patch("dalton_core.governance_cli.ephemeral_call") as call:
            document_recovery_cli.authorize_all_escalated(
                self.state, actor=None, apply=False, max_total_cost_usd=1.0)
        self.assertEqual(call.call_count, 0)

    def test_the_writer_exposes_both_doors_as_human_only_operations(self):
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS, OPERATION_FIELDS,
            WriterServer,
        )

        for name in ("authorize_mission_document_paid_recovery",
                     "authorize_mission_document_unproved_recovery"):
            with self.subTest(name=name):
                self.assertIn(name, HUMAN_GOVERNANCE_OPERATIONS)
                self.assertEqual(
                    OPERATION_FIELDS[name],
                    frozenset({"admission_ref", "max_cost_usd", "actor_ref"}))
                self.assertEqual(OPERATION_ACTOR_FIELDS[name], "actor_ref")
                self.assertTrue(hasattr(WriterServer, "_op_" + name))

    def test_a_non_human_principal_is_refused_before_anything_opens(self):
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchLaneError, authorize_paid_recovery,
            authorize_unproved_recovery,
        )

        class _Server:
            def lane_launcher(self, _kwarg):
                raise AssertionError("must not reach the lane")

        for door in (authorize_paid_recovery, authorize_unproved_recovery):
            with self.subTest(door=door.__name__):
                with self.assertRaises(MissionDocumentResearchLaneError):
                    door(_Server(), {"admission_ref": self.PAID,
                                     "actor_ref": "automation:document-research"})
                with self.assertRaises(MissionDocumentResearchLaneError):
                    door(_Server(), {"admission_ref": "not-an-admission",
                                     "actor_ref": "human:lumos"})

    def test_an_uninstalled_lane_says_so_instead_of_opening_databases(self):
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchLaneError, authorize_paid_recovery,
        )

        class _Server:
            def lane_launcher(self, _kwarg):
                return None

        with self.assertRaisesRegex(
            MissionDocumentResearchLaneError, "document research lane is absent",
        ):
            authorize_paid_recovery(_Server(), {"admission_ref": self.PAID,
                                                "actor_ref": "human:lumos"})

    def test_the_owner_list_names_the_exact_command_per_reason(self):
        from dalton_core.needs_human import held_lanes

        items = {item["detail"]["admission_ref"]: item
                 for item in held_lanes(None, state_dir=self.state)
                 if item["detail"].get("admission_ref")}
        for admission, subcommand in (
            (self.PAID, "authorize-paid"),
            (self.UNPROVED, "authorize-unproved"),
            (self.REENTRY, "authorize-reentry"),
        ):
            with self.subTest(admission=admission):
                action = items[admission]["action"]
                self.assertIn(
                    f"python -m dalton_core.document_recovery_cli {subcommand} "
                    f"--state-dir {self.state} --admission-ref {admission}",
                    action)
                self.assertIn("--apply --actor human:<owner>", action)
                self.assertNotIn("MissionDocumentResearchExecutor", action)


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

class EnvironmentScopeTests(unittest.TestCase):
    """A to-do list belongs to the environment showing it, and to no other.

    Live, every environment was showing every other environment's missing
    research goal: the legacy page offered to fix the Hyperscaler workspace and
    each workspace offered to fix its neighbour.  Nobody can do any of that
    from the page they are looking at, so the item is noise everywhere except
    on the one page that owns it.
    """

    # The same two-Core fixture as above, borrowed rather than copied; a
    # subclass would re-run its tests for nothing.
    setUp = WorkspaceTests.setUp
    core = WorkspaceTests.core

    def probes(self):
        return [
            {"slug": "ws-a", "name": "美国 Hyperscaler 研究",
             "core_db": self.core("a", with_mission=False)},
            {"slug": "ws-b", "name": "邻居环境",
             "core_db": self.core("b", with_mission=False)},
        ]

    def test_the_legacy_environment_keeps_no_workspace_probe(self):
        self.assertEqual(workspaces_in_scope(self.probes(), LEGACY_ENVIRONMENT), [])
        # And an unscoped call means the legacy environment, which is what the
        # docstring promises and what the command line defaults to.
        self.assertEqual(workspaces_in_scope(self.probes(), None), [])

    def test_a_workspace_keeps_only_itself(self):
        kept = workspaces_in_scope(self.probes(), "ws-a")
        self.assertEqual([probe["slug"] for probe in kept], ["ws-a"])
        self.assertEqual(workspaces_in_scope(self.probes(), "ws-absent"), [])

    def test_collect_under_the_legacy_scope_lists_no_workspace_item(self):
        for environment in (LEGACY_ENVIRONMENT, None):
            with self.subTest(environment=environment):
                result = collect(state_dir=self.root, workspaces=self.probes(),
                                 environment=environment, clock=clock)
                self.assertEqual(
                    [item for item in result["items"]
                     if item["kind"] == "no_active_mission"], [])

    def test_collect_under_a_workspace_scope_lists_only_that_workspace(self):
        result = collect(state_dir=self.root, workspaces=self.probes(),
                         environment="ws-a", clock=clock)
        items = [item for item in result["items"]
                 if item["kind"] == "no_active_mission"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ref"], "workspace:ws-a")
        self.assertIn("美国 Hyperscaler 研究", items[0]["title"])
        # Still the most urgent thing there is, and still counted as work.
        self.assertEqual(items[0]["urgency"], 1)
        self.assertEqual(result["items"][0], items[0])
        self.assertIn("美国 Hyperscaler 研究", result["headline"])

    def test_a_legacy_scope_does_not_go_looking_for_other_environments(self):
        # Not a filter at the end: under the legacy scope the manager
        # configuration is never read and no neighbouring Core is opened.
        opened = []
        import dalton_core.needs_human as module

        original = module.workspace_probes
        module.workspace_probes = lambda path: opened.append(path) or []
        self.addCleanup(setattr, module, "workspace_probes", original)
        collect(state_dir=self.root, workspace_manager_config_path=self.root / "m.json",
                clock=clock)
        self.assertEqual(opened, [])

    def test_the_command_line_defaults_to_the_legacy_environment(self):
        args = build_parser().parse_args(["--state-dir", str(self.root)])
        self.assertEqual(args.environment, LEGACY_ENVIRONMENT)
        self.assertEqual(
            build_parser().parse_args(
                ["--state-dir", str(self.root), "--environment", "ws-a"]).environment,
            "ws-a")


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
