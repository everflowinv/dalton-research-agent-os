"""Writer lane for already-admitted directed document research.

The planner/research-task producer writes the immutable admission.  This lane
only selects an unstarted admission and launches its admission-ref-only child.
An orphaned or failed child is re-entered only from persisted Scheduler and
typed recovery authority.  Timed safe recovery does not block later independent
admissions; an unproved send state buys the executor's one bounded automatic
retry and only escalates to a person when that retry fails as well.
"""

from __future__ import annotations

import hashlib
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
# The lane's memory of whose turn the one child slot is, and where a capped
# recovery sweep stopped.  Advisory: an unreadable file means "no memory",
# which is the pre-existing order (recoveries first, oldest first).
TURN_FILE = "dispatch-turn.json"
# How many held admissions one tick re-classifies (reads their typed recovery
# state and rewrites their hold).  Every one of them is read-only bookkeeping;
# the cap only bounds a tick's latency when the hold ledger is long.  When it
# is hit the next tick starts after the last admission this one looked at, so
# every hold is reached.  Live on 2026-09-24 the ledgers held 20 (legacy) and
# 6 (ws-7d) admissions, so one tick covers all of them.
MAX_RECOVERY_EVALUATIONS_PER_TICK = 25
# Prefix of the hold reason written for an admission whose own evaluation
# raised.  The tick goes on for every other admission.
DISPATCH_ERROR_REASON = "dispatch_error"
# The hold reason for a controlled re-entry that failed again *after* the lane
# had already rebound this admission onto a new ticket identity by itself.
# Automatic once, then a person -- the same rule the two retry doors follow.
REENTRY_ESCALATED_REASON = "reentry_failed_after_automatic_rebind"
# Research feedback outcomes that settle an admission for good.
TERMINAL_RESEARCH_OUTCOMES = frozenset({"query_miss", "no_verified_claim"})
MODEL_AUTHORITY_PREEXECUTION_ERROR = (
    "MissionDocumentResearchError: mission document admission is no longer executable"
)
# The executor's own stage WorkOrder identities, and the stage each recovery
# chain ordinal belongs to.  Only these two stages ever have recovery links.
STAGE_WORK_ORDER_PREFIX = "work:mission-document-research-"
STAGE_NAMES = {
    2: "qualitative_model_draft",
    3: "independent_qualitative_verifier",
}
# The hold reason for an admission whose recovery chain this lane cannot read.
# It is a person's errand -- nothing automatic may resume a chain it cannot
# verify -- but it stops with that one admission.
HINT_DRIFT_HOLD_REASON = "recovery_hint_unverifiable"

# How long every admission may be held, with none startable, before the lane
# reopens the oldest one that provably never sent anything.  Live on
# 2026-09-16 this state had lasted 557 consecutive ticks -- about two days --
# with ten held admissions, zero promotions and zero outcomes.
DEADLOCK_ESCAPE_AFTER = timedelta(hours=6)
# How many times one admission may be reopened this way, ever.  A second
# attempt is worth making; a third is a loop.
MAX_ESCAPES_PER_ADMISSION = 2
# The exact words the owner needs, for the admissions no automation may touch.
# Since the bounded unproved-send retry exists, this is no longer the sentence
# for "送达状态不明" -- that class is now retried once by the lane itself, and
# only reaches a person through the escalation note below.  What is left here
# is everything neither automatic door covers: a started admission whose hold
# reason is not a model-recovery reason at all.
OWNER_AUTHORIZATION_NOTE = (
    "这条 admission 已经启动过，但它停住的原因不属于系统可以自动重试的那两类，"
    "因此不会自动重开。"
    "（系统会自动重试一次的只有两种：一是「已经证明送达并结算、仅仅是回复不符合输出"
    "契约」，二是「送达/计费状态无法证明」；这一条都不是。）"
    "需要 owner 授权一次受控恢复："
    "`python -m dalton_core.document_recovery_cli holds --state-dir <state>` "
    "先看它到底停在什么原因上；属于下面两类之一时才有对应的门可以按。"
)
# The words for the unproved send that the lane has already retried once by
# itself.  The ask must say the retry happened, and must say the worst case:
# the earlier call may also have been charged.
UNPROVED_SEND_ESCALATION_NOTE = (
    "这条的模型调用是否真的送达/计费无法证明；系统已经按上限自动重试过一次"
    "（同一个 WorkOrder 的 budget.max_cost_usd 上限、只放一条新 WorkOrder、"
    "授权记录的 actor_ref 是 \"automation:document-research-unproved-send-retry\"），"
    "重试出来的这一次仍然失败，所以才升级给人。"
    "最坏情况：先前那次无法证明的调用其实已经送达并计费，加上这次自动重试，"
    "这个阶段最多已经花掉两次调用——所以系统不会再自动买第三次。"
    "先看模型/线路为什么连续两次都拿不回可用结果；确认值得再买一次时，"
    "由 owner 授权最后一次受控恢复，执行："
    "`python -m dalton_core.document_recovery_cli authorize-unproved "
    "--state-dir <state> --admission-ref <ref> --max-cost-usd <上限美元> "
    "--apply --actor human:<owner>`"
    "（不加 --apply 是只读预览；--max-cost-usd 是愿意花的上限，"
    "那一阶段自己的预算超过它就直接拒绝。授权记录由 writer 按执行器要求的闭合格式生成，"
    "actor_ref 是 \"operator:owner-authorized-document-recovery\"、只放一条新 WorkOrder、"
    "max_cost_usd 等于那条失败 WorkOrder 自己的 budget.max_cost_usd。）"
    "（如果那条重试失败的原因是「已证明送达并结算、只是回复不合契约」，"
    "则改用 authorize-paid，车道给出的原因里会写明是哪一种。）"
)
# The words for an admission whose persisted recovery chain this lane cannot
# read back.  Nothing automatic may resume a chain it cannot verify, but the
# ask has to say that this is one admission's bookkeeping problem and that the
# rest of the lane keeps running.
HINT_DRIFT_ESCALATION_NOTE = (
    "这条 admission 的恢复链（mission_document_research_recovery_links）"
    "对不上车道能自己重算出来的阶段 WorkOrder 身份，因此车道不敢把它当作重启提示使用——"
    "无法核验的恢复链一律不自动恢复。"
    "只有这一条被挂起，同一批其它 admission 的派发不受影响。"
    "先看是哪一条链记录对不上："
    "`python -m dalton_core.document_recovery_cli holds --state-dir <state>`，"
    "车道原因里带有那条 recovery_link_ref；"
    "确认那条链确实是这条 admission 自己的（admission_ref/admission_hash、"
    "失败 WorkOrder 的 metadata 指向同一条 admission 与同一个阶段）之后，"
    "再由 owner 走受控恢复授权重开这一条。"
)
# The words for a controlled re-entry that failed again after the lane already
# rebound the admission onto a new ticket identity by itself.
REENTRY_ESCALATION_NOTE = (
    "这条 admission 的子进程票据身份变过（通常是发布或配置换了），系统已经自动把它"
    "改绑到新票据并重新进入过一次，但重新进入又失败了，所以才升级给人。"
    "先看车道原因里那条 LaneChildRejected 原文说的是什么（配置文件缺失、"
    "工作区绑定不对、票据被占用等），修好之后这条会自己继续。"
    "确实需要人重开时，才用 owner 的受控恢复授权。"
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
    "确认值得再买一次时，由 owner 授权最后一次受控恢复，执行："
    "`python -m dalton_core.document_recovery_cli authorize-paid "
    "--state-dir <state> --admission-ref <ref> --max-cost-usd <上限美元> "
    "--apply --actor human:<owner>`"
    "（不加 --apply 是只读预览；--max-cost-usd 是愿意花的上限，"
    "那一阶段自己的预算超过它就直接拒绝，不会偷偷少买。）"
)

