"""P10d: the missing quarters are queued from authority the system already holds."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.mission_sec_quarters import (
    MAX_ATTEMPTS_PER_FILING,
    accession_in_hand,
    MissionSecQuartersCoordinator,
    classify_failure,
    source_lag_retry_at,
    transport_retry_at,
    quarterly_filings,
    read_artifact,
    submissions_filings,
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


AUTHORIZED_POLICY = {
    "research_plan_auto_start": {
        "enabled": True,
        "rules": ["research-plan-auto-start:sec-public-company-facts:v1"]},
    "research_candidate_auto_commit": {
        "enabled": True, "max_records": 20,
        "rules": ["research-auto-commit:sec-public-company-facts-growth:v1"]},
}


class _Store:
    """The reads the coordinator makes, without a Core."""

    policy: dict = AUTHORIZED_POLICY

    def active_policy(self):
        return {"policy_version_id": "policy-4", "policy": self.policy}

    def __init__(self, *, artifact_hash: str | None = None, periods: tuple[str, ...] = (),
                 attempts: dict[str, int] | None = None, open_dispatches: int = 0,
                 artifacts: tuple[str, ...] | None = None,
                 other_metric_periods: tuple[str, ...] = (),
                 statement_filings: tuple[dict, ...] = (),
                 failed_runs: tuple[dict, ...] = (),
                 envelopes: dict | None = None, policy: dict | None = None) -> None:
        self.envelopes = envelopes or {}
        if policy is not None:
            self.policy = policy
        self.open_dispatches = open_dispatches
        self.failed_runs = failed_runs
        # Newest first, as the evidence query orders them.
        self.artifacts = artifacts if artifacts is not None else (
            (artifact_hash,) if artifact_hash is not None else ())
        self.periods = periods
        self.other_metric_periods = other_metric_periods
        self.attempts = attempts or {}
        self.statement_filings = statement_filings
        self.connection = self

    def execute(self, sql, params=()):
        rows: list[dict] = []
        if "FROM evidence_relations" in sql:
            rows = [{"evidence_json": json.dumps({
                "source_ref": "source:sec-edgar",
                "artifact_refs": [{"ref": f"{ARTIFACT}:{index}", "hash": "0" * 64}],
            }), "created_at": "2026-09-01"} for index, _ in enumerate(self.artifacts)]
        elif "observability_artifact_versions_v2" in sql:
            index = int(params[0].rsplit(":", 1)[-1])
            rows = [{"artifact_content_hash": self.artifacts[index]}]
        elif "claim_retirement_decisions" in sql:
            rows = []
        elif "coverage_mission_statement_filings" in sql:
            rows = [dict(item) for item in self.statement_filings]
        elif "scheduler_result_envelopes" in sql:
            envelope = self.envelopes.get(params[0])
            rows = [] if envelope is None else [{"result_envelope_json": json.dumps(envelope)}]
        elif "dispatch_reason" in sql:
            rows = [dict(item) for item in self.failed_runs]
        elif "coverage_mission_sec_dispatches" in sql:
            rows = ([{"n": self.open_dispatches}] if "status IN" in sql
                    else [{"expected_accession": a, "n": n} for a, n in self.attempts.items()])
        elif "FROM claim_versions" in sql:
            periods = list(self.periods)
            # The real query filters on the metric; the fake only honours that
            # if the query asks, so an unfiltered query would see these too.
            if "metric_or_aspect" not in sql:
                periods += list(self.other_metric_periods)
            rows = [{"id": f"claim-version:{i}", "period": p} for i, p in enumerate(periods)]
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
        # The 10-K stands for the quarter ending on its fiscal year end; it
        # reports only the year, so it names no quarterly start.
        self.assertEqual([(f["period"], f["form"]) for f in filings],
                         [("2026-03-01..2026-05-31", "10-Q"), ("2025-12-01..2026-02-28", "10-Q"),
                          (None, "10-K")])
        self.assertEqual((filings[2]["end"], filings[2]["fourth_quarter"]), ("2025-08-31", False))
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

    def test_a_company_with_its_newest_quarters_answered_or_no_artifact_is_left_alone(self) -> None:
        done = self.coordinator(_entry(4), store=_Store(
            artifact_hash=self.digest,
            periods=("2026-03-01..2026-05-31", "2025-12-01..2026-02-28"),
        )).dispatch_once()
        self.assertEqual(done["status"], "idle")
        # The one quarter still missing is the fiscal fourth, in a 10-K of
        # year totals only: nothing any rule can answer, so nothing queued.
        self.assertIn("只报全年数", json.dumps(done["skipped"], ensure_ascii=False))
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


# What the newest SEC artifact behind CTSH's Claims actually was on 2026-09-24:
# an EDGAR submissions payload, not company facts.
CTSH = "company:sec-cik:0001058290"
SUBMISSIONS = {
    "cik": "0001058290", "name": "COGNIZANT TECHNOLOGY SOLUTIONS CORP",
    "filings": {"recent": {
        "form": ["10-Q", "8-K", "10-Q", "10-K", "10-Q", "10-Q", "10-Q"],
        "accessionNumber": ["0001058290-26-000031", "0001058290-26-000030",
                            "0001058290-26-000016", "0001058290-26-000008",
                            "0001058290-25-000341", "0001058290-25-000267",
                            "0001058290-25-000125"],
        "filingDate": ["2026-07-29", "2026-07-29", "2026-04-29", "2026-02-12",
                       "2025-10-29", "2025-07-31", "2025-05-01"],
        "reportDate": ["2026-06-30", "2026-06-30", "2026-03-31", "2025-12-31",
                       "2025-09-30", "2025-06-30", "2025-03-31"],
    }},
}
# 2026-09-28: the fourth quarter is held too, so these fixtures keep asking
# about the 10-Q they were written for (the 10-K has its own tests below).
CTSH_HELD = ("2025-01-01..2025-03-31", "2025-04-01..2025-06-30",
             "2025-07-01..2025-09-30", "2025-10-01..2025-12-31", "2026-01-01..2026-03-31")


def _ctsh_entry() -> dict:
    # The checklist counted every quantitative Claim period -- thousands once
    # statement lines were promoted -- so it always said "complete".
    return {"company_ref": CTSH, "ticker": "CTSH",
            "items": [{"item_ref": "quarterly_financials", "have": 412, "required": 4,
                       "status": "complete"}]}


class NewestQuarterTests(unittest.TestCase):
    """2026-09-24: CTSH's Q2 10-Q was observed and never dispatched."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def spool(self, payload) -> str:
        raw = json.dumps(payload).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / "transcript-spool" / "connector-spool" / "objects" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return digest

    def run_once(self, store, entry, missions=None):
        missions = missions or _Missions()
        result = MissionSecQuartersCoordinator(
            store=store, missions=missions, state_dir=self.root,
            checklist=lambda: [entry],
            clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc),
        ).dispatch_once()
        return result, missions

    def test_submissions_list_only_quarterly_reports(self) -> None:
        filings = submissions_filings(SUBMISSIONS)
        self.assertEqual([(f["end"], f["form"]) for f in filings],
                         [("2026-06-30", "10-Q"), ("2026-03-31", "10-Q"), ("2025-12-31", "10-K"),
                          ("2025-09-30", "10-Q"), ("2025-06-30", "10-Q"), ("2025-03-31", "10-Q")])
        # The index does not say whether a 10-K reports its fourth quarter.
        self.assertIsNone(filings[2]["fourth_quarter"])
        self.assertEqual(filings[0]["accession"], "0001058290-26-000031")
        self.assertEqual(submissions_filings({}), [])
        self.assertEqual(submissions_filings({"filings": {"recent": {"form": "10-Q"}}}), [])

    def test_a_complete_checklist_does_not_hide_a_missing_newest_quarter(self) -> None:
        digest = self.spool(SUBMISSIONS)
        result, missions = self.run_once(
            _Store(artifacts=(digest,), periods=CTSH_HELD), _ctsh_entry())
        self.assertEqual(result["status"], "queued")
        self.assertEqual([q["accession"] for q in result["queued"]], ["0001058290-26-000031"])
        self.assertEqual(result["recent_quarters_missing"], ["2026-06-30"])
        queued = missions.queued[0]
        self.assertEqual((queued["filed_from"], queued["filed_to"]), ("2026-07-27", "2026-07-31"))
        self.assertEqual(queued["observation_ref"], f"sec-submissions-artifact:{digest}")

    def test_company_facts_behind_a_newer_submissions_payload_are_still_found(self) -> None:
        facts = self.spool(PAYLOAD)
        listed = self.spool({"filings": {"recent": {
            "form": [], "accessionNumber": [], "filingDate": [], "reportDate": []}}})
        result, missions = self.run_once(
            _Store(artifacts=(listed, facts)), _entry(9))
        self.assertEqual([q["accession"] for q in result["queued"]],
                         ["0001467373-26-000032", "0001467373-26-000014"])
        self.assertEqual(missions.queued[0]["observation_ref"],
                         f"sec-company-facts-artifact:{facts}")

    def test_the_statement_lane_filings_are_an_observation_too(self) -> None:
        # ws-7d: no SEC artifact behind any Claim at all, but the statement
        # lane had ingested each company's newest 10-Q.
        store = _Store(statement_filings=({
            "ingest_id": "statement-ingest:abc", "accession": "0001018724-26-000026",
            "filed": "2026-07-31", "report_date": "2026-06-30"},))
        entry = {"company_ref": "company:ticker:amzn", "ticker": "AMZN",
                 "items": [{"item_ref": "quarterly_financials", "have": 0, "required": 4}]}
        result, missions = self.run_once(store, entry)
        self.assertEqual([q["accession"] for q in result["queued"]], ["0001018724-26-000026"])
        self.assertEqual(missions.queued[0]["observation_ref"], "statement-ingest:statement-ingest:abc")

    def test_another_metric_for_the_same_quarter_does_not_count_as_held(self) -> None:
        digest = self.spool(SUBMISSIONS)
        result, _ = self.run_once(_Store(
            artifacts=(digest,), periods=CTSH_HELD,
            other_metric_periods=("2026-04-01..2026-06-30",)), _ctsh_entry())
        self.assertEqual([q["accession"] for q in result["queued"]], ["0001058290-26-000031"])

    def test_only_the_newest_four_quarters_are_chased(self) -> None:
        digest = self.spool(SUBMISSIONS)
        # The newest four are held; the fifth and sixth (2025-06-30, 2025-03-31)
        # are not and never matter.
        held = ("2025-07-01..2025-09-30", "2025-10-01..2025-12-31",
                "2026-01-01..2026-03-31", "2026-04-01..2026-06-30")
        result, missions = self.run_once(_Store(artifacts=(digest,), periods=held), _ctsh_entry())
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        # And an exhausted newest quarter is not replaced by an older one.
        exhausted = _Store(artifacts=(digest,), periods=CTSH_HELD,
                           attempts={"0001058290-26-000031": 3})
        result, missions = self.run_once(exhausted, _ctsh_entry())
        self.assertEqual((result["status"], missions.queued), ("idle", []))

    def test_answered_statement_filings_skip_reading_artifacts(self) -> None:
        store = _Store(artifacts=("f" * 64,), periods=CTSH_HELD + ("2026-04-01..2026-06-30",),
                       statement_filings=tuple({
                           "ingest_id": f"statement-ingest:{end}", "accession": accession,
                           "filed": filed, "report_date": end}
                           for accession, filed, end in (
                               ("0001058290-26-000031", "2026-07-29", "2026-06-30"),
                               ("0001058290-26-000016", "2026-04-29", "2026-03-31"),
                               ("0001058290-25-000341", "2025-10-29", "2025-09-30"),
                               ("0001058290-25-000267", "2025-07-31", "2025-06-30"))))
        result, _ = self.run_once(store, _ctsh_entry())
        # "f"*64 is not in the spool: had it been opened the reason would say so.
        self.assertEqual(result["status"], "idle")
        self.assertIn("都已入账", json.dumps(result["skipped"], ensure_ascii=False))


