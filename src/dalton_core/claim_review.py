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
from pathlib import Path
from typing import Any, Callable

from .claim_retirement import (
    ClaimRetirementAuthority,
    ClaimRetirementError,
    DETERMINISTIC_REASONS,
    detect,
    subject_needles,
)
from .store import authorization_flag

SCHEMA_VERSION = "0.1"
_SCHEMA = Path(__file__).with_name("claim_review_schema.sql")
# The detector set an examination was made with.  Bump it and every Claim is
# re-examined once, which is the only thing that should re-open a clear one.
DETECTOR_SET_REF = "claim-detectors:boilerplate+subject-absent:v1"
# C2-5.  The original note said "a Claim that passes the detectors leaves no
# marker, so a claim-count cursor would never advance", and bounded the I/O
# instead -- forty distinct originals per pass, "and the rest wait for the next
# tick".  The next tick never came: without a marker the query returns the same
# oldest Claims for ever, so the same forty originals were re-read on every
# tick from 2026-09-07 to 2026-09-16 while 5,400 newer Claims were never looked
# at once.  The marker is now written (``claim_review_examinations``), so the
# set drains and the bound is a rate rather than a wall.
DEFAULT_MAX_DOCUMENTS = 40
# Examinations that failed to read the original are retried, slowly: a spool
# object can arrive late, and 815 permanently unreadable Claims must not eat
# the pass every tick.  This many per run, oldest attempt first.
DEFAULT_UNREADABLE_RETRIES = 4
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
        self._authorization = authorization_flag(
            self.connection, "dalton_claim_review_authorized")
        self.connection.executescript(_SCHEMA.read_text(encoding="utf-8"))

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
        hold: str | None = None
        for row in rows:
            mission = self.missions.mission(row["mission_version_id"])
            if WRITE_SCOPE in mission["autonomy"]["may_write"]:
                return mission["autonomy"]["automation_principal"], None
            # Keep looking: returning on the first mission made a second
            # mission's grant unreachable.
            hold = (
                f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                "已标记的结论等你确认后才会退役"
            )
        return None, hold or "没有生效中的任务"

    def _roster_needles(self) -> dict[str, list[str]]:
        """The mission universe's own tickers and names, as a fallback.

        ``needles_from_plans`` keys on the discovery plan's company entries. A
        Claim whose ``subject_ref`` is not exactly one of those keys gets an
        empty needle list, and ``subject_absent_from_source`` returns False for
        an empty list by design -- so the misattribution detector silently
        cannot fire for that company. The mission roster always knows the
        ticker; use it wherever the plan does not.
        """

        roster: dict[str, list[str]] = {}
        try:
            rows = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall()
            for row in rows:
                mission = self.missions.mission(row["mission_version_id"])
                for member in mission.get("universe") or ():
                    found = subject_needles(member)
                    if found:
                        roster.setdefault(member["company_ref"], []).extend(found)
        except Exception:  # noqa: BLE001 - a missing roster is not a gate
            return {}
        return {ref: sorted(set(values)) for ref, values in roster.items()}

    def _needles_for(self, subject_ref: Any, roster: Mapping[str, Sequence[str]]) -> list[str]:
        planned = list(self.needles.get(subject_ref, ()))
        if planned:
            return planned
        return list(roster.get(subject_ref, ()))

    # -- the examination marker ---------------------------------------------

    def _examinations(self) -> dict[str, dict[str, Any]]:
        return {
            row["claim_version_ref"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM claim_review_examinations").fetchall()
        }

    def _record_examination(
        self, *, claim_version_ref: str, claim_version_hash: str,
        source_content_hash: str | None, outcome: str, attempts: int = 1,
    ) -> None:
        at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "INSERT INTO claim_review_examinations("
                "claim_version_ref,claim_version_hash,source_content_hash,detector_ref,"
                "outcome,attempts,examined_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(claim_version_ref) DO UPDATE SET "
                "outcome=excluded.outcome, source_content_hash=excluded.source_content_hash, "
                "attempts=claim_review_examinations.attempts+1, "
                "examined_at=excluded.examined_at",
                (claim_version_ref, claim_version_hash, source_content_hash,
                 DETECTOR_SET_REF, outcome, attempts, at),
            )
            self.connection.commit()
        except Exception:  # noqa: BLE001 - a marker failure must not lose the pass
            self.connection.rollback()
        finally:
            self._authorization.authorized = False

    def run_once(
        self,
        *,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_claims: int | None = None,
        unreadable_retries: int = DEFAULT_UNREADABLE_RETRIES,
    ) -> dict[str, Any]:
        """Detect, then write only under the mission's grant.

        Detection is free and always reported; persisting a challenge or a
        decision is a write, and ADR-0004 says automation writes only what the
        mission grants.  Without the grant the run says exactly what it found
        and changes nothing, which is what the owner needs to decide.

        ``max_claims`` is accepted as a spelling of ``max_documents`` because
        the lane registry advertises that name and the writer passes it
        through; the two have always meant the same bound.
        """

        if max_claims is not None:
            max_documents = int(max_claims)
        summary: dict[str, Any] = {
            "status": "idle", "schema_version": SCHEMA_VERSION, "scanned": 0,
            "detected": [], "challenged": [], "retired": [], "unreadable": 0,
            "deferred": 0, "documents_read": 0, "skipped": [],
            # C2-5: how far the patrol has got through the Ledger, which is the
            # number that was silently zero for nine days.
            "examined": 0, "already_examined": 0, "reexamined_unreadable": 0,
            "remaining": 0,
        }
        principal, hold = self._grant()
        summary["grant"] = {"principal": principal, "hold": hold}
        examined = self._examinations()
        # A clear or challenged examination is final until the detectors change.
        settled = {
            ref for ref, item in examined.items()
            if item["outcome"] in ("clear", "challenged")
            and item["detector_ref"] == DETECTOR_SET_REF
        }
        # Unreadable ones come back slowly, oldest attempt first.
        retryable = sorted(
            (ref for ref, item in examined.items()
             if item["outcome"] == "unreadable"
             and item["detector_ref"] == DETECTOR_SET_REF),
            key=lambda ref: (examined[ref]["attempts"], examined[ref]["examined_at"]),
        )[:max(0, int(unreadable_retries))]
        deferred_unreadable = {
            ref for ref, item in examined.items()
            if item["outcome"] == "unreadable" and ref not in set(retryable)
        }
        # Newest first: a wrong Claim committed this morning should be caught
        # today, and the backlog drains from the top because an examined Claim
        # never comes back.
        rows = self.connection.execute(
            "SELECT c.claim_version_id AS ref, c.claim_json AS claim_json, c.content_hash AS hash "
            "FROM claim_versions c LEFT JOIN claim_retirement_challenges h "
            "ON h.claim_version_ref=c.claim_version_id "
            "WHERE h.challenge_id IS NULL ORDER BY c.created_at DESC, c.claim_version_id"
        ).fetchall()
        chain = self._citation_chain() if rows else {}
        texts: dict[str, str | None] = {}
        detections: list[dict[str, Any]] = []
        roster = self._roster_needles()
        outcomes: list[tuple[str, str, str | None, str]] = []
        for row in rows:
            claim = json.loads(row["claim_json"])
            if claim.get("claim_kind") != "qualitative":
                continue
            if row["ref"] in settled:
                summary["already_examined"] += 1
                continue
            if row["ref"] in deferred_unreadable:
                summary["deferred"] += 1
                continue
            summary["scanned"] += 1
            digest = chain.get(row["ref"])
            text: str | None = None
            if digest is not None:
                if digest not in texts:
                    if len(texts) >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        summary["remaining"] += 1
                        continue  # this original waits for the next tick
                    texts[digest] = self.source_text(digest)
                text = texts[digest]
            if row["ref"] in set(retryable):
                summary["reexamined_unreadable"] += 1
            if digest is None or text is None:
                summary["unreadable"] += 1
                outcomes.append((row["ref"], row["hash"], digest, "unreadable"))
                continue
            hit = detect(
                statement=claim["normalized_statement"], source_text=text,
                needles=self._needles_for(claim["subject_ref"], roster),
            )
            if hit is None:
                outcomes.append((row["ref"], row["hash"], digest, "clear"))
                continue
            reason, rationale = hit
            outcomes.append((row["ref"], row["hash"], digest, "challenged"))
            detections.append({
                "claim_version_ref": row["ref"], "claim_version_hash": row["hash"],
                "reason_code": reason, "rationale": rationale,
                "subject_ref": claim["subject_ref"],
                "statement": claim["normalized_statement"][:160],
                "source_content_hash": digest,
            })
        # The marker is what makes the next pass a different pass.  Written
        # whether or not the mission grants the write scope: examining is a
        # read, and remembering that it was done costs nobody anything.
        for claim_version_ref, claim_version_hash, digest, outcome in outcomes:
            self._record_examination(
                claim_version_ref=claim_version_ref,
                claim_version_hash=claim_version_hash,
                source_content_hash=digest, outcome=outcome,
            )
            summary["examined"] += 1
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
                    subject_needles=self._needles_for(challenge["subject_ref"], roster),
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
    "DEFAULT_MAX_DOCUMENTS",
    "DEFAULT_UNREADABLE_RETRIES",
    "DETECTOR_SET_REF",
    "WRITE_SCOPE",
    "needles_from_plans",
    "needles_from_search_terms",
]
