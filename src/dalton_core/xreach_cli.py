"""S3: read X through `xreach` out of process, into a bounded wire.

Same discipline as every other connector child here, and each step is a
refusal cheaper than the one after it: approval first, then the credential
slots, then the artifact, then the contract.

**The credential part is the whole reason this is a child.** `xreach` holds
two X cookies in its own store. Dalton never reads them, never sees them and
does not know where they live. What a run carries is a grant envelope naming
two logical slots; without one covering both, the run is refused by name
before a process is spawned.

**Completeness is asserted, not assumed.** A handle's timeline paged to its
end is `enumerated`. A keyword search is `ranked` no matter how many pages it
returns, because what came back is what X chose to return. That word travels on
the wire so that a later reader cannot quietly upgrade it.

`x_search` is not built here and is not reachable from here. It is synthetic,
unpageable, and cannot be used to show that something does not exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_inventory import load_packaged_connector_inventory
from .crowd_credential_grants import (
    CrowdCredentialSlotUnbound,
    load_credential_grant,
    redacted,
    require_slots,
    slot_binding_summary,
)
from .lane_child_launcher import write_owner_only
from .raw_spool import RawSpool
from .xreach_core import (
    CREDENTIAL_SLOT_REFS,
    OPERATIONS,
    SEARCH_OPERATION,
    TEMPLATE_KEY,
    THREAD_OPERATION,
    USER_TIMELINE_OPERATION,
    xreach_identity,
)

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
MAX_RAW_BYTES = 8 * 1024 * 1024
MAX_POSTS = 400
MAX_COUNT = 100
DEFAULT_DEADLINE_SECONDS = 90.0


class XreachRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    governance = ConnectorGovernance.load(path)
    identity = xreach_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise XreachRunError(
            f"governance record covers {governance.capability_id}, not {operation}"
        )
    if not governance.approved:
        raise XreachRunError(
            f"xreach {operation} governance record is {governance.status}; "
            "owner approval is required"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise XreachRunError("governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise XreachRunError(
            "governance schema hash differs from the packaged contract; "
            "the approval does not cover this output contract"
        )
    return governance


def _output_schema(operation: str) -> dict[str, Any]:
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    ref = f"schema:connector-inventory:{TEMPLATE_KEY}:{operation}:output:0.1"
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise XreachRunError(f"packaged template has no {operation} output contract")


def tool_argv(
    operation: str,
    *,
    tool: str,
    handle: str | None,
    query: str | None,
    post_ref: str | None,
    count: int,
    cursor: str | None,
) -> list[str]:
    """The host tool's own command line.

    These are `xreach`'s real subcommands, which is why the frozen contract
    records them as `source_method`: `tweets`, `search`, `thread`. The Dalton
    operation names differ and are allowed to.
    """

    if operation == USER_TIMELINE_OPERATION:
        command = [tool, "--json", "tweets", str(handle), "--count", str(count)]
    elif operation == SEARCH_OPERATION:
        command = [tool, "--json", "search", str(query), "--count", str(count),
                   "--type", "latest"]
    elif operation == THREAD_OPERATION:
        command = [tool, "--json", "thread", str(post_ref)]
    else:
        raise XreachRunError(f"xreach has no {operation!r} operation")
    if cursor and operation != THREAD_OPERATION:
        command += ["--cursor", cursor]
    return command


def call_host_tool(command: Sequence[str], *, deadline_seconds: float) -> bytes:
    try:
        finished = subprocess.run(  # noqa: S603 - operator-installed tool path
            list(command), capture_output=True, timeout=deadline_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise XreachRunError(
            f"the xreach host tool is not installed at {command[0]!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise XreachRunError(
            f"xreach did not answer within {deadline_seconds:g}s"
        ) from exc
    if finished.returncode != 0:
        detail = finished.stderr.decode("utf-8", errors="replace").strip()[:300]
        # A 401 from X is an unbound or expired cookie, and saying so is more
        # use than repeating the exit code.
        raise XreachRunError(f"xreach exited {finished.returncode}: {detail}")
    if not finished.stdout.strip():
        raise XreachRunError("xreach printed nothing")
    return finished.stdout


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number >= 0 else None


def normalise_post(item: Mapping[str, Any]) -> dict[str, Any]:
    author = item.get("author")
    if isinstance(author, Mapping):
        handle = _text(author.get("username") or author.get("screen_name"))
        author_id = _text(author.get("id"))
    else:
        handle = _text(author or item.get("username"))
        author_id = _text(item.get("author_id"))
    post_id = _text(item.get("post_id") or item.get("id") or item.get("id_str"))
    if not post_id:
        raise XreachRunError("an X post arrived without an id")
    created_at = _text(item.get("created_at") or item.get("createdAt"))
    if not created_at:
        raise XreachRunError(f"X post {post_id} arrived without a timestamp")
    return {
        "post_id": post_id,
        "url": _text(item.get("url")),
        "created_at": created_at,
        "author": handle,
        "author_id": author_id,
        "text": _text(item.get("text") or item.get("full_text")),
        "reply_count": _counter(item.get("reply_count") or item.get("replyCount")),
        "like_count": _counter(item.get("like_count") or item.get("likeCount")),
        "repost_count": _counter(item.get("retweet_count") or item.get("retweetCount")),
        "view_count": _counter(item.get("view_count") or item.get("viewCount")),
        "is_reply": bool(item.get("is_reply") or item.get("in_reply_to_status_id")),
    }


def completeness_for(operation: str, *, next_cursor: str | None) -> str:
    """What this response is complete with respect to.

    A search is `ranked` whatever happens: the cursor means there are more
    ranked results, never that the set can be reconciled. A timeline or a
    thread is `enumerated` only when it has been paged to its end; while a
    cursor remains it is `partial`, which is the honest word for "there is more
    and we stopped".
    """

    if operation == SEARCH_OPERATION:
        return "ranked"
    return "partial" if next_cursor else "enumerated"


def build_wire(
    operation: str,
    *,
    raw: Any,
    source_record_refs: Sequence[str],
    provider_status: int = 200,
) -> dict[str, Any]:
    rows = raw
    next_cursor = None
    has_more = False
    if isinstance(raw, Mapping):
        next_cursor = _text(raw.get("next_cursor") or raw.get("cursor"))
        # `items` is what the tool actually returns; the rest are accepted
        # because a CLI that renames its envelope key should not silently
        # produce an empty timeline, which reads exactly like a quiet account.
        has_more = bool(raw.get("hasMore"))
        for key in ("items", "posts", "tweets", "results", "data"):
            if isinstance(raw.get(key), list):
                rows = raw[key]
                break
    if not isinstance(rows, list):
        raise XreachRunError("xreach returned no list of posts")
    posts = [normalise_post(row) for row in rows[:MAX_POSTS]
             if isinstance(row, Mapping)]
    return {
        "schema_version": "0.1",
        "operation": operation,
        "completeness": completeness_for(
            operation, next_cursor=next_cursor or ("more" if has_more else None)),
        "posts": posts,
        "source_record_refs": list(source_record_refs),
        "next_cursor": next_cursor,
        "provider_status": provider_status,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary_dir = Path(args.summary_dir).expanduser().resolve() if args.summary_dir else state
    # A refusal must be able to say why, and it cannot if the directory it
    # would say it in does not exist. The launcher always makes the ticket
    # directory first; a person running the child by hand does not.
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "connector": TEMPLATE_KEY,
        "operation": args.operation,
        "transport": "fixture" if args.fixture_file else "host-tool",
        "handle": args.handle,
        "query": args.query,
        "post_ref": args.post_ref,
        "since": args.since,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "credential": slot_binding_summary(None),
        "artifact": None,
        "record_count": 0,
        "observation": None,
    }
    try:
        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), args.operation)
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        grant = load_credential_grant(args.credential_grant) if args.credential_grant else None
        identity = xreach_identity(args.operation)
        summary["credential"] = require_slots(
            grant, slot_refs=list(CREDENTIAL_SLOT_REFS),
            operation=args.operation, target_ref=identity["adapter_ref"],
        )

        if args.fixture_file:
            payload = Path(args.fixture_file).expanduser().read_bytes()
        else:
            if not args.tool:
                raise XreachRunError(
                    "a networked run needs --tool; this Core does not know "
                    "where xreach lives"
                )
            payload = call_host_tool(
                tool_argv(
                    args.operation, tool=args.tool, handle=args.handle,
                    query=args.query, post_ref=args.post_ref,
                    count=args.count, cursor=args.cursor,
                ),
                deadline_seconds=args.deadline_seconds,
            )

        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise XreachRunError(f"xreach did not print JSON: {exc}") from exc
        if isinstance(raw, Mapping):
            raw = redacted(raw)
        wire = build_wire(
            args.operation, raw=raw,
            source_record_refs=[f"raw-sink:{artifact.content_hash}"],
        )
        if args.since:
            wire["posts"] = [post for post in wire["posts"]
                             if post["created_at"][:10] >= args.since]
        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(args.operation), "output")

        summary.update({
            "status": "succeeded", "observation": wire,
            "record_count": len(wire["posts"]),
        })
    except (XreachRunError, CrowdCredentialSlotUnbound, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True)
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--handle", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--post-ref", default=None)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--credential-grant", default=None,
                        help="host grant envelope naming both X cookie slots")
    parser.add_argument("--tool", default=None, help="the xreach executable")
    parser.add_argument("--deadline-seconds", type=float,
                        default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None)
    parser.add_argument("--emit-wire", action="store_true",
                        help="print the validated wire on stdout, for a runner "
                             "that records it rather than reading the summary")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.fixture_file) == bool(args.allow_network):
        parser.error("choose --fixture-file or --allow-network")
    if args.operation == USER_TIMELINE_OPERATION and not args.handle:
        parser.error("user_timeline needs --handle")
    if args.operation == SEARCH_OPERATION and not args.query:
        parser.error("search needs --query")
    if args.operation == THREAD_OPERATION and not args.post_ref:
        parser.error("thread needs --post-ref")
    if not 1 <= args.count <= MAX_COUNT:
        parser.error(f"--count must be 1..{MAX_COUNT}")
    if args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    summary = run(args)
    # One JSON document on stdout is the contract a host-tool runner reads:
    # it records the wire and never has to know where the summary was written.
    # The summary is written either way, because a refusal has no wire and
    # still has a reason.
    if args.emit_wire:
        print(json.dumps(summary["observation"], ensure_ascii=False))
    elif not args.quiet:
        print(json.dumps({key: summary[key] for key in (
            "status", "failure_reason", "operation", "record_count",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_COUNT",
    "MAX_POSTS",
    "MAX_RAW_BYTES",
    "XreachRunError",
    "build_parser",
    "build_wire",
    "call_host_tool",
    "completeness_for",
    "main",
    "normalise_post",
    "run",
    "tool_argv",
]