CTSH_Q2 = "0001058290-26-000031"
GOVERNANCE_LINE = (
    "lane precondition failed: active Core governance policy 'policy-4' does not "
    "authorize the SEC company-facts lane; the lane never installs governance policy "
    "itself. Install a new policy version through the governance CLI with: "
    "research_plan_auto_start must be {enabled: true, rules: "
    "['research-plan-auto-start:sec-public-company-facts:v1', ...known rules]}")
LAG_MESSAGE = "SEC company facts has no 10-Q accession in the filing window"


class FailuresThatAreNotTheFilingsTests(unittest.TestCase):
    """2026-09-26: governance and source lag stopped spending a filing's budget.

    ws-7d's policy lacked ``research_plan_auto_start``; every run refused at the
    lane precondition and the coordinator queued on regardless, burning the 47
    attempts that had just been given back.  Legacy CTSH's 10-Q was listed by
    EDGAR but not yet in company facts, and two of its three attempts went on
    "no 10-Q accession in the filing window".
    """

    setUp = NewestQuarterTests.setUp
    spool = NewestQuarterTests.spool
    run_once = NewestQuarterTests.run_once

    def run_at(self, store, entry, when, missions=None):
        missions = missions or _Missions()
        result = MissionSecQuartersCoordinator(
            store=store, missions=missions, state_dir=self.root,
            checklist=lambda: [entry], clock=lambda: when,
        ).dispatch_once()
        return result, missions

    def ticket(self, name, *, log=None, summary=None) -> str:
        directory = self.root / "sec-lane-runs" / name
        directory.mkdir(parents=True)
        if log is not None:
            (directory / "run.log").write_text(log, encoding="utf-8")
        if summary is not None:
            (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return f"sec-lane-run:{name}"

    @staticmethod
    def settled(accession, ticket_ref, *, at="2026-09-23T12:00:00+00:00", reason=None):
        return {"dispatch_id": f"mission-sec-dispatch:{ticket_ref[-6:]}",
                "expected_accession": accession, "status": "launched",
                "ticket_ref": ticket_ref, "dispatch_reason": None,
                "updated_at": at, "settled_reason": reason, "settled_at": at}

    def lag_summary(self, envelope_ref):
        return {"ok": False, "issuers": [{"status": "blocked", "failure": {
            "status": "failed", "result_envelope_ref": envelope_ref}}]}

    def test_an_unauthorized_lane_is_held_before_anything_is_queued(self) -> None:
        digest = self.spool(SUBMISSIONS)
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, policy={
            "research_candidate_auto_commit": AUTHORIZED_POLICY["research_candidate_auto_commit"]})
        result, missions = self.run_once(store, _ctsh_entry())
        self.assertEqual(result["status"], "held")
        self.assertIn("research_plan_auto_start", result["reason"])
        self.assertEqual(missions.queued, [])

    def test_an_unreadable_policy_holds_too(self) -> None:
        class Broken(_Store):
            def active_policy(self):
                raise LookupError("no pointer")

        digest = self.spool(SUBMISSIONS)
        result, missions = self.run_once(Broken(artifacts=(digest,), periods=CTSH_HELD),
                                         _ctsh_entry())
        self.assertEqual((result["status"], missions.queued), ("held", []))

    def test_runs_refused_by_governance_are_not_attempts(self) -> None:
        digest = self.spool(SUBMISSIONS)
        # Three runs, all dead at the lane precondition, which left only a log.
        runs = tuple(self.settled(CTSH_Q2, self.ticket(f"gov{i}", log=GOVERNANCE_LINE + "\n"))
                     for i in range(3))
        store = _Store(artifacts=(digest,), periods=CTSH_HELD,
                       attempts={CTSH_Q2: 3}, failed_runs=runs)
        result, missions = self.run_once(store, _ctsh_entry())
        self.assertEqual(result["status"], "queued")
        self.assertEqual([q["attempt"] for q in result["queued"]], [1])
        # The window still widens past the three already used.
        self.assertEqual(len(missions.queued), 1)

    def test_a_rejection_because_the_mission_moved_is_not_an_attempt(self) -> None:
        digest = self.spool(SUBMISSIONS)
        rejected = {"dispatch_id": "mission-sec-dispatch:r", "expected_accession": CTSH_Q2,
                    "status": "rejected", "ticket_ref": None,
                    "dispatch_reason": "CoverageMissionConflict: SEC automation must bind "
                                       "the active mission version",
                    "updated_at": "2026-09-25T06:18:38+00:00",
                    "settled_reason": None, "settled_at": None}
        other = dict(rejected, dispatch_id="mission-sec-dispatch:o",
                     dispatch_reason="CoverageMissionConflict: SEC automation company/ticker "
                                     "is outside the mission universe")
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, attempts={CTSH_Q2: 3},
                       failed_runs=(rejected, other))
        result, _ = self.run_once(store, _ctsh_entry())
        # One excused, one still counting: 2 of 3 used.
        self.assertEqual([q["attempt"] for q in result["queued"]], [3])

    def test_a_real_failure_still_counts(self) -> None:
        digest = self.spool(SUBMISSIONS)
        runs = tuple(self.settled(CTSH_Q2, self.ticket(f"bad{i}"), reason=(
            "AuthorityResolutionConflict: adapter structured output does not match"))
            for i in range(3))
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, attempts={CTSH_Q2: 3},
                       failed_runs=runs)
        result, missions = self.run_once(store, _ctsh_entry())
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        self.assertIn(f"已经试过 {MAX_ATTEMPTS_PER_FILING} 次",
                      json.dumps(result["skipped"], ensure_ascii=False))

    def test_source_lag_is_not_an_attempt_and_is_retried_later(self) -> None:
        digest = self.spool(SUBMISSIONS)
        envelopes = {f"result-envelope:{i}": {"status": "failed", "error": {
            "code": "normalization_error", "message": LAG_MESSAGE, "retryable": False}}
            for i in range(2)}
        runs = (
            self.settled(CTSH_Q2, self.ticket("lag0", summary=self.lag_summary("result-envelope:0")),
                         at="2026-09-26T12:31:04+00:00"),
            self.settled(CTSH_Q2, self.ticket("lag1", summary=self.lag_summary("result-envelope:1")),
                         at="2026-09-26T12:41:06+00:00"),
        )
        # Legacy CTSH: one mission-drift rejection plus the two lag failures.
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, attempts={CTSH_Q2: 3},
                       failed_runs=runs, envelopes=envelopes)
        # Ten minutes later: not yet -- two lag failures back off two days.
        soon = datetime(2026, 9, 26, 12, 51, tzinfo=timezone.utc)
        result, missions = self.run_at(store, _ctsh_entry(), soon)
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        text = json.dumps(result["skipped"], ensure_ascii=False)
        self.assertIn("还没收录", text)
        self.assertIn("2026-09-28T12:41", text)
        # After the backoff it is tried again, and the budget shows the lag
        # failures were not spent: of three dispatches only one still counts.
        later = datetime(2026, 9, 28, 12, 42, tzinfo=timezone.utc)
        result, missions = self.run_at(store, _ctsh_entry(), later)
        self.assertEqual(result["status"], "queued")
        self.assertEqual([q["attempt"] for q in result["queued"]], [2])

    def test_the_settled_reason_is_used_before_the_disk(self) -> None:
        self.assertEqual(classify_failure(LAG_MESSAGE), "source_lag")
        self.assertEqual(classify_failure(GOVERNANCE_LINE), "governance")
        self.assertEqual(classify_failure(
            "SEC company facts has no 10-K accession in the filing window"), "source_lag")
        self.assertIsNone(classify_failure("SEC company facts latest 10-Q accession is ambiguous"))
        self.assertIsNone(classify_failure(None))

    def test_the_lag_backoff_doubles_and_is_capped_at_a_week(self) -> None:
        first = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertIsNone(source_lag_retry_at([]))
        self.assertEqual(source_lag_retry_at([first]), first + timedelta(days=1))
        self.assertEqual(source_lag_retry_at([first] * 2), first + timedelta(days=2))
        self.assertEqual(source_lag_retry_at([first] * 3), first + timedelta(days=4))
        self.assertEqual(source_lag_retry_at([first] * 9), first + timedelta(days=7))


    # 2026-09-27, ws-7d: GOOGL 0001652044-25-000062 failed on "connector
    # transport exceeded the authority deadline" (envelope code
    # deadline_exceeded, sec-lane-run a1ba4288) and that counted, 2 of 3.

    def deadline_envelopes(self, count, *, message=(
            "connector transport exceeded the authority deadline")):
        return {f"result-envelope:t{i}": {"status": "retryable", "error": {
            "code": "deadline_exceeded", "message": message, "retryable": True}}
            for i in range(count)}

    def test_a_transport_timeout_is_not_an_attempt_and_backs_off(self) -> None:
        digest = self.spool(SUBMISSIONS)
        runs = (
            self.settled(CTSH_Q2, self.ticket("t0", summary=self.lag_summary("result-envelope:t0")),
                         at="2026-09-27T09:00:00+00:00"),
            self.settled(CTSH_Q2, self.ticket("t1", summary=self.lag_summary("result-envelope:t1")),
                         at="2026-09-27T09:54:02+00:00"),
        )
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, attempts={CTSH_Q2: 3},
                       failed_runs=runs, envelopes=self.deadline_envelopes(2))
        # Two timeouts: the second waits an hour (30 min doubled), not a tick.
        soon = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)
        result, missions = self.run_at(store, _ctsh_entry(), soon)
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        text = json.dumps(result["skipped"], ensure_ascii=False)
        self.assertIn("超时", text)
        self.assertIn("2026-09-27T10:54", text)
        # After it, retried -- and of three dispatches only one still counts.
        later = datetime(2026, 9, 27, 10, 55, tzinfo=timezone.utc)
        result, missions = self.run_at(store, _ctsh_entry(), later)
        self.assertEqual(result["status"], "queued")
        self.assertEqual([q["attempt"] for q in result["queued"]], [2])

    def test_a_deadline_is_recognised_by_its_code_or_either_message(self) -> None:
        self.assertEqual(classify_failure(
            "connector transport exceeded the authority deadline"), "transport_timeout")
        self.assertEqual(classify_failure(
            "connector transport completed after the authority deadline"),
            "transport_timeout")
        self.assertEqual(classify_failure(
            "recorded source page timed out [deadline_exceeded]"), "transport_timeout")
        # A filing that is genuinely bad is still the filing's.
        self.assertIsNone(classify_failure(
            "AuthorityResolutionConflict: adapter structured output does not match"))
        self.assertIsNone(classify_failure("the plan deadline passed"))
        # The envelope's code reaches the classifier even when the message
        # does not spell it.
        digest = self.spool(SUBMISSIONS)
        runs = tuple(
            self.settled(CTSH_Q2, self.ticket(f"c{i}", summary=self.lag_summary(
                f"result-envelope:t{i}")), at="2026-09-20T00:00:00+00:00")
            for i in range(3))
        store = _Store(artifacts=(digest,), periods=CTSH_HELD, attempts={CTSH_Q2: 3},
                       failed_runs=runs, envelopes=self.deadline_envelopes(
                           3, message="recorded source page timed out"))
        result, _ = self.run_at(store, _ctsh_entry(),
                                datetime(2026, 9, 27, tzinfo=timezone.utc))
        self.assertEqual([q["attempt"] for q in result["queued"]], [1])

    def test_the_transport_backoff_doubles_and_is_capped_at_six_hours(self) -> None:
        first = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertIsNone(transport_retry_at([]))
        self.assertEqual(transport_retry_at([first]), first + timedelta(minutes=30))
        self.assertEqual(transport_retry_at([first] * 2), first + timedelta(hours=1))
        self.assertEqual(transport_retry_at([first] * 5), first + timedelta(hours=6))
        self.assertEqual(transport_retry_at([first] * 400), first + timedelta(hours=6))


