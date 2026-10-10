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
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .terminology import label as _term
from .terminology import scrub

SCHEMA_VERSION = "0.1"

# The kinds of thing that can be waiting.  Closed, because an open vocabulary
# here would make the page's ordering an accident.
KINDS: tuple[str, ...] = (
    "no_active_mission",
    "empty_workspace",
    "gate_decision",
    "memo_decision",
    "controlled_recovery",
    "provider_rate_limited",
    "source_not_connected",
    "governance_record",
    "reopen_proposal",
    "gate_auto_returned",
    "raw_spool_capacity",
    "fetch_failure_capped",
    "data_foundation_stalled",
    "scenario_decision",
    "publication_route_blocked",
    "model_cooldown",
    "earnings_release_unread",
    "connector_login_expired",
    "publication_parked",
    # Batch E: the material acquisition service's only three doors.
    "material_sources_exhausted",
    "material_budget_exceeded",
    "material_credentials_needed",
    "model_coverage_gap",
)
# Blast radius, smallest number first.
URGENCY: Mapping[str, int] = MappingProxyType({
    "no_active_mission": 1,
    "empty_workspace": 3,
    "gate_decision": 1,
    # The last human gate before active coverage: until it is decided the
    # company's research never becomes a formal view.
    "memo_decision": 1,
    "controlled_recovery": 2,
    "provider_rate_limited": 2,
    "source_not_connected": 3,
    "governance_record": 3,
    "reopen_proposal": 4,
    "gate_auto_returned": 5,
    "raw_spool_capacity": 2,
    "fetch_failure_capped": 4,
    # Holds the company's whole pipeline: no screen, no dossier, no gate,
    # no events, no debate map.
    "data_foundation_stalled": 1,
    # ADR-0012: a scenario proposal holds nothing up -- until it is adopted
    # its triggers are only observed -- but nobody else can adopt it.
    "scenario_decision": 4,
    # Batch w: the owner sees no final text for anything the verifier must
    # check until the model selection changes -- a whole stage stopped.
    "publication_route_blocked": 2,
    # 2026-10-07: a cooled model takes a link out of every chain it is in;
    # the stage keeps running on the next link, or stops if there is none.
    "model_cooldown": 2,
    # ADR-0015: one quarter's preliminary numbers are missing until the
    # 10-Q/10-K lands; everything else keeps running.
    "earnings_release_unread": 4,
    # 2026-10-08: a host-tool source refuses every call until the owner
    # re-logs in; its searches and acquisitions are all paused.
    "connector_login_expired": 2,
    # 2026-10-09: one product (or one batch of interface text) whose Chinese
    # final text gave up and that no automatic retry will reopen.
    "publication_parked": 4,
    # Batch E: every source tried and the material is still missing -- the
    # product is drafted with the gap named; nothing is stopped.
    "material_sources_exhausted": 5,
    # Batch E: external sources paused for the day (or for one request)
    # until the owner approves the named extra spend.
    "material_budget_exceeded": 3,
    # Batch E: a source the ladder needs refuses until someone signs in or
    # approves it; the other sources keep going.
    "material_credentials_needed": 3,
    "model_coverage_gap": 4,
})
# Batch am (text evaluation 7.1): the priority words come from the terminology
# registry, so the page, Feishu and the brief all say the same thing.
URGENCY_LABELS: Mapping[int, str] = MappingProxyType({
    level: _term("urgency", str(level)) for level in range(1, 6)
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

# The document research lane's escalated states -- the ones it reaches only
# after it has already spent its own bounded automatic retry.  Matched as a
# substring because the lane sends either the bare reason or the long owner
# sentence that contains it.
CONTRACT_RETRIED_REASON = "contract_failed_after_automatic_retry"
UNPROVED_SEND_RETRIED_REASON = "unproved_send_failed_after_automatic_retry"
REENTRY_REBOUND_REASON = "reentry_failed_after_automatic_rebind"
# Not retried at all: a proved paid call whose provider usage broke the
# WorkOrder budget would break it again on the same route.
PROVIDER_BUDGET_REFUSED_REASON = "provider_budget_exceeded_not_retried"
# 2026-10-06: the lane now retries that once under a corrected ceiling (the
# provider's own count plus headroom); these two are what the correction could
# not settle.  The hold reason carries ``input=<used>><admitted>`` and
# ``need=<tokens>``; the only useful owner action is a withdrawal.
PROVIDER_BUDGET_CORRECTED_REASON = "provider_budget_exceeded_after_corrected_retry"
PROVIDER_BUDGET_CORRECTION_REFUSED_REASON = "provider_budget_exceeded_correction_refused"
ESCALATED_HOLD_REASONS: tuple[str, ...] = (
    CONTRACT_RETRIED_REASON, UNPROVED_SEND_RETRIED_REASON, REENTRY_REBOUND_REASON,
    PROVIDER_BUDGET_CORRECTED_REASON, PROVIDER_BUDGET_CORRECTION_REFUSED_REASON,
)
# The lanes that keep their own durable hold ledger beside their tickets, and
# the directory each keeps it in.  Read directly, because a tick result only
# describes the one admission the lane touched this pass.
HOLDS_LEDGER_FILE = "holds.json"
LANE_HOLD_LEDGERS: Mapping[str, str] = MappingProxyType({
    "mission_document_research": "mission-document-research-runs",
    "mission_annual_research": "mission-annual-research-runs",
})

# 2026-09-30 (P1-5): the lane's hold reasons, in the owner's language.  Ordered
# because they are matched as substrings and a rebind reason can carry
# ``controlled_reentry_unavailable`` inside it.  An unknown reason is shown as
# the lane wrote it -- a wrong translation is worse than the lane's own words.
# 2026-10-03: the cause the document lane appends to an escalated unproved
# send (``mission_document_research_lane.UNPROVED_SEND_CAUSES``).  Matched
# before the bare reason, so the owner reads what the failure actually was.
PREFLIGHT_REFUSED_CAUSE = "not_sent_host_preflight_refused"
EMPTY_REPLY_CAUSES: tuple[str, ...] = (
    "empty_reply_output_cap_exhausted", "empty_or_invalid_reply",
)
PROVIDER_NOTHING_CAUSE = "provider_returned_nothing"
# 2026-10-09: the words live in the terminology registry (``hold_reason``);
# this is only the order the markers are matched in -- the causes a reason
# carries before the bare reasons that contain them.
_HOLD_REASON_MARKERS: tuple[tuple[str, str], ...] = (
    (PREFLIGHT_REFUSED_CAUSE, PREFLIGHT_REFUSED_CAUSE),
    ("empty_reply_output_cap_exhausted", "empty_reply_output_cap_exhausted"),
    ("empty_or_invalid_reply", "empty_or_invalid_reply"),
    (PROVIDER_NOTHING_CAUSE, PROVIDER_NOTHING_CAUSE),
    (CONTRACT_RETRIED_REASON, CONTRACT_RETRIED_REASON),
    (UNPROVED_SEND_RETRIED_REASON, UNPROVED_SEND_RETRIED_REASON),
    (REENTRY_REBOUND_REASON, REENTRY_REBOUND_REASON),
    (PROVIDER_BUDGET_CORRECTED_REASON + "_final", PROVIDER_BUDGET_CORRECTED_REASON + "_final"),
    (PROVIDER_BUDGET_CORRECTED_REASON, PROVIDER_BUDGET_CORRECTED_REASON),
    (PROVIDER_BUDGET_CORRECTION_REFUSED_REASON, PROVIDER_BUDGET_CORRECTION_REFUSED_REASON),
    (PROVIDER_BUDGET_REFUSED_REASON, PROVIDER_BUDGET_REFUSED_REASON),
    ("automatic_unproved_send_retry_day_cap_reached", "automatic_unproved_send_retry_day_cap_reached"),
    ("controlled_reentry_unavailable", "controlled_reentry_unavailable"),
    ("send_state_unproved", "send_state_unproved"),
    ("withdrawn_by_owner", "withdrawn_by_owner"),
    ("withdrawn_after_deadline", "withdrawn_after_deadline"),
    ("no unstarted document research admission", "no_unstarted_admission"),
)
HOLD_REASON_LABELS: tuple[tuple[str, str], ...] = tuple(
    (marker, _term("hold_reason", key)) for marker, key in _HOLD_REASON_MARKERS)
#: The fresh-work caps (``mission_document_research_executor``): a bound on
#: automatic retries, labelled with the failure behind them.
FRESH_WORK_CAP_REASONS: tuple[str, ...] = (
    "fresh_work_recovery_exhausted", "fresh_work_recovery_deadline_exceeded",
    "fresh_work_recovery_day_window_exceeded", "fresh_work_recovery_disabled",
)
ALL_MODELS_UNAVAILABLE = "all_models_unavailable"
HELD_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    "recovery_required": "等待受控恢复授权",
    "not_permitted": "当前治理规则尚未授权",
    "ungranted": "缺少任务授权",
    "resumed": "本轮在处理另一条研究任务",
})
# The lanes this module can name without importing the cockpit.  Any other
# lane is named by the cockpit's registry table when it can be imported.
_LANE_NAMES: Mapping[str, str] = MappingProxyType({
    "mission_document_research": "基于已获取原文开展专题研究",
    "mission_annual_research": "基于已获取年报开展专题研究",
})
# Where the owner deals with these, as a real view of the cockpit page.  The
# page has six views (workspace / goal / steer / log / ask / approve); "运行"
# was none of them.
APPROVE_VIEW = "approve"
APPROVE_VIEW_LABEL = "待办审批"
# 2026-10-07 (BUG-09): every "在哪里做" names something the page really shows.
# The cockpit has no 运行状态 / 来源 / 模型 / 待办 page; the nav labels are the
# six views below, and 资料来源 / 模型与预算 are dialogs opened from the
# 研究目标与计划 view (``where_open`` names the dialog, the page opens it).
GOAL_VIEW = "goal"
GOAL_VIEW_LABEL = "研究目标与计划"
WORKSPACE_VIEW = "workspace"
WORKSPACE_VIEW_LABEL = "研究环境"
SOURCES_DIALOG = "sources"
SOURCES_DIALOG_LABEL = "资料来源"
MODELS_DIALOG = "models"
MODELS_DIALOG_LABEL = "模型与预算"
# 2026-10-09: where a held research task is settled in the cockpit -- the
# administrator page's 运行控制 tab (负责人放行 / 撤回), and the card itself.
ADMIN_RUN_WHERE = "本卡片，或「管理员设置 → 运行控制」"
ADMIN_RUN_HREF = "/admin#run"
# Where nothing on the page can do it: a person at the machine has to.
TECHNICAL_ADMIN_WHERE = "交给技术管理员在本机处理（命令见技术详情）"
NO_ACTION_WHERE = "无需操作，系统会自动重试"
# The owner reads times in this zone everywhere else on the page.
OWNER_TIMEZONE = "Asia/Shanghai"
# Where the per-admission CLI door lives.  The withdraw door exists only for
# document research admissions (2026-09-30).
WITHDRAW_LANES = frozenset({"mission_document_research"})

MAX_ITEMS = 200

