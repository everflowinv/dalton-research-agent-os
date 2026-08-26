"""Normalize a read-only legacy Coverage snapshot into a stable perception contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .provider_token_estimate import (
    PROVIDER_INPUT_ESTIMATOR_REF,
    estimate_provider_input_tokens,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
# Deterministic bounding rule: when a snapshot must fit a provider-token budget,
# the oldest evidence rows are dropped first, then the oldest catalysts, then
# the oldest filings.  Every list is already newest-first, so dropping is a
# pop from the tail.  What was fetched and what was dropped is recorded in
# the snapshot itself, so the cycle that binds the snapshot can audit it.
PERCEPTION_BOUNDING_REF = "perception-bounding:oldest-first-drop:0.1"
_BOUNDED_SECTIONS = ("evidence", "catalysts", "filings")
_REQUIRED_COLUMNS = {
    "companies": {"slug", "name", "ticker", "market", "coverage_tier", "coverage_status", "archetype", "investment_view", "updated_at"},
    "events": {"id", "event_key", "company_slug", "event_type", "occurred_at", "title", "summary", "materiality", "status", "source_url", "updated_at"},
    "evidence": {"id", "evidence_key", "company_slug", "claim", "stance", "source", "source_url", "as_of", "confidence", "valid_until", "created_at"},
    "filings": {"id", "company_slug", "form", "filing_date", "report_date", "accession_no", "created_at"},
}


class PerceptionError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PerceptionError("perception snapshot must be an object")
    expected = {
        "schema_version", "snapshot_id", "generated_at", "source_kind",
        "source_snapshot_hash", "company", "catalysts", "evidence", "filings",
        "content_hash",
    }
    # ``bounding`` is the only optional key: it is present exactly when the
    # adapter was asked to fit a provider-token budget.
    if set(value) - {"bounding"} != expected or value.get("schema_version") != SCHEMA_VERSION:
        raise PerceptionError("perception snapshot has an invalid closed shape")
    wire = dict(value)
    asserted = wire.pop("content_hash")
    if asserted != content_hash(wire):
        raise PerceptionError("perception snapshot content hash mismatch")
    if not isinstance(value["company"], Mapping):
        raise PerceptionError("perception company must be an object")
    for name in ("catalysts", "evidence", "filings"):
        if not isinstance(value[name], list) or not all(isinstance(item, Mapping) for item in value[name]):
            raise PerceptionError(f"perception {name} must be an object array")
    if "bounding" in value:
        bounding = value["bounding"]
        if (
            not isinstance(bounding, Mapping)
            or bounding.get("bounding_ref") != PERCEPTION_BOUNDING_REF
            or bounding.get("estimator_ref") != PROVIDER_INPUT_ESTIMATOR_REF
            or not isinstance(bounding.get("max_estimated_tokens"), int)
            or not isinstance(bounding.get("estimated_tokens"), int)
            or bounding["estimated_tokens"] > bounding["max_estimated_tokens"]
            or not isinstance(bounding.get("fetched"), Mapping)
            or not isinstance(bounding.get("dropped"), Mapping)
            or set(bounding["fetched"]) != set(_BOUNDED_SECTIONS)
            or set(bounding["dropped"]) != set(_BOUNDED_SECTIONS)
        ):
            raise PerceptionError("perception bounding record is invalid")
        for name in _BOUNDED_SECTIONS:
            if bounding["fetched"][name] - bounding["dropped"][name] != len(value[name]):
                raise PerceptionError("perception bounding record does not match the snapshot")
    return dict(value)


def _bound_sections(
    header: Mapping[str, Any],
    company: Mapping[str, Any],
    sections: dict[str, list[dict[str, Any]]],
    max_estimated_tokens: int,
) -> dict[str, Any]:
    """Drop oldest rows until the whole snapshot wire fits ``max_estimated_tokens``.

    The estimate covers the exact wire that will be registered -- header,
    company, sections, this bounding record and a content hash of the same
    length -- so the number recorded here is the number the coordinator can
    hold the prompt against.
    """

    if isinstance(max_estimated_tokens, bool) or not isinstance(max_estimated_tokens, int) or max_estimated_tokens < 1:
        raise PerceptionError("max_estimated_tokens must be a positive integer")
    fetched = {name: len(sections[name]) for name in _BOUNDED_SECTIONS}

    def record(estimated: int) -> dict[str, Any]:
        return {
            "bounding_ref": PERCEPTION_BOUNDING_REF,
            "estimator_ref": PROVIDER_INPUT_ESTIMATOR_REF,
            "max_estimated_tokens": max_estimated_tokens,
            "estimated_tokens": estimated,
            "fetched": fetched,
            "dropped": {name: fetched[name] - len(sections[name]) for name in _BOUNDED_SECTIONS},
        }

    def estimate(previous: int) -> int:
        wire = {
            **dict(header),
            "company": dict(company),
            **{name: sections[name] for name in _BOUNDED_SECTIONS},
            "bounding": record(previous),
            "content_hash": "0" * 64,
        }
        return estimate_provider_input_tokens(canonical_json(wire))

    estimated = estimate(max_estimated_tokens)
    while estimated > max_estimated_tokens:
        for name in _BOUNDED_SECTIONS:
            if sections[name]:
                sections[name].pop()
                break
        else:
            raise PerceptionError(
                "perception snapshot exceeds the estimated token budget even with no rows"
            )
        estimated = estimate(estimated)
    # One more pass with the final digits in the record itself.
    estimated = estimate(estimated)
    if estimated > max_estimated_tokens:
        raise PerceptionError("perception bounding did not converge")
    return record(estimated)


class LegacyCoveragePerceptionAdapter:
    """The only Phase 1 component allowed to know the legacy Coverage schema."""

    def __init__(self, source_db: str | Path):
        self.source_db = Path(source_db).expanduser().resolve()
        if not self.source_db.is_file():
            raise PerceptionError("legacy coverage database is unavailable")

    @staticmethod
    def _assert_schema(conn: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = {row[1] for row in rows}
            missing = required - columns
            if missing:
                raise PerceptionError(f"legacy table {table} is missing required columns")

    def build(
        self, company_slug: str, *, max_estimated_tokens: int | None = None
    ) -> dict[str, Any]:
        """Build the snapshot; optionally bound it to a provider-token estimate.

        ``max_estimated_tokens`` is measured with the same estimator the Agenda
        coordinator uses for its pre-flight budget check, so a bounded snapshot
        is one the policy's ``max_input_tokens`` can actually hold.
        """

        if not isinstance(company_slug, str) or not company_slug:
            raise PerceptionError("company_slug must be a non-empty string")
        with tempfile.TemporaryDirectory(prefix="dalton-perception-") as directory:
            snapshot_db = Path(directory) / "coverage-snapshot.sqlite"
            source_uri = f"file:{self.source_db}?mode=ro"
            source = sqlite3.connect(source_uri, uri=True)
            target = sqlite3.connect(snapshot_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            source_snapshot_hash = hashlib.sha256(snapshot_db.read_bytes()).hexdigest()
            conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                self._assert_schema(conn)
                company = conn.execute(
                    "SELECT slug,name,ticker,market,coverage_tier,coverage_status,archetype,investment_view,updated_at FROM companies WHERE slug=?",
                    (company_slug,),
                ).fetchone()
                if company is None:
                    raise PerceptionError("trial company is absent from legacy coverage")
                catalysts = [dict(row) for row in conn.execute(
                    "SELECT event_key,event_type,occurred_at,title,summary,materiality,status,source_url,updated_at FROM events WHERE company_slug=? ORDER BY occurred_at DESC,event_key LIMIT 40",
                    (company_slug,),
                ).fetchall()]
                evidence = [dict(row) for row in conn.execute(
                    "SELECT evidence_key,claim,stance,source,source_url,as_of,confidence,valid_until,created_at FROM evidence WHERE company_slug=? ORDER BY as_of DESC,evidence_key LIMIT 80",
                    (company_slug,),
                ).fetchall()]
                filings = [dict(row) for row in conn.execute(
                    "SELECT form,filing_date,report_date,accession_no,created_at FROM filings WHERE company_slug=? ORDER BY filing_date DESC,accession_no LIMIT 40",
                    (company_slug,),
                ).fetchall()]
            finally:
                conn.close()
        generated_at = _now()
        header = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"perception-snapshot:{company_slug}:{generated_at[:10]}",
            "generated_at": generated_at,
            "source_kind": "legacy-coverage-sqlite-backup-v1",
            "source_snapshot_hash": source_snapshot_hash,
        }
        bounding = None
        if max_estimated_tokens is not None:
            sections = {"catalysts": catalysts, "evidence": evidence, "filings": filings}
            bounding = _bound_sections(header, dict(company), sections, max_estimated_tokens)
        wire = {
            **header,
            "company": dict(company),
            "catalysts": catalysts,
            "evidence": evidence,
            "filings": filings,
        }
        if bounding is not None:
            wire["bounding"] = bounding
        wire["content_hash"] = content_hash(wire)
        return validate_snapshot(wire)

    def write(
        self,
        company_slug: str,
        output_path: str | Path,
        *,
        max_estimated_tokens: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self.build(company_slug, max_estimated_tokens=max_estimated_tokens)
        _atomic_json(Path(output_path).expanduser().resolve(), snapshot)
        return snapshot


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a normalized Dalton perception snapshot")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = LegacyCoveragePerceptionAdapter(args.source_db).write(args.company, args.output)
    print(json.dumps({"snapshot_id": result["snapshot_id"], "content_hash": result["content_hash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
