"""Synthetic material for the S3 crowd-source tests.

Nothing here is real. No real post, no real review, no real handle and
certainly no real credential: a grant envelope carries refs and an expiry by
construction, and the ones built here are made of the word "synthetic".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from dalton_core.store import content_hash

SYNTHETIC_HASH = "a" * 64


def _wire_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def credential_grant(
    *,
    slot_refs: Sequence[str],
    operations: Sequence[str],
    target_ref: str,
    expires_in_hours: float = 24.0,
    grant_id: str = "credential-grant:synthetic:1",
    max_calls: int = 50,
) -> dict[str, Any]:
    """One valid grant envelope. It names slots; it holds no material."""

    now = datetime.now(timezone.utc)
    wire: dict[str, Any] = {
        "schema_version": "0.1",
        "id": grant_id,
        "created_at": _wire_time(now - timedelta(minutes=1)),
        "expires_at": _wire_time(now + timedelta(hours=expires_in_hours)),
        "authority_ref": "credential-authority:synthetic",
        "grant_kind": "mcp_managed",
        "target_ref": target_ref,
        "connector_profile_ref": "connector-profile:synthetic",
        "connector_profile_hash": SYNTHETIC_HASH,
        "capability_lease_ref": "capability-lease:synthetic",
        "capability_lease_hash": SYNTHETIC_HASH,
        "adapter_ref": target_ref,
        "adapter_hash": SYNTHETIC_HASH,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "credential_slot_refs": list(slot_refs),
        "allowed_operations": list(operations),
        "max_calls": max_calls,
    }
    wire["content_hash"] = content_hash(wire)
    return wire


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def xueqiu_posts(count: int = 2, *, first_id: int = 1) -> dict[str, Any]:
    return {
        "posts": [
            {
                "id": str(first_id + index),
                "url": f"https://example.invalid/{first_id + index}",
                "created_at": f"2026-09-0{1 + index} 10:00:00",
                "author": f"synthetic-author-{index}",
                "author_id": str(9000 + index),
                "title": "synthetic title",
                "text": "synthetic body about a covered company",
                "reply_count": index,
                "like_count": index * 2,
                "retweet_count": 0,
                "view_count": 100 + index,
                "truncated": False,
            }
            for index in range(count)
        ]
    }


def xreach_posts(count: int = 2, *, first_id: int = 100) -> dict[str, Any]:
    """The envelope the tool really uses: `items` and `hasMore`."""

    return {
        "next_cursor": None,
        "hasMore": False,
        "items": [
            {
                "id": str(first_id + index),
                "url": f"https://example.invalid/x/{first_id + index}",
                "created_at": f"2026-09-0{1 + index}T12:00:00Z",
                "author": {"username": "SyntheticCo", "id": "42"},
                "text": "synthetic post about a covered company",
                "reply_count": 1,
                "like_count": 2,
                "retweet_count": 3,
                "view_count": 4,
            }
            for index in range(count)
        ],
    }


def blind_page(*, unlocked: int = 1, locked: int = 1) -> bytes:
    """A synthetic page in the shape the real one has: data in a flight payload."""

    rows: list[dict[str, Any]] = []
    for index in range(unlocked):
        rows.append({
            "id": f"r{index}",
            "createdAt": f"2026-09-0{1 + index}T00:00:00Z",
            "summary": "synthetic summary",
            "overall": 4.0, "career": 3.5, "balance": 3.0,
            "compensation": 4.5, "culture": 3.0, "management": 2.5,
            "pros": "synthetic pros text",
            "cons": "synthetic cons text",
            "jobgroup": "Engineering",
            "memberLocation": "Nowhere",
        })
    for index in range(locked):
        rows.append({
            "id": f"l{index}",
            "createdAt": f"2026-08-0{1 + index}T00:00:00Z",
            "summary": "synthetic locked summary",
            "overall": 2.0, "career": 2.0, "balance": 2.0,
            "compensation": 2.0, "culture": 2.0, "management": 2.0,
            "pros": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
            "cons": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
            "jobgroup": "Consulting",
            "memberLocation": "Elsewhere",
        })
    # Compact, because the real payload is compact and the parser looks for
    # the exact key `"reviews":{"list":[` with no spaces in it.
    payload = json.dumps({"reviews": {"list": rows}}, ensure_ascii=False,
                         separators=(",", ":"))
    inner = payload[1:-1]  # the flight payload is a fragment, not a document
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    total = unlocked + locked
    counts = f',\\"filteredCount\\":{total},\\"totalCount\\":{total}'
    return (
        "<html><body><script>self.__next_f.push([1,\""
        + escaped + counts + "\"])</script></body></html>"
    ).encode("utf-8")


__all__ = [
    "SYNTHETIC_HASH",
    "blind_page",
    "credential_grant",
    "write_json",
    "xreach_posts",
    "xueqiu_posts",
]
