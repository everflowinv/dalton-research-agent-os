"""S5: ask the local changedetection.io what moved on a declared IR page.

The host-tool child.  It prints one closed observation wire on stdout and
nothing else, because those exact bytes are what
:class:`~dalton_core.host_tool_runner.HostToolRunner` hashes into the spool
before anything parses them.

**It reaches one address and it is loopback.**  ``127.0.0.1:5055`` by default,
overridable only to another loopback address -- a host tool that could be
pointed at a remote host would be a network connector wearing a host tool's
permissions, and the check is here rather than in a comment.

**It reads declared pages and reports the rest.**  Every watch the tool holds
is listed; only the ones declared in
``deploy/phase9/p9-us-it-services-ir-pages-v1.json`` are marked ``declared``,
and ``get_watch_diff`` refuses a watch that is not.  An operator can therefore
see that the shared tool is watching pages Dalton ignores, which is the point:
the alternative is a connector whose scope is whatever somebody last typed
into a local web UI.

**Absent is not broken.**  A Core with no changedetection instance gets
``unconfigured`` with the address it tried, and the lane reports it and does
nothing.  Most Cores are that Core.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .store import content_hash
from .ir_page_watch_core import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    DIFF_OPERATION,
    LIST_OPERATION,
    OPERATIONS,
    IrPageWatchError,
    declared_page,
    diff_hash,
    excerpt_of,
    host_of,
    line_change_counts,
    load_ir_page_declaration,
    normalise_url,
)

SCHEMA_VERSION = "0.1"
PROVIDER_STATUS = 200
MAX_WATCHES = 500
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class IrPageWatchRunError(RuntimeError):
    """The watch run cannot proceed as asked."""


def _finish(wire: dict[str, Any]) -> dict[str, Any]:
    """Name the whole observation, so a spooled wire is checkable on its own."""

    wire["content_hash"] = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    return wire


def _require_loopback(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise IrPageWatchRunError(
            f"{base_url!r} is not a loopback address; this connector talks to "
            "the changedetection instance on this machine and to nothing else"
        )
    return base_url.rstrip("/")


def _get_json(base_url: str, path: str, *, timeout: float) -> Any:
    import urllib.error
    import urllib.request

    url = f"{base_url}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read(MAX_SNAPSHOT_BYTES + 1))
    except urllib.error.URLError as exc:
        raise IrPageWatchRunError(f"changedetection is not answering at {url}: {exc}") from exc


def _get_text(base_url: str, path: str, *, timeout: float) -> str:
    import urllib.error
    import urllib.request

    url = f"{base_url}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.read(MAX_SNAPSHOT_BYTES + 1).decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise IrPageWatchRunError(f"changedetection is not answering at {url}: {exc}") from exc


def _stamp(value: Any) -> str | None:
    """A changedetection timestamp, as text, or None.

    The tool serves epoch seconds as an integer in some fields and an ISO
    string in others. Both become text here rather than a datetime, because
    the only thing anything downstream does with them is print them and
    compare them for equality.
    """

    if value in (None, "", 0):
        return None
    return str(value)[:64]


def list_watches_wire(
    watches: Mapping[str, Any], *, declaration: Mapping[str, Any], since: str | None
) -> dict[str, Any]:
    """Everything the tool is watching, and which of it Dalton may read."""

    rows: list[dict[str, Any]] = []
    undeclared = 0
    for watch_id, record in sorted(watches.items()):
        if not isinstance(record, Mapping):
            continue
        url = record.get("url")
        try:
            normalised = normalise_url(url)
        except IrPageWatchError:
            continue
        entry = declared_page(declaration, normalised)
        if entry is None:
            undeclared += 1
        history = record.get("history") or record.get("snapshots") or {}
        rows.append({
            "watch_id": str(watch_id)[:128],
            "url": normalised,
            "host": host_of(normalised),
            "title": (str(record["title"])[:200] if record.get("title") else None),
            "last_checked": _stamp(record.get("last_checked")),
            "last_changed": _stamp(record.get("last_changed")),
            "snapshot_count": (
                len(history) if isinstance(history, (dict, list))
                else int(record.get("snapshot_count") or 0)
            ),
            "paused": bool(record.get("paused")),
            "declared": entry is not None,
            "company_ref": None if entry is None else entry["company_ref"],
        })
    rows = rows[:MAX_WATCHES]
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "since": since,
        "watches": rows,
        "undeclared_count": undeclared,
        "source_record_refs": [f"ir-watch:{row['watch_id']}" for row in rows],
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


def watch_diff_wire(
    *,
    watch_id: str,
    record: Mapping[str, Any],
    previous: str | None,
    current: str,
    previous_at: str | None,
    changed_at: str | None,
    declaration: Mapping[str, Any],
) -> dict[str, Any]:
    """One change: this page, from these bytes to those bytes."""

    normalised = normalise_url(record.get("url"))
    entry = declared_page(declaration, normalised)
    if entry is None:
        raise IrPageWatchRunError(
            f"{normalised} is not a declared IR page; a watch nobody declared "
            "is a page nobody approved and it is not read"
        )
    current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
    previous_hash = (
        None if previous is None
        else hashlib.sha256(previous.encode("utf-8")).hexdigest()
    )
    added, removed = line_change_counts(previous, current)
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "watch_id": str(watch_id)[:128],
        "url": normalised,
        "host": host_of(normalised),
        "company_ref": entry["company_ref"],
        "title": (str(record["title"])[:200] if record.get("title") else entry["label"] or None),
        "changed_at": _stamp(changed_at),
        "previous_snapshot_at": _stamp(previous_at),
        "diff_hash": diff_hash(
            url=normalised,
            previous_snapshot_hash=previous_hash,
            current_snapshot_hash=current_hash,
        ),
        "previous_snapshot_hash": previous_hash,
        "current_snapshot_hash": current_hash,
        "added_line_count": added,
        "removed_line_count": removed,
        "excerpt": excerpt_of(previous, current),
        "source_record_refs": [f"ir-watch-snapshot:{current_hash}"],
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


def _fixture(path: str) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def run(args: argparse.Namespace) -> dict[str, Any]:
    declaration = load_ir_page_declaration(args.declaration)
    if args.fixture_file:
        payload = _fixture(args.fixture_file)
    else:
        base = _require_loopback(args.base_url)
        payload = None

    if args.operation == LIST_OPERATION:
        watches = (
            payload["watches"] if payload is not None
            else _get_json(base, "/api/v1/watch", timeout=args.timeout)
        )
        if not isinstance(watches, Mapping):
            raise IrPageWatchRunError("changedetection did not return a watch map")
        return list_watches_wire(
            watches, declaration=declaration, since=args.since
        )

    watch_id = args.watch_id
    if not watch_id:
        raise IrPageWatchRunError("--watch-id is required for get_watch_diff")
    if payload is not None:
        record = payload["watch"]
        history = payload.get("history") or {}
    else:
        record = _get_json(base, f"/api/v1/watch/{watch_id}", timeout=args.timeout)
        stamps = _get_json(
            base, f"/api/v1/watch/{watch_id}/history", timeout=args.timeout
        )
        if not isinstance(stamps, Mapping) or not stamps:
            raise IrPageWatchRunError(f"watch {watch_id} has no snapshot history")
        history = {
            stamp: _get_text(
                base, f"/api/v1/watch/{watch_id}/history/{stamp}", timeout=args.timeout
            )
            for stamp in sorted(stamps, key=str)[-2:]
        }
    if not isinstance(record, Mapping):
        raise IrPageWatchRunError(f"changedetection has no watch {watch_id}")
    ordered = sorted(history, key=str)
    if not ordered:
        raise IrPageWatchRunError(f"watch {watch_id} has no snapshot history")
    current_at = ordered[-1]
    previous_at = ordered[-2] if len(ordered) > 1 else None
    wire = watch_diff_wire(
        watch_id=watch_id,
        record=record,
        previous=None if previous_at is None else str(history[previous_at]),
        current=str(history[current_at]),
        previous_at=previous_at,
        changed_at=current_at,
        declaration=declaration,
    )
    if args.output_dir:
        # The fetched page itself, kept beside the run. The wire carries only
        # its hash, so without this the diff would name bytes nobody holds.
        target = Path(args.output_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        (target / f"snapshot-{wire['current_snapshot_hash'][:16]}.txt").write_text(
            str(history[current_at]), encoding="utf-8"
        )
    return wire


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--declaration", required=True,
                        help="the declared IR pages, per company")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--since", default=None)
    parser.add_argument("--watch-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured changedetection response")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        wire = run(args)
    except (IrPageWatchRunError, IrPageWatchError, KeyError, ValueError) as exc:
        # stderr, never stdout: stdout is the artifact, and a run that failed
        # must not leave a half-wire in the spool that parses as an answer.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(wire, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "LOOPBACK_HOSTS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_WATCHES",
    "PROVIDER_STATUS",
    "SCHEMA_VERSION",
    "IrPageWatchRunError",
    "build_parser",
    "list_watches_wire",
    "main",
    "run",
    "watch_diff_wire",
]
