"""D4: the one list of things only a person can unblock, and what to do about each.

Everything in here was already knowable.  A gate draft waiting three days is in
``deep_insight_gate_versions``; a connector nobody approved is a ``status`` field
in a JSON file; a source the mission needs and does not have is a row in its own
``source_plan``; a lane that cannot restart without an authorisation says so in
its tick result; a provider that has been returning 429 for two days is six
hundred result envelopes in the Scheduler.  Each of those lives in a different
place, is phrased for a different reader, and answers a different question from
the one the owner is actually asking, which is: *what do I have to do today, and
what happens if I don't.*

So this module answers that question and nothing else.  It is a pure read -- no
writes, no model calls, no network -- and it is deterministic: the same state
produces the same list in the same order, because a to-do list that reshuffles
itself is one the owner stops trusting.

Every item carries four sentences and they are always the same four:

* **what it is** -- one line, in the words the owner uses;
* **why it is blocking research** -- the concrete thing that is not happening;
* **what to do** -- the exact action.  Which page, which record, which
  connector, which credential.  "Approve the governance record" is not an
  action; "在「来源」页批准 sec-form144-notices" is;
* **what happens if you don't** -- not a threat, a fact.  Usually "this stage
  never advances" or "this lane will keep asking every tick".

Urgency is a small integer and it is about *blast radius*, not age.  An
environment with no research goal at all has nothing running in it; a gate
decision holds one company's entire remaining pipeline; a not-connected source
makes three specific questions permanently unanswerable; an unapproved connector
for a market this fund does not cover is a tidy-up.  Within a band, oldest
first: among equals, the thing that has been waiting longest.

Nothing here decides anything, and nothing here is a checkpoint.  It is a view
over checkpoints that already exist.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "0.1"

# The kinds of thing that can be waiting.  Closed, because an open vocabulary
# here would make the page's ordering an accident.
KINDS: tuple[str, ...] = (
    "no_active_mission",
    "gate_decision",
    "controlled_recovery",
    "provider_rate_limited",
    "source_not_connected",
    "governance_record",
    "reopen_proposal",
    "gate_auto_returned",
)
# Blast radius, smallest number first.
URGENCY: Mapping[str, int] = MappingProxyType({
    "no_active_mission": 1,
    "gate_decision": 1,
    "controlled_recovery": 2,
    "provider_rate_limited": 2,
    "source_not_connected": 3,
    "governance_record": 3,
    "reopen_proposal": 4,
    "gate_auto_returned": 5,
})
URGENCY_LABELS: Mapping[int, str] = MappingProxyType({
    1: "挡住了整条研究链",
    2: "有一整个环节停着",
    3: "有一类问题永远答不了",
    4: "积压的待办",
    5: "知会一下，不用你动手",
})

# The governance statuses that mean "a person has said yes".  Everything else
# -- ``proposed``, ``revoked``, ``expired``, anything new -- is something
# waiting, and fail-closed is right here: a record whose status this version
# does not recognise should appear on the list rather than be assumed fine.
APPROVED_GOVERNANCE_STATUSES = frozenset({"approved", "active"})
GOVERNANCE_DIR_NAME = "connector-governance"
# How far back the provider-failure probe looks, and how many failures in that
# window are worth telling somebody about.  A handful of 429s is a busy
# afternoon; six hundred is a subscription limit.
PROVIDER_WINDOW_HOURS = 24
PROVIDER_FAILURE_FLOOR = 20
PROVIDER_ERROR_CODES = frozenset({"RATE_LIMITED"})
# The lane statuses that mean a person has to authorise something before the
# lane can move.  Read off the tick result rather than guessed: the lane knows
# why it is held and writes it down every tick.
HELD_LANE_STATUSES = frozenset({"recovery_required", "not_permitted", "ungranted"})

MAX_ITEMS = 200


def _now(clock: Any | None = None) -> datetime:
    return (clock() if clock is not None else datetime.now(timezone.utc))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _open(path: Path | str | None) -> sqlite3.Connection | None:
    """One database, read-only, or ``None`` if it is not there or not readable.

    Never fatal.  This list's whole value is that it is complete enough to be
    trusted, and a Core that cannot be opened is a reason to say so beside the
    items rather than a reason to show the owner an empty page.
    """

    if path is None:
        return None
    try:
        from .readonly_sqlite import connect_read_only

        connection = connect_read_only(path)
    except (OSError, ValueError, sqlite3.Error):
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _table(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _item(kind: str, *, ref: str, at: str, title: str, why: str, action: str,
          consequence: str, where: str, detail: Mapping[str, Any] | None = None,
          actionable: bool = True) -> dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "at": at,
        "urgency": URGENCY[kind],
        "urgency_label": URGENCY_LABELS[URGENCY[kind]],
        "title": title,
        "why_blocked": why,
        "action": action,
        "consequence": consequence,
        "where": where,
        "actionable": actionable,
        "detail": dict(detail or {}),
    }


# ---------------------------------------------------------------------------
# the probes, one per source
# ---------------------------------------------------------------------------


def gate_decisions(core: Any, *, state_dir: Path | None = None) -> list[dict[str, Any]]:
    """Gate drafts waiting for a person, with what the standard makes of each.

    The quality summary travels with the item because "twelve answers are
    waiting" and "twelve answers are waiting, three of which are answered" are
    different pieces of news, and only the second one tells the owner whether
    to read it now or ask for a better draft.
    """

    if core is None or not _table(core, "deep_insight_gate_versions"):
        return []
    from .deep_insight_gate_quality import assess, load_standard

    try:
        standard = load_standard(state_dir)
    except Exception:  # noqa: BLE001 - a broken threshold file is not this page's problem
        standard = None
    rows = core.execute(
        "SELECT v.version_id, v.company_ref, v.version_number, v.created_at, "
        "v.record_json FROM deep_insight_gate_versions v "
        "LEFT JOIN deep_insight_gate_decisions d ON d.gate_version_ref=v.version_id "
        "WHERE d.decision_id IS NULL ORDER BY v.created_at, v.version_id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        dossier = _dossier_for(core, record)
        try:
            quality = assess(record, dossier=dossier, verifier_passed=True,
                             standard=standard)
        except Exception:  # noqa: BLE001 - never let a summary break the list
            quality = None
        counts = (quality or {}).get("counts") or {}
        answered = counts.get("answered")
        summary = ((quality or {}).get("summary")
                   or "这份草稿的质量摘要这次算不出来。")
        out.append(_item(
            "gate_decision", ref=str(row["version_id"]), at=str(row["created_at"]),
            title=f"深度认知评审等你裁决：{_short(str(row['company_ref']))}"
                  f"（第 {row['version_number']} 版）",
            why=("深度认知门是六个研究阶段之间唯一的人闸。在你给出结论之前，"
                 "这家公司的估值、投资论点、备忘录都不会开始。" + f"当前草稿：{summary}"),
            action="打开「待办」页，读完十二问，选「通过」「退回补充」或「否决」。"
                   "选「退回补充」时请写清楚哪几问不行——系统会把你的原话带进下一版。",
            consequence="不裁决，这家公司就一直停在第一阶段之后，后面五个阶段一步都不会走。",
            where="待办",
            detail={"company_ref": row["company_ref"],
                    "version": row["version_number"],
                    "answered": answered,
                    "unknown": counts.get("unknown"),
                    "evidence_refs": counts.get("evidence_refs"),
                    "quality_summary": summary,
                    "submittable": (quality or {}).get("submittable")},
        ))
    return out


def _dossier_for(core: Any, record: Mapping[str, Any]) -> dict[str, Any] | None:
    ref = str((record.get("bindings") or {}).get("dossier_version_ref") or "")
    if not ref or not _table(core, "company_dossier_versions"):
        return None
    row = core.execute(
        "SELECT record_json FROM company_dossier_versions WHERE version_id=?", (ref,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None


def auto_returned_drafts(state_dir: Path | None) -> list[dict[str, Any]]:
    """Drafts the system held back, and what it is waiting for.

    Not a demand on the owner -- there is nothing to click -- but it belongs on
    this page, because the alternative is a queue that empties for reasons
    nobody can see.  "Nothing is waiting for you" and "nothing is waiting for
    you because the last four drafts were not good enough to show you" are very
    different states of the world.
    """

    if state_dir is None:
        return []
    from .deep_insight_gate_quality import read_notes

    out: list[dict[str, Any]] = []
    for note in read_notes(state_dir):
        assessment = note.get("assessment") or {}
        gaps = assessment.get("question_gaps") or []
        out.append(_item(
            "gate_auto_returned", ref=f"gate-return:{note.get('company_ref')}",
            at=str(note.get("created_at") or ""),
            title=f"{_short(str(note.get('company_ref')))}：草稿尚不足以提交，没有放进你的待办",
            why=str(assessment.get("summary") or "系统判定草稿尚不足以提交。"),
            action="不用你动手。要更快推进，可以先把这家公司缺的证据补上"
                   "（下面逐题列了缺什么），或者放宽提交标准。",
            consequence="在证据变化之前，系统不会重复起草，也不会重复花模型费用；"
                        "这家公司会一直停在这里。",
            where="研究进度",
            actionable=False,
            detail={"company_ref": note.get("company_ref"),
                    "shortfalls": assessment.get("shortfall_labels") or [],
                    "counts": assessment.get("counts") or {},
                    "question_gaps": gaps[:12]},
        ))
    return out


def governance_records(governance_dir: Path | None) -> list[dict[str, Any]]:
    """Connector governance records nobody has approved.

    Read from the files rather than from any lane's held reason, and this is
    the correction that matters: a lane's ``held`` text is a record of what was
    true when it last ran, and several of the records those texts name have
    since been approved.  A to-do list that tells the owner to approve
    something they approved last week is a to-do list they stop opening.
    """

    if governance_dir is None or not Path(governance_dir).is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(Path(governance_dir).glob("*.json")):
        record = _read_json(path)
        if not isinstance(record, Mapping):
            continue
        status = str(record.get("status") or "")
        if status in APPROVED_GOVERNANCE_STATUSES:
            continue
        capability = str(record.get("capability_id") or record.get("id") or path.stem)
        name = capability.rsplit(":", 1)[-1]
        out.append(_item(
            "governance_record", ref=str(record.get("id") or path.stem),
            at=str(record.get("effective_from") or ""),
            title=f"数据源接口等你批准：{name}",
            why=("这个接口的治理记录还停在「已提出」。没有批准，用到它的研究车道每一轮"
                 "都会空转一次，取不到任何数据。"),
            action=f"打开「来源」页，找到 {name}，看清它要的权限（网络访问、"
                   f"风险等级 {record.get('allowed_permissions', {}).get('risk_class', '未标注')}）"
                   f"，然后批准或明确拒绝。",
            consequence="不处理，这条数据通道永远不通，依赖它的研究问题答不了；"
                        "车道还会继续每轮空转。",
            where="来源",
            detail={"capability_id": capability, "status": status,
                    "file": path.name,
                    "risk_class": (record.get("allowed_permissions") or {}).get(
                        "risk_class")},
        ))
    return out


def unconnected_sources(core: Any) -> list[dict[str, Any]]:
    """Sources the mission's own plan says it needs and does not have."""

    if core is None or not _table(core, "coverage_mission_pointer"):
        return []
    row = core.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref "
        "LIMIT 1").fetchone()
    if row is None:
        return []
    version = core.execute(
        "SELECT record_json, created_at FROM coverage_mission_versions "
        "WHERE mission_version_id=?", (row[0],)).fetchone()
    if version is None:
        return []
    try:
        mission = json.loads(version["record_json"])
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for entry in mission.get("source_plan") or []:
        if str(entry.get("status") or "") == "connected":
            continue
        ref = str(entry.get("source_ref") or "")
        out.append(_item(
            "source_not_connected", ref=ref, at=str(version["created_at"]),
            title=f"研究目标点名要的来源还没接上：{ref.split(':', 1)[-1]}",
            why=(f"研究目标把「{entry.get('role') or ref}」列为必需来源，但它的状态是"
                 "「未接通」。靠它回答的问题只能一直空着。"),
            action=f"打开「来源」页，把 {ref.split(':', 1)[-1]} 接上："
                   "先批准对应的连接器治理记录，再确认它能取到数据。",
            consequence="不接上，依赖这个来源的研究问题（初筛的一部分、估值、"
                        "投资判断的其中一道闸门）会一直答不了，而不是答错。",
            where="来源",
            detail={"source_ref": ref, "role": entry.get("role"),
                    "status": entry.get("status")},
        ))
    return out


