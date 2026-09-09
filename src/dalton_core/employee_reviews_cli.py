"""S3: read Blind's employee reviews out of process, into a bounded wire.

No credential anywhere in this file. Blind is public HTTPS to one host, and
the connector's auth boundary says `none`, so unlike the other two crowd
children this one has no slot to check and refuses nothing on credential
grounds. Approval first, artifact always, contract last -- the rest is the
same.

**The body lock.** Blind releases the prose of its most recent page only.
Everything older comes back with a placeholder in place of the pros and cons,
and it is placeholder *text*, not an empty field: left alone it would pass a
word-frequency count as if it were what somebody wrote. So the substituted
prose is nulled and the row is marked `body_locked`, and the ratings, summary,
job group, location and date on that same row -- which are real -- are kept.
A rating series may use every row. Anything built from the prose is a sample of
the newest page and has to say so.

The reviews are read here rather than in a host tool because there is nothing
to read them with: the page is a Next.js document whose data sits in a flight
payload, and the parse is short and worth owning. It is the `sec-financials`
trade in miniature -- the bytes are fetched by this process rather than by
Dalton's own transport, so the whole response is hashed into the spool and
every row names the review it came from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .employee_reviews_core import (
    BLIND_HOST,
    OPERATION,
    RATING_DIMENSIONS,
    TEMPLATE_KEY,
    body_locked,
    employee_reviews_identity,
)
from .lane_child_launcher import write_owner_only
from .raw_spool import RawSpool

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_PAGES = 20
MAX_REVIEWS = 1000
PAGE_SIZE = 30
DEFAULT_DEADLINE_SECONDS = 60.0
USER_AGENT = "Dalton Research Agent OS (employee-reviews connector)"

_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
_REVIEWS_KEY = '"reviews":{"list":['
_COUNTS = re.compile(r'"filteredCount":(\d+),"totalCount":(\d+)')


class EmployeeReviewsRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _load_governance(path: Path) -> ConnectorGovernance:
    governance = ConnectorGovernance.load(path)
    identity = employee_reviews_identity()
    if governance.capability_id != identity["capability_id"]:
        raise EmployeeReviewsRunError("governance record covers a different capability")
    if not governance.approved:
        raise EmployeeReviewsRunError(
            f"employee-reviews governance record is {governance.status}; "
            "owner approval is required"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise EmployeeReviewsRunError(
            "governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise EmployeeReviewsRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _output_schema() -> dict[str, Any]:
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    ref = f"schema:connector-inventory:{TEMPLATE_KEY}:{OPERATION}:output:0.1"
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise EmployeeReviewsRunError("packaged template has no review output contract")


def review_page_url(employer_slug: str, page: int) -> str:
    """The one URL shape this connector may fetch, on the one allowed host."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,80}", employer_slug or ""):
        raise EmployeeReviewsRunError(
            f"{employer_slug!r} is not a Blind employer slug"
        )
    base = f"https://{BLIND_HOST}/company/{urllib.parse.quote(employer_slug)}/reviews"
    return base if page <= 1 else f"{base}?page={page}"


