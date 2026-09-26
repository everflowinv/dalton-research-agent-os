"""2026-09-26: ask again what the support check held only because it could not run.

From 00:00 UTC every call of ``claim_support_verifier`` was refused by the
router before any model saw it ("CockpitModelRouteUnavailable: no model route
is available right now": the purposes sat on the cheap chain, none of whose
links can serve a WorkOrder with a provider output contract).  The verifier
counted those refusals, and after three a window's statements were staged as
"held for human review: the independent support check could not be run" and
the review was closed as ``extraction_staged`` -- 105 statements on legacy and
94 on ws-7d by 04:36, every one of them waiting for a person although nobody
had judged it.

The verifier no longer counts a systemic failure (``systemic_failure``), so
nothing new is held that way.  This pass returns the ones already held to the
path they should have taken, without anybody clearing them one by one:

* **Which.**  A batch that stands exhausted with a systemic last reason was
  exhausted by the system.  Its statements are read back from the WorkOrders it
  was asked in (the Scheduler keeps each prompt: ``UNTRUSTED_ITEMS``, the
  statement and the exact cited text), and a staged candidate is one of them
  when its statement and its citation binding's ``source_sha256`` match an
  item's.  Nothing is inferred from a hold's wording, a run summary or a time
  window.
* **Only open ones.**  A candidate a person has decided, or that is already in
  the Ledger, is left alone.
* **The same check.**  The statement goes through the same
  ``ClaimSupportVerifier`` (its ceiling, its hourly retry, its independence
  rule), keyed by the same item key, so a verdict the admission path or the
  backfill already stored is read, not paid for again.
* **The same door.**  A supported, about-the-subject verdict commits the very
  candidate that was staged, through ``commit_policy_candidate`` under the
  active policy -- the call admission makes.  Anything else stays held for a
  person, now with a verdict that says why.  The review is not reopened: its
  windows are keyed by the review's hash, so a reopen would re-draft (and pay
  for) every window and stage the same statements a second time beside the
  held ones.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from .claim_support_verification import (
    MAX_ATTEMPTS,
    MAX_STATEMENT_CHARS,
    PURPOSE,
    VERIFIED_SOURCE_REFS,
    ClaimSupportVerifier,
    admissible,
    support_item,
    systemic_failure,
)

PASS_REF = "claim-support-recheck:systemic-failure:v1"
BINDING_PREFIX = "transcript-claim-citation-binding:"
_ITEMS_MARKER = "UNTRUSTED_ITEMS="
# Statements asked per run: two forward calls' worth.  The backlog is a few
# hundred at most and the purpose's own daily ceiling still applies.
MAX_ITEMS_PER_RUN = 24


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def outage_items(connection: sqlite3.Connection, scheduler_db: str | Path, *,
                 purpose: str = PURPOSE, max_attempts: int = MAX_ATTEMPTS
                 ) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """Every statement of a batch a systemic failure exhausted, and the earliest exhaustion.

    Keyed by ``(sha256(statement), sha256(cited_text))`` as the prompt carried
    them.  The Scheduler is opened read-only; a batch with no WorkOrder left
    contributes nothing (it is simply not rechecked).
    """

    rows = connection.execute(
        "SELECT request_key, last_reason, updated_at FROM claim_support_attempts "
        "WHERE purpose=? AND attempts>=? AND instr(request_key, ':')=0",
        (purpose, max_attempts),
    ).fetchall()
    exhausted = [row for row in rows if systemic_failure(row["last_reason"])]
    if not exhausted:
        return {}, None
    since = min(row["updated_at"] for row in exhausted)
    path = Path(scheduler_db).resolve()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as scheduler:
        for row in exhausted:
            for (question,) in scheduler.execute(
                "SELECT json_extract(work_order_json, '$.question') FROM scheduler_work_orders "
                "WHERE work_order_id LIKE ? "
                "AND json_extract(work_order_json, '$.metadata.request_id') LIKE ?",
                (f"work:cockpit-{purpose}-%", f"{row['request_key']}:%"),
            ):
                if not isinstance(question, str) or _ITEMS_MARKER not in question:
                    continue
                try:
                    payload = json.loads(question[question.rindex(_ITEMS_MARKER) + len(_ITEMS_MARKER):])
                except ValueError:
                    continue
                for item in payload if isinstance(payload, list) else ():
                    if (isinstance(item, Mapping) and isinstance(item.get("statement"), str)
                            and isinstance(item.get("cited_text"), str)):
                        found[(_sha256(item["statement"]), _sha256(item["cited_text"]))] = dict(item)
    return found, since


class ClaimSupportRecheck:
    """One bounded pass over the candidates a systemic failure held."""

    def __init__(self, *, store: Any, reviewer: Any, verifier: ClaimSupportVerifier,
                 scheduler_db: str | Path, max_items: int = MAX_ITEMS_PER_RUN) -> None:
        self.store = store
        self.connection = store.connection
        self.reviewer = reviewer
        self.verifier = verifier
        self.scheduler_db = scheduler_db
        self.max_items = int(max_items)

    def _binding(self, evidence: Mapping[str, Any]) -> dict[str, Any] | None:
        ref = next((item.get("ref") for item in evidence.get("artifact_refs") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("ref", "")).startswith(BINDING_PREFIX)), None)
        if ref is None:
            return None
        row = self.connection.execute(
            "SELECT correction_set_version_ref, record_json FROM transcript_claim_citation_bindings "
            "WHERE binding_id=?", (ref,)).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        return {"source_sha256": record.get("source_sha256"),
                "correction_set_version_ref": row["correction_set_version_ref"]}

    def _producer_route(self, correction_ref: str) -> str | None:
        from .claim_review import _ROUTE_REF_RE

        row = self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (correction_ref,)).fetchone()
        if row is None:
            return None
        rationale = (json.loads(row["record_json"]).get("raw_review") or {}).get("rationale")
        match = _ROUTE_REF_RE.search(rationale) if isinstance(rationale, str) else None
        return None if match is None else match.group(1)

    def _committed(self, candidate_ref: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM reviewed_candidate_commits WHERE candidate_claim_ref=?",
            (candidate_ref,)).fetchone() is not None

    def pending(self) -> tuple[list[tuple[str, dict[str, Any]]], int]:
        """``(candidate version id, support item)`` for every open candidate to ask again."""

        items, _since = outage_items(self.connection, self.scheduler_db,
                                     purpose=self.verifier.purpose,
                                     max_attempts=self.verifier.max_attempts)
        if not items:
            return [], 0
        # Narrowed in SQL by the statements the batches asked about; the
        # digest match below is what decides.
        statements = sorted({item["statement"] for item in items.values()})
        rows = []
        for start in range(0, len(statements), 400):
            part = statements[start:start + 400]
            rows.extend(self.reviewer.connection.execute(
                "SELECT c.version_id, c.created_at FROM candidate_claim_versions c "
                "WHERE json_extract(c.record_json, '$.normalized_statement') IN "
                f"({','.join('?' for _ in part)}) "
                "AND NOT EXISTS (SELECT 1 FROM human_review_decisions d "
                "  WHERE d.candidate_claim_version_ref=c.version_id)", part).fetchall())
        rows.sort(key=lambda row: (row["created_at"], row["version_id"]))
        found: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            candidate_ref = row["version_id"]
            bundle = self.reviewer.candidate_bundle(candidate_ref)
            claim, evidence = bundle["claim"], bundle["evidence"]
            if (claim.get("claim_kind") != "qualitative"
                    or evidence.get("source_ref") not in VERIFIED_SOURCE_REFS):
                continue
            binding = self._binding(evidence)
            statement = str(claim.get("normalized_statement") or "")
            if binding is None or not statement:
                continue
            item = items.get((_sha256(statement[:MAX_STATEMENT_CHARS]), binding["source_sha256"]))
            if item is None:
                # Not a statement an exhausted batch asked about.  (A cited
                # text the prompt cut cannot match the binding's digest.)
                continue
            if self._committed(candidate_ref):
                continue
            found.append((candidate_ref, support_item(
                subject_ref=claim["subject_ref"], subject_name=item.get("subject"),
                statement=statement, cited_text=item["cited_text"],
                producer_route_ref=self._producer_route(binding["correction_set_version_ref"]))))
        return found, len(found)

    def run_once(self, *, mission: Mapping[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {"pass_ref": PASS_REF, "status": "idle", "remaining_before": 0,
                                   "asked": 0, "admitted": [], "still_held": 0,
                                   "deferred": None, "calls": 0, "cost_micros": 0}
        pending, summary["remaining_before"] = self.pending()
        pending = pending[:self.max_items]
        if not pending:
            return summary
        summary["asked"] = len(pending)
        outcome = self.verifier.verify(mission=mission, items=[item for _ref, item in pending])
        summary["calls"] = outcome.get("calls", 0)
        summary["cost_micros"] = outcome.get("cost_micros", 0)
        if outcome["status"] == "deferred":
            summary["deferred"] = outcome.get("reason")
        for candidate_ref, item in pending:
            verdict = outcome["verdicts"].get(item["item_key"])
            if verdict is None:
                if item["item_key"] in outcome["unverifiable"]:
                    summary["still_held"] += 1
                continue
            if not admissible(verdict):
                summary["still_held"] += 1
                continue
            try:
                promotion = self.store.commit_policy_candidate(
                    **self.reviewer.candidate_authority_bundle(candidate_ref),
                    idempotency_key=f"policy-ledger:{PASS_REF}:{candidate_ref}")
                if promotion.get("status") == "conflict":
                    raise RuntimeError("the policy commit conflicted")
            except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest
                summary.setdefault("refused", []).append(
                    {"candidate_claim_ref": candidate_ref,
                     "reason": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            summary["admitted"].append({
                "candidate_claim_ref": candidate_ref,
                "status": promotion.get("status"),
                "claim_version_ref": promotion.get("claim_version_ref")})
        summary["status"] = ("admitted" if summary["admitted"] else
                             "deferred" if summary["deferred"] else "examined")
        return summary


__all__ = ["ClaimSupportRecheck", "MAX_ITEMS_PER_RUN", "PASS_REF", "outage_items"]
