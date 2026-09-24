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
from .industry_evidence_refresh import EvidenceRefreshError, refresh_evidence_pack
from .industry_research import IndustryResearchAuthority, IndustryResearchError
from .store import DaltonStore, content_hash
from .weekly_brief import (
    WeeklyBriefAuthority,
    WeeklyBriefNotFound,
)
from .writer_client import WriterClient


SCHEMA_VERSION = "0.1"
REFRESH_SCHEMA_VERSION = "0.2"
MAX_CLAIM_WINDOW_DAYS = 730
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
class EvidenceRefreshSpec:
    """Plan 0.2: rebuild the evidence pack from the Ledger before each issue."""

    evidence_pack_ref: str
    company_overlay_refs: tuple[str, ...]
    claim_window_days: int

    @classmethod
    def from_mapping(cls, raw: Any) -> "EvidenceRefreshSpec":
        if not isinstance(raw, Mapping) or set(raw) != {
            "evidence_pack_ref", "company_overlay_refs", "claim_window_days",
        }:
            raise WeeklyBriefCoordinatorError(
                "evidence_refresh has an invalid closed shape"
            )
        overlays_raw = raw["company_overlay_refs"]
        if not isinstance(overlays_raw, list) or not overlays_raw:
            raise WeeklyBriefCoordinatorError(
                "evidence_refresh.company_overlay_refs must be a non-empty array"
            )
        overlays = tuple(
            _text(value, "evidence_refresh.company_overlay_refs[]")
            for value in overlays_raw
        )
        if len(overlays) != len(set(overlays)):
            raise WeeklyBriefCoordinatorError(
                "evidence_refresh.company_overlay_refs must be unique"
            )
        window = raw["claim_window_days"]
        if isinstance(window, bool) or not isinstance(window, int) or not (
            1 <= window <= MAX_CLAIM_WINDOW_DAYS
        ):
            raise WeeklyBriefCoordinatorError(
                "evidence_refresh.claim_window_days must be an integer from 1 to "
                f"{MAX_CLAIM_WINDOW_DAYS}"
            )
        return cls(
            evidence_pack_ref=_text(
                raw["evidence_pack_ref"], "evidence_refresh.evidence_pack_ref"
            ),
            company_overlay_refs=overlays, claim_window_days=window,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_pack_ref": self.evidence_pack_ref,
            "company_overlay_refs": list(self.company_overlay_refs),
            "claim_window_days": self.claim_window_days,
        }


@dataclass(frozen=True, slots=True)
class WeeklyBriefSchedulePlan:
    """A 0.1 plan pins one exact pack version; a 0.2 plan refreshes it.

    The plan hash is what governance authorizes, so a 0.2 plan keeps one hash
    across weeks while the pack version it resolves to moves with the Ledger;
    each cycle admission still freezes the exact version it published.
    """

    plan_ref: str
    brief_ref: str
    timezone: str
    weekday: int
    hour: int
    minute: int
    effective_from: str
    evidence_pack_version_id: str | None
    company_overlay_version_ids: tuple[str, ...]
    company_thesis_refs: Mapping[str, str]
    destination_ref: str
    evidence_refresh: EvidenceRefreshSpec | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WeeklyBriefSchedulePlan":
        common = {
            "schema_version", "plan_ref", "brief_ref", "timezone", "weekday",
            "hour", "minute", "effective_from", "company_thesis_refs",
            "destination_ref",
        }
        version = raw.get("schema_version")
        if version == SCHEMA_VERSION:
            expected = common | {
                "evidence_pack_version_id", "company_overlay_version_ids",
            }
        elif version == REFRESH_SCHEMA_VERSION:
            expected = common | {"evidence_refresh"}
        else:
            expected = None
        if expected is None or set(raw) != expected:
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
        refresh = None
        pack_version_id = None
        overlays: tuple[str, ...] = ()
        if version == REFRESH_SCHEMA_VERSION:
            refresh = EvidenceRefreshSpec.from_mapping(raw["evidence_refresh"])
        else:
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
            pack_version_id = _text(
                raw["evidence_pack_version_id"], "evidence_pack_version_id"
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
            evidence_pack_version_id=pack_version_id,
            company_overlay_version_ids=overlays,
            company_thesis_refs=MappingProxyType(theses),
            destination_ref=_text(raw["destination_ref"], "destination_ref"),
            evidence_refresh=refresh,
        )

    def to_dict(self) -> dict[str, Any]:
        # A 0.1 plan must serialize exactly as before: its hash is what the
        # active governance policy and the research constitution bind.
        wire: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "plan_ref": self.plan_ref, "brief_ref": self.brief_ref,
            "timezone": self.timezone, "weekday": self.weekday,
            "hour": self.hour, "minute": self.minute,
            "effective_from": self.effective_from,
        }
        if self.evidence_refresh is None:
            wire["evidence_pack_version_id"] = self.evidence_pack_version_id
            wire["company_overlay_version_ids"] = list(
                self.company_overlay_version_ids
            )
        else:
            wire["schema_version"] = REFRESH_SCHEMA_VERSION
            wire["evidence_refresh"] = self.evidence_refresh.to_dict()
        wire["company_thesis_refs"] = dict(self.company_thesis_refs)
        wire["destination_ref"] = self.destination_ref
        return wire

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


