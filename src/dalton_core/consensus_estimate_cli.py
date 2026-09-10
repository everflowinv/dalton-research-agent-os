"""P11b: fetch one company's street expectations and publish them, out of process.

The same three-step order the price lane converged on, and each step is there
because skipping it has cost this codebase a day at some point.

**Approval first.** The governance record has to be approved *and* still
describe the packaged contract, checked before the network is touched.

**Artifact always.** The library's whole output is canonicalised, hashed and
written to the raw spool before a single number is read out of it, on a failed
run as much as a successful one. That hash is what makes a stored consensus
replayable, and it is the only thing standing between "the street expected
$14.66" and "some website said so once".

**Contract last.** The wire is validated against the frozen output schema
before it reaches the authority.

One thing this child does that the price child does not: it will not run
without the company's fiscal year end and last reported period end. Yahoo's
``0q``/``+1q``/``0y``/``+1y`` are relative to a calendar it does not publish,
and an estimate filed against the wrong quarter is worse than no estimate,
because it will be compared against an actual it was never about. The lane
reads both out of the SEC filings this system already holds; a company with no
10-K ingested is skipped rather than guessed at.
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
from .consensus_estimate import (
    ConsensusEstimateAuthority,
    ConsensusEstimateError,
    FiscalMappingError,
)
from .market_price_adapter import (
    MarketDataAdapterError,
    analyst_estimates_wire,
    fetch_analyst_estimates,
    json_safe,
)
from .raw_spool import RawSpool
from .store import DaltonStore, canonical_json
from .yfinance_core import (
    ANALYST_ESTIMATES_OPERATION,
    invocation_ref as build_invocation_ref,
    yfinance_identity,
    yfinance_output_schema,
)

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# Four small JSON blocks. A ceiling the spool refuses beyond, not an
# expectation.
MAX_RAW_BYTES = 8 * 1024 * 1024


class ConsensusEstimateRunError(RuntimeError):
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
        raise ConsensusEstimateRunError(
            "yfinance analyst-estimates governance record is not approved"
        )
    identity = yfinance_identity(ANALYST_ESTIMATES_OPERATION)
    if governance.capability_id != identity["capability_id"]:
        raise ConsensusEstimateRunError("governance record covers a different capability")
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise ConsensusEstimateRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise ConsensusEstimateRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": ANALYST_ESTIMATES_OPERATION,
        "transport": "fixture" if args.fixture_file else "public-https",
        "company_ref": args.company_ref,
        "ticker": args.ticker,
        "fiscal_year_end": args.fiscal_year_end,
        "last_reported_period_end": args.last_reported_period_end,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "as_of": None,
        "consensus_status": None,
        "consensus_version_ref": None,
        "consensus_version_hash": None,
        "changed_fields": [],
        "eps_period_count": 0,
        "revenue_period_count": 0,
        "recommendation_period_count": 0,
        "target_price_mean": None,
        "mapped_periods": [],
    }
    store: DaltonStore | None = None
    try:
        # The parser refuses a run with neither mode or both, but ``run`` is
        # called directly by tests and by anything that builds a Namespace
        # itself. Reaching Yahoo because a flag was forgotten is not a mistake
        # this should make on the caller's behalf.
        if bool(args.fixture_file) == bool(getattr(args, "allow_network", False)):
            raise ConsensusEstimateRunError(
                "exactly one of --fixture-file or --allow-network must be chosen"
            )
        governance = _load_governance(Path(args.governance).expanduser().resolve())
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        if args.fixture_file:
            raw = json.loads(
                Path(args.fixture_file).expanduser().read_text(encoding="utf-8")
            )
        else:
            raw = fetch_analyst_estimates(args.ticker)
        raw = json_safe(raw)
        if not isinstance(raw, dict):
            raise ConsensusEstimateRunError(
                "the library returned something that is not a call"
            )

        payload = canonical_json(raw).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        wire = analyst_estimates_wire(
            raw, source_record_refs=[f"raw-sink:{artifact.content_hash}"]
        )
        from .authority_resolver import _schema_matches

        _schema_matches(
            wire, yfinance_output_schema(ANALYST_ESTIMATES_OPERATION), "output"
        )
        summary["as_of"] = wire["as_of"]
        summary["eps_period_count"] = len(wire["eps_estimates"])
        summary["revenue_period_count"] = len(wire["revenue_estimates"])
        summary["recommendation_period_count"] = len(wire["recommendations"])
        summary["target_price_mean"] = wire["price_target"]["mean"]

        invocation = build_invocation_ref(
            operation=ANALYST_ESTIMATES_OPERATION,
            governance_ref=governance.id,
            governance_hash=governance.content_hash,
            parameters={"ticker": wire["ticker"]},
            artifact_hash=artifact.content_hash,
        )
        summary["invocation_ref"] = invocation

        if args.no_publish:
            summary.update({"status": "succeeded", "consensus_status": "not_published"})
        else:
            store = DaltonStore(str(state / "core.sqlite"))
            authority = ConsensusEstimateAuthority(store)
            published = authority.publish_consensus(
                company_ref=args.company_ref,
                wire=wire,
                fiscal_year_end=args.fiscal_year_end,
                last_reported_period_end=args.last_reported_period_end,
                invocation_ref=invocation,
                artifact_hash=artifact.content_hash,
                governance_ref=governance.id,
                governance_hash=governance.content_hash,
                captured_at=str(raw.get("captured_at") or summary["created_at"]),
                actor_ref=args.actor_ref,
            )
            summary.update({
                "status": "succeeded",
                "consensus_status": published["status"],
                "consensus_version_ref": published["id"],
                "consensus_version_hash": published["content_hash"],
                # What *this run* changed, not what the version it landed on
                # once changed: a duplicate returns the standing version, whose
                # own changed_fields describe the run that created it.
                "changed_fields": (
                    list(published.get("changed_fields") or [])
                    if published["status"] == "fresh" else []
                ),
                "mapped_periods": sorted({
                    row["label"] for row in (
                        *published["eps_estimates"], *published["revenue_estimates"]
                    )
                }),
            })
    except (
        ConsensusEstimateRunError, ConsensusEstimateError, FiscalMappingError,
        MarketDataAdapterError, ConnectorGovernanceError,
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
                        help="approved yfinance-analyst-estimates record")
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--fiscal-year-end", required=True,
                        help="MM-DD, from the company's newest 10-K report date")
    parser.add_argument("--last-reported-period-end", required=True,
                        help="YYYY-MM-DD, the newest filing this system holds")
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--actor-ref", default="core:consensus-estimate-worker")
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
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "consensus_status", "as_of",
            "target_price_mean", "eps_period_count", "revenue_period_count",
            "recommendation_period_count", "mapped_periods", "changed_fields",
            "invocation_ref", "consensus_version_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_RAW_BYTES",
    "ConsensusEstimateRunError",
    "build_parser",
    "main",
    "run",
]
