"""P10d: the missing quarters are queued from authority the system already holds."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.mission_sec_quarters import (
    MissionSecQuartersCoordinator,
    quarterly_filings,
    read_artifact,
)

ACN = "company:sec-cik:0001467373"
AUTOMATION = "automation:coverage-mission"
ARTIFACT = "artifact-version:acn-company-facts"

PAYLOAD = {
    "cik": 1467373,
    "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        # Two quarters, each also reported as a year-to-date row from the same
        # filing; only the quarter is a quarter.
        {"form": "10-Q", "start": "2026-03-01", "end": "2026-05-31",
         "accn": "0001467373-26-000032", "filed": "2026-06-18", "val": 1},
        {"form": "10-Q", "start": "2025-09-01", "end": "2026-05-31",
         "accn": "0001467373-26-000032", "filed": "2026-06-18", "val": 3},
        {"form": "10-Q", "start": "2025-12-01", "end": "2026-02-28",
         "accn": "0001467373-26-000014", "filed": "2026-03-19", "val": 1},
        {"form": "10-K", "start": "2024-09-01", "end": "2025-08-31",
         "accn": "0001467373-25-000100", "filed": "2025-10-10", "val": 4},
    ]}}}},
}


class _Missions:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.queued: list[dict] = []

    def mission(self, ref):
        return {"id": ref, "autonomy": {"automation_principal": AUTOMATION}}

    def authorize_sec_lane(self, *, company_ref, ticker, actor_ref, **kwargs):
        if self.refuse:
            raise RuntimeError("mission does not grant this run")
        return {"company_ref": company_ref, "ticker": ticker, "actor_ref": actor_ref}

    def queue_sec_dispatch(self, **kwargs):
        self.queued.append(kwargs)
        return {"status": "fresh", "dispatch_id": f"d{len(self.queued)}"}


class _Store:
    """The three reads the coordinator makes, without a Core."""

    def __init__(self, *, artifact_hash: str | None, periods: tuple[str, ...] = (),
                 attempts: dict[str, int] | None = None, open_dispatches: int = 0) -> None:
        self.open_dispatches = open_dispatches
        self.artifact_hash = artifact_hash
        self.periods = periods
        self.attempts = attempts or {}
        self.connection = self

    def execute(self, sql, params=()):
        rows: list[dict] = []
        if "FROM evidence_relations" in sql:
            if self.artifact_hash is not None:
                rows = [{"evidence_json": json.dumps({
                    "source_ref": "source:sec-edgar",
                    "artifact_refs": [{"ref": ARTIFACT, "hash": "0" * 64}],
                }), "created_at": "2026-09-01"}]
        elif "observability_artifact_versions_v2" in sql:
            rows = [{"artifact_content_hash": self.artifact_hash}] if self.artifact_hash else []
        elif "claim_retirement_decisions" in sql:
            rows = []
        elif "coverage_mission_sec_dispatches" in sql:
            rows = ([{"n": self.open_dispatches}] if "status IN" in sql
                    else [{"expected_accession": a, "n": n} for a, n in self.attempts.items()])
        elif "FROM claim_versions" in sql:
            rows = [{"id": f"claim-version:{i}", "period": p} for i, p in enumerate(self.periods)]
        elif "coverage_mission_pointer" in sql:
            rows = [{"mission_version_id": "coverage-mission-version:test:1"}]

        class _Result:
            @staticmethod
            def fetchall():
                return rows
            @staticmethod
            def fetchone():
                return rows[0] if rows else None
        return _Result()


def _entry(have: int) -> dict:
    return {
        "company_ref": ACN, "ticker": "ACN",
        "items": [{"item_ref": "quarterly_financials", "label": "过去 4 个季度的财报数字",
                   "have": have, "required": 4, "status": "partial"}],
    }


class PayloadTests(unittest.TestCase):
    def test_only_true_quarters_are_filings_to_queue(self) -> None:
        filings = quarterly_filings(PAYLOAD)
        self.assertEqual([f["period"] for f in filings],
                         ["2026-03-01..2026-05-31", "2025-12-01..2026-02-28"])
        self.assertEqual(filings[0]["accession"], "0001467373-26-000032")
        self.assertEqual(filings[0]["filed"], "2026-06-18")
        self.assertEqual(quarterly_filings({}), [])

    def test_only_the_recent_filings_are_candidates(self) -> None:
        """Company facts carry every filing ever; live the lane walked into 2023."""

        rows = [
            {"form": "10-Q", "start": f"20{year:02d}-03-01", "end": f"20{year:02d}-05-31",
             "accn": f"0001467373-{year:02d}-000001", "filed": f"20{year:02d}-06-18", "val": 1}
            for year in range(18, 27)
        ]
        payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rows}}}}}
        filings = quarterly_filings(payload)
        self.assertEqual(len(filings), 6)
        self.assertEqual(filings[0]["end"], "2026-05-31")
        self.assertEqual(filings[-1]["end"], "2021-05-31")
        self.assertEqual(len(quarterly_filings(payload, limit=2)), 2)

    def test_the_artifact_is_read_by_its_own_hash_or_not_at_all(self) -> None:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        raw = json.dumps(PAYLOAD).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = root / "transcript-spool" / "connector-spool" / "objects" / digest[:2] / digest
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        self.assertEqual(read_artifact(root, digest)["cik"], 1467373)
        # Bytes that do not hash to what authority recorded are not used.
        path.write_bytes(raw + b" ")
        self.assertIsNone(read_artifact(root, digest))
        self.assertIsNone(read_artifact(root, "f" * 64))


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = json.dumps(PAYLOAD).encode("utf-8")
        self.digest = hashlib.sha256(raw).hexdigest()
        path = self.root / "transcript-spool" / "connector-spool" / "objects" / self.digest[:2] / self.digest
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)

    def coordinator(self, entry, *, store=None, missions=None):
        return MissionSecQuartersCoordinator(
            store=store or _Store(artifact_hash=self.digest), missions=missions or _Missions(),
            state_dir=self.root, checklist=lambda: [entry],
            clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
        )

    def test_missing_quarters_are_queued_bound_to_their_own_accession(self) -> None:
        missions = _Missions()
        result = self.coordinator(_entry(0), missions=missions).dispatch_once()
        self.assertEqual(result["status"], "queued")
        self.assertEqual([q["accession"] for q in result["queued"]],
                         ["0001467373-26-000032", "0001467373-26-000014"])
        first = missions.queued[0]
        self.assertEqual(first["form"], "10-Q")
        self.assertEqual(first["expected_accession"], "0001467373-26-000032")
        # The window is narrow around the filing date, so the lane finds that filing.
        self.assertEqual((first["filed_from"], first["filed_to"]), ("2026-06-16", "2026-06-20"))
        self.assertTrue(first["observation_ref"].endswith(self.digest))

    def test_one_filing_is_dispatched_once_even_when_it_answers_two_periods(self) -> None:
        """A 10-Q also reports the prior-year quarter; live that queued it twice."""

        payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"form": "10-Q", "start": "2025-09-01", "end": "2025-11-30",
             "accn": "0001467373-25-000222", "filed": "2025-12-18", "val": 1},
            {"form": "10-Q", "start": "2024-09-01", "end": "2024-11-30",
             "accn": "0001467373-25-000222", "filed": "2025-12-18", "val": 1},
        ]}}}}}
        raw = json.dumps(payload).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / "transcript-spool" / "connector-spool" / "objects" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        missions = _Missions()
        result = MissionSecQuartersCoordinator(
            store=_Store(artifact_hash=digest), missions=missions, state_dir=self.root,
            checklist=lambda: [_entry(0)], clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
        ).dispatch_once()
        self.assertEqual([q["accession"] for q in result["queued"]], ["0001467373-25-000222"])
        self.assertEqual(len(missions.queued), 1)

    def test_a_period_already_in_the_ledger_is_not_queued_again(self) -> None:
        store = _Store(artifact_hash=self.digest, periods=("2026-03-01..2026-05-31",))
        missions = _Missions()
        result = self.coordinator(_entry(1), store=store, missions=missions).dispatch_once()
        self.assertEqual([q["accession"] for q in result["queued"]], ["0001467373-26-000014"])

    def test_a_company_with_four_quarters_or_no_artifact_is_left_alone(self) -> None:
        done = self.coordinator({**_entry(4), "items": [
            {"item_ref": "quarterly_financials", "have": 4, "required": 4, "status": "complete"}]}).dispatch_once()
        self.assertEqual(done["status"], "idle")
        self.assertIn("已有四个季度", json.dumps(done["skipped"], ensure_ascii=False))
        missing = self.coordinator(_entry(1), store=_Store(artifact_hash=None)).dispatch_once()
        self.assertEqual(missing["status"], "idle")
        self.assertIn("原始件", json.dumps(missing["skipped"], ensure_ascii=False))

    def test_a_retry_widens_the_window_and_stops_after_three(self) -> None:
        """A terminated plan is terminal; the identical window would replay it."""

        missions = _Missions()
        store = _Store(artifact_hash=self.digest, attempts={"0001467373-26-000032": 1})
        result = self.coordinator(_entry(0), store=store, missions=missions).dispatch_once()
        first = next(q for q in result["queued"] if q["accession"] == "0001467373-26-000032")
        self.assertEqual(first["attempt"], 2)
        queued = next(q for q in missions.queued if q["expected_accession"] == "0001467373-26-000032")
        self.assertEqual((queued["filed_from"], queued["filed_to"]), ("2026-06-15", "2026-06-21"))
        exhausted = _Store(artifact_hash=self.digest, attempts={"0001467373-26-000032": 3})
        out = self.coordinator(_entry(0), store=exhausted, missions=_Missions()).dispatch_once()
        self.assertNotIn("0001467373-26-000032", [q["accession"] for q in out.get("queued", [])])
        self.assertIn("已经试过 3 次", json.dumps(out, ensure_ascii=False))

    def test_a_company_with_filings_still_queued_is_left_to_drain(self) -> None:
        missions = _Missions()
        store = _Store(artifact_hash=self.digest, open_dispatches=2)
        result = self.coordinator(_entry(1), store=store, missions=missions).dispatch_once()
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        self.assertIn("在队列里等着跑", json.dumps(result["skipped"], ensure_ascii=False))

    def test_a_refused_grant_is_reported_and_nothing_is_queued(self) -> None:
        missions = _Missions(refuse=True)
        result = self.coordinator(_entry(1), missions=missions).dispatch_once()
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        self.assertIn("does not grant", json.dumps(result["skipped"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
