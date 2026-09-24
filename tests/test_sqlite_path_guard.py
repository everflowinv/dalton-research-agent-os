"""An open connection handed over as a database path is refused, not opened.

``sqlite3.connect(str(connection))`` creates an empty database named after the
connection's repr in the working directory.  Two such files were committed to
the repository root on 2026-09-16 by a test that passed
``staging.connection`` to ``HumanReviewAuthority``; the owners that take a
path now refuse anything that is not one before touching the filesystem.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.human_intent import HumanIntentAuthority
from dalton_core.lane_failure_ledger import LaneFailureLedger
from dalton_core.research_coordinator import ResearchCoordinatorStore
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.scheduler import Scheduler
from dalton_core.sqlite_path import sqlite_path
from dalton_core.store import DaltonStore
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.tick_ledger import TickLedger

REPO = Path(__file__).resolve().parents[1]
OWNERS = (
    CapabilityCatalog, HumanIntentAuthority, LaneFailureLedger,
    ResearchCoordinatorStore, HumanReviewAuthority, CandidateStagingStore,
    Scheduler, DaltonStore, ThesisImpactBudgetStore, TickLedger,
)


class SqlitePathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.cwd = tempfile.TemporaryDirectory()
        self.addCleanup(self.cwd.cleanup)
        previous = os.getcwd()
        os.chdir(self.cwd.name)
        self.addCleanup(os.chdir, previous)

    def test_paths_pass_through(self) -> None:
        self.assertEqual(":memory:", sqlite_path(":memory:"))
        self.assertEqual("/tmp/x.sqlite", sqlite_path(Path("/tmp/x.sqlite")))
        self.assertEqual("/tmp/x.sqlite", sqlite_path(b"/tmp/x.sqlite"))

    def test_every_path_owner_refuses_a_connection_without_creating_a_file(self) -> None:
        for owner in OWNERS:
            with self.subTest(owner=owner.__name__):
                with self.assertRaisesRegex(TypeError, "database path"):
                    owner(self.connection)
                self.assertEqual([], os.listdir(self.cwd.name))

    def test_repository_root_tracks_no_connection_repr_files(self) -> None:
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--", "<sqlite3.Connection*"],
            capture_output=True, text=True, check=False,
        )
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual("", tracked.stdout)
        ignored = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", "--no-index",
             "<sqlite3.Connection object at 0x1090dcb80>"],
            check=False,
        )
        self.assertEqual(0, ignored.returncode)


if __name__ == "__main__":
    unittest.main()