def _resolve_evidence(
    weekly: WeeklyBriefAuthority,
    schedule: WeeklyBriefSchedulePlan,
    *,
    active: Mapping[str, Any],
    cycle_id: str,
    scheduled_for: str,
) -> tuple[str, list[str], dict[str, Any] | None]:
    """The exact pack and overlay versions this not-yet-admitted cycle binds.

    A 0.1 plan names them.  A 0.2 plan runs the deterministic Ledger refresh
    first; if the Ledger cannot support a valid pack the cycle falls back to
    the current pack pointer and its bound overlays, and says so in the cycle
    result rather than silently re-sending stale evidence as if it were new.
    """

    refresh_spec = schedule.evidence_refresh
    if refresh_spec is None:
        return (
            str(schedule.evidence_pack_version_id),
            list(schedule.company_overlay_version_ids), None,
        )
    industry = weekly.industry_research
    try:
        result = refresh_evidence_pack(
            industry,
            evidence_pack_ref=refresh_spec.evidence_pack_ref,
            company_overlay_refs=refresh_spec.company_overlay_refs,
            claim_window_days=refresh_spec.claim_window_days,
            scheduled_for=scheduled_for,
            authority={
                "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
                "plan_ref": schedule.plan_ref,
                "plan_hash": schedule.content_hash,
                "policy_version_ref": active["policy_version_id"],
                "policy_version_hash": active["content_hash"],
                "cycle_ref": cycle_id, "scheduled_for": scheduled_for,
            },
        )
    except (EvidenceRefreshError, IndustryResearchError) as exc:
        return _fallback_evidence(industry, refresh_spec, str(exc))
    return (
        result["evidence_pack_version_ref"],
        list(result["company_overlay_version_refs"]), result,
    )


def _fallback_evidence(
    industry: IndustryResearchAuthority, spec: "EvidenceRefreshSpec", reason: str,
) -> tuple[str, list[str], dict[str, Any]]:
    connection = industry.connection
    pointer = connection.execute(
        "SELECT version_id FROM industry_evidence_pack_pointer WHERE evidence_pack_ref=?",
        (spec.evidence_pack_ref,),
    ).fetchone()
    if pointer is None:
        raise WeeklyBriefCoordinatorPrecondition(
            f"evidence refresh failed and {spec.evidence_pack_ref} has no version: {reason}"
        )
    pack = industry.evidence_pack(pointer["version_id"])
    overlays = []
    for overlay_ref in spec.company_overlay_refs:
        row = connection.execute(
            "SELECT version_id FROM company_overlay_versions WHERE overlay_ref=? "
            "AND evidence_pack_version_ref=? AND evidence_pack_version_hash=? "
            "ORDER BY version_number DESC LIMIT 1",
            (overlay_ref, pack["id"], pack["content_hash"]),
        ).fetchone()
        if row is not None:
            overlays.append(row["version_id"])
    if not overlays:
        raise WeeklyBriefCoordinatorPrecondition(
            f"evidence refresh failed and no overlay is bound to {pack['id']}: {reason}"
        )
    return pack["id"], overlays, {
        "status": "fallback", "reason": reason,
        "evidence_pack_version_ref": pack["id"],
        "company_overlay_version_refs": overlays,
    }


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
    refresh: dict[str, Any] | None = None
    try:
        admission = weekly.cycle_admission(cycle_id)
        admission_status = "duplicate"
    except WeeklyBriefNotFound:
        # max_issues_per_week is per brief, not per plan: when a new plan
        # (say pinned 0.1 -> refreshing 0.2) takes over at a slot the old plan
        # already issued, the slot is done rather than published twice.
        issued = store.connection.execute(
            "SELECT cycle_id,plan_ref,issue_version_ref FROM weekly_brief_cycle_admissions "
            "WHERE brief_ref=? AND scheduled_for=? LIMIT 1",
            (schedule.brief_ref, scheduled_for),
        ).fetchone()
        if issued is not None:
            return {
                "status": "already_issued", "plan_ref": schedule.plan_ref,
                "plan_hash": schedule.content_hash, "scheduled_for": scheduled_for,
                "issued_cycle_ref": issued["cycle_id"],
                "issued_plan_ref": issued["plan_ref"],
                "issue_version_ref": issued["issue_version_ref"],
            }
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
        pack_version_ref, overlay_refs, refresh = _resolve_evidence(
            weekly, schedule, active=active, cycle_id=cycle_id,
            scheduled_for=scheduled_for,
        )
        admission = weekly.admit_scheduled_cycle(
            cycle_id=cycle_id, plan_ref=schedule.plan_ref,
            plan_hash=schedule.content_hash,
            policy_version_ref=active["policy_version_id"],
            policy_version_hash=active["content_hash"],
            scheduled_for=scheduled_for, period_start=period_start,
            period_end=scheduled_for, brief_ref=schedule.brief_ref,
            issue_version_ref=issue_version_ref, prior_version_ref=prior,
            evidence_pack_version_ref=pack_version_ref,
            company_overlay_version_refs=overlay_refs,
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
        "evidence_pack_version_ref": admission["evidence_pack_version_ref"],
        **({} if refresh is None else {"evidence_refresh": refresh}),
    }


__all__ = [
    "EvidenceRefreshSpec", "REFRESH_SCHEMA_VERSION",
    "WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF", "WeeklyBriefCoordinator",
    "WeeklyBriefCoordinatorConfig", "WeeklyBriefCoordinatorError",
    "WeeklyBriefCoordinatorPrecondition", "WeeklyBriefSchedulePlan",
    "run_weekly_brief_cycle",
]