# The words for a paid call the adapter refused because the provider's own
# token telemetry exceeded the WorkOrder budget.  Nothing is retried on the
# same route automatically: the identical WorkOrder would route to the same
# model and pay for the same refusal again.
PROVIDER_BUDGET_ESCALATION_NOTE = (
    "这条的模型调用已经送达并计费，但供应商回报的 token 用量超过了 WorkOrder 冻结的"
    "预算（PROVIDER_BUDGET_EXCEEDED），所以结果被拒。同一条路由再试一次只会同样超出、"
    "再付一次钱，因此系统不做自动重试，直接停下来等人。"
    "先确认已部署的版本把 CLI 网关的固定开销算进了预算（否则再买一次还是同样结果）；"
    "确认值得再买一次时，由 owner 授权最后一次受控恢复，执行："
    "`python -m dalton_core.document_recovery_cli authorize-unproved "
    "--state-dir <state> --admission-ref <ref> --max-cost-usd <上限美元> "
    "--apply --actor human:<owner>`"
    "（不加 --apply 是只读预览；--max-cost-usd 是愿意花的上限。）"
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


class MissionDocumentResearchHintDrift(MissionDocumentResearchLaneError):
    """One admission's recovery chain cannot be read as relaunch hints.

    Deliberately its own class: a chain the lane cannot verify says nothing
    about the other admissions, and on 2026-09-23 raising the generic lane
    error for it took the whole document-research lane down for 143 of 143
    ticks over two hours.  Every caller inside :meth:`dispatch_once` catches
    this and holds exactly the one admission it names.
    """

    def __init__(
        self, message: str, *, admission_ref: str, recovery_link_ref: Any = None,
    ) -> None:
        super().__init__(message)
        self.admission_ref = admission_ref
        self.recovery_link_ref = (
            recovery_link_ref if isinstance(recovery_link_ref, str) else None
        )


def _hint_drift_recovery(
    exc: "MissionDocumentResearchHintDrift",
) -> dict[str, Any]:
    """Turn an unreadable recovery chain into one admission's hold reason."""

    recovery: dict[str, Any] = {
        "action": "recovery_required",
        "reason": HINT_DRIFT_HOLD_REASON,
        "detail": str(exc),
    }
    if exc.recovery_link_ref is not None:
        recovery["recovery_link_ref"] = exc.recovery_link_ref
    return recovery


class _StoredWorkReader:
    """Read-only Scheduler Work lookups over the lane's own connection.

    Just enough of the Scheduler for the executor's stored-rebind reader,
    without opening a Scheduler (which would migrate and write) in the lane.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def work_order_authority(self, work_order_id: Any) -> dict[str, Any] | None:
        if not isinstance(work_order_id, str) or not work_order_id:
            return None
        row = self.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?", (work_order_id,),
        ).fetchone()
        if row is None:
            return None
        from .contracts import WorkOrder

        work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
        if (canonical_json(work) != row["work_order_json"]
                or content_hash(work) != row["work_order_hash"]):
            raise ValueError("stored WorkOrder authority drifted")
        return {"work_order": work, "work_order_hash": row["work_order_hash"]}


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
        say what the person is being asked to do -- and, when the lane has
        already tried something itself, what that was.
        """

        from .mission_document_research_executor import (
            CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
            PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED,
            UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY,
        )

        escalation_notes = {
            CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY: CONTRACT_ESCALATION_NOTE,
            UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY: UNPROVED_SEND_ESCALATION_NOTE,
            PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED: PROVIDER_BUDGET_ESCALATION_NOTE,
            HINT_DRIFT_HOLD_REASON: HINT_DRIFT_ESCALATION_NOTE,
        }

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
                    (REENTRY_ESCALATION_NOTE
                     if held["reason"].startswith(REENTRY_ESCALATED_REASON)
                     else escalation_notes.get(
                         held["reason"], OWNER_AUTHORIZATION_NOTE))
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
        """The one terminal ticket a ``ticket_ref: None`` hold re-enters from.

        An admission that has been re-entered (or rebound after a release
        renamed its ticket identity) owns several terminal tickets; live on
        2026-09-24 ``...ca9bac39`` owned four -- two succeeded, two failed,
        09-12 to 09-22 -- and raising here stopped the whole lane every tick.
        Several owned tickets are the expected history, not a conflict, so the
        lane picks one by a fixed rule:

        1. the newest ticket whose child exited cleanly (``succeeded``): its
           summary is complete and written by the child itself, which is what
           the launcher archives and checks before a controlled re-entry;
        2. otherwise the newest terminal ticket of any status.

        "Newest" is ``started_at`` then ``completed_at``, ties broken by the
        ticket ref.  The rule must be deterministic, not just reasonable: the
        re-entry authorization names the prior ticket, and the launcher's
        one-shot claim ledger is keyed on it, so a choice that could differ
        between ticks would hand the same admission one fresh paid attempt per
        ticket it owns.  Tickets of a different admission hash are never
        candidates.
        """

        matches: list[tuple[bool, str, str, str]] = []
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
                matches.append((
                    ticket.get("status") == "succeeded",
                    str(ticket.get("started_at") or ""),
                    str(ticket.get("completed_at") or ""),
                    ticket_ref,
                ))
        if not matches:
            return None
        return max(matches)[3]

    def _read_turn(self) -> dict[str, Any]:
        empty = {"schema_version": "0.1", "last_spawn": None,
                 "recovery_cursor": None}
        path = self.launcher.tickets_dir / TURN_FILE
        if not path.is_file():
            return empty
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(value, Mapping):
            return empty
        last = value.get("last_spawn")
        cursor = value.get("recovery_cursor")
        return {
            "schema_version": "0.1",
            "last_spawn": last if last in {"recovery", "fresh"} else None,
            "recovery_cursor": cursor if isinstance(cursor, str) else None,
        }

    def _write_turn(self, previous: Mapping[str, Any], **changes: Any) -> None:
        record = {**previous, **changes, "schema_version": "0.1"}
        if record != dict(previous):
            write_owner_only(self.launcher.tickets_dir / TURN_FILE, record)

    def _isolate_admission_error(
        self, holds: dict[str, dict[str, Any]], admission: Mapping[str, Any],
        exc: BaseException, errors: list[dict[str, Any]],
    ) -> None:
        """One admission's failure is that admission's hold, not the tick's.

        An admission with no hold yet is held ``recovery_required`` under a
        ``dispatch_error:`` reason, which the recovery sweep evaluates again
        next tick.  An admission already held keeps its hold untouched: its
        reason is what the owner's doors key on, and an escalation must not be
        downgraded into a generic error.  Either way the error is in the tick's
        result.  Integrity failures of the lane's own ledgers (hold, latest
        pointer, admission hash drift) are raised before this is reached.
        """

        reason = f"{DISPATCH_ERROR_REASON}:{type(exc).__name__}:{exc}"[:300]
        errors.append({"admission_ref": admission["id"], "error": reason})
        if admission["id"] not in holds:
            self._hold(
                holds, admission, reason=reason, ticket_ref=None,
                disposition="recovery_required",
            )

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
            has_rejections = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='mission_document_research_candidate_rejections'"
            ).fetchone() is not None
            joins = where = ""
            if has_outcomes:
                joins = (
                    "LEFT JOIN mission_document_research_outcomes o "
                    "ON o.admission_ref=a.admission_id "
                )
                pending = "o.outcome_id IS NULL"
                if promotion_required:
                    joins += (
                        "LEFT JOIN mission_document_research_promotions p "
                        "ON p.admission_ref=a.admission_id "
                    )
                    pending = "o.outcome_id IS NULL OR p.promotion_id IS NULL"
                where = f"WHERE {pending} "
                if has_rejections:
                    # An admission whose one candidate the governance rule
                    # refused is settled: it has an outcome and never will
                    # have a promotion, and re-dispatching it would only buy
                    # the same refusal a second time.
                    joins += (
                        "LEFT JOIN mission_document_research_candidate_rejections r "
                        "ON r.admission_ref=a.admission_id "
                    )
                    where = f"WHERE ({pending}) AND r.rejection_id IS NULL "
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

    def _preexecution_model_authority_revalidation(
        self, admission: Mapping[str, Any], held: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Name the one terminal hold that is safe to re-enter without spend.

        This is intentionally narrower than the ordinary deadlock escape.  It
        recognizes the sealed child failure written when current model
        authority invalidated an admission before the executor created any
        model Work.  The existing controlled-reentry claim remains the audit
        and replay bound; this method grants no paid or unproved-send retry.
        """

        if (held.get("disposition") != "terminal_hold"
                or held.get("reason") != "failed"
                or self._started(admission["id"])):
            return None
        ticket_ref = held.get("ticket_ref")
        if not isinstance(ticket_ref, str):
            return None
        try:
            ticket = self.launcher.status(ticket_ref)
        except (LookupError, LaneChildRejected):
            return None
        summary = ticket.get("summary")
        if not isinstance(summary, Mapping):
            return None
        body = dict(summary)
        asserted = body.pop("content_hash", None)
        if (ticket.get("status") != "failed"
                or ticket.get("admission_ref") != admission["id"]
                or ticket.get("admission_hash") != admission["content_hash"]
                or set(summary) != {
                    "schema_version", "created_at", "admission_ref",
                    "admission_hash", "status", "outcomes", "error",
                    "content_hash",
                }
                or summary.get("schema_version") != "0.1"
                or summary.get("admission_ref") != admission["id"]
                or summary.get("admission_hash") != admission["content_hash"]
                or summary.get("status") != "failed"
                or summary.get("outcomes") != []
                or summary.get("error") != MODEL_AUTHORITY_PREEXECUTION_ERROR
                or asserted != content_hash(body)):
            return None
        try:
            model_work = self.store.connection.execute(
                "SELECT 1 FROM scheduler_work_orders "
                "WHERE json_extract(work_order_json, "
                "'$.metadata.mission_document_research_admission_ref')=? "
                "AND json_extract(work_order_json, '$.metadata.stage') IN (?,?) "
                "LIMIT 1",
                (admission["id"], "qualitative_model_draft",
                 "independent_qualitative_verifier"),
            ).fetchone()
        except sqlite3.Error:
            return None
        if model_work is not None:
            return None
        summary_path = (
            self.launcher.tickets_dir / ticket_ref.split(":", 1)[1] / "summary.json"
        )
        try:
            if summary_path.is_symlink():
                return None
            summary_bytes = summary_path.read_bytes()
            if json.loads(summary_bytes) != summary:
                return None
        except (OSError, ValueError, TypeError):
            return None
        recovery = {
            "action": "resume",
            "reason": "infrastructure_model_authority_revalidation",
            "work_order_ref": None,
            "expected_summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        }
        consumed = getattr(self.launcher, "controlled_reentry_consumed", None)
        if consumed is not None:
            try:
                if consumed(
                    ticket_ref,
                    self._reentry_authorization(admission, ticket_ref, recovery),
                ):
                    return None
            except Exception:  # noqa: BLE001 - unreadable audit fails closed
                return None
        return recovery

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
                raise MissionDocumentResearchHintDrift(
                    "document research recovery hint is invalid",
                    admission_ref=admission["id"],
                    recovery_link_ref=row["recovery_link_id"],
                ) from exc
            body = dict(wire) if isinstance(wire, Mapping) else {}
            asserted = body.pop("content_hash", None)
            stage = wire.get("stage_ordinal") if isinstance(wire, Mapping) else None
            sealed_root = (
                isinstance(wire, Mapping)
                and stage in {2, 3}
                and wire.get("recovery_number") == 1
                and row["recovery_number"] == 1
                and wire.get("failed_work_order_ref")
                == row["failed_work_order_ref"]
                and self._sealed_stage_work(
                    admission, stage,
                    work_ref=wire.get("failed_work_order_ref"),
                    work_hash=wire.get("failed_work_order_hash"),
                )
            )
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
                or (not sealed_root
                    and (wire.get("failed_work_order_ref") != refs[stage - 1]
                         or row["failed_work_order_ref"] != refs[stage - 1]))
                or wire.get("recovery_work_order_ref")
                != row["recovery_work_order_ref"]
            ):
                raise MissionDocumentResearchHintDrift(
                    "document research recovery hint drifted",
                    admission_ref=admission["id"],
                    recovery_link_ref=row["recovery_link_id"],
                )
            refs[stage - 1] = wire["recovery_work_order_ref"]
            rebound = self._epoch_rebound_hint(admission, stage, wire)
            if rebound is not None:
                refs[stage - 1] = rebound
            next_number[stage] += 1
        return refs

    def _epoch_rebound_hint(
        self, admission: Mapping[str, Any], stage: int,
        link: Mapping[str, Any],
    ) -> str | None:
        """The Work that actually ran a recovery a model-authority roll renamed.

        When a recovery was authorized under one model authority and the
        authority rolled before it ran, the executor never claims the
        authorized recovery Work: it records a model-authority epoch rebind
        and runs the rebound Work instead, and any later link of the stage is
        rooted at that rebound Work.  The authorized Work stays ``ready``
        forever.  Watching it made every such admission look resumable, so a
        refused re-entry was reported as ``reentry_failed_after_automatic_rebind``
        and the owner was sent to the wrong door (live legacy 6a2bcd, a9e588b0
        and 918307dc on 2026-09-25, whose rebound Work had in fact failed
        twice).  The mapping is verified by the executor's own stored-rebind
        reader, so the lane follows exactly the Work the executor would.
        """

        try:
            row = self.store.connection.execute(
                "SELECT rebind_id,authorized_recovery_work_ref,"
                "rebound_work_order_ref FROM "
                "mission_document_research_model_authority_epoch_rebinds "
                "WHERE recovery_link_ref=?", (link["id"],),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        if row is None:
            return None
        from .mission_document_research_executor import (
            MissionDocumentResearchExecutorError,
            _read_stored_epoch_rebind,
        )

        def drift(exc: BaseException | None = None) -> None:
            raise MissionDocumentResearchHintDrift(
                "document research model authority rebind hint drifted",
                admission_ref=admission["id"],
                recovery_link_ref=link.get("id"),
            ) from exc

        rebound_ref = row["rebound_work_order_ref"]
        if row["authorized_recovery_work_ref"] != link.get(
                "recovery_work_order_ref"):
            drift()
        stored = _StoredWorkReader(self.store.connection).work_order_authority(
            rebound_ref)
        if stored is None:
            # Recorded but not yet enqueued: the executor re-derives and
            # enqueues it on its next run, which the ready authorized Work
            # already sends it to.
            return None
        work = stored["work_order"]
        metadata = work.get("metadata") if isinstance(work, Mapping) else None
        if (not isinstance(metadata, Mapping)
                or work.get("id") != rebound_ref
                or metadata.get("mission_document_research_admission_ref")
                != admission["id"]
                or metadata.get("mission_document_research_admission_hash")
                != admission["content_hash"]
                or metadata.get("stage") != STAGE_NAMES.get(stage)):
            drift()
        try:
            record = _read_stored_epoch_rebind(
                self.store.connection, _StoredWorkReader(self.store.connection),
                work,
            )
        except (MissionDocumentResearchExecutorError, ValueError,
                TypeError) as exc:
            drift(exc)
        if (record.get("id") != row["rebind_id"]
                or record.get("recovery_link_ref") != link.get("id")
                or record.get("stage_ordinal") != stage):
            drift()
        return rebound_ref

    def _sealed_stage_work(
        self,
        admission: Mapping[str, Any],
        stage_ordinal: int,
        *,
        work_ref: Any,
        work_hash: Any,
    ) -> bool:
        """Is this the executor's own Work for this admission at this stage?

        The lane cannot always re-derive a stage WorkOrder identity from the
        admission alone: the executor mixes a model-authority refresh hash into
        the identity of stages 2..4 when current model authority differs from
        the admitted one, and that refresh is a live input rather than a field
        of the immutable admission.  Re-deriving is therefore only a guess, and
        a wrong guess used to take the whole lane down.

        The chain root is accepted instead from persisted Scheduler authority:
        the named Work must exist, be content-sealed to exactly the hash the
        recovery link itself asserts, and carry this admission's ref and hash
        with this stage.  That is the same binding the ordinary execution read
        enforces, so no audited guarantee is traded away for the tolerance.
        """

        if (not isinstance(work_ref, str)
                or not work_ref.startswith(STAGE_WORK_ORDER_PREFIX)
                or not _sha256(work_hash)
                or stage_ordinal not in STAGE_NAMES):
            return False
        try:
            row = self.store.connection.execute(
                "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
                "WHERE work_order_id=?", (work_ref,),
            ).fetchone()
        except sqlite3.Error:
            return False
        if row is None or row["work_order_hash"] != work_hash:
            return False
        try:
            work = json.loads(row["work_order_json"])
        except (TypeError, ValueError, RecursionError):
            return False
        if (not isinstance(work, Mapping)
                or canonical_json(work) != row["work_order_json"]
                or content_hash(work) != work_hash
                or work.get("id") != work_ref):
            return False
        metadata = work.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        return (
            metadata.get("mission_document_research_admission_ref")
            == admission["id"]
            and metadata.get("mission_document_research_admission_hash")
            == admission["content_hash"]
            and metadata.get("stage") == STAGE_NAMES[stage_ordinal]
        )

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
        from .mission_document_research_executor import (
            AUTOMATIC_UNPROVED_SEND_RETRY_REASON,
            LEGACY_PAID_CONTRACT_REASON,
            LEGACY_UNPROVED_SEND_REASON,
        )

        legacy_retry_available = {
            # Releases before the bounded automatic contract retry existed
            # recorded a proved paid contract rejection as permanently stopped
            # and waited for a signature.
            LEGACY_PAID_CONTRACT_REASON: "automatic_contract_retry_available",
            # And every release before the bounded unproved-send retry existed
            # did the same for a failure whose send state nothing could prove.
            # Twenty live holds, seventeen of them waiting on a person who was
            # being asked the same question every tick.
            LEGACY_UNPROVED_SEND_REASON: "automatic_unproved_send_retry_available",
        }
        if (recovery["status"] == "stopped"
                and recovery.get("reason") in legacy_retry_available):
            # Such an admission has not spent its one automatic retry, so the
            # child may re-enter: the executor revalidates the whole failure
            # state before it can enqueue anything, and then records either the
            # retry or the daily cap.  Any newer row for this same Work
            # supersedes the legacy verdict.
            newer = next((by_status[key] for key in ("waiting", "admitted")
                          if key in by_status), None)
            if newer is None:
                return {
                    "action": "resume",
                    "reason": legacy_retry_available[recovery["reason"]],
                    "work_order_ref": work_ref,
                }
            recovery = newer["recovery"]
        if recovery["status"] == "admitted":
            # A controlled retry was already admitted for this Work.  Re-enter:
            # the executor rebuilds the exact effective Work, so this also
            # repairs an interrupted link write.
            return {
                "action": "resume",
                "reason": (
                    "controlled_unproved_send_retry_admitted"
                    if recovery.get("reason") == AUTOMATIC_UNPROVED_SEND_RETRY_REASON
                    else "controlled_contract_retry_admitted"),
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

    @staticmethod
    def _reentry_authorization(
        admission: Mapping[str, Any], ticket_ref: str,
        recovery: Mapping[str, Any],
    ) -> str:
        return canonical_json({
            "schema_version": "0.1",
            "kind": "exact_scheduler_replay",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "prior_ticket_ref": ticket_ref,
            "work_order_ref": recovery.get("work_order_ref"),
            "reason": recovery["reason"],
        })

    def _reentry_hold_reason(
        self, admission: Mapping[str, Any], ticket_ref: str | None,
        recovery: Mapping[str, Any], exc: Exception,
    ) -> str:
        """Automatic once, then a person -- the rule the retry doors follow.

        A first refused re-entry is the lane's own business: the next tick may
        well succeed, and a ticket identity that moved is repaired without
        anyone being told.  A refusal after the lane has already spent that one
        attempt is a different fact, and it has to reach the owner saying so.
        """

        if ticket_ref is None:
            return "controlled_reentry_unavailable:" + str(exc)
        try:
            if self.launcher.status(ticket_ref).get(
                    "rebound_from_ticket_ref") is not None:
                return REENTRY_ESCALATED_REASON + ":" + str(exc)
        except Exception:  # noqa: BLE001 - an unreadable ticket asks a person
            return REENTRY_ESCALATED_REASON + ":" + str(exc)
        # "Claimed" is not "attempted".  The launcher writes its one-shot
        # marker before it starts the child, so a re-entry refused in between
        # leaves a spent marker and no run; escalating on that would hand a
        # person an admission the lane never actually retried, which is what
        # three live admissions did within an hour of the rebinding shipping.
        consumed = getattr(self.launcher, "controlled_reentry_consumed", None)
        try:
            attempted = bool(consumed(
                ticket_ref,
                self._reentry_authorization(admission, ticket_ref, recovery),
            )) if consumed is not None else False
        except Exception:  # noqa: BLE001 - no claim ledger, no escalation
            attempted = False
        if attempted:
            return REENTRY_ESCALATED_REASON + ":" + str(exc)
        return "controlled_reentry_unavailable:" + str(exc)

    def _resume(
        self, admission: Mapping[str, Any], *, ticket_ref: str,
        recovery: Mapping[str, Any], settled: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        authorization = self._reentry_authorization(admission, ticket_ref, recovery)
        resumed = self.launcher.resume(
            admission_ref=admission["id"],
            admission_hash=admission["content_hash"],
            prior_ticket_ref=ticket_ref,
            authorization=authorization,
            expected_summary_sha256=recovery.get("expected_summary_sha256"),
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
        result = {
            "status": "resumed", "ticket_ref": resumed["id"],
            "admission_ref": admission["id"], "last": settled,
        }
        rebound_from = resumed.get("rebound_from_ticket_ref")
        if rebound_from is not None:
            # Say it out loud in the tick the owner reads, not only in the
            # ticket file: the run that continues this admission is not the
            # ticket the hold named.
            result["rebound_from_ticket_ref"] = rebound_from
            result["reason"] = (
                f"子进程票据身份已变（{rebound_from} → {resumed['id']}），"
                "admission 本身没变，已自动改绑并重新进入。"
            )
        return result

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "document research lane is absent"}
        holds = _read_holds(self.holds_path)
        admissions = self._admissions()
        # A completed child records query_miss/no_verified_claim/candidate_staged
        # as an immutable observation.  Older dispatches could later recreate a
        # generic started_without_owned_live_ticket hold because those terminal
        # outcomes intentionally do not occupy the promoted-outcome table.
        #
        # Any validated terminal research feedback settles its admission.  It
        # used to take only the *latest* observation, but the feedback is
        # stamped with the admission's own ``created_at`` (its identity is
        # deterministic), so it always sorts before the recovery observations
        # of any admission that needed a recovery first.  Live on 2026-09-25,
        # ca9bac39 completed ``no_verified_claim`` at 06:29 and stayed in the
        # hold ledger as ``reentry_failed_after_automatic_rebind`` behind its
        # 2026-09-11 recovery observations.  The executor writes feedback only
        # once every model stage has succeeded, and nothing re-enters an
        # admission after it, so no recovery can follow it.
        from .mission_document_research_executor import (
            read_mission_document_research_candidate_rejections,
            read_mission_document_research_observations,
        )
        completed_refs = {
            observation["admission_ref"]
            for observation in read_mission_document_research_observations(
                self.store.connection
            )
            if observation["outcome"] in TERMINAL_RESEARCH_OUTCOMES
        }
        # A candidate the governance rule refused settles its admission the
        # same way: the child completed, the refusal is written down, and a
        # hold left over from the older failing runs has nothing left to wait
        # for.
        completed_refs |= {
            rejection["admission_ref"]
            for rejection in read_mission_document_research_candidate_rejections(
                self.store.connection
            )
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
        errors: list[dict[str, Any]] = []
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
                try:
                    recovery = (
                        self._execution_state(admission)
                        if self._started(admission["id"]) else {
                            "action": "terminal_hold",
                            "reason": child_status or ticket["status"],
                        }
                    )
                except MissionDocumentResearchHintDrift as exc:
                    # This admission's chain is a person's errand; the tick
                    # continues for every other admission below.
                    recovery = _hint_drift_recovery(exc)
                except Exception as exc:  # noqa: BLE001 - isolated to this admission
                    reason = f"{DISPATCH_ERROR_REASON}:{type(exc).__name__}:{exc}"[:300]
                    errors.append({"admission_ref": admission["id"], "error": reason})
                    recovery = {"action": "recovery_required", "reason": reason}
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
                    except Exception as exc:  # noqa: BLE001 - held, tick goes on
                        reason = (
                            self._reentry_hold_reason(
                                admission, ticket["id"], recovery, exc)
                            if isinstance(exc, LaneChildRejected) else
                            f"{DISPATCH_ERROR_REASON}:{type(exc).__name__}:{exc}"[:300]
                        )
                        self._hold(
                            holds, admission, reason=reason,
                            ticket_ref=ticket["id"],
                            disposition="recovery_required",
                        )
                        settled["recovery"] = {
                            "action": "recovery_required", "reason": reason,
                        }
                        recovery = settled["recovery"]
                    else:
                        if holds.pop(admission["id"], None) is not None:
                            _write_holds(self.holds_path, holds)
                        self._write_turn(self._read_turn(), last_spawn="recovery")
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
        turn = self._read_turn()
        # The launcher runs one child at a time (a second spawn is a
        # LaneChildConflict), so a tick can start at most one run.  What a
        # tick can do for many admissions is everything short of the spawn:
        # re-classify every held admission, and line up every one that is
        # ready to re-enter.  Only then is the one slot handed out -- and it
        # alternates between a recovery and a fresh admission whenever both
        # are waiting, so twenty owner-authorized recoveries no longer keep
        # every new admission out for twenty runs.
        recoveries: list[dict[str, Any]] = []

        # A model-policy/profile outage can fail the child before it writes a
        # start or any model Work.  Re-enter such an exact sealed failure
        # through the launcher's existing one-shot claim ledger.  No paid
        # recovery authority is created, and every broader terminal hold
        # continues through the ordinary hold path below.
        for admission in admissions:
            held = holds.get(admission["id"])
            if held is None:
                continue
            if held["admission_hash"] != admission["content_hash"]:
                raise MissionDocumentResearchLaneError(
                    "document research hold admission hash drifted"
                )
            try:
                recovery = self._preexecution_model_authority_revalidation(
                    admission, held
                )
            except Exception as exc:  # noqa: BLE001 - isolated to this admission
                self._isolate_admission_error(holds, admission, exc, errors)
                continue
            if recovery is None:
                continue
            recoveries.append({
                "admission": admission, "ticket_ref": held["ticket_ref"],
                "recovery": recovery, "held_ticket_ref": held["ticket_ref"],
                "rejected_disposition": "terminal_hold",
            })

        queued = {item["admission"]["id"] for item in recoveries}
        cursor = turn["recovery_cursor"]
        order = list(admissions)
        if cursor is not None:
            refs = [admission["id"] for admission in order]
            if cursor in refs:
                split = refs.index(cursor) + 1
                order = order[split:] + order[:split]
        evaluated = deferred = 0
        last_evaluated = None
        for admission in order:
            held = holds.get(admission["id"])
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
            if admission["id"] in queued:
                continue
            if evaluated >= MAX_RECOVERY_EVALUATIONS_PER_TICK:
                deferred += 1
                continue
            evaluated += 1
            last_evaluated = admission["id"]
            try:
                recovery = self._execution_state(admission)
            except MissionDocumentResearchHintDrift:
                # Held, not raised: the admissions after this one still get
                # their tick.
                self._hold(
                    holds, admission, reason=HINT_DRIFT_HOLD_REASON,
                    ticket_ref=held["ticket_ref"], disposition="recovery_required",
                )
                continue
            except Exception as exc:  # noqa: BLE001 - isolated to this admission
                self._isolate_admission_error(holds, admission, exc, errors)
                continue
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
            try:
                ticket_ref = held["ticket_ref"] or self._owned_terminal_ticket_ref(
                    admission
                )
            except Exception as exc:  # noqa: BLE001 - isolated to this admission
                self._isolate_admission_error(holds, admission, exc, errors)
                continue
            if ticket_ref is None:
                self._hold(
                    holds, admission, reason="controlled_reentry_ticket_unavailable",
                    ticket_ref=None, disposition="recovery_required",
                )
                continue
            recoveries.append({
                "admission": admission, "ticket_ref": ticket_ref,
                "recovery": recovery, "held_ticket_ref": held["ticket_ref"],
                "rejected_disposition": "recovery_required",
            })
        next_cursor = last_evaluated if deferred else None

        fresh: list[Mapping[str, Any]] = []
        for admission in admissions:
            held = holds.get(admission["id"])
            if held is not None:
                if held["admission_hash"] != admission["content_hash"]:
                    raise MissionDocumentResearchLaneError(
                        "document research hold admission hash drifted"
                    )
                continue
            try:
                started = self._started(admission["id"])
            except Exception as exc:  # noqa: BLE001 - isolated to this admission
                self._isolate_admission_error(holds, admission, exc, errors)
                continue
            if started:
                self._hold(
                    holds, admission,
                    reason="started_without_owned_live_ticket",
                    ticket_ref=None, disposition="recovery_required",
                )
                continue
            fresh.append(admission)

        def spawn_recovery() -> dict[str, Any] | None:
            for index, item in enumerate(recoveries):
                admission = item["admission"]
                try:
                    result = self._resume(
                        admission, ticket_ref=item["ticket_ref"],
                        recovery=item["recovery"], settled=settled,
                    )
                except LaneChildConflict as exc:
                    return {"status": "busy", "reason": str(exc), "last": settled}
                except LaneChildRejected as exc:
                    self._hold(
                        holds, admission,
                        reason=self._reentry_hold_reason(
                            admission, item["ticket_ref"], item["recovery"], exc),
                        ticket_ref=item["held_ticket_ref"],
                        disposition=item["rejected_disposition"],
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - isolated to this admission
                    self._isolate_admission_error(holds, admission, exc, errors)
                    continue
                holds.pop(admission["id"], None)
                _write_holds(self.holds_path, holds)
                self._write_turn(turn, last_spawn="recovery",
                                 recovery_cursor=next_cursor)
                result["recoveries_queued"] = len(recoveries) - index - 1
                return result
            return None

        def spawn_fresh() -> dict[str, Any] | None:
            for admission in fresh:
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
                except Exception as exc:  # noqa: BLE001 - isolated to this admission
                    self._isolate_admission_error(holds, admission, exc, errors)
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
                self._write_turn(turn, last_spawn="fresh",
                                 recovery_cursor=next_cursor)
                return {
                    "status": "launched", "ticket_ref": ticket["id"],
                    "admission_ref": admission["id"], "last": settled,
                    "recoveries_queued": len(recoveries),
                }
            return None

        # Recoveries go first unless the previous run this lane started was
        # itself a recovery and a fresh admission is waiting: then the fresh
        # one gets this slot and the recoveries the next.
        prefer_fresh = bool(fresh) and bool(recoveries) and (
            turn["last_spawn"] == "recovery"
        )
        for spawn in ((spawn_fresh, spawn_recovery) if prefer_fresh
                      else (spawn_recovery, spawn_fresh)):
            spawned = spawn()
            if spawned is not None:
                if errors:
                    spawned["admission_errors"] = errors
                return spawned
        self._write_turn(turn, recovery_cursor=next_cursor)
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
        result = {
            "status": status,
            "reason": reason,
            "held": len(holds),
            "holds": detail,
            "waiting_on_owner": len(needs_owner),
            "plan_next_steps": next_steps,
            "last": settled,
        }
        if errors:
            result["admission_errors"] = errors
        return result


def authorize_reentry(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Record one owner grant of one further controlled re-entry.

    The door the ``reentry_failed_after_automatic_rebind`` escalation leads
    to.  It exists because the escalation has to lead somewhere: before this,
    the owner was told the lane had given up and had nothing to press.  It
    grants exactly what the lane grants itself -- one re-entry -- and it runs
    in the writer, which is the process that owns this lane's ticket
    directory, reached through the owner's ephemeral human principal.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "document research lane is absent"}
    return launcher.authorize_controlled_reentry(
        params["admission_ref"], actor_ref=params["actor_ref"],
        granted_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    )


OWNER_RECOVERY_ACTOR = "operator:owner-authorized-document-recovery"
# Which model stage each escalated observation belongs to, in the ordinals the
# executor's authorization rows use (2 = draft, 3 = independent verifier).
STAGE_ORDINALS: Mapping[str, int] = {
    "qualitative_model_draft": 2,
    "independent_qualitative_verifier": 3,
}


def _recovery_doors() -> dict[str, dict[str, Any]]:
    from .mission_document_research_executor import (
        CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
        PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED,
        UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY,
    )

    return {
        "paid": {
            "reason": CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
            "reasons": (CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,),
            "prefix": "mission-document-paid-recovery-authorization",
            "method": "authorize_paid_contract_recovery",
            "cli": "authorize-paid",
        },
        "unproved": {
            "reason": UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY,
            # A provider-budget refusal was sent and charged like an unproved
            # send is feared to be, and this door buys exactly one more call.
            "reasons": (UNPROVED_SEND_FAILED_AFTER_AUTOMATIC_RETRY,
                        PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED),
            "prefix": "mission-document-unproved-send-recovery-authorization",
            "method": "authorize_unproved_send_recovery",
            "cli": "authorize-unproved",
        },
    }


def _escalated_stage_ordinal(
    connection: Any, admission: Mapping[str, Any], reason: str | Sequence[str],
) -> int | None:
    """The stage whose *second* failure is the one waiting on a person."""

    reasons = {reason} if isinstance(reason, str) else set(reason)
    from .mission_document_research_executor import (
        read_mission_document_research_observations,
    )

    matching = [
        item for item in read_mission_document_research_observations(
            connection, mission_version_ref=admission["mission_version_ref"])
        if item["admission_ref"] == admission["id"]
        and item["outcome"] == "recovery_required"
        and isinstance(item.get("recovery"), Mapping)
        and item["recovery"].get("reason") in reasons
    ]
    if not matching:
        return None
    return STAGE_ORDINALS.get(str(matching[-1].get("stage")))


def _existing_owner_authorization(
    executor: Any, admission_ref: str, stage_ordinal: int, prefix: str,
) -> dict[str, Any] | None:
    """Replay the owner row already written for this admission and stage.

    Every authorization id is a hash of its own body, instant included, so a
    second call that minted a fresh one would be a *different* authorization
    on a stage that already has the owner's -- refused, not idempotent.  It is
    keyed by (admission, stage) rather than by the failed Work because opening
    the door moves the effective Work forward: the replacement WorkOrder is
    the stage's Work from the next call onwards, and it has no row of its own.
    """

    rows = executor.connection.execute(
        "SELECT record_json FROM "
        "mission_document_research_controlled_recovery_authorizations "
        "WHERE admission_ref=? AND stage_ordinal=? ORDER BY created_at",
        (admission_ref, stage_ordinal),
    ).fetchall()
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(record, Mapping) and str(
                record.get("id", "")).startswith(prefix + ":"):
            return dict(record)
    return None


def authorize_owner_recovery(
    executor: Any, admission_ref: str, *, door: str, actor_ref: str,
    max_cost_usd: Any = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Open one of the two escalated recovery doors, by hand, exactly once.

    The lane retries a contract failure and an unproved send once each by
    itself and then stops.  Until this existed the escalation told the owner
    to call an executor method, in a process that must not open these
    databases -- an instruction nobody could follow.  This builds the exact
    authorization the executor demands (same closed shape, same hash rules,
    same ``operator:owner-authorized-document-recovery`` actor as the
    automatic doors write for themselves) and hands it to that same door, in
    the writer, which is the process that owns this state.

    ``max_cost_usd`` is a ceiling on what the owner is agreeing to spend, not
    the amount: the authorization must carry the failed WorkOrder's own
    ``budget.max_cost_usd``, so a stage that costs more than the cap is
    refused rather than quietly trimmed.
    """

    from decimal import Decimal

    from .mission_document_research_executor import (
        _effective_stage, _formal_hash, _formal_ref, _ref,
    )
    from .store import content_hash as _content_hash

    spec = _recovery_doors()[door]
    admission = executor.authority.resolve_for_execution(admission_ref)
    ordinal = _escalated_stage_ordinal(
        executor.authority.connection, admission, spec["reasons"])
    if ordinal is None:
        # Not a refusal of the owner: this admission is not in the state this
        # door opens, and authorising it would buy a call for a failure that
        # never happened.
        return {"status": "not_escalated", "admission_ref": admission_ref,
                "door": door, "reason": spec["reason"]}
    index = ordinal - 1
    worker = executor.draft_worker if index == 1 else executor.verifier_worker
    work, _links = _effective_stage(
        executor.authority, executor.scheduler, admission, index, worker=worker)
    ceiling = work["budget"]["max_cost_usd"]
    if max_cost_usd is not None and Decimal(str(ceiling)) > Decimal(str(max_cost_usd)):
        return {
            "status": "refused", "admission_ref": admission_ref, "door": door,
            "reason": "stage_budget_exceeds_authorized_cap",
            "stage_ordinal": ordinal, "max_cost_usd": ceiling,
            "authorized_cap_usd": max_cost_usd,
        }
    authorization = _existing_owner_authorization(
        executor, admission["id"], ordinal, spec["prefix"])
    if authorization is None:
        formal = executor.scheduler.formal_result(work["id"])
        if formal is None:
            return {"status": "refused", "admission_ref": admission_ref,
                    "door": door, "reason": "failed_stage_result_unavailable",
                    "stage_ordinal": ordinal}
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        body = {
            "schema_version": "0.1",
            "actor_ref": OWNER_RECOVERY_ACTOR,
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "stage_ordinal": ordinal,
            "failed_work_order_ref": work["id"],
            "failed_work_order_hash": _content_hash(work),
            "formal_result_ref": _formal_ref(formal),
            "formal_result_hash": _formal_hash(formal),
            "max_fresh_work_orders": 1,
            "max_cost_usd": ceiling,
            "authorized_at": moment.isoformat(timespec="microseconds"),
        }
        authorization = {**body, "id": _ref(spec["prefix"], body)}
        authorization["content_hash"] = _content_hash(authorization)
    result = getattr(executor, spec["method"])(admission["id"], authorization)
    return {
        "status": result.get("status", "admitted"),
        "admission_ref": admission["id"], "door": door,
        "stage_ordinal": ordinal,
        "authorization_ref": authorization["id"],
        "authorized_by": actor_ref,
        "work_order_ref": result.get("work_order_ref"),
        "model_calls": result.get("model_calls"),
        "max_cost_usd": ceiling,
    }


def _open_writer_executor(server: Any, admission_ref: str) -> Any:
    """Build this lane's executor inside the writer, out of the writer's state.

    The child process the lane normally launches assembles exactly this from
    exactly these files.  The owner's door has to reach the same executor
    without a second process opening the Ledger, the Scheduler and the staging
    store behind the writer's back, so it is assembled here instead, from the
    launcher's own configuration -- the same paths, proved the same way.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        raise MissionDocumentResearchLaneError("document research lane is absent")
    row = server.store.connection.execute(
        "SELECT content_hash FROM mission_document_research_admissions "
        "WHERE admission_id=?", (admission_ref,),
    ).fetchone()
    if row is None:
        raise MissionDocumentResearchLaneError(
            "mission document admission is unavailable")
    from .mission_document_research_runtime import MissionDocumentResearchRuntime

    return MissionDocumentResearchRuntime(
        state_dir=launcher.state_dir,
        staging_path=launcher.staging_path,
        planner_scheduler_db=launcher.planner_scheduler_db,
        planner_model_config_path=launcher.planner_model_config_path,
        document_config_path=launcher.document_config_path,
        draft_config_path=launcher.draft_model_config_path,
        verifier_config_path=launcher.verifier_model_config_path,
        admission_ref=admission_ref,
        expected_admission_hash=row["content_hash"],
    )


def _authorize_recovery(
    server: Any, params: Mapping[str, Any], *, door: str,
) -> dict[str, Any]:
    actor_ref = params["actor_ref"]
    if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
        raise MissionDocumentResearchLaneError(
            "a controlled recovery authorization needs a human actor")
    admission_ref = params["admission_ref"]
    if (not isinstance(admission_ref, str)
            or not admission_ref.startswith(
                "mission-document-research-admission:")):
        raise MissionDocumentResearchLaneError(
            "admission_ref is not a mission document research admission")
    runtime = _open_writer_executor(server, admission_ref)
    try:
        return authorize_owner_recovery(
            runtime.executor, admission_ref, door=door, actor_ref=actor_ref,
            max_cost_usd=params.get("max_cost_usd"),
        )
    finally:
        runtime.close()


def authorize_paid_recovery(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Owner door for a contract failure the lane has already retried once."""

    return _authorize_recovery(server, params, door="paid")


def authorize_unproved_recovery(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Owner door for an unproved send the lane has already retried once."""

    return _authorize_recovery(server, params, door="unproved")


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
    "OWNER_RECOVERY_ACTOR",
    "STAGE_ORDINALS",
    "authorize_owner_recovery",
    "authorize_paid_recovery",
    "authorize_reentry",
    "authorize_unproved_recovery",
    "DEADLOCK_ESCAPE_AFTER",
    "ESCAPES_FILE",
    "MAX_ESCAPES_PER_ADMISSION",
    "OWNER_AUTHORIZATION_NOTE",
    "REENTRY_ESCALATED_REASON",
    "REENTRY_ESCALATION_NOTE",
    "UNPROVED_SEND_ESCALATION_NOTE",
    "DRIVER_KEY", "LANE", "LANE_CONFIG", "LAUNCHER_KWARG",
    "MissionDocumentResearchCoordinator", "MissionDocumentResearchLaneError",
    "add_arguments", "argv_fragment", "build_launcher", "lane_configuration",
]
