"""Writer lane for already-admitted directed document research.

The planner/research-task producer writes the immutable admission.  This lane
only selects an unstarted admission and launches its admission-ref-only child.
An orphaned or failed child is re-entered only from persisted Scheduler and
typed recovery authority.  Timed safe recovery does not block later independent
admissions; unproved send state remains held.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    write_owner_only,
)
from .lane_registry import LaneSpec, register_lane
from .store import canonical_json, content_hash


LANE_CONFIG = "mission-document-research-lane.json"
LAUNCHER_KWARG = "mission_document_research_launcher"
DRIVER_KEY = "mission_document_research"
LATEST_FILE = "latest.json"
HOLDS_FILE = "holds.json"
# C2-3: the escape ledger.  Deliberately a second file rather than another key
# in ``holds.json``: the hold ledger's shape is closed and content-sealed, and
# an installed writer must be able to read the file it already has.
ESCAPES_FILE = "recovery-escapes.json"

# How long every admission may be held, with none startable, before the lane
# reopens the oldest one that provably never sent anything.  Live on
# 2026-09-16 this state had lasted 557 consecutive ticks -- about two days --
# with ten held admissions, zero promotions and zero outcomes.
DEADLOCK_ESCAPE_AFTER = timedelta(hours=6)
# How many times one admission may be reopened this way, ever.  A second
# attempt is worth making; a third is a loop.
MAX_ESCAPES_PER_ADMISSION = 2
# The exact words the owner needs, for the admissions no automation may touch.
OWNER_AUTHORIZATION_NOTE = (
    "这条已经发起过模型调用、但调用是否真的送达/计费无法证明，因此系统不会自动重开。"
    "（只有「已经证明送达并结算、仅仅是回复不符合输出契约」那一种失败，系统才会自动"
    "重试一次；这一条不是那一种。）"
    "需要 owner 授权一次受控恢复："
    "MissionDocumentResearchExecutor.authorize_paid_contract_recovery("
    "admission_ref, authorization)，其中 authorization 的 actor_ref 必须是 "
    "\"operator:owner-authorized-document-recovery\"、stage_ordinal 为 2 或 3、"
    "max_fresh_work_orders 为 1、max_cost_usd 等于该 WorkOrder 自己的 budget.max_cost_usd。"
    "目前没有任何 CLI 或 writer 操作可以下发这条授权，只能由 owner 运行脚本。"
)
# The words for the one contract failure that has already been retried once by
# the lane itself.  This is the only contract state a person is asked about,
# and the ask has to say what was already tried, or the owner will just
# authorise the same purchase again.
CONTRACT_ESCALATION_NOTE = (
    "这条的模型回复连续两次不符合输出契约：第一次失败之后系统已经自动重试过一次"
    "（同一个 WorkOrder 的 budget.max_cost_usd 上限、只放一条新 WorkOrder、"
    "授权记录的 actor_ref 是 \"automation:document-research-contract-retry\"），"
    "重试买回来的回复仍然不合契约，所以才升级给人。"
    "先看模型或提示词为什么连续两次给不出合契约的回复——连续两次同样失败通常是契约/提示词"
    "的问题，再买一次大概率还是同一个结果。"
    "确认值得再买一次时，由 owner 授权最后一次受控恢复："
    "MissionDocumentResearchExecutor.authorize_paid_contract_recovery("
    "admission_ref, authorization)，其中 authorization 的 actor_ref 必须是 "
    "\"operator:owner-authorized-document-recovery\"、stage_ordinal 为 2 或 3、"
    "max_fresh_work_orders 为 1、max_cost_usd 等于那条失败 WorkOrder 自己的 "
    "budget.max_cost_usd（注意此时失败的是自动重试放出来的那条 WorkOrder）。"
)


# G2: the two document kinds a gap-filling inquiry is followed up out of.
# Both spellings of the annual report are here because both are live: the
# filings index writes ``annual-report-10k`` and the older search plans wrote
# ``annual-reports``, and a suggestion that silently skipped half the held
# 10-Ks would be worse than none.
PLAN_FOLLOW_UP_SPECS: frozenset[str] = frozenset({
    "annual-report-10k", "annual-reports", "earnings-call-transcripts",
})
PLAN_FOLLOW_UP_LABELS: Mapping[str, str] = {
    "annual-report-10k": "年报",
    "annual-reports": "年报",
    "earnings-call-transcripts": "电话会纪要",
}
# How many suggestions one tick shows.  A list nobody finishes reading is a
# list nobody reads.
MAX_PLAN_NEXT_STEPS = 5


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class MissionDocumentResearchLaneError(RuntimeError):
    pass


def _read_holds(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionDocumentResearchLaneError("document research hold ledger is invalid") from exc
    body = dict(record)
    asserted = body.pop("content_hash", None)
    if (
        set(record) != {"schema_version", "holds", "content_hash"}
        or record.get("schema_version") != "0.1"
        or not isinstance(record.get("holds"), dict)
        or asserted != content_hash(body)
    ):
        raise MissionDocumentResearchLaneError("document research hold ledger drifted")
    holds: dict[str, dict[str, Any]] = {}
    for admission_ref, value in record["holds"].items():
        if (
            not isinstance(admission_ref, str)
            or not admission_ref.startswith("mission-document-research-admission:")
            or not isinstance(value, Mapping)
            or set(value) != {
                "admission_hash", "ticket_ref", "reason", "disposition",
                "retry_at",
            }
            or not _sha256(value.get("admission_hash"))
            or (
                value.get("ticket_ref") is not None
                and (
                    not isinstance(value.get("ticket_ref"), str)
                    or not value["ticket_ref"].startswith(
                        "mission-document-research:"
                    )
                )
            )
            or not isinstance(value.get("reason"), str)
            or not value["reason"]
            or value.get("disposition") not in {
                "terminal_hold", "recovery_required", "recovery_wait",
            }
            or (
                value.get("disposition") == "recovery_wait"
                and (
                    not isinstance(value.get("retry_at"), str)
                    or not value["retry_at"]
                )
            )
            or (
                value.get("disposition") != "recovery_wait"
                and value.get("retry_at") is not None
            )
        ):
            raise MissionDocumentResearchLaneError(
                "document research hold ledger has an invalid entry"
            )
        if value["disposition"] == "recovery_wait":
            try:
                parsed = datetime.fromisoformat(value["retry_at"])
            except ValueError as exc:
                raise MissionDocumentResearchLaneError(
                    "document research hold ledger has an invalid retry_at"
                ) from exc
            if parsed.tzinfo is None:
                raise MissionDocumentResearchLaneError(
                    "document research hold ledger retry_at lacks timezone"
                )
        holds[admission_ref] = dict(value)
    return holds


def _write_holds(path: Path, holds: Mapping[str, Mapping[str, Any]]) -> None:
    body = {
        "schema_version": "0.1",
        "holds": {key: dict(holds[key]) for key in sorted(holds)},
    }
    write_owner_only(path, {**body, "content_hash": content_hash(body)})


class MissionDocumentResearchCoordinator:
    def __init__(
        self, *, store: Any, launcher: Any | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def latest_path(self) -> Path:
        return self.launcher.tickets_dir / LATEST_FILE

    @property
    def holds_path(self) -> Path:
        return self.launcher.tickets_dir / HOLDS_FILE

    @property
    def escapes_path(self) -> Path:
        return self.launcher.tickets_dir / ESCAPES_FILE

    def _read_escapes(self) -> dict[str, Any]:
        """The lane's own memory of being stuck, and of what it did about it."""

        if not self.escapes_path.is_file():
            return {"schema_version": "0.1", "stuck_since": None, "escapes": {}}
        try:
            value = json.loads(self.escapes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": "0.1", "stuck_since": None, "escapes": {}}
        if not isinstance(value, Mapping) or not isinstance(value.get("escapes"), Mapping):
            return {"schema_version": "0.1", "stuck_since": None, "escapes": {}}
        return {"schema_version": "0.1",
                "stuck_since": value.get("stuck_since"),
                "escapes": {str(key): int(count)
                            for key, count in value["escapes"].items()
                            if isinstance(count, int) and not isinstance(count, bool)}}

    def _write_escapes(self, record: Mapping[str, Any]) -> None:
        write_owner_only(self.escapes_path, {
            "schema_version": "0.1",
            "stuck_since": record.get("stuck_since"),
            "escapes": {key: int(value) for key, value in sorted(
                (record.get("escapes") or {}).items())},
        })

    def _hold_detail(
        self, holds: Mapping[str, Mapping[str, Any]],
        admissions: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Every held admission and the real reason, for the owner's list.

        The terminal return used to say ``no unstarted document research
        admission`` and nothing else, so the ten actual reasons --
        ``fresh_work_recovery_disabled``, ``send_state_unproved``,
        ``started_without_owned_live_ticket`` -- were written to a file nothing
        else in the system reads.  A lane that only a person can unblock has to
        say what the person is being asked to do.
        """

        from .mission_document_research_executor import (
            CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
        )

        order = {admission["id"]: index for index, admission in enumerate(admissions)}
        detail = []
        for admission_ref, held in holds.items():
            started = self._started(admission_ref)
            # An admission that never started sent nothing and cost nothing, so
            # reopening it is free and the lane may do it.  One that started may
            # have been charged for a send nobody can prove, and only the owner
            # may authorise that.  A timed wait -- a budget day, the automatic
            # contract retry's daily cap -- is the lane's own business and must
            # never be put in front of a person: that is exactly the pile the
            # owner asked not to be shown.
            needs_owner = bool(started) and held["disposition"] == "recovery_required"
            detail.append({
                "admission_ref": admission_ref,
                "disposition": held["disposition"],
                "reason": held["reason"],
                "ticket_ref": held.get("ticket_ref"),
                "started": started,
                "needs_owner_authorization": needs_owner,
                "owner_action": (
                    (CONTRACT_ESCALATION_NOTE
                     if held["reason"] == CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY
                     else OWNER_AUTHORIZATION_NOTE)
                    if needs_owner else None
                ),
                "order": order.get(admission_ref, len(order)),
            })
        detail.sort(key=lambda item: (item["order"], item["admission_ref"]))
        return detail

    def _escape_deadlock(
        self, holds: dict[str, dict[str, Any]], detail: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """Reopen at most one never-started admission after a long deadlock.

        Bounded three ways: only after ``DEADLOCK_ESCAPE_AFTER`` of an
        unbroken all-held state, only for an admission with no start row (so
        no model call was ever made and no reservation can be open), and at
        most ``MAX_ESCAPES_PER_ADMISSION`` times for any one admission.  Every
        reopen is written down with its reason.

        This is deliberately the *only* automatic escape.  An admission that
        started is left exactly where it is, because the discipline that keeps
        this system honest is that a send whose outcome is unknown is never
        replayed without a person saying so.
        """

        record = self._read_escapes()
        now = self.clock().astimezone(timezone.utc)
        stuck_since = record.get("stuck_since")
        if not stuck_since:
            record["stuck_since"] = now.isoformat(timespec="microseconds")
            self._write_escapes(record)
            return None
        try:
            since = datetime.fromisoformat(str(stuck_since))
        except ValueError:
            record["stuck_since"] = now.isoformat(timespec="microseconds")
            self._write_escapes(record)
            return None
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if now - since < DEADLOCK_ESCAPE_AFTER:
            return None
        counts = dict(record.get("escapes") or {})
        candidate = next(
            (item for item in detail
             if not item["started"]
             and counts.get(item["admission_ref"], 0) < MAX_ESCAPES_PER_ADMISSION),
            None,
        )
        if candidate is None:
            return None
        admission_ref = candidate["admission_ref"]
        holds.pop(admission_ref, None)
        _write_holds(self.holds_path, holds)
        counts[admission_ref] = counts.get(admission_ref, 0) + 1
        self._write_escapes({"stuck_since": now.isoformat(timespec="microseconds"),
                             "escapes": counts})
        return {
            "admission_ref": admission_ref,
            "prior_reason": candidate["reason"],
            "attempt": counts[admission_ref],
            "held_since": stuck_since,
            "rationale": (
                "所有 admission 都被 hold 且超过 "
                f"{int(DEADLOCK_ESCAPE_AFTER.total_seconds() // 3600)} 小时没有任何一条可启动；"
                "这一条从未写入 start 记录，因此没有发出过模型调用、也没有未结算的预留，"
                "重开它不会重复计费。"
            ),
        }

    def _latest(self) -> dict[str, Any] | None:
        if not self.latest_path.is_file():
            return None
        try:
            value = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer is invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer has an invalid shape"
            )
        body = dict(value)
        asserted = body.pop("content_hash", None)
        if (
            set(value) != {
                "ticket_ref", "admission_ref", "admission_hash", "content_hash",
            }
            or asserted != content_hash(body)
            or not isinstance(value.get("ticket_ref"), str)
            or not value["ticket_ref"].startswith("mission-document-research:")
            or not isinstance(value.get("admission_ref"), str)
            or not value["admission_ref"].startswith(
                "mission-document-research-admission:"
            )
            or not _sha256(value.get("admission_hash"))
        ):
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer drifted"
            )
        return dict(value)

    def _owned_terminal_ticket_ref(
        self, admission: Mapping[str, Any],
    ) -> str | None:
        matches = []
        for path in self.launcher.tickets_dir.glob("*/ticket.json"):
            ticket_ref = "mission-document-research:" + path.parent.name
            try:
                ticket = self.launcher.status(ticket_ref)
            except LookupError:
                continue
            if (
                ticket.get("status") != "running"
                and ticket.get("admission_ref") == admission["id"]
                and ticket.get("admission_hash") == admission["content_hash"]
            ):
                matches.append(ticket_ref)
        if len(matches) > 1:
            raise MissionDocumentResearchLaneError(
                "document research admission has multiple owned terminal tickets"
            )
        return matches[0] if matches else None

    def _admissions(self) -> list[dict[str, Any]]:
        try:
            has_outcomes = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='mission_document_research_outcomes'"
            ).fetchone() is not None
            from .research_auto_commit import policy_lists_document_rule

            promotion_required = policy_lists_document_rule(
                self.store.active_policy()
            )
            has_promotions = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='mission_document_research_promotions'"
            ).fetchone() is not None
            if promotion_required and has_outcomes and not has_promotions:
                raise MissionDocumentResearchLaneError(
                    "document research promotion authority is unavailable"
                )
            joins = where = ""
            if has_outcomes:
                joins = (
                    "LEFT JOIN mission_document_research_outcomes o "
                    "ON o.admission_ref=a.admission_id "
                )
                where = "WHERE o.outcome_id IS NULL "
                if promotion_required:
                    joins += (
                        "LEFT JOIN mission_document_research_promotions p "
                        "ON p.admission_ref=a.admission_id "
                    )
                    where = "WHERE o.outcome_id IS NULL OR p.promotion_id IS NULL "
            query = (
                "SELECT a.* FROM mission_document_research_admissions a "
                + joins + where + "ORDER BY a.created_at,a.admission_id"
            )
            rows = self.store.connection.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        result = []
        for row in rows:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research admission record is invalid"
                ) from exc
            body = dict(wire)
            asserted = body.pop("content_hash", None)
            if (
                canonical_json(wire) != row["record_json"]
                or wire.get("id") != row["admission_id"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
            ):
                raise MissionDocumentResearchLaneError(
                    "document research admission authority drifted"
                )
            result.append(wire)
        return result

    def _latest_plan_row(self) -> dict[str, Any] | None:
        """The newest research plan of a mission this install actually points at."""

        for query in (
            "SELECT p.plan_id AS plan_id, p.mission_version_ref AS mission_version_ref, "
            "p.inquiries_json AS inquiries_json, p.created_at AS created_at "
            "FROM coverage_mission_research_plans p "
            "JOIN coverage_mission_pointer m ON m.mission_version_id=p.mission_version_ref "
            "ORDER BY p.created_at DESC, p.plan_id DESC LIMIT 1",
            "SELECT plan_id, mission_version_ref, inquiries_json, created_at "
            "FROM coverage_mission_research_plans "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 1",
        ):
            try:
                row = self.store.connection.execute(query).fetchone()
            except sqlite3.Error:
                continue
            if row is not None:
                return dict(row)
        return None

    def _admitted_inquiry_refs(self) -> set[str]:
        """Every inquiry this install has already bought work for, ever.

        Read straight from the admission table rather than from the open list:
        an inquiry whose admission has already run and settled is answered, and
        offering it again as a next step is how a list of suggestions becomes a
        list nobody trusts.
        """

        try:
            rows = self.store.connection.execute(
                "SELECT record_json FROM mission_document_research_admissions"
            ).fetchall()
        except sqlite3.Error:
            return set()
        refs: set[str] = set()
        for row in rows:
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError):
                continue
            ref = record.get("inquiry_ref") if isinstance(record, Mapping) else None
            if isinstance(ref, str) and ref:
                refs.add(ref)
        return refs

    def _plan_next_steps(self) -> list[dict[str, Any]]:
        """What the newest plan asks for that the held material can already answer.

        G2.  ``research_planner.gap_filling_inquiries`` marks an inquiry
        ``addressable`` when the plan named an exact document for it.  When
        that document has also been acquired *and* fully read, the question is
        answerable out of bytes already paid for -- and until now nothing said
        so anywhere a person looks.

        Read-only, and deliberately so.  Acting on one of these means writing
        an admission, and an admission is a paid-work authority that needs the
        owner's whole chain: the exact plan, the exact inquiry, the exact
        question version, the exact document registration.  This lane holds
        none of that, so it says what it sees and stops.

        Fail-open at every step: a Core without these tables, an unreadable
        plan, an inquiry whose identity cannot be computed -- each is silence,
        never a failed tick.  A suggestion is worth nothing if it can stop the
        lane it is advising.
        """

        from .research_planner import gap_filling_inquiries

        plan = self._latest_plan_row()
        if plan is None:
            return []
        try:
            inquiries = json.loads(plan["inquiries_json"])
            ranked = gap_filling_inquiries({"inquiries": inquiries})
        except (TypeError, ValueError, RecursionError):
            return []
        admitted = self._admitted_inquiry_refs()
        steps: list[dict[str, Any]] = []
        for row in ranked:
            if len(steps) >= MAX_PLAN_NEXT_STEPS:
                break
            if not row["addressable"]:
                continue
            strategy = row["directed_document"]
            document_ref = (strategy or {}).get("document_ref")
            if not isinstance(document_ref, str) or not document_ref:
                continue
            held = self._held_document(plan["mission_version_ref"], document_ref)
            if held is None or held["spec_ref"] not in PLAN_FOLLOW_UP_SPECS:
                continue
            if held["status"] != "acquired" or not held["read_complete"]:
                continue
            position = row["plan_position"]
            raw = (inquiries[position]
                   if isinstance(inquiries, list) and position < len(inquiries) else None)
            if isinstance(raw, Mapping) and self._inquiry_ref(raw) in admitted:
                # Already bought once.  Suggesting it again is how a list of
                # next steps becomes a list nobody trusts.
                continue
            kind = PLAN_FOLLOW_UP_LABELS.get(held["spec_ref"], "原始材料")
            subject = row["company_ref"] or "全行业"
            steps.append({
                "rank": row["rank"],
                "company_ref": row["company_ref"],
                "document_ref": document_ref,
                "spec_ref": held["spec_ref"],
                "question": row["question"],
                "suggestion": (
                    f"计划建议的下一步：{subject}「{row['question']}」——"
                    f"计划点名的{kind} {document_ref} 已经获取并读完，"
                    "现在就能从已有材料里回答。"
                    "本车道只做展示：真要做这条，需要 owner 的授权链写入 admission。"
                ),
            })
        return steps

    def _held_document(
        self, mission_version_ref: Any, document_ref: str,
    ) -> dict[str, Any] | None:
        """Acquisition status, document kind and whether it has been read through."""

        try:
            row = self.store.connection.execute(
                "SELECT d.status AS status, s.spec_ref AS spec_ref "
                "FROM coverage_mission_discovered_documents d "
                "LEFT JOIN coverage_mission_source_discoveries s "
                "ON s.record_id=d.discovery_ref "
                "WHERE d.mission_version_ref=? AND d.document_ref=?",
                (mission_version_ref, document_ref),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            proof = self.store.connection.execute(
                "SELECT 1 FROM document_read_completion_proofs WHERE document_ref=? LIMIT 1",
                (document_ref,),
            ).fetchone()
        except sqlite3.Error:
            # No read-completion authority in this Core: "read through" cannot
            # be proved, so it is not claimed.
            return None
        return {"status": row["status"], "spec_ref": row["spec_ref"],
                "read_complete": proof is not None}

    @staticmethod
    def _inquiry_ref(inquiry: Mapping[str, Any]) -> str | None:
        from .research_task import inquiry_content_hash, inquiry_ref_for

        try:
            return inquiry_ref_for(inquiry_content_hash(inquiry))
        except Exception:  # noqa: BLE001 - an unhashable inquiry is not a decision
            return None

    def _started(self, admission_ref: str) -> bool:
        try:
            return self.store.connection.execute(
                "SELECT 1 FROM mission_document_research_starts WHERE admission_ref=?",
                (admission_ref,),
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return False
            raise

    def _hold(
        self, holds: dict[str, dict[str, Any]], admission: Mapping[str, Any],
        *, reason: str, ticket_ref: str | None,
        disposition: str = "terminal_hold",
        retry_at: str | None = None,
    ) -> None:
        holds[admission["id"]] = {
            "admission_hash": admission["content_hash"],
            "ticket_ref": ticket_ref,
            "reason": reason,
            "disposition": disposition,
            "retry_at": retry_at,
        }
        _write_holds(self.holds_path, holds)

    def _effective_work_hints(
        self, admission: Mapping[str, Any],
    ) -> list[str]:
        """Read recovery replacements only as relaunch hints.

        The child executor revalidates the complete admission, route, budget,
        no-send proof, and recovery chain before it can claim or send.  This
        read prevents the Writer from permanently watching a superseded Work;
        it does not itself authorize a provider call.
        """

        refs = [
            "work:mission-document-research-" + content_hash({
                "admission_identity_hash": admission["identity_hash"],
                "ordinal": ordinal,
            })[:32]
            for ordinal in range(1, 5)
        ]
        try:
            rows = self.store.connection.execute(
                "SELECT * FROM mission_document_research_recovery_links "
                "WHERE admission_ref=? ORDER BY stage_ordinal,recovery_number",
                (admission["id"],),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return refs
            raise
        next_number = {2: 1, 3: 1}
        for row in rows:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research recovery hint is invalid"
                ) from exc
            body = dict(wire) if isinstance(wire, Mapping) else {}
            asserted = body.pop("content_hash", None)
            stage = wire.get("stage_ordinal") if isinstance(wire, Mapping) else None
            if (
                not isinstance(wire, Mapping)
                or canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
                or wire.get("id") != row["recovery_link_id"]
                or wire.get("admission_ref") != admission["id"]
                or wire.get("admission_hash") != admission["content_hash"]
                or stage not in {2, 3}
                or row["stage_ordinal"] != stage
                or wire.get("recovery_number") != next_number[stage]
                or row["recovery_number"] != next_number[stage]
                or wire.get("failed_work_order_ref") != refs[stage - 1]
                or row["failed_work_order_ref"] != refs[stage - 1]
                or wire.get("recovery_work_order_ref")
                != row["recovery_work_order_ref"]
            ):
                raise MissionDocumentResearchLaneError(
                    "document research recovery hint drifted"
                )
            refs[stage - 1] = wire["recovery_work_order_ref"]
            next_number[stage] += 1
        return refs

    def _typed_recovery_state(
        self, admission: Mapping[str, Any], work_ref: str,
    ) -> dict[str, Any] | None:
        mission_ref = admission.get("mission_version_ref")
        if not isinstance(mission_ref, str):
            return None
        from .mission_document_research_executor import (
            read_mission_document_research_observations,
        )

        observations = [
            item for item in read_mission_document_research_observations(
                self.store.connection, mission_version_ref=mission_ref,
            )
            if item["admission_ref"] == admission["id"]
            and item["work_order_ref"] == work_ref
            and item["outcome"] == "recovery_required"
        ]
        if not observations:
            return None
        by_status = {
            item["recovery"]["status"]: item for item in observations
            if isinstance(item.get("recovery"), Mapping)
        }
        stopped = [
            item for item in observations
            if isinstance(item.get("recovery"), Mapping)
            and item["recovery"].get("status") == "stopped"
        ]
        # A current-contract terminal observation supersedes an immutable
        # legacy deadline row regardless of their content-addressed ID order.
        current_stopped = [
            item for item in stopped
            if item["recovery"].get("reason")
            != "fresh_work_recovery_deadline_exceeded"
        ]
        selected = (
            current_stopped[-1] if current_stopped else
            stopped[-1] if stopped else
            next((by_status[key] for key in ("eligible", "waiting", "admitted")
                  if key in by_status), None)
        )
        if selected is None:
            raise MissionDocumentResearchLaneError(
                "document research recovery observation has an invalid state"
            )
        recovery = selected["recovery"]
        from .mission_document_research_executor import LEGACY_PAID_CONTRACT_REASON

        if (recovery["status"] == "stopped"
                and recovery.get("reason") == LEGACY_PAID_CONTRACT_REASON):
            # Releases before the bounded automatic contract retry existed
            # recorded a proved paid contract rejection as permanently stopped
            # and waited for a signature.  Such an admission has not spent its
            # one automatic retry, so the child may re-enter: the executor
            # revalidates the whole paid-send proof before it can enqueue
            # anything, and then records either the retry or the daily cap.
            # Any newer row for this same Work supersedes the legacy verdict.
            newer = next((by_status[key] for key in ("waiting", "admitted")
                          if key in by_status), None)
            if newer is None:
                return {
                    "action": "resume",
                    "reason": "automatic_contract_retry_available",
                    "work_order_ref": work_ref,
                }
            recovery = newer["recovery"]
        if recovery["status"] == "admitted":
            # A controlled retry was already admitted for this Work.  Re-enter:
            # the executor rebuilds the exact effective Work, so this also
            # repairs an interrupted link write.
            return {
                "action": "resume", "reason": "controlled_contract_retry_admitted",
                "work_order_ref": work_ref,
            }
        if recovery["status"] == "stopped":
            # Releases before the bounded UTC-day recovery contract could
            # persist a daily-budget refusal as permanently stopped when its
            # midnight retry fell beyond the ordinary elapsed window.  Keep
            # that immutable row, but allow its first proven daily reset to
            # reach the executor, which revalidates the full refusal authority
            # before it can enqueue a fresh WorkOrder.
            proof = recovery.get("proof")
            legacy_day_wait = (
                recovery.get("reason") == "fresh_work_recovery_deadline_exceeded"
                and recovery.get("used_fresh_work_orders") == 0
                and isinstance(proof, Mapping)
                and proof.get("classification") == "atomic_day_budget_refusal"
            )
            if legacy_day_wait:
                retry_at = recovery.get("retry_at")
                old_deadline = recovery.get("deadline")
                if (not isinstance(retry_at, str) or not retry_at
                        or not isinstance(old_deadline, str) or not old_deadline):
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget recovery lacks its time bounds"
                    )
                try:
                    parsed = datetime.fromisoformat(retry_at)
                    parsed_old_deadline = datetime.fromisoformat(old_deadline)
                except ValueError as exc:
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget time bound is invalid"
                    ) from exc
                if parsed.tzinfo is None or parsed_old_deadline.tzinfo is None:
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget time bound lacks timezone"
                    )
                due = parsed.astimezone(timezone.utc)
                prior_deadline = parsed_old_deadline.astimezone(timezone.utc)
                from .mission_document_research_executor import _recovery_policy
                stage_index = {
                    "qualitative_model_draft": 1,
                    "independent_qualitative_verifier": 2,
                }.get(selected.get("stage"))
                if stage_index is None:
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget recovery stage is invalid"
                    )
                policy = _recovery_policy(admission, stage_index)
                try:
                    failed_at = datetime.fromisoformat(
                        str(proof.get("failed_at")).replace("Z", "+00:00")
                    )
                    refusal_reset = datetime.fromisoformat(
                        str(proof.get("refusal_day"))
                    ).replace(tzinfo=timezone.utc) + timedelta(days=1)
                except (TypeError, ValueError) as exc:
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget proof time is invalid"
                    ) from exc
                if failed_at.tzinfo is None:
                    raise MissionDocumentResearchLaneError(
                        "document research daily-budget proof time lacks timezone"
                    )
                failed_at = failed_at.astimezone(timezone.utc)
                expected_due = max(
                    failed_at + timedelta(seconds=policy["retry_backoff_seconds"]),
                    refusal_reset,
                )
                expected_old_deadline = failed_at + timedelta(
                    seconds=policy["max_elapsed_seconds"],
                )
                # A current-contract stopped row has a deadline beyond its
                # eligible instant.  Only the exact old cross-UTC shape is
                # eligible for this compatibility path.
                if (due != expected_due or prior_deadline != expected_old_deadline
                        or prior_deadline > due):
                    return {
                        "action": "recovery_required", "reason": recovery["reason"],
                        "work_order_ref": work_ref,
                    }
                extended_deadline = due + timedelta(
                    seconds=policy["max_elapsed_seconds"],
                )
                now = self.clock().astimezone(timezone.utc)
                if now < due:
                    return {
                        "action": "waiting", "reason": "fresh_work_recovery_backoff",
                        "retry_at": retry_at, "work_order_ref": work_ref,
                    }
                if now >= extended_deadline:
                    return {
                        "action": "recovery_required",
                        "reason": "fresh_work_recovery_day_window_exceeded",
                        "work_order_ref": work_ref,
                    }
                return {
                    "action": "resume", "reason": "typed_day_budget_recovery_due",
                    "work_order_ref": work_ref,
                }
            return {
                "action": "recovery_required", "reason": recovery["reason"],
                "work_order_ref": work_ref,
            }
        retry_at = recovery.get("retry_at")
        if not isinstance(retry_at, str) or not retry_at:
            raise MissionDocumentResearchLaneError(
                "document research recovery observation lacks retry_at"
            )
        try:
            parsed = datetime.fromisoformat(retry_at)
        except ValueError as exc:
            raise MissionDocumentResearchLaneError(
                "document research recovery retry_at is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise MissionDocumentResearchLaneError(
                "document research recovery retry_at lacks timezone"
            )
        due = parsed.astimezone(timezone.utc)
        if self.clock().astimezone(timezone.utc) < due:
            return {
                "action": "waiting", "reason": recovery["reason"],
                "retry_at": retry_at, "work_order_ref": work_ref,
            }
        return {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": work_ref,
        }

    def _execution_state(self, admission: Mapping[str, Any]) -> dict[str, Any]:
        """Classify only authority already persisted for this admission.

        A ready/missing node can be resumed because the executor reconstructs
        the exact Work and the adapter replays an existing route before any
        new send.  A formal failure is never relaunched here; typed local
        capacity and budget failures are exposed for the recovery graph, and
        every other formal failure stays terminal/unknown.
        """

        from .contracts import ResultEnvelope, WorkOrder

        for work_ref in self._effective_work_hints(admission):
            row = self.store.connection.execute(
                "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
                "WHERE work_order_id=?", (work_ref,),
            ).fetchone()
            if row is None:
                return {
                    "action": "resume",
                    "reason": "next_exact_work_not_yet_admitted",
                    "work_order_ref": work_ref,
                }
            try:
                work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
            except Exception as exc:
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work authority is invalid"
                ) from exc
            if (
                canonical_json(work) != row["work_order_json"]
                or content_hash(work) != row["work_order_hash"]
                or work["id"] != work_ref
                or work["metadata"].get(
                    "mission_document_research_admission_ref"
                ) != admission["id"]
                or work["metadata"].get(
                    "mission_document_research_admission_hash"
                ) != admission["content_hash"]
            ):
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work authority drifted"
                )
            formal = self.store.connection.execute(
                "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
                (work_ref,),
            ).fetchone()
            if formal is not None:
                try:
                    envelope = ResultEnvelope.from_dict(
                        json.loads(formal["result_envelope_json"])
                    ).to_dict()
                except Exception as exc:
                    raise MissionDocumentResearchLaneError(
                        "document research formal result is invalid"
                    ) from exc
                formal_body = {
                    "id": formal["result_record_id"],
                    "work_order_id": formal["work_order_id"],
                    "attempt_number": formal["attempt_number"],
                    "result_envelope_id": formal["result_envelope_id"],
                    "result_envelope_hash": formal["result_envelope_hash"],
                    "terminal_state": formal["terminal_state"],
                    "created_at": formal["created_at"],
                }
                if (
                    canonical_json(envelope) != formal["result_envelope_json"]
                    or envelope["work_order_ref"] != work_ref
                    or envelope["id"] != formal["result_envelope_id"]
                    or content_hash(envelope) != formal["result_envelope_hash"]
                    or content_hash(formal_body) != formal["content_hash"]
                ):
                    raise MissionDocumentResearchLaneError(
                        "document research formal result authority drifted"
                    )
                if formal["terminal_state"] == "succeeded":
                    continue
                typed = self._typed_recovery_state(admission, work_ref)
                return typed or {
                    "action": "resume",
                    "reason": "recovery_classification_not_yet_recorded",
                    "work_order_ref": work_ref,
                }
            event = self.store.connection.execute(
                "SELECT state,not_before,attempt_number FROM scheduler_attempt_events "
                "WHERE work_order_id=? ORDER BY event_seq DESC LIMIT 1",
                (work_ref,),
            ).fetchone()
            if event is None:
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work has no attempt authority"
                )
            refusal = self._budget_refusal(work, int(event["attempt_number"]))
            if refusal is not None:
                return {
                    "action": "recovery_required", "reason": refusal,
                    "work_order_ref": work_ref,
                }
            if event["state"] == "ready":
                not_before = event["not_before"]
                if not_before is not None and self.clock().astimezone(
                    timezone.utc
                ) < datetime.fromisoformat(not_before).astimezone(timezone.utc):
                    return {
                        "action": "waiting", "reason": "scheduler_backoff",
                        "retry_at": not_before, "work_order_ref": work_ref,
                    }
                return {
                    "action": "resume", "reason": "scheduler_ready",
                    "work_order_ref": work_ref,
                }
            if event["state"] == "leased":
                lease = self.store.connection.execute(
                    "SELECT expires_at FROM scheduler_leases WHERE work_order_id=? "
                    "ORDER BY lease_version DESC LIMIT 1", (work_ref,),
                ).fetchone()
                if lease is None:
                    raise MissionDocumentResearchLaneError(
                        "document research leased Work lost its lease authority"
                    )
                expires_at = lease["expires_at"]
                if self.clock().astimezone(timezone.utc) < datetime.fromisoformat(
                    expires_at
                ).astimezone(timezone.utc):
                    return {
                        "action": "waiting", "reason": "existing_lease",
                        "retry_at": expires_at, "work_order_ref": work_ref,
                    }
                return {
                    "action": "resume", "reason": "expired_lease_replay",
                    "work_order_ref": work_ref,
                }
            typed = self._typed_recovery_state(admission, work_ref)
            return typed or {
                "action": "resume",
                "reason": "recovery_classification_not_yet_recorded",
                "work_order_ref": work_ref,
            }
        return {
            "action": "resume", "reason": "outcome_commit_not_yet_finished",
            "work_order_ref": None,
        }

    @staticmethod
    def _budget_refusal(
        work: Mapping[str, Any], attempt_number: int,
    ) -> str | None:
        stage = work.get("metadata", {}).get("stage")
        if stage not in {
            "qualitative_model_draft", "independent_qualitative_verifier",
        }:
            return None
        path = work["metadata"].get("budget_db")
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        phase = (
            "verification"
            if stage == "independent_qualitative_verifier" else "assessment"
        )
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            candidates = []
            for table in (
                "thesis_impact_day_rejections", "model_budget_pool_rejections",
            ):
                try:
                    candidates.extend(connection.execute(
                        f"SELECT record_json,content_hash FROM {table} "
                        "WHERE work_order_ref=? AND attempt_number=? AND phase=?",
                        (work["id"], attempt_number, phase),
                    ).fetchall())
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
            if not candidates:
                return None
            if len(candidates) != 1:
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal is not unique"
                )
            row = candidates[0]
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal is invalid"
                ) from exc
            body = dict(wire) if isinstance(wire, Mapping) else {}
            asserted = body.pop("content_hash", None)
            if (
                not isinstance(wire, Mapping)
                or canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
                or wire.get("work_order_ref") != work["id"]
                or wire.get("attempt_number") != attempt_number
                or wire.get("phase") != phase
            ):
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal authority drifted"
                )
            reason = wire.get("reason")
            return (
                "budget_or_pool_refused:"
                + (reason if isinstance(reason, str) and reason else "owner_budget_exceeded")
            )
        finally:
            connection.close()

    def _resume(
        self, admission: Mapping[str, Any], *, ticket_ref: str,
        recovery: Mapping[str, Any], settled: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        authorization = canonical_json({
            "schema_version": "0.1",
            "kind": "exact_scheduler_replay",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "prior_ticket_ref": ticket_ref,
            "work_order_ref": recovery.get("work_order_ref"),
            "reason": recovery["reason"],
        })
        resumed = self.launcher.resume(
            admission_ref=admission["id"],
            admission_hash=admission["content_hash"],
            prior_ticket_ref=ticket_ref,
            authorization=authorization,
        )
        pointer = {
            "ticket_ref": resumed["id"],
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        write_owner_only(
            self.latest_path,
            {**pointer, "content_hash": content_hash(pointer)},
        )
        return {
            "status": "resumed", "ticket_ref": resumed["id"],
            "admission_ref": admission["id"], "last": settled,
        }

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "document research lane is absent"}
        holds = _read_holds(self.holds_path)
        admissions = self._admissions()
        # A completed child records query_miss/no_verified_claim/candidate_staged
        # as an immutable observation.  Older dispatches could later recreate a
        # generic started_without_owned_live_ticket hold because those terminal
        # outcomes intentionally do not occupy the promoted-outcome table.
        # Reconcile only from the latest fully validated observation; a later
        # recovery_required observation must remain visible.
        from .mission_document_research_executor import (
            read_mission_document_research_observations,
        )
        latest_observation: dict[str, Mapping[str, Any]] = {}
        for observation in read_mission_document_research_observations(
            self.store.connection
        ):
            latest_observation[observation["admission_ref"]] = observation
        completed_refs = {
            admission_ref
            for admission_ref, observation in latest_observation.items()
            if observation["outcome"] in {
                "query_miss", "no_verified_claim",
            }
        }
        admissions = [
            admission for admission in admissions
            if admission["id"] not in completed_refs
        ]
        by_ref = {item["id"]: item for item in admissions}
        if any(admission_ref in holds for admission_ref in completed_refs):
            holds = {
                admission_ref: held
                for admission_ref, held in holds.items()
                if admission_ref not in completed_refs
            }
            _write_holds(self.holds_path, holds)
        latest = self._latest()
        settled = None
        if latest is not None and latest["admission_ref"] in by_ref:
            admission = by_ref[latest["admission_ref"]]
            if latest["admission_hash"] != admission["content_hash"]:
                raise MissionDocumentResearchLaneError("latest admission hash drifted")
            ticket = self.launcher.status(latest["ticket_ref"])
            if ticket["status"] == "running":
                return {
                    "status": "busy", "ticket_ref": ticket["id"],
                    "admission_ref": admission["id"],
                }
            summary = ticket.get("summary")
            child_status = None if summary is None else summary.get("status")
            settled = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "status": child_status or ticket["status"],
            }
            if child_status != "complete":
                recovery = (
                    self._execution_state(admission)
                    if self._started(admission["id"]) else {
                        "action": "terminal_hold",
                        "reason": child_status or ticket["status"],
                    }
                )
                settled["recovery"] = recovery
                if recovery["action"] == "waiting":
                    self._hold(
                        holds, admission, reason=recovery["reason"],
                        ticket_ref=ticket["id"], disposition="recovery_wait",
                        retry_at=recovery["retry_at"],
                    )
                    recovery = None
                elif recovery["action"] == "resume":
                    try:
                        result = self._resume(
                            admission, ticket_ref=ticket["id"],
                            recovery=recovery, settled=settled,
                        )
                    except LaneChildConflict as exc:
                        return {"status": "busy", "reason": str(exc), "last": settled}
                    except LaneChildRejected as exc:
                        self._hold(
                            holds, admission,
                            reason="controlled_reentry_unavailable:" + str(exc),
                            ticket_ref=ticket["id"],
                            disposition="recovery_required",
                        )
                        settled["recovery"] = {
                            "action": "recovery_required",
                            "reason": "controlled_reentry_unavailable:" + str(exc),
                        }
                        recovery = settled["recovery"]
                    else:
                        if holds.pop(admission["id"], None) is not None:
                            _write_holds(self.holds_path, holds)
                        return result
                if recovery is not None:
                    disposition = (
                        "recovery_required"
                        if recovery["action"] == "recovery_required"
                        else "terminal_hold"
                    )
                    self._hold(
                        holds, admission, reason=recovery["reason"],
                        ticket_ref=ticket["id"], disposition=disposition,
                    )

        now = self.clock().astimezone(timezone.utc)
        for admission in admissions:
            held = holds.get(admission["id"])
            legacy_day_hold = (
                held is not None
                and held["disposition"] == "recovery_required"
                and held["reason"] == "fresh_work_recovery_deadline_exceeded"
            )
            if held is None or held["disposition"] not in {
                "recovery_wait", "recovery_required",
            }:
                continue
            if held["admission_hash"] != admission["content_hash"]:
                raise MissionDocumentResearchLaneError(
                    "document research hold admission hash drifted"
                )
            if held["disposition"] == "recovery_wait":
                try:
                    retry_at = datetime.fromisoformat(held["retry_at"]).astimezone(
                        timezone.utc
                    )
                except ValueError as exc:
                    raise MissionDocumentResearchLaneError(
                        "document research hold retry_at is invalid"
                    ) from exc
                if now < retry_at:
                    continue
            recovery = self._execution_state(admission)
            if recovery["action"] == "waiting":
                self._hold(
                    holds, admission, reason=recovery["reason"],
                    ticket_ref=held["ticket_ref"], disposition="recovery_wait",
                    retry_at=recovery["retry_at"],
                )
                continue
            if recovery["action"] != "resume":
                self._hold(
                    holds, admission, reason=recovery["reason"],
                    ticket_ref=held["ticket_ref"],
                    disposition=("recovery_required"
                                 if recovery["action"] == "recovery_required"
                                 else "terminal_hold"),
                )
                continue
            ticket_ref = held["ticket_ref"] or self._owned_terminal_ticket_ref(
                admission
            )
            if ticket_ref is None:
                self._hold(
                    holds, admission, reason="controlled_reentry_ticket_unavailable",
                    ticket_ref=None, disposition="recovery_required",
                )
                continue
            try:
                result = self._resume(
                    admission, ticket_ref=ticket_ref,
                    recovery=recovery, settled=settled,
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "last": settled}
            except LaneChildRejected as exc:
                self._hold(
                    holds, admission,
                    reason="controlled_reentry_unavailable:" + str(exc),
                    ticket_ref=held["ticket_ref"],
                    disposition="recovery_required",
                )
                continue
            holds.pop(admission["id"], None)
            _write_holds(self.holds_path, holds)
            return result

        for admission in admissions:
            held = holds.get(admission["id"])
            if held is not None:
                if held["admission_hash"] != admission["content_hash"]:
                    raise MissionDocumentResearchLaneError(
                        "document research hold admission hash drifted"
                    )
                continue
            if self._started(admission["id"]):
                self._hold(
                    holds, admission,
                    reason="started_without_owned_live_ticket",
                    ticket_ref=None, disposition="recovery_required",
                )
                continue
            try:
                ticket = self.launcher.start(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "last": settled}
            except LaneChildRejected as exc:
                self._hold(
                    holds, admission, reason=str(exc), ticket_ref=None
                )
                continue
            pointer = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "admission_hash": admission["content_hash"],
            }
            write_owner_only(
                self.latest_path,
                {**pointer, "content_hash": content_hash(pointer)},
            )
            return {
                "status": "launched", "ticket_ref": ticket["id"],
                "admission_ref": admission["id"], "last": settled,
            }
        detail = self._hold_detail(holds, admissions)
        status = (
            "recovery_required"
            if any(item["disposition"] == "recovery_required"
                   for item in holds.values())
            else "waiting"
            if any(item["disposition"] == "recovery_wait"
                   for item in holds.values())
            else "idle"
        )
        escaped = None
        if holds and status in ("recovery_required", "waiting"):
            # C2-3: nothing is startable and nothing is resumable.  After a
            # long enough unbroken deadlock, reopen exactly one admission that
            # provably never sent anything; the next tick starts it.
            escaped = self._escape_deadlock(holds, detail)
        elif not holds or status == "idle":
            # Not stuck: forget the clock, so a later deadlock is timed from
            # when it actually began.  Written only when there is something to
            # forget, so a healthy lane does not rewrite a file every tick.
            record = self._read_escapes()
            if record.get("stuck_since"):
                self._write_escapes({"stuck_since": None,
                                     "escapes": record.get("escapes") or {}})
        if escaped is not None:
            return {
                "status": "recovered", "reason": escaped["rationale"],
                "escaped": escaped, "held": len(holds), "holds": detail,
                "last": settled,
            }
        needs_owner = [item for item in detail if item["needs_owner_authorization"]]
        # G2: the lane has nothing it may start.  That is exactly the moment to
        # say what the plan thinks is worth doing next out of material already
        # held -- read-only, because creating the work needs an authority chain
        # this lane does not have.
        try:
            next_steps = self._plan_next_steps()
        except Exception:  # noqa: BLE001 - a suggestion may never fail a tick
            next_steps = []
        reason = "no unstarted document research admission"
        if needs_owner:
            # The reason a person reads.  It used to be the fixed sentence
            # above, which told nobody that the lane could only ever be
            # unblocked by hand.
            reason = (
                f"{len(needs_owner)} 条文档研究 admission 停在待恢复状态，"
                f"最早的一条的原因是「{needs_owner[0]['reason']}」。"
                + (needs_owner[0]["owner_action"] or OWNER_AUTHORIZATION_NOTE)
            )
        return {
            "status": status,
            "reason": reason,
            "held": len(holds),
            "holds": detail,
            "waiting_on_owner": len(needs_owner),
            "plan_next_steps": next_steps,
            "last": settled,
        }


def dispatch(server: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionDocumentResearchCoordinator(
            store=server.store, launcher=launcher
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--mission-document-research-lane", type=Path, default=None,
        help="Enable admission-ref-only document research execution from this closed config.",
    )


def lane_configuration(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionDocumentResearchLaneError(
            "mission document research lane config cannot be read"
        ) from exc
    if value == {"schema_version": "0.1", "enabled": True}:
        return dict(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "enabled", "directed_admission"}
        or value.get("schema_version") != "0.2"
        or value.get("enabled") is not True
        or not isinstance(value.get("directed_admission"), dict)
        or set(value["directed_admission"]) - {
            "max_admissions_per_tick", "task_budget",
        }
    ):
        raise MissionDocumentResearchLaneError(
            "mission document research lane config has an invalid closed shape"
        )
    from .call_budget import default_run_budget
    from .research_task import ResearchTaskError, validate_task_budget

    controls = value["directed_admission"]
    requested = controls.get(
        "max_admissions_per_tick",
        default_run_budget("research_task")["max_admissions_per_tick"],
    )
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise MissionDocumentResearchLaneError(
            "directed_admission.max_admissions_per_tick must be a positive integer"
        )
    try:
        validate_task_budget(controls.get("task_budget", {}))
    except ResearchTaskError as exc:
        raise MissionDocumentResearchLaneError(
            "directed_admission.task_budget is invalid"
        ) from exc
    return dict(value)


def directed_admission_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the document lane's producer bounds without enabling ad-hoc work."""

    from .call_budget import default_run_budget
    from .research_task import validate_task_budget

    controls = value.get("directed_admission", {})
    return {
        "max_admissions_per_tick": controls.get(
            "max_admissions_per_tick",
            default_run_budget("research_task")["max_admissions_per_tick"],
        ),
        "retired_templates": (),
        "task_budget": validate_task_budget(controls.get("task_budget", {})),
    }


def build_launcher(args: Any) -> Any | None:
    path = getattr(args, "mission_document_research_lane", None)
    if path is None:
        return None
    lane_configuration(path)
    required = {
        "candidate staging": getattr(args, "candidate_staging", None),
        "planner Scheduler": getattr(args, "scheduler", None),
        "research planner model config": getattr(
            args, "research_planner_model_config", None
        ),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise MissionDocumentResearchLaneError(
            "mission document research lane requires " + ", ".join(missing)
        )
    from .mission_document_research_launcher import MissionDocumentResearchLauncher

    state_dir = Path(args.db).expanduser().resolve().parent
    return MissionDocumentResearchLauncher(
        state_dir=state_dir,
        staging_path=required["candidate staging"],
        planner_scheduler_db=required["planner Scheduler"],
        planner_model_config_path=required["research planner model config"],
        document_config_path=state_dir / "document-research-config.json",
    )


def argv_fragment(context: Any) -> list[str]:
    path = context.state / LANE_CONFIG
    return [] if not path.is_file() else ["--mission-document-research-lane", str(path)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_document_research",
    order=152,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="Execute exact source-neutral directed-document admissions out of process; "
         "only typed Scheduler recovery can re-enter an orphaned or failed child.",
))


__all__ = [
    "CONTRACT_ESCALATION_NOTE",
    "DEADLOCK_ESCAPE_AFTER",
    "ESCAPES_FILE",
    "MAX_ESCAPES_PER_ADMISSION",
    "OWNER_AUTHORIZATION_NOTE",
    "DRIVER_KEY", "LANE", "LANE_CONFIG", "LAUNCHER_KWARG",
    "MissionDocumentResearchCoordinator", "MissionDocumentResearchLaneError",
    "add_arguments", "argv_fragment", "build_launcher", "lane_configuration",
]
