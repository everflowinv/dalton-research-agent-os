"""C1: read one company's diary from two sources and publish it, out of process.

A child for the reason every connector lane's child is one: the library reaches
the network and the writer's store thread cannot be held while it does.

The order is the one the connector lanes converged on.

**Approval first.** The ``yfinance-calendar`` governance record has to be
approved *and* still describe the packaged contract. A record whose contract
moved is refused before Yahoo is touched.

**Artifact always.** The library's whole output is canonicalised, hashed and
spooled before a single date is read out of it, and it is spooled even when the
run then fails to publish.

**Contract last.** The wire is validated against the frozen output schema
before it reaches the authority.

The SEC half of the run makes **no call at all**. It reads the raw body of a
governed ``list_filings`` invocation that is already spooled, which is where
every Item 2.02 8-K these companies have filed this year already sits. It is
therefore not gated on the yfinance approval and not counted against any quota:
if the vendor call fails, the run still publishes what the company itself said.

Two modes, exactly one of which must be chosen: ``--allow-network`` calls
Yahoo, ``--fixture-file`` replays a captured call.
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

from .catalyst_calendar import (
    CatalystCalendarAuthority,
    CatalystCalendarError,
    PUBLISHING_CHANGES,
)
from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .market_price_adapter import MarketDataAdapterError, json_safe
from .raw_spool import RawSpool
from .store import DaltonStore, canonical_json
from .yfinance_calendar_adapter import (
    calendar_entries as yfinance_calendar_entries,
    calendar_wire,
    fetch_calendar,
    quarter_subject,
)
from .yfinance_core import (
    CALENDAR_OPERATION,
    invocation_ref as build_invocation_ref,
    yfinance_identity,
    yfinance_output_schema,
)

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# One company's calendar is a handful of dates. This is a ceiling the spool
# refuses beyond, not an expectation.
MAX_RAW_BYTES = 4 * 1024 * 1024


class CatalystCalendarRunError(RuntimeError):
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
        raise CatalystCalendarRunError(
            "yfinance calendar governance record is not approved"
        )
    identity = yfinance_identity(CALENDAR_OPERATION)
    if governance.capability_id != identity["capability_id"]:
        raise CatalystCalendarRunError("governance record covers a different capability")
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise CatalystCalendarRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise CatalystCalendarRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _sec_entries(
    connection: Any, state: Path, summary: dict[str, Any], *,
    issuer: str | None, today: str,
) -> list[dict[str, Any]]:
    """What the company itself said, from filings already held. Never fatal.

    A company whose discovery run has not happened, or whose artifact has been
    pruned, leaves the vendor's estimate standing alone. That is a thinner
    calendar and not a broken one, so it is reported rather than raised.
    """

    if not issuer:
        summary["sec_status"] = "not_requested"
        return []
    from .sec_earnings_release import (  # noqa: PLC0415 - lazy, sqlite only
        calendar_entries as sec_calendar_entries,
        releases_for_issuer,
    )

    try:
        found = releases_for_issuer(connection, state, issuer=issuer, today=today)
    except Exception as exc:  # noqa: BLE001 - the vendor half still stands
        summary["sec_status"] = "failed"
        summary["sec_reason"] = f"{type(exc).__name__}: {exc}"
        return []
    summary["sec_status"] = found["status"]
    summary["sec_reason"] = found.get("reason")
    summary["sec_release_count"] = len(found.get("releases") or [])
    if found["status"] != "read" or not found["releases"]:
        return []
    summary["sec_invocation_ref"] = found["invocation_ref"]
    summary["sec_artifact_hash"] = found["artifact_hash"]
    return sec_calendar_entries(
        found["releases"], invocation_ref=found["invocation_ref"],
        artifact_hash=found["artifact_hash"], subject_for=quarter_subject,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    today = args.as_of or datetime.now(timezone.utc).date().isoformat()
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": CALENDAR_OPERATION,
        "transport": "fixture" if args.fixture_file else "public-https",
        "company_ref": args.company_ref,
        "ticker": args.ticker,
        "issuer": args.issuer,
        "as_of": today,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "earnings_dates": [],
        "ex_dividend_date": None,
        "vendor_entry_count": 0,
        "sec_status": "not_read",
        "sec_reason": None,
        "sec_release_count": 0,
        "sec_invocation_ref": None,
        "sec_artifact_hash": None,
        "sec_entry_count": 0,
        "calendar_status": None,
        "calendar_version_ref": None,
        "calendar_version_hash": None,
        "entry_count": 0,
        "next_catalyst_date": None,
        "change_reason": None,
        "changes": [],
        "moved_entry_refs": [],
        "published_change_count": 0,
    }
    store: DaltonStore | None = None
    try:
        # The parser refuses a run with neither mode or both, but ``run`` is
        # called directly by tests and by anything that builds a Namespace
        # itself. Reaching Yahoo because a flag was forgotten is not a mistake
        # this should make on the caller's behalf.
        if bool(args.fixture_file) == bool(getattr(args, "allow_network", False)):
            raise CatalystCalendarRunError(
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
            raw = fetch_calendar(args.ticker)
        raw = json_safe(raw)
        if not isinstance(raw, dict):
            raise CatalystCalendarRunError(
                "the library returned something that is not a call"
            )

        payload = canonical_json(raw).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        wire = calendar_wire(
            raw, source_record_refs=[f"raw-sink:{artifact.content_hash}"]
        )
        from .authority_resolver import _schema_matches  # noqa: PLC0415 - lazy

        _schema_matches(wire, yfinance_output_schema(CALENDAR_OPERATION), "output")
        summary["earnings_dates"] = list(wire["earnings_dates"])
        summary["ex_dividend_date"] = wire["ex_dividend_date"]

        invocation = build_invocation_ref(
            operation=CALENDAR_OPERATION,
            governance_ref=governance.id,
            governance_hash=governance.content_hash,
            parameters={"ticker": wire["ticker"]},
            artifact_hash=artifact.content_hash,
        )
        summary["invocation_ref"] = invocation

        entries = yfinance_calendar_entries(wire, invocation_ref=invocation)
        summary["vendor_entry_count"] = len(entries)
        # One store for both halves: the SEC side is a read of an artifact
        # this same database points at, and opening a second connection to the
        # file a publish is about to write is how a lock shows up later.
        store = DaltonStore(str(state / "core.sqlite"))
        from_sec = _sec_entries(
            store.connection, state, summary, issuer=args.issuer, today=today
        )
        summary["sec_entry_count"] = len(from_sec)
        entries = _merge(entries, from_sec)

        if not entries:
            # Neither source had anything to say about this company's diary.
            # A real answer for a company Yahoo has no calendar for and whose
            # filings index has not been read.
            summary.update({"status": "succeeded", "calendar_status": "empty"})
        elif args.no_publish:
            summary.update({"status": "succeeded", "calendar_status": "not_published"})
        else:
            evidence = [invocation]
            if summary["sec_invocation_ref"]:
                evidence.append(summary["sec_invocation_ref"])
            authority = CatalystCalendarAuthority(store)
            published = _publish(
                authority, company_ref=args.company_ref, entries=entries,
                evidence_refs=evidence, actor_ref=args.actor_ref, today=today,
            )
            changes = (
                list(published.get("changes") or [])
                if published["status"] == "fresh" else []
            )
            summary.update({
                "status": "succeeded",
                "calendar_status": published["status"],
                "calendar_version_ref": published["id"],
                "calendar_version_hash": published["content_hash"],
                "entry_count": published["entry_count"],
                "next_catalyst_date": published["next_catalyst_date"],
                "change_reason": (
                    published["change_reason"] if published["status"] == "fresh"
                    else None
                ),
                "changes": changes,
                # What the lane needs in order to emit a ``date_change`` event:
                # which occurrences actually moved in *this* run.
                "moved_entry_refs": sorted({
                    change["entry_ref"] for change in changes
                    if change["change"] == "date_moved"
                }),
                "published_change_count": len([
                    change for change in changes
                    if change["change"] in PUBLISHING_CHANGES
                ]),
            })
    except (
        CatalystCalendarRunError, CatalystCalendarError, MarketDataAdapterError,
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


def _merge(
    vendor: list[dict[str, Any]], filed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One observation per occurrence, with both sources' sources in it.

    The authority refuses a run that names the same occurrence twice, and it is
    right to: two entries for one thing in one publish would silently drop one.
    So the two mappers are folded here, where the merge is one dictionary
    lookup and visible, rather than inside the authority where it would be a
    special case for this lane.
    """

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in [*vendor, *filed]:
        key = (entry["event_kind"], entry["subject"])
        standing = merged.get(key)
        if standing is None:
            merged[key] = {**entry, "sources": list(entry["sources"])}
            continue
        standing["sources"].extend(entry["sources"])
        standing["notes"] = standing["notes"] or entry["notes"]
    return [merged[key] for key in sorted(merged)]


