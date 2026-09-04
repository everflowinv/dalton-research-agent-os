"""Policy-admitted, replayable weekly brief scheduling.

The controller supplies an immutable plan and a clock.  Core resolves the
latest due schedule, freezes the active governance policy in an append-only
cycle admission, publishes the exact WeeklyBriefIssue and enqueues its exact
Markdown artifact.  A crash between those writes is safe to replay.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agenda import AgendaStore
from .store import DaltonStore, content_hash
from .weekly_brief import (
    WeeklyBriefAuthority,
    WeeklyBriefNotFound,
)
from .writer_client import WriterClient


SCHEMA_VERSION = "0.1"
WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF = (
    "weekly-brief-auto-publish:scheduled-exact-plan:v1"
)


class WeeklyBriefCoordinatorError(RuntimeError):
    pass


class WeeklyBriefCoordinatorPrecondition(WeeklyBriefCoordinatorError):
    pass


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeeklyBriefCoordinatorError(f"{name} must be non-empty text")
    return value.strip()


def _absolute_path(value: Any, name: str) -> Path:
    value = _text(value, name)
    path = Path(value)
    if not path.is_absolute():
        raise WeeklyBriefCoordinatorError(f"{name} must be an absolute path")
    return path


def _instant(value: Any, name: str) -> datetime:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeeklyBriefCoordinatorError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise WeeklyBriefCoordinatorError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class WeeklyBriefSchedulePlan:
    plan_ref: str
    brief_ref: str
    timezone: str
    weekday: int
    hour: int
    minute: int
    effective_from: str
    evidence_pack_version_id: str
    company_overlay_version_ids: tuple[str, ...]
    company_thesis_refs: Mapping[str, str]
    destination_ref: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WeeklyBriefSchedulePlan":
        expected = {
            "schema_version", "plan_ref", "brief_ref", "timezone", "weekday",
            "hour", "minute", "effective_from", "evidence_pack_version_id",
            "company_overlay_version_ids", "company_thesis_refs",
            "destination_ref",
        }
        if set(raw) != expected or raw.get("schema_version") != SCHEMA_VERSION:
            raise WeeklyBriefCoordinatorError(
                "weekly brief schedule plan has an invalid closed shape"
            )
        weekday = raw["weekday"]
        hour = raw["hour"]
        minute = raw["minute"]
        if (
            isinstance(weekday, bool) or not isinstance(weekday, int)
            or not 0 <= weekday <= 6
        ):
            raise WeeklyBriefCoordinatorError("weekday must be an integer from 0 to 6")
        if (
            isinstance(hour, bool) or not isinstance(hour, int)
            or not 0 <= hour <= 23
        ):
            raise WeeklyBriefCoordinatorError("hour must be an integer from 0 to 23")
        if (
            isinstance(minute, bool) or not isinstance(minute, int)
            or not 0 <= minute <= 59
        ):
            raise WeeklyBriefCoordinatorError("minute must be an integer from 0 to 59")
        timezone_name = _text(raw["timezone"], "timezone")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise WeeklyBriefCoordinatorError("timezone is not an IANA zone") from exc
        effective_from = _utc(_instant(raw["effective_from"], "effective_from"))
        overlays_raw = raw["company_overlay_version_ids"]
        if not isinstance(overlays_raw, list) or not overlays_raw:
            raise WeeklyBriefCoordinatorError(
                "company_overlay_version_ids must be a non-empty array"
            )
        overlays = tuple(
            _text(value, "company_overlay_version_ids[]") for value in overlays_raw
        )
        if len(overlays) != len(set(overlays)):
            raise WeeklyBriefCoordinatorError(
                "company_overlay_version_ids must be unique"
            )
        theses_raw = raw["company_thesis_refs"]
        if not isinstance(theses_raw, Mapping):
            raise WeeklyBriefCoordinatorError("company_thesis_refs must be an object")
        theses = {
            _text(key, "company_thesis_refs key"): _text(
                value, "company_thesis_refs value"
            )
            for key, value in theses_raw.items()
        }
        return cls(
            plan_ref=_text(raw["plan_ref"], "plan_ref"),
            brief_ref=_text(raw["brief_ref"], "brief_ref"),
            timezone=timezone_name, weekday=weekday, hour=hour, minute=minute,
            effective_from=effective_from,
            evidence_pack_version_id=_text(
                raw["evidence_pack_version_id"], "evidence_pack_version_id"
            ),
            company_overlay_version_ids=overlays,
            company_thesis_refs=MappingProxyType(theses),
            destination_ref=_text(raw["destination_ref"], "destination_ref"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_ref": self.plan_ref, "brief_ref": self.brief_ref,
            "timezone": self.timezone, "weekday": self.weekday,
            "hour": self.hour, "minute": self.minute,
            "effective_from": self.effective_from,
            "evidence_pack_version_id": self.evidence_pack_version_id,
            "company_overlay_version_ids": list(self.company_overlay_version_ids),
            "company_thesis_refs": dict(self.company_thesis_refs),
            "destination_ref": self.destination_ref,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    def latest_due(self, as_of: datetime) -> datetime | None:
        if as_of.tzinfo is None:
            raise WeeklyBriefCoordinatorError("coordinator clock must include timezone")
        zone = ZoneInfo(self.timezone)
        local = as_of.astimezone(zone)
        candidate_date = local.date() - timedelta(
            days=(local.weekday() - self.weekday) % 7
        )
        candidate = datetime.combine(
            candidate_date, time(self.hour, self.minute), tzinfo=zone
        )
        if candidate > local:
            candidate -= timedelta(days=7)
        scheduled = candidate.astimezone(timezone.utc)
        if scheduled < _instant(self.effective_from, "effective_from"):
            return None
        return scheduled


@dataclass(frozen=True, slots=True)
class WeeklyBriefCoordinatorConfig:
    writer_socket: Path
    token_config: Path
    plan: WeeklyBriefSchedulePlan

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WeeklyBriefCoordinatorConfig":
        if set(raw) != {"writer_socket", "token_config", "plan"}:
            raise WeeklyBriefCoordinatorError(
                "weekly brief coordinator config has an invalid closed shape"
            )
        plan_raw = raw["plan"]
        if not isinstance(plan_raw, Mapping):
            raise WeeklyBriefCoordinatorError("plan must be an object")
        return cls(
            writer_socket=_absolute_path(raw["writer_socket"], "writer_socket"),
            token_config=_absolute_path(raw["token_config"], "token_config"),
            plan=WeeklyBriefSchedulePlan.from_mapping(plan_raw),
        )


class WeeklyBriefCoordinator:
    def __init__(
        self,
        config: WeeklyBriefCoordinatorConfig,
        *,
        client: WriterClient | None = None,
    ) -> None:
        self.config = config
        if client is None:
            # Lazy import avoids making writer_server -> coordinator ->
            # writer_server a module-load cycle.
            from .writer_server import load_principals

            principal = load_principals(config.token_config).get("core")
            if principal is None:
                raise WeeklyBriefCoordinatorError(
                    "core writer principal is unavailable"
                )
            client = WriterClient(str(config.writer_socket), principal.token, timeout=30)
        self.client = client

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise WeeklyBriefCoordinatorError(
                "coordinator clock must include timezone"
            )
        return self.client.run_weekly_brief_cycle(
            plan=self.config.plan.to_dict(), as_of=_utc(now)
        )


def _validate_policy(
    active: Mapping[str, Any], plan: WeeklyBriefSchedulePlan, as_of: datetime
) -> None:
    effective_from = _instant(active.get("effective_from"), "policy.effective_from")
    effective_until_raw = active.get("effective_until")
    effective_until = (
        None if effective_until_raw is None
        else _instant(effective_until_raw, "policy.effective_until")
    )
    if as_of < effective_from or (
        effective_until is not None and as_of >= effective_until
    ):
        raise WeeklyBriefCoordinatorPrecondition(
            "active governance policy is outside its effective window"
        )
    policy = active.get("policy")
    if not isinstance(policy, Mapping):
        raise WeeklyBriefCoordinatorPrecondition(
            "active governance policy record is invalid"
        )
    rule = policy.get("weekly_brief_auto_publish")
    expected = {
        "enabled", "rule_ref", "allowed_plan_bindings", "max_issues_per_week"
    }
    if not isinstance(rule, Mapping) or set(rule) != expected:
        raise WeeklyBriefCoordinatorPrecondition(
            "active policy lacks the closed weekly_brief_auto_publish rule"
        )
    if (
        rule.get("enabled") is not True
        or rule.get("rule_ref") != WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF
        or rule.get("max_issues_per_week") != 1
    ):
        raise WeeklyBriefCoordinatorPrecondition(
            "active policy does not authorize one scheduled issue per week"
        )
    bindings = rule.get("allowed_plan_bindings")
    if not isinstance(bindings, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"plan_ref", "plan_hash"}
        for item in bindings
    ):
        raise WeeklyBriefCoordinatorPrecondition(
            "active policy has invalid weekly brief plan bindings"
        )
    if {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash} not in [
        dict(item) for item in bindings
    ]:
        raise WeeklyBriefCoordinatorPrecondition(
            "active policy does not authorize the exact weekly brief plan hash"
        )


def run_weekly_brief_cycle(
    store: DaltonStore,
    weekly: WeeklyBriefAuthority,
    agenda: AgendaStore,
    *,
    plan: Mapping[str, Any],
    as_of: str,
    actor_ref: str,
) -> dict[str, Any]:
    if actor_ref != "core":
        raise WeeklyBriefCoordinatorError(
            "weekly brief cycle requires the core writer principal"
        )
    schedule = WeeklyBriefSchedulePlan.from_mapping(plan)
    now = _instant(as_of, "as_of")
    due = schedule.latest_due(now)
    if due is None:
        return {
            "status": "waiting", "plan_ref": schedule.plan_ref,
            "plan_hash": schedule.content_hash, "as_of": _utc(now),
            "reason": "no schedule is due after plan effective_from",
        }
    scheduled_for = _utc(due)
    period_start = _utc(due - timedelta(days=7))
    identity = {
        "plan_ref": schedule.plan_ref, "plan_hash": schedule.content_hash,
        "scheduled_for": scheduled_for,
    }
    digest = content_hash(identity)[:32]
    cycle_id = f"weekly-brief-cycle:{digest}"
    issue_version_ref = f"weekly-brief-version:{digest}"
    try:
        admission = weekly.cycle_admission(cycle_id)
        admission_status = "duplicate"
    except WeeklyBriefNotFound:
        try:
            active = store.active_policy()
        except Exception as exc:
            raise WeeklyBriefCoordinatorPrecondition(
                f"Core has no active governance policy: {exc}"
            ) from exc
        _validate_policy(active, schedule, now)
        latest = store.connection.execute(
            "SELECT version_id FROM weekly_brief_issue_versions "
            "WHERE brief_ref=? ORDER BY version_number DESC LIMIT 1",
            (schedule.brief_ref,),
        ).fetchone()
        prior = None if latest is None else latest["version_id"]
        admission = weekly.admit_scheduled_cycle(
            cycle_id=cycle_id, plan_ref=schedule.plan_ref,
            plan_hash=schedule.content_hash,
            policy_version_ref=active["policy_version_id"],
            policy_version_hash=active["content_hash"],
            scheduled_for=scheduled_for, period_start=period_start,
            period_end=scheduled_for, brief_ref=schedule.brief_ref,
            issue_version_ref=issue_version_ref, prior_version_ref=prior,
            evidence_pack_version_ref=schedule.evidence_pack_version_id,
            company_overlay_version_refs=list(
                schedule.company_overlay_version_ids
            ),
            company_thesis_refs=schedule.company_thesis_refs,
            destination_ref=schedule.destination_ref, actor_ref=actor_ref,
            idempotency_key=f"weekly-brief-admission:{digest}",
        )
        admission_status = admission["status"]
    if admission["plan_hash"] != schedule.content_hash:
        raise WeeklyBriefCoordinatorPrecondition(
            "existing cycle admission does not match the configured plan hash"
        )
    issue = weekly.publish_scheduled_issue(
        cycle_id, actor_ref=actor_ref,
        idempotency_key=f"weekly-brief-publication:{digest}",
    )
    rendered = weekly.render_markdown(issue["id"])
    artifact_sha256 = hashlib.sha256(
        rendered["body"].encode("utf-8")
    ).hexdigest()
    outbox = agenda.enqueue_weekly_brief(
        payload={
            "schema_version": SCHEMA_VERSION,
            "kind": "weekly_research_brief", "cycle_ref": cycle_id,
            "issue_version_ref": issue["id"],
            "issue_version_hash": issue["content_hash"],
            "brief_ref": issue["brief_ref"],
            "industry_ref": issue["industry_ref"], "period": issue["period"],
            "destination_ref": admission["destination_ref"],
            "artifact_sha256": artifact_sha256, "body": rendered["body"],
            "created_at": scheduled_for,
        },
        actor_ref=actor_ref,
        idempotency_key=f"weekly-brief-outbox:{digest}",
    )
    if not isinstance(outbox, Mapping) or "message_id" not in outbox:
        # A rebuilt payload that no longer byte-matches the enqueued message
        # replays as a conflict; surface that instead of crashing on the key.
        raise WeeklyBriefCoordinatorError(
            "weekly brief outbox enqueue did not replay an existing message; "
            f"status={outbox.get('status') if isinstance(outbox, Mapping) else 'invalid'}"
        )
    return {
        "status": "ready", "cycle_ref": cycle_id,
        "scheduled_for": scheduled_for, "plan_ref": schedule.plan_ref,
        "plan_hash": schedule.content_hash,
        "admission_status": admission_status,
        "issue_status": issue["status"], "issue_version_ref": issue["id"],
        "issue_version_hash": issue["content_hash"],
        "outbox_status": outbox["status"],
        "outbox_message_ref": outbox["message_id"],
    }


__all__ = [
    "WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF", "WeeklyBriefCoordinator",
    "WeeklyBriefCoordinatorConfig", "WeeklyBriefCoordinatorError",
    "WeeklyBriefCoordinatorPrecondition", "WeeklyBriefSchedulePlan",
    "run_weekly_brief_cycle",
]