def held_lanes(heartbeat: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Lanes that stopped and named an authorisation they are waiting for.

    Read from the tick result the controller already writes, in the lane's own
    words.  This module deliberately does not try to translate every lane's
    reason into a recommended action -- it cannot know them all, and a wrong
    instruction is worse than the lane's own sentence.  What it adds is the
    framing: this is a thing a person has to authorise, here is where it lives.
    """

    if not isinstance(heartbeat, Mapping):
        return []
    planner = heartbeat.get("bounded_planner")
    result = (planner or {}).get("last_result") if isinstance(planner, Mapping) else None
    if not isinstance(result, Mapping):
        return []
    at = str((planner or {}).get("last_completed_at") or "")
    out: list[dict[str, Any]] = []
    for lane, value in sorted(result.items()):
        if not isinstance(value, Mapping):
            continue
        status = str(value.get("status") or "")
        if status not in HELD_LANE_STATUSES:
            continue
        reason = str(value.get("reason") or "")
        recovery = (value.get("last") or {}).get("recovery") if isinstance(
            value.get("last"), Mapping) else None
        if isinstance(recovery, Mapping) and recovery.get("reason"):
            reason = str(recovery["reason"])
        out.append(_item(
            "controlled_recovery", ref=f"lane:{lane}", at=at,
            title=f"研究车道停着等授权：{lane}",
            why=(f"这个车道本轮的状态是「{status}」，它给出的原因是：{reason or '未说明'}。"
                 "它不会自己重新开始。"),
            action="打开「运行」页找到这个车道，按它写的原因给出一次受控恢复授权；"
                   "如果原因看不懂，把这句原文发给维护者。",
            consequence="不授权，这个车道每一轮都会重复报同样的状态，它负责的研究一直不产出。",
            where="运行",
            detail={"lane": lane, "status": status, "reason": reason,
                    "held": value.get("held")},
        ))
    return out


def provider_failures(
    scheduler_db: Path | str | None, *, now: datetime,
    model_router_db: Path | str | None = None,
) -> list[dict[str, Any]]:
    """A provider that has been refusing work long enough to be a decision.

    Two places are consulted, in order.  If the router carries a cooldown table
    -- the model line's own record of "this credential is resting until X" --
    that is the authority and it is read.  Otherwise the Scheduler's result
    envelopes are counted, which is where a 429 actually lands: six hundred of
    them in a day is not a busy afternoon, it is a subscription limit, and the
    owner's move is a different one from "wait".
    """

    cooled = _cooldowns(model_router_db, now=now)
    if cooled:
        return cooled
    connection = _open(scheduler_db)
    if connection is None:
        return []
    if not _table(connection, "scheduler_result_envelopes"):
        connection.close()
        return []
    since = _iso(now - timedelta(hours=PROVIDER_WINDOW_HOURS))
    try:
        rows = connection.execute(
            "SELECT result_envelope_json, created_at FROM scheduler_result_envelopes "
            "WHERE outcome!='succeeded' AND created_at >= ? ORDER BY created_at",
            (since,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
    by_profile: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            envelope = json.loads(row["result_envelope_json"])
        except (TypeError, ValueError):
            continue
        code = str((envelope.get("error") or {}).get("code") or "")
        if code not in PROVIDER_ERROR_CODES:
            continue
        # Keyed by the *name* rather than the ref: the same model arrives as a
        # profile id in one envelope and as a versioned profile ref in the next,
        # and two lines about one subscription limit is one line too many.
        profile = _profile_name(_failed_profile(envelope.get("metadata") or {}))
        entry = by_profile.setdefault(
            profile, {"count": 0, "first": None, "last": None, "refs": set()})
        entry["count"] += 1
        entry["first"] = entry["first"] or str(row["created_at"])
        entry["last"] = str(row["created_at"])
        entry["refs"].add(str((envelope.get("metadata") or {}).get(
            "profile_version_ref") or ""))
    out: list[dict[str, Any]] = []
    for name, entry in sorted(by_profile.items()):
        if entry["count"] < PROVIDER_FAILURE_FLOOR:
            continue
        out.append(_item(
            "provider_rate_limited", ref=f"provider:{name}",
            at=str(entry["first"] or ""),
            title=f"模型通道一直被限流：{name}",
            why=(f"过去 {PROVIDER_WINDOW_HOURS} 小时里，这个模型通道有 "
                 f"{entry['count']} 次请求被对方以「请求过于频繁」挡回。"
                 "这不是程序出错，是账号额度用完了。"),
            action="两条路选一条：等对方的订阅额度自己重置；"
                   "或者在「模型」页把这个环节改走另一条 API 通道（需要填入该通道的密钥）。",
            consequence="不处理，需要这个模型的研究环节会持续退到备用模型或直接失败，"
                        "产出质量下降而且看不出原因。",
            where="模型",
            detail={"profile": name, "failures": entry["count"],
                    "profile_version_refs": sorted(ref for ref in entry["refs"] if ref),
                    "first_at": entry["first"], "last_at": entry["last"]},
        ))
    return out


def _failed_profile(metadata: Mapping[str, Any]) -> str:
    """Which model the provider refused, by the name the owner would recognise.

    The router's own ``excluded_profile_ids`` is preferred over the version ref
    because it is the *profile* -- ``profile:gpt-6-astra`` -- rather than a
    version of it with a content hash in the middle, and two versions of one
    profile are one problem rather than two lines on a to-do list.
    """

    retry = metadata.get("provider_retry_state")
    if isinstance(retry, Mapping):
        excluded = retry.get("excluded_profile_ids")
        if isinstance(excluded, (list, tuple)) and excluded:
            return str(excluded[0])
    return str(metadata.get("profile_version_ref") or "未标注")


def _profile_name(profile_ref: str) -> str:
    if profile_ref.startswith("profile:"):
        return profile_ref.split(":", 1)[1]
    tail = profile_ref.rsplit(":", 1)[0].rsplit(":", 1)[-1]
    for prefix in ("broker-",):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
    return tail.rsplit("-", 1)[0] if "-" in tail else tail


def _cooldowns(model_router_db: Path | str | None, *, now: datetime) -> list[dict[str, Any]]:
    """The model line's own cooldown table, if this Core has one yet.

    Probed rather than required.  The table is another work package's to add;
    this reads it the day it appears and falls back to counting failures until
    then, which is the only shape that does not need the two branches to land
    together.
    """

    connection = _open(model_router_db)
    if connection is None:
        return []
    try:
        return _read_cooldowns(connection, now=now)
    finally:
        connection.close()


def _read_cooldowns(connection: Any, *, now: datetime) -> list[dict[str, Any]]:
    for table in ("model_credential_cooldowns", "model_route_cooldowns"):
        if not _table(connection, table):
            continue
        try:
            rows = connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        except sqlite3.Error:
            continue
        out: list[dict[str, Any]] = []
        for row in rows:
            fields = {key: row[key] for key in row.keys()}
            until = str(fields.get("cooldown_until") or fields.get("until") or "")
            if until and until < _iso(now):
                continue
            name = str(fields.get("profile_id") or fields.get("credential_slot_ref")
                       or fields.get("profile_version_ref") or "未标注")
            out.append(_item(
                "provider_rate_limited", ref=f"cooldown:{name}",
                at=str(fields.get("created_at") or ""),
                title=f"模型通道正在冷却：{_profile_name(name)}",
                why=f"模型路由把这个通道标记为暂停使用，原因是"
                    f"{fields.get('reason') or '连续失败'}。",
                action="等冷却结束，或在「模型」页把这个环节改走另一条通道。",
                consequence="冷却期间这个环节会退到备用模型，产出质量可能下降。",
                where="模型",
                detail={key: str(value) for key, value in fields.items()},
            ))
        return out
    return []


def open_reopen_proposals(core: Any) -> list[dict[str, Any]]:
    """Reopen proposals that are still about the current screen version.

    The stale ones are not here, and that is D3's work: forty-seven proposals
    against superseded versions are not forty-seven decisions, they are one
    bug.  What survives the filter is a real question about a real document.
    """

    if core is None:
        return []
    from .initial_screen_reopen_hygiene import partition

    try:
        live = partition(core)["live"]
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for proposal in live:
        out.append(_item(
            "reopen_proposal", ref=str(proposal["proposal_ref"]),
            at=str(proposal["created_at"]),
            title=f"初步筛查报告是否要重出一版：{_short(str(proposal['company_ref']))}",
            why="这家公司通过初筛之后又积累了新证据，系统认为结论可能会变。",
            action="打开「待办」页，选「批准修订」让系统重出一版，或「保留当前版本」。",
            consequence="不处理，后续研究继续建立在可能已经过时的那版筛查结论上。",
            where="待办",
            detail={"company_ref": proposal["company_ref"],
                    "passed_version_ref": proposal["passed_version_ref"]},
        ))
    return out


def workspaces_without_mission(
    probes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Research environments that exist and have nothing to research.

    An environment with no active mission is not idle, it is unstarted: every
    lane in it reports "no active mission" for ever, and it will never say
    anything else without a person.
    """

    out: list[dict[str, Any]] = []
    for probe in probes:
        name = str(probe.get("name") or probe.get("slug") or "研究环境")
        core = _open(probe.get("core_db"))
        if core is None:
            continue
        try:
            has = _table(core, "coverage_mission_pointer") and core.execute(
                "SELECT 1 FROM coverage_mission_pointer LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            continue
        finally:
            core.close()
        if has:
            continue
        out.append(_item(
            "no_active_mission", ref=f"workspace:{probe.get('slug') or name}",
            at=str(probe.get("created_at") or ""),
            title=f"研究环境「{name}」还没有研究目标",
            why="这个环境里所有研究车道每一轮都报「没有生效的研究目标」，一条研究都不会开始。",
            action=f"打开「{name}」这个环境的首页，写下你要研究什么"
                   "（行业、公司范围、想回答的问题），确认发布。",
            consequence="不确认，这个环境就一直空转：占着端口和进程，不产出任何研究。",
            where="研究环境",
            detail={"slug": probe.get("slug"), "url": probe.get("url")},
        ))
    return out


def workspace_probes(manager_config_path: Path | str | None) -> list[dict[str, Any]]:
    """Where each other research environment keeps its Core.

    Derived from the workspace manager's own configuration and creation
    records; an installation without the manager has exactly one environment
    and nothing to say here.
    """

    if manager_config_path is None:
        return []
    config = _read_json(Path(manager_config_path))
    if not isinstance(config, Mapping):
        return []
    root = config.get("host_root")
    if not isinstance(root, str):
        return []
    requests = Path(root) / "creation-requests"
    if not requests.is_dir():
        return []
    probes: list[dict[str, Any]] = []
    for path in sorted(requests.glob("*.json")):
        record = _read_json(path)
        if not isinstance(record, Mapping):
            continue
        slug = str(record.get("slug") or "")
        if not slug:
            continue
        core = Path(root) / "workspaces" / slug / "state" / "dalton-core" / "core.sqlite"
        if not core.is_file():
            continue
        probes.append({"slug": slug, "name": record.get("name") or slug,
                       "url": record.get("url"), "core_db": core})
    return probes


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


def collect(
    *,
    core_db: Path | str | None = None,
    state_dir: Path | str | None = None,
    heartbeat_path: Path | str | None = None,
    governance_dir: Path | str | None = None,
    scheduler_db: Path | str | None = None,
    model_router_db: Path | str | None = None,
    workspace_manager_config_path: Path | str | None = None,
    workspaces: Sequence[Mapping[str, Any]] | None = None,
    clock: Any | None = None,
) -> dict[str, Any]:
    """Everything waiting on a person, in the order they should deal with it.

    Every argument is optional and every probe is independent.  A Core with no
    Scheduler beside it still gets its gate decisions; a state directory with no
    governance folder still gets its sources.  Partial is the normal case and
    the list says which probes ran.
    """

    now = _now(clock)
    state = None if state_dir is None else Path(state_dir).expanduser()
    if governance_dir is None and state is not None:
        governance_dir = state / GOVERNANCE_DIR_NAME
    if heartbeat_path is None and state is not None:
        candidate = state / "run" / "heartbeat.json"
        heartbeat_path = candidate if candidate.is_file() else None
    if scheduler_db is None and state is not None:
        candidate = state / "scheduler.sqlite"
        scheduler_db = candidate if candidate.is_file() else None
    if model_router_db is None and state is not None:
        candidate = state / "model-router.sqlite"
        model_router_db = candidate if candidate.is_file() else None
    if core_db is None and state is not None:
        candidate = state / "core.sqlite"
        core_db = candidate if candidate.is_file() else None

    core = _open(core_db)
    heartbeat = None if heartbeat_path is None else _read_json(Path(heartbeat_path))
    probes = list(workspaces or []) or workspace_probes(workspace_manager_config_path)

    items: list[dict[str, Any]] = []
    items += workspaces_without_mission(probes)
    items += gate_decisions(core, state_dir=state)
    items += held_lanes(heartbeat)
    items += provider_failures(scheduler_db, now=now, model_router_db=model_router_db)
    items += unconnected_sources(core)
    items += governance_records(None if governance_dir is None else Path(governance_dir))
    items += open_reopen_proposals(core)
    items += auto_returned_drafts(state)
    if core is not None:
        core.close()

    items.sort(key=lambda row: (row["urgency"], row["at"] or "", row["kind"], row["ref"]))
    items = items[:MAX_ITEMS]
    actionable = [row for row in items if row["actionable"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": _iso(now),
        "count": len(actionable),
        "total": len(items),
        "items": items,
        "by_kind": _counts(items),
        "headline": _headline(actionable),
    }


def _counts(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[str(item["kind"])] = out.get(str(item["kind"]), 0) + 1
    return dict(sorted(out.items()))


def _headline(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "目前没有需要你处理的事。"
    first = items[0]
    if len(items) == 1:
        return f"有 1 件事需要你处理：{first['title']}"
    return (f"有 {len(items)} 件事需要你处理，最要紧的一件是：{first['title']}")


def _short(company_ref: str) -> str:
    return company_ref.rsplit(":", 1)[-1] or company_ref


__all__ = [
    "APPROVED_GOVERNANCE_STATUSES",
    "GOVERNANCE_DIR_NAME",
    "HELD_LANE_STATUSES",
    "KINDS",
    "MAX_ITEMS",
    "PROVIDER_ERROR_CODES",
    "PROVIDER_FAILURE_FLOOR",
    "PROVIDER_WINDOW_HOURS",
    "SCHEMA_VERSION",
    "URGENCY",
    "URGENCY_LABELS",
    "auto_returned_drafts",
    "collect",
    "gate_decisions",
    "governance_records",
    "held_lanes",
    "open_reopen_proposals",
    "provider_failures",
    "unconnected_sources",
    "workspace_probes",
    "workspaces_without_mission",
]
