"""P10b: the lane that reads admitted Claims back against their originals.

One pass per controller tick, bounded and idempotent:

1. take qualitative Claims that carry no challenge yet, oldest first;
2. resolve each Claim's exact original through its own citation chain
   (Claim → supports relation → Evidence → citation binding → correction set
   → source content hash → raw spool object), and verify the bytes hash to
   what the correction set recorded;
3. run the deterministic detectors and record a challenge for any hit;
4. when the mission grants ``claim_challenge`` to its automation principal,
   uphold each deterministic challenge; the authority re-runs the detector
   before it writes.

A Claim whose original cannot be read is never challenged: the check fails
closed, and the run reports how many were left alone and why.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .claim_retirement import (
    ClaimRetirementAuthority,
    ClaimRetirementError,
    DETERMINISTIC_REASONS,
    detect,
)

SCHEMA_VERSION = "0.1"
# Every unchallenged Claim is re-derived each pass: a Claim that passes the
# detectors leaves no marker, so a claim-count cursor would never advance past
# the first batch (it did not, the first time this ran).  What is bounded is
# the I/O: at most this many distinct originals are read per pass, and the
# rest wait for the next tick.
DEFAULT_MAX_DOCUMENTS = 40
WRITE_SCOPE = "claim_challenge"
BINDING_PREFIX = "transcript-claim-citation-binding:"
# Words that name an industry rather than a company; a document mentioning
# "technology" tells us nothing about whether it is about DXC Technology.
_GENERIC_TERMS = frozenset({
    "technology", "technologies", "systems", "system", "group", "holdings",
    "international", "business", "machines", "company", "corp", "corporation",
    "inc", "plc", "ltd", "limited", "the", "and",
})


def needles_from_search_terms(terms: str) -> list[str]:
    """The company's own names, from the search terms the discovery plan uses."""

    found = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z.&-]{1,}", terms or "")
        if token.lower() not in _GENERIC_TERMS and len(token) >= 2
    }
    return sorted(found)