# 2026-09-28: ws-7d signed the annual rule and the lane still said
# ``idle skipped 4``; nothing here ever looked at a 10-K.
ACN_10K = "0001467373-25-000217"
ACN_ANNUAL = {"cik": 1467373, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
    # ACN's fiscal year ends 31 August.  Its 10-K reports the year and, in its
    # quarterly note, every quarter of it and of the year before.
    {"form": "10-K", "start": "2024-09-01", "end": "2025-08-31", "accn": ACN_10K,
     "filed": "2025-10-10", "val": 69672977000, "fp": "FY"},
    {"form": "10-K", "start": "2023-09-01", "end": "2024-08-31", "accn": ACN_10K,
     "filed": "2025-10-10", "val": 64896000000, "fp": "FY"},
    {"form": "10-K", "start": "2025-06-01", "end": "2025-08-31", "accn": ACN_10K,
     "filed": "2025-10-10", "val": 17596260000, "fp": "FY"},
    {"form": "10-K", "start": "2025-03-01", "end": "2025-05-31", "accn": ACN_10K,
     "filed": "2025-10-10", "val": 17728000000, "fp": "FY"},
    {"form": "10-K", "start": "2024-06-01", "end": "2024-08-31", "accn": ACN_10K,
     "filed": "2025-10-10", "val": 16405819000, "fp": "FY"},
    {"form": "10-Q", "start": "2026-03-01", "end": "2026-05-31",
     "accn": "0001467373-26-000032", "filed": "2026-06-18", "val": 1},
    {"form": "10-Q", "start": "2025-12-01", "end": "2026-02-28",
     "accn": "0001467373-26-000014", "filed": "2026-03-19", "val": 1},
    {"form": "10-Q", "start": "2025-09-01", "end": "2025-11-30",
     "accn": "0001467373-25-000222", "filed": "2025-12-18", "val": 1},
    {"form": "10-Q", "start": "2025-03-01", "end": "2025-05-31",
     "accn": "0001467373-25-000150", "filed": "2025-06-20", "val": 1},
]}}}}}
ACN_10Q_HELD = ("2025-09-01..2025-11-30", "2025-12-01..2026-02-28", "2026-03-01..2026-05-31")

