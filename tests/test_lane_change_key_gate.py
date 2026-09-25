"""B1-5: the cheap key in front of the two expensive per-tick fingerprints.

The dossier and debate-map lanes both decide by fingerprinting a subject's
evidence, and on the live Core both fingerprints cost seconds of the writer's
single store thread -- every tick, for every subject, including the four of
five that gained nothing.  These tests pin the gate that stops that: an
unchanged change key reuses the signature, a changed one recomputes, and a
restart honours the key written to disk instead of recomputing everything at
once on the first tick after a deploy.

The last test is the one that would have caught the original bug: it counts
the expensive calls per tick rather than timing them, because a timing test
passes on a fixture of twelve Claims no matter how quadratic the code is.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import dalton_core.mission_debate_map_lane as debate_lane
import dalton_core.mission_dossier_lane as dossier_lane
from dalton_core.lane_change_key import ChangeKeyMemo
from dalton_core.mission_debate_map_lane import (
    CHANGE_KEY_FILE as DEBATE_CHANGE_KEY_FILE, MissionDebateMapLaneCoordinator,
    subject_change_keys,
)
from dalton_core.mission_dossier_lane import (
    CHANGE_KEY_FILE, MissionDossierLaneCoordinator, company_change_keys,
)
from tests.test_dossier_lane import ACN, CoordinatorTests, Harness


class _Counter:
    """Patches one expensive call and records who asked for it."""

    def __init__(self, module, name):
        self.module = module
        self.name = name
        self.calls: list[str] = []

    def __enter__(self):
        original = getattr(self.module, self.name)

        def counted(connection, company_ref, *args, **kwargs):
            self.calls.append(str(company_ref))
            return original(connection, company_ref, *args, **kwargs)

        self._patch = patch.object(self.module, self.name, counted)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class DossierChangeKeyTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection
        self.state = Path(tempfile.mkdtemp())

    def coordinator(self, launcher, **kwargs):
        return MissionDossierLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN], failure_ledger_dir=self.state, **kwargs)

    def test_an_unchanged_key_never_asks_for_the_expensive_signature_again(self):
        launcher = CoordinatorTests.Launcher()
        coordinator = self.coordinator(launcher)
        with _Counter(dossier_lane, "company_ledger_signature") as counter:
            first = coordinator.dispatch_once()
            self.assertEqual(counter.calls, [ACN])
            self.assertEqual(first.get("recomputed"), [ACN])
            # Three more ticks over a Ledger nobody has written to.  The lane
            # still reaches the same signature and the same decision; it just
            # does not pay for it again.
            signatures = set()
            for _ in range(3):
                result = coordinator.dispatch_once()
                self.assertNotIn("recomputed", result)
                if result.get("signature"):
                    signatures.add(result["signature"])
            self.assertEqual(counter.calls, [ACN])
        self.assertEqual(signatures or {first["signature"]}, {first["signature"]})

    def test_a_claim_that_lands_moves_the_key_and_the_signature_is_rebuilt(self):
        launcher = CoordinatorTests.Launcher()
        coordinator = self.coordinator(launcher)
        first = coordinator.dispatch_once()
        before = first["signature"]
        self.harness.tag("moved-1", "demand_drivers",
                         statement="新的季度材料说需求在回暖。")
        with _Counter(dossier_lane, "company_ledger_signature") as counter:
            after = coordinator.dispatch_once()
        self.assertEqual(counter.calls, [ACN])
        self.assertEqual(after.get("recomputed"), [ACN])
        self.assertNotEqual(after.get("signature") or before, before)

    def test_the_key_is_read_back_from_disk_after_a_restart(self):
        launcher = CoordinatorTests.Launcher()
        first = self.coordinator(launcher).dispatch_once()
        self.assertTrue((self.state / CHANGE_KEY_FILE).is_file())

        # A new coordinator is what a writer restart produces: same state
        # directory, empty memory.  Without the file this tick would rebuild
        # every screened company's signature at once.
        restarted = self.coordinator(CoordinatorTests.Launcher())
        with _Counter(dossier_lane, "company_ledger_signature") as counter:
            again = restarted.dispatch_once()
        self.assertEqual(counter.calls, [])
        self.assertNotIn("recomputed", again)
        self.assertEqual(again.get("signature") or first["signature"],
                         first["signature"])

    def test_a_coordinator_with_nowhere_to_write_still_gates_in_memory(self):
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=CoordinatorTests.Launcher(),
            companies=lambda: [ACN])
        with _Counter(dossier_lane, "company_ledger_signature") as counter:
            coordinator.dispatch_once()
            coordinator.dispatch_once()
        self.assertEqual(counter.calls, [ACN])

    def test_an_unreadable_key_recomputes_rather_than_refusing(self):
        coordinator = self.coordinator(CoordinatorTests.Launcher())
        with patch.object(dossier_lane, "company_change_keys",
                          side_effect=RuntimeError("no such table")):
            with _Counter(dossier_lane, "company_ledger_signature") as counter:
                first = coordinator.dispatch_once()
                coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(counter.calls, [ACN, ACN])

    def test_the_key_covers_every_table_the_fingerprint_reads(self):
        """A write to any of them must move the key, or a company freezes."""

        def key():
            return company_change_keys(self.connection, [ACN])[ACN]

        start = key()
        self.assertEqual(start, key())
        self.harness.tag("covered-1", "demand_drivers", statement="新材料。")
        moved = key()
        self.assertNotEqual(moved, start)
        # And the control half: a redeployed contract is in the signature, so
        # it is in the key.
        self.assertNotEqual(
            company_change_keys(self.connection, [ACN], control=("v2",))[ACN],
            moved)

    def test_one_company_moving_does_not_re_sign_the_others(self):
        other = "company:sec-cik:0000000002"
        coordinator = MissionDossierLaneCoordinator(
            connection=self.connection, launcher=CoordinatorTests.Launcher(),
            companies=lambda: [ACN, other], failure_ledger_dir=self.state)
        coordinator.dispatch_once()
        # Settle the open ticket so the next tick reaches the loop again.
        coordinator.dispatch_once()
        self.harness.tag("moved-2", "demand_drivers", statement="只有这一家动了。")
        with _Counter(dossier_lane, "company_ledger_signature") as counter:
            coordinator.dispatch_once()
        self.assertEqual(counter.calls, [ACN])


class DebateMapChangeKeyTests(unittest.TestCase):
    class Launcher:
        def __init__(self):
            self.started: list[tuple[str, str]] = []

        def start(self, *, subject_ref, fingerprint):
            self.started.append((subject_ref, fingerprint))
            return {"id": f"debate-map-run:{len(self.started):024d}"}

        def status(self, ticket_ref):
            return {"id": ticket_ref, "status": "succeeded",
                    "subject_ref": self.started[-1][0],
                    "evidence_fingerprint": self.started[-1][1],
                    "summary": {"map_status": "fresh"}}

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        # These count Ledger reads per tick with a launcher whose "fresh" run
        # never publishes, so the same subject is chosen tick after tick.
        # Redraw pacing (2026-09-25) would hold it and move the loop on to the
        # next subject -- a different, deliberate read; it is tested in
        # test_debate_map_lane and switched off here.
        pacing = patch.object(debate_lane, "MIN_REDRAW_SECONDS", 0)
        pacing.start()
        self.addCleanup(pacing.stop)
        self.state = Path(tempfile.mkdtemp())
        self.mission = {
            "id": self.harness.mission["id"],
            "content_hash": self.harness.mission["content_hash"],
            "universe": [{"company_ref": ACN}],
            "industry_ref": "industry:us-it-services",
        }

    def coordinator(self, launcher=None):
        return MissionDebateMapLaneCoordinator(
            store=self.harness.store, launcher=launcher or self.Launcher(),
            mission=lambda: dict(self.mission), failure_ledger_dir=self.state)

    def test_an_unchanged_key_never_reads_the_ledger_again(self):
        coordinator = self.coordinator()
        with patch("dalton_core.debate_map_draft.subject_claim_refs",
                   wraps=debate_lane_refs()) as refs:
            first = coordinator.dispatch_once()
            # Only the first subject: the loop stops at the subject whose
            # map it is about to draw, exactly as it did before the gate.
            self.assertEqual(first.get("recomputed"), [ACN])
            before = refs.call_count
            self.assertGreater(before, 0)
            coordinator.dispatch_once()
            coordinator.dispatch_once()
            self.assertEqual(refs.call_count, before)

    def test_a_claim_moves_only_its_own_subject(self):
        coordinator = self.coordinator()
        coordinator.dispatch_once()
        coordinator.dispatch_once()
        self.harness.tag("debate-moved", "demand_drivers", statement="新材料到了。")
        with patch("dalton_core.debate_map_draft.subject_claim_refs",
                   wraps=debate_lane_refs()) as refs:
            result = coordinator.dispatch_once()
        self.assertEqual(result.get("recomputed"), [ACN])
        self.assertEqual([call.args[1] for call in refs.call_args_list], [ACN])

    def test_the_key_survives_a_restart(self):
        self.coordinator().dispatch_once()
        self.assertTrue((self.state / DEBATE_CHANGE_KEY_FILE).is_file())
        restarted = self.coordinator()
        with patch("dalton_core.debate_map_draft.subject_claim_refs",
                   wraps=debate_lane_refs()) as refs:
            result = restarted.dispatch_once()
        self.assertEqual(refs.call_count, 0)
        self.assertNotIn("recomputed", result)

    def test_a_subject_with_no_claims_is_remembered_as_such(self):
        # In front of the queue, so the loop really does reach it: a subject
        # nobody has written about yet would otherwise force a fresh Ledger
        # snapshot on every tick for ever.
        self.mission["universe"] = [{"company_ref": "company:sec-cik:0000000009"},
                                    {"company_ref": ACN}]
        coordinator = self.coordinator()
        first = coordinator.dispatch_once()
        self.assertEqual(first.get("recomputed"),
                         ["company:sec-cik:0000000009", ACN])
        with patch("dalton_core.debate_map_draft.subject_claim_refs",
                   wraps=debate_lane_refs()) as refs:
            coordinator.dispatch_once()
        self.assertEqual(refs.call_count, 0)

    def test_the_key_is_narrow_on_purpose(self):
        """Only Claims and index entries may move a debate-map key."""

        subjects = [ACN, "industry:us-it-services"]
        start = subject_change_keys(self.harness.store.connection, subjects)
        self.assertEqual(start, subject_change_keys(
            self.harness.store.connection, subjects))
        self.harness.tag("narrow-1", "demand_drivers", statement="新材料。")
        moved = subject_change_keys(self.harness.store.connection, subjects)
        self.assertNotEqual(moved[ACN], start[ACN])
        self.assertEqual(moved["industry:us-it-services"],
                         start["industry:us-it-services"])


def debate_lane_refs():
    from dalton_core.debate_map_draft import subject_claim_refs

    return subject_claim_refs


class ExpensiveCallsPerTickTests(unittest.TestCase):
    """Count the reads, do not time them.

    A fixture holds a dozen Claims and will run fast however quadratic the
    code underneath is; the live Core holds 13,816 and did not.  What the live
    failure actually was is visible only as a count: one Ledger snapshot per
    company instead of one per tick, and one index read per *aspect* instead
    of one per company -- ten of them, over the same four thousand entries.
    """

    def setUp(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.state = Path(tempfile.mkdtemp())

    def _counted(self, module, name):
        original = getattr(module, name)
        calls: list[Any] = []

        def counted(*args, **kwargs):
            calls.append(args[1:2])
            return original(*args, **kwargs)

        return calls, patch.object(module, name, counted)

    def _snapshots(self):
        from dalton_core.store import DaltonStore

        return self._counted(DaltonStore, "claim_index_snapshot")

    def _index_reads(self):
        import dalton_core.claim_index_authority as index_module

        return self._counted(index_module, "current_entries")

    def test_the_dossier_tick_reads_one_snapshot_and_then_none_at_all(self):
        coordinator = MissionDossierLaneCoordinator(
            connection=self.harness.store.connection,
            launcher=CoordinatorTests.Launcher(), companies=lambda: [ACN],
            failure_ledger_dir=self.state)
        snapshots, counter = self._snapshots()
        with counter:
            coordinator.dispatch_once()
            self.assertEqual(len(snapshots), 1)
            snapshots.clear()
            for _ in range(4):
                coordinator.dispatch_once()
            self.assertEqual(snapshots, [])

    def test_one_company_signature_reads_the_index_once_not_once_per_aspect(self):
        from dalton_core.company_dossier_cli import SECTIONS
        from dalton_core.mission_dossier_lane import company_ledger_signature

        self.assertGreater(len(SECTIONS), 1)
        reads, counter = self._index_reads()
        with counter:
            company_ledger_signature(self.harness.store.connection, ACN)
        self.assertEqual(len(reads), 1, "one company, one index read")

    def test_the_debate_map_tick_reads_one_snapshot_for_every_subject(self):
        mission = {
            "id": self.harness.mission["id"],
            "content_hash": self.harness.mission["content_hash"],
            # The subject with no Claims first, so the tick really does
            # fingerprint two subjects and the shared read is what is counted.
            "universe": [{"company_ref": "company:sec-cik:0000000003"},
                         {"company_ref": ACN}],
            "industry_ref": "industry:us-it-services",
        }
        coordinator = MissionDebateMapLaneCoordinator(
            store=self.harness.store,
            launcher=DebateMapChangeKeyTests.Launcher(),
            mission=lambda: dict(mission), failure_ledger_dir=self.state)
        pacing = patch.object(debate_lane, "MIN_REDRAW_SECONDS", 0)  # as above
        pacing.start()
        self.addCleanup(pacing.stop)
        snapshots, counter = self._snapshots()
        with counter:
            coordinator.dispatch_once()
            self.assertEqual(len(snapshots), 1,
                             "every subject shares one Ledger read")
            snapshots.clear()
            for _ in range(4):
                coordinator.dispatch_once()
            self.assertEqual(snapshots, [])


class ChangeKeyReadTests(unittest.TestCase):
    """A table that is not here, and a read that is broken, are different."""

    def setUp(self):
        import sqlite3

        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def test_an_optional_table_this_core_never_opened_is_an_empty_answer(self):
        from dalton_core.lane_change_key import (
            append_probe, claim_index_change_keys, head_change_keys)

        self.assertEqual(claim_index_change_keys(self.connection), {})
        self.assertEqual(
            head_change_keys(self.connection, "company_dossier_versions",
                             "company_ref"), {})
        self.assertEqual(append_probe(self.connection, "evidence_relations"),
                         "absent")

    def test_a_ledger_that_cannot_be_read_raises_rather_than_going_quiet(self):
        """The one read whose silence would freeze a company for ever."""

        from dalton_core.lane_change_key import claim_change_keys

        with self.assertRaises(Exception):
            claim_change_keys(self.connection)
        # And a table that exists but answers wrongly is a fault too.
        self.connection.execute("CREATE TABLE claim_versions (unrelated TEXT)")
        with self.assertRaises(Exception):
            claim_change_keys(self.connection)


class ChangeKeyMemoTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "keys.json"

    def test_it_answers_only_for_the_key_it_was_written_under(self):
        memo = ChangeKeyMemo(self.path)
        self.assertIsNone(memo.cached("a", "k1"))
        memo.remember("a", "k1", "sig-1")
        self.assertEqual(memo.cached("a", "k1"), "sig-1")
        self.assertIsNone(memo.cached("a", "k2"))
        self.assertIsNone(memo.cached("b", "k1"))
        self.assertIsNone(memo.cached("a", None))

    def test_a_corrupt_file_is_an_empty_memo_not_an_error(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ChangeKeyMemo(self.path).cached("a", "k1"))

    def test_it_forgets_subjects_nobody_asks_about(self):
        memo = ChangeKeyMemo(self.path)
        memo.remember("a", "k1", "sig-1")
        memo.remember("b", "k1", "sig-2")
        memo.forget(["a"])
        self.assertEqual(memo.cached("a", "k1"), "sig-1")
        self.assertIsNone(memo.cached("b", "k1"))
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(written["subjects"]), ["a"])

    def test_a_memo_with_no_path_keeps_everything_in_memory(self):
        memo = ChangeKeyMemo(None)
        memo.remember("a", "k1", "sig-1")
        self.assertEqual(memo.cached("a", "k1"), "sig-1")


if __name__ == "__main__":
    unittest.main()
