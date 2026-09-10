"""S5: read one ownership filing out of process, and say what happened in it.

A child for the reason every connector lane's child is one: it reaches the
network, and the writer's store thread must not be held while it does.

The order is the one the connector lanes converged on, and each step is here
because skipping it has cost this codebase a day at some point.

**Approval first.** The governance record has to be approved *and* still
describe the packaged contract for the operation being run. A record whose
contract moved is refused before SEC is touched, not discovered halfway
through a run. The operation is checked against the form as well: a Form 4
accession may not be read through the 13F approval, because that would be an
approval for one document spent on another.

**Artifact always.** The exact bytes SEC served are hashed into the raw spool
before a single tag is read out of them, and they are written even when the
parse then fails. That hash is what every parsed figure is bound to; without
it a number here would be a number with a citation and no evidence.

**Contract last.** The parsed wire is validated against the frozen output
schema before anything is emitted from it. A filing the contract cannot
describe is refused, not stored and explained afterwards.

**What this writes, and what it does not.** It writes a summary containing
typed ResearchEvent payloads and nothing else. The events are recorded by the
lane through P14a's ``record_event``, never from here: this process has no
mission and no business deciding that a director's sale is a fact about a
company Dalton covers. And it never writes a Claim, a figure or a statement
line -- see ``sec_ownership_core.OWNERSHIP_GRADE`` for why that is a rule
rather than an omission.
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
from .raw_spool import RawSpool
from .sec_ownership_adapter import (
    TRANSACTION_CODE_MEANINGS,
    SecOwnershipParseError,
    aggregate_holdings,
    compare_holdings,
    parse_beneficial_ownership,
    parse_form13f,
    parse_form144,
    parse_form4,
)
from .sec_ownership_core import (
    BENEFICIAL_OWNERSHIP_OPERATION,
    FILING_INDEX_DOCUMENT,
    PRIMARY_DOCUMENT,
    FORM13F_OPERATION,
    FORM144_OPERATION,
    FORM4_OPERATION,
    FORMS_BY_OPERATION,
    OPERATIONS,
    OWNERSHIP_EVIDENCE_TIER,
    SecOwnershipError,
    document_url,
    filing_index_url,
    information_table_name,
    invocation_ref as build_invocation_ref,
    ownership_identity,
    ownership_output_schema,
    primary_document_url,
)
from .store import canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# One primary document. The information table of a very large manager is the
# only thing here that is not tiny.
MAX_RAW_BYTES = 32 * 1024 * 1024
# How many events one filing may produce. A Form 4 with two hundred rows is a
# plan administrator's monthly batch, not two hundred decisions, and the tick
# summary is not the place to render it. What is beyond the cap is counted and
# named in the summary rather than silently dropped.
MAX_EVENTS_PER_RUN = 40
DEFAULT_USER_AGENT = "Dalton Research Agent OS SEC ownership lane (owner: lumos)"

KIND_BY_OPERATION = {
    FORM4_OPERATION: "insider_transaction",
    BENEFICIAL_OWNERSHIP_OPERATION: "ownership_change",
    FORM144_OPERATION: "ownership_change",
    FORM13F_OPERATION: "holdings_change",
}


class SecOwnershipRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    governance = ConnectorGovernance.load(path)
    identity = ownership_identity(operation)
    if not governance.approved:
        raise SecOwnershipRunError(
            f"the sec {operation} governance record is not approved"
        )
    if governance.capability_id != identity["capability_id"]:
        raise SecOwnershipRunError(
            "governance record covers a different capability; an approval for "
            "one ownership operation is not an approval for another"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise SecOwnershipRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise SecOwnershipRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def fetch_document(url: str, *, user_agent: str, timeout: float = 30.0) -> bytes:
    """One GET against a URL this module composed from an accession.

    Deliberately not general: the caller cannot hand in a URL, only an
    accession, and ``sec_ownership_core`` is the only thing that turns one
    into the other. SEC asks for an identifying User-Agent and publishes a
    ten-per-second limit; the lane's quota is two orders of magnitude below it.
    """

    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(MAX_RAW_BYTES + 1)


def _read_source(
    args: argparse.Namespace, *, fixture: str | None, url_path: str | None
) -> bytes:
    if fixture:
        return Path(fixture).expanduser().read_bytes()
    if url_path is None:  # pragma: no cover - guarded by the caller
        raise SecOwnershipRunError("nothing to read")
    return fetch_document(url_path, user_agent=args.user_agent)


def _spool(spool: RawSpool, raw: bytes) -> Any:
    """Bytes into the raw sink, before anything is read out of them."""

    if len(raw) > MAX_RAW_BYTES:
        raise SecOwnershipRunError("the document exceeds the spool byte ceiling")
    digest = hashlib.sha256(raw).hexdigest()
    sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
    sink.write(raw)
    return sink.finalize()


def form13f_documents(
    args: argparse.Namespace, spool: RawSpool
) -> tuple[str | None, bytes, list[dict[str, Any]]]:
    """The cover page and the information table of one 13F, both spooled.

    A 13F is not one document. ``primary_doc.xml`` is the cover page -- the
    manager's name, the period, the totals -- and the holdings live in a
    second file whose name the filer chooses and whose name only the filing's
    own ``index.json`` knows. Fetching the cover page and parsing *that* for
    holdings is how this returned a successful read of zero positions, which is
    the worst shape a bug can take here: a filing that says nothing changed.

    Three GETs, which is what the quota is set for: cover page, filing index,
    information table. All three are spooled before anything is parsed; the
    information table is the artifact every holding binds to, and the other two
    are companions the summary names.

    A ``13F-NT`` is the exception and is not a hole: it is a notice that
    another manager reports these holdings, it has no information table, and
    saying so is the correct outcome rather than an empty book.
    """

    if args.fixture_file:
        cover = (
            Path(args.cover_file).expanduser().read_text(encoding="utf-8")
            if args.cover_file else None
        )
        return cover, Path(args.fixture_file).expanduser().read_bytes(), []

    companions: list[dict[str, Any]] = []
    cover_raw = fetch_document(
        primary_document_url(args.holder_cik, args.accession),
        user_agent=args.user_agent,
    )
    cover_object = _spool(spool, cover_raw)
    companions.append({
        "document": PRIMARY_DOCUMENT, **cover_object.to_dict(),
    })
    index_raw = fetch_document(
        filing_index_url(args.holder_cik, args.accession), user_agent=args.user_agent
    )
    index_object = _spool(spool, index_raw)
    companions.append({
        "document": FILING_INDEX_DOCUMENT, **index_object.to_dict(),
    })
    name = information_table_name(json.loads(index_raw.decode("utf-8", "replace")))
    table_raw = fetch_document(
        document_url(args.holder_cik, args.accession, name), user_agent=args.user_agent
    )
    return cover_raw.decode("utf-8", "replace"), table_raw, companions


def _day(value: str | None) -> str | None:
    return None if not value else f"{value}T00:00:00+00:00"


def _form4_events(
    wire: dict[str, Any], *, filed_at: str | None, invocation: str, artifact_hash: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    form = wire["document_type"] + ("/A" if wire["is_amendment"] else "")
    for owner in wire["reporting_owners"]:
        for row in wire["transactions"]:
            occurred = (
                _day(row["transaction_date"])
                or _day(wire["period_of_report"])
                or _day(filed_at)
            )
            if occurred is None:
                continue
            code = row["transaction_code"]
            events.append({
                "kind": "insider_transaction",
                "occurred_at": occurred,
                "evidence_tier": OWNERSHIP_EVIDENCE_TIER,
                "payload": {
                    "accession": wire["accession"],
                    "form": form,
                    "owner_name": owner["owner_name"],
                    "owner_cik": owner["owner_cik"],
                    "role": owner["role"],
                    "transaction_code": code,
                    "transaction_meaning": TRANSACTION_CODE_MEANINGS.get(code or ""),
                    "transaction_date": row["transaction_date"],
                    "security_title": row["security_title"],
                    "shares": row["shares"],
                    "price_per_share": row["price_per_share"],
                    "acquired_disposed": row["acquired_disposed"],
                    "shares_owned_following": row["shares_owned_following"],
                    "direct_or_indirect": row["direct_or_indirect"],
                    "issuer_name": wire["issuer_name"],
                    "invocation_ref": invocation,
                    "artifact_hash": artifact_hash,
                    # The row *and the owner it is reported for*. A joint
                    # filing -- two spouses, a fund and its general partner --
                    # names several reporting owners against the same
                    # transaction rows, and keying on the row alone made every
                    # owner after the first a duplicate the ledger silently
                    # dropped. The row hash is the adapter's, so the event and
                    # the wire still cannot disagree about the numbers.
                    "event_key": content_hash({
                        "row": row["record_hash"],
                        "owner": owner["owner_cik"] or owner["owner_name"],
                    }),
                },
            })
    return events


def _beneficial_events(
    wire: dict[str, Any], *, filed_at: str | None, invocation: str, artifact_hash: str
) -> list[dict[str, Any]]:
    form = wire["form_type"] + ("/A" if wire["is_amendment"] else "")
    occurred = _day(wire["event_date"]) or _day(wire["date_of_signature"]) or _day(filed_at)
    if occurred is None:
        return []
    return [{
        "kind": "ownership_change",
        "occurred_at": occurred,
        "evidence_tier": OWNERSHIP_EVIDENCE_TIER,
        "payload": {
            "accession": wire["accession"],
            "form": form,
            "is_amendment": wire["is_amendment"],
            "amendment_no": wire["amendment_no"],
            "reporting_person": person["reporting_person_name"],
            "person_cik": person["reporting_person_cik"],
            "person_type": person["person_type"],
            "percent_of_class": person["percent_of_class"],
            "aggregate_shares": person["aggregate_shares"],
            "sole_voting_power": person["sole_voting_power"],
            "shared_voting_power": person["shared_voting_power"],
            "event_date": wire["event_date"],
            "security_class": wire["security_class_title"],
            "cusip": wire["cusip"],
            "purpose_text_hash": wire["purpose_text_hash"],
            "invocation_ref": invocation,
            "artifact_hash": artifact_hash,
            "event_key": person["record_hash"],
        },
    } for person in wire["reporting_persons"]]


def _form144_events(
    wire: dict[str, Any], *, filed_at: str | None, invocation: str, artifact_hash: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for notice in wire["notices"]:
        occurred = (
            _day(notice["approx_sale_date"]) or _day(wire["filing_date"]) or _day(filed_at)
        )
        if occurred is None:
            continue
        events.append({
            "kind": "ownership_change",
            "occurred_at": occurred,
            "evidence_tier": OWNERSHIP_EVIDENCE_TIER,
            "payload": {
                "accession": wire["accession"],
                "form": "144",
                "is_amendment": False,
                "amendment_no": None,
                "reporting_person": notice["seller_name"],
                "person_cik": None,
                # A 144 is a *notice of proposed sale*. It says an affiliate
                # has told the SEC they may sell; the sale may be smaller,
                # later, or never happen. The word "planned" is in the payload
                # so the judgement prompt cannot read it as a completed trade.
                "person_type": (
                    f"planned sale by {notice['relationship_to_issuer']}"
                    if notice["relationship_to_issuer"] else "planned sale"
                ),
                "percent_of_class": None,
                "aggregate_shares": notice["shares_to_be_sold"],
                "sole_voting_power": None,
                "shared_voting_power": None,
                "event_date": notice["approx_sale_date"],
                "security_class": notice["security_class_title"],
                "cusip": None,
                "purpose_text_hash": None,
                "invocation_ref": invocation,
                "artifact_hash": artifact_hash,
                "event_key": notice["record_hash"],
            },
        })
    return events


def _form13f_events(
    wire: dict[str, Any],
    comparison: dict[str, Any],
    *,
    company_cusips: frozenset[str],
    filed_at: str | None,
    invocation: str,
    artifact_hash: str,
) -> list[dict[str, Any]]:
    """One event per changed position, restricted to the covered company.

    A large manager holds thousands of names and Dalton covers five. Emitting
    the whole book would drown the ledger in positions nobody is tracking, so
    the CUSIPs the lane cares about are handed in and everything else is
    counted, not recorded.
    """

    occurred = _day(wire["report_calendar_or_quarter"]) or _day(filed_at)
    if occurred is None:
        return []
    form = wire["form_type"] + ("/A" if wire["is_amendment"] else "")
    rows = comparison["changes"] or [
        {
            "cusip": row["cusip"], "put_call": row["put_call"],
            "name_of_issuer": row["name_of_issuer"],
            "title_of_class": row["title_of_class"],
            # A first reading is not a change, and it says so.
            "action": "first_reading",
            "shares": row["shares_or_principal_amount"], "prior_shares": None,
            "share_change": None, "value_usd": row["value_usd"],
            "prior_value_usd": None, "record_hash": row["record_hash"],
        }
        # Through the same aggregation the comparison uses, so a position the
        # filer split across three lines is one first reading here too.
        for row in aggregate_holdings(wire["holdings"]).values()
    ]
    events: list[dict[str, Any]] = []
    for row in rows:
        if company_cusips and row["cusip"] not in company_cusips:
            continue
        if row["action"] == "unchanged":
            continue
        events.append({
            "kind": "holdings_change",
            "occurred_at": occurred,
            "evidence_tier": OWNERSHIP_EVIDENCE_TIER,
            "payload": {
                "accession": wire["accession"],
                "form": form,
                "holder_name": wire["holder_name"],
                "holder_cik": wire["holder_cik"],
                "quarter": wire["quarter"],
                "prior_quarter": comparison["prior_quarter"],
                "cusip": row["cusip"],
                "issuer_name": row["name_of_issuer"],
                "title_of_class": row["title_of_class"],
                "put_call": row["put_call"],
                "action": row["action"],
                "shares": row["shares"],
                "prior_shares": row["prior_shares"],
                "share_change": row["share_change"],
                "value_usd": row["value_usd"],
                "prior_value_usd": row["prior_value_usd"],
                "value_unit": wire["value_unit"],
                "value_unit_basis": wire["value_unit_basis"],
                "invocation_ref": invocation,
                "artifact_hash": artifact_hash,
                "event_key": row["record_hash"],
            },
        })
    return events


def run(args: argparse.Namespace) -> dict[str, Any]:  # noqa: PLR0915 - one linear run
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = (
        Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    )
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "operation": args.operation,
        "transport": "fixture" if args.fixture_file else "public-https",
        "company_ref": args.company_ref,
        "issuer": args.issuer,
        "accession": args.accession,
        "form_type": args.form_type,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "parsed_row_count": 0,
        "events": [],
        "event_count": 0,
        "events_over_cap": 0,
        "comparison_status": None,
        "prior_quarter": None,
        "value_unit": None,
        "value_unit_basis": None,
        # The 13F cover page and filing index, spooled beside the information
        # table that the holdings actually bind to.
        "companion_artifacts": [],
        "holdings_status": None,
    }
    try:
        if args.operation not in OPERATIONS:
            raise SecOwnershipRunError(f"{args.operation!r} is not an ownership operation")
        if bool(args.fixture_file) == bool(getattr(args, "allow_network", False)):
            raise SecOwnershipRunError(
                "exactly one of --fixture-file or --allow-network must be chosen"
            )
        # The form and the approval have to agree before anything is read. An
        # approval for one document spent on another is the failure this whole
        # per-operation split exists to prevent, and checking it here is what
        # makes the split load-bearing rather than decorative.
        if args.form_type not in FORMS_BY_OPERATION[args.operation]:
            raise SecOwnershipRunError(
                f"form {args.form_type!r} is not read by {args.operation}; that "
                f"operation reads {list(FORMS_BY_OPERATION[args.operation])}"
            )
        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), args.operation
        )
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        cover_text: str | None = None
        if args.operation == FORM13F_OPERATION:
            if args.prior_file and not args.prior_accession:
                # Without it every ``exit`` event would cite the *current*
                # filing as the source of a position that is only in the prior
                # one -- a citation that points at bytes which do not contain
                # the number it is offered for.
                raise SecOwnershipRunError(
                    "--prior-file needs --prior-accession; a prior quarter with "
                    "no accession of its own cannot be cited"
                )
            cover_text, raw, companions = form13f_documents(args, spool)
            summary["companion_artifacts"] = companions
        else:
            raw = _read_source(
                args,
                fixture=args.fixture_file,
                url_path=(
                    None if args.fixture_file
                    else primary_document_url(args.issuer, args.accession)
                ),
            )
        artifact = _spool(spool, raw)
        summary["artifact"] = artifact.to_dict()

        invocation = build_invocation_ref(
            operation=args.operation,
            governance_ref=governance.id,
            governance_hash=governance.content_hash,
            parameters={
                "issuer": args.issuer, "holder_cik": args.holder_cik,
                "filing_accession": args.accession, "quarter": args.quarter,
            },
            artifact_hash=artifact.content_hash,
        )
        summary["invocation_ref"] = invocation
        refs = [f"sec:filing:{args.accession}", f"raw-sink:{artifact.content_hash}"]
        text = raw.decode("utf-8", "replace")

        if args.operation == FORM4_OPERATION:
            wire = parse_form4(
                text, accession=args.accession, artifact_hash=artifact.content_hash,
                source_record_refs=refs,
            )
            events = _form4_events(
                wire, filed_at=args.filed_at, invocation=invocation,
                artifact_hash=artifact.content_hash,
            )
            summary["parsed_row_count"] = len(wire["transactions"])
        elif args.operation == BENEFICIAL_OWNERSHIP_OPERATION:
            wire = parse_beneficial_ownership(
                text, accession=args.accession, artifact_hash=artifact.content_hash,
                form_type=args.form_type, source_record_refs=refs,
            )
            events = _beneficial_events(
                wire, filed_at=args.filed_at, invocation=invocation,
                artifact_hash=artifact.content_hash,
            )
            summary["parsed_row_count"] = len(wire["reporting_persons"])
        elif args.operation == FORM144_OPERATION:
            wire = parse_form144(
                text, accession=args.accession, artifact_hash=artifact.content_hash,
                filing_date=args.filed_at, source_record_refs=refs,
            )
            events = _form144_events(
                wire, filed_at=args.filed_at, invocation=invocation,
                artifact_hash=artifact.content_hash,
            )
            summary["parsed_row_count"] = len(wire["notices"])
        else:
            wire = parse_form13f(
                text, accession=args.accession, artifact_hash=artifact.content_hash,
                holder_cik=args.holder_cik, primary_text=cover_text,
                quarter=args.quarter, source_record_refs=refs,
            )
            # A 13F-HR that parsed to nothing is not an institution that sold
            # everything. It is a document this run did not read -- the cover
            # page instead of the information table, a schema this parser does
            # not know, an empty response. Reporting it as a successful read of
            # zero positions is the one outcome that would put a fabricated
            # liquidation into the ledger, so it is a failure with the count in
            # the reason. A 13F-NT is the honest empty case and says so.
            if not wire["holdings"]:
                if wire["form_type"] != "13F-NT":
                    raise SecOwnershipRunError(
                        f"{wire['form_type']} {args.accession} parsed to zero "
                        "holdings; a holdings report with no holdings is a "
                        "document that was not read, not a book that is empty"
                    )
                summary["holdings_status"] = "notice_only"
            prior = None
            if args.prior_file:
                prior = parse_form13f(
                    Path(args.prior_file).expanduser().read_text(encoding="utf-8"),
                    accession=args.prior_accession,
                    artifact_hash=hashlib.sha256(
                        Path(args.prior_file).expanduser().read_bytes()
                    ).hexdigest(),
                    holder_cik=args.holder_cik,
                    primary_text=(
                        Path(args.prior_cover_file).expanduser().read_text(encoding="utf-8")
                        if args.prior_cover_file else None
                    ),
                )
            comparison = compare_holdings(wire, prior)
            summary["comparison_status"] = comparison["status"]
            summary["prior_quarter"] = comparison["prior_quarter"]
            summary["value_unit"] = wire["value_unit"]
            summary["value_unit_basis"] = wire["value_unit_basis"]
            events = _form13f_events(
                wire, comparison,
                company_cusips=frozenset(
                    cusip.strip().upper() for cusip in (args.company_cusips or "").split(",")
                    if cusip.strip()
                ),
                filed_at=args.filed_at, invocation=invocation,
                artifact_hash=artifact.content_hash,
            )
            summary["parsed_row_count"] = len(wire["holdings"])

        # Contract last: an observation the frozen schema cannot describe is
        # refused after the bytes are safe and before anything reads it.
        from .authority_resolver import _schema_matches

        _schema_matches(wire, ownership_output_schema(args.operation), "output")

        for event in events:
            event["source_refs"] = list(refs)
        summary["events_over_cap"] = max(0, len(events) - MAX_EVENTS_PER_RUN)
        summary["events"] = events[:MAX_EVENTS_PER_RUN]
        summary["event_count"] = len(summary["events"])
        summary["status"] = "succeeded"
    except (
        SecOwnershipRunError, SecOwnershipError, SecOwnershipParseError,
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
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--form-type", required=True,
                        help="the form as the filings index spelled it")
    parser.add_argument("--issuer", default=None, help="the issuer CIK")
    parser.add_argument("--holder-cik", default=None,
                        help="the filing manager's CIK, for 13F")
    parser.add_argument("--quarter", default=None)
    parser.add_argument("--filed-at", default=None,
                        help="the filing date, used when the document dates nothing")
    parser.add_argument("--company-cusips", default=None,
                        help="comma-separated CUSIPs the lane is tracking, for 13F")
    parser.add_argument("--cover-file", default=None,
                        help="the 13F cover page, already spooled")
    parser.add_argument("--prior-file", default=None,
                        help="the previous quarter's information table, already spooled")
    parser.add_argument("--prior-cover-file", default=None)
    parser.add_argument("--prior-accession", default=None)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--actor-ref", default="core:sec-ownership-worker")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured document instead of reaching SEC")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if args.operation == FORM13F_OPERATION and not args.holder_cik:
        parser.error("--holder-cik is required for form13f_holdings")
    if args.prior_file and not args.prior_accession:
        parser.error("--prior-file needs --prior-accession")
    if args.operation != FORM13F_OPERATION and not args.issuer:
        parser.error("--issuer is required for the issuer-keyed operations")
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "accession",
            "parsed_row_count", "event_count", "events_over_cap",
            "comparison_status", "prior_quarter", "value_unit",
            "value_unit_basis", "holdings_status", "invocation_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "DEFAULT_USER_AGENT",
    "form13f_documents",
    "KIND_BY_OPERATION",
    "MAX_EVENTS_PER_RUN",
    "MAX_RAW_BYTES",
    "SecOwnershipRunError",
    "build_parser",
    "fetch_document",
    "main",
    "run",
]