def fetch_page(url: str, *, deadline_seconds: float) -> bytes:
    """One page of HTML, with the host checked against the frozen allowlist."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != BLIND_HOST:
        raise EmployeeReviewsRunError(
            f"this connector may only reach https://{BLIND_HOST}"
        )
    request = urllib.request.Request(  # noqa: S310 - scheme and host checked above
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(request, timeout=deadline_seconds) as response:  # noqa: S310
            return response.read(MAX_RAW_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise EmployeeReviewsRunError(
                "this employer slug does not exist on the review site"
            ) from exc
        raise EmployeeReviewsRunError(f"the review site answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise EmployeeReviewsRunError(f"the review site was unreachable: {exc}") from exc


def _slice_array(text: str, start: int) -> str:
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise EmployeeReviewsRunError(
        "the review list in the page payload never closes; the page structure moved"
    )


def parse_page(html: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The reviews and the library counts out of one page's flight payload."""

    joined = "".join(_FLIGHT_CHUNK.findall(html))
    payload = joined.encode("utf-8", "ignore").decode("unicode_escape", errors="ignore")
    start = payload.find(_REVIEWS_KEY)
    if start < 0:
        return [], {}
    raw = _slice_array(payload, start + len(_REVIEWS_KEY) - 1)
    try:
        rows = json.loads(raw)
    except ValueError as exc:
        raise EmployeeReviewsRunError(
            f"the review list in the page payload is not JSON: {exc}"
        ) from exc
    counts: dict[str, Any] = {}
    match = _COUNTS.search(payload)
    if match:
        counts = {"filtered_count": int(match.group(1)),
                  "total_count": int(match.group(2))}
    return [row for row in rows if isinstance(row, Mapping)], counts


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rating(value: Any) -> str | None:
    """A rating as text, because a float is not what was read."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    text = str(value).strip()
    return text if re.fullmatch(r"(0|[1-9][0-9]*)([.][0-9]+)?", text) else None


def normalise_review(row: Mapping[str, Any]) -> dict[str, Any]:
    """One review row. Locked prose is nulled; everything real is kept."""

    review_id = _text(row.get("review_id") or row.get("id"))
    if not review_id:
        raise EmployeeReviewsRunError("a review arrived without an id")
    created_at = _text(row.get("created_at") or row.get("createdAt"))
    if not created_at:
        raise EmployeeReviewsRunError(f"review {review_id} arrived without a date")
    locked = body_locked(row.get("pros"))
    return {
        "review_id": review_id,
        "created_at": created_at,
        "summary": _text(row.get("summary")),
        "ratings": {name: _rating(row.get(name)) for name in RATING_DIMENSIONS},
        "body_locked": locked,
        "pros": None if locked else _text(row.get("pros")),
        "cons": None if locked else _text(row.get("cons")),
        "jobgroup": _text(row.get("jobgroup")),
        "location": _text(row.get("location") or row.get("memberLocation")),
    }


def build_wire(
    *,
    employer_slug: str,
    rows: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    source_record_refs: Sequence[str],
    since: str | None = None,
    provider_status: int = 200,
) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[:MAX_REVIEWS]:
        review = normalise_review(row)
        if review["review_id"] in seen:
            continue
        if since and review["created_at"][:10] < since:
            continue
        seen.add(review["review_id"])
        reviews.append(review)
    total = counts.get("total_count")
    return {
        "schema_version": "0.1",
        "employer_slug": employer_slug,
        "library_total": total if isinstance(total, int) else None,
        "body_locked_count": sum(1 for item in reviews if item["body_locked"]),
        "reviews": reviews,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
        "provider_status": provider_status,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "connector": TEMPLATE_KEY,
        "operation": OPERATION,
        "transport": "fixture" if args.fixture_file else "public-https",
        "employer_slug": args.employer_slug,
        "since": args.since,
        "pages": args.pages,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "artifact": None,
        "record_count": 0,
        "body_locked_count": 0,
        "observation": None,
    }
    try:
        governance = _load_governance(Path(args.governance).expanduser().resolve())
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        pages: list[bytes] = []
        if args.fixture_file:
            pages = [Path(args.fixture_file).expanduser().read_bytes()]
        else:
            for page in range(1, args.pages + 1):
                html = fetch_page(review_page_url(args.employer_slug, page),
                                  deadline_seconds=args.deadline_seconds)
                pages.append(html)
                # Parsed here only to decide whether there is another page.
                # A short page is the last page; the real parse happens once,
                # below, after the bytes have been hashed.
                if len(parse_page(html.decode("utf-8", errors="ignore"))[0]) < PAGE_SIZE:
                    break

        # The artifact is every page, whole, hashed before a field is read.
        payload = b"\n".join(pages)
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        rows: list[Mapping[str, Any]] = []
        counts: dict[str, Any] = {}
        for html in pages:
            found, page_counts = parse_page(html.decode("utf-8", errors="ignore"))
            counts.update(page_counts)
            rows.extend(found)
        if not rows:
            raise EmployeeReviewsRunError(
                "the employer page carried no reviews for this company"
            )
        wire = build_wire(
            employer_slug=args.employer_slug, rows=rows, counts=counts,
            source_record_refs=[f"raw-sink:{artifact.content_hash}"],
            since=args.since,
        )
        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(), "output")

        summary.update({
            "status": "succeeded", "observation": wire,
            "record_count": len(wire["reviews"]),
            "body_locked_count": wire["body_locked_count"],
        })
    except (EmployeeReviewsRunError, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True)
    parser.add_argument("--employer-slug", required=True)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--deadline-seconds", type=float,
                        default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured page instead of reaching the site")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if not 1 <= args.pages <= MAX_PAGES:
        parser.error(f"--pages must be 1..{MAX_PAGES}")
    if args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    summary = run(args)
    if not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "record_count", "body_locked_count",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_PAGES",
    "MAX_RAW_BYTES",
    "MAX_REVIEWS",
    "PAGE_SIZE",
    "EmployeeReviewsRunError",
    "build_parser",
    "build_wire",
    "fetch_page",
    "main",
    "normalise_review",
    "parse_page",
    "review_page_url",
    "run",
]
