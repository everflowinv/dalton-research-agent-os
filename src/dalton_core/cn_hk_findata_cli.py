"""S4: run one China / Hong Kong fundamentals call, out of process.

A child for the reason every connector child is one: the library reaches the
network, a decade of statements is a dozen paged requests behind one function
call, and the writer's store thread cannot be held that long.

The order is the one the connector lanes converged on, and each step is here
because skipping it has cost this codebase a day at some point.

**Approval first.** The governance record has to be approved *and* still
describe the packaged contract for this exact operation. A record whose
contract moved is refused before anything is reached, not discovered halfway
through. Six operations means six records, and one of them cannot stand in for
another: the schema hash binds one.

**Artifact always.** Every frame the library returns is canonicalised --
column order kept, every cell rendered as text, no floats anywhere -- and the
capture is hashed into the raw spool before a single number is read out of it.
That hash is what stands in for Dalton verifying 东方财富's bytes. It is
written for a run that then fails to normalise, too, because a capture that
could not be read is exactly the capture somebody will want to look at.

What is hashed is what the vendor said, and not when this machine happened to
ask: the two local clock fields are lifted out first (``CLOCK_FIELDS``) and
kept on the summary and the wire instead. With them inside, every run minted a
new hash and therefore a new invocation ref, so two readings of the same
unchanged quarter looked like two different facts.

**Contract last.** The wire is validated against the frozen output schema
before it goes anywhere. An observation the contract cannot describe is
refused, not stored and explained afterwards.

There is no authority behind this yet -- the mission universe is American and
the lane that would consume these rows is not built. So the validated wire is
written beside the summary rather than published, which is the whole deliverable
until a China/HK mission exists.

Two modes, exactly one of which must be chosen: ``--allow-network`` calls the
library, ``--fixture-file`` replays a captured call, which is how everything
here is tested without reaching out.
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

from .cn_hk_findata_adapter import (
    CnHkFinDataAdapterError,
    CnHkFinDataVendorRefusal,
    FETCHERS,
    WIRE_BUILDERS,
)
from .cn_hk_findata_core import (
    CnHkFinDataError,
    HOSTS_BY_OPERATION,
    KIND_BY_OPERATION,
    OPERATIONS,
    cn_hk_findata_identity,
    cn_hk_findata_output_schema,
    invocation_ref as build_invocation_ref,
)
from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .raw_spool import RawSpool
from .store import canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# A decade of a three-hundred-line statement, canonicalised to text, is a few
# megabytes. This is a ceiling the spool refuses beyond, not an expectation.
MAX_RAW_BYTES = 64 * 1024 * 1024

# Read off the capture before it is hashed, and kept on the summary and the
# wire instead.
#
# These two say when this machine made the call. They are not something the
# source returned, and leaving them inside the hashed bytes meant every run
# minted a new artifact hash and therefore a new invocation ref -- so two
# fetches of the same unchanged quarter looked like two different facts, which
# is the exact distinction the invocation ref exists to make. What is hashed
# is what the vendor said.
CLOCK_FIELDS = ("captured_at", "observed_on")

# Which command-line arguments each operation's fetcher takes. Frozen here so a
# run cannot pass a parameter the approval never described.
PARAMETERS_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "financial_statements": ("market", "ticker", "statement_kind", "period_type"),
    "shareholders": ("a_ticker", "period_end"),
    "buybacks": ("a_ticker",),
    "margin_balance": ("exchange", "start", "end"),
    "northbound_flow": ("as_of",),
    "ah_premium": ("ticker",),
}


class CnHkFinDataRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    """The approved record, checked against the identity it claims to cover."""

    governance = ConnectorGovernance.load(path)
    if not governance.approved:
        raise CnHkFinDataRunError(
            f"the cn-hk-findata {operation} governance record is not approved"
        )
    identity = cn_hk_findata_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise CnHkFinDataRunError(
            "governance record covers a different capability; six operations "
            "means six approvals and one does not stand in for another"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise CnHkFinDataRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise CnHkFinDataRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _parameters(args: argparse.Namespace, operation: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in PARAMETERS_BY_OPERATION[operation]:
        value = getattr(args, name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CnHkFinDataRunError(
                f"{operation} requires --{name.replace('_', '-')}"
            )
        values[name] = value.strip() if isinstance(value, str) else value
    return values


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    operation = args.operation
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": operation,
        "transport": "fixture" if args.fixture_file else "public-https",
        "allowed_hosts": list(HOSTS_BY_OPERATION.get(operation, ())),
        "parameters": None,
        "status": "failed",
        "failure_reason": None,
        "refusal_kind": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "captured_at": None,
        "source_vendor": None,
        "fallback_used": None,
        "row_count": 0,
        "dropped_row_count": 0,
        "wire_path": None,
        "caliber_notes": [],
    }
    try:
        if operation not in OPERATIONS:
            raise CnHkFinDataRunError(f"cn-hk-findata has no {operation!r} operation")
        # The parser refuses a run with neither mode or both, but ``run`` is
        # called directly by tests and by anything that builds a Namespace
        # itself. Reaching 东方财富 because a flag was forgotten is not a
        # mistake this should make on the caller's behalf.
        if bool(args.fixture_file) == bool(getattr(args, "allow_network", False)):
            raise CnHkFinDataRunError(
                "exactly one of --fixture-file or --allow-network must be chosen"
            )
        parameters = _parameters(args, operation)
        summary["parameters"] = dict(parameters)
        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), operation)
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        if args.fixture_file:
            raw = json.loads(
                Path(args.fixture_file).expanduser().read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise CnHkFinDataRunError("the fixture is not a capture")
            raw = dict(raw)
            # A replay answers the question the capture answered, not the one
            # the command line asked. They have to be the same question. A
            # capture of 600519 replayed under --a-ticker 000001 would produce
            # a wire that validates, publishes and is about a different
            # company; there is nothing further down the chain that could
            # notice.
            captured = dict(raw.get("parameters") or {})
            if captured != parameters:
                raise CnHkFinDataRunError(
                    "the fixture answers a different request than the one "
                    f"asked for: captured {captured}, asked {parameters}. A "
                    "replay cannot be relabelled -- the rows describe what was "
                    "captured, whatever the command line says"
                )
            raw["parameters"] = captured
        else:
            raw = FETCHERS[operation](**parameters)
        if not isinstance(raw, dict):
            raise CnHkFinDataRunError("the library returned something that is not a call")

        # The artifact is the capture, canonical and hashed before anything is
        # read out of it: whatever the normaliser drops stays recoverable, and
        # the same answer twice is the same hash twice. The two local clock
        # fields are lifted out first -- see CLOCK_FIELDS -- so that "the same
        # answer" means what the vendor said and not what time it was said at.
        payload = canonical_json(
            {key: value for key, value in raw.items() if key not in CLOCK_FIELDS}
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        wire = WIRE_BUILDERS[operation](
            raw, source_record_refs=[f"raw-sink:{artifact.content_hash}"])
        from .authority_resolver import _schema_matches

        _schema_matches(wire, cn_hk_findata_output_schema(operation), "output")

        rows = wire.get("lines")
        if rows is None:
            rows = wire.get("rows")
        if rows is None:
            rows = list(wire.get("top_holders") or []) + list(
                wire.get("holder_counts") or [])
        summary["row_count"] = len(rows)
        summary["dropped_row_count"] = wire["dropped_row_count"]
        summary["captured_at"] = wire["captured_at"]
        vendors = sorted({row["source_vendor"] for row in rows})
        # A wire with no rows is a real answer -- this company announced no
        # buyback, this code is not half of an A+H pair -- and it has no
        # vendor, which is different from having an empty list of them. One
        # vendor reads as a string because that is what almost every operation
        # returns; only 融资融券 can carry two, and only if a caller merged
        # exchanges.
        summary["source_vendor"] = (
            None if not vendors else
            vendors[0] if len(vendors) == 1 else vendors
        )
        summary["fallback_used"] = any(row["fallback_used"] for row in rows)
        summary["caliber_notes"] = sorted(
            {row["caliber_note"] for row in rows if row["caliber_note"]})

        summary["invocation_ref"] = build_invocation_ref(
            operation=operation,
            governance_ref=governance.id,
            governance_hash=governance.content_hash,
            parameters=parameters,
            artifact_hash=artifact.content_hash,
        )
        # No authority consumes these rows yet, so the validated wire is the
        # deliverable. Written beside the summary rather than published,
        # because inventing a half-authority to hold it would be a worse
        # answer than saying there is not one.
        if not args.no_wire:
            wire_path = summary_dir / f"wire-{operation}.json"
            _write_owner_only(wire_path, wire)
            summary["wire_path"] = str(wire_path)
        summary["status"] = "succeeded"
    except CnHkFinDataVendorRefusal as exc:
        # Kept apart from every other failure in the summary, because it is the
        # one a lane must not answer by asking somewhere else.
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        summary["refusal_kind"] = "vendor_unavailable_no_substitute"
    except (
        CnHkFinDataRunError, CnHkFinDataError, CnHkFinDataAdapterError,
        ConnectorGovernanceError,
    ) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved record for this exact operation")
    parser.add_argument("--operation", required=True, choices=sorted(OPERATIONS))
    parser.add_argument("--market", default=None, choices=["a", "hk"])
    parser.add_argument("--ticker", default=None,
                        help="A-share six digits or Hong Kong five digits")
    parser.add_argument("--a-ticker", default=None,
                        help="A-share six digits; these operations have no HK route")
    parser.add_argument("--statement-kind", default=None,
                        choices=["income", "balance", "cash"])
    parser.add_argument("--period-type", default=None, choices=["report", "annual"])
    parser.add_argument("--period-end", default=None, help="report date, YYYY-MM-DD")
    parser.add_argument("--exchange", default=None, choices=["sse", "szse"])
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--as-of", default=None, help="the trading day asked for")
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured call instead of reaching the source")
    parser.add_argument("--no-wire", action="store_true",
                        help="validate without writing the wire out")
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
            "status", "failure_reason", "refusal_kind", "operation",
            "source_vendor", "fallback_used", "row_count", "dropped_row_count",
            "invocation_ref", "wire_path", "caliber_notes",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_RAW_BYTES",
    "PARAMETERS_BY_OPERATION",
    "CnHkFinDataRunError",
    "build_parser",
    "main",
    "run",
]
