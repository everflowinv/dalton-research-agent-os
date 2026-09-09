"""Shared fixtures for P14a: a Core with a mission, a passed screen and prices."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
EPAM = "company:sec-cik:0001352010"
IBM = "company:sec-cik:0000051143"
DXC = "company:sec-cik:001688568"
UNIVERSE = (ACN, CTSH, EPAM, IBM, DXC)
AUTOMATION = "automation:coverage-mission"
OWNER = "human:lumos"
INVOCATION = "connector-invocation:yfinance:" + "a" * 32
ARTIFACT = "1" * 64
GOVERNANCE = "connector-governance:yfinance-daily-prices:v1"
GOVERNANCE_HASH = "2" * 64
SETTLED_AT = "2026-09-30T23:30:00+00:00"


def bar(date: str, close: str = "100", **overrides: Any) -> dict[str, Any]:
    row = {
        "date": date, "open": close, "high": str(float(close) + 2),
        "low": str(float(close) - 2), "close": close, "adj_close": close,
        "volume": "1000000",
    }
    row.update(overrides)
    return row


class P14aHarness(unittest.TestCase):
    """A Core on disk (the lanes spawn against a state directory) with a mission."""

    grants: tuple[str, ...] = ("market_event", "observation", "stage_record", "deliverable")

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.playbook = self.state["playbook"]
        self.missions = CoverageMissionAuthority(self.store)
        self.params = mission_params(self.state)
        self.mission_ref = self.params.pop("mission_ref")
        self.mission = self.missions.create_mission(self.mission_ref, **self.params)
        self._version = 1
        self._seq = 0
        # Accumulated, not rebuilt from the manifest each time: a second grant
        # that dropped the first would republish a mission the events already
        # recorded under the old one cannot be written against.
        self._scopes: list[str] = []
        self._checkpoints: list[str] = []
        if self.grants:
            self.grant(*self.grants)

    def grant(self, *scopes: str, checkpoints: tuple[str, ...] = ()) -> dict[str, Any]:
        self._scopes = list(dict.fromkeys(self._scopes + list(scopes)))
        self._checkpoints = list(dict.fromkeys(self._checkpoints + list(checkpoints)))
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = list(
            dict.fromkeys(list(autonomy["may_write"]) + self._scopes)
        )
        autonomy["human_checkpoints"] = list(
            dict.fromkeys(list(autonomy["human_checkpoints"]) + self._checkpoints)
        )
        params["autonomy"] = autonomy
        self._version += 1
        params.update({
            "version_id": f"coverage-mission-version:us-it-services:{self._version}",
            "prior_version_ref": self.mission["id"],
            "idempotency_key": f"coverage-mission:us-it-services:{self._version}",
        })
        self.mission = self.missions.create_mission(self.mission_ref, **params)
        return self.mission

    # -- stage records -----------------------------------------------------

    def enter_screen(self, company_ref: str) -> dict[str, Any]:
        return self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=company_ref, stage_ref="initial_screen", status="entered",
            evidence_refs=[self.mission["id"]], rationale="fixture",
            actor_ref=AUTOMATION,
            idempotency_key=f"{self.mission['id']}:{company_ref}:initial_screen:entered",
        )

    def pass_screen(self, company_ref: str) -> dict[str, Any]:
        self.enter_screen(company_ref)
        return self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=company_ref, stage_ref="initial_screen", status="gate_passed",
            evidence_refs=[self.mission["id"]], rationale="fixture",
            actor_ref=AUTOMATION,
            idempotency_key=f"{self.mission['id']}:{company_ref}:initial_screen:passed",
        )

    # -- ledger rows -------------------------------------------------------

    def claim(
        self, *, subject: str = ACN, statement: str = "管理层说需求在改善。",
        value: Any = None, created_at: str = "2026-09-09T00:00:00+00:00",
    ) -> str:
        self._seq += 1
        n = self._seq
        claim = {
            "schema_version": "0.2", "id": f"claim-version:{n:064d}",
            "claim_ref": f"claim:test:{n}", "version": 1, "subject_ref": subject,
            "metric_or_aspect": "aspect:test", "period": "2026Q2", "basis": "fixture",
            "normalized_statement": statement,
            "claim_kind": "quantitative" if value is not None else "qualitative",
            "value": value, "unit": "USD" if value is not None else None,
            "currency": None, "scale": None, "producer_execution_refs": [],
            "semantic_review_ref": None, "semantic_review_hash": None,
            "candidate_origin_ref": None, "candidate_origin_hash": None,
            "actor_ref": "system:research-auto-commit", "prior_version_ref": None,
            "created_at": created_at,
        }
        claim["content_hash"] = content_hash(
            {k: v for k, v in claim.items() if k != "content_hash"}
        )
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,"
                "claim_json,content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1,
                 json.dumps(claim, sort_keys=True), claim["content_hash"],
                 claim["created_at"]),
            )
        return claim["id"]

    def prices(
        self, company_ref: str, closes: list[tuple[str, str]], *,
        ticker: str = "ACN", captured_at: str = SETTLED_AT,
    ) -> dict[str, Any]:
        from dalton_core.market_price import MarketPriceSeriesAuthority

        authority = getattr(self, "_prices", None)
        if authority is None:
            authority = MarketPriceSeriesAuthority(self.store)
            self._prices = authority
        return authority.publish_series(
            company_ref=company_ref, ticker=ticker, currency="USD",
            bars=[bar(date, close) for date, close in closes],
            invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            requested_start=closes[0][0], requested_end=closes[-1][0],
            captured_at=captured_at,
        )


__all__ = [
    "ACN", "ARTIFACT", "AUTOMATION", "CTSH", "DXC", "EPAM", "GOVERNANCE",
    "GOVERNANCE_HASH", "IBM", "INVOCATION", "OWNER", "P14aHarness", "SETTLED_AT",
    "UNIVERSE", "bar",
]
