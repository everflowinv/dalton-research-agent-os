"""``GET /v2/data/feedback`` -- the feedback binding of each evidence item.

``/v1/cockpit/claims`` lists a company's conclusions without the content hash
a feedback verdict is bound to (the analyst journal binds a verdict to the
exact version it was given on: praise for a conclusion that has since been
rewritten is praise for the old one).  This read adds only that: for the
claim versions the page is showing, the binding the existing
``POST /v1/cockpit/feedback`` takes, and what the owner already said.

Read-only: it opens the Core through the cockpit plane's own read-only
connection and writes nothing.  The write stays ``/v1/cockpit/feedback``.

    GET /v2/data/feedback?company=<company_ref>&refs=<claim version>,<...>
    -> {"enabled": bool, "bindings": {ref: {target_ref, target_hash,
        target_kind: "claim", company_ref, enabled, entries}}, "reason"?}
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

FEEDBACK_PATH = "/v2/data/feedback"
MAX_REFS = 50
_REF = re.compile(r"[A-Za-z0-9:_.\-]{1,512}")
_HASH = re.compile(r"[0-9a-f]{64}")


def _refs(query: Mapping[str, str]) -> list[str]:
    from ..cockpit_plane import CockpitError

    raw = str(query.get("refs") or "").strip()
    if not raw:
        return []
    refs = [part.strip() for part in raw.split(",") if part.strip()]
    if len(refs) > MAX_REFS:
        raise CockpitError(f"一次最多读取 {MAX_REFS} 条结论的反馈")
    for ref in refs:
        if not _REF.fullmatch(ref):
            raise CockpitError("结论编号不正确，请刷新页面后再试")
    return list(dict.fromkeys(refs))


def feedback_bindings(app: Any, _login: str, query: Mapping[str, str], _path: str) -> dict[str, Any]:
    from ..cockpit_plane import CockpitError

    refs = _refs(query)
    company = str(query.get("company") or "").strip() or None
    if company is not None and len(company) > 512:
        raise CockpitError("公司编号不正确")
    plane = getattr(app, "cockpit_plane", None)
    if plane is None:
        return {"enabled": False, "bindings": {},
                "reason": "这个研究项目还没有研究资料库，暂时不能反馈。"}
    if not refs:
        return {"enabled": True, "bindings": {}}
    try:
        with plane._core() as core:
            journal = plane._journal(core)
            marks = ",".join("?" for _ in refs)
            rows = core.execute(
                f"SELECT claim_version_id, content_hash, claim_json FROM claim_versions "
                f"WHERE claim_version_id IN ({marks})", refs).fetchall()
    except sqlite3.Error:
        return {"enabled": False, "bindings": {},
                "reason": "研究资料库暂时无法读取，反馈稍后再试。"}
    bindings: dict[str, Any] = {}
    for row in rows:
        digest = str(row["content_hash"] or "")
        if not _HASH.fullmatch(digest):
            continue
        try:
            claim = json.loads(row["claim_json"])
        except (TypeError, ValueError):
            claim = {}
        subject = claim.get("subject_ref") or claim.get("company_ref") or None
        if company is not None and subject not in (None, company):
            continue
        ref = row["claim_version_id"]
        bindings[ref] = {
            "target_ref": ref, "target_hash": digest, "target_kind": "claim",
            "company_ref": subject, "enabled": bool(journal.get("enabled")),
            "entries": journal.get("by_target", {}).get(ref, []),
        }
    return {"enabled": bool(journal.get("enabled")), "bindings": bindings}


def register(router: Any) -> None:
    router.add_json(FEEDBACK_PATH, feedback_bindings)


__all__ = ["FEEDBACK_PATH", "MAX_REFS", "feedback_bindings", "register"]
