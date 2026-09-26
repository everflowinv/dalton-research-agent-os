"""2026-09-24: the support check, once, for the qualitative Claims admitted before it existed.

``claim_support_verification`` asks, before a morning-note or AlphaEngine
statement is admitted, whether its cited sentences support it and whether it
is about the company it is filed under.  About twelve thousand qualitative
Claims were admitted before that question was asked.  This asks it of them --
the ones from the sources the check covers, not yet retired, not already
challenged -- a bounded number per run, and routes a rejection down the path
a wrong Claim already takes: a challenge, then a retirement decision, both
append-only (``claim_retirement``).  Nothing is edited and nothing is deleted.

**Bounded and re-entrant.**  One run reads at most ``max_documents`` originals
and asks about at most ``max_items`` statements, in calls of the backfill
purpose's size, newest Claims first, under the backfill purpose's own daily
ceiling.  Every Claim it has looked at gets a marker for this pass (a verdict
key, an unreadable original, or a drafter no verifier can be independent of),
so the next run starts where this one stopped and a crash repeats nothing
that was paid for: a verdict is stored before its Claim is marked, and a
statement already judged -- by the admission-time check or an earlier run --
is read, not asked again.

**Who may retire.**  Exactly as for the review patrol: the mission must grant
``claim_challenge`` to its automation principal.  Without it the run still
asks and records, reports what it would have challenged, and the challenges
are raised on the first run after the grant.  The retirement authority does
not take the run's word for any of it: it re-reads the verdict bound to the
exact claim version before it writes (``recorded_rejection``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .claim_support_verification import (
    ClaimSupportVerifier,
    admissible,
    support_item,
    systemic_failure,
)

PASS_REF = "claim-support-backfill:v1"
# 2026-09-25: a Claim this pass marked ``unverifiable`` only because the
# verifier's own output contract was not wired (the adapter refused every call
# before sending it) was never looked at.  Marks are append-only, so such a
# Claim is re-opened under this second pass ref -- the schema's own mechanism,
# "a new pass ref re-opens every Claim that is not settled by a verdict" --
# and its next mark is written there.
RETRY_PASS_REF = "claim-support-backfill:v1:after-contract-wiring"
# 2026-09-26: the same for "no model route" (the support purposes sat on a
# chain that could not serve them): 60 first-pass and 20 after-contract-wiring
# marks in each workspace say ``unverifiable`` for that alone.  Such a mark
# settles nothing either; the Claim's next mark is written under the first of
# these pass refs it has none under, so a Claim marked by both outages still
# gets one real look.  The last one settles whatever it says -- and a systemic
# failure no longer ends in an ``unverifiable`` mark at all, it defers.
ROUTE_RETRY_PASS_REF = "claim-support-backfill:v1:after-route-unavailable"
PASS_REFS = (PASS_REF, RETRY_PASS_REF, ROUTE_RETRY_PASS_REF)
SUPPORT_REASON = "citation_support_rejected"
WRITE_SCOPE = "claim_challenge"
DEFAULT_MAX_DOCUMENTS = 60
MAX_ACTIONS_PER_RUN = 200


def _rationale(verdict: Mapping[str, Any]) -> str:
    parts = []
    if verdict.get("support") != "supported":
        parts.append("所引原文句子不支持这条结论")
    if verdict.get("subject_relation") != "about_subject":
        other = verdict.get("other_subject")
        parts.append(f"这条结论说的是{other or '别的公司/行业'}，不是它所挂的公司")
    return (f"独立模型核验（{verdict.get('purpose')}，{verdict.get('work_order_ref')}）："
            + "；".join(parts) + "。")


class ClaimSupportBackfill:
    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        verifier: ClaimSupportVerifier,
        reader: Any,
        challenges: Any,
        claim_sources: Sequence[str],
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.missions = missions
        self.verifier = verifier
        self.reader = reader
        self.challenges = challenges
        self.claim_sources = tuple(claim_sources)

    # -- inputs ---------------------------------------------------------------

    def _missions(self) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
        """The mission the calls are budgeted under, and who may retire."""

        rows = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall()
        first = None
        hold = None
        for row in rows:
            mission = self.missions.mission(row["mission_version_id"])
            first = first or mission
            if WRITE_SCOPE in (mission.get("autonomy") or {}).get("may_write", []):
                return mission, mission["autonomy"]["automation_principal"], None
            hold = (f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "核验不通过的结论等授权后才会被质疑并退役")
        return first, None, hold or "没有生效中的任务"

    def _candidates(self) -> list[sqlite3.Row]:
        if not self.claim_sources:
            return []
        prefixes = " OR ".join("json_extract(c.claim_json,'$.claim_ref') LIKE ?"
                               for _ in self.claim_sources)
        # A mark settles a Claim unless it is an ``unverifiable`` a systemic
        # failure caused (the verifier's contract wiring, no model route);
        # such a Claim is re-opened under the next pass ref it has no mark
        # under (``PASS_REFS``), and a mark under the last one settles it.
        self.connection.create_function(
            "dalton_systemic_support_failure", 1,
            lambda text: 1 if systemic_failure(text) else 0, deterministic=True)
        passes = ",".join("?" for _ in PASS_REFS)
        return self.connection.execute(
            "SELECT c.claim_version_id AS ref, c.claim_json AS claim_json, c.content_hash AS hash, "
            "(SELECT json_group_array(f.pass_ref) FROM claim_support_backfill_marks f "
            "  WHERE f.claim_version_ref=c.claim_version_id) AS marked_passes "
            "FROM claim_versions c "
            "WHERE json_extract(c.claim_json,'$.claim_kind')='qualitative' "
            f"AND ({prefixes}) "
            "AND NOT EXISTS (SELECT 1 FROM claim_retirement_challenges h "
            "  WHERE h.claim_version_ref=c.claim_version_id) "
            "AND NOT EXISTS (SELECT 1 FROM claim_support_backfill_marks m "
            f"  WHERE m.claim_version_ref=c.claim_version_id AND m.pass_ref IN ({passes}) "
            "  AND NOT (m.outcome='unverifiable' AND dalton_systemic_support_failure(m.detail))) "
            "AND NOT EXISTS (SELECT 1 FROM claim_support_backfill_marks r "
            "  WHERE r.claim_version_ref=c.claim_version_id AND r.pass_ref=?) "
            "ORDER BY c.created_at DESC, c.claim_version_id",
            (*(f"claim:{source}:%" for source in self.claim_sources), *PASS_REFS,
             PASS_REFS[-1]),
        ).fetchall()

    @staticmethod
    def _subject_names(mission: Mapping[str, Any]) -> dict[str, str]:
        from .document_subject import subject_label
        from .mission_company_names import mission_name_table

        universe = list(mission.get("universe") or ())
        try:
            table = mission_name_table(universe)
        except Exception:  # noqa: BLE001 - a name table is a nicety, not a gate
            table = {}
        names: dict[str, str] = {}
        for member in universe:
            ref, ticker = member.get("company_ref"), member.get("ticker")
            if isinstance(ref, str) and isinstance(ticker, str) and ticker:
                names[ref] = subject_label(ticker, table)
        return names

    def _mark(self, row: Mapping[str, Any], outcome: str, *, item_key: str | None = None,
              detail: str | None = None) -> None:
        marked = set(json.loads(row["marked_passes"] or "[]"))
        self.verifier.records.mark(
            claim_version_ref=row["ref"], claim_version_hash=row["hash"],
            pass_ref=next(ref for ref in PASS_REFS if ref not in marked),
            outcome=outcome, item_key=item_key, detail=detail)

    # -- the pass -------------------------------------------------------------

    def run_once(self, *, max_items: int, max_documents: int = DEFAULT_MAX_DOCUMENTS) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "status": "idle", "pass_ref": PASS_REF, "claim_sources": list(self.claim_sources),
            "remaining_before": 0, "examined": 0, "already_judged": 0, "supported": 0,
            "rejected": 0, "unreadable": 0, "unverifiable": 0, "deferred": None,
            "calls": 0, "cost_micros": 0, "detected": 0, "challenged": [], "retired": [],
            "skipped": [],
        }
        mission, principal, hold = self._missions()
        summary["grant"] = {"principal": principal, "hold": hold}
        if mission is None:
            summary["status"] = "no_mission"
            return summary
        rows = self._candidates()
        summary["remaining_before"] = len(rows)
        pending: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        if rows and max_items > 0:
            citations = self.reader._citations()
            names = self._subject_names(mission)
            texts: dict[str, str | None] = {}
            for row in rows:
                if len(pending) >= max_items:
                    break
                claim = json.loads(row["claim_json"])
                citation = citations.get(row["ref"])
                if citation is None:
                    self._mark(row, "unreadable", detail="no citation chain")
                    summary["unreadable"] += 1
                    continue
                digest = citation["digest"]
                if digest not in texts:
                    if len(texts) >= max(1, int(max_documents)):
                        break  # this run's reading budget; the rest wait, unmarked
                    self.reader._render_deferred = False
                    text = self.reader.source_text(digest)
                    if text is None and getattr(self.reader, "_render_deferred", False):
                        continue  # out of renders this run, not unreadable
                    texts[digest] = text
                text = texts[digest]
                start, end = citation.get("start"), citation.get("end")
                if (text is None or not isinstance(start, int) or not isinstance(end, int)
                        or not 0 <= start < end <= len(text)):
                    self._mark(row, "unreadable", detail="the cited original cannot be read")
                    summary["unreadable"] += 1
                    continue
                subject_ref = claim["subject_ref"]
                pending.append((row, support_item(
                    subject_ref=subject_ref, subject_name=names.get(subject_ref, subject_ref),
                    statement=claim["normalized_statement"], cited_text=text[start:end],
                    producer_route_ref=citation.get("route_decision_ref"))))
        if pending:
            known = self.verifier.verdicts([item["item_key"] for _row, item in pending])
            summary["already_judged"] = sum(1 for _row, item in pending if item["item_key"] in known)
            outcome = self.verifier.verify(mission=mission, items=[item for _row, item in pending])
            summary["calls"] = outcome.get("calls", 0)
            summary["cost_micros"] = outcome.get("cost_micros", 0)
            if outcome["status"] == "deferred":
                summary["deferred"] = outcome.get("reason")
            for row, item in pending:
                key = item["item_key"]
                verdict = outcome["verdicts"].get(key)
                if verdict is not None:
                    self._mark(row, "verdict", item_key=key)
                    summary["examined"] += 1
                    summary["supported" if admissible(verdict) else "rejected"] += 1
                elif key in outcome["unverifiable"]:
                    self._mark(row, "unverifiable", item_key=key,
                               detail=outcome["unverifiable"][key])
                    summary["unverifiable"] += 1
        self._act(principal, summary)
        summary["remaining_after"] = len(self._candidates())
        if summary["challenged"] or summary["retired"]:
            summary["status"] = "acted"
        elif summary["examined"] or summary["unverifiable"] or summary["unreadable"]:
            summary["status"] = "examined"
        elif summary["deferred"]:
            summary["status"] = "deferred"
        return summary

    def _act(self, principal: str | None, summary: dict[str, Any]) -> None:
        """Challenge and retire every recorded rejection not yet decided."""

        from .claim_retirement import ClaimRetirementError

        rows = self.connection.execute(
            "SELECT m.claim_version_ref AS ref, m.claim_version_hash AS hash, v.record_json AS verdict "
            "FROM claim_support_backfill_marks m "
            "JOIN claim_support_verdicts v ON v.item_key=m.item_key "
            "WHERE m.outcome='verdict' "
            "AND NOT (v.support='supported' AND v.subject_relation='about_subject') "
            "AND NOT EXISTS (SELECT 1 FROM claim_retirement_decisions d "
            "  WHERE d.claim_version_ref=m.claim_version_ref) "
            "ORDER BY m.marked_at, m.claim_version_ref LIMIT ?",
            (MAX_ACTIONS_PER_RUN,),
        ).fetchall()
        summary["detected"] = len(rows)
        if principal is None:
            return
        for row in rows:
            verdict = json.loads(row["verdict"])
            try:
                challenge = self.challenges.challenge(
                    claim_version_ref=row["ref"], claim_version_hash=row["hash"],
                    reason_code=SUPPORT_REASON, rationale=_rationale(verdict),
                    actor_ref=principal)
                if challenge.get("status") == "fresh":
                    summary["challenged"].append(row["ref"])
                decision = self.challenges.decide(
                    challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
                    decision="retired", actor_ref=principal, rationale=challenge["rationale"])
            except ClaimRetirementError as exc:
                summary["skipped"].append({"claim_version_ref": row["ref"], "reason": str(exc)})
                continue
            if decision.get("status") == "fresh":
                summary["retired"].append(row["ref"])


def run_backfill(*, store: Any, missions: Any, model_config: Mapping[str, Any],
                 scheduler_db: Any, state_dir: Any, spool: Any) -> dict[str, Any]:
    """One bounded run on the extraction lane's configuration, from its child."""

    from .claim_retirement import ClaimRetirementAuthority
    from .claim_review import ClaimReviewDriver
    from .claim_support_verification import BACKFILL_PURPOSE, build_verifier, load_settings

    settings = load_settings(state_dir)
    batches = int(settings["backfill_batches_per_run"])
    per_batch = int(settings["backfill_items_per_batch"])
    if batches <= 0 or per_batch <= 0:
        return {"status": "disabled"}
    challenges = ClaimRetirementAuthority(store)
    verifier = build_verifier(
        store=store, model_config=model_config, scheduler_db=scheduler_db,
        purpose=BACKFILL_PURPOSE, daily_cap_usd=settings["backfill_daily_cap_usd"],
        max_items_per_call=per_batch)
    reader = ClaimReviewDriver(store=store, missions=missions, challenges=challenges, spool=spool)
    return ClaimSupportBackfill(
        store=store, missions=missions, verifier=verifier, reader=reader,
        challenges=challenges, claim_sources=settings["backfill_claim_sources"],
    ).run_once(max_items=batches * per_batch)


__all__ = [
    "ClaimSupportBackfill",
    "DEFAULT_MAX_DOCUMENTS",
    "PASS_REF",
    "PASS_REFS",
    "RETRY_PASS_REF",
    "ROUTE_RETRY_PASS_REF",
    "SUPPORT_REASON",
    "run_backfill",
]
