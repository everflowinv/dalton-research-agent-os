"""S3: read Xueqiu posts out of process, into a bounded wire.

Runs as a child for the same reason every other lane child does: the read
reaches a host tool over the network and takes longer than the writer's request
timeout, and the writer's store thread cannot be held that long.

The order is the one every connector child in this repository keeps, and each
step of it is a refusal that is cheaper than the step after it:

1. **Approval first.** An unapproved or drifted governance record ends the run
   before anything is spawned.
2. **The credential slot next.** The two post operations need the host's Xueqiu
   cookie. This process never reads it; it is shown a grant envelope naming the
   slot, and refuses by name when there is none. `hot_rank` is exempt, because
   its fallback route is credential-free.
3. **Artifact always.** Whatever the host tool printed is hashed into the spool
   before a single field is read out of it, so anything the normaliser drops
   stays recoverable.
4. **Contract last.** The normalised wire is validated against the frozen
   output schema, and an observation the contract cannot describe is refused
   rather than stored and explained afterwards.

Two modes, exactly one of which must be chosen: `--allow-network` drives the
host tool, `--fixture-file` replays a captured run, which is how this is tested
without reaching Xueqiu.

**What this is not.** Nothing here is a figure. A Xueqiu post is a record that
somebody wrote something, and the lane that reads these records grades them as
crowd evidence, which no quantitative Claim may rest on alone.
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

from .xueqiu_core import (
    CREDENTIALLED_OPERATIONS,
    CREDENTIAL_SLOT_REF,
    GET_POST_OPERATION,
    HOT_RANK_OPERATION,
    OPERATIONS,
    SEARCH_POSTS_OPERATION,
    TEMPLATE_KEY,
    xueqiu_fallback_route,
    xueqiu_identity,
)

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_SPOOL_NAME = "connector-spool"
# One page of posts is tens of kilobytes; this is a ceiling the spool enforces
# rather than an expectation, and a host tool that floods is cut off.
MAX_RAW_BYTES = 8 * 1024 * 1024
MAX_PAGES = 5
MAX_POSTS = 400
DEFAULT_DEADLINE_SECONDS = 90.0
PRIMARY_PROVENANCE_LABEL = "xueqiu_agent_reach_channel"


class XueqiuRunError(RuntimeError):
    """The run cannot proceed as asked."""


def _load_governance(path: Path, operation: str) -> ConnectorGovernance:
    """The approved record, checked against the identity it claims to cover."""

    governance = ConnectorGovernance.load(path)
    identity = xueqiu_identity(operation)
    if governance.capability_id != identity["capability_id"]:
        raise XueqiuRunError(
            f"governance record covers {governance.capability_id}, not {operation}"
        )
    if not governance.approved:
        raise XueqiuRunError(
            f"Xueqiu {operation} governance record is {governance.status}; "
            "owner approval is required"
        )
    if governance.wire["expected_source_hash"] != identity["source_hash"]:
        raise XueqiuRunError("governance source hash differs from the packaged template")
    if governance.wire["expected_schema_hash"] != identity["schema_hash"]:
        raise XueqiuRunError(
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
    raise XueqiuRunError(f"packaged template has no {operation} output contract")


def tool_argv(
    operation: str,
    *,
    tool: str,
    query: str | None,
    post_ref: str | None,
    since: str | None,
    pages: int,
    limit: int,
    stock_type: int,
) -> list[str]:
    """The host-side command line, written down rather than assembled ad hoc.

    This is a contract with the host, not an implementation detail: the tool is
    expected to take these subcommands and to print one JSON document on
    stdout. The two post subcommands are the ones the host's own Xueqiu reader
    already uses; ``hot-rank`` is the ranking, which the primary channel and
    the cn-hk-findata fallback both answer.
    """

    if operation == SEARCH_POSTS_OPERATION:
        command = [tool, "search", str(query), "--pages", str(pages), "--json"]
        if since:
            command += ["--since", since]
        return command
    if operation == GET_POST_OPERATION:
        return [tool, "post", str(post_ref), "--json"]
    if operation == HOT_RANK_OPERATION:
        return [tool, "hot-rank", "--limit", str(limit),
                "--stock-type", str(stock_type), "--json"]
    raise XueqiuRunError(f"Xueqiu has no {operation!r} operation")


def call_host_tool(command: Sequence[str], *, deadline_seconds: float) -> bytes:
    """Run the host tool under a hard deadline and return exactly what it printed.

    The bytes are returned rather than parsed here because they are the
    artifact: whatever happens next, this is what the source said.
    """

    try:
        finished = subprocess.run(  # noqa: S603 - operator-installed tool path
            list(command), capture_output=True, timeout=deadline_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise XueqiuRunError(
            f"the Xueqiu host tool is not installed at {command[0]!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise XueqiuRunError(
            f"the Xueqiu host tool did not answer within {deadline_seconds:g}s"
        ) from exc
    if finished.returncode != 0:
        detail = finished.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise XueqiuRunError(
            f"the Xueqiu host tool exited {finished.returncode}: {detail}"
        )
    if not finished.stdout.strip():
        raise XueqiuRunError("the Xueqiu host tool printed nothing")
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
    """One post, in the shape the frozen contract describes."""

    post_id = _text(item.get("post_id") or item.get("id"))
    if not post_id:
        raise XueqiuRunError("a Xueqiu post arrived without an id")
    created_at = _text(item.get("created_at"))
    if not created_at:
        raise XueqiuRunError(f"Xueqiu post {post_id} arrived without a timestamp")
    return {
        "post_id": post_id,
        "url": _text(item.get("url")),
        "created_at": created_at,
        "author": _text(item.get("author")),
        "author_id": _text(item.get("author_id")),
        "title": _text(item.get("title")),
        "text": _text(item.get("text")),
        "reply_count": _counter(item.get("reply_count")),
        "like_count": _counter(item.get("like_count")),
        "retweet_count": _counter(item.get("retweet_count")),
        "view_count": _counter(item.get("view_count")),
        "truncated": bool(item.get("truncated")),
    }


def _ranking_rows(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        for key in ("ranking", "items", "list", "data"):
            rows = raw.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
        return []
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, Mapping)]
    return []


def build_wire(
    operation: str,
    *,
    raw: Any,
    source_record_refs: Sequence[str],
    provenance_label: str,
    provider_status: int = 200,
) -> dict[str, Any]:
    """Normalise whatever the host tool printed into the frozen wire."""

    if operation == HOT_RANK_OPERATION:
        ranking = []
        for index, row in enumerate(_ranking_rows(raw), start=1):
            symbol = _text(row.get("symbol") or row.get("code"))
            if not symbol:
                continue
            value = row.get("value", row.get("percent"))
            ranking.append({
                "symbol": symbol,
                "name": _text(row.get("name")),
                "rank": int(row.get("rank") or index),
                "value": None if value is None else str(value),
            })
        return {
            "schema_version": "0.1",
            "provenance_label": provenance_label,
            "ranking": ranking,
            "source_record_refs": list(source_record_refs),
            "next_cursor": None,
            "provider_status": provider_status,
        }

    if operation == GET_POST_OPERATION:
        item = raw.get("post") if isinstance(raw, Mapping) and "post" in raw else raw
        if not isinstance(item, Mapping):
            raise XueqiuRunError("the host tool returned no post")
        posts = [normalise_post(item)]
    else:
        rows = raw
        if isinstance(raw, Mapping):
            rows = raw.get("posts") or raw.get("list") or raw.get("data") or []
        if not isinstance(rows, list):
            raise XueqiuRunError("the host tool returned no list of posts")
        posts = [normalise_post(row) for row in rows[:MAX_POSTS]
                 if isinstance(row, Mapping)]
    return {
        "schema_version": "0.1",
        "operation": operation,
        "posts": posts,
        "source_record_refs": list(source_record_refs),
        "next_cursor": None,
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
        "query": args.query,
        "post_ref": args.post_ref,
        "since": args.since,
        "status": "failed",
        "failure_reason": None,
        "governance_ref": None,
        "governance_hash": None,
        "credential": slot_binding_summary(None),
        "provenance_label": None,
        "artifact": None,
        "record_count": 0,
        "observation": None,
    }
    try:
        governance = _load_governance(
            Path(args.governance).expanduser().resolve(), args.operation)
        summary["governance_ref"] = governance.id
        summary["governance_hash"] = governance.content_hash

        grant = None
        if args.credential_grant:
            grant = load_credential_grant(args.credential_grant)
        if args.operation in CREDENTIALLED_OPERATIONS:
            identity = xueqiu_identity(args.operation)
            summary["credential"] = require_slots(
                grant, slot_refs=[CREDENTIAL_SLOT_REF],
                operation=args.operation, target_ref=identity["adapter_ref"],
            )
        else:
            summary["credential"] = slot_binding_summary(grant)

        provenance_label = PRIMARY_PROVENANCE_LABEL
        tool = args.tool
        if args.operation == HOT_RANK_OPERATION and not tool and args.fallback_tool:
            fallback = xueqiu_fallback_route(HOT_RANK_OPERATION)
            if fallback is None:  # pragma: no cover - the template declares one
                raise XueqiuRunError("the template declares no hot_rank fallback")
            tool, provenance_label = args.fallback_tool, fallback["provenance_label"]
        summary["provenance_label"] = provenance_label

        if args.fixture_file:
            payload = Path(args.fixture_file).expanduser().read_bytes()
        else:
            if not tool:
                raise XueqiuRunError(
                    "a networked run needs --tool (or --fallback-tool for hot_rank); "
                    "this Core does not know where the host tool lives"
                )
            payload = call_host_tool(
                tool_argv(
                    args.operation, tool=tool, query=args.query,
                    post_ref=args.post_ref, since=args.since, pages=args.pages,
                    limit=args.limit, stock_type=args.stock_type,
                ),
                deadline_seconds=args.deadline_seconds,
            )

        # The artifact is what the source said, hashed before it is read.
        digest = hashlib.sha256(payload).hexdigest()
        spool = RawSpool(str(state / DEFAULT_SPOOL_NAME), max_total_bytes=1_000_000_000)
        sink = spool.open_sink(f"raw-sink:{digest}", max_response_bytes=MAX_RAW_BYTES)
        sink.write(payload)
        artifact = sink.finalize()
        summary["artifact"] = artifact.to_dict()

        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise XueqiuRunError(f"the host tool did not print JSON: {exc}") from exc
        if isinstance(raw, Mapping):
            raw = redacted(raw)
        wire = build_wire(
            args.operation, raw=raw,
            source_record_refs=[f"raw-sink:{artifact.content_hash}"],
            provenance_label=provenance_label,
        )
        from .authority_resolver import _schema_matches

        _schema_matches(wire, _output_schema(args.operation), "output")

        summary.update({
            "status": "succeeded",
            "observation": wire,
            "record_count": len(wire.get("posts", wire.get("ranking", []))),
        })
    except (XueqiuRunError, CrowdCredentialSlotUnbound, ConnectorGovernanceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one run, reported not raised
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
    write_owner_only(summary_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--governance", required=True,
                        help="approved governance record for this operation")
    parser.add_argument("--operation", required=True, choices=list(OPERATIONS))
    parser.add_argument("--query", default=None)
    parser.add_argument("--post-ref", default=None)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--stock-type", type=int, default=10)
    parser.add_argument("--credential-grant", default=None,
                        help="host grant envelope naming the cookie slot")
    parser.add_argument("--tool", default=None, help="host Xueqiu reader")
    parser.add_argument("--fallback-tool", default=None,
                        help="cn-hk-findata ranking, for hot_rank only")
    parser.add_argument("--deadline-seconds", type=float,
                        default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--fixture-file", default=None,
                        help="replay a captured run instead of reaching Xueqiu")
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
    if args.operation == SEARCH_POSTS_OPERATION and not args.query:
        parser.error("search_posts needs --query")
    if args.operation == GET_POST_OPERATION and not args.post_ref:
        parser.error("get_post needs --post-ref")
    if not 1 <= args.pages <= MAX_PAGES:
        parser.error(f"--pages must be 1..{MAX_PAGES}")
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
            "provenance_label",
        )}, ensure_ascii=False, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_PAGES",
    "MAX_POSTS",
    "MAX_RAW_BYTES",
    "PRIMARY_PROVENANCE_LABEL",
    "XueqiuRunError",
    "build_parser",
    "build_wire",
    "call_host_tool",
    "main",
    "normalise_post",
    "run",
    "tool_argv",
]
