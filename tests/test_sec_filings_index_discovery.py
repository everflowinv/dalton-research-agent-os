"""P10s: the SEC filings index as a mission discovery, end to end."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from dalton_core.connector_governance import ConnectorGovernance, build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import validate_discovery_plan
from dalton_core.sec_filings_index_cli import run_discovery
from dalton_core.store import DaltonStore, content_hash

from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

OWNER = "human:lumos"
ACN = "company:sec-cik:0001467373"
CIK = "0001467373"


def submissions_body() -> bytes:
    rows = [
        ("0001467373-25-000217", "10-K", "2025-10-10", "acn-20250831.htm", None),
        ("0001467373-25-000100", "8-K", "2025-09-02", "event.htm", None),
        ("0001467373-25-000001", "10-K", "2025-08-04", "older.htm", None),
    ]
    return json.dumps({"cik": CIK, "filings": {"recent": {
        "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows],
        "amendmentOf": [r[4] for r in rows],
    }}}).encode()


class _Response:
    status = 200
    reason = "OK"

    def __init__(self, body: bytes) -> None:
        self._body = body

    def getheaders(self):
        return [("content-type", "application/json"),
                ("content-length", str(len(self._body)))]

    def read(self, _amount=None):
        body, self._body = self._body, b""
        return body

    def close(self):
        return None


def fixture_adapter():
    from dalton_core.public_http_transport import PublicHttpTransport
    from dalton_core.sec_public_adapter import SecPublicHttpAdapter

    return SecPublicHttpAdapter(
        transport=PublicHttpTransport(
            resolver=lambda _h, _p: ("93.184.216.34",),
            exchange=lambda *_a, **_k: _Response(submissions_body()),
        ),
    )


def sec_plan() -> dict:
    body = {
        "schema_version": "0.4",
        "id": "discovery-plan:us-it-services:sec-filings:1",
        "created_at": "2026-09-08T00:00:00.000000+00:00",
        "mission_ref": "coverage-mission:us-it-services",
        "source_ref": "source:sec-edgar",
        "budget": {"max_calls_24h": 50},
        "companies": {ACN: {"cik": CIK}},
        "specs": [{
            "spec_ref": "annual-report-10k", "form": "10-K",
            "lookback_days": 400, "rediscovery_interval_days": 30,
            "retry_interval_days": 2,
        }],
    }
    body["content_hash"] = content_hash(
        {k: v for k, v in body.items() if k != "content_hash"}
    )
    return validate_discovery_plan(body)


class SecFilingsIndexDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(parents=True)
        core = DaltonStore(str(self.state / "core.sqlite"))
        authorities = bootstrap_method_authorities(core)
        missions = CoverageMissionAuthority(core)
        params = mission_params(authorities)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + [
            "source_discovery"
        ]
        for entry in params["source_plan"]:
            if entry["source_ref"] == "source:sec-edgar":
                entry["status"] = "connected"
        ref = params.pop("mission_ref")
        self.mission = missions.create_mission(ref, **params)
        self.principal = self.mission["autonomy"]["automation_principal"]
        core.close()
        self.governance = ConnectorGovernance(
            build_governance_record(
                "sec-filings-index", approved_by=OWNER, status="approved"
            )
        )

    def run_once(self, **overrides):
        kwargs = dict(
            state_dir=self.state,
            governance=self.governance,
            plan=sec_plan(),
            company_ref=ACN,
            spec_ref="annual-report-10k",
            requested_by=self.principal,
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            as_of=date(2026, 9, 8),
            summary_dir=self.state / "summaries",
            adapter=fixture_adapter(),
        )
        kwargs.update(overrides)
        return run_discovery(**kwargs)

    def test_the_index_becomes_a_mission_discovery_with_fetchable_documents(self) -> None:
        summary = self.run_once()
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])
        self.assertEqual(summary["provider_calls"], 1)
        self.assertTrue(summary["discovery_ref"])
        # Only the two 10-Ks; the 8-K in the same block is not the mission's ask.
        self.assertEqual(summary["document_count"], 2)
        self.assertEqual({f["form"] for f in summary["filings"]}, {"10-K"})
        self.assertIn(
            "https://www.sec.gov/Archives/edgar/data/1467373/"
            "000146737325000217/acn-20250831.htm",
            [f["canonical_url"] for f in summary["filings"]],
        )

        # The documents are queued under the mission for the acquisition lane,
        # which is the whole point: the index names them, fetch retrieves them.
        core = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(core.close)
        rows = core.connection.execute(
            "SELECT document_ref, status, host, source_ref FROM "
            "coverage_mission_discovered_documents WHERE company_ref=?",
            (ACN,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"discovered"})
        self.assertEqual({row["host"] for row in rows}, {"www.sec.gov"})
        self.assertEqual({row["source_ref"] for row in rows}, {"source:sec-edgar"})
        # The queue names the filing, not the URL: a queued document has to be
        # one record the envelope actually returned.
        self.assertEqual(
            {row["document_ref"] for row in rows},
            {"sec:filing:0001467373-25-000217", "sec:filing:0001467373-25-000001"},
        )
        # The URL is still known, carried for the acquisition step.
        self.assertEqual(
            {f["record_ref"] for f in summary["filings"]},
            {row["document_ref"] for row in rows},
        )

    def test_a_rerun_does_not_queue_the_same_filing_twice(self) -> None:
        first = self.run_once()
        self.assertEqual(first["status"], "succeeded")
        second = self.run_once()
        self.assertEqual(second["status"], "succeeded", second["failure_reason"])
        # The child reads the source every time it is asked to; holding back a
        # repeat is the coordinator's cadence, not the child's business. What
        # the child must not do is queue the same filing twice -- that would
        # have the fetch lane pay for the same 10-K on every discovery pass.
        self.assertEqual(second["provider_calls"], 1)
        core = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(core.close)
        count = core.connection.execute(
            "SELECT COUNT(*) FROM coverage_mission_discovered_documents WHERE company_ref=?",
            (ACN,),
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_a_company_outside_the_plan_is_refused_not_guessed(self) -> None:
        summary = self.run_once(company_ref="company:sec-cik:0000051143")
        self.assertEqual(summary["status"], "failed")
        self.assertIsNotNone(summary["failure_reason"])

    def test_the_company_facts_approval_cannot_drive_this_lane(self) -> None:
        other = ConnectorGovernance(
            build_governance_record(
                "sec-company-facts", approved_by=OWNER, status="approved"
            )
        )
        summary = self.run_once(governance=other)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("filings-index capability", summary["failure_reason"])

    def test_the_summary_is_written_for_the_parent_even_on_failure(self) -> None:
        self.run_once(company_ref="company:sec-cik:0000051143")
        written = json.loads(
            (self.state / "summaries" / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(written["status"], "failed")
        self.assertEqual(written["source_ref"], "source:sec-edgar")


if __name__ == "__main__":
    unittest.main()