MSFT = "company:ticker:msft"
MSFT_10K = "0001193125-26-323660"
# MSFT's fiscal year ends 30 June, and its 10-K reports only fiscal years.
MSFT_ANNUAL_ONLY = {"cik": 789019, "facts": {"us-gaap": {
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        {"form": "10-K", "start": "2025-07-01", "end": "2026-06-30", "accn": MSFT_10K,
         "filed": "2026-07-29", "val": 1, "fp": "FY"},
        {"form": "10-K", "start": "2024-07-01", "end": "2025-06-30", "accn": MSFT_10K,
         "filed": "2026-07-29", "val": 1, "fp": "FY"},
        {"form": "10-Q", "start": "2026-01-01", "end": "2026-03-31",
         "accn": "0001193125-26-191507", "filed": "2026-04-29", "val": 1},
        {"form": "10-Q", "start": "2025-10-01", "end": "2025-12-31",
         "accn": "0001193125-26-027207", "filed": "2026-01-28", "val": 1},
        {"form": "10-Q", "start": "2025-07-01", "end": "2025-09-30",
         "accn": "0001193125-25-256321", "filed": "2025-10-29", "val": 1},
    ]}}}}}
MSFT_10Q_HELD = ("2025-07-01..2025-09-30", "2025-10-01..2025-12-31", "2026-01-01..2026-03-31")


