"""W4: read one Hong Kong disclosure out of process, and say what was in it.

A child for the reason every connector lane's child is one: it reaches the
network, and the writer's store thread must not be held while it does.

The order is the one the connector lanes converged on, and every step is here
because skipping it has cost this codebase a day at some point.

**Approval first.** The governance record has to be approved *and* still
describe the packaged contract for the operation being run. A record whose
contract moved is refused before HKEX is touched. One operation's record
cannot run another's: reading what a company bought back is not permission to
read who its directors are.

**Artifact always.** The canonical capture is hashed into the raw spool before
a single figure is read out of it, and it is written even when the parse then
fails -- the capture that cannot be read is exactly the one somebody will want
to look at. In network mode the exact upstream bytes are spooled beside it and
their SHA-256 is inside the capture, so the hashed object is bound to the bytes
the Exchange served.

**The clock is not part of what was said.** The capture carries
``captured_at`` and ``observed_on``; both are lifted out before the artifact is
hashed (:data:`CLOCK_FIELDS`). With them inside, every run of an unchanged day
minted a new artifact hash and therefore a new ``invocation_ref``, and telling
"the same fact read twice" from "two different facts" is the entire job of an
invocation ref. S4 learned this the expensive way; this inherits the lesson.

**Replay answers the question that was asked.** In fixture mode the capture's
own parameters must match the command line word for word. A 00700 capture
replayed under ``--hk-ticker 00001`` would produce a wire that validates, says
00001, and is entirely about Tencent, and nothing downstream could notice.

**Contract last.** The wire is validated against the frozen output schema
before anything is emitted from it, and a wire that fails is not written at all.

**What this writes, and what it does not.** A summary holding typed
ResearchEvent payloads, their derived context, and the validated wire. The
events are recorded by the lane through P14a's ``record_event``, never from
here: this process has no mission and no business deciding that a Hong Kong
company's buy-back is a fact about a company Dalton covers.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .hkex_filings_adapter import (
    HkexFilingsParseError,
    MAX_DI_NOTICES,
    buyback_events,
    di_corporation_from_list,
    di_events,
    di_notice_rows,
    parse_capture,
    stock_id_from_prefix,
)
from .hkex_filings_core import (
    ANNOUNCEMENTS_INDEX_OPERATION,
    CAPTURE_SCHEMA_VERSION,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    DAILY_BUYBACK_TAPE_OPERATION,
    HKEX_EVIDENCE_TIER,
    HkexFilingsError,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    OPERATIONS,
    SOURCE_REF,
    TIER_ONE_ALL,
    TIER_ONE_MONTHLY_RETURNS,
    USER_AGENT,
    company_ref as build_company_ref,
    di_all_form_list_url,
    di_corp_list_url,
    di_form_detail_url,
    hkex_allowed_hosts,
    hkex_identity,
    hkex_output_schema,
    invocation_ref as build_invocation_ref,
    normalise_ticker,
    share_buyback_report_url,
    stock_prefix_url,
    title_search_url,
)
from .raw_spool import RawSpool
from .store import canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_RAW_BYTES = 16 * 1024 * 1024
# How many events one run may produce. A month of daily buy-backs is twenty and
# a busy DI window is forty; what is beyond the cap is counted and named rather
# than dropped in silence.
MAX_EVENTS_PER_RUN = 60
# Recorded on the capture and excluded from the artifact hash. When the
# document was read is a fact about this process, not about the document.
CLOCK_FIELDS: tuple[str, ...] = ("captured_at", "observed_on")


class HkexFilingsRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _write_owner_only(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_bytes_owner_only(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    governance = ConnectorGovernance.load(path)
    identity = hkex_identity(operation)
    if not governance.approved:
        raise HkexFilingsRunError(
            f"the hkex-filings {operation} governance record is not approved"
        )
    if governance.capability_id != identity["capability_id"]:
        raise HkexFilingsRunError(
            "governance record covers a different capability; an approval for "
            "one Hong Kong operation is not an approval for another"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise HkexFilingsRunError(
            "governance source hash differs from the packaged template"
        )
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise HkexFilingsRunError(
            "governance schema hash differs from the packaged contract; the "
            "approval does not cover this output contract"
        )
    return governance


def fetch(url: str, *, operation: str, timeout: float = 60.0) -> tuple[bytes, str]:
    """One GET against a URL this connector composed, on a host it declared.

    The host check is here as well as in the profile because a redirect is how
    a declared host turns into an undeclared one, and www.hkexnews.hk really
    does redirect the buy-back report path somewhere that is not it.
    """

    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    allowed = hkex_allowed_hosts(operation)
    if host not in allowed:
        raise HkexFilingsRunError(
            f"{operation} may reach {list(allowed)} and this URL is on {host!r}"
        )
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read(MAX_RAW_BYTES + 1)
        content_type = response.headers.get("Content-Type", "")
    if len(raw) > MAX_RAW_BYTES:
        raise HkexFilingsRunError("the document exceeds the byte ceiling")
    return raw, content_type.split(";")[0].strip() or "application/octet-stream"


def _document(url: str, role: str, raw: bytes, content_type: str,
              **extra: Any) -> dict[str, Any]:
    return {
        "role": role,
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "content_type": content_type,
        **extra,
    }


def _workbook_grid(raw: bytes) -> list[list[str]]:
    """The Exchange's workbook as the text it holds, cell by cell.

    The one place in this connector where a third-party library appears, and it
    is optional for the reason every optional parser in this repository is:
    without it the run refuses with its reason rather than guessing, and the
    fixture path -- which is what the tests use -- never touches it.
    """

    try:
        import xlrd
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the venv
        raise HkexFilingsRunError(
            "reading the Exchange's share buy-back report needs the optional "
            "`hk-filings` extra (xlrd); HKEXnews publishes that report as a "
            "workbook and in no other form"
        ) from exc
    book = xlrd.open_workbook(file_contents=raw)
    if book.nsheets != 1:
        raise HkexFilingsRunError(
            f"the buy-back report has {book.nsheets} sheets rather than one"
        )
    sheet = book.sheet_by_index(0)
    grid: list[list[str]] = []
    for index in range(sheet.nrows):
        row: list[str] = []
        for cell in sheet.row(index):
            if cell.ctype == xlrd.XL_CELL_TEXT:
                row.append(str(cell.value))
            elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                row.append("")
            else:
                raise HkexFilingsRunError(
                    f"row {index} of the buy-back report holds a non-text cell; "
                    "this report has always been text and a number here would "
                    "need a rendering decision nobody has made"
                )
        grid.append(row)
    return grid


def _capture_envelope(operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "operation": operation,
        "parameters": parameters,
        "captured_at": now.isoformat(timespec="microseconds"),
        "observed_on": now.date().isoformat(),
        "documents": [],
    }


def _parameters(args: argparse.Namespace) -> dict[str, Any]:
    ticker = normalise_ticker(args.hk_ticker)
    if args.operation == NEXT_DAY_DISCLOSURE_OPERATION:
        return {"hk_ticker": ticker, "as_of": args.as_of}
    parameters = {"hk_ticker": ticker, "since": args.since, "until": args.until}
    if args.operation == ANNOUNCEMENTS_INDEX_OPERATION:
        parameters["tier_one"] = args.headline_category or TIER_ONE_ALL
    if args.operation == MONTHLY_RETURNS_OPERATION:
        parameters["tier_one"] = TIER_ONE_MONTHLY_RETURNS
    return parameters


def capture_live(args: argparse.Namespace, spool: RawSpool) -> dict[str, Any]:
    """Reach HKEX and build the canonical capture, spooling every byte served."""

    parameters = _parameters(args)
    capture = _capture_envelope(args.operation, parameters)
    ticker = parameters["hk_ticker"]

    def spooled(url: str, role: str, **extra: Any) -> tuple[bytes, dict[str, Any]]:
        raw, content_type = fetch(url, operation=args.operation)
        digest = hashlib.sha256(raw).hexdigest()
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(raw)
        sink.finalize()
        document = _document(url, role, raw, content_type, **extra)
        capture["documents"].append(document)
        return raw, document

    if args.operation == NEXT_DAY_DISCLOSURE_OPERATION:
        raw, document = spooled(
            share_buyback_report_url(args.as_of), "share_buyback_report"
        )
        document["grid"] = _workbook_grid(raw)
        return capture

    if args.operation in (MONTHLY_RETURNS_OPERATION, ANNOUNCEMENTS_INDEX_OPERATION):
        prefix_raw, prefix_document = spooled(stock_prefix_url(ticker), "stock_prefix")
        prefix_text = prefix_raw.decode("utf-8", "replace")
        prefix_document["text"] = prefix_text
        stock_id = stock_id_from_prefix(prefix_text, ticker=ticker)
        search_raw, search_document = spooled(
            title_search_url(
                stock_id=stock_id, since=args.since, until=args.until,
                tier_one=parameters["tier_one"],
            ),
            "title_search",
        )
        search_document["text"] = search_raw.decode("utf-8", "replace")
        return capture

    corp_raw, corp_document = spooled(
        di_corp_list_url(ticker=ticker, since=args.since, until=args.until),
        "di_corp_list",
    )
    corp_text = corp_raw.decode("utf-8", "replace")
    corp_document["text"] = corp_text
    corporation = di_corporation_from_list(corp_text, ticker=ticker)
    list_raw, list_document = spooled(
        di_all_form_list_url(
            sid=corporation["sid"],
            corporation_name=corporation["corporation_name"],
            ticker=ticker, since=args.since, until=args.until,
        ),
        "di_form_list",
    )
    list_text = list_raw.decode("utf-8", "replace")
    list_document["text"] = list_text
    ceiling = max(0, min(int(args.max_forms), MAX_DI_NOTICES))
    for row in di_notice_rows(list_text)[:ceiling]:
        if not row["form_path"]:
            continue
        detail_raw, detail_document = spooled(
            di_form_detail_url(
                form_path=row["form_path"], sid=corporation["sid"],
                corporation_name=corporation["corporation_name"],
                ticker=ticker, since=args.since, until=args.until,
            ),
            f"di_form:{row['form_serial_number']}",
        )
        detail_document["text"] = detail_raw.decode("utf-8", "replace")
    return capture


def replayed_capture(args: argparse.Namespace) -> dict[str, Any]:
    """A captured read, checked against the question this run was asked."""

    capture = json.loads(Path(args.fixture_file).expanduser().read_text("utf-8"))
    if capture.get("operation") != args.operation:
        raise HkexFilingsRunError(
            f"this capture is a {capture.get('operation')!r} read and this run "
            f"is a {args.operation!r} one"
        )
    wanted = _parameters(args)
    held = dict(capture.get("parameters") or {})
    if held != wanted:
        raise HkexFilingsRunError(
            "the capture was taken for different parameters and replaying it "
            f"would answer a question nobody asked: capture={held}, run={wanted}"
        )
    return capture


def artifact_payload(capture: dict[str, Any]) -> dict[str, Any]:
    """The capture with the clock taken out, which is what gets hashed."""

    payload = copy.deepcopy(capture)
    for field in CLOCK_FIELDS:
        payload.pop(field, None)
    return payload


def spool_capture(spool: RawSpool, capture: dict[str, Any]) -> Any:
    raw = canonical_json(artifact_payload(capture)).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
    sink.write(raw)
    return sink.finalize()


def _daily_acquisition(*, state: Path, as_of: str,
                       governance: ConnectorGovernance, spool: RawSpool) -> dict[str, Any]:
    """Return one verified, day-scoped market tape under an interprocess lock."""
    url = share_buyback_report_url(as_of)
    identity = hkex_identity(DAILY_BUYBACK_TAPE_OPERATION)
    key_payload = {
        "operation": DAILY_BUYBACK_TAPE_OPERATION, "as_of": as_of, "url": url,
        "governance_ref": governance.id, "governance_hash": governance.content_hash,
        "source_hash": identity["source_hash"], "schema_hash": identity["schema_hash"],
    }
    key = content_hash(key_payload)
    root = state / "hkex-daily-acquisitions" / key
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    manifest_path, source_path = root / "manifest.json", root / "source.xls"
    descriptor = os.open(root / "acquisition.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text("utf-8"))
                if manifest.get("key") != key or manifest.get("identity") != key_payload:
                    raise HkexFilingsRunError("daily acquisition cache manifest identity is corrupt")
                raw = source_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != manifest.get("source_sha256"):
                    raise HkexFilingsRunError("daily acquisition cached source bytes are corrupt")
                capture = manifest.get("capture")
                if not isinstance(capture, dict):
                    raise HkexFilingsRunError("daily acquisition cached capture is corrupt")
                digest = hashlib.sha256(canonical_json(artifact_payload(capture)).encode("utf-8")).hexdigest()
                if digest != (manifest.get("artifact") or {}).get("content_hash"):
                    raise HkexFilingsRunError("daily acquisition cached artifact is corrupt")
                locator = str((manifest.get("artifact") or {}).get("storage_locator") or "")
                if not re.fullmatch(r"spool:objects/[0-9a-f]{2}/[0-9a-f]{64}", locator):
                    raise HkexFilingsRunError("daily acquisition cached artifact locator is corrupt")
                if locator.rsplit("/", 1)[-1] != digest:
                    raise HkexFilingsRunError("daily acquisition cached artifact locator is corrupt")
                artifact_bytes = (state / DEFAULT_SPOOL_NAME / "connector-spool" /
                                  locator.removeprefix("spool:")).read_bytes()
                if hashlib.sha256(artifact_bytes).hexdigest() != digest:
                    raise HkexFilingsRunError("daily acquisition spooled artifact is corrupt")
                expected_invocation = build_invocation_ref(
                    operation=DAILY_BUYBACK_TAPE_OPERATION,
                    governance_ref=governance.id,
                    governance_hash=governance.content_hash,
                    parameters={"as_of": as_of}, artifact_hash=digest,
                )
                if manifest.get("invocation_ref") != expected_invocation:
                    raise HkexFilingsRunError("daily acquisition cached invocation is corrupt")
                documents = capture.get("documents") or []
                if len(documents) != 1 or not isinstance(documents[0], dict):
                    raise HkexFilingsRunError("daily acquisition cached document is corrupt")
                document = documents[0]
                expected_wire = {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "operation": DAILY_BUYBACK_TAPE_OPERATION,
                    "report_printed_on": as_of, "report_url": url,
                    "report_sha256": hashlib.sha256(raw).hexdigest(),
                    "universe_grid": document.get("grid"),
                    "artifact_hash": digest,
                    "source_record_refs": [SOURCE_REF, f"raw-sink:{digest}"],
                    "next_cursor": None, "provider_status": 200,
                }
                if document.get("url") != url or document.get("sha256") != expected_wire["report_sha256"]:
                    raise HkexFilingsRunError("daily acquisition cached document identity is corrupt")
                if manifest.get("wire") != expected_wire:
                    raise HkexFilingsRunError("daily acquisition cached output is corrupt")
                from .authority_resolver import _schema_matches
                _schema_matches(expected_wire, hkex_output_schema(DAILY_BUYBACK_TAPE_OPERATION),
                                "cached daily acquisition output")
                return dict(manifest, cache_status="hit")
            raw, content_type = fetch(url, operation=DAILY_BUYBACK_TAPE_OPERATION)
            source_sha = hashlib.sha256(raw).hexdigest()
            raw_sink = spool.open_sink(f"raw-sink:{source_sha}", max_response_bytes=MAX_RAW_BYTES)
            raw_sink.write(raw)
            raw_sink.finalize()
            capture = _capture_envelope(DAILY_BUYBACK_TAPE_OPERATION, {"as_of": as_of})
            document = _document(url, "share_buyback_report", raw, content_type)
            document["grid"] = _workbook_grid(raw)
            capture["documents"].append(document)
            artifact = spool_capture(spool, capture)
            daily_wire = {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "operation": DAILY_BUYBACK_TAPE_OPERATION,
                "report_printed_on": as_of, "report_url": url,
                "report_sha256": source_sha, "universe_grid": document["grid"],
                "artifact_hash": artifact.content_hash,
                "source_record_refs": [SOURCE_REF, f"raw-sink:{artifact.content_hash}"],
                "next_cursor": None, "provider_status": 200,
            }
            from .authority_resolver import _schema_matches
            _schema_matches(daily_wire, hkex_output_schema(DAILY_BUYBACK_TAPE_OPERATION),
                            "daily acquisition output")
            invocation = build_invocation_ref(
                operation=DAILY_BUYBACK_TAPE_OPERATION, governance_ref=governance.id,
                governance_hash=governance.content_hash, parameters={"as_of": as_of},
                artifact_hash=artifact.content_hash,
            )
            manifest = {"schema_version": "0.1", "key": key, "identity": key_payload,
                        "source_sha256": source_sha, "artifact": artifact.to_dict(),
                        "invocation_ref": invocation, "capture": capture,
                        "wire": daily_wire}
            _write_bytes_owner_only(source_path, raw)
            _write_owner_only(manifest_path, manifest)
            return dict(manifest, cache_status="miss")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HkexFilingsRunError(f"daily acquisition cache is unreadable: {exc}") from exc


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
        "company_ref": None,
        "hk_ticker": None,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "invocation_ref": None,
        "derived_view_ref": None,
        "acquisition": None,
        "source_documents": [],
        "parsed_row_count": 0,
        "universe_row_count": None,
        "record_count": None,
        "events": [],
        "event_count": 0,
        "events_over_cap": 0,
        "caliber_notes": [],
        "allowed_hosts": [],
        "wire": None,
    }
    try:
        if args.operation not in OPERATIONS:
            raise HkexFilingsRunError(
                f"{args.operation!r} is not an hkex-filings operation"
            )
        if bool(args.fixture_file) == bool(getattr(args, "allow_network", False)):
            raise HkexFilingsRunError(
                "exactly one of --fixture-file or --allow-network must be chosen"
            )
        ticker = normalise_ticker(args.hk_ticker)
        company_ref = args.company_ref or build_company_ref(ticker)
        summary["hk_ticker"] = ticker
        summary["company_ref"] = company_ref
        summary["allowed_hosts"] = list(hkex_allowed_hosts(args.operation))

        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), args.operation
        )
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        if (args.operation == NEXT_DAY_DISCLOSURE_OPERATION and not args.fixture_file
                and getattr(args, "daily_buyback_tape_governance", None)):
            acquisition_governance = _load_governance(
                Path(args.daily_buyback_tape_governance).expanduser().resolve(),
                DAILY_BUYBACK_TAPE_OPERATION,
            )
            acquisition = _daily_acquisition(
                state=state, as_of=args.as_of, governance=acquisition_governance,
                spool=spool,
            )
            capture = copy.deepcopy(acquisition["capture"])
            capture["operation"] = NEXT_DAY_DISCLOSURE_OPERATION
            capture["parameters"] = _parameters(args)
            artifact_dict = acquisition["artifact"]
            artifact_hash = artifact_dict["content_hash"]
            invocation = acquisition["invocation_ref"]
            summary["acquisition"] = {key: acquisition[key] for key in (
                "key", "identity", "artifact", "invocation_ref", "cache_status"
            )}
            summary["derived_view_ref"] = "hkex-derived-view:" + content_hash({
                "acquisition_invocation_ref": invocation, "operation": args.operation,
                "parameters": _parameters(args),
            })[:32]
        else:
            capture = replayed_capture(args) if args.fixture_file else capture_live(args, spool)
            artifact = spool_capture(spool, capture)
            artifact_dict = artifact.to_dict()
            artifact_hash = artifact.content_hash
            invocation = build_invocation_ref(
                operation=args.operation, governance_ref=governance.id,
                governance_hash=governance.content_hash, parameters=_parameters(args),
                artifact_hash=artifact_hash,
            )
        summary["artifact"] = artifact_dict
        summary["source_documents"] = [
            {key: document[key] for key in ("role", "url", "sha256", "byte_length",
                                            "content_type")}
            for document in capture.get("documents", [])
        ]

        summary["invocation_ref"] = invocation
        refs = [
            f"{SOURCE_REF}:{ticker}",
            f"raw-sink:{artifact_hash}",
        ]

        wire = parse_capture(
            args.operation, capture, ticker=ticker,
            artifact_hash=artifact_hash, source_record_refs=refs,
        )

        # Contract last: a read the frozen schema cannot describe is refused
        # after the bytes are safe and before anything is emitted from it.
        from .authority_resolver import _schema_matches

        _schema_matches(wire, hkex_output_schema(args.operation), "output")

        prior_rows = _prior_rows(args)
        if args.operation == NEXT_DAY_DISCLOSURE_OPERATION:
            events = buyback_events(
                wire, company_ref=company_ref, invocation_ref=invocation,
                artifact_hash=artifact_hash, prior_rows=prior_rows,
                current_price=args.current_price,
            )
            summary["universe_row_count"] = wire["universe_row_count"]
        elif args.operation == DISCLOSURE_OF_INTERESTS_OPERATION:
            events = di_events(
                wire, company_ref=company_ref, invocation_ref=invocation,
                artifact_hash=artifact_hash,
                prior_rows=prior_rows or wire["rows"],
            )
        else:
            # The two index operations are an index. They tell a filings index
            # that a buy-back mandate, a placing or a results date exists; they
            # do not claim to have read one, so they emit nothing.
            events = []
            summary["record_count"] = wire["record_count"]

        for event in events:
            event["source_refs"] = list(refs)
            event["evidence_tier"] = HKEX_EVIDENCE_TIER
        summary["events_over_cap"] = max(0, len(events) - MAX_EVENTS_PER_RUN)
        summary["events"] = events[:MAX_EVENTS_PER_RUN]
        summary["event_count"] = len(summary["events"])
        summary["parsed_row_count"] = wire["row_count"]
        summary["caliber_notes"] = sorted({
            row["caliber_note"] for row in wire["rows"]
            if isinstance(row, dict) and row.get("caliber_note")
        })
        summary["wire"] = wire
        summary["status"] = "succeeded"
    except (
        HkexFilingsRunError, HkexFilingsError, HkexFilingsParseError,
        ConnectorGovernanceError,
    ) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    _write_owner_only(summary_dir / "summary.json", summary)
    return summary


def _prior_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Earlier rows the lane already read, handed in so the context is replayable.

    A file rather than a lookup: this process has no database, and a derived
    comparison that reached for one would stop being a function of its inputs.
    """

    if not args.prior_rows_file:
        return []
    payload = json.loads(Path(args.prior_rows_file).expanduser().read_text("utf-8"))
    if not isinstance(payload, list):
        raise HkexFilingsRunError("--prior-rows-file must hold a list of rows")
    return [row for row in payload if isinstance(row, dict)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved record for this exact operation")
    parser.add_argument("--daily-buyback-tape-governance", default=None,
                        help="approved day-scoped acquisition record; absent keeps legacy reads")
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--hk-ticker", required=True,
                        help="the Hong Kong stock code, e.g. 00700")
    parser.add_argument("--company-ref", default=None,
                        help="defaults to company:hk-secucode:<code>.HK")
    parser.add_argument("--as-of", default=None,
                        help="the day the share buy-back report was printed")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--headline-category", default=None,
                        help="HKEXnews tier-one code; -2 means every category")
    parser.add_argument("--max-forms", type=int, default=MAX_DI_NOTICES,
                        help="how many DI notices' own forms to read")
    parser.add_argument("--prior-rows-file", default=None,
                        help="rows this lane already read, for the derived context")
    parser.add_argument("--current-price", default=None,
                        help="the latest close, when a price authority can hold one")
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--actor-ref", default="core:hkex-filings-worker")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured read instead of reaching HKEX")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if args.operation == NEXT_DAY_DISCLOSURE_OPERATION and not args.as_of:
        parser.error("--as-of is required for next_day_disclosure_returns")
    if args.operation != NEXT_DAY_DISCLOSURE_OPERATION and not (
        args.since and args.until
    ):
        parser.error("--since and --until are required for a windowed operation")
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "hk_ticker", "company_ref",
            "parsed_row_count", "universe_row_count", "record_count",
            "event_count", "events_over_cap", "invocation_ref",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "CLOCK_FIELDS",
    "MAX_EVENTS_PER_RUN",
    "MAX_RAW_BYTES",
    "HkexFilingsRunError",
    "artifact_payload",
    "build_parser",
    "capture_live",
    "fetch",
    "main",
    "replayed_capture",
    "run",
    "spool_capture",
]
