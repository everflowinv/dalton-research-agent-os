"""The owner's weekly brief, publishable from any environment's own cockpit.

The legacy environment got its weekly brief by hand: a plan file under
``deploy/phase1``, a governance policy that names its exact hash, a
constitution and mission rebound to that policy, the plan pasted into
``service.json`` and the controller restarted (``scripts/
switch_weekly_brief_plan.py``).  A workspace had none of it -- no plan, no
rule, no entry point -- so README section 7's weekly brief simply did not
exist outside legacy (review 2026-09-30, gap d2).

This module is the one owner decision that replaces all of that:

* :func:`build_workspace_weekly_brief_plan` derives a refreshing (schema 0.2)
  schedule plan from the active mission: the brief, the evidence pack and one
  overlay per covered company are named after the mission, the way legacy's
  ``us-it-services`` plan names them;
* :func:`publish_workspace_weekly_brief_plan` authorizes that exact plan hash
  in the active policy's ``weekly_brief_auto_publish`` rule and cascades the
  policy -> constitution -> mission rebinding through the real authorities,
  then writes the plan to ``<state>/weekly-brief-plan.json`` (with an
  append-only copy under ``weekly-brief-plans/``).  The controller reads that
  file on every run when ``service.json`` names it (``plan_path``), so a new
  plan needs no restart and no ``service.json`` edit;
* the destination is optional.  Without Discord the brief is delivered to a
  **local** destination (``cockpit:weekly-brief:<slug>``): the controller
  writes the exact Markdown into ``<state>/weekly-briefs/`` and records the
  delivery in Core, and the cockpit's weekly brief page lists it.

Nothing here makes a model call or a network request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import content_hash
from .weekly_brief_coordinator import (
    PROGRESS_SCHEMA_VERSION, REFRESH_SCHEMA_VERSION,
    WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
    WeeklyBriefCoordinatorError,
    WeeklyBriefSchedulePlan,
)

PLAN_FILENAME = "weekly-brief-plan.json"
PLAN_REVISIONS_DIRNAME = "weekly-brief-plans"
LOCAL_BRIEFS_DIRNAME = "weekly-briefs"
LOCAL_DESTINATION_PREFIX = "cockpit:weekly-brief:"
DEFAULT_CLAIM_WINDOW_DAYS = 200
RULE_KEY = "weekly_brief_auto_publish"
_UTC = timezone.utc
_SLUG = re.compile(r"[^a-z0-9-]+")
_VERSION = re.compile(r":v([0-9]+)$")
_IDENTITY_SEGMENT = re.compile(r"^[0-9a-f]{16,64}$")
_TRAILING_NUMBER = re.compile(r"^(?P<prefix>.*?)(?P<number>[0-9]+)$")


class WorkspaceWeeklyBriefError(ValueError):
    """The plan cannot be built or published as asked."""


class WorkspaceWeeklyBriefConflict(WorkspaceWeeklyBriefError):
    """The plan changed since the caller read it; reload and retry."""


# ---------------------------------------------------------------------------
# destinations


def _slug(value: str) -> str:
    slug = _SLUG.sub("-", str(value).strip().lower()).strip("-")
    if not slug:
        raise WorkspaceWeeklyBriefError("cannot derive a weekly brief name")
    return slug[:80]


def local_destination_ref(slug: str) -> str:
    """The destination a brief is delivered to when nobody named a channel."""

    return f"{LOCAL_DESTINATION_PREFIX}{_slug(slug)}"


def is_local_destination(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith(LOCAL_DESTINATION_PREFIX)


def brief_slug_for_mission(mission: Mapping[str, Any]) -> str:
    """``coverage-mission:ws-7d89...`` -> ``ws-7d89...``; legacy keeps its name."""

    ref = str(mission.get("mission_ref") or "")
    _, _, tail = ref.partition(":")
    return _slug(tail or ref)


# ---------------------------------------------------------------------------
# the plan


def build_workspace_weekly_brief_plan(
    mission: Mapping[str, Any], *, brief_slug: str, version: int,
    weekday: int, hour: int, minute: int, timezone: str,
    destination_ref: str, effective_from: str,
    claim_window_days: int = DEFAULT_CLAIM_WINDOW_DAYS,
) -> dict[str, Any]:
    """A refreshing schedule plan over the mission's own universe.

    Named the way legacy's ``weekly-brief-plan:us-it-services:v4`` is: one
    brief, one evidence pack, one overlay per covered company (lower-case
    ticker).  The plan is validated by the same parser the controller uses,
    so a plan this returns is a plan the controller can run.
    """

    slug = _slug(brief_slug)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise WorkspaceWeeklyBriefError("plan version must be a positive integer")
    overlays: list[str] = []
    for member in mission.get("universe") or []:
        if not isinstance(member, Mapping):
            continue
        tag = member.get("ticker") or str(member.get("company_ref") or "").rpartition(":")[2]
        if tag:
            ref = f"company-overlay:{_slug(tag)}"
            if ref not in overlays:
                overlays.append(ref)
    if not overlays:
        raise WorkspaceWeeklyBriefError("the mission names no company to brief on")
    wire = {
        "schema_version": PROGRESS_SCHEMA_VERSION if mission.get("mission_ref") else REFRESH_SCHEMA_VERSION,
        "plan_ref": f"weekly-brief-plan:{slug}:v{version}",
        "brief_ref": f"weekly-brief:{slug}",
        "timezone": timezone, "weekday": weekday, "hour": hour, "minute": minute,
        "effective_from": effective_from,
        "evidence_refresh": {
            "evidence_pack_ref": f"industry-evidence-pack:{slug}",
            "company_overlay_refs": overlays,
            "claim_window_days": claim_window_days,
        },
        "company_thesis_refs": {},
        "destination_ref": destination_ref,
    }
    if mission.get("mission_ref"):
        wire["mission_progress"] = {"mission_ref": mission["mission_ref"]}
    try:
        return WeeklyBriefSchedulePlan.from_mapping(wire).to_dict()
    except WeeklyBriefCoordinatorError as exc:
        raise WorkspaceWeeklyBriefError(str(exc)) from exc


def plan_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / PLAN_FILENAME


def read_plan(state_dir: str | Path) -> dict[str, Any] | None:
    """The currently published plan, or ``None`` when none has been."""

    path = plan_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkspaceWeeklyBriefError(f"{PLAN_FILENAME} is unreadable: {exc}") from exc
    try:
        return WeeklyBriefSchedulePlan.from_mapping(raw).to_dict()
    except (WeeklyBriefCoordinatorError, TypeError) as exc:
        raise WorkspaceWeeklyBriefError(f"{PLAN_FILENAME} is invalid: {exc}") from exc


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_plan(state_dir: Path, plan: Mapping[str, Any]) -> None:
    digest = content_hash(plan)
    revision = state_dir / PLAN_REVISIONS_DIRNAME / f"{digest}.json"
    if not revision.exists():
        _atomic_write(revision, plan)
    _atomic_write(plan_path(state_dir), plan)


def _schedule_of(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: plan.get(key) for key in (
        "weekday", "hour", "minute", "timezone", "destination_ref")}


# ---------------------------------------------------------------------------
# governance


def _next_version_id(current_id: str, version: int) -> str:
    """``policy-7`` -> ``policy-8``; a hash-named first version gets a number."""

    prefix, _, last = current_id.rpartition(":")
    if prefix and _IDENTITY_SEGMENT.fullmatch(last):
        return f"{prefix}:{version}"
    match = _TRAILING_NUMBER.match(current_id)
    if match is None:
        raise WorkspaceWeeklyBriefError(f"cannot derive the next version id from {current_id!r}")
    return f"{match.group('prefix')}{version}"


def _rule_with(rule: Any, binding: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
    """The closed rule, enabled, with this exact plan binding in it."""

    expected = {"enabled", "rule_ref", "allowed_plan_bindings", "max_issues_per_week"}
    if rule is None:
        return {"enabled": True, "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
                "allowed_plan_bindings": [dict(binding)],
                "max_issues_per_week": 1}, True
    if (not isinstance(rule, Mapping) or set(rule) != expected
            or rule.get("rule_ref") != WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF
            or not isinstance(rule.get("allowed_plan_bindings"), list)):
        raise WorkspaceWeeklyBriefError(
            "the active policy's weekly_brief_auto_publish rule has an unexpected shape; "
            "it is not changed here")
    bindings = [dict(item) for item in rule["allowed_plan_bindings"]]
    changed = rule.get("enabled") is not True or rule.get("max_issues_per_week") != 1
    if dict(binding) not in bindings:
        bindings.append(dict(binding))
        changed = True
    return {"enabled": True, "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
            "allowed_plan_bindings": bindings, "max_issues_per_week": 1}, changed


def _active_mission(store: Any) -> dict[str, Any]:
    from .coverage_mission import CoverageMissionAuthority

    row = store.connection.execute(
        "SELECT mission_ref FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if row is None:
        raise WorkspaceWeeklyBriefError(
            "no research mission is published yet; publish the first mission before "
            "scheduling its weekly brief")
    return CoverageMissionAuthority(store).active_mission(row[0])


def authorize_plan(store: Any, plan: Mapping[str, Any], *, actor_ref: str,
                   now: str) -> dict[str, Any]:
    """Policy -> constitution -> mission, each only if it does not already bind it.

    The same cascade ``set_research_budget_authority_chain`` performs, so the
    versions it mints are named the way every other owner edit names them.
    The previous plan bindings stay in the rule: a controller still holding
    the previous plan keeps working until its next read of the plan file.
    """

    from .coverage_mission import CoverageMissionAuthority
    from .research_constitution import ResearchConstitutionAuthority

    schedule = WeeklyBriefSchedulePlan.from_mapping(plan)
    binding = {"plan_ref": schedule.plan_ref, "plan_hash": schedule.content_hash}
    mission = _active_mission(store)
    current = store.active_policy_version().to_dict()
    body = dict(current["policy"])
    rule, changed = _rule_with(body.get(RULE_KEY), binding)
    published: dict[str, str] = {}
    if changed:
        number = int(current["version"]) + 1
        body[RULE_KEY] = rule
        # ``to_dict`` keeps the independence predicates beside ``policy``;
        # publishing ``policy`` alone would silently drop them.
        body = {**body, "independence_predicates": list(
            current.get("independence_predicates") or [])}
        created = store.create_policy(
            body, policy_version_id=_next_version_id(current["id"], number),
            version_number=number, activate=True, policy_ref=current["policy_ref"],
            effective_from=now, actor_ref=actor_ref, prior_version_ref=current["id"],
            change_reason=(
                f"owner scheduled weekly brief plan {schedule.plan_ref} "
                f"(hash {schedule.content_hash}) from the cockpit; the plan binding is "
                "appended to weekly_brief_auto_publish, every other rule is unchanged"))
        record = created.get("policy_version", created)
        policy = {"ref": record.get("id") or record["policy_version_id"],
                  "hash": record["content_hash"]}
        published["policy"] = policy["ref"]
    else:
        policy = {"ref": current["id"], "hash": current["content_hash"]}
    constitutions = ResearchConstitutionAuthority(store)
    old = constitutions.constitution(mission["bindings"]["constitution_version"]["ref"])
    active = constitutions.active_constitution(old["constitution_ref"])
    bindings = json.loads(json.dumps(active["bindings"]))
    bindings["governance_policy_version"] = policy
    bindings["weekly_brief_plan"] = {"ref": schedule.plan_ref, "hash": schedule.content_hash}
    stamp = schedule.content_hash[:12]
    if active["bindings"] == bindings:
        constitution = active
    else:
        number = int(active["version"]) + 1
        constitution = constitutions.publish_constitution(
            active["constitution_ref"], industry_ref=active["industry_ref"],
            title=active["title"], bindings=bindings, method=active["method"],
            actor_ref=actor_ref,
            version_id=f"constitution-version:{active['constitution_ref'].split(':', 1)[1]}:{number}",
            prior_version_ref=active["id"],
            idempotency_key=f"{active['constitution_ref']}:{number}:weekly-brief-plan:{stamp}")
        published["constitution"] = constitution["id"]
    mission_bindings = {**mission["bindings"], "constitution_version": {
        "ref": constitution["id"], "hash": constitution["content_hash"]}}
    if mission["bindings"] != mission_bindings:
        number = int(mission["version"]) + 1
        values = {key: json.loads(json.dumps(mission[key])) for key in (
            "title", "objective", "industry_ref", "universe", "research_questions",
            "deliverables", "source_plan", "autonomy", "budget")}
        created_mission = CoverageMissionAuthority(store).create_mission(
            mission["mission_ref"], **values, bindings=mission_bindings,
            actor_ref=actor_ref,
            version_id=f"coverage-mission-version:{mission['mission_ref'].split(':', 1)[1]}:{number}",
            prior_version_ref=mission["id"],
            idempotency_key=f"{mission['mission_ref']}:{number}:weekly-brief-plan:{stamp}")
        published["mission"] = created_mission["id"]
    return {"policy": policy, "published": published}


def publish_workspace_weekly_brief_plan(
    store: Any, state_dir: str | Path, *, weekday: int, hour: int, minute: int,
    timezone: str, destination_ref: str | None, expected_plan_hash: str | None,
    actor_ref: str, now: datetime | None = None,
) -> dict[str, Any]:
    """Publish (or confirm) the environment's weekly brief schedule.

    ``expected_plan_hash`` is the hash of the plan the caller read (``None``
    when there was none): a stale page cannot overwrite a newer decision.
    The same schedule and destination again is ``unchanged`` and writes
    nothing.
    """

    if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
        raise WorkspaceWeeklyBriefError("a weekly brief schedule is an owner decision")
    state = Path(state_dir)
    current = read_plan(state)
    current_hash = None if current is None else content_hash(current)
    if expected_plan_hash != current_hash:
        raise WorkspaceWeeklyBriefConflict(
            "the weekly brief plan changed since this page read it; reload and retry")
    mission = _active_mission(store)
    slug = brief_slug_for_mission(mission)
    destination = (destination_ref.strip() if isinstance(destination_ref, str)
                   and destination_ref.strip() else local_destination_ref(slug))
    moment = (now or datetime.now(_UTC)).astimezone(_UTC)
    stamp = moment.isoformat(timespec="microseconds")
    wanted = {"weekday": weekday, "hour": hour, "minute": minute,
              "timezone": timezone, "destination_ref": destination}
    if current is not None and _schedule_of(current) == wanted:
        authorization = authorize_plan(store, current, actor_ref=actor_ref, now=stamp)
        return {"status": "unchanged" if not authorization["published"] else "authorized",
                "plan": current, "plan_hash": current_hash,
                "local": is_local_destination(destination), **authorization}
    version = 1
    if current is not None:
        match = _VERSION.search(str(current.get("plan_ref", "")))
        version = (int(match.group(1)) + 1) if match else 2
    plan = build_workspace_weekly_brief_plan(
        mission, brief_slug=slug, version=version, weekday=weekday, hour=hour,
        minute=minute, timezone=timezone, destination_ref=destination,
        effective_from=stamp)
    authorization = authorize_plan(store, plan, actor_ref=actor_ref, now=stamp)
    _write_plan(state, plan)
    return {"status": "published", "plan": plan, "plan_hash": content_hash(plan),
            "prior_plan_hash": current_hash, "local": is_local_destination(destination),
            **authorization}


# ---------------------------------------------------------------------------
# local delivery


class LocalWeeklyBriefDelivery:
    """Deliver scheduled briefs for a local destination into a directory.

    The same three steps ``OpenClawAgendaBridge`` takes for Discord -- claim
    the outbox message, put the artifact where the reader is, record the
    delivery on both the weekly brief and the outbox -- with a file in place
    of a message.  The file name is derived from the issue, the delivery time
    from the payload, so a replay after a crash records the same delivery.
    """

    def __init__(self, client: Any, *, endpoint_ref: str, directory: str | Path,
                 claim_ttl_seconds: int = 120, max_attempts: int = 5) -> None:
        if not is_local_destination(endpoint_ref):
            raise WorkspaceWeeklyBriefError("local delivery needs a local destination")
        self.client = client
        self.endpoint_ref = endpoint_ref
        self.directory = Path(directory)
        self.claim_ttl_seconds = claim_ttl_seconds
        self.max_attempts = max_attempts

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        import uuid
        from datetime import timedelta

        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claimed = self.client.claim_agenda_outbox(
            endpoint_ref=self.endpoint_ref,
            now=moment.isoformat(timespec="microseconds"),
            claim_ttl_seconds=self.claim_ttl_seconds, max_attempts=self.max_attempts,
            limit=5, idempotency_key=f"weekly-brief-local-claim:{uuid.uuid4().hex}")
        delivered: list[dict[str, Any]] = []
        failed = 0
        for claim in claimed.get("claims", []) if isinstance(claimed, Mapping) else []:
            attempt = str(claim.get("delivery_attempt_id"))
            try:
                receipt = self._deliver(claim)
            except Exception as exc:  # noqa: BLE001 - one message, not the run
                failed += 1
                self.client.record_agenda_delivery(
                    message_id=claim.get("message_id"), state="failed",
                    delivery_attempt_id=attempt,
                    error_code=type(exc).__name__[:80] or "local_delivery_error",
                    retry_after=(moment + timedelta(seconds=60)).isoformat(
                        timespec="microseconds"),
                    idempotency_key=f"agenda-delivery-failed:{attempt}")
                continue
            self.client.record_agenda_delivery(
                message_id=claim.get("message_id"), state="delivered",
                delivery_attempt_id=attempt, delivery_receipt_id=receipt["receipt"],
                idempotency_key=f"agenda-delivery-complete:{attempt}")
            delivered.append(receipt)
        return {"status": "degraded" if failed else "ready",
                "claimed": len(delivered) + failed, "delivered": len(delivered),
                "failed": failed, "files": [item["path"] for item in delivered]}

    def _deliver(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        payload = claim.get("payload")
        if not isinstance(payload, Mapping) or payload.get("kind") != "weekly_research_brief":
            raise WorkspaceWeeklyBriefError("unsupported_outbox_payload")
        if payload.get("destination_ref") != self.endpoint_ref:
            raise WorkspaceWeeklyBriefError("weekly_brief_destination_mismatch")
        body = payload.get("body")
        if not isinstance(body, str) or not body:
            raise WorkspaceWeeklyBriefError("weekly_brief_payload_is_invalid")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest != payload.get("artifact_sha256"):
            raise WorkspaceWeeklyBriefError("weekly_brief_artifact_hash_mismatch")
        name = content_hash({"issue_version_ref": payload.get("issue_version_ref")})[:32]
        path = self.directory / f"weekly-brief-{name}.md"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        identity = {"issue_version_ref": payload.get("issue_version_ref"),
                    "destination_ref": payload.get("destination_ref")}
        key = content_hash(identity)[:32]
        receipt = f"file:{digest}"
        self.client.record_scheduled_weekly_brief_delivery(
            cycle_id=payload.get("cycle_ref"),
            issue_version_ref=payload.get("issue_version_ref"),
            issue_version_hash=payload.get("issue_version_hash"),
            destination_ref=payload.get("destination_ref"),
            external_message_ref=receipt, artifact_sha256=digest,
            delivered_at=payload.get("created_at"),
            delivery_id=f"weekly-brief-delivery:{key}",
            idempotency_key=f"weekly-brief-delivery:{key}")
        return {"receipt": receipt, "path": str(path),
                "issue_version_ref": payload.get("issue_version_ref")}


def local_briefs(state_dir: str | Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """The briefs delivered to this environment's local destination, newest first."""

    directory = Path(state_dir) / LOCAL_BRIEFS_DIRNAME
    try:
        files = [path for path in directory.glob("weekly-brief-*.md") if path.is_file()]
    except OSError:
        return []
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        stat = path.stat()
        rows.append({"name": path.name, "bytes": stat.st_size,
                     "written_at": datetime.fromtimestamp(
                         stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return rows


__all__ = [
    "LOCAL_BRIEFS_DIRNAME", "LOCAL_DESTINATION_PREFIX", "LocalWeeklyBriefDelivery",
    "PLAN_FILENAME", "PLAN_REVISIONS_DIRNAME", "WorkspaceWeeklyBriefConflict",
    "WorkspaceWeeklyBriefError", "authorize_plan", "brief_slug_for_mission",
    "build_workspace_weekly_brief_plan", "is_local_destination", "local_briefs",
    "local_destination_ref", "plan_path", "publish_workspace_weekly_brief_plan",
    "read_plan",
]