# Which research environment a list belongs to.  An installation can run
# several -- the legacy environment plus one Core per workspace -- and each one
# serves its own copy of this page.  "研究环境「X」还没有研究目标" is an errand
# for the people looking at X, and putting it on every other environment's list
# turns a to-do list into a noticeboard about the neighbours.  The scope is the
# workspace slug, or this constant for the legacy environment.
LEGACY_ENVIRONMENT = "legacy"
RAW_SPOOL_DEFAULT_MAX_TOTAL_BYTES = 1_000_000_000
RAW_SPOOL_WARNING_RATIO = 0.9


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
          actionable: bool = True, where_view: str | None = None,
          where_open: str | None = None) -> dict[str, Any]:
    place: dict[str, Any] = {}
    if where_view is not None:
        place["where_view"] = where_view
    if where_open is not None:
        place["where_open"] = where_open
    return place | {
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


def _gigabytes(value: int) -> str:
    """Bytes as the owner reads them: ``3758735605`` -> ``3.76 GB``."""

    for unit, size in (("GB", 1_000_000_000), ("MB", 1_000_000), ("KB", 1_000)):
        if value >= size:
            return f"{value / size:.2f} {unit}"
    return f"{value} 字节"


def _duration(seconds: int) -> str:
    """Seconds as days and hours: ``604800`` -> ``7 天``."""

    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    if days and hours:
        return f"{days} 天 {hours} 小时"
    if days:
        return f"{days} 天"
    if hours:
        return f"{hours} 小时"
    return f"{max(1, rest // 60)} 分钟"


def _local_time(value: Any) -> str:
    """An ISO instant as 年月日 时:分 in the owner's zone; unparseable text unchanged."""

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    text = str(value or "")
    try:
        instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    try:
        local = instant.astimezone(ZoneInfo(OWNER_TIMEZONE))
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata ships with the runtime
        local = instant.astimezone(timezone(timedelta(hours=8)))
    return f"{local.year}年{local.month}月{local.day}日 {local:%H:%M}"


# 2026-10-07 (BUG-09): the acquisition lane's failure reasons in the owner's
# words.  Matched as substrings, first hit wins; the original stays in the
# item's detail (技术详情).  An unknown reason is not guessed at.
FETCH_REASON_LABELS: tuple[tuple[str, str], ...] = (
    ("PublicTransportResponseTooLarge", "目标文件过大，超出单次下载上限"),
    ("exceeded the authority deadline", "连接超时，稍后自动重试"),
    ("PublicTransportNetworkError", "网络请求失败，没能连上目标网站"),
    ("empty response body", "目标网站返回了空内容"),
    ("HTTP 429", "目标网站限制访问频率（HTTP 429）"),
    ("HTTP 503", "目标网站暂时不可用（HTTP 503）"),
    ("HTTP 404", "目标网页不存在（HTTP 404）"),
    ("HTTP 403", "目标网站拒绝访问（HTTP 403）"),
    ("HTTP 402", "目标网站要求付费才能访问（HTTP 402）"),
    ("HTTP 400", "目标网站拒绝了这次请求（HTTP 400）"),
    ("database is locked", "本机数据库正忙，这次结果没能写入"),
    ("LeaseExpired", "处理超时，迟到的结果被作废"),
    ("acquisition ended orphaned", "获取进程意外中断"),
    ("acquisition ended failed", "获取进程异常退出"),
    ("manifest body_bytes", "下载结果记录不完整"),
    ("PublicWebAuthorityConflict", "搜索结果与引用来源对不上"),
)


def fetch_reason_label(reason: Any) -> str:
    text = str(reason or "")
    for needle, label in FETCH_REASON_LABELS:
        if needle in text:
            return label
    return "获取失败（原始原因见技术详情）"


def raw_spool_capacity(state_dir: Path | None, *, now: datetime) -> list[dict[str, Any]]:
    """Warn before the connector spool reaches its configured hard bound."""

    if state_dir is None:
        return []
    from .raw_spool import RawSpoolError, RawSpoolReader, effective_capacity

    spool_dir = state_dir / "connector-spool"
    try:
        reader = RawSpoolReader(spool_dir)
        used = reader.total_bytes()
    except (OSError, RawSpoolError):
        return []
    # The same resolution a write is held to -- environment, then the ceiling
    # configured beside the spool, then the built-in default.  Reading only the
    # environment here is how, on 2026-09-23, this item quoted 1 GB and "111%"
    # while every running service was actually bounded at the configured 4 GB.
    try:
        capacity = effective_capacity(
            spool_dir, default_max_total_bytes=RAW_SPOOL_DEFAULT_MAX_TOTAL_BYTES
        )
        limit = capacity.max_total_bytes
        source = capacity.source
        archive_after_seconds = capacity.archive_after_seconds
    except (OSError, RawSpoolError):
        limit = RAW_SPOOL_DEFAULT_MAX_TOTAL_BYTES
        source = "unreadable_policy"
        archive_after_seconds = None
    if limit < 1:
        return []
    over_ceiling = used >= limit
    if not over_ceiling and used / limit < RAW_SPOOL_WARNING_RATIO:
        return []
    percent = used * 100 // limit
    overflow = max(0, used - limit)
    archiving = (
        f"；超过上限时会先自动把 {_duration(archive_after_seconds)}以前的旧资料压缩归档，"
        "只有归档也腾不出空间才会拒绝"
        if archive_after_seconds is not None
        else "；这个环境没有配置自动归档，因此超过上限会直接拒绝"
    )
    source_label = {"environment": "环境变量", "configured": "环境配置",
                    "default": "系统默认值"}.get(source, "读不到配置，按默认值")
    usage = f"{_gigabytes(used)} / {_gigabytes(limit)}（{percent}%）"
    if over_ceiling:
        title = f"连接器原始资料存储已超过上限 {_gigabytes(overflow)}（{usage}）"
        why = (
            f"原始资料存储已使用 {usage}，超过当前上限 {_gigabytes(overflow)}"
            f"（上限来自{source_label}）。"
            "受影响的只有需要新保存一份原始资料的抓取——取回的内容存不下，那一次抓取失败；"
            "已保存资料的读取、以及不需要保存原文的工作照常进行，"
            "所以其它任务仍会显示正在运行" + archiving + "。"
        )
    else:
        title = f"连接器原始资料存储已用到上限的 {percent}%"
        why = (
            f"原始资料存储已使用 {usage}（上限来自{source_label}）；"
            "到达上限之后，需要新保存原始资料的抓取会被拒绝，"
            "已保存资料的读取不受影响" + archiving + "。"
        )
    import shlex
    archive_age = archive_after_seconds if archive_after_seconds is not None else 604_800
    commands = [{
        "label": (f"腾出空间：把 {_duration(archive_age)}以前的原始资料无损压缩归档"
                  "（不删除任何资料，读取不受影响）"),
        "apply": " ".join([
            "python", "-m", "dalton_core.raw_spool_maintenance", "archive",
            "--spool-dir", shlex.quote(str(spool_dir)),
            "--max-total-bytes", str(limit), "--min-age-seconds", str(archive_age)]),
    }]
    return [_item(
        "raw_spool_capacity",
        ref="raw-spool:capacity",
        at=_iso(now),
        title=title,
        why=why,
        action=(
            f"请技术管理员腾出空间：把 {_duration(archive_age)}以前的原始资料无损压缩归档"
            "（不删除任何资料，命令见技术详情）；如果磁盘够用，也可以请他调高这个环境的"
            "存储上限（在环境配置里设定，重启该环境的服务后生效）。"
            f"已经交代下去、暂时不想再看到，可以点本卡片上的「{_term('ack', 'noted')}」或「{_term('ack', 'snooze')}」："
            "用量跨入更高一档（95% / 98% / 超过上限）时会重新提醒。"
        ),
        consequence=(
            "不处理，下一次需要新保存原始资料的抓取会失败，"
            "依赖这些来源的公司就停在缺原始证据上；其余工作不受影响。"
        ),
        where=TECHNICAL_ADMIN_WHERE,
        detail={
            "commands": commands,
            "raise_ceiling": (
                "raw_spool.max_total_bytes in service.json, then re-render the LaunchAgent "
                "and restart; DALTON_RAW_SPOOL_MAX_TOTAL_BYTES only for a one-off command"),
            "used_bytes": used,
            "max_total_bytes": limit,
            "percent": percent,
            "over_ceiling": over_ceiling,
            "overflow_bytes": overflow,
            "ceiling_source": source,
            "archive_after_seconds": archive_after_seconds,
        },
    )]


def capped_fetches(core: Any, *, now: datetime) -> list[dict[str, Any]]:
    """Documents whose hourly retry was stopped by the failure cap (P2-5).

    The acquisition lane stops retrying a document after
    ``FETCH_FAILURE_CAP`` failures in a row and rests it for
    ``FETCH_FAILURE_COOLDOWN``.  That keeps the budget from being spent on the
    same failure every hour, and it means nobody would notice the document is
    missing unless it is listed here.
    """

    if core is None or not _table(core, "coverage_mission_acquisition_attempts"):
        return []
    from .coverage_mission import FETCH_FAILURE_CAP, capped_fetch_failures

    try:
        rows = [dict(row) for row in core.execute(
            "SELECT attempt_ref,source_ref,document_ref,host,outcome,created_at "
            "FROM coverage_mission_acquisition_attempts")]
        capped = capped_fetch_failures(rows, as_of=now)
    except (sqlite3.Error, ValueError, KeyError):
        return []
    documents = _table(core, "coverage_mission_discovered_documents")
    labels = _company_labels(core) if capped else {}
    out: list[dict[str, Any]] = []
    for entry in capped:
        latest = None
        if documents:
            latest = core.execute(
                "SELECT company_ref, failure_reason FROM coverage_mission_discovered_documents "
                "WHERE source_ref=? AND document_ref=? ORDER BY updated_at DESC LIMIT 1",
                (entry["source_ref"], entry["document_ref"])).fetchone()
        company = (_company_label(labels, latest["company_ref"])
                   if latest and latest["company_ref"] else "某家公司")
        reason = (str(latest["failure_reason"]) if latest and latest["failure_reason"]
                  else str(entry["last_outcome"]))
        shown_reason = _fetch_reason_text(reason)
        host = entry.get("host") or "未知站点"
        sec = str(entry["document_ref"]).startswith("sec:filing:") or host.endswith("sec.gov")
        too_large = "ResponseTooLarge" in reason
        out.append(_item(
            "fetch_failure_capped", ref=f"fetch-cap:{entry['document_ref']}",
            at=entry["last_failure_at"],
            title=(f"{company} 的一份资料连续 {entry['consecutive_failures']} 次没取回来，"
                   f"已暂停重试（{host}）"),
            why=(f"{host} 上的这份资料连续 {entry['consecutive_failures']} 次获取失败，"
                 f"最近一次原因：{fetch_reason_label(reason)}。原先每小时重试一次，每次都同样失败；"
                 f"达到连续 {FETCH_FAILURE_CAP} 次后暂停，到 {_local_time(entry['until'])} 再试一次。"),
            action=((("这是 SEC 申报文件，正文超过了网页抓取的大小上限；年报正文只走这一条通道，"
                      "所以这家公司的年报会一直缺着。这是代码上限，不是研究判断：部署放宽 SEC "
                      "原文大小上限的版本后，冷却结束会自动重取。")
                     if too_large else
                     ("这是 SEC 申报文件，网页抓取取不回。年报正文只走这一条通道，"
                      "请确认 SEC 站点可达；不需要这份文件的话可以不管。")) if sec else
                    (("这份网页超过了单次下载的大小上限，每次重试都会同样失败。"
                      if too_large else "")
                     + f"确认这份资料是否仍然需要：不需要就点本卡片上的「{_term('ack', 'abandon_document')}」"
                       "（系统不再获取它，卡片随之消失）；需要的话请技术管理员换一个来源或人工取回原文；"
                       f"只是暂时不想看到这条提醒，点「{_term('ack', 'noted')}」或「{_term('ack', 'snooze')}」"
                       "（再失败一次会重新提醒）。")),
            consequence=("不处理不会再每小时消耗额度，只是这份原文一直缺着；"
                         "冷却结束后会再试一次，仍失败就再暂停。"),
            # A SEC body over the size cap waits for a code change and retries by
            # itself; anything else is a sourcing question, answered where the
            # sources are listed.
            where=NO_ACTION_WHERE if sec and too_large else SOURCES_DIALOG_LABEL,
            where_view=None if sec and too_large else GOAL_VIEW,
            where_open=None if sec and too_large else SOURCES_DIALOG,
            detail={"source_ref": entry["source_ref"], "document_ref": entry["document_ref"],
                    "host": entry.get("host"),
                    "consecutive_failures": entry["consecutive_failures"],
                    "last_outcome": entry["last_outcome"],
                    "last_failure_at": entry["last_failure_at"],
                    "resting_until": entry["until"], "reason": reason[:500]},
        ))
    return out


def model_coverage_gaps(core: Any) -> list[dict[str, Any]]:
    from .model_coverage_status import model_coverage_status

    labels = _company_labels(core)
    return [_item(
        "model_coverage_gap", ref=f"model-coverage:{ref}", at=status["as_of"],
        title=f"{_company_label(labels, ref)} 的财务预测尚未就绪",
        why=status["label"],
        action="打开公司研究查看预测缺口与研究进度；这条提醒不需要审批，也不会阻止其他研究。",
        consequence="财务预测就绪前，依赖模型的情景估值不可用。",
        where="公司研究", detail=status, actionable=False,
    ) for ref, status in model_coverage_status(core).items() if status["status"] == "blocked"]


def earnings_release_failures(core: Any) -> list[dict[str, Any]]:
    """ADR-0015: an earnings release whose Exhibit 99.1 could not be read.

    One card per filing, and only while its quarter is not yet filed: once
    the 10-Q/10-K is ingested the filed numbers are what every reader uses
    and the preliminary ones no longer matter.
    """

    if core is None or not _table(core, "earnings_release_reads"):
        return []
    from .earnings_release_intake import (
        EXHIBIT_NOT_FOUND_REASON, failed_reads, filed_quarters, fiscal_year_end_month,
    )

    try:
        failures = failed_reads(core)
    except (sqlite3.Error, ValueError, KeyError):
        return []
    labels = _company_labels(core) if failures else {}
    out: list[dict[str, Any]] = []
    for read in failures:
        company_ref = read["company_ref"]
        try:
            filed = filed_quarters(core, company_ref, fiscal_year_end_month(core, company_ref))
        except (sqlite3.Error, ValueError):
            filed = {}
        if read.get("period") and read["period"] in filed:
            continue
        company = _company_label(labels, company_ref)
        unavailable = read["status"] == "exhibit_unavailable"
        out.append(_item(
            "earnings_release_unread", ref=f"earnings-release:{company_ref}:{read['accession']}",
            at=read.get("created_at"),
            title=(f"{company} 的业绩稿（8-K {read['accession']}，{read.get('period') or '财季未知'}）"
                   + ("没取到附件 99.1" if unavailable else "没能自动读出利润表")),
            why=(f"这份 8-K（Item 2.02，{read.get('report_date') or '日期未知'}）"
                 f"{'的附件 99.1 没有取回' if unavailable else '的附件 99.1 已取回，但利润表无法确定地读取'}："
                 f"{read.get('reason') or '原因未记录'}。系统不会猜数字，所以这一季没有初步业绩。"),
            action=((("系统会按这份申报自带的文档类型表（EX-99 / EX-99.1）重新查找新闻稿附件，"
                      "找到后自动读出初步业绩，这张卡随之消失；不需要你操作。")
                     if read.get("reason") == EXHIBIT_NOT_FOUND_REASON else "")
                    + f"可以打开 {read.get('exhibit_url') or 'SEC 上这份 8-K'} 自己看这一季的数字；"
                    "10-Q/10-K 申报数入库后也会自动补上，这张卡会消失。"
                    f"暂时不想看到这条提醒，点「{_term('ack', 'noted')}」或「{_term('ack', 'snooze')}」。"),
            consequence="不处理不会影响其他研究，只是这一季在申报数到达前没有初步业绩、初步对账和业绩事件。",
            where="SEC 上的这份 8-K 原文（链接见详情）",
            detail={"company_ref": company_ref, "accession": read["accession"],
                    "document_ref": read.get("document_ref"), "exhibit_url": read.get("exhibit_url"),
                    "status": read["status"], "reason": str(read.get("reason") or "")[:500],
                    "period": read.get("period")},
        ))
    return out


def stalled_foundations(state_dir: Path | None, *, now: datetime) -> list[dict[str, Any]]:
    """Companies whose Initial Screen has waited on its data foundation too long.

    2026-10-02 (ws-7d): four companies waited fifteen days with the drafting
    lane reporting ``idle`` every hour.  The lane now writes *why* each missing
    reading is not arriving into its summary; this lists the companies that
    have waited past ``data_foundation.ESCALATE_AFTER``, one item each.  A
    company whose every gap will close by itself is listed as a notice, not as
    a to-do: the owner should know the screen is late, not be asked to act.
    """

    from .data_foundation import ESCALATE_AFTER, latest_summary, overdue

    summary = latest_summary(state_dir)
    out: list[dict[str, Any]] = []
    for block in overdue(summary, now=now):
        ticker = str(block.get("ticker") or _short(str(block.get("company_ref") or "")))
        items = [item for item in block.get("items") or () if isinstance(item, Mapping)]
        owner_items = [item for item in items if not item.get("automatic")]
        missing = "、".join(str(item.get("label")) for item in items)
        actions: list[str] = []
        for item in owner_items or items:
            line = f"{item.get('label')}：{item.get('action')}"
            if line not in actions:
                actions.append(line)
        why = "；".join(str(item.get("why")) for item in items)
        out.append(_item(
            "data_foundation_stalled", ref=f"foundation:{block.get('company_ref')}",
            at=str(block.get("waiting_since") or summary.get("created_at") or ""),
            title=(f"{ticker} 的初筛已经等了 {block.get('days_waiting')} 天：资料底座缺 {missing}"),
            why=(f"初筛要先读齐资料底座才起草，{ticker} 缺的几项一直没有进来：{why}。"
                 "初筛不过，公司档案、深度认知门、事件判断和多空地图都没有事可做。")[:1500],
            action="；".join(actions)[:1500],
            consequence=("这家公司一直停在立项初筛，后面每个环节都不会开始；"
                         "系统每小时检查一次，资料一齐就会自动起草。"),
            where=GOAL_VIEW_LABEL,
            where_view=GOAL_VIEW,
            actionable=bool(owner_items),
            detail={"company_ref": block.get("company_ref"),
                    "waiting_since": block.get("waiting_since"),
                    "days_waiting": block.get("days_waiting"),
                    "escalate_after_days": ESCALATE_AFTER.days,
                    "items": [{key: item.get(key) for key in (
                        "item_ref", "label", "have", "required", "cause", "automatic",
                        "missing_periods", "candidate_periods") if key in item}
                        for item in items]},
        ))
    return out


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
    labels = _company_labels(core) if rows else {}
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        dossier = dossier_for(core, record)
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
            title=f"深度认知评审等你裁决：{_company_label(labels, row['company_ref'])}"
                  f"（第 {row['version_number']} 版）",
            why=("深度认知门是六个研究阶段之间唯一的人闸。在你给出结论之前，"
                 "这家公司的估值、投资论点、备忘录都不会开始。" + f"当前草稿：{summary}"),
            action="打开「待办审批」页，读完十二问，选「通过」「退回补充」或「否决」。"
                   "选「退回补充」时请写清楚哪几问不行——系统会把你的原话带进下一版。",
            consequence="不裁决，这家公司就一直停在第一阶段之后，后面五个阶段一步都不会走。",
            where=APPROVE_VIEW_LABEL,
            where_view=APPROVE_VIEW,
            detail={"company_ref": row["company_ref"],
                    "version": row["version_number"],
                    "answered": answered,
                    "unknown": counts.get("unknown"),
                    "evidence_refs": counts.get("evidence_refs"),
                    "quality_summary": summary,
                    "submittable": (quality or {}).get("submittable")},
        ))
    return out


