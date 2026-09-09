"""P11a: fetch one company's price window and publish it, out of process.

A child for the same reason every other lane child is one: the library reaches
the network, a three-year backfill takes longer than the writer's request
timeout, and the writer's store thread cannot be held that long.

The order is the one the connector lanes have converged on, and each step is
there because skipping it has cost this codebase a day at some point.

**Approval first.** The governance record has to be approved *and* still
describe the packaged contract. A record whose contract moved is refused before
the network is touched, not discovered halfway through a run.

**Artifact always.** The library's whole output is canonicalised, hashed and
written to the raw spool before a single number is read out of it. That hash is
what stands in for Dalton verifying Yahoo's bytes -- it is the only thing that
makes a stored price replayable, and it is written even for a run that then
fails to publish.

**Contract last.** The wire is validated against the frozen output schema
before it reaches the authority. An observation the contract cannot describe is
refused, not stored and explained afterwards.

Two modes, exactly one of which must be chosen: ``--allow-network`` calls
Yahoo, ``--fixture-file`` replays a captured call, which is how this is tested
without reaching out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .market_price import MarketPriceError, MarketPriceSeriesAuthority
from .market_price_adapter import (
    MarketDataAdapterError,
    daily_prices_wire,
    fetch_daily_prices,
    json_safe,
)
from .raw_spool import RawSpool
from .store import DaltonStore, canonical_json
from .yfinance_core import (
    DAILY_PRICES_OPERATION,
    invocation_ref as build_invocation_ref,
    yfinance_identity,
    yfinance_output_schema,
)

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# Twenty years of daily bars for one company is a few megabytes of JSON. This
# is a ceiling the spool refuses beyond, not an expectation.
MAX_RAW_BYTES = 64 * 1024 * 1024


class MarketPriceRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_governance(path: Path) -> ConnectorGovernance:
    """The approved record, checked against the identity it claims to cover."""

    governance = ConnectorGovernance.load(path)
    if not governance.approved:
        raise MarketPriceRunError("yfinance daily-prices governance record is not approved")
    identity = yfinance_identity(DAILY_PRICES_OPERATION)
    if governance.capability_id != identity["capability_id"]:
        raise MarketPriceRunError("governance record covers a different capability")
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise MarketPriceRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise MarketPriceRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": DAILY_PRICES_OPERATION,
        "transport": "fixture" if args.fixture_file else "public-https",
        "company_ref": args.company_ref,
        "ticker": args.ticker,
        "requested_start": args.start,
        "requested_end": args.end,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "bar_count": 0,
        "observation_count": 0,
        "series_status": None,
        "series_version_ref": None,
        "series_version_hash": None,
        "first_bar_date": None,
        "last_bar_date": None,
        "added_bar_count": 0,
        "restated_bar_dates": [],
    }
    store: DaltonStore | None = None
    try:
        governance = _load_governance(Path(args.governance).expanduser().resolve())
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        if args.fixture_file:
            raw = json.loads(Path(args.fixture_file).expanduser().read_text(encoding="utf-8"))
        else:
            raw = fetch_daily_prices(args.ticker, start=args.start, end=args.end)
        raw = json_safe(raw)
        if not isinstance(raw, dict):
            raise MarketPriceRunError("the library returned something that is not a call")

        # The artifact is the call, kept whole and hashed before anything is
        # read out of it: whatever the normaliser drops stays recoverable.
        payload = canonical_json(raw).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        wire = daily_prices_wire(
            raw, source_record_refs=[f"raw-sink:{artifact.content_hash}"]
        )
        # Validate before recording: an observation the frozen contract cannot
        # describe is refused, not stored and explained later.
        from .authority_resolver import _schema_matches

        _schema_matches(wire, yfinance_output_schema(DAILY_PRICES_OPERATION), "output")
        summary["bar_count"] = len(wire["bars"])
        summary["observation_count"] = len(wire["observations"])

        invocation = build_invocation_ref(
            operation=DAILY_PRICES_OPERATION,
            governance_ref=governance.id,
            governance_hash=governance.content_hash,
            parameters={
                "ticker": wire["ticker"],
                "start": wire["requested_start"],
                "end": wire["requested_end"],
            },
            artifact_hash=artifact.content_hash,
        )
        summary["invocation_ref"] = invocation

        if not wire["bars"]:
            # A window with no trading days in it is a normal answer -- a
            # weekend, a holiday, a company that has not traded yet -- and not
            # a failure. There is simply nothing to publish.
            summary.update({"status": "succeeded", "series_status": "empty"})
        elif args.no_publish:
            summary.update({"status": "succeeded", "series_status": "not_published"})
        else:
            store = DaltonStore(str(state / "core.sqlite"))
            authority = MarketPriceSeriesAuthority(store)
            published = authority.publish_series(
                company_ref=args.company_ref,
                ticker=wire["ticker"],
                currency=wire["currency"],
                bars=wire["bars"],
                observations=wire["observations"],
                invocation_ref=invocation,
                artifact_hash=artifact.content_hash,
                governance_ref=governance.id,
                governance_hash=governance.content_hash,
                requested_start=wire["requested_start"],
                requested_end=wire["requested_end"],
                actor_ref=args.actor_ref,
            )
            summary.update({
                "status": "succeeded",
                "series_status": published["status"],
                "series_version_ref": published["id"],
                "series_version_hash": published["content_hash"],
                "first_bar_date": published["first_bar_date"],
                "last_bar_date": published["last_bar_date"],
                "series_bar_count": published["bar_count"],
                # What *this run* changed, not what the version it landed on
                # once did. A duplicate returns the standing version, whose
                # own ``added_bar_dates`` describe the run that created it --
                # reporting those here would tell the lane that a tick which
                # added nothing had added ten days.
                "added_bar_count": (
                    len(published.get("added_bar_dates") or [])
                    if published["status"] == "fresh" else 0
                ),
                "restated_bar_dates": (
                    list(published.get("restated_bar_dates") or [])
                    if published["status"] == "fresh" else []
                ),
            })
    except (
        MarketPriceRunError, MarketPriceError, MarketDataAdapterError,
        ConnectorGovernanceError,
    ) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if store is not None:
            store.close()
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved yfinance-daily-prices record")
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start", required=True, help="first day of the window (inclusive)")
    parser.add_argument("--end", required=True,
                        help="last day of the window (exclusive, as Yahoo reads it)")
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--actor-ref", default="core:market-price-worker")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured call instead of reaching Yahoo")
    parser.add_argument("--no-publish", action="store_true",
                        help="fetch and validate without writing to the authority")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if args.end < args.start:
        parser.error("--end precedes --start")
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "series_status", "bar_count",
            "first_bar_date", "last_bar_date", "added_bar_count",
            "restated_bar_dates", "invocation_ref", "series_version_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["MAX_RAW_BYTES", "MarketPriceRunError", "build_parser", "main", "run"]
