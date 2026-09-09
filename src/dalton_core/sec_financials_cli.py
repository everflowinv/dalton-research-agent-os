"""P13ag: read one company's SEC statements out of process, into a bounded wire.

Runs as a child for the same reason every other lane child does: the parse
reaches the network and takes longer than the writer's request timeout, and the
writer's store thread cannot be held that long.

What it produces is one observation validated against the frozen output
contract, plus the raw parser output hashed into the spool. The hash is the
provenance the owner accepted in place of Dalton verifying the SEC bytes
itself: the parse is trusted, and every line names the accession it came from,
so any figure can be taken back to SEC and checked.

Two modes, and exactly one must be chosen. ``--allow-network`` parses a live
filing. ``--fixture-file`` replays a captured parse, which is how this is
tested without reaching SEC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .raw_spool import RawSpool
from .sec_financials_core import OPERATION, sec_financials_identity
from .sec_financials_normalise import (
    STATEMENT_BY_TYPE,
    build_wire,
    normalise_filing,
)
from .store import canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_FILINGS = 8
# One parsed filing is a few megabytes of JSON at most; this is a ceiling, not
# an expectation, and the spool refuses beyond it rather than growing.
MAX_RAW_BYTES = 64 * 1024 * 1024


class SecFinancialsRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_governance(path: Path) -> ConnectorGovernance:
    """The approved record, checked against the identity it claims to cover.

    A record that is merely present is not authority: it must be approved, and
    its frozen hashes must still describe the packaged contract. A drifted
    record is refused here rather than after the network call.
    """

    governance = ConnectorGovernance.load(path)
    if not governance.approved:
        raise SecFinancialsRunError(
            "SEC financial-statements governance record is not approved"
        )
    identity = sec_financials_identity()
    if governance.capability_id != identity["capability_id"]:
        raise SecFinancialsRunError("governance record covers a different capability")
    # The constructor checks these are well-formed hashes, not that they still
    # describe the packaged contract. That difference is the whole P13z lesson:
    # a record whose contract moved is refused here, not discovered mid-run.
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise SecFinancialsRunError("governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise SecFinancialsRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _output_schema() -> dict[str, Any]:
    template = load_packaged_connector_inventory()["templates"]["sec-financials"]
    ref = f"schema:connector-inventory:sec-financials:{OPERATION}:output:0.1"
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise SecFinancialsRunError("packaged template has no statements output contract")


def parse_live(ticker: str | None, cik: str | None, *, form: str, limit: int,
               user_agent: str) -> dict[str, Any]:
    """Parse the newest filings for one company. Imports the parser lazily.

    The import is here and not at module scope so that a Core without the
    optional extra installed still loads this module and refuses with a reason,
    rather than failing to import at all.
    """

    os.environ.setdefault("EDGAR_IDENTITY", user_agent)
    try:
        from edgar import Company
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise SecFinancialsRunError(
            "the SEC statements parser is not installed; "
            "install this package with the [sec-financials] extra"
        ) from exc

    company = Company(ticker) if ticker else Company(cik)
    filings = company.get_filings(form=form).latest(limit)
    if filings is None:
        raise SecFinancialsRunError(f"no {form} filing found for this company")
    # P13am: ``latest(1)`` returns one Filing and ``latest(n)`` returns a
    # collection, and the collection is not a list -- so testing for ``list``
    # wrapped the whole collection in a one-element list and then asked it for
    # its XBRL. The lane ran at depth one for a day without noticing, and
    # failed on every company the moment a model asked for history.
    #
    # A single filing is the thing that can answer ``xbrl``; anything else is
    # something to iterate.
    filings = [filings] if hasattr(filings, "xbrl") else list(filings)
    if not filings:
        raise SecFinancialsRunError(f"no {form} filing found for this company")
    parsed: list[dict[str, Any]] = []
    for filing in filings:
        xbrl = filing.xbrl()
        if xbrl is None:
            continue
        facts = xbrl.query().with_dimensions().to_dataframe()
        accessors = {
            "income": xbrl.statements.income_statement,
            "balance": xbrl.statements.balance_sheet,
            "cash": xbrl.statements.cashflow_statement,
        }
        statements: dict[str, Any] = {}
        for statement_type, name in STATEMENT_BY_TYPE.items():
            try:
                structure = accessors[name]().to_dataframe().to_dict("records")
            except Exception:  # noqa: BLE001 - a statement this filing lacks
                continue
            rows = facts[facts["statement_type"] == statement_type]
            statements[name] = {
                "structure": structure,
                "facts": rows.to_dict("records"),
            }
        parsed.append({
            "accession": str(filing.accession_no),
            "form": str(filing.form),
            "filed": str(filing.filing_date),
            "report_date": str(filing.report_date),
            "statements": statements,
        })
    return {
        "cik": str(company.cik),
        "entity_name": str(company.name),
        "filings": parsed,
    }


def _json_safe(value: Any) -> Any:
    """Plain JSON for the artifact, including whatever the parser hands back.

    The parser returns dataframe rows, so absent cells arrive as float NaN and
    numbers as numpy scalars. NaN is not JSON and is not a number: it is the
    parser's way of saying the filing had nothing there, so it becomes null.
    Anything else unrecognised is stringified rather than dropped -- the
    artifact is the parse, and losing part of it would defeat keeping it.
    """

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, int):
        return value
    # numpy scalars and pandas NA both answer here rather than above.
    for attr in ("item",):
        converter = getattr(value, attr, None)
        if callable(converter):
            try:
                return _json_safe(converter())
            except (TypeError, ValueError):
                break
    text = str(value)
    return None if text in {"nan", "NaN", "<NA>", "NaT", "None"} else text


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": OPERATION,
        "transport": "fixture" if args.fixture_file else "public-https",
        "ticker": args.ticker,
        "cik": args.cik,
        "form": args.form,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "filings": [],
        "line_count": 0,
        "dropped": {},
        "observation": None,
    }
    try:
        governance = _load_governance(Path(args.governance).expanduser().resolve())
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        if args.fixture_file:
            raw = json.loads(Path(args.fixture_file).expanduser().read_text(encoding="utf-8"))
        else:
            raw = parse_live(args.ticker, args.cik, form=args.form,
                             limit=args.limit, user_agent=args.user_agent)
        raw = _json_safe(raw)
        if not raw.get("filings"):
            raise SecFinancialsRunError("the parser returned no filing with XBRL")

        # The artifact is the parse, kept whole and hashed before anything is
        # read out of it: whatever the normaliser drops stays recoverable.
        payload = canonical_json(raw).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        filings, dropped = [], {}
        for filing in raw["filings"][:MAX_FILINGS]:
            result = normalise_filing(
                accession=filing["accession"], form=filing["form"],
                filed=filing["filed"], report_date=filing["report_date"],
                statements=filing.get("statements") or {},
            )
            filings.append(result)
            for reason, count in result["dropped"].items():
                dropped[reason] = dropped.get(reason, 0) + count

        wire = build_wire(
            cik=raw["cik"], entity_name=raw["entity_name"], filings=filings,
            source_record_refs=[f"raw-sink:{artifact.content_hash}"],
        )
        # Validate before recording: an observation the frozen contract cannot
        # describe is refused, not stored and explained later.
        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(), "output")

        summary.update({
            "status": "succeeded",
            "observation": wire,
            "line_count": sum(len(item["lines"]) for item in wire["filings"]),
            "dropped": dropped,
            "filings": [
                {"accession": item["accession"], "form": item["form"],
                 "report_date": item["report_date"], "lines": len(item["lines"])}
                for item in wire["filings"]
            ],
        })
    except (SecFinancialsRunError, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved sec-financial-statements record")
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--cik", default=None)
    parser.add_argument("--form", default="10-Q", choices=["10-Q", "10-K"])
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--user-agent", default="Dalton Research Agent OS <owner>")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured parse instead of reaching SEC")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if not args.ticker and not args.cik:
        parser.error("one of --ticker or --cik is required")
    if not 1 <= args.limit <= MAX_FILINGS:
        parser.error(f"--limit must be 1..{MAX_FILINGS}")
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "line_count", "filings", "dropped",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = ["MAX_FILINGS", "SecFinancialsRunError", "build_parser", "main", "run"]