def memo_decisions(core: Any) -> list[dict[str, Any]]:
    """Verified Investment Memo drafts waiting for the owner (ADR-0011).

    The current head of each company's memo chain, under any version of the
    active mission, that no ``investment_memo`` stage decision cites yet.  The
    approvals page carries the memo itself and its two buttons; this item is
    what makes it show up on the one list the owner reads every day.
    """

    if core is None or not all(_table(core, name) for name in (
            "mission_deliverable_pointer", "mission_deliverable_versions",
            "coverage_mission_pointer", "coverage_mission_versions",
            "coverage_mission_stage_records")):
        return []
    try:
        rows = core.execute(
            "SELECT v.version_id, v.record_json, v.created_at "
            "FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "JOIN coverage_mission_versions m ON m.mission_version_id=v.mission_version_ref "
            "JOIN coverage_mission_pointer a ON a.mission_ref=m.mission_ref "
            "WHERE v.kind='investment_memo' ORDER BY v.created_at, v.version_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    labels = _company_labels(core) if rows else {}
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        decided = core.execute(
            "SELECT 1 FROM coverage_mission_stage_records WHERE stage_ref='investment_memo' "
            "AND status IN ('gate_passed','gate_failed') AND company_ref=? "
            "AND record_json LIKE ?",
            (record.get("subject_ref"), f"%{row['version_id']}%"),
        ).fetchone()
        if decided is not None:
            continue
        gate = record.get("gate") or {}
        verdict = (gate.get("verifier") or {}).get("verdict") or "缺失"
        who = _company_label(labels, record.get("subject_ref"))
        out.append(_item(
            "memo_decision", ref=str(row["version_id"]), at=str(row["created_at"]),
            title=f"投资备忘录等你裁决：{who}",
            why=("这家公司已经走完行业模型和公司模型两道门，备忘录草稿的十二问、"
                 f"Anti-thesis 和数字回指都已通过确定性检查，独立核验结论：{verdict}。"
                 "备忘录是进入持续覆盖之前的最后一道人闸。"),
            action="打开「待办审批」页读完备忘录，选「批准并进入持续覆盖」或「拒绝」，并写一句理由。",
            consequence="不裁决，这家公司就不会进入持续覆盖，研究结论也不会成为正式观点。",
            where=APPROVE_VIEW_LABEL,
            where_view=APPROVE_VIEW,
            detail={"company_ref": record.get("subject_ref"),
                    "memo_version_ref": row["version_id"],
                    "verifier_verdict": verdict,
                    "key_questions": len(gate.get("key_questions") or [])},
        ))
    return out


def scenario_decisions(core: Any) -> list[dict[str, Any]]:
    """ADR-0012: structured-scenario proposals waiting for the owner."""

    if core is None or not _table(core, "company_scenario_sets"):
        return []
    try:
        from .company_scenario import read_sets

        sets = [item for item in read_sets(core)
                if item["origin"] == "automation" and item["state"] in {"pending", "deferred"}]
    except Exception:  # noqa: BLE001 - an unreadable proposal is not an item
        return []
    labels = _company_labels(core) if sets else {}
    out = []
    for item in sets:
        who = _company_label(labels, item["company_ref"])
        out.append(_item(
            "scenario_decision", ref=item["id"], at=item["created_at"],
            title=f"情景假设等你裁决：{who}",
            why=(f"系统从 {who} 的研究档案整理出 {len(item.get('scenario_keys') or [])} 个情景，"
                 "每个都带概率、触发条件和否定条件，条件里的数字都能回指出处。"),
            action="打开「待办审批」页看这组情景，选「采纳」「驳回」或「暂缓」。",
            consequence=("不裁决，这些触发条件只被观察、不会交给事件研判，"
                         "也不会调整这家公司的跟踪频率；新的情景提案也会等这一组先裁决。"),
            where=APPROVE_VIEW_LABEL,
            where_view=APPROVE_VIEW,
            detail={"company_ref": item["company_ref"], "set_ref": item["id"],
                    "state": item["state"]},
        ))
    return out


def dossier_for(core: Any, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The company dossier a gate draft binds, or ``None`` if it is not there.

    Public because the approvals page asks the same question of the same draft
    and two readers of one binding should read it the same way.
    """

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


def auto_returned_drafts(state_dir: Path | None,
                         labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
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
            title=(f"{_company_label(labels or {}, note.get('company_ref'))}："
                   "草稿尚不足以提交，没有放进你的待办"),
            why=str(assessment.get("summary") or "系统判定草稿尚不足以提交。"),
            action="不用你动手。要更快推进，可以先把这家公司缺的证据补上"
                   "（下面逐题列了缺什么），或者放宽提交标准。",
            consequence="在证据变化之前，系统不会重复起草，也不会重复花模型费用；"
                        "这家公司会一直停在这里。",
            where=GOAL_VIEW_LABEL,
            where_view=GOAL_VIEW,
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
        if record.get("schema_version") == "owner-delivery-contract-0.1":
            # Vision-gap #13: not a data source but the push channel itself.
            # Quiet (not actionable): research does not wait on it, and the
            # owner turned it on or off on purpose either way.
            out.append(_item(
                "governance_record", ref=str(record.get("id") or path.stem),
                at=str(record.get("approved_at") or ""),
                title="飞书推送还没开通（周报、需要你处理、模型回退、紧急停机）",
                why="推送设置还停在「已提出」。不开通，这些消息只在投研工作台里，不会主动推给你。",
                action=("这一步没有页面按钮：请技术管理员在本机运行飞书推送开通脚本，"
                        "填上要接收消息的飞书群聊并批准（脚本见技术详情）。"),
                consequence="不开通也不影响研究；只是你得自己打开投研工作台看。",
                where=TECHNICAL_ADMIN_WHERE,
                detail={"capability_id": capability, "status": status,
                        "file": path.name,
                        "commands": [{
                            "label": "开通飞书推送（脚本会询问飞书群聊 chat id，并在批准后生效）",
                            "apply": "docs/ops/owner-delivery-enable.sh",
                        }]},
                actionable=False,
            ))
            continue
        out.append(_item(
            "governance_record", ref=str(record.get("id") or path.stem),
            at=str(record.get("effective_from") or ""),
            title=f"数据源接口等你批准：{name}",
            why=("这个接口的治理记录还停在「已提出」。没有批准，用到它的研究流程每一轮"
                 "都会空转一次，取不到任何数据。"),
            action=f"打开「研究目标与计划」页的「资料来源」，找到 {name}，看清它要的权限（网络访问、"
                   f"风险等级 {record.get('allowed_permissions', {}).get('risk_class', '未标注')}）"
                   f"，然后批准或明确拒绝。",
            consequence="不处理，这条数据通道永远不通，依赖它的研究问题答不了；"
                        "相关研究流程还会继续每轮空转。",
            where=SOURCES_DIALOG_LABEL,
            where_view=GOAL_VIEW,
            where_open=SOURCES_DIALOG,
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
        from .terminology import label
        source_name = label("research_source", ref, "任务书中指定的资料来源")
        out.append(_item(
            "source_not_connected", ref=ref, at=str(version["created_at"]),
            title=f"研究所需资料来源尚未连接：{source_name}",
            why=f"研究目标需要使用{source_name}，目前尚未连接，依赖它的研究问题暂时无法回答。",
            action=f"请管理员在「管理员设置」的「数据连接」页面连接{source_name}，并确认可以获取资料。",
            consequence="不接上，依赖这个来源的研究问题（初筛的一部分、估值、"
                        "投资判断的其中一道闸门）会一直答不了，而不是答错。",
            where=SOURCES_DIALOG_LABEL,
            where_view=GOAL_VIEW,
            where_open=SOURCES_DIALOG,
            detail={"source_ref": ref, "role": entry.get("role"),
                    "status": entry.get("status")},
        ))
    return out


def lane_hold_ledgers(
    state_dir: Path | None,
) -> dict[str, dict[str, Any]]:
    """Each lane's own hold ledger, as the lane left it.

    The ledger is the durable fact; a tick result is a snapshot of one pass.
    Live on 2026-09-18 a workspace held one escalated admission for hours and
    this list was empty the whole time, because in that tick the lane had
    resumed a *different* admission and its result therefore said "resumed".
    A thing waiting on a person does not stop waiting because the lane found
    something else to do that second.
    """

    if state_dir is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for lane, dirname in LANE_HOLD_LEDGERS.items():
        path = Path(state_dir) / dirname / HOLDS_LEDGER_FILE
        record = _read_json(path)
        holds = record.get("holds") if isinstance(record, Mapping) else None
        if not isinstance(holds, Mapping):
            continue
        waiting = []
        for admission_ref, value in sorted(holds.items()):
            if (not isinstance(value, Mapping)
                    or value.get("disposition") != "recovery_required"):
                continue
            waiting.append({
                "admission_ref": str(admission_ref),
                "reason": str(value.get("reason") or ""),
                "ticket_ref": value.get("ticket_ref"),
            })
        try:
            at = _iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        except OSError:
            at = ""
        out[lane] = {"path": str(path), "at": at, "waiting": waiting,
                     "escalated": [item for item in waiting
                                   if _escalated_reason(item["reason"])]}
    return out


def _escalated_reason(reason: str) -> bool:
    """Has the lane handed this hold to a person with a door to press?

    The lane's own automatic attempt is spent (the historical list), or the
    executor's policy offers the owner a door other than withdrawal -- since
    2026-10-09 a fresh-work cap that is a policy bound (one more round).
    """

    if any(name in reason for name in ESCALATED_HOLD_REASONS):
        return True
    try:
        from .mission_document_research_executor import available_doors
    except Exception:  # noqa: BLE001
        return False
    return any(door["door"] not in {"withdraw", "technical_help"}
               for door in available_doors({"reason": reason}))


def recovery_command(
    subcommand: str, *, state_dir: Any = "<state>", admission_ref: str = "<ref>",
    cap: str | None = None,
) -> str:
    """The exact line the owner types, with nothing left to fill in but who."""

    return (
        f"python -m dalton_core.document_recovery_cli {subcommand} "
        f"--state-dir {state_dir} --admission-ref {admission_ref}"
        + (f" --max-cost-usd {cap}" if cap else "")
        + " --apply --actor human:<owner>"
    )


def _retry_words(
    reason: str, *, state_dir: Any = "<state>", admission_ref: str = "<ref>",
) -> tuple[str, str]:
    """What the lane already tried for this reason, and what to do next.

    The ask has to say what has already been bought, or the owner authorises
    the same purchase a second time without knowing the first one happened --
    and it has to name the one command that does it.  Naming an executor
    method, as this did until 2026-09-18, is not an action: that method lives
    in a process the owner must not start.
    """

    def _line(subcommand: str, *, cap: str | None = None) -> str:
        return recovery_command(subcommand, state_dir=state_dir,
                                admission_ref=admission_ref, cap=cap)

    if CONTRACT_RETRIED_REASON in reason:
        return (
            "模型的回复格式不符合要求，系统已经按同样的预算自动重试过一次，"
            "重试的回复仍然不符合，所以需要你决定。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "先看模型或提示词为什么连续两次给不出合契约的回复——连续两次同样失败通常是"
            "契约或提示词的问题，再买一次大概率还是同一个结果。确认值得再买一次，就执行："
            + _line("authorize-paid", cap="<上限美元>")
            + "（先不加 --apply 是只读预览；--max-cost-usd 是你愿意花的上限，"  # term-ok: CLI (技术详情)
            "那一阶段自己的预算超过它就直接拒绝，不会偷偷少买。）",
        )
    if UNPROVED_SEND_RETRIED_REASON in reason and PREFLIGHT_REFUSED_CAUSE in reason:
        return (
            "核验两次都在发送前被拦下：请求没有发出，没有计费。这条研究任务立项时定下的"
            "输入上限装不下核验需要的原文摘录和起草稿，同样的内容每次都会被拦下。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "再买一次没有用。先在 Cockpit 里调高 mission_directed_document_verifier 的输入上限，"
            "再撤回这条 admission，规划器会按新预算重新立项："
            + _line("withdraw").replace(" --apply", " --reason <原因> --apply")  # term-ok: CLI (技术详情)
            + "（先不加 --apply 是只读预览。）",  # term-ok: CLI (技术详情)
        )
    if UNPROVED_SEND_RETRIED_REASON in reason and any(
            cause in reason for cause in EMPTY_REPLY_CAUSES):
        return (
            "模型连续返回空答案：推理模型把这条研究任务立项时定下的输出额度全部用在了"
            "思考上，没有写出答案，每次都已计费。按原额度再试，大概率还是空答案。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "先用 `python -m dalton_core.document_recovery_cli holds --state-dir <state>` 看它是否"
            "被标成 output_cap_exhausted_below_current_budget；是的话直接撤回，规划器会按当前"
            "（更高的）输出上限重新立项："
            + _line("withdraw").replace(" --apply", " --reason <原因> --apply")  # term-ok: CLI (技术详情)
            + "（先不加 --apply 是只读预览。）",  # term-ok: CLI (技术详情)
        )
    if UNPROVED_SEND_RETRIED_REASON in reason:
        return (
            "无法确认这次调用是否已经送达并计费，系统已经按同样的预算自动重试过一次，"
            "重试也失败了。最坏情况下这一步已经花了两次调用的钱，所以系统不再自动尝试第三次。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "先看模型或线路为什么连续两次都拿不回可用结果；确认值得再买一次，就执行："
            + _line("authorize-unproved", cap="<上限美元>")
            + "（先不加 --apply 是只读预览；--max-cost-usd 是你愿意花的上限。）",  # term-ok: CLI (技术详情)
        )
    if (PROVIDER_BUDGET_CORRECTED_REASON in reason
            or PROVIDER_BUDGET_CORRECTION_REFUSED_REASON in reason):
        return (
            "这次调用已经送达并计费，但模型实际用量超过了这条研究任务的预算上限。"
            + ("系统已按实际用量加 25% 余量调高上限，自动重试过一次，仍然超出。"
               if PROVIDER_BUDGET_CORRECTED_REASON in reason else
               "这次超出无法靠自动调高上限解决（输出或费用超限，或所需额度超过原定的 2 倍）。")
            + "具体用量和上限数字见技术详情。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "再按同样的上限买一次只会同样超出，所以不提供「再买一次」。一条命令撤回，"
            "规划器会按当前预算重新立项（重新立项的起草若再超出，会再自动修正一次）："
            + _line("withdraw").replace(" --apply", " --reason <原因> --apply")  # term-ok: CLI (技术详情)
            + "（先不加 --apply 是只读预览。）",  # term-ok: CLI (技术详情)
        )
    if PROVIDER_BUDGET_REFUSED_REASON in reason:
        return (
            "这次调用已经送达并计费，但模型实际用量超过了这条研究任务的预算，结果没有采用。"
            "这是旧版本留下的记录：当前版本会按实际用量加 25% 余量调高上限，"
            "自动重试一次（同样受当天预算限制）。",
            "不需要操作；系统下一轮会自己处理。修正后仍超出时，这里会改成带具体数字的原因并给出处理命令。",
        )
    if REENTRY_REBOUND_REASON + ":controlled_reentry_unavailable:" in reason:
        return (
            "系统两次尝试恢复这条研究任务都在同一处被拦下，没有发出模型调用，也没有花钱；"
            "原因是任务的执行凭证或工作环境的一个固定状态，需要技术管理员查看。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "先看原因里 controlled_reentry_unavailable 后面那句原文并修好它，车道每轮都会"
            "自己再检查、修好后自己继续；修好后仍停着，再执行："
            + _line("authorize-reentry")
            + "放行一次重新进入（先不加 --apply 是只读预览；重新进入不买模型调用）。",  # term-ok: CLI (技术详情)
        )
    if REENTRY_REBOUND_REASON in reason:
        return (
            "这条研究任务的后台执行凭证变过（通常是系统升级或配置变更），系统已经自动更新"
            "到新凭证并重试过一次，仍然失败，所以需要你决定。",
            # term-ok: CLI instructions for the technical administrator (技术详情)
            "先看原因里那条 LaneChildRejected 原文说的是什么（配置文件、工作区绑定、"
            "票据占用）；修好之后执行："
            + _line("authorize-reentry")
            + "放行一次重新进入。"  # term-ok: CLI (技术详情)
            "（先不加 --apply 是只读预览；`holds` 子命令列出账本里所有停着的 admission；"
            "`authorize-all-escalated --max-total-cost-usd N` 按最早停的先来，"
            "在一个总预算里一次清完。）",
        )
    code = _reason_code(reason)
    if code in {"send_state_unproved", "paid_send_output_contract_failed"}:
        return (
            "这是旧版本留下的停止记录：当前版本会对这一类失败自动重试一次（同样受当天预算限制）。",
            "不需要你操作；系统下一轮会自己处理。",
        )
    if code in FRESH_WORK_CAP_REASONS and ALL_MODELS_UNAVAILABLE in reason:
        next_at = reason.split("next=", 1)[1] if "next=" in reason else None
        return (
            "这项研究的模型调用被拒：当时所有可用的 AI 模型都暂不可用（额度用完、冷却中或授权过期），"
            "请求没有发出、没有计费。系统已按规定自动重试到次数上限，所以先停下。",
            (f"不需要你操作：今天的自动重试已经用过，系统最早在 {_local_time(next_at)} 自动再试一轮。"
             if next_at else
             "不需要你操作：一旦有模型恢复可用（冷却结束、授权续期或额度重置），系统会自动再试一轮，"
             "每天最多一次。"),
        )
    if code in FRESH_WORK_CAP_REASONS:
        return (
            "这个研究步骤失败后，系统已按规定的次数（或时间窗口）自动重试，上限已到，所以停下。"
            "这是系统的重试上限，不是对研究本身的结论；是否在当前模型设置下再试一轮由你决定。",
            f"点「{_term('recovery_door', 'retry_round')}」再试一轮（在立项时已批准的费用上限内）；"
            f"不想再试，点「{_term('recovery_door', 'withdraw')}」。",
        )
    if code in {"recovery_hint_unverifiable", "started_without_owned_live_ticket",
                "controlled_reentry_ticket_unavailable", "dispatch_error",
                "budget_or_pool_refused"}:
        return (
            "系统无法自动继续这项研究，需要技术管理员看一下原因（原文见技术详情）。",
            f"点「{_term('recovery_door', 'technical_help')}」把它交给技术管理员；"
            f"不再需要这项研究，点「{_term('recovery_door', 'withdraw')}」。",
        )
    return (
        "它不会自己重新开始。",
        f"这个原因本版本还没有对应的处理按钮：点「{_term('recovery_door', 'technical_help')}」"
        f"把技术详情里的原文交给技术管理员；不再需要这项研究，点「{_term('recovery_door', 'withdraw')}」。",
    )


def _reason_code(text: str) -> str:
    try:
        from .mission_document_research_executor import hold_reason_code
    except Exception:  # noqa: BLE001 - a label is never a failure of the list
        return text.split(":", 1)[0].strip()[:80]
    return hold_reason_code(text)


def hold_reason_label(reason: Any) -> str:
    """The owner's words for a hold reason, from the terminology registry.

    A fresh-work cap reads as the cap plus the failure behind it.  A reason
    this release has no words for reads 「其他原因：<代码>」 -- honest, and the
    full original stays in 技术详情.
    """

    text = str(reason or "").strip()
    if not text:
        return "未说明"
    code = _reason_code(text)
    if code in FRESH_WORK_CAP_REASONS:
        rest = [part for part in text[len(code):].split(":") if part]
        causes = [_term("hold_reason", part, default="") for part in rest
                  if not part.startswith("next=")]
        causes = [cause for cause in causes if cause]
        if ALL_MODELS_UNAVAILABLE in rest:
            causes = [cause for cause in causes
                      if cause != _term("hold_reason", "route_rejected_definitely_not_sent")]
        return "：".join([_term("hold_reason", code), "；".join(causes)]) if causes \
            else _term("hold_reason", code)
    for marker, label in HOLD_REASON_LABELS:
        if marker in text:
            return label
    known = _term("hold_reason", code, default="")
    return known or _term("hold_reason", "other", code=code or text[:80])


def lane_label(lane: str) -> str:
    if lane in _LANE_NAMES:
        return _LANE_NAMES[lane]
    try:
        from .cockpit_plane import REGISTRY_LANE_LABELS
    except Exception:  # noqa: BLE001 - a name is a nicety, never a failure
        return lane
    return REGISTRY_LANE_LABELS.get(lane, lane)


def _company_labels(core: Any) -> dict[str, str]:
    """company_ref -> "TICKER · Name" (or the ticker) from the active mission."""

    if core is None or not _table(core, "coverage_mission_pointer"):
        return {}
    from .cockpit_plane import COMPANY_NAMES

    labels: dict[str, str] = {}
    try:
        rows = core.execute(
            "SELECT v.record_json FROM coverage_mission_versions v "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=v.mission_version_id "
            "ORDER BY p.updated_at DESC").fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        try:
            universe = json.loads(row[0]).get("universe") or []
        except (TypeError, ValueError, AttributeError):
            continue
        for member in universe:
            if not isinstance(member, Mapping) or not member.get("company_ref"):
                continue
            ticker = str(member.get("ticker") or "").strip()
            # Same naming as the cockpit's own company label: a mission
            # member recorded without a name still reads 「CTSH · Cognizant」.
            name = str(member.get("name") or COMPANY_NAMES.get(ticker) or "").strip()
            label = (f"{ticker} · {name}" if ticker and name and name.upper() != ticker.upper()
                     else ticker or name)
            if label:
                labels.setdefault(str(member["company_ref"]), label)
    return labels


def _fetch_reason_text(reason: str) -> str:
    """A fetch failure in the owner's words; the raw text stays in ``detail``.

    「PublicTransportResponseTooLarge: response Content-Length exceeds the
    configured limit」 reads 「目标文件过大，超出单次下载上限」 through the same
    table the log and the ops page use.
    """

    from .cockpit_plane import _display_reason

    return _display_reason(reason, fallback="获取失败，原始原因见技术详情")[:200]


def _company_label(labels: Mapping[str, str], company_ref: Any) -> str:
    ref = str(company_ref or "")
    return labels.get(ref) or _short(ref) or "未知公司"


def admission_subjects(core: Any, admission_refs: Iterable[str]) -> dict[str, dict[str, Any]]:
    """What each held admission is about: its company and its research question.

    Read-only.  The hold ledger is keyed by admission hash only, which is what
    the cards used to put in their titles ("等人决定：0746fe99…") -- a name
    nobody can act on.  The admission row names the company and the question
    version, and the question version holds the question itself.
    """

    refs = sorted({str(ref) for ref in admission_refs if ref})
    if core is None or not refs or not _table(core, "mission_document_research_admissions"):
        return {}
    has_questions = _table(core, "backlog_question_versions")
    labels = _company_labels(core)
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(refs), 200):
        chunk = refs[start:start + 200]
        marks = ",".join("?" for _ in chunk)
        sql = ("SELECT a.admission_id, a.company_ref, "
               + ("q.question" if has_questions else "NULL")
               + " FROM mission_document_research_admissions a "
               + ("LEFT JOIN backlog_question_versions q ON q.version_id=a.question_version_ref "
                  if has_questions else "")
               + f"WHERE a.admission_id IN ({marks})")
        try:
            rows = core.execute(sql, chunk).fetchall()
        except sqlite3.Error:
            continue
        for admission_id, company_ref, question in rows:
            out[str(admission_id)] = {
                "company_ref": company_ref,
                "company": _company_label(labels, company_ref),
                "question": " ".join(str(question or "").split()) or None,
            }
    return out


def _summary(text: Any, limit: int = 40) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _cli(state_dir: Any, subcommand: str, *args: str) -> str:
    """One runnable line: this interpreter, the real state directory, quoted."""

    import shlex
    import sys

    return " ".join([
        shlex.quote(sys.executable or "python"), "-m", "dalton_core.document_recovery_cli",
        subcommand, "--state-dir", shlex.quote(str(state_dir)), *args,
    ])


def hold_commands(lane: str, admission_ref: str, reason: str, *,
                  state_dir: Any, door_used: bool = False) -> list[dict[str, str]]:
    """The exact commands for one escalated hold, for 技术详情 where they can be copied.

    Mirrors ``document_recovery_cli holds``: the door this reason leads to
    (unless the owner already used it -- the executor opens each door once),
    the withdraw door for a document admission, and the read-only listing.
    Every command without ``--apply`` is a dry run.
    """

    import shlex

    ref = shlex.quote(admission_ref)
    commands: list[dict[str, str]] = []
    subcommand = None
    primary = next((door["door"] for door in policy_doors(lane, reason)
                    if door["door"] not in {"withdraw", "technical_help"}), None)
    subcommand = {"paid": "authorize-paid", "unproved": "authorize-unproved",
                  "reentry": "authorize-reentry",
                  "retry_round": "authorize-reentry"}.get(str(primary))
    if subcommand is not None and not door_used:
        cap = [] if subcommand == "authorize-reentry" else ["--max-cost-usd", "<上限美元>"]
        commands.append({
            "label": (_term("recovery_door", "retry_round") if primary == "retry_round"
                      else _term("recovery_door", "reentry") if subcommand == "authorize-reentry"
                      else _term("recovery_door", "paid")),
            "preview": _cli(state_dir, subcommand, "--admission-ref", ref, *cap),
            "apply": _cli(state_dir, subcommand, "--admission-ref", ref, *cap,
                          "--apply", "--actor", "human:<owner>"),
        })
    if lane in WITHDRAW_LANES:
        commands.append({
            "label": _term("recovery_door", "withdraw"),
            "preview": _cli(state_dir, "withdraw", "--admission-ref", ref,
                            "--reason", shlex.quote("<撤回理由>")),
            "apply": _cli(state_dir, "withdraw", "--admission-ref", ref,
                          "--reason", shlex.quote("<撤回理由>"),
                          "--apply", "--actor", "human:<owner>"),
        })
    commands.append({
        "label": "只读：列出这个环境所有停着的研究任务和各自的命令",
        "preview": _cli(state_dir, "holds"),
    })
    return commands


def _doors_used(state_dir: Any) -> dict[str, list[dict[str, Any]]]:
    try:
        from .document_recovery_cli import owner_doors_used
        return owner_doors_used(state_dir)
    except Exception:  # noqa: BLE001 - a hint, never a failure of the list
        return {}


def _door_used(used: Mapping[str, Sequence[Mapping[str, Any]]],
               hold: Mapping[str, Any]) -> bool:
    """Has the owner already opened the one door this hold's reason leads to?"""

    try:
        from .document_recovery_cli import door_for
    except Exception:  # noqa: BLE001
        return False
    operation = door_for(str(hold.get("reason") or ""))
    return operation is not None and any(
        entry.get("operation") == operation
        for entry in used.get(str(hold.get("admission_ref") or ""), ()))


def policy_doors(lane: str, reason: str) -> list[dict[str, Any]]:
    """The doors the executor's policy permits for this hold (``available_doors``).

    Only the document research lane has cockpit doors; any other lane's hold
    is handed to the technical administrator.  When the policy cannot be read
    at all, withdrawal and the hand-off are still offered -- never nothing.
    """

    if lane not in WITHDRAW_LANES:
        return [{"door": "technical_help", "operation": None, "cost": "free",
                 "automatic_retry": False, "next_automatic_retry": None}]
    try:
        from .mission_document_research_executor import available_doors
    except Exception:  # noqa: BLE001 - the safe pair, never an empty card
        return [{"door": "withdraw", "operation": "withdraw_mission_document_admission",
                 "cost": "free", "automatic_retry": False, "next_automatic_retry": None},
                {"door": "technical_help", "operation": None, "cost": "free",
                 "automatic_retry": False, "next_automatic_retry": None}]
    return available_doors({"reason": reason})


_DOOR_CONFIRMS: Mapping[str, str] = MappingProxyType({
    "reentry": "{label}：系统下一轮（约 5 分钟内）按当前设置继续这项研究。确认？",
    "retry_round": ("{label}：系统下一轮（约 5 分钟内）为这个研究步骤再安排一轮重试，"
                    "在立项时已批准的费用上限内执行，不新增预算。确认？"),
    "paid": "{label}：系统在你填写的检索预算上限内重新获取资料。确认？",
    "unproved": "{label}：系统在你填写的检索预算上限内重新获取资料。确认？",
    "withdraw": "{label}：这项专项课题不再继续，现有研究底稿和记录保留；需要时可以重新发起同一课题。确认？",
})


def hold_doors(lane: str, admission_ref: str, reason: str, *,
               door_used: bool = False) -> list[dict[str, Any]]:
    """The cockpit doors one held task offers, in the order to try them.

    Exactly the doors :func:`policy_doors` permits (the executor's
    ``available_doors``), sent from the page as the owner's own principal
    (``cockpit_owner_doors``); the primary door is dropped once the owner has
    used it (the executor opens each paid door once).  Withdrawal is last.
    The hand-off to the technical administrator is not a button that writes
    anything, so it is not here (see :func:`hold_handoff`).
    """

    from .cockpit_owner_doors import DOOR_LABELS

    doors: list[dict[str, Any]] = []
    for door in policy_doors(lane, reason):
        name = door["door"]
        if name == "technical_help" or name not in DOOR_LABELS:
            continue
        if door_used and name != "withdraw":
            continue
        label = DOOR_LABELS[name]
        entry = {"door": name, "label": label,
                 "needs_cost_cap": name in {"paid", "unproved"},
                 "admission_ref": admission_ref,
                 "cost": door.get("cost", "free"),
                 "cost_label": _term("door_cost", door.get("cost", "free")),
                 "confirm": _DOOR_CONFIRMS[name].format(label=label)}
        if name == "withdraw":
            entry["danger"] = True
        doors.append(entry)
    return doors


def hold_handoff(lane: str, reason: str) -> dict[str, Any] | None:
    """「交技术协助」 when the policy hands this hold to the technical administrator."""

    if not any(door["door"] == "technical_help" for door in policy_doors(lane, reason)):
        return None
    return {"label": _term("recovery_door", "technical_help"),
            "note": "把「技术详情」里的原因原文和命令交给技术管理员处理。"}


def hold_automatic_retry(lane: str, reason: str) -> dict[str, Any] | None:
    """When the system retries this hold by itself, and the earliest known instant."""

    for door in policy_doors(lane, reason):
        if door.get("automatic_retry"):
            return {"next_at": door.get("next_automatic_retry")}
    return None


def last_owner_reentry(runs_dir: Path, admission_ref: str) -> dict[str, Any] | None:
    """The owner's latest 「放行一次重新进入」 for one admission, and what it bought.

    2026-10-09: the owner pressed the button several times on legacy EPAM and
    the card came straight back, saying nothing about the presses.  Read-only:
    a grant still waiting (``controlled-reentry-grant-<digest>.json``), or the
    newest spent one (``…-used-<marker>.json``) and the run that spent it --
    the ticket directory holding ``controlled-reentry-<marker>.json`` -- with
    its status, finish time and error.
    """

    import hashlib

    from .lane_reentry_claim import GRANT_PREFIX

    runs = Path(runs_dir)
    stem = GRANT_PREFIX + hashlib.sha256(str(admission_ref).encode("utf-8")).hexdigest()[:24]
    pending = _read_json(runs / f"{stem}.json")
    if isinstance(pending, Mapping):
        return {"pending": True, "granted_at": pending.get("granted_at")}
    latest = None
    for path in runs.glob(f"{stem}-used-*.json"):
        record = _read_json(path)
        if not isinstance(record, Mapping):
            continue
        marker = path.name[len(stem) + len("-used-"):-len(".json")]
        key = str(record.get("consumed_at") or "")
        if latest is None or key > latest[0]:
            latest = (key, marker, record)
    if latest is None:
        return None
    consumed_at, marker, record = latest
    out: dict[str, Any] = {"pending": False, "granted_at": record.get("granted_at"),
                           "consumed_at": consumed_at or None, "run_status": None,
                           "run_error": None, "finished_at": None}
    for claim in runs.glob(f"*/controlled-reentry-{marker}.json"):
        summary = _read_json(claim.parent / "summary.json")
        if isinstance(summary, Mapping) and summary.get("admission_ref") == admission_ref:
            out.update(run_status=summary.get("status"), run_error=summary.get("error"),
                       finished_at=summary.get("created_at"),
                       ticket_dir=claim.parent.name)
            break
    return out


def _reentry_sentence(attempt: Mapping[str, Any] | None) -> str:
    if not attempt:
        return ""
    if attempt.get("pending"):
        return (f"你已在 {_local_time(attempt.get('granted_at'))} 恢复这项研究，系统下一轮（约 5 分钟内）"
                "会执行，请稍候，不必重复点。")
    if attempt.get("run_status") == "failed":
        error = str(attempt.get("run_error") or "原因未记录").split(": ", 1)[-1]
        return (f"上次恢复（{_local_time(attempt.get('granted_at'))}）后的研究在 "
                f"{_local_time(attempt.get('finished_at'))} 再次失败：{error[:160]}。"
                f"如果这是已修复的系统问题，可以再恢复一次；否则请点「{_term('recovery_door', 'withdraw')}」。")
    if attempt.get("run_status") is None:
        return f"上次恢复（{_local_time(attempt.get('granted_at'))}）已被系统取用，正在执行。"
    return ""


def _technical_reentry_exhaustion(item: dict[str, Any]) -> dict[str, Any]:
    """Classify the lane's exact exhausted re-entry refusal, not arbitrary prose.

    This is an execution hand-off, not a request for an analyst to buy sources.
    Keep the existing recovery doors and uncertain accounting records intact.
    """

    detail = item.get("detail") or {}
    if (detail.get("lane") != "mission_document_research"
            or detail.get("reason") != REENTRY_REBOUND_REASON
                + ":controlled reentry was already attempted"):
        return item
    company = detail.get("company") or "研究任务"
    item.update(
        title=f"{company}：自动恢复仍未完成，等待管理员检查",
        why_blocked="系统已自动更新执行凭证并尝试恢复，但该次恢复已经用过，研究尚未完成。",
        action="管理员检查执行凭证和已记录的失败，再按技术详情中的既有恢复条件处理；原调用与费用记录保留。",
        consequence="这项研究仍未形成答案；不会因此自动增加恢复次数或费用额度。",
        where=ADMIN_RUN_WHERE, actionable=False,
        responsibility="administrator", status="system_repair_pending",
    )
    return item


def _escalated_hold_item(
    lane: str, hold: Mapping[str, Any], *, at: str, path: str,
    subject: Mapping[str, Any] | None = None, door_used: bool = False,
) -> dict[str, Any]:
    """One held task waiting on a person: its card, whatever its reason.

    2026-10-09: built for every ``recovery_required`` hold of the ledger, not
    only for the reasons this module happened to know (live legacy EPAM, two
    ``fresh_work_recovery_exhausted`` holds counted by the group card with no
    card of their own).  The doors are exactly what the executor's policy
    permits (``available_doors``); an unknown reason still gets withdrawal and
    a hand-off to the technical administrator.
    """

    reason = str(hold.get("reason") or "")
    admission_ref = str(hold.get("admission_ref") or "")
    # ``path`` is <state>/<lane runs dir>/holds.json, so the state directory
    # the owner has to name is two levels up -- which means the command in
    # front of them is the real one, not a template.
    state_dir = Path(path).parent.parent
    why, policy_action = _retry_words(
        reason, state_dir=state_dir, admission_ref=admission_ref)
    subject = subject or {}
    company = subject.get("company") or "未知公司"
    question = subject.get("question")
    reason_label = hold_reason_label(reason)
    commands = hold_commands(lane, admission_ref, reason, state_dir=state_dir,
                             door_used=door_used)
    doors = hold_doors(lane, admission_ref, reason, door_used=door_used)
    handoff = hold_handoff(lane, reason)
    automatic = hold_automatic_retry(lane, reason)
    attempt = last_owner_reentry(Path(path).parent, admission_ref)
    if attempt and attempt.get("pending"):
        # A grant is waiting for the lane: another press would only replay it.
        doors = [door for door in doors if door["door"] != "reentry"]
    # 2026-10-09: every door is a button -- on this card and in 管理员设置 →
    # 运行控制 -- sent through the writer as the owner; the terminal commands
    # stay in 技术详情 for whoever wants them.
    stoppable = any(door["door"] == "withdraw" for door in doors)
    first = doors[0]["door"] if doors else None
    # 2026-10-09 (financial review 痛点3, T03/T04; batch am text evaluation
    # 7.3): the registry's business words -- 恢复专项课题研究 / 扩大资料检索
    # 范围并重新获取 / 暂缓当前专项课题 -- never 放行 / 再买一次 / 撤回.
    resume, refetch, pause, again = (
        _term("recovery_door", "reentry"), _term("recovery_door", "paid"),
        _term("recovery_door", "withdraw"), _term("recovery_door", "retry_round"))
    if door_used:
        action = ("这项研究已经恢复过一次，系统只允许恢复一次，再恢复会被拒绝。"
                  + (f"如果它仍然停着，点「{pause}」，需要时可以重新发起同一课题。" if stoppable else
                     "如果它仍然停着，请技术管理员按技术详情里的做法把它关掉。"))
    elif automatic is not None:
        action = policy_action + (f"不想再等，可以点「{pause}」。" if stoppable else "")
    elif first == "reentry":
        action = (f"点「{resume}」：系统下一轮会按当前设置继续这项研究，成功后这张卡片自动消失。"
                  f"不想再试，点「{pause}」。")
    elif first == "retry_round":
        action = (f"点「{again}」：系统下一轮为这个研究步骤再安排一轮重试，费用在立项时已批准的上限之内，"
                  f"不新增预算；成功后这张卡片自动消失。不想再试，点「{pause}」。")
    elif first in {"paid", "unproved"}:
        action = (f"先看原因；值得再试，点「{refetch}」并填写本次检索预算上限（美元）；"
                  f"不值得再试，点「{pause}」。")
    elif handoff is not None:
        action = (f"系统无法自己处理这个原因：点「{handoff['label']}」把技术详情里的原文交给技术管理员"
                  + (f"；不再需要这项研究，点「{pause}」。" if stoppable else "。"))
    elif stoppable:
        action = (f"这一类失败按原样重试不会有不同结果，所以没有「再试」按钮；点「{pause}」，"
                  "需要时可以重新发起同一课题。")
    else:
        action = "这类研究任务暂时没有页面按钮，需要技术管理员处理（做法见技术详情）。"
    if attempt and attempt.get("pending"):
        action = _reentry_sentence(attempt) + (f"不想再等，可以点「{pause}」。" if stoppable else "")
    elif attempt and automatic is None:
        action = _reentry_sentence(attempt) + action
    if doors:
        action += "（同样的操作在「管理员设置 → 运行控制」也有；技术详情里保留了对应的命令行做法，供管理员使用。）"
    title = (f"等你决定：{company} · {_summary(question)}" if question
             else f"等你决定：{company} 的一项原文专题研究")
    if automatic is not None:
        title = (f"系统将自动重试：{company} · {_summary(question)}" if question
                 else f"系统将自动重试：{company} 的一项原文专题研究")
    return _technical_reentry_exhaustion(_item(
        "controlled_recovery", ref=f"lane:{lane}:{admission_ref}", at=at,
        title=title,
        why=(f"「{lane_label(lane)}」里这项研究任务已停下，原因：{reason_label}。" + why),
        action=action,
        consequence=("系统会自动再试；在那之前，这项研究要回答的问题暂时没有答案。"
                     if automatic is not None else
                     "不处理，这项研究任务不会再自己动，它要回答的那个问题一直没有答案。"),
        where=ADMIN_RUN_WHERE if doors else TECHNICAL_ADMIN_WHERE,
        detail={"lane": lane, "lane_label": lane_label(lane),
                "admission_ref": admission_ref, "reason": reason,
                "owner_doors": doors,
                "handoff": handoff,
                "automatic_retry": automatic,
                "last_reentry": attempt,
                "reason_label": reason_label,
                "company_ref": subject.get("company_ref"), "company": subject.get("company"),
                "question": question,
                "ticket_ref": hold.get("ticket_ref"), "holds_path": path,
                "owner_door_used": door_used,
                "commands": commands,
                # 2026-10-06: the page offers the stop button itself (the
                # same withdraw door, through the writer, with a receipt).
                "stop_line": stoppable},
    ))


def _numeric_contract_system_item(core, lane, hold, *, at, path, subject):
    if (core is None or lane != "mission_document_research"
            or not any(reason in str(hold.get("reason") or "") for reason in (
                CONTRACT_RETRIED_REASON, "numeric_technical_maintenance_failed",
                "unproved_send_failed_after_automatic_retry", "numeric_technical_maintenance_day_cap_reached",
                "numeric_maintenance_budget_backoff_day_cap", "numeric_maintenance_budget_waiting"))):
        return None
    from .directed_numeric_recovery import read_numeric_contract_hold
    try:
        state = read_numeric_contract_hold(core, hold["admission_ref"])
    except Exception as exc:  # Read-only diagnostic failure is not a spending decision.
        if "unproved_send_failed_after_automatic_retry" in str(hold.get("reason") or ""):
            return None  # No proof of the finite numeric defect: retain the existing door.
        state = {"admission_ref": hold["admission_ref"],
                 "state_unverified": True, "read_error_type": type(exc).__name__}
    if state is None:
        return None
    unverified = bool(state.get("state_unverified"))
    automatic = not unverified and state["automatic_window_available"]
    technical_failed = state.get("technical_maintenance_failed") is True
    schema_candidate = state.get("technical_schema_candidate") is True
    budget_waiting = state.get("budget_waiting") is True
    system_owned = automatic or budget_waiting
    handler = "系统处理" if system_owned else "管理员检查"
    company = (subject or {}).get("company") or "研究项目"
    question = (subject or {}).get("question")
    card = _item(
        "controlled_recovery", ref=f"lane:{lane}:{hold['admission_ref']}", at=at,
        title=f"{company}：管理员检查研究恢复状态" if unverified else
              f"{company}：系统处理数字任务，等待可用预算" if budget_waiting else
              f"{company}：{handler}数字任务技术恢复" if technical_failed or schema_candidate else f"{company}：{handler}数字回复格式",
        why=(("当前无法核实这项研究的恢复记录。" if unverified else
              "当前预算暂不足以继续这项数字任务，系统在下一预算周期复查。" if budget_waiting else
              "系统正在核对独立核验接口要求的回复格式；早先的发送状态和费用记录保留。" if schema_candidate else
              "这项数字任务的一次技术恢复尚未完成，原失败和费用记录保留。" if technical_failed else
              "模型回复未按已定的指标、期间或单位返回，数字核验尚未通过。")
             + ("自动恢复状态暂未核实，等待系统重新读取记录。" if unverified else
                "不会增加已设定的费用额度。" if budget_waiting else
                "系统仍有一次受原预算约束的自动恢复机会。" if automatic else
                "该阶段的自动恢复次数已用完，等待系统维护复核。")),
        action=("系统将在下一预算周期自动复查，额度足够时继续。" if budget_waiting else
                "系统按修复后的格式说明继续检查原文；完成核验后再登记数字。" if automatic else
                "管理员检查回复格式和后续恢复安排；原失败和费用记录保留。"),
        consequence=("这项研究的后续恢复仍待核对。" if unverified else
                     "这项研究问题仍未形成正式数字答案。"),
        where=NO_ACTION_WHERE if system_owned else TECHNICAL_ADMIN_WHERE,
        actionable=False,
        detail={"lane": lane, "admission_ref": hold["admission_ref"],
                "reason": hold["reason"], "holds_path": path,
                "company_ref": (subject or {}).get("company_ref"), "question": question,
                "contract_recovery": state, "owner_doors": [], "commands": []},
    )
    card.update(responsibility="background" if system_owned else "administrator",
                status="recovery_state_unverified" if unverified else
                    "automatic_recovery_pending" if automatic else "system_repair_pending")
    return card


def held_lanes(
    heartbeat: Mapping[str, Any] | None, *, state_dir: Path | None = None,
    core: Any = None,
) -> list[dict[str, Any]]:
    """Lanes that stopped and named an authorisation they are waiting for.

    Two sources, in this order.  A lane that keeps a hold ledger beside itself
    is read from that ledger -- one item per admission the lane has already
    tried and given up on, plus one aggregate for the lane -- because the
    ledger survives a tick that did something else.  A lane with no ledger is
    read from the tick result the controller writes, in the lane's own words.

    This module deliberately does not try to translate every lane's reason into
    a recommended action -- it cannot know them all, and a wrong instruction is
    worse than the lane's own sentence.  What it adds is the framing: this is a
    thing a person has to authorise, here is where it lives.  Reasons it does
    know (:data:`HOLD_REASON_LABELS`) are shown in Chinese with the original
    kept under 技术详情.
    """

    ledgers = lane_hold_ledgers(state_dir)
    planner = (heartbeat or {}).get("bounded_planner") if isinstance(
        heartbeat, Mapping) else None
    result = (planner or {}).get("last_result") if isinstance(planner, Mapping) else None
    result = result if isinstance(result, Mapping) else {}
    at = str((planner or {}).get("last_completed_at") or "")
    # 2026-10-09: one card per held task waiting on a person, whatever its
    # reason -- the group card below counts exactly the cards it rendered.
    subjects = admission_subjects(core, (
        hold["admission_ref"] for ledger in ledgers.values() for hold in ledger["waiting"]))
    used = _doors_used(state_dir) if state_dir is not None and any(
        ledger["waiting"] for ledger in ledgers.values()) else {}
    out: list[dict[str, Any]] = []
    # 2026-10-09: two held admissions asking one question (live ws-7d, MSFT,
    # 「…末融资租赁负债…」 and 「…末的融资租赁负债…」) are one decision; the
    # later one says so and points at withdrawing it.
    from .document_inquiry_equivalence import normalize_question

    first_of: dict[tuple[str, str], str] = {}
    for lane in sorted(ledgers):
        ledger = ledgers[lane]
        rendered: list[dict[str, Any]] = []
        unrendered: list[tuple[str, str]] = []
        for hold in ledger["waiting"]:
            system_item = _numeric_contract_system_item(
                core, lane, hold, at=at or ledger["at"], path=ledger["path"],
                subject=subjects.get(hold["admission_ref"]))
            if system_item is not None:
                out.append(system_item)
                continue
            try:
                item = _escalated_hold_item(
                    lane, hold, at=at or ledger["at"], path=ledger["path"],
                    subject=subjects.get(hold["admission_ref"]),
                    door_used=_door_used(used, hold))
                # An exhausted empty provider reply is an execution problem,
                # not an analyst's decision to buy more research. Keep the
                # original recovery doors and uncertain-send audit intact.
                if (str(hold.get("reason") or "").startswith(
                        UNPROVED_SEND_RETRIED_REASON + ":HOST_EMPTY_COMPLETION:")
                        and PROVIDER_NOTHING_CAUSE in str(hold["reason"]).split(":")):
                    company = (subjects.get(hold["admission_ref"]) or {}).get("company") or "研究项目"
                    item.update(
                        title=f"{company}：模型未返回内容，等待管理员检查",
                        why_blocked="系统自动重试后，模型仍没有返回可用内容。",
                        action="管理员检查模型服务和恢复安排；原调用与费用记录保留。",
                        consequence="这项研究尚未形成答案，等待管理员处理。",
                        where=ADMIN_RUN_WHERE, actionable=False,
                        responsibility="administrator", status="system_repair_pending")
            except Exception as exc:  # noqa: BLE001 - said on the group card
                unrendered.append((hold["admission_ref"], f"{type(exc).__name__}: {exc}"[:200]))
                continue
            subject = subjects.get(hold["admission_ref"]) or {}
            key = (str(subject.get("company_ref") or ""),
                   normalize_question(str(subject.get("question") or "")))
            if key[0] and key[1]:
                if key in first_of:
                    item["detail"]["duplicate_of"] = first_of[key]
                    item["action"] = ("这项与另一项停着的研究问的是同一个问题（只差一个虚词），"
                                      f"建议点「{_term('recovery_door', 'withdraw')}」，只保留另一项。"
                                      + item["action"])
                else:
                    first_of[key] = hold["admission_ref"]
            rendered.append(item)
        out.extend(rendered)
        if not rendered and not unrendered:
            continue
        tick = result.get(lane) if isinstance(result.get(lane), Mapping) else {}
        status = str(tick.get("status") or "")
        status_label = HELD_STATUS_LABELS.get(status) or (
            hold_reason_label(status) if status else "未报告")
        automatic = sum(1 for item in rendered if item["detail"].get("automatic_retry"))
        deciding = sum(1 for item in rendered if item.get("actionable") is not False
                       and not item["detail"].get("automatic_retry"))
        missing = ("" if not unrendered else
                   f"另有 {len(unrendered)} 项无法生成单独的卡片（{unrendered[0][1]}），"
                   f"请点「{_term('recovery_door', 'technical_help')}」把技术详情交给技术管理员。")
        group = _item(
            "controlled_recovery", ref=f"lane:{lane}", at=at or ledger["at"],
            title=f"「{lane_label(lane)}」有 {len(rendered)} 项研究任务停着等处理",
            why=(f"本页逐项列出了这 {len(rendered)} 项研究任务，每张卡片都写了原因"
                 + (f"；其中 {automatic} 项会由系统自动再试，不需要你操作" if automatic else "")
                 + (f"；{deciding} 项等你决定" if deciding else "")
                 + "。" + missing
                 + ((f"本轮流程自己的状态是「{status_label}」"
                     + ("——它这一轮在忙别的任务，不代表这些不再等着。" if status == "resumed" else "。"))
                    if status else "")),
            action=("逐条看本页按研究任务列出的卡片，每张都写了原因，并带着可以直接点的按钮；"
                    "同样的按钮也在「管理员设置 → 运行控制」。"),
            consequence="不处理，等你决定的研究任务不会再自己动，它们负责的研究一直不产出。",
            where=ADMIN_RUN_WHERE,
            # A summary is not a second decision. Unrendered holds still need
            # attention because their individual decision cards are absent.
            actionable=bool(unrendered),
            detail={"lane": lane, "lane_label": lane_label(lane),
                    "held": len(rendered),
                    "waiting": len(ledger["waiting"]),
                    "escalated": len(ledger["escalated"]),
                    "automatic_retry": automatic,
                    "unrendered": [{"admission_ref": ref, "error": error}
                                   for ref, error in unrendered],
                    "holds_path": ledger["path"],
                    "status": tick.get("status"),
                    "commands": [{
                        "label": "只读：列出这个环境所有停着的研究任务和各自的命令",
                        "preview": _cli(Path(ledger["path"]).parent.parent, "holds"),
                    }]},
        )
        if not unrendered:
            group.update(responsibility="administrator" if any(
                item.get("responsibility") == "administrator" for item in rendered
            ) else "background", status="recovery_summary")
        if deciding == 0 and not unrendered:
            group.update(
                why_blocked="这些任务由系统继续处理或等待管理员检查，进展见各任务卡片。",
                action="查看各项处理进展。", consequence="相关研究答案仍待补齐。")
        out.append(group)
    for lane, value in sorted(result.items()):
        if not isinstance(value, Mapping) or lane in ledgers:
            continue
        status = str(value.get("status") or "")
        if status not in HELD_LANE_STATUSES:
            continue
        reason = str(value.get("reason") or "")
        recovery = (value.get("last") or {}).get("recovery") if isinstance(
            value.get("last"), Mapping) else None
        if isinstance(recovery, Mapping) and recovery.get("reason"):
            reason = str(recovery["reason"])
        # The document lane retries a proved-paid contract rejection, an
        # unproved send and a moved ticket identity once each by itself.  When
        # it still asks for a person, the ask has to say that, or the owner
        # authorises the same purchase a second time without knowing the first
        # one already happened.
        retried = _escalated_reason(reason)
        why, action = _retry_words(reason)
        # 2026-10-07 (BUG-09): a sentence with a shell command in it is for the
        # technical administrator; the card says what it is in plain words and
        # the full instruction moves to 技术详情.
        instructions = None
        if "dalton_core." in action or "--apply" in action:
            instructions, action = action, (
                "这一步没有页面按钮：需要技术管理员在本机执行一条授权命令；"
                "具体做法和命令见技术详情。")
        out.append(_technical_reentry_exhaustion(_item(
            "controlled_recovery", ref=f"lane:{lane}", at=at,
            title=(f"「{lane_label(lane)}」自动重试过一次仍然失败，等你决定" if retried
                   else f"「{lane_label(lane)}」停着等授权"),
            why=(f"这个流程本轮的状态是「{HELD_STATUS_LABELS.get(status, status)}」，"
                 f"原因：{hold_reason_label(reason)}。" + why),
            action=action,
            consequence="不授权，这个流程每一轮都会重复报同样的状态，它负责的研究一直不产出。",
            where=TECHNICAL_ADMIN_WHERE if instructions else ADMIN_RUN_WHERE,
            detail={"lane": lane, "lane_label": lane_label(lane),
                    "status": status, "reason": reason,
                    "reason_label": hold_reason_label(reason),
                    "held": value.get("held"),
                    **({"instructions": instructions} if instructions else {})},
        )))
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
                   "或者在「研究目标与计划」页的「模型与预算」把这个环节改走另一条 API 通道（需要填入该通道的密钥）。",
            consequence="不处理，需要这个模型的研究环节会持续退到备用模型或直接失败，"
                        "产出质量下降而且看不出原因。",
            where=MODELS_DIALOG_LABEL,
            where_view=GOAL_VIEW,
            where_open=MODELS_DIALOG,
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


#: The tables a model router has kept its cooldowns in, newest shape first.
#: ``model_profile_cooldowns`` is the live one (WP-A/A2, ``model_router``); the
#: other two are the names this reader was written against before that table
#: existed, kept so an older Core still shows its cooldowns.  2026-10-07: the
#: reader knew only the old two, so fourteen deepseek-v4-flash cooldowns and
#: 501 skipped links in two days never reached this list.
COOLDOWN_TABLES: tuple[str, ...] = (
    "model_profile_cooldowns", "model_credential_cooldowns", "model_route_cooldowns",
)
#: How far back "what this cooldown blocked" is counted from the link ledger.
COOLDOWN_BLOCK_WINDOW_HOURS = 48


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _local_day(value: Any) -> str:
    """``MM-DD`` in the owner's zone, for "自 10-05 起"; unparseable -> ``?``."""

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    moment = _instant(value)
    if moment is None:
        return "?"
    try:
        local = moment.astimezone(ZoneInfo(OWNER_TIMEZONE))
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata ships with the runtime
        local = moment.astimezone(timezone(timedelta(hours=8)))
    return f"{local:%m-%d}"


def _cooldown_rows(connection: Any, table: str) -> list[dict[str, Any]]:
    """Every row of one cooldown table, normalised to the live table's fields.

    Schema-aware rather than schema-assuming: the columns are read off the
    table, the row's own JSON (when it has one) fills in what the columns do
    not carry, and the three spellings of "which model" and "until when" the
    tables have used are folded into one.
    """

    try:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        order = ("cooldown_sequence" if "cooldown_sequence" in columns else "rowid")
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        fields = {key: row[key] for key in row.keys()}
        body: dict[str, Any] = {}
        raw = fields.get("cooldown_json")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            body = parsed if isinstance(parsed, dict) else {}
        merged = {**body, **{key: value for key, value in fields.items()
                             if value is not None and key != "cooldown_json"}}
        profile = str(merged.get("profile_id") or merged.get("credential_slot_ref")
                      or merged.get("profile_version_ref") or "")
        until = str(merged.get("until") or merged.get("cooldown_until") or "")
        if not profile or _instant(until) is None:
            continue
        out.append({
            "table": table,
            "profile_id": profile,
            "reason": merged.get("reason"),
            "failure_code": merged.get("failure_code"),
            "streak": merged.get("streak"),
            "started_at": str(merged.get("started_at") or merged.get("created_at") or ""),
            "until": until,
            "cooldown_seconds": merged.get("cooldown_seconds"),
            "route_decision_ref": merged.get("route_decision_ref"),
            "cooldown_id": merged.get("cooldown_id") or merged.get("id"),
        })
    return out


def _health_span(connection: Any, profile_id: str,
                 started_at: str) -> tuple[bool, str | None, str | None]:
    """(served since this cooldown began, last served at, first failure after it)."""

    if not _table(connection, "model_profile_health_events"):
        return False, None, None
    try:
        served = connection.execute(
            "SELECT MAX(observed_at) AS at FROM model_profile_health_events "
            "WHERE profile_id=? AND outcome='served'", (profile_id,)).fetchone()["at"]
        first = connection.execute(
            "SELECT MIN(observed_at) AS at FROM model_profile_health_events "
            "WHERE profile_id=? AND outcome='provider_failure' AND observed_at>?",
            (profile_id, served or "")).fetchone()["at"]
    except sqlite3.Error:
        return False, None, None
    served_since = (_instant(served) is not None and _instant(started_at) is not None
                    and _instant(served) >= _instant(started_at))
    return served_since, served, first


def _cooldown_blocks(connection: Any, profile_id: str, *,
                     now: datetime) -> list[dict[str, Any]]:
    """Which stages' chains lost this link, and who answered instead.

    Read off the link ledger the router writes for every route: a cooled
    profile is a ``provider_cooldown`` link in that stage's chain.  Measured,
    not inferred from a policy -- the chain a stage actually walked.
    """

    if not _table(connection, "model_route_chain_links"):
        return []
    from .model_profile_health import COOLDOWN_SKIP_REASON

    since = _iso(now - timedelta(hours=COOLDOWN_BLOCK_WINDOW_HOURS))
    try:
        skipped = connection.execute(
            "SELECT purpose, tier, COUNT(*) AS skips FROM model_route_chain_links "
            "WHERE profile_id=? AND skip_reason=? AND created_at>=? "
            "GROUP BY purpose, tier ORDER BY skips DESC, purpose",
            (profile_id, COOLDOWN_SKIP_REASON, since)).fetchall()
        served = connection.execute(
            "SELECT k.purpose AS purpose, s.profile_id AS profile_id, COUNT(*) AS calls "
            "FROM model_route_chain_links k JOIN model_route_chain_links s "
            "ON s.work_order_id=k.work_order_id AND s.capability=k.capability "
            "AND s.attempt_number=k.attempt_number AND s.served=1 "
            "WHERE k.profile_id=? AND k.skip_reason=? AND k.created_at>=? "
            "GROUP BY k.purpose, s.profile_id ORDER BY calls DESC",
            (profile_id, COOLDOWN_SKIP_REASON, since)).fetchall()
    except sqlite3.Error:
        return []
    instead: dict[str, list[str]] = {}
    for row in served:
        instead.setdefault(str(row["purpose"]), []).append(
            _profile_name(str(row["profile_id"])))
    try:
        from .model_selection import PURPOSE_LABELS
    except Exception:  # noqa: BLE001 - a label table is never worth losing the item
        PURPOSE_LABELS = {}
    return [{
        "purpose": str(row["purpose"]),
        "label": PURPOSE_LABELS.get(str(row["purpose"]), str(row["purpose"])),
        "tier": str(row["tier"]),
        "skips": int(row["skips"]),
        "served_instead": instead.get(str(row["purpose"]), []),
    } for row in skipped]


def _provider_of(connection: Any, profile_id: str) -> str | None:
    if not _table(connection, "model_endpoint_profile_versions"):
        return None
    try:
        row = connection.execute(
            "SELECT provider FROM model_endpoint_profile_versions WHERE profile_id=? "
            "ORDER BY version DESC LIMIT 1", (profile_id,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["provider"])


def model_cooldowns(model_router_db: Path | str | None, *,
                    now: datetime) -> list[dict[str, Any]]:
    """Every profile the router is refusing to offer right now, one entry each.

    The same rule the router routes by (``ModelRouter.active_cooldowns``):
    the latest cooldown per profile, unexpired, and not overtaken by a served
    call.  Read-only, and shared with the parity report so the two never
    disagree about what is cooling.
    """

    connection = _open(model_router_db)
    if connection is None:
        return []
    try:
        return _read_cooldowns(connection, now=now)
    finally:
        connection.close()


def _read_cooldowns(connection: Any, *, now: datetime) -> list[dict[str, Any]]:
    from .model_profile_health import (
        COOLDOWN_REASON_LABELS, failure_code_label, is_billing_code,
    )

    for table in COOLDOWN_TABLES:
        if not _table(connection, table):
            continue
        latest: dict[str, dict[str, Any]] = {}
        history: dict[str, list[dict[str, Any]]] = {}
        for row in _cooldown_rows(connection, table):
            latest[row["profile_id"]] = row
            history.setdefault(row["profile_id"], []).append(row)
        out: list[dict[str, Any]] = []
        for profile_id, row in sorted(latest.items()):
            if _instant(row["until"]) <= now:
                continue
            served_since, last_served, first_failure = _health_span(
                connection, profile_id, row["started_at"])
            if served_since:
                continue
            # Every cooldown since the endpoint last answered: the run the
            # owner is looking at, not just its latest six hours.
            run = [item for item in history[profile_id]
                   if last_served is None or item["started_at"] > last_served]
            since = first_failure or (run[0]["started_at"] if run else row["started_at"])
            code = row["failure_code"]
            billing = row["reason"] == "provider_billing" or is_billing_code(code)
            out.append({
                **row,
                "name": _profile_name(profile_id),
                "provider": _provider_of(connection, profile_id),
                "billing": billing,
                "reason_label": COOLDOWN_REASON_LABELS.get(
                    str(row["reason"]), str(row["reason"] or "连续失败")),
                "failure_code_label": failure_code_label(code),
                "failing_since": since,
                "last_served_at": last_served,
                "cooldowns_in_run": len(run) or 1,
                "blocks": _cooldown_blocks(connection, profile_id, now=now),
            })
        return out
    return []


def _cooldowns(model_router_db: Path | str | None, *, now: datetime) -> list[dict[str, Any]]:
    """One 「需要你处理」 card per cooled profile, in the owner's words."""

    return [_cooldown_item(entry) for entry in model_cooldowns(model_router_db, now=now)]


def _blocks_sentence(blocks: Sequence[Mapping[str, Any]]) -> str:
    if not blocks:
        return (f"过去 {COOLDOWN_BLOCK_WINDOW_HOURS} 小时没有环节因为它被跳过"
                "（用到它的环节还没运行，或它不在任何正在用的链里）。")
    parts = []
    for block in blocks:
        instead = "、".join(block.get("served_instead") or [])
        parts.append(f"「{block['label']}」跳过它 {block['skips']} 次"
                     + (f"，改由 {instead} 承接" if instead else "，没有别的模型承接"))
    return f"过去 {COOLDOWN_BLOCK_WINDOW_HOURS} 小时里，" + "；".join(parts) + "。"


def _cooldown_item(entry: Mapping[str, Any]) -> dict[str, Any]:
    name = str(entry["name"])
    day = _local_day(entry["failing_since"])
    until = _local_time(entry["until"])
    blocks = list(entry.get("blocks") or [])
    provider = str(entry.get("provider") or "供应商")
    detail = {
        "profile_id": entry["profile_id"], "provider": entry.get("provider"),
        "reason": entry.get("reason"), "reason_label": entry.get("reason_label"),
        "failure_code": entry.get("failure_code"),
        "failure_code_label": entry.get("failure_code_label"),
        "streak": entry.get("streak"), "cooldowns_in_run": entry.get("cooldowns_in_run"),
        "failing_since": entry.get("failing_since"),
        "failing_since_label": _local_time(entry.get("failing_since")),
        "last_served_at": entry.get("last_served_at"),
        "started_at": entry.get("started_at"), "until": entry.get("until"),
        "until_label": until, "route_decision_ref": entry.get("route_decision_ref"),
        "cooldown_id": entry.get("cooldown_id"), "table": entry.get("table"),
        "blocks": blocks,
    }
    affected = "、".join(f"「{block['label']}」" for block in blocks) or "用到它的环节"
    common = dict(
        kind="model_cooldown", ref=f"cooldown:{entry['profile_id']}",
        at=str(entry.get("failing_since") or entry.get("started_at") or ""),
        where=MODELS_DIALOG_LABEL, where_view=GOAL_VIEW, where_open=MODELS_DIALOG,
        detail=detail,
        consequence=(f"不处理，{affected}会一直跳过 {name}、改由链上后面的模型承接；"
                     "链上没有别的模型时，这个环节直接停下。"),
    )
    since_line = (f"自 {_local_time(entry.get('failing_since'))} 起一次都没有成功，"
                  f"已连续冷却 {entry.get('cooldowns_in_run')} 轮，这一轮到 {until}。")
    if entry.get("billing"):
        return _item(
            title=f"供应商账户余额不足：{name}，自 {day} 起探活失败；充值后自动恢复",
            why=(f"{provider} 对这个模型的每次调用都回 HTTP 402（账户余额不足）。" + since_line
                 + _blocks_sentence(blocks)),
            action=(f"给 {provider} 账户充值。充值后不用做别的：冷却到期时的下一次探测成功，"
                    "这个模型就自动回到链上。"),
            **common)
    code = entry.get("failure_code")
    if code == "INVALID_HOST_RESULT":
        action = ("先确认真正的原因：本机 OpenClaw 日志（/tmp/openclaw/openclaw-日期.log）里"
                  f"搜 provider={entry.get('provider') or '…'}，看 status=：402 是账户余额不足（充值）；"
                  "401/403 是密钥被拒（换密钥）；200 却没有正文是模型把输出上限用在思考上（调高输出上限）。"
                  "要让这张卡直接写出原因，需要技术管理员更新宿主补丁和模型 broker（见技术详情）。")
        detail["owner_steps"] = [
            "停机窗口内对 OpenClaw 安装目录执行 integrations/openclaw_host_patches/"
            "patch_provider_failure_bridge.py --openclaw-root <OpenClaw 安装目录>"
            "（让宿主把 HTTP 402 转给 broker）",
            "用 scripts/openclaw_broker_stopped_window.py 把模型 broker 托管插件更新到含 "
            "PROVIDER_INSUFFICIENT_BALANCE 的版本",
        ]
    elif entry.get("reason") == "provider_rate_limited":
        action = ("两条路选一条：等对方的额度自己重置（冷却到期会自动再试）；"
                  "或者在「研究目标与计划」页的「模型与预算」把这个环节改走另一条通道。")
    else:
        action = ("冷却到期会自动再试一次；一直失败就在「研究目标与计划」页的「模型与预算」"
                  "把它从链上移走，或换一条通道。")
    return _item(
        title=(f"模型冷却中：{name}，自 {day} 起一直失败"
               f"（{str(entry.get('failure_code_label') or '').split('（', 1)[0]}）"),
        why=(f"失败原因：{entry.get('failure_code_label')}；冷却原因：{entry.get('reason_label')}。"
             + since_line + _blocks_sentence(blocks)),
        action=action, **common)


#: The marker a refused verifier chain carries (``cockpit_model``).
PUBLICATION_ROUTE_MARKER = "verifier_not_independent"


def connector_login_lapses(state_dir: Path | None, *, now: datetime) -> list[dict[str, Any]]:
    """A host-tool source whose login the provider no longer accepts (2026-10-08).

    Read from ``<state>/connector-health/`` (``connector_health``), which the
    discovery coordinator writes when a search or acquisition is refused with
    a login/session error such as ``E_UPSTREAM_API: 用户状态发生变更，刷新
    token``.  The desktop app may still say it is signed in -- that is exactly
    the case this card exists for.
    """

    from .connector_health import SOURCE_APPS, SOURCE_NAMES, held_records

    out: list[dict[str, Any]] = []
    for record in held_records(state_dir):
        source_ref = str(record.get("source_ref") or "")
        name = SOURCE_NAMES.get(source_ref, source_ref)
        app = SOURCE_APPS.get(source_ref, f"{name} 客户端")
        message = str((record.get("last_detail") or {}).get("message") or "")
        blocked = record.get("blocked") or {}
        searches = int(blocked.get("search") or 0)
        acquisitions = int(blocked.get("acquisition") or 0)
        since = str(record.get("since") or "")
        if "刷新 token" in message or "刷新token" in message or "token" in message.lower():
            cause = "服务器要求刷新 token"
        else:
            cause = "服务器拒绝了当前登录"
        title = f"{name} 登录状态失效：{cause}（desktop 显示已就绪但请求被拒）"
        counts = []
        if searches:
            counts.append(f"{searches} 次资料检索")
        if acquisitions:
            counts.append(f"{acquisitions} 次原文获取")
        last_probe = record.get("last_probe") or {}
        probe_text = ""
        if last_probe.get("at"):
            probe_text = (f"最近一次自动探测（{_local_time(last_probe['at'])}）"
                          f"{'仍被拒绝' if not last_probe.get('ok') else '已通过'}。")
        out.append(_item(
            "connector_login_expired", ref=f"connector-health:{source_ref}", at=since,
            title=title,
            why=(f"自 {_local_time(since)} 起，{name} 的请求都被服务器以登录失效拒绝"
                 + (f"，已有{'、'.join(counts)}被挡回" if counts else "")
                 + "。在登录恢复之前，系统暂停这条来源的检索和原文获取（不再按天重试）。"
                 + (f"服务器原话：「{message[:120]}」。" if message else "")
                 + probe_text),
            action=(f"请在 {app} 重新登录/刷新，恢复后系统自动继续"
                    "（每 15 分钟自动探测一次，探测通过后下一轮就补跑被挡回的检索）。"),
            consequence=f"不处理，{name} 的检索和原文获取会一直暂停，相关研究缺口补不上。",
            where=f"{app}（本机）",
            detail={"source_ref": source_ref, "since": since,
                    "hold_until": record.get("hold_until"),
                    "blocked_searches": searches, "blocked_acquisitions": acquisitions,
                    "provider_message": message, "failure_class": record.get("failure_class"),
                    "probes": record.get("probes"), "last_probe": last_probe or None,
                    "blocked_refs": list(record.get("blocked_refs") or [])},
        ))
    return out


def material_acquisition_cards(state_dir: Path | None, *, now: datetime,
                               labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Batch E: the material acquisition service's three doors, and only those.

    Every source exhausted (with 「已尝试：…」), a budget cap reached (with the
    amount to approve), a source needing a login or an approval.  Everything
    else the service does -- waiting for a held source, a timed-out request,
    data another lane owns -- is automatic and never becomes a card.  Read
    from ``material-requests.sqlite`` (``material_acquisition.owner_door_cards``).
    """

    if state_dir is None:
        return []
    try:
        from .material_acquisition import owner_door_cards

        raw = owner_door_cards(state_dir, now=_iso(now), company_labels=labels or {})
    except Exception:  # noqa: BLE001 - an unreadable ledger raises no card
        return []
    out = []
    for card in raw:
        item = _item(
            card["kind"], ref=card["ref"], at=card["at"], title=card["title"], why=card["why"],
            action=card["action"], consequence=card["consequence"], where=card["where"],
            detail=card.get("detail"),
            actionable=card["kind"] != "material_sources_exhausted",
        )
        # An exhausted automatic search is not an approval request. Optional
        # supplied material remains visible; budget/login doors keep their gate.
        if card["kind"] == "material_sources_exhausted":
            item.update({"responsibility": "background", "status": "waiting_material"})
        out.append(item)
    return out


def current_publication_products(work_dir: Path) -> set[tuple[str, Any]] | None:
    """``(identity hash, product hash)`` of every product the worker's last poll found.

    The worker's own checkpoint (``worker-last-run.json``) lists every product
    that is still the current version; a state file for anything else is
    history.  ``None`` when the checkpoint is missing or carries no product
    list (a gate-held worker), in which case every state counts, as before.
    """

    from .research_publication_worker import _hash

    record = _read_json(Path(work_dir) / "worker-last-run.json")
    products = record.get("products") if isinstance(record, Mapping) else None
    if not isinstance(products, list) or not products:
        return None
    out: set[tuple[str, Any]] = set()
    for row in products:
        if isinstance(row, Mapping) and isinstance(row.get("identity"), Mapping):
            out.add((_hash(dict(row["identity"])), row.get("product_hash")))
    return out


def drafting_chain_now_independent(state_dir: Path | None) -> bool | None:
    """Would a fresh draft under today's chains pass the verifier preflight?

    Read-only, through the worker's own configuration and the router it
    names.  ``None`` when that cannot be read; then the card says what it
    always said.
    """

    if state_dir is None:
        return None
    from .final_text_state import WORKER_CONFIG_FILE

    config = _read_json(Path(state_dir) / WORKER_CONFIG_FILE)
    if not isinstance(config, Mapping):
        return None
    try:
        configs = {key: _read_json(Path(str(config[key])))
                   for key in ("model_config", "verifier_config", "brain_config")}
        if not all(isinstance(value, Mapping) for value in configs.values()):
            return None
        from .research_output_preparation import (
            VerifierNotIndependent, drafting_family, verifier_preflight)

        family = drafting_family(configs["model_config"])
        if family is None:
            return None
        try:
            verifier_preflight(configs["verifier_config"], configs["brain_config"],
                               draft_route_ref=None, check_only=False, producer_family=family)
        except VerifierNotIndependent:
            return False
        return True
    except Exception:  # noqa: BLE001 - a hint, never a failure of the list
        return None


def publication_route_blocks(state_dir: Path | None) -> list[dict[str, Any]]:
    """One item for every publication product stopped on a refused verifier.

    Batch w: live 10-07 the worker logged 146 "MODEL_ROUTE_REJECTED" and 57
    "no model route" failures in a day, all one cause -- the localization
    verifier's chain held only the drafting model's family.  That is one
    decision for the owner, not 146 failures, so it is one item: how many
    products wait on it, the reason in the cockpit's own words (it names the
    chain and the family), and the page where the selection is changed.
    Read-only: the worker's product states, nothing else.
    """

    if state_dir is None:
        return []
    from .final_text_state import publication_work_dir
    from .research_publication_worker import failure_reasons

    root = publication_work_dir(state_dir) / "products"
    if not root.is_dir() or root.is_symlink():
        return []
    current = current_publication_products(publication_work_dir(state_dir))
    blocked: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.json")):
        state = _read_json(path)
        if not isinstance(state, Mapping) or state.get("status") not in {"pending", "exhausted"}:
            continue
        # 2026-10-09: seven of the eight products on legacy's card were
        # versions since replaced (event judgements and a cycle reflection);
        # the worker never offers them again, so nothing could clear them.
        if current is not None and (path.stem, state.get("product_hash")) not in current:
            continue
        result = state.get("result") if isinstance(state.get("result"), Mapping) else {}
        reasons = [reason for reason in failure_reasons(result)
                   if PUBLICATION_ROUTE_MARKER in reason]
        if reasons:
            blocked.append((str(result.get("failed_at") or ""), reasons[0]))
    if not blocked:
        return []
    blocked.sort()
    first_at, _ = blocked[0]
    latest_reason = blocked[-1][1]
    reason = latest_reason.split("（预检拦下", 1)[0]
    reason = reason[reason.find("独立核验"):] if "独立核验" in reason else reason
    if drafting_chain_now_independent(state_dir):
        # 2026-10-09: the owner already moved the drafting chain (legacy,
        # 10-08 04Z) and the card kept asking for it.  Say what happens now.
        return [_item(
            "publication_route_blocked", ref="publication:verifier-route", at=first_at,
            title=f"中文定稿卡在独立核验：{len(blocked)} 份研究产物等下一次重试（模型选择已改好）",
            why=(f"这 {len(blocked)} 份产物上次被拦下时，初稿还是 Gemini 写的：{reason[:300]}"
                 "你已经把「整理研究内容的中文初稿」改成其它家族的模型，现在的设置可以独立核验。"),
            action=("不需要再改。系统每天自动重试一次，下一次会丢弃旧初稿、用现在的模型重写后再核验，"
                    "通过后这张卡片自动消失。"),
            consequence="无需处理；下一次每日重试后中文定稿会出来。",
            where=NO_ACTION_WHERE, actionable=False,
            detail={"products": len(blocked), "first_at": first_at, "reason": latest_reason[:600],
                    "chain_now_independent": True},
        )]
    return [_item(
        "publication_route_blocked", ref="publication:verifier-route",
        at=first_at,
        title=f"中文定稿卡在独立核验：{len(blocked)} 份研究产物等你改模型选择",
        why=(f"{reason[:400]} 这 {len(blocked)} 份产物（档案、周度自评、界面文字等）"
             "因此停在「待处理」，你看不到定稿中文。"),
        action=("在「研究目标与计划」页打开「模型与预算」，找到环节「整理研究内容的中文初稿」，"
                "改为手动指定模型，依次 zai-glm-5-3-flash、deepseek-v4-flash、"
                "gemini-3-8-flash-antigravity，保存（会同步到所有环境，不用重启）。"
                "原因：「独立核验研究中文版本」只能由支持受控核验的 Gemini 直连模型承担，"
                "初稿也用 Gemini 写时就没有模型能独立核验；初稿换成别家模型后核验即可通过。"),
        consequence="不处理，这些产物每天会自动重试一次但仍会被拦下，中文定稿一直出不来。",
        where=MODELS_DIALOG_LABEL,
        where_view=GOAL_VIEW,
        where_open=MODELS_DIALOG,
        detail={"products": len(blocked), "first_at": first_at, "reason": latest_reason[:600]},
    )]


PUBLICATION_KIND_LABELS: Mapping[str, str] = MappingProxyType({
    "dossier": _term("report_kind", "dossier"), "initial_screen": _term("report_kind", "initial_screen"),
    "debate_map": _term("report_kind", "debate_map"),
    "industry_framework": _term("report_kind", "industry_framework"), "deep_insight_gate": "深度认知评审",
    "investment_memo": _term("report_kind", "investment_memo"), "surface_thesis": "投资论点", "surface_weekly_brief": "每周简报",
    "surface_event_judgement": "事件判断", "surface_cycle_reflection": "研究周期复盘",
    "surface_model_notes": "预测模型说明", "surface_earnings_preview": "业绩前瞻",
})
LANGUAGE_RETRY_LABEL = "重新生成中文定稿"
#: The one card that stands for several UI-text batches the worker gave up on
#: (2026-10-09, independent review T01 / financial 痛点5): one decision, not six.
UI_TEXT_GROUP_REF = "publication-parked:ui-text-batches"
UI_TEXT_GROUP_RETRY_LABEL = "全部重新生成中文定稿"


def _required_initial_screen_gaps(state_dir: Path, identity: Mapping[str, Any], product_hash: str | None = None) -> dict[str, Any] | None:
    """Only current, strongly held mandatory narratives become material waits."""
    if identity.get("kind") != "initial_screen" or not (state_dir / "core.sqlite").is_file():
        return None
    connection = sqlite3.connect((state_dir / "core.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        from .coverage_mission import CoverageMissionAuthority
        from .cockpit_research_library import research_library
        from .research_localization import localization_blockers
        reader = object.__new__(CoverageMissionAuthority); reader.connection = connection
        for pointer in connection.execute("SELECT mission_version_id FROM coverage_mission_pointer"):
            mission = reader._mission_record(pointer[0])
            if identity["subject_ref"] not in {m["company_ref"] for m in mission["universe"]}:
                continue
            product = next(p for p in research_library(connection, mission, identity["subject_ref"], localize=False)["products"] if p["kind"] == "initial_screen")
            if product.get("status") != "available" or product.get("version_ref") != identity.get("version_ref"):
                return None
            from .research_source_projection import publication_source_hash
            if product_hash is not None and publication_source_hash(product) != product_hash:
                return None
            blockers = localization_blockers(product)
            gaps = [b for b in blockers if b["code"] in {"empty_source_section", "gap_only", "missing_section"}]
            if gaps:
                return {"version_ref":product["version_ref"],"source_hash":product["content_hash"],
                        "required_section_gaps":gaps,"classification":"waiting_material_required_narrative"}
        return None
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        # Unverified or unknown material retains the existing review path.
        return None
    finally:
        connection.close()


def _current_skipped_source(state_dir: Path, identity: Mapping[str, Any],
                            previous_projection_hash: str) -> dict[str, Any] | None:
    """Recheck a legacy skip against the same strongly held current source.

    A pass can spend several minutes on preceding products before replacing
    the final checkpoint. This is a read-only display classification, never
    a completion receipt, retry, or change to its original publication cap.
    """
    db = state_dir / "core.sqlite"
    if not db.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        from .coverage_mission import CoverageMissionAuthority
        from .cockpit_research_library import research_library
        from .final_surface_products import final_surface_products
        from .research_publication_worker import _hash, _identity, source_blockers
        from .research_source_projection import publication_source_hash
        reader = object.__new__(CoverageMissionAuthority)
        reader.connection = connection
        for pointer in connection.execute("SELECT mission_version_id FROM coverage_mission_pointer"):
            mission = reader._mission_record(pointer[0])
            subject = identity.get("subject_ref")
            members = [m["company_ref"] for m in mission["universe"]]
            if subject in members:
                company = subject
            elif subject in {mission["mission_ref"], mission["industry_ref"]} and members:
                company = members[0]
            else:
                continue
            if str(identity.get("kind") or "").startswith("surface_"):
                # Surface projectors preserve display fields but their light
                # JSON reader does not itself prove the formal record hash.
                # Verify exactly the source this warning names before using it.
                source = {
                    "surface_model_notes": ("forecast_model_versions", "version_id", "company_ref"),
                    "surface_earnings_preview": ("mission_deliverable_versions", "version_id", "subject_ref"),
                    "surface_earnings_calibration": ("mission_deliverable_versions", "version_id", "subject_ref"),
                    "surface_event_judgement": ("event_judgements", "judgement_id", "company_ref"),
                    "surface_cycle_reflection": ("research_cycle_reflection_versions", "version_id", "mission_ref"),
                }.get(identity["kind"])
                if source is None:
                    return None
                table, id_column, subject_column = source
                row = connection.execute(
                    f"SELECT {id_column}, {subject_column}, content_hash, record_json FROM {table} WHERE {id_column}=?",
                    (identity["version_ref"],)).fetchone()
                if row is None:
                    return None
                formal = json.loads(row["record_json"])
                from .store import content_hash
                if (formal.get("id") != identity["version_ref"]
                        or formal.get(subject_column) != subject or row[subject_column] != subject
                        or formal.get("content_hash") != row["content_hash"]
                        or content_hash({k:v for k,v in formal.items() if k != "content_hash"}) != row["content_hash"]):
                    return None
                products = final_surface_products(connection, mission, company, kinds={identity["kind"]})
            else:
                products = research_library(connection, mission, company, localize=False)["products"]
            product = next((p for p in products if p.get("status") == "available"
                            and _identity(p) == dict(identity)), None)
            if product is None:
                continue
            # Exact inverse projection proof, not a caller-selected source key.
            if publication_source_hash(product) != previous_projection_hash:
                return None
            if _hash(product) == previous_projection_hash or source_blockers(product):
                return None
            return {"classification": "current_source_pending_language",
                    "source_hash": product.get("authority_content_hash", product["content_hash"]),
                    "current_projection_hash": _hash(product),
                    "prior_projection_hash": previous_projection_hash,
                    "version_ref": product["version_ref"]}
        return None
    except (ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, OSError):
        # Without current authority and the exact old limiter identity, keep
        # the recorded warning. Never infer success from a kind or short text.
        return None
    finally:
        if connection is not None:
            connection.close()


def parked_publications(state_dir: Path | None, labels: Mapping[str, str] | None = None
                        ) -> list[dict[str, Any]]:
    """Final texts no automatic retry will reopen, one card each (2026-10-09).

    Two kinds, both read from the worker's own files and nothing else: a
    current product whose last failure no retry class covers (or whose one
    retry for its round is spent), and a UI-text batch the worker sealed as
    ``blocked`` after its attempt limit.  Each card carries the in-page
    action 「重新进行语言质检」 -- the cockpit's ``language_retry``, through
    the writer as the owner -- which prepares it again from a fresh draft.
    """

    if state_dir is None:
        return []
    from .final_text_state import publication_work_dir
    from .research_publication_worker import (
        _hash, awaiting_owner, failure_reasons, retry_chain)

    work = publication_work_dir(state_dir)
    record = _read_json(work / "worker-last-run.json")
    if not isinstance(record, Mapping):
        return []
    labels = labels or {}
    out: list[dict[str, Any]] = []
    root = work / "products"
    for row in record.get("products") or ():
        if not isinstance(row, Mapping) or not row.get("parked") \
                or not isinstance(row.get("identity"), Mapping):
            continue
        identity = dict(row["identity"])
        identity_hash = _hash(identity)
        state = _read_json(root / f"{identity_hash}.json")
        if not isinstance(state, Mapping) or state.get("product_hash") != row.get("product_hash"):
            continue
        kind = str(identity.get("kind") or "")
        subject = str(identity.get("subject_ref") or "")
        what = PUBLICATION_KIND_LABELS.get(kind, "研究产物")
        company = labels.get(subject) or (_short(subject) if subject.startswith("company:") else "")
        result = state.get("result") if isinstance(state.get("result"), Mapping) else {}
        if state.get("status") == "skipped" and result.get("skip_reason") == "source_section_empty":
            # Batch C2: a recorded skip, not a failed proofread -- a language
            # retry cannot help (the worker refuses it); the source needs a
            # redraft.  No retry button.
            sections = [str(x) for x in result.get("empty_sections") or ()][:8]
            current_source = _current_skipped_source(state_dir, identity, state["product_hash"])
            if current_source is not None:
                card = _item(
                    "publication_parked", ref=f"publication-skipped:{identity_hash}",
                    at=str(result.get("skipped_at") or ""),
                    title=f"{company + ' 的' if company else ''}{what}正等待中文版核验",
                    why="当前正式源稿已通过正文完整性检查；之前的空节提示来自旧版展示字段检查。中文版仍需整理并通过语言核验。",
                    action="系统按现有预算与调用限额继续中文整理和核验，结果可在研究页查看。",
                    consequence="当前正式版本保留，语言核验通过前不把它标为中文定稿。",
                    where=NO_ACTION_WHERE, actionable=False,
                    detail={"identity_hash":identity_hash,"product_hash":state["product_hash"],
                            "product_kind":kind,"product_kind_label":what,"subject_ref":subject,
                            "company_ref":subject if subject.startswith("company:") else None,
                            "status":"waiting_publication","original_worker_status":"skipped",
                            "original_skip_reason":"source_section_empty",**current_source})
                card["responsibility"]="background"
                out.append(card)
                continue
            held_gap = _required_initial_screen_gaps(state_dir, identity, state.get("product_hash"))
            if held_gap is not None:
                sections = [b["title"] for b in held_gap["required_section_gaps"]][:8]
                card = _item(
                    "publication_parked", ref=f"publication-skipped:{identity_hash}",
                    at=str(result.get("skipped_at") or ""),
                    title=f"{company + ' · ' if company else ''}初筛报告待补齐",
                    why="部分必需章节还缺少资料和研究结论，具体缺项见下方。",
                    action="系统按研究计划继续补充资料和报告正文。你可以查看当前报告与具体缺项。",
                    consequence="未补齐的章节保留缺项说明，已有研究仍可查看。",
                    where=NO_ACTION_WHERE, actionable=False,
                    detail={"identity_hash":identity_hash,"product_hash":state.get("product_hash"),
                            "product_kind":kind,"product_kind_label":what,"subject_ref":subject,
                            "company_ref":subject,"version_ref":identity["version_ref"],
                            "status":"waiting_material","skip_reason":"source_section_empty",
                            "empty_sections":sections,**held_gap})
                card["responsibility"]="background"
                out.append(card)
                continue
            out.append(_item(
                "publication_parked", ref=f"publication-skipped:{identity_hash}",
                at=str(result.get("skipped_at") or ""),
                title=(f"{company + ' 的' if company else ''}{what}没有生成中文版：源稿有空节"
                       f"（{'、'.join(sections) or '未记录'}）"),
                why=(f"这份{what}的源稿里有小节没有可发布的正文，系统不为空节生成中文版，"
                     "也不会自动重试（没有花费）。"),
                action=("重新起草这份研究产物（新版本会自动重新进入中文定稿）；"
                        f"暂时不需要也可以点「{_term('ack', 'noted')}」。"),
                consequence=f"不处理，这份{what}一直没有中文定稿；其他研究不受影响。",
                where="本卡片",
                detail={"identity_hash": identity_hash, "product_hash": state.get("product_hash"),
                        "product_kind": kind, "product_kind_label": what,
                        "subject_ref": subject or None,
                        "company_ref": subject if subject.startswith("company:") else None,
                        "version_ref": identity.get("version_ref"), "status": "skipped",
                        "skip_reason": "source_section_empty", "empty_sections": sections,
                        "reason": str(result.get("reason") or "")[:600]},
            ))
            continue
        try:
            chain = retry_chain(root, identity_hash)
        except (OSError, ValueError):
            continue
        if not awaiting_owner(state, chain):
            continue
        reasons = failure_reasons(state.get("result"))
        reason = reasons[0] if reasons else "未记录原因"
        failed_at = str((state.get("result") or {}).get("failed_at") or "")
        card = _item(
            "publication_parked", ref=f"publication-parked:{identity_hash}",
            at=failed_at,
            title=(f"{company + ' 的' if company else ''}{what}中文定稿暂未完成：中文定稿校对没有通过，"
                   "系统不会再自动重试"),
            why=(f"这份{what}的中文版本没有通过中文定稿校对（原因已记录在技术信息里），"
                 "这类情况系统不会自动重试；研究页上暂时显示它的初稿。"),
            action=(f"管理员可在本卡片使用「{LANGUAGE_RETRY_LABEL}」：系统会丢弃上次的译稿，重新翻译并校对"
                    f"（约 5 分钟内开始）。暂时不需要这份中文，也可以点「{_term('ack', 'noted')}」。"),
            consequence=f"不处理，这份{what}一直只有英文原稿或草稿，中文定稿出不来；其他研究不受影响。",
            where="本卡片",
            detail={"identity_hash": identity_hash, "product_hash": state.get("product_hash"),
                    "product_kind": kind, "product_kind_label": what,
                    "subject_ref": subject or None,
                    "company_ref": subject if subject.startswith("company:") else None,
                    "version_ref": identity.get("version_ref"), "status": state.get("status"),
                    "reason": reason[:600],
                    "retry": {"action": "language_retry",
                              "payload": {"identity_hash": identity_hash,
                                          "product_hash": state.get("product_hash")}}},
            actionable=False,
        )
        card["responsibility"] = "administrator"
        out.append(card)
    ui = record.get("ui_texts") if isinstance(record.get("ui_texts"), Mapping) else {}
    for batch in ui.get("batches") or ():
        if not isinstance(batch, Mapping) or batch.get("status") != "blocked":
            continue
        batch_ref = str(batch.get("batch_ref") or "")
        if not batch_ref.startswith("ui-text-batch:"):
            continue
        reason = str(batch.get("reason") or "")
        tail = reason.split("最后一次失败原因：", 1)[-1]
        card = _item(
            "publication_parked", ref=f"publication-parked:{batch_ref}",
            at=str(record.get("checked_at") or ""),
            title=(f"部分研究结论的中文译稿待重新生成：已连续 {batch.get('attempts') or '多'} 次没有通过校对"),
            why=("这批短句（研究页上的结论、证据说明等）的中文译稿连续没有通过校对（原因已记录在技术信息里），"
                 "系统不会再自动重试；这些短句在页面上暂时显示英文原文。"),
            action=(f"点本卡片上的「{LANGUAGE_RETRY_LABEL}」：系统会丢弃上次的译稿，从头重新翻译并校对"
                    f"（约 5 分钟内开始）。不需要也可以点「{_term('ack', 'noted')}」。"),
            consequence="不处理，这批短句在研究页上一直显示英文；其他研究不受影响。",
            where="本卡片",
            detail={"batch_ref": batch_ref, "attempts": batch.get("attempts"),
                    "reason_class": batch.get("reason_class"), "reason": reason[:600],
                    "last_failure": _summary(tail, 160),
                    "retry": {"action": "language_retry", "payload": {"batch_ref": batch_ref}}},
            actionable=False,
        )
        card["responsibility"] = "administrator"
        out.append(card)
    return out


def group_ui_text_batches(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several parked UI-text batches become one card (2026-10-09, T01).

    Each batch keeps its own card ref, condition hash and retry payload under
    ``detail.members`` -- acknowledging or retrying the group is the same
    per-batch write for each member, so the acknowledgement book and the
    publication worker see exactly what they saw before.  One batch stays a
    card of its own.
    """

    batches = [row for row in items if row.get("kind") == "publication_parked"
               and str(row.get("ref") or "").startswith("publication-parked:ui-text-batch:")]
    if len(batches) < 2:
        return items
    from .needs_human_ack import acknowledge_entry

    members = []
    for row in batches:
        detail = row.get("detail") or {}
        members.append({
            "ref": row["ref"], "at": row.get("at"), "title": row.get("title"),
            "attempts": detail.get("attempts"), "batch_ref": detail.get("batch_ref"),
            "last_failure": detail.get("last_failure"), "reason": detail.get("reason"),
            "retry": detail.get("retry"), "acknowledge": acknowledge_entry(row),
        })
    newest = max((str(row.get("at") or "") for row in batches), default="")
    group = _item(
        "publication_parked", ref=UI_TEXT_GROUP_REF, at=newest,
        title=f"部分研究结论的中文译稿待重新生成（{len(batches)} 批）",
        why=(f"{len(batches)} 批短句（研究页上的结论、证据说明等）的中文译稿连续没有通过校对，"
             "系统不会再自动重试；这些短句在页面上暂时显示英文原文。原因相同，所以合并成一件事。"),
        action=(f"点「{UI_TEXT_GROUP_RETRY_LABEL}」一次重新生成全部 {len(batches)} 批（约 5 分钟内开始）；"
                "也可以展开逐批处理，或点「全部已知悉」。"),
        consequence="不处理，这些短句在研究页上一直显示英文；其他研究不受影响。",
        where="本卡片",
        detail={"group": "ui_text_batches", "count": len(batches), "members": members},
        actionable=False,
    )
    group["responsibility"] = "administrator"
    out = [row for row in items if row not in batches]
    out.append(group)
    return out


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
    labels = _company_labels(core) if live else {}
    out: list[dict[str, Any]] = []
    labels = _company_labels(core) if live else {}
    for proposal in live:
        out.append(_item(
            "reopen_proposal", ref=str(proposal["proposal_ref"]),
            at=str(proposal["created_at"]),
            title=f"初步筛查报告是否要重出一版：{_company_label(labels, proposal['company_ref'])}",
            why="这家公司通过初筛之后又积累了新证据，系统认为结论可能会变。",
            action="打开「待办审批」页，选「批准修订」让系统重出一版，或「保留当前版本」。",
            consequence="不处理，后续研究继续建立在可能已经过时的那版筛查结论上。",
            where=APPROVE_VIEW_LABEL,
            where_view=APPROVE_VIEW,
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
        if probe.get("lifecycle", "active") != "active":
            continue
        name = str(probe.get("name") or probe.get("slug") or "研究环境")
        core = _open(probe.get("core_db"))
        snapshot = None
        if core is None:
            try:
                from .readonly_sqlite import connect_cold_wal_snapshot
                snapshot = connect_cold_wal_snapshot(probe.get("core_db"))
                core = snapshot.__enter__()
            except (sqlite3.Error, OSError, ValueError, TypeError):
                continue
        try:
            has = _table(core, "coverage_mission_pointer") and core.execute(
                "SELECT 1 FROM coverage_mission_pointer LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            continue
        finally:
            if snapshot is None:
                core.close()
            else:
                snapshot.__exit__(None, None, None)
        if has:
            continue
        created = probe.get("kept_at") or probe.get("created_at")
        age_days = 0
        if isinstance(created, str):
            try:
                timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if timestamp.tzinfo is not None:
                    age_days = max(0, (datetime.now(timezone.utc) - timestamp).days)
            except ValueError:
                pass
        old_empty = age_days >= 7 and probe.get("slug") != "legacy"
        out.append(_item(
            "empty_workspace" if old_empty else "no_active_mission", ref=f"workspace:{probe.get('slug') or name}",
            at=str(probe.get("created_at") or ""),
            title=(f"研究项目「{name}」已空置 {age_days} 天" if old_empty else f"研究环境「{name}」还没有研究目标"),
            why="这个研究项目还没有生效的研究目标，一条研究都不会开始。",
            action=("在研究项目管理中选择暂停、删除或继续保留；删除前检查待决策项和未发布草稿。" if old_empty else
                    f"打开「{name}」这个环境的首页，写下你要研究什么（行业、公司范围、想回答的问题），确认发布。"),
            consequence="不确认，这个研究项目一直没有研究产出；设定研究目标后即可开始。",
            where=WORKSPACE_VIEW_LABEL,
            where_view=WORKSPACE_VIEW,
            detail={"slug": probe.get("slug"), "url": probe.get("url"), "age_days": age_days,
                    "actions": ["pause", "delete", "keep"] if old_empty else ["set_goal"]},
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
                       "url": record.get("url"), "core_db": core, "lifecycle": record.get("lifecycle", "active"),
                       "created_at": record.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                       "kept_at": record.get("kept_at")})
    return probes


def workspaces_in_scope(
    probes: Sequence[Mapping[str, Any]], environment: str | None,
) -> list[Mapping[str, Any]]:
    """The environments one environment's page is allowed to talk about: itself.

    ``environment`` is a workspace slug, or ``LEGACY_ENVIRONMENT``/``None`` for
    the legacy environment.  The legacy environment owns no workspace, so it
    keeps none of the probes; a workspace keeps exactly the probe that is
    itself.  Everything else in :func:`collect` already reads only the Core and
    the state directory of the environment asking, so this is the one place
    where the scope has to be said out loud.
    """

    if environment is None or environment == LEGACY_ENVIRONMENT:
        return []
    return [probe for probe in probes
            if str(probe.get("slug") or "") == environment]


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
    environment: str | None = None,
    clock: Any | None = None,
) -> dict[str, Any]:
    """Everything waiting on a person, in the order they should deal with it.

    Every argument is optional and every probe is independent.  A Core with no
    Scheduler beside it still gets its gate decisions; a state directory with no
    governance folder still gets its sources.  Partial is the normal case and
    the list says which probes ran.

    ``environment`` says whose list this is: a workspace slug, or
    ``LEGACY_ENVIRONMENT``.  An unscoped call means the legacy environment --
    the safe reading, because a list that names no environment cannot honestly
    speak for one that has a slug, and because every caller that represents a
    workspace knows its slug and passes it.  So the legacy environment lists no
    workspace items at all and a workspace lists only its own missing research
    goal; every other probe here already reads nothing but the Core and the
    state directory it was handed.
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
    # Under a legacy scope nobody else's Core is even opened: the cheapest way
    # to keep one environment out of another's files is not to go looking.
    probes: Sequence[Mapping[str, Any]] = (
        [] if environment is None or environment == LEGACY_ENVIRONMENT
        else (list(workspaces or [])
              or workspace_probes(workspace_manager_config_path)))

    items: list[dict[str, Any]] = []
    items += workspaces_without_mission(workspaces_in_scope(probes, environment))
    items += gate_decisions(core, state_dir=state)
    items += memo_decisions(core)
    items += scenario_decisions(core)
    items += held_lanes(heartbeat, state_dir=state, core=core)
    items += provider_failures(scheduler_db, now=now, model_router_db=model_router_db)
    items += publication_route_blocks(state)
    items += parked_publications(state, _company_labels(core) if core is not None else {})
    items += connector_login_lapses(state, now=now)
    items += unconnected_sources(core)
    items += governance_records(None if governance_dir is None else Path(governance_dir))
    items += open_reopen_proposals(core)
    items += auto_returned_drafts(state, _company_labels(core))
    items += raw_spool_capacity(state, now=now)
    items += capped_fetches(core, now=now)
    items += earnings_release_failures(core)
    items += model_coverage_gaps(core)
    items += stalled_foundations(state, now=now)
    items += material_acquisition_cards(state, now=now, labels=_company_labels(core) if core is not None else {})
    # 2026-10-09: the owner's 「已知悉 / 暂不提醒 / 放弃这份资料」.  A card
    # stays hidden while the newest acknowledgement still matches what it is
    # about and has not expired; anything else brings it back by itself.
    from .needs_human_ack import latest_acknowledgements, split_acknowledged

    try:
        acks = latest_acknowledgements(core)
    except sqlite3.Error:
        acks = {}
    items, acknowledged = split_acknowledged(items, acks, now=now)
    if core is not None:
        core.close()
    items = group_ui_text_batches(items)

    items.sort(key=lambda row: (row["urgency"], row["at"] or "", row["kind"], row["ref"]))
    items = items[:MAX_ITEMS]
    for row in items:
        row["entry"] = completion_entry(row)
        analyst_view(row)
    actionable = [row for row in items if row["actionable"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": _iso(now),
        "count": len(actionable),
        "total": len(items),
        "items": items,
        "by_kind": _counts(items),
        "headline": _headline(actionable),
        "acknowledged": [{**row, "title": scrub(row.get("title"))} for row in acknowledged],
    }


# 2026-10-08 analyst UX review 1.5: what the page can do with each card.  The
# 资料来源 dialog lists sources and their state; it has no approve, connect or
# upload button, so a card that sends the owner there says so instead of
# leaving them to look for one.  The same holds for the company drawer, which
# shows what is missing but cannot add a document or change a search plan.
SOURCES_DIALOG_NOTE = ("「资料来源」页只列出各来源的状态，目前没有批准、连接或补传资料的按钮；"
                       "这一步需要技术管理员完成。")
COMPANY_GAP_NOTE = ("公司详情里能看到缺什么；工作台上暂时没有补传资料、修改检索计划或放宽提交标准的按钮，"
                    "需要时请交给技术管理员。")
NO_PAGE_ACTION_NOTE = "工作台上没有完成这一步的按钮。"
TECHNICAL_ADMIN_NOTE = "工作台上没有完成这一步的按钮；需要技术管理员在本机处理，命令见技术详情。"
_COMPANY_GAP_KINDS = frozenset({"data_foundation_stalled", "gate_auto_returned"})


def completion_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    """How the owner finishes this card on the cockpit page, or that they cannot.

    ``mode`` is one the page knows: ``dialog`` (open 资料来源 / 模型与预算),
    ``approvals`` (the decision on the 待办审批 page), ``company`` (open the
    company's research detail), ``goal_input`` (write this environment's goal),
    ``view`` (another page), ``link`` (an https original), or ``none`` -- in
    which case ``note`` says plainly that nothing on the page does it.
    """

    kind = str(item.get("kind") or "")
    where = str(item.get("where") or "")
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
    actionable = bool(item.get("actionable", True))
    entry: dict[str, Any]
    if kind == "empty_workspace":
        entry = {"mode": "admin", "href": "/v2/admin/#projects", "tab": "projects", "label": "管理空置研究项目"}
    elif kind == "no_active_mission":
        entry = {"mode": "goal_input", "label": "去写研究目标"}
    elif kind == "model_coverage_gap" and detail.get("company_ref"):
        entry = {"mode": "company", "company_ref": str(detail["company_ref"]),
                 "label": "查看财务预测进度", "note": "这条是研究进度提醒，无需审批；模型发布后自动消失。"}
    elif kind in _COMPANY_GAP_KINDS and detail.get("company_ref"):
        entry = {"mode": "company", "company_ref": str(detail["company_ref"]),
                 "label": "打开这家公司的研究详情", "note": COMPANY_GAP_NOTE}
    elif item.get("where_open") == SOURCES_DIALOG:
        entry = {"mode": "dialog", "open": SOURCES_DIALOG,
                 "label": f"打开「{SOURCES_DIALOG_LABEL}」", "note": SOURCES_DIALOG_NOTE}
    elif item.get("where_open") == MODELS_DIALOG:
        entry = {"mode": "dialog", "open": MODELS_DIALOG, "label": f"打开「{MODELS_DIALOG_LABEL}」"}
    elif kind == "controlled_recovery" and where == ADMIN_RUN_WHERE:
        # 2026-10-09: the doors are buttons -- on the card (``actions``) and in
        # 管理员设置 → 运行控制 (``href``); the CLI stays in 技术详情.
        entry = {"mode": "admin", "href": ADMIN_RUN_HREF, "tab": "run",
                 "label": "在「管理员设置 → 运行控制」处理",
                 "actions": list(detail.get("owner_doors") or ()),
                 "note": "每个操作都需要写一句理由，会记录在项目历史中。"}
        # 2026-10-09: the hand-off to the technical administrator, and a task
        # the system retries by itself, are said on the card -- not buttons.
        if isinstance(detail.get("handoff"), Mapping):
            entry["handoff"] = dict(detail["handoff"])
        if isinstance(detail.get("automatic_retry"), Mapping):
            entry["automatic_retry"] = dict(detail["automatic_retry"])
    elif kind == "publication_parked" and isinstance(detail.get("retry"), Mapping):
        entry = {"mode": "action", "action": detail["retry"]["action"],
                 "payload": dict(detail["retry"]["payload"]),
                 "label": LANGUAGE_RETRY_LABEL,
                 "confirm": "重新生成中文定稿：丢弃上次的译稿，从头翻译并校对。确认？",
                 "note": "约 5 分钟内开始；完成后这张卡片自动消失。"}
    elif kind == "publication_parked" and detail.get("group") == "ui_text_batches":
        # One button for the group; each member keeps its own retry and
        # acknowledgement (the page writes them one by one).
        entry = {"mode": "action_group", "action": "language_retry",
                 "label": UI_TEXT_GROUP_RETRY_LABEL,
                 "payloads": [dict(m["retry"]["payload"]) for m in detail.get("members") or ()
                              if isinstance(m.get("retry"), Mapping)],
                 "members": [{"ref": m.get("ref"), "title": m.get("title"),
                              "attempts": m.get("attempts"), "at": m.get("at"),
                              "retry": m.get("retry"), "acknowledge": m.get("acknowledge")}
                             for m in detail.get("members") or ()],
                 "note": "约 5 分钟内开始；全部完成后这张卡片自动消失。"}
    elif item.get("where_view") == APPROVE_VIEW:
        # Only cards with a decision on the approvals list point there.
        entry = {"mode": "approvals", "label": "到「需要你确认的研究决定」处理"}
    elif item.get("where_view"):
        entry = {"mode": "view", "view": str(item["where_view"]), "label": f"打开「{where}」页"}
    elif kind == "earnings_release_unread" and str(detail.get("exhibit_url") or "").startswith("https://"):
        entry = {"mode": "link", "href": str(detail["exhibit_url"]), "label": "打开 SEC 上的 8-K 原文",
                 "note": "无需你操作；申报数入库后这张卡会自动消失。"}
    elif kind == "material_budget_exceeded":
        entry = {"mode": "none", "label": where,
                 "note": "在本卡片上批准追加额度即可；不批准，系统按额度继续，等待中的请求以后自动继续。"}
    elif kind == "connector_login_expired":
        entry = {"mode": "none", "label": where,
                 "note": "工作台上没有重新登录的按钮；请在本机的这个应用里重新登录，恢复后系统自动继续。"}
    elif where == TECHNICAL_ADMIN_WHERE:
        entry = {"mode": "none", "label": where, "note": TECHNICAL_ADMIN_NOTE}
    elif where == NO_ACTION_WHERE or not actionable:
        entry = {"mode": "none", "label": where or NO_ACTION_WHERE, "note": "无需你操作。"}
    else:
        entry = {"mode": "none", "label": where, "note": NO_PAGE_ACTION_NOTE}
    # 2026-10-09: a card with nothing to press can be acknowledged (已知悉 /
    # 暂不提醒 N 天 / 放弃这份资料); it comes back by itself when what it is
    # about changes.  The original way to look at it stays as ``secondary``.
    from .needs_human_ack import acknowledge_entry

    acknowledge = None if entry.get("mode") == "action_group" or (item.get("actionable") is False and item.get("responsibility") == "background") else acknowledge_entry(item)
    if acknowledge is not None:
        if entry.get("mode") == "action":
            entry["acknowledge"] = acknowledge
        else:
            secondary = {key: value for key, value in entry.items() if key != "mode"}
            entry = {"mode": "acknowledge", "label": f"{_term('ack', 'noted')} / {_term('ack', 'snooze')}",
                     **acknowledge,
                     **({"secondary": {"mode": entry["mode"], **secondary}}
                        if entry.get("mode") not in {None, "none"} else {}),
                     **({"secondary_note": entry.get("note")} if entry.get("note") else {})}
    return entry


#: The card fields an analyst reads (the page, Feishu, the brief).  Batch am:
#: every one passes through the terminology registry's scrub() at the one
#: place cards leave this module, so a stored reason, an older ledger line or
#: a new probe written tomorrow reads in the same vocabulary.  ``action`` and
#: ``detail.commands`` are the technical administrator's (技术详情) and keep
#: their CLI words.
ANALYST_FIELDS = ("title", "why_blocked", "consequence", "urgency_label", "where")
_ENTRY_FIELDS = ("label", "confirm", "note", "secondary_note")


def _scrub_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        return
    for name in _ENTRY_FIELDS:
        if isinstance(entry.get(name), str):
            entry[name] = scrub(entry[name])
    for key in ("actions", "choices"):
        for option in entry.get(key) or ():
            _scrub_entry(option)
    for member in entry.get("members") or ():
        if isinstance(member, dict):
            if isinstance(member.get("title"), str):
                member["title"] = scrub(member["title"])
            _scrub_entry(member.get("acknowledge"))
    _scrub_entry(entry.get("acknowledge"))
    _scrub_entry(entry.get("secondary"))


def analyst_view(row: dict[str, Any]) -> dict[str, Any]:
    """Scrub one card's analyst-visible fields in place (and return it)."""

    for name in ANALYST_FIELDS:
        if isinstance(row.get(name), str):
            row[name] = scrub(row[name])
    detail = row.get("detail")
    if isinstance(detail, dict):
        for name in ("reason_label", "lane_label"):
            if isinstance(detail.get(name), str):
                detail[name] = scrub(detail[name])
        for door in detail.get("owner_doors") or ():
            _scrub_entry(door)
    _scrub_entry(row.get("entry"))
    return row


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
    "CONTRACT_RETRIED_REASON",
    "ESCALATED_HOLD_REASONS",
    "HOLDS_LEDGER_FILE",
    "LANE_HOLD_LEDGERS",
    "REENTRY_REBOUND_REASON",
    "UNPROVED_SEND_RETRIED_REASON",
    "PROVIDER_BUDGET_REFUSED_REASON",
    "PROVIDER_BUDGET_CORRECTED_REASON",
    "PROVIDER_BUDGET_CORRECTION_REFUSED_REASON",
    "lane_hold_ledgers",
    "recovery_command",
    "COOLDOWN_TABLES",
    "model_cooldowns",
    "GOVERNANCE_DIR_NAME",
    "HELD_LANE_STATUSES",
    "KINDS",
    "LEGACY_ENVIRONMENT",
    "MAX_ITEMS",
    "PROVIDER_ERROR_CODES",
    "PROVIDER_FAILURE_FLOOR",
    "PROVIDER_WINDOW_HOURS",
    "RAW_SPOOL_DEFAULT_MAX_TOTAL_BYTES",
    "RAW_SPOOL_WARNING_RATIO",
    "SCHEMA_VERSION",
    "URGENCY",
    "URGENCY_LABELS",
    "raw_spool_capacity",
    "auto_returned_drafts",
    "collect",
    "analyst_view",
    "completion_entry",
    "connector_login_lapses",
    "stalled_foundations",
    "dossier_for",
    "gate_decisions",
    "governance_records",
    "held_lanes",
    "open_reopen_proposals",
    "provider_failures",
    "unconnected_sources",
    "workspace_probes",
    "workspaces_in_scope",
    "workspaces_without_mission",
]