def needles_from_plans(plans: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """company_ref → its names, collected from every discovery plan."""

    result: dict[str, set[str]] = {}
    for plan in plans:
        for company_ref, entry in ((plan or {}).get("companies") or {}).items():
            terms = entry.get("search_terms") if isinstance(entry, Mapping) else None
            if isinstance(terms, str):
                result.setdefault(company_ref, set()).update(needles_from_search_terms(terms))
    return {ref: sorted(values) for ref, values in result.items()}


class ClaimReviewDriver:
    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        challenges: ClaimRetirementAuthority,
        spool: Any,
        needles: Mapping[str, Sequence[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.missions = missions
        self.challenges = challenges
        self.spool = spool
        self.needles = {ref: list(values) for ref, values in (needles or {}).items()}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- resolution ----------------------------------------------------------

    def _citation_chain(self) -> dict[str, str]:
        """claim_version_ref → the source content hash of the original it cites."""

        evidence_by_claim = {
            row["claim_version_id"]: row["evidence_version_id"]
            for row in self.connection.execute(
                "SELECT claim_version_id, evidence_version_id FROM evidence_relations "
                "WHERE relation='supports'"
            ).fetchall()
        }
        bindings = {}
        for row in self.connection.execute(
            "SELECT record_json FROM transcript_claim_citation_bindings"
        ).fetchall():
            record = json.loads(row["record_json"])
            bindings[record["id"]] = record.get("correction_set_version_ref")
        corrections = {}
        for row in self.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions"
        ).fetchall():
            record = json.loads(row["record_json"])
            corrections[record["id"]] = record
        evidence = {}
        for row in self.connection.execute(
            "SELECT evidence_json FROM evidence_versions"
        ).fetchall():
            record = json.loads(row["evidence_json"])
            evidence[record["id"]] = record
        chain: dict[str, str] = {}
        for claim_ref, evidence_ref in evidence_by_claim.items():
            record = evidence.get(evidence_ref)
            if record is None:
                continue
            binding_ref = next(
                (
                    item["ref"] for item in record.get("artifact_refs", ())
                    if isinstance(item, Mapping) and str(item.get("ref", "")).startswith(BINDING_PREFIX)
                ),
                None,
            )
            correction = corrections.get(bindings.get(binding_ref or "", ""))
            if correction is not None:
                chain[claim_ref] = correction["source_content_hash"]
        return chain

    def source_text(self, content_sha256: str) -> str | None:
        """The exact original bytes, verified against the hash the chain names."""

        try:
            raw = self.spool.read_object(content_sha256)
        except Exception:  # noqa: BLE001 - a missing object is "cannot read", not a crash
            return None
        if hashlib.sha256(raw).hexdigest() != content_sha256:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # -- the pass ------------------------------------------------------------

    def _grant(self) -> tuple[str | None, str | None]:
        """The automation principal allowed to retire, or the reason there is none."""

        rows = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall()
        for row in rows:
            mission = self.missions.mission(row["mission_version_id"])
            if WRITE_SCOPE in mission["autonomy"]["may_write"]:
                return mission["autonomy"]["automation_principal"], None
            return None, (
                f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                "已标记的结论等你确认后才会退役"
            )
        return None, "没有生效中的任务"

    def run_once(self, *, max_documents: int = DEFAULT_MAX_DOCUMENTS) -> dict[str, Any]:
        """Detect, then write only under the mission's grant.

        Detection is free and always reported; persisting a challenge or a
        decision is a write, and ADR-0004 says automation writes only what the
        mission grants.  Without the grant the run says exactly what it found
        and changes nothing, which is what the owner needs to decide.
        """

        summary: dict[str, Any] = {
            "status": "idle", "schema_version": SCHEMA_VERSION, "scanned": 0,
            "detected": [], "challenged": [], "retired": [], "unreadable": 0,
            "deferred": 0, "documents_read": 0, "skipped": [],
        }
        principal, hold = self._grant()
        summary["grant"] = {"principal": principal, "hold": hold}
        rows = self.connection.execute(
            "SELECT c.claim_version_id AS ref, c.claim_json AS claim_json, c.content_hash AS hash "
            "FROM claim_versions c LEFT JOIN claim_retirement_challenges h "
            "ON h.claim_version_ref=c.claim_version_id "
            "WHERE h.challenge_id IS NULL ORDER BY c.created_at, c.claim_version_id"
        ).fetchall()
        chain = self._citation_chain() if rows else {}
        texts: dict[str, str | None] = {}
        detections: list[dict[str, Any]] = []
        for row in rows:
            claim = json.loads(row["claim_json"])
            if claim.get("claim_kind") != "qualitative":
                continue
            summary["scanned"] += 1
            digest = chain.get(row["ref"])
            text: str | None = None
            if digest is not None:
                if digest not in texts:
                    if len(texts) >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        continue  # this original waits for the next tick
                    texts[digest] = self.source_text(digest)
                text = texts[digest]
            if digest is None or text is None:
                summary["unreadable"] += 1
            hit = detect(
                statement=claim["normalized_statement"], source_text=text,
                needles=self.needles.get(claim["subject_ref"], ()),
            )
            if hit is None:
                continue
            reason, rationale = hit
            detections.append({
                "claim_version_ref": row["ref"], "claim_version_hash": row["hash"],
                "reason_code": reason, "rationale": rationale,
                "subject_ref": claim["subject_ref"],
                "statement": claim["normalized_statement"][:160],
                "source_content_hash": digest,
            })
        summary["documents_read"] = len(texts)
        summary["detected"] = [
            {k: v for k, v in item.items() if k in ("claim_version_ref", "reason_code", "subject_ref", "statement")}
            for item in detections
        ]
        if principal is None:
            summary["status"] = "held" if detections else "idle"
            return summary
        for item in detections:
            try:
                record = self.challenges.challenge(
                    claim_version_ref=item["claim_version_ref"],
                    claim_version_hash=item["claim_version_hash"],
                    reason_code=item["reason_code"], rationale=item["rationale"],
                    actor_ref=principal,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"claim_version_ref": item["claim_version_ref"], "reason": str(exc)})
                continue
            if record.get("status") == "fresh":
                summary["challenged"].append({
                    "claim_version_ref": item["claim_version_ref"], "reason_code": item["reason_code"],
                    "challenge_ref": record["id"], "subject_ref": item["subject_ref"],
                })
        for challenge in self.challenges.challenges(open_only=True, limit=200):
            if challenge["reason_code"] not in DETERMINISTIC_REASONS:
                continue
            text = None
            if challenge["reason_code"] == "subject_absent_from_source":
                digest = chain.get(challenge["claim_version_ref"])
                if digest is None:
                    digest = self._citation_chain().get(challenge["claim_version_ref"])
                if digest is not None:
                    text = texts.get(digest) or self.source_text(digest)
            try:
                self.challenges.decide(
                    challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                    decision="retired", actor_ref=principal, rationale=challenge["rationale"],
                    subject_needles=self.needles.get(challenge["subject_ref"], ()),
                    source_text=text,
                )
            except ClaimRetirementError as exc:
                summary["skipped"].append({"challenge_ref": challenge["id"], "reason": str(exc)})
                continue
            summary["retired"].append({
                "claim_version_ref": challenge["claim_version_ref"],
                "reason_code": challenge["reason_code"], "subject_ref": challenge["subject_ref"],
            })
        if summary["challenged"] or summary["retired"]:
            summary["status"] = "acted"
        return summary


__all__ = [
    "ClaimReviewDriver",
    "DEFAULT_MAX_CLAIMS",
    "WRITE_SCOPE",
    "needles_from_plans",
    "needles_from_search_terms",
]