def _publish(
    authority: Any, *, company_ref: str, entries: list[dict[str, Any]],
    evidence_refs: list[str], actor_ref: str, today: str,
) -> dict[str, Any]:
    """Offer the reason the diff shows, without deciding what the diff is.

    The authority derives the ``change_reason`` its own contents require and
    refuses a mismatch, so this child cannot label a moved date as anything
    other than a ``driver_event``. What it does here is offer the one that fits
    an ordinary automated run: a first attempt at ``evidence_thicker``, and
    ``driver_event`` when the authority says a date moved. A person publishing
    by hand supplies their own and gets ``human_revision``.
    """

    from .catalyst_calendar import CatalystCalendarConflict  # noqa: PLC0415

    try:
        return authority.publish(
            company_ref=company_ref, entries=entries,
            change_reason="evidence_thicker", evidence_refs=evidence_refs,
            actor_ref=actor_ref, now=today,
        )
    except CatalystCalendarConflict as exc:
        if "driver_event" not in str(exc):
            raise
        return authority.publish(
            company_ref=company_ref, entries=entries,
            change_reason="driver_event", evidence_refs=evidence_refs,
            actor_ref=actor_ref, now=today,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved yfinance-calendar record")
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument(
        "--issuer", default=None,
        help="the company's SEC CIK; without one the run has only the vendor",
    )
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--actor-ref", default="core:catalyst-calendar-worker")
    parser.add_argument(
        "--as-of", default=None,
        help="the day this run is about; for replaying a captured artifact",
    )
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
            "status", "failure_reason", "calendar_status", "earnings_dates",
            "sec_status", "sec_release_count", "entry_count",
            "next_catalyst_date", "change_reason", "moved_entry_refs",
            "invocation_ref", "calendar_version_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_RAW_BYTES",
    "CatalystCalendarRunError",
    "build_parser",
    "main",
    "run",
]