class FourthQuarterTests(unittest.TestCase):
    """The quarter a 10-K reports is one of the newest four, whatever the fiscal year."""

    setUp = NewestQuarterTests.setUp
    spool = NewestQuarterTests.spool
    run_once = NewestQuarterTests.run_once

    @staticmethod
    def entry(company_ref, ticker):
        return {"company_ref": company_ref, "ticker": ticker,
                "items": [{"item_ref": "quarterly_financials", "have": 300, "required": 4}]}

    def test_a_10k_stands_for_the_quarter_ending_on_its_fiscal_year_end(self) -> None:
        filings = quarterly_filings(ACN_ANNUAL)
        fourth = next(item for item in filings if item["form"] == "10-K")
        self.assertEqual((fourth["accession"], fourth["period"], fourth["fourth_quarter"]),
                         (ACN_10K, "2025-06-01..2025-08-31", True))
        # Its other quarterly rows are comparatives, not quarters to chase.
        self.assertEqual([item["form"] for item in filings].count("10-K"), 1)
        self.assertEqual([item["end"] for item in filings][:4],
                         ["2026-05-31", "2026-02-28", "2025-11-30", "2025-08-31"])
        msft = next(item for item in quarterly_filings(MSFT_ANNUAL_ONLY) if item["form"] == "10-K")
        self.assertEqual((msft["end"], msft["start"], msft["fourth_quarter"]),
                         ("2026-06-30", None, False))

    def test_a_missing_fourth_quarter_is_queued_as_a_10k(self) -> None:
        digest = self.spool(ACN_ANNUAL)
        result, missions = self.run_once(
            _Store(artifacts=(digest,), periods=ACN_10Q_HELD), self.entry(ACN, "ACN"))
        self.assertEqual(result["status"], "queued", result)
        self.assertEqual(result["recent_quarters_missing"], ["2025-08-31"])
        self.assertEqual([(q["accession"], q["form"]) for q in result["queued"]],
                         [(ACN_10K, "10-K")])
        [queued] = missions.queued
        # Form 10-K selects COMPANY_FACTS_RULE_REFS["10-K"] in the lane.
        self.assertEqual(queued["form"], "10-K")
        self.assertEqual(queued["expected_accession"], ACN_10K)
        self.assertEqual((queued["filed_from"], queued["filed_to"]), ("2025-10-08", "2025-10-12"))
        self.assertEqual(queued["observation_ref"], f"sec-company-facts-artifact:{digest}")
        # Held, it is done: the fourth quarter is one of the four.
        done, missions = self.run_once(
            _Store(artifacts=(digest,), periods=ACN_10Q_HELD + ("2025-06-01..2025-08-31",)),
            self.entry(ACN, "ACN"))
        self.assertEqual((done["status"], missions.queued), ("idle", []))
        self.assertIn("都已入账", json.dumps(done["skipped"], ensure_ascii=False))

    def test_a_10k_of_year_totals_only_is_not_queued(self) -> None:
        """MSFT/AMZN/GOOGL/META: the 10-K has no Q4 row; FY - 9M is not a rule."""

        digest = self.spool(MSFT_ANNUAL_ONLY)
        # The statement lane lists the 10-K too; company facts say what is in it.
        store = _Store(artifacts=(digest,), periods=MSFT_10Q_HELD, statement_filings=({
            "ingest_id": "statement-ingest:k", "accession": MSFT_10K, "form": "10-K",
            "filed": "2026-07-29", "report_date": "2026-06-30"},))
        result, missions = self.run_once(store, self.entry(MSFT, "MSFT"))
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        text = json.dumps(result["skipped"], ensure_ascii=False)
        self.assertIn(MSFT_10K, text)
        self.assertIn("只报全年数", text)

    def test_a_10k_only_the_index_lists_is_queued_once_and_not_after_it_proves_annual(self) -> None:
        digest = self.spool(SUBMISSIONS)
        held = ("2025-07-01..2025-09-30", "2026-01-01..2026-03-31", "2026-04-01..2026-06-30")
        result, missions = self.run_once(_Store(artifacts=(digest,), periods=held), _ctsh_entry())
        self.assertEqual([(q["accession"], q["form"]) for q in result["queued"]],
                         [("0001058290-26-000008", "10-K")])
        # The run found only fiscal-year totals: that ends the chase.
        runs = ({"dispatch_id": "mission-sec-dispatch:k", "form": "10-K",
                 "expected_accession": "0001058290-26-000008", "status": "launched",
                 "ticket_ref": "sec-lane-run:k", "dispatch_reason": None,
                 "updated_at": "2026-09-28T00:00:00+00:00", "settled_at": "2026-09-28T00:00:00+00:00",
                 "settled_reason": "SecPublicAdapterError: no allowlisted revenue concept "
                                   "resolves on the latest 10-K accession"},)
        store = _Store(artifacts=(digest,), periods=held, failed_runs=runs,
                       attempts={"0001058290-26-000008": 1})
        result, missions = self.run_once(store, _ctsh_entry())
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        self.assertIn("只报全年数", json.dumps(result["skipped"], ensure_ascii=False))

    def test_a_10k_the_planner_path_has_queued_is_left_to_it(self) -> None:
        digest = self.spool(ACN_ANNUAL)
        result, missions = self.run_once(
            _Store(artifacts=(digest,), periods=ACN_10Q_HELD, open_dispatches=1),
            self.entry(ACN, "ACN"))
        self.assertEqual((result["status"], missions.queued), ("idle", []))
        self.assertIn("在队列里等着跑", json.dumps(result["skipped"], ensure_ascii=False))

    def test_accession_in_hand_sees_open_and_succeeded_dispatches_only(self) -> None:
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE coverage_mission_sec_dispatches(dispatch_id TEXT, form TEXT, "
            "status TEXT, expected_accession TEXT, created_at TEXT);"
            "CREATE TABLE coverage_mission_sec_dispatch_settlements(dispatch_id TEXT, detail TEXT);")
        rows = (("d-failed", "launched", "failed"), ("d-open", "pending", None),
                ("d-ok", "launched", "succeeded"), ("d-rejected", "rejected", None))
        for index, (dispatch_id, status, detail) in enumerate(rows):
            connection.execute("INSERT INTO coverage_mission_sec_dispatches VALUES(?,?,?,?,?)",
                               (dispatch_id, "10-K", status, f"acc-{index}", "2026-09-28"))
            if detail:
                connection.execute(
                    "INSERT INTO coverage_mission_sec_dispatch_settlements VALUES(?,?)",
                    (dispatch_id, detail))
        self.assertIsNone(accession_in_hand(connection, "acc-0"))
        self.assertEqual(accession_in_hand(connection, "acc-1")["state"], "open")
        self.assertEqual(accession_in_hand(connection, "acc-2")["state"], "succeeded")
        self.assertIsNone(accession_in_hand(connection, "acc-3"))
        self.assertIsNone(accession_in_hand(connection, "nope"))


if __name__ == "__main__":
    unittest.main()
