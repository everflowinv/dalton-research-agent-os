"""Read-only data the administrator page needs and no ``/v1/cockpit/*`` view serves.

2026-10-08 (cockpit v2, analyst UX review §1.3 / §9.1 「管理员：服务和诊断」):
everything the review moves out of the analyst's view lands on one
administrator page.  Most of it already has a cockpit view (models, verifier
independence, sources, levers, ops, fund templates, weekly brief, workspaces)
and the page reads those directly.  This module answers the rest:

* ``connections``  -- connector governance records, login-expired holds,
  credential slots, discovery plans (owner-governed fields vs code-owned specs
  with drift), source budgets, IR page declaration, Feishu delivery contract
  and receipts;
* ``costs``        -- today / this week, by lane ("purpose") and pool and by
  environment, from the budget day ledgers;
* ``runtime``      -- owner-door jobs and their states, restart / deploy
  guidance as copyable commands;
* ``health``       -- parity rows, the failure ledger per lane in four cells,
  writer / tick health, recent incidents, publication worker summary.

Every read here is read-only: SQLite through ``mode=ro`` URIs, files through
``read_text``/``stat``.  Nothing connects to a socket, runs a process or calls
the writer.  Each section is wrapped so one unreadable source becomes a
``{"available": False, "reason": ...}`` block rather than a failed page: a
fresh workspace is a normal state, not a fault.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Bounds: a page that refreshes must not walk an unbounded directory.
MAX_PUBLICATION_FILES = 20000
PUBLICATION_TTL_SECONDS = 60.0
MAX_RETRY_ITEMS = 20
MAX_GOVERNANCE_FILES = 200
MAX_OWNER_DOOR_JOBS = 40
MAX_DELIVERY_RECEIPTS = 12
#: How long an expensive section (parity) is reused.
PARITY_TTL_SECONDS = 120.0
COST_TTL_SECONDS = 30.0

#: Known incidents worth keeping in front of the administrator.  The
#: 2026-10-07 store-thread stall is the type the brief names: an owner door
#: rebuilt the runtime on the writer's single store thread right after a cold
#: start; batch s moved owner doors onto a background job ledger.
KNOWN_INCIDENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "incident:2026-10-07-store-thread-stall",
        "at": "2026-10-07T01:01:00+00:00",
        "resolved_at": "2026-10-07T01:20:00+00:00",
        "title": "写入服务（writer）存储线程卡住约 15 分钟",
        "kind": "store_thread_stall",
        "kind_label": "写入服务存储线程阻塞",
        "what": ("部署后 writer 冷启动，紧接着执行了重型负责人放行操作（authorize-unproved），"
                 "该操作在 store 线程上同步重建运行环境，一条读查询占住线程约 15 分钟，"
                 "期间所有流程的调度都没有完成。"),
        "impact": "期间研究流程全部停顿；重启 writer 后恢复，三个环境正常。",
        "fix": ("批次 s 起，负责人放行操作改为后台任务并留回执（owner_door_job_events），"
                "writer 启动后前 10 分钟拒绝重型放行操作，而不是排队。"),
        "watch": "部署后 10 分钟内不要执行放行/撤回类命令；看下方「负责人放行任务」的状态。",
        "doc": "docs/ops/pending-deploy.md",
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _guard(name: str, build: Callable[[], Any]) -> Any:
    """One section; an exception becomes a readable unavailable block."""

    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - a section must not fail the page
        return {"available": False,
                "reason": f"{name} 暂时读不到：{type(exc).__name__}: {str(exc)[:200]}"}


#: Words the 2026-10-09 reviews ask the page not to show (MUSE §10.1):
#: internal terms some upstream sentences still carry.
PLAIN_WORDS = (("车道", "研究流程"), ("票据", "执行凭证"), ("工单", "任务"))


def plain(text: Any) -> Any:
    """The administrator's wording of an upstream sentence."""

    if not isinstance(text, str):
        return text
    for word, replacement in PLAIN_WORDS:
        text = text.replace(word, replacement)
    return text


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def split_fix(text: Any) -> tuple[str | None, str | None]:
    """A parity ``fix`` as (plain sentence, technical detail).

    A sentence in Chinese is shown; a command or an English engineering note
    goes under 技术详情, collapsed (MUSE P0 #8: no command shown by default).
    """

    if not isinstance(text, str) or not text.strip():
        return None, None
    return (plain(text), None) if _has_cjk(text) else (None, text)


def _company_labels(plane: Any) -> dict[str, Any]:
    try:
        with plane._core() as core:
            return plane._members(plane._mission(core))
    except Exception:  # noqa: BLE001 - a fresh workspace has no mission
        return {}


def _company_label(subject: Any, members: Mapping[str, Any]) -> str | None:
    if not isinstance(subject, str) or not subject:
        return None
    try:
        from ...cockpit_plane import _ops_item_label

        label = _ops_item_label(subject, members)
    except Exception:  # noqa: BLE001
        return None
    if label in {"任务记录", "整条流程"} or not any(ch.isalpha() for ch in str(label)):
        return None  # no bare CIK digits or hashes as a company name
    return label


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _connect_ro(path: str | Path | None) -> sqlite3.Connection | None:
    if path is None:
        return None
    target = Path(path).expanduser()
    if not target.is_file():
        return None
    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _state_dir(plane: Any) -> Path:
    return Path(plane.config.state_dir)


def _model_config(plane: Any) -> dict[str, Any]:
    path = getattr(plane.config, "model_config_path", None)
    if path is None:
        return {}
    value = _read_json(Path(path))
    return value if isinstance(value, dict) else {}


def _mission(plane: Any) -> dict[str, Any] | None:
    from ...cockpit_plane import CockpitMissionMissing

    try:
        with plane._core() as core:
            return plane._mission(core)
    except (CockpitMissionMissing, sqlite3.Error, OSError):
        return None


def environment_identity(plane: Any) -> dict[str, Any]:
    context = dict(getattr(plane, "workspace_context", None) or {})
    return {
        "mode": context.get("mode") or "legacy",
        "slug": context.get("slug"),
        "workspace_id": context.get("workspace_id"),
        "name": context.get("name") or ("原研究环境" if not context.get("slug") else context.get("slug")),
    }


# -- 数据连接 ----------------------------------------------------------------

GOVERNANCE_STATUS_LABELS = {
    "approved": "已批准", "active": "已批准（生效中）", "proposed": "已提出，等待批准",
    "rejected": "已拒绝", "revoked": "已撤销",
}


def governance_records(plane: Any, actor_ref: str) -> dict[str, Any]:
    directory = _state_dir(plane) / "connector-governance"
    if not directory.is_dir():
        return {"available": True, "items": [], "note": "本环境还没有任何数据源接口的治理记录。"}
    items = []
    for path in sorted(directory.glob("*.json"))[:MAX_GOVERNANCE_FILES]:
        record = _read_json(path)
        if not isinstance(record, Mapping):
            items.append({"file": path.name, "status": "unreadable",
                          "status_label": "文件无法读取", "approved": False})
            continue
        status = str(record.get("status") or "")
        permissions = record.get("allowed_permissions") if isinstance(
            record.get("allowed_permissions"), Mapping) else {}
        approved = status in {"approved", "active"}
        item = {
            "file": path.name,
            "id": record.get("id"),
            "capability_id": record.get("capability_id") or record.get("kind"),
            "status": status, "status_label": GOVERNANCE_STATUS_LABELS.get(status, status or "未标明"),
            "approved": approved,
            "approved_by_set": bool(record.get("approved_by")),
            "approved_at": record.get("approved_at"),
            "effective_from": record.get("effective_from"),
            "risk_class": permissions.get("risk_class"),
            "network": permissions.get("network"),
        }
        if not approved:
            item["commands"] = [{
                "label": "先查看记录，确认权限与风险等级后再批准（批准需要负责人身份）",
                "preview": f"python -m dalton_core.connector_governance_cli show --path {path}",
                "apply": (f"python -m dalton_core.connector_governance_cli approve --path {path} "
                          f"--approved-by {actor_ref}"),
            }]
        items.append(item)
    return {"available": True, "items": items,
            "pending": sum(1 for item in items if not item.get("approved")),
            "note": ("数据源接口的批准目前只有命令行入口：下面给出只读查看和批准两条命令。"
                     "批准后，相关研究流程下一轮即可使用该接口。")}


SOURCE_NAMES = {"alphaengine": "AlphaEngine", "guidepoint": "Guidepoint", "sec-edgar": "SEC EDGAR",
                "sec": "SEC EDGAR", "web": "网络检索", "yfinance": "行情源（yfinance）"}


def source_label(ref: Any) -> str:
    """A data source's name for a person, from its ``source:...`` reference."""

    raw = str(ref or "").removeprefix("source:")
    head = raw.split(":", 1)[0].split("/", 1)[0]
    return SOURCE_NAMES.get(head) or SOURCE_NAMES.get(raw) or (head or "数据源")


def connector_holds(plane: Any) -> dict[str, Any]:
    from ...connector_health import read_records

    records = read_records(_state_dir(plane))
    items = []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        held = record.get("status") == "held"
        blocked = record.get("blocked") if isinstance(record.get("blocked"), Mapping) else {}
        detail = record.get("last_detail") if isinstance(record.get("last_detail"), Mapping) else {}
        probe = record.get("last_probe") if isinstance(record.get("last_probe"), Mapping) else None
        items.append({
            "source_ref": record.get("source_ref"),
            "label": source_label(record.get("source_ref")),
            "status": "login_expired" if held else record.get("status"),
            "status_label": "登录已过期（冷却中）" if held else (
                "已恢复" if record.get("status") == "recovered" else str(record.get("status") or "")),
            "held": held,
            "since": record.get("since"),
            "hold_until": record.get("hold_until"),
            "recovered_at": record.get("recovered_at"),
            "blocked_searches": blocked.get("search"),
            "blocked_acquisitions": blocked.get("acquisition") or blocked.get("download"),
            "blocked": dict(blocked),
            "probes": record.get("probes"),
            "last_probe_at": None if probe is None else probe.get("at"),
            "last_probe_ok": None if probe is None else probe.get("ok"),
            "provider_message": str(detail.get("message") or "")[:300] or None,
        })
    return {"available": True, "items": items,
            "note": ("登录过期的数据源会暂停检索与下载，冷却期满后自动重试；"
                     "重新登录对应服务后，下一轮即恢复。")}


def credential_slots(plane: Any) -> dict[str, Any]:
    config = _model_config(plane)
    declared = [str(slot) for slot in config.get("credential_slot_refs") or [] if isinstance(slot, str)]
    router_db = config.get("model_router_db") if isinstance(config.get("model_router_db"), str) else None
    profiles: list[dict[str, Any]] = []
    connection = _connect_ro(router_db)
    if connection is not None:
        with closing(connection):
            try:
                rows = connection.execute(
                    "SELECT p.profile_id, p.provider, p.family, p.credential_slot_ref, p.version "
                    "FROM model_endpoint_profile_versions p JOIN ("
                    " SELECT profile_id, MAX(version) AS version FROM model_endpoint_profile_versions "
                    " GROUP BY profile_id) latest ON latest.profile_id=p.profile_id "
                    "AND latest.version=p.version ORDER BY p.credential_slot_ref, p.profile_id"
                ).fetchall()
            except sqlite3.Error:
                rows = []
        for row in rows:
            profiles.append({
                "profile_id": row["profile_id"], "provider": row["provider"],
                "family": row["family"], "credential_slot_ref": row["credential_slot_ref"],
                "slot_declared_here": row["credential_slot_ref"] in declared,
            })
    slots: dict[str, dict[str, Any]] = {}
    for slot in declared:
        slots.setdefault(slot, {"credential_slot_ref": slot, "declared": True, "profiles": []})
    for profile in profiles:
        entry = slots.setdefault(profile["credential_slot_ref"], {
            "credential_slot_ref": profile["credential_slot_ref"], "declared": False, "profiles": []})
        entry["profiles"].append(profile["profile_id"])
    return {
        "available": bool(config) or bool(profiles),
        "reason": None if config else "本环境没有模型配置文件，研究调用尚未接入。",
        "slots": sorted(slots.values(), key=lambda s: (not s["declared"], s["credential_slot_ref"])),
        "note": ("凭证槽只记录名字，密钥本身由模型网关保管，这里不读取也不显示。"
                 "「未声明」表示模型档案用到了这个槽，但本环境的模型配置没有列出它，调用会在网关被拒。"),
    }


def _plan_section(label: str, resolve: Callable[[Path], Path],
                  report: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
                  state: Path) -> dict[str, Any]:
    from ...mission_source_discovery import load_discovery_plan

    try:
        path = resolve(state)
    except Exception as exc:  # noqa: BLE001 - reported
        return {"label": label, "available": False,
                "reason": f"计划选择文件无法解析：{str(exc)[:200]}"}
    if not path.is_file():
        return {"label": label, "available": False, "file": path.name,
                "reason": "还没有已批准的检索计划（发布研究目标时会自动生成）。"}
    plan = load_discovery_plan(path)
    budget = plan.get("budget") if isinstance(plan.get("budget"), Mapping) else {}
    companies = plan.get("companies") if isinstance(plan.get("companies"), list) else []
    out: dict[str, Any] = {
        "label": label, "available": True, "file": path.name,
        "plan_ref": plan.get("id"), "plan_hash": plan.get("content_hash"),
        "owner_fields": {
            "budget": dict(budget),
            "companies": [
                {"ticker": c.get("ticker"), "name": c.get("name") or c.get("company_name")}
                if isinstance(c, Mapping) else {"ticker": str(c)} for c in companies][:80],
            "company_count": len(companies),
        },
        "specs": [],
    }
    if report is not None:
        detail = report(plan)
        out["rendered_from"] = detail.get("rendered_from")
        out["approved_plan_hash"] = detail.get("approved_plan_hash")
        out["effective_plan_hash"] = detail.get("effective_plan_hash")
        out["code_hash"] = detail.get("specs_hash") or detail.get("forms_hash")
        out["names_unresolved"] = detail.get("names_unresolved") or []
        for spec in detail.get("specs") or []:
            drift = spec.get("drift")
            if drift is None:
                if spec.get("rendered_from") not in (None, "code"):
                    drift = "plan_forms_used"
                elif spec.get("plan_forms_differ"):
                    drift = "plan_forms_ignored"
                else:
                    drift = "none"
            out["specs"].append({
                "spec_ref": spec.get("spec_ref"),
                "form": spec.get("form") or spec.get("document_type"),
                "lookback_days": spec.get("lookback_days"),
                "rediscovery_interval_days": spec.get("rediscovery_interval_days"),
                "retry_interval_days": spec.get("retry_interval_days"),
                "intervals_from": spec.get("intervals_from"),
                "drift": drift,
                "drift_label": DRIFT_LABELS.get(drift, str(drift)),
            })
    else:
        for spec in plan.get("specs") or plan.get("queries") or []:
            if isinstance(spec, Mapping):
                out["specs"].append({
                    "spec_ref": spec.get("spec_ref") or spec.get("id"),
                    "lookback_days": spec.get("lookback_days"),
                    "rediscovery_interval_days": spec.get("rediscovery_interval_days"),
                    "retry_interval_days": spec.get("retry_interval_days"),
                    "drift": "not_code_owned", "drift_label": DRIFT_LABELS["not_code_owned"],
                })
    return out


DRIFT_LABELS = {
    "none": "计划文件与代码一致",
    "added_by_code": "代码新增（计划文件未列出）",
    "form_from_code": "表单以代码为准（计划文件写法不同）",
    "plan_only": "仅计划文件有（代码未声明），按文件执行",
    "plan_forms_ignored": "计划文件里的检索式与代码不同，已忽略，以代码为准",
    "plan_forms_used": "无法按代码渲染，沿用计划文件自己的检索式",
    "not_code_owned": "该来源的检索式不由代码统一管理",
}


def discovery_plans(plane: Any) -> dict[str, Any]:
    from ...alphaengine_query_forms import query_form_report
    from ...macos_launchagent import (
        _alphaengine_discovery_plan, _sec_discovery_plan, _web_discovery_plan,
    )
    from ...sec_plan_specs import sec_plan_report

    state = _state_dir(plane)
    plans = [
        _guard("AlphaEngine 检索计划", lambda: _plan_section(
            "AlphaEngine 研报/纪要检索", _alphaengine_discovery_plan, query_form_report, state)),
        _guard("SEC 检索计划", lambda: _plan_section(
            "SEC 申报检索", _sec_discovery_plan, sec_plan_report, state)),
        _guard("网络检索计划", lambda: _plan_section(
            "网络检索", _web_discovery_plan, None, state)),
    ]
    return {"available": True, "plans": plans,
            "note": ("负责人控制的字段：预算、公司名单、各规格的回看天数与重查间隔；"
                     "检索式（表单/规格）由代码统一维护，这里只读并标出与计划文件的差异。"
                     "公司名单通过「运行控制 → 覆盖名单调整」修改，单家公司的重查频率通过「优先看这家」修改。")}


def source_budgets(plane: Any, mission: Mapping[str, Any] | None) -> dict[str, Any]:
    budget = dict((mission or {}).get("budget") or {})
    heartbeat = _read_json(Path(plane.config.heartbeat_path)) or {}
    result = ((heartbeat.get("bounded_planner") or {}).get("last_result") or {}) \
        if isinstance(heartbeat, Mapping) else {}
    discovery = result.get("mission_source_discovery") if isinstance(result, Mapping) else None
    discovery = discovery if isinstance(discovery, Mapping) else {}

    def measured(block: Any) -> dict[str, Any] | None:
        if not isinstance(block, Mapping):
            return None
        for key in ("discovery", "acquisition"):
            inner = block.get(key)
            if isinstance(inner, Mapping) and isinstance(inner.get("budget"), Mapping):
                return {"stage": key, **dict(inner["budget"])}
        if isinstance(block.get("budget"), Mapping):
            return dict(block["budget"])
        return None

    shared = None
    try:
        from ...shared_daily_budget import shared_alphaengine_view
        shared = shared_alphaengine_view(plane.config.core_db)
    except Exception:  # noqa: BLE001 - optional host-wide cap
        shared = None
    return {
        "available": True,
        "mission_caps": {
            "max_alphaengine_calls_24h": budget.get("max_alphaengine_calls_24h"),
            "max_daily_document_reads": budget.get("max_daily_document_reads"),
            "max_daily_cost_usd": budget.get("max_daily_cost_usd"),
        },
        "measured": {
            "alphaengine": measured(discovery),
            "web": measured(discovery.get("web_search")),
        },
        "shared_alphaengine": shared if isinstance(shared, Mapping) else None,
        "note": ("AlphaEngine 每 24 小时搜索上限在「预算与费用」里修改；SEC 与网络检索的次数上限写在"
                 "各自已批准的检索计划里，目前没有页面入口。"),
    }


def ir_pages(plane: Any) -> dict[str, Any]:
    from ...ir_page_watch_core import STATE_DECLARATION_FILENAME, load_ir_page_declaration

    path = _state_dir(plane) / STATE_DECLARATION_FILENAME
    out: dict[str, Any] = {"available": True, "file": path.name, "present": path.is_file()}
    if path.is_file():
        stat = path.stat()
        out["modified_at"] = _iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc))
        try:
            declaration = load_ir_page_declaration(path)
            out["valid"] = True
            out["page_count"] = len(declaration) if hasattr(declaration, "__len__") else None
        except Exception as exc:  # noqa: BLE001 - reported
            out["valid"] = False
            out["problem"] = str(exc)[:240]
    heartbeat = _read_json(Path(plane.config.heartbeat_path)) or {}
    lane = None
    if isinstance(heartbeat, Mapping):
        result = (heartbeat.get("bounded_planner") or {}).get("last_result") or {}
        for value in (result.values() if isinstance(result, Mapping) else []):
            if isinstance(value, Mapping) and isinstance(value.get("ir_pages"), Mapping):
                lane = value["ir_pages"]
                break
    out["last_run"] = None if lane is None else {
        "status": lane.get("status"), "reason": lane.get("display_reason") or lane.get("reason")}
    out["note"] = ("IR 页面监控读取本环境的 ir-pages.json 声明；文件缺失时这条流程不运行。"
                   if not out["present"] else "声明文件存在；最近一次运行状态见下。")
    return out


def feishu_delivery(plane: Any, actor_ref: str) -> dict[str, Any]:
    from ...owner_delivery import contract_path, ledger_summary, load_contract

    state = _state_dir(plane)
    contract = load_contract(state).summary()
    try:
        ledger = ledger_summary(state, limit=MAX_DELIVERY_RECEIPTS)
    except Exception as exc:  # noqa: BLE001 - reported
        ledger = {"present": False, "receipts": [], "problem": str(exc)[:200]}
    receipts = []
    for record in ledger.get("receipts") or []:
        if not isinstance(record, Mapping):
            continue
        receipts.append({
            "artifact_kind": record.get("artifact_kind"),
            "artifact_label": DELIVERY_KIND_LABELS.get(str(record.get("artifact_kind")),
                                                       str(record.get("artifact_kind") or "")),
            "outcome": record.get("outcome"),
            "outcome_label": DELIVERY_OUTCOME_LABELS.get(str(record.get("outcome")),
                                                         str(record.get("outcome") or "")),
            "at": record.get("recorded_at") or record.get("created_at") or record.get("at"),
        })
    enable = f"zsh docs/ops/owner-delivery-enable.sh --state-dir '{state}'"
    return {
        "available": True,
        "contract": contract,
        "contract_file": contract_path(state).name,
        "enabled": bool(contract.get("approved")),
        "status_label": ("已开通" if contract.get("approved") else
                         "已提出，等待批准" if contract.get("status") == "proposed" else "未开通"),
        "counts": ledger.get("counts") or {},
        "receipts": receipts,
        "commands": [
            {"label": "查看当前开通状态（只读）", "preview": f"{enable} --status"},
            {"label": "开通：填写接收消息的飞书群聊 chat id，并以负责人身份批准（不加 apply 只预览）",
             "preview": f"{enable} --chat-id <飞书群聊 chat id> --approved-by {actor_ref}",
             "apply": f"{enable} --chat-id <飞书群聊 chat id> --approved-by {actor_ref} apply"},
            {"label": "停用推送（回到「已提出」）", "preview": f"{enable} --revoke",
             "apply": f"{enable} --revoke apply", "danger": True},
        ],
        "note": ("飞书推送负责周报、需要处理事项、模型回退通知和紧急停机通知。开通与停用只有脚本入口；"
                 "每次发送都会留下回执（下方列出最近几条）。"),
    }


DELIVERY_KIND_LABELS = {
    "owner_lever": "运行控制通知", "model_fallback_notice": "模型回退通知",
    "weekly_brief": "每周简报", "needs_human_digest": "待处理事项摘要",
}
DELIVERY_OUTCOME_LABELS = {
    "attempting": "发送中", "delivered": "已送达", "failed": "发送失败",
    "capped": "超过发送上限，未发送", "indeterminate": "结果不确定",
}


def connections(plane: Any, actor_ref: str) -> dict[str, Any]:
    mission = _mission(plane)
    return {
        "available": True,
        "as_of": _iso(_now()),
        "environment": environment_identity(plane),
        "governance": _guard("接口治理记录", lambda: governance_records(plane, actor_ref)),
        "holds": _guard("连接状态", lambda: connector_holds(plane)),
        "credentials": _guard("凭证槽", lambda: credential_slots(plane)),
        "discovery": _guard("检索计划", lambda: discovery_plans(plane)),
        "source_budgets": _guard("来源预算", lambda: source_budgets(plane, mission)),
        "ir_pages": _guard("IR 页面", lambda: ir_pages(plane)),
        "feishu": _guard("飞书推送", lambda: feishu_delivery(plane, actor_ref)),
    }


# -- 预算与费用 --------------------------------------------------------------

_cost_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cost_lock = threading.Lock()


def _ledger_spend(budget_db: str | Path, since_day: str) -> dict[str, Any]:
    """Spend by day, lane and pool since ``since_day`` in one day ledger."""

    connection = _connect_ro(budget_db)
    if connection is None:
        raise FileNotFoundError(f"没有预算台账：{Path(budget_db).name}")
    with closing(connection):
        columns = {row["name"] for row in connection.execute(
            "PRAGMA table_info(thesis_impact_day_admissions)")}
        if not columns:
            return {"rows": []}
        lane = "a.pool_lane" if "pool_lane" in columns else "NULL"
        pool = "a.pool" if "pool" in columns else "NULL"
        rows = connection.execute(
            f"SELECT a.day AS day, {lane} AS lane_key, {pool} AS pool_key, a.phase AS phase, "
            "COUNT(*) AS calls, "
            "SUM(COALESCE(c.corrected_micros, s.actual_micros, a.reserved_micros)) AS micros "
            "FROM thesis_impact_day_admissions a "
            "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
            "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
            "WHERE a.day >= ? GROUP BY a.day, lane_key, pool_key, a.phase", (since_day,)).fetchall()
    return {"rows": [{"day": row["day"], "lane": row["lane_key"], "pool": row["pool_key"],
                      "phase": row["phase"], "calls": row["calls"], "micros": row["micros"]}
                     for row in rows]}


def _fold(rows: list[Mapping[str, Any]], today: str) -> dict[str, Any]:
    from ...cockpit_plane import REGISTRY_LANE_LABELS

    def bucket() -> dict[str, Any]:
        return {"calls": 0, "cost_usd": 0.0}

    today_total, week_total = bucket(), bucket()
    by_lane: dict[str, dict[str, Any]] = {}
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        calls = int(row.get("calls") or 0)
        usd = int(row.get("micros") or 0) / 1_000_000
        lane = str(row.get("lane") or "")
        pool = str(row.get("pool") or "")
        is_today = row.get("day") == today
        for target in ([today_total, week_total] if is_today else [week_total]):
            target["calls"] += calls
            target["cost_usd"] += usd
        lane_entry = by_lane.setdefault(lane, {
            "lane": lane or None,
            "label": (REGISTRY_LANE_LABELS.get(lane) or EXTRA_LANE_LABELS.get(lane) or lane)
                     if lane else "未标注流程（较早的记录）",
            "today": bucket(), "week": bucket()})
        pool_entry = by_pool.setdefault(pool, {
            "pool": pool or None, "label": POOL_LABELS.get(pool, pool or "未分池"),
            "today": bucket(), "week": bucket()})
        for entry in (lane_entry, pool_entry):
            for key in (("today", "week") if is_today else ("week",)):
                entry[key]["calls"] += calls
                entry[key]["cost_usd"] += usd

    def rounded(entry: dict[str, Any]) -> dict[str, Any]:
        for key in ("today", "week"):
            if key in entry:
                entry[key]["cost_usd"] = round(entry[key]["cost_usd"], 4)
        return entry

    today_total["cost_usd"] = round(today_total["cost_usd"], 4)
    week_total["cost_usd"] = round(week_total["cost_usd"], 4)
    return {
        "today": today_total, "week": week_total,
        "by_lane": sorted((rounded(e) for e in by_lane.values()),
                          key=lambda e: -e["week"]["cost_usd"]),
        "by_pool": sorted((rounded(e) for e in by_pool.values()),
                          key=lambda e: -e["week"]["cost_usd"]),
    }


#: Budget lanes that are not registry lanes (the ledger names the caller).
EXTRA_LANE_LABELS = {
    "research_language_revision": "研究报告语言修订", "plan": "研究规划",
    "ask": "研究问答", "cockpit": "投研工作台", "thesis_impact": "论点影响评估",
}

POOL_LABELS = {
    "coverage": "覆盖研究", "event": "事件跟踪", "deep": "深度研究",
    "maintenance": "维护", "ask": "问答", "reserve": "备用",
}


def costs(plane: Any) -> dict[str, Any]:
    host = (plane._host_settings_view() if hasattr(plane, "_host_settings_view") else None)
    key = str(plane.config.state_dir) + str(None if host is None else host.get("desired_hash"))
    now = time.monotonic()
    with _cost_lock:
        hit = _cost_cache.get(key)
        if hit is not None and now - hit[0] < COST_TTL_SECONDS:
            return {**hit[1], "host_settings": host}
    value = _costs(plane)
    with _cost_lock:
        _cost_cache[key] = (now, value)
    return {**value, "host_settings": host}


def thinking_cost_comparison(budget_db: str | Path, since_day: str,
                             router_db: str | Path | None = None) -> list[dict[str, Any]]:
    """Compare recorded calls around the latest owner edit, without relabelling history."""
    from ...call_budget import resolve_thinking_level
    from ...model_selection import PURPOSE_LABELS
    changes: dict[str, Any] = {}
    route_purposes: dict[str, str] = {}
    router = _connect_ro(router_db)
    if router is not None:
        with closing(router):
            for row in router.execute("SELECT policy_json FROM model_routing_policy_versions"):
                policy = json.loads(row[0])
                for purpose, edit in (policy.get("thinking_level_overrides") or {}).items():
                    if edit["created_at"] >= changes.get(purpose, {}).get("created_at", ""):
                        changes[purpose] = edit
            tables = {row[0] for row in router.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "model_route_chain_links" in tables:
                route_purposes = {row["decision_id"]: row["purpose"] for row in router.execute(
                    "SELECT decision_id,purpose FROM model_route_chain_links WHERE created_at>=?", (since_day,))}
    ledger = _connect_ro(budget_db)
    if ledger is None:
        return []
    comparison: dict[str, dict[str, Any]] = {}
    with closing(ledger):
        rows = ledger.execute(
            "SELECT a.record_json, a.created_at, s.created_at AS settled_at, "
            "COALESCE(c.corrected_micros,s.actual_micros,a.reserved_micros) AS micros "
            "FROM thesis_impact_day_admissions a "
            "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
            "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
            "WHERE a.day>=? ORDER BY a.created_at", (since_day,))
        for row in rows:
            record = json.loads(row["record_json"])
            purpose = record.get("purpose") or route_purposes.get(record.get("route_decision_ref")) or "unknown"
            edit = changes.get(purpose)
            current = resolve_thinking_level(purpose, requested=edit["level"] if edit else None)
            entry = comparison.setdefault(purpose, {"purpose": purpose,
                "label": PURPOSE_LABELS.get(purpose, "未标注环节（较早的记录）"),
                "current_level": current, "changed_at": edit["created_at"] if edit else None,
                "reason": edit["reason"] if edit else None,
                "before": {"calls": 0, "cost_usd": 0.0, "timed_calls": 0, "seconds": 0.0, "levels": []},
                "after": {"calls": 0, "cost_usd": 0.0, "timed_calls": 0, "seconds": 0.0, "levels": []}})
            level = record.get("thinking_level")
            before = (row["created_at"] < edit["created_at"] if edit else level is None)
            bucket = entry["before" if before else "after"]
            bucket["calls"] += 1
            bucket["cost_usd"] += int(row["micros"] or 0) / 1_000_000
            declared = level or "未记录"
            if declared not in bucket["levels"]:
                bucket["levels"].append(declared)
            if row["settled_at"]:
                duration = (datetime.fromisoformat(row["settled_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds()
                if duration >= 0:
                    bucket["timed_calls"] += 1
                    bucket["seconds"] += duration
    for entry in comparison.values():
        for key in ("before", "after"):
            bucket = entry[key]
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)
            bucket["average_cost_usd"] = (round(bucket["cost_usd"] / bucket["calls"], 6)
                                          if bucket["calls"] else None)
            bucket["average_seconds"] = (round(bucket["seconds"] / bucket["timed_calls"], 2)
                                         if bucket["timed_calls"] else None)
    return sorted(comparison.values(), key=lambda item: item["purpose"])


def _costs(plane: Any) -> dict[str, Any]:
    today_dt = _now()
    today = today_dt.date().isoformat()
    since = (today_dt.date() - timedelta(days=6)).isoformat()
    config = _model_config(plane)
    budget_db = config.get("budget_db") if isinstance(config.get("budget_db"), str) else None
    mission = _mission(plane)
    out: dict[str, Any] = {
        "available": True, "as_of": _iso(today_dt), "day": today, "week_from": since,
        "environment": environment_identity(plane),
        "mission": None if mission is None else {
            "mission_ref": mission.get("mission_ref"), "hash": mission.get("content_hash"),
            "budget": dict(mission.get("budget") or {})},
        "note": ("按研究流程和预算池统计，一周＝含今天在内的最近 7 个自然日（UTC）。"
                 "已结算的按实际费用，未结算的按预留额计入。"),
    }
    out['publication_pipeline'] = _guard('发布流水线费用',lambda: publication_pipeline_observability(plane))
    if budget_db is None:
        out["local"] = {"available": False, "reason": "本环境没有绑定预算台账（尚未接入付费模型调用）。"}
        out["environments"] = []
        return out
    local = _guard("本环境费用", lambda: {"available": True,
                                          **_fold(_ledger_spend(budget_db, since)["rows"], today)})
    out["local"] = local
    out["thinking_comparison"] = _guard("思考强度费用对比", lambda: thinking_cost_comparison(
        budget_db, since, config.get("model_router_db")))
    out["thinking_comparison_note"] = ("按最近一次有理由的思考强度变更分前后；没有变更记录时，"
        "未记录强度的旧调用列在此前，显式声明的调用列在此后。旧记录不推断为 low。"
        "耗时为预算准入至费用结算，包含排队；未结算费用仍按预留计入。")
    environments: list[dict[str, Any]] = []
    shared = None
    try:
        from ...shared_daily_budget import resolve_shared_gate, shared_day_view
        shared = shared_day_view(budget_db, today)
        gate = resolve_shared_gate(budget_db)
    except Exception as exc:  # noqa: BLE001 - optional host-wide gate
        gate = None
        out["shared_problem"] = str(exc)[:200]
    out["shared_daily"] = shared
    identity = environment_identity(plane)
    environments.append({"name": f"{identity['name']}（本环境）", "local": True,
                         **({"today": local["today"], "week": local["week"]}
                            if local.get("available") else {"reason": local.get("reason")})})
    for peer in (gate or {}).get("peer_ledgers") or []:
        name = peer.get("name") or peer.get("environment_id") or "环境"
        try:
            folded = _fold(_ledger_spend(peer["budget_db"], since)["rows"], today)
            environments.append({"name": name, "environment_id": peer.get("environment_id"),
                                 "local": False, "today": folded["today"], "week": folded["week"]})
        except Exception as exc:  # noqa: BLE001 - an asleep peer is named
            environments.append({"name": name, "environment_id": peer.get("environment_id"),
                                 "local": False, "reason": f"读不到：{str(exc)[:120]}"})
    out["environments"] = environments
    return out


# -- 运行控制 ----------------------------------------------------------------

OWNER_DOOR_STATE_LABELS = {
    "requested": "已受理，排队中", "running": "执行中", "succeeded": "已完成", "failed": "失败",
}
OWNER_DOOR_OPERATION_LABELS = {
    "authorize_document_research_paid": "放行付费文档研究",
    "authorize_document_research_unproved": "放行未验证的文档研究",
    "authorize_document_research_reentry": "放行研究重新进入",
    "withdraw_document_research": "撤回文档研究",
}


COCKPIT_DOOR_OPERATIONS = {
    "authorize_mission_document_reentry": "放行一次重新进入",
    "authorize_mission_document_paid_recovery": "再买一次模型调用（合同失败）",
    "authorize_mission_document_unproved_recovery": "再买一次模型调用（送达未确认）",
    "withdraw_mission_document_admission": "撤回一项专项研究",
}


def cockpit_owner_door_requests(plane: Any) -> list[dict[str, Any]]:
    """The owner's door presses from the cockpit, with what each one bought.

    2026-10-09: 「放行一次重新进入」 is synchronous (a grant file the lane
    spends), so it never became an owner-door job and the owner's presses on
    legacy EPAM were nowhere on this list.  Each press is in the cockpit
    journal (``cockpit_owner_doors``); a re-entry press is completed with the
    run the grant bought (``needs_human.last_owner_reentry``).
    """

    journal = getattr(plane, "journal", None)
    if journal is None:
        return []
    try:
        rows = journal.rows(
            "SELECT event_id, at, title, refs_json FROM cockpit_events WHERE kind='lever' "
            "AND refs_json LIKE '%\"operation\": \"%mission_document%' "
            "ORDER BY event_id DESC LIMIT ?", (MAX_OWNER_DOOR_JOBS,))
    except Exception:  # noqa: BLE001 - a list, never a failure of the page
        return []
    from ...mission_document_research_launcher import TICKETS_DIRNAME
    from ...needs_human import last_owner_reentry

    runs = _state_dir(plane) / TICKETS_DIRNAME
    newest_reentry: set[str] = set()
    out = []
    for row in rows:
        try:
            refs = json.loads(row["refs_json"])
        except (TypeError, ValueError):
            continue
        operation = refs.get("operation")
        if operation not in COCKPIT_DOOR_OPERATIONS:
            continue
        admission = str(refs.get("admission_ref") or "")
        state, label, error, updated = "requested", "已受理", None, row["at"]
        if refs.get("job_ref"):
            continue  # a governed job: listed from its own events above
        if operation == "authorize_mission_document_reentry" and refs.get("door") == "retry_round":
            # 2026-10-09: one more round of automatic recovery, recorded
            # append-only; the lane runs it on its next tick.
            state, label = "succeeded", "已安排再试一轮（系统下一轮执行）"
        elif operation == "authorize_mission_document_reentry":
            state, label = "succeeded", "已放行（已由车道取用）"
            if admission not in newest_reentry:
                newest_reentry.add(admission)
                try:
                    attempt = last_owner_reentry(runs, admission)
                except OSError:
                    attempt = None
                if attempt and attempt.get("pending"):
                    label = "已放行（等车道执行）"
                elif attempt:
                    if attempt.get("run_status") == "failed":
                        state, label = "failed", "重新进入失败"
                        error = str(attempt.get("run_error") or "")[:300]
                        updated = attempt.get("finished_at") or updated
                    elif attempt.get("run_status") is None:
                        state, label = "running", "车道执行中"
                    else:
                        label = "重新进入已执行"
                        updated = attempt.get("finished_at") or updated
        elif refs.get("status") in {"withdrawn", "replayed"}:
            state, label = "succeeded", "已撤回"
        out.append({
            "job_ref": f"cockpit-event:{row['event_id']}", "state": state, "state_label": label,
            "operation": operation,
            "operation_label": ("在当前模型设置下再试一次" if refs.get("door") == "retry_round"
                                else COCKPIT_DOOR_OPERATIONS[operation]),
            "admission_ref": admission, "requested_at": row["at"], "updated_at": updated,
            "error": error, "source": "cockpit",
        })
    return out


def owner_door_jobs(plane: Any) -> dict[str, Any]:
    with plane._core() as core:
        try:
            rows = core.execute(
                "SELECT job_ref, MAX(sequence) AS last_sequence, MIN(created_at) AS requested_at, "
                "MAX(created_at) AS updated_at FROM owner_door_job_events "
                "GROUP BY job_ref ORDER BY updated_at DESC LIMIT ?",
                (MAX_OWNER_DOOR_JOBS,)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            rows = []
        items = []
        for row in rows:
            last = core.execute(
                "SELECT state, operation, admission_ref, writer_boot_ref, record_json "
                "FROM owner_door_job_events WHERE job_ref=? AND sequence=?",
                (row["job_ref"], row["last_sequence"])).fetchone()
            if last is None:
                continue
            try:
                record = json.loads(last["record_json"])
            except (TypeError, ValueError):
                record = {}
            error = record.get("error") if isinstance(record, Mapping) else None
            items.append({
                "job_ref": row["job_ref"], "state": last["state"],
                "state_label": OWNER_DOOR_STATE_LABELS.get(last["state"], last["state"]),
                "operation": last["operation"],
                "operation_label": OWNER_DOOR_OPERATION_LABELS.get(last["operation"], last["operation"]),
                "admission_ref": last["admission_ref"],
                "requested_at": row["requested_at"], "updated_at": row["updated_at"],
                "error": None if error is None else str(error)[:300],
            })
    items += cockpit_owner_door_requests(plane)
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    items = items[:MAX_OWNER_DOOR_JOBS]
    if not items:
        return {"available": True, "items": [],
                "note": "还没有任何负责人放行任务（本环境的写入服务尚未记录过）。"}
    return {"available": True, "items": items,
            "note": ("负责人放行/撤回类操作在写入服务的后台线程执行，每一步都留回执。"
                     "「执行中」超过 30 分钟或写入服务重启后仍未完成的，按中断处理，可重新发起。")}


def restart_guidance(plane: Any) -> dict[str, Any]:
    identity = environment_identity(plane)
    slug = identity.get("slug")
    namespace = f"space.lumos.dalton.workspace.{slug}" if slug else "space.lumos.dalton"
    roles = [("writer", "写入服务（writer）"), ("controller", "调度服务（controller）"),
             ("control", "投研工作台网页服务（control）"),
             ("thesis-impact", "论点影响评估服务（thesis-impact）")]
    if slug:
        roles.append(("publication-worker", "定稿发布服务（publication worker）"))
    restart = [{
        "label": f"重启{label}",
        "apply": f"launchctl kickstart -k gui/$(id -u)/{namespace}.{role}",
        "danger": role == "writer",
    } for role, label in roles]
    status = [{"label": f"查看{label}是否在运行（只读）",
               "preview": f"launchctl print gui/$(id -u)/{namespace}.{role} | head -20"}
              for role, label in roles[:2]]
    state = _state_dir(plane)
    try:
        from ...service_config_location import service_config_path
        service_json = service_config_path(state)
    except Exception:  # noqa: BLE001 - shown as the lexical default
        service_json = state / "service.json"
    return {
        "available": True,
        "environment": identity,
        "sections": [
            {"title": "部署（构建新版本并切换）",
             "explain": ("把主分支当前提交构建成新版本并切换所有服务。部署会让写入服务冷启动，"
                         "冷启动后 10 分钟内不要执行放行/撤回类命令（见「健康与故障」里的 10-07 事故）。"
                         "部署前确认没有进行中的负责人放行任务。"),
             "commands": [{"label": "部署 main 当前提交", "apply":
                           "zsh ~/Projects/dalton-research-agent-os/docs/ops/deploy.sh",
                           "danger": True}]},
            {"title": "重启单个服务",
             "explain": ("改了需要常驻服务重新读取的配置（例如部分模型环节「保存后需重启」、"
                         "允许使用新模型后重启模型网关）时使用。重启写入服务会中断正在执行的写入，"
                         "只有在写入服务卡住（调度长时间不推进）时才这样做。"),
             "commands": status + restart},
            {"title": "健康检查与环境一致性（只读）",
             "explain": "只读检查：服务心跳、写入/控制套接字、数据库文件，以及与其它环境的配置一致性。",
             "commands": [
                 {"label": "服务健康检查",
                  "preview": f"dalton-health --config '{service_json}'"},
                 {"label": "环境一致性检查",
                  "preview": (f"dalton-workspace-parity --workspace {slug}" if slug
                              else f"dalton-workspace-parity --state-dir '{state}'")},
                 {"label": "列出本机全部研究环境",
                  "preview": ("source ~/Projects/dalton-research-agent-os/docs/ops/host-environments.zsh"
                              " && dalton_environments")},
             ]},
        ],
        "note": ("这些命令需要在运行 Dalton 的 Mac 上、由技术管理员在终端执行；页面本身不会执行任何命令。"
                 "带「执行」字样的会改变运行状态，请先看说明。"),
    }


def held_tasks(needs_human: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every held research task from the needs-human list, with its cockpit doors.

    2026-10-09: every per-task ``controlled_recovery`` card -- not only those
    with a door (live legacy EPAM: two ``fresh_work_recovery_exhausted`` holds
    were counted on the group card while this list said none were waiting).
    A task the system retries by itself says so; one whose reason has no door
    carries the hand-off to the technical administrator.
    """

    from ...terminology import label as term

    out = []
    for item in (needs_human or {}).get("items") or []:
        detail = item.get("detail") if isinstance(item, Mapping) else None
        if (not isinstance(detail, Mapping) or item.get("kind") != "controlled_recovery"
                or not detail.get("admission_ref")):
            continue
        doors = []
        for door in detail.get("owner_doors") or ():
            if not isinstance(door, Mapping):
                continue
            cost = str(door.get("cost") or ("paid" if door.get("needs_cost_cap") else "free"))
            doors.append({**door, "label": plain(door.get("label")),
                          "confirm": plain(door.get("confirm")),
                          "danger": bool(door.get("danger") or door.get("door") == "withdraw"),
                          "cost": cost,
                          "cost_label": term("door_cost", cost, default=term("door_cost", "free"))})
        costs = {door["cost"] for door in doors}
        cost = ("paid" if "paid" in costs else "within_budget" if "within_budget" in costs
                else "free")
        automatic = detail.get("automatic_retry") if isinstance(
            detail.get("automatic_retry"), Mapping) else None
        handoff = detail.get("handoff") if isinstance(detail.get("handoff"), Mapping) else None
        out.append({
            "ref": item.get("ref"), "title": plain(item.get("title")),
            "company": detail.get("company"), "company_ref": detail.get("company_ref"),
            "question": detail.get("question"),
            "lane_label": plain(detail.get("lane_label")),
            "reason": detail.get("reason"),
            "reason_label": plain(detail.get("reason_label")),
            "cost": cost,
            "cost_label": ("其中一个选项会再做一次模型调用，费用上限由你填写" if cost == "paid"
                           else "其中一个选项可能再做一次模型调用，费用在立项时已批准的上限之内"
                           if cost == "within_budget" else "所有选项都不会新增模型调用费用"),
            "automatic_retry": automatic,
            "handoff": ({"label": plain(handoff.get("label")), "note": plain(handoff.get("note"))}
                        if handoff else None),
            "why": plain(item.get("why_blocked")), "action": plain(item.get("action")),
            "consequence": plain(item.get("consequence")),
            "admission_ref": detail.get("admission_ref"),
            "doors": doors})
    return out


def runtime(plane: Any, needs_human: Mapping[str, Any] | None = None) -> dict[str, Any]:
    commands = []
    for item in (needs_human or {}).get("items") or []:
        detail = item.get("detail") if isinstance(item, Mapping) else None
        if isinstance(detail, Mapping) and detail.get("commands"):
            commands.append({"title": item.get("title"), "why": item.get("why_blocked"),
                             "kind": item.get("kind"), "commands": detail["commands"]})
    return {
        "available": True, "as_of": _iso(_now()),
        "environment": environment_identity(plane),
        # 2026-10-09: every held research task with the doors it offers, so
        # 运行控制 can open them without a terminal (cockpit_owner_doors).
        "held_tasks": held_tasks(needs_human),
        "owner_door_jobs": _guard("负责人放行任务", lambda: owner_door_jobs(plane)),
        "guidance": _guard("重启与部署说明", lambda: restart_guidance(plane)),
        "pending_commands": commands,
    }


# -- 健康与故障 --------------------------------------------------------------

_parity_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_parity_lock = threading.Lock()

PARITY_STATUS_LABELS = {
    "ok": "一致", "gap": "缺口", "warn": "警告", "drift": "不一致", "pending": "待完成", "info": "说明",
}
PARITY_SECTION_LABELS = {
    "governance": "接口治理", "authorization": "授权", "mission": "研究目标", "lanes": "研究流程",
    "config": "配置", "owner_levers": "运行控制", "verifier_independence": "核验独立性",
    "host": "本机服务",
}


def parity(plane: Any) -> dict[str, Any]:
    key = str(plane.config.state_dir)
    now = time.monotonic()
    with _parity_lock:
        hit = _parity_cache.get(key)
        if hit is not None and now - hit[0] < PARITY_TTL_SECONDS:
            return hit[1]
        from ...workspace_health_parity import Environment, check_environment

        identity = environment_identity(plane)
        env = Environment(identity["name"], plane.config.state_dir)
        try:
            report = check_environment(env, include_host=False)
        finally:
            closer = getattr(env, "close", None)
            if callable(closer):
                closer()
        rows = []
        for row in report.get("rows") or []:
            fix, fix_technical = split_fix(row.get("fix"))
            rows.append({
                **row, "fix": fix, "fix_technical": fix_technical,
                "status_label": PARITY_STATUS_LABELS.get(row.get("status"), row.get("status")),
                "section_label": PARITY_SECTION_LABELS.get(row.get("section"), row.get("section")),
            })
        value = {"available": True, "as_of": _iso(_now()), "counts": report.get("counts") or {},
                 "rows": rows, "mission_version": report.get("mission_version"),
                 "note": ("本环境的配置一致性检查（不含本机服务进程检查，那一部分请用命令行）。"
                          "每 2 分钟最多重新计算一次。")}
        _parity_cache[key] = (now, value)
        return value


def ledger_by_lane(plane: Any) -> dict[str, Any]:
    backlog = plane.ops_backlog(terminal_limit=1, historical_limit=500)
    if not backlog.get("available"):
        return {"available": False, "reason": backlog.get("reason") or "还没有失败台账。"}
    lanes: dict[str, dict[str, Any]] = {}

    def lane_entry(lane: Any, label: Any) -> dict[str, Any]:
        key = str(lane or "")
        return lanes.setdefault(key, {"lane": lane, "lane_label": label or key or "未标注流程",
                                      "parked": 0, "permission": 0, "terminal": 0,
                                      "historical": 0, "last_seen": None})

    def seen(entry: dict[str, Any], at: Any) -> None:
        if isinstance(at, str) and (entry["last_seen"] is None or at > entry["last_seen"]):
            entry["last_seen"] = at

    for dependency in backlog.get("dependencies") or []:
        for item in dependency.get("items") or []:
            entry = lane_entry(item.get("lane"), item.get("lane_label"))
            entry["parked"] += 1
            seen(entry, item.get("last_seen"))
    for item in backlog.get("permission_items") or []:
        entry = lane_entry(item.get("lane"), item.get("lane_label"))
        entry["permission"] += 1
        seen(entry, item.get("last_seen"))
    for group in backlog.get("terminal_groups") or []:
        entry = lane_entry(group.get("lane"), group.get("lane_label"))
        entry["terminal"] += int(group.get("count") or 0)
        seen(entry, group.get("last_seen"))
    for item in backlog.get("historical_items") or []:
        entry = lane_entry(item.get("lane"), item.get("lane_label"))
        entry["historical"] += 1
    rows = sorted(lanes.values(), key=lambda e: (-(e["parked"] + e["permission"]), -e["terminal"],
                                                 str(e["lane_label"])))
    page = backlog.get("historical_page") or {}
    return {
        "available": True,
        "cells": [
            {"key": "parked", "label": "等待外部服务", "hint": "依赖恢复后自动重试"},
            {"key": "permission", "label": "等待授权", "hint": "批准后继续"},
            {"key": "terminal", "label": "已停止", "hint": "未通过检查，需修复后重新安排"},
            {"key": "historical", "label": "已处理（历史）", "hint": "配置已更新，仅作记录"},
        ],
        "totals": {"parked": backlog.get("parked_items", 0),
                   "permission": backlog.get("permission_count", 0),
                   "terminal": backlog.get("terminal_count", 0),
                   "historical": backlog.get("historical_count", 0)},
        # Parked items released without a lane resuming them (dependency came
        # back elsewhere, or not parked again for a week): not in any cell.
        "released": backlog.get("released_count", 0),
        "historical_truncated": page.get("next_offset") is not None,
        "lanes": rows,
        "note": backlog.get("note"),
    }


def writer_tick_health(plane: Any) -> dict[str, Any]:
    heartbeat_path = Path(plane.config.heartbeat_path)
    heartbeat = _read_json(heartbeat_path) if heartbeat_path.is_file() else None
    now = _now()
    out: dict[str, Any] = {"available": True, "heartbeat_present": isinstance(heartbeat, Mapping)}
    if isinstance(heartbeat, Mapping):
        last = heartbeat.get("last_tick_at")
        age = None
        if isinstance(last, str):
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                age = None
        planner = heartbeat.get("bounded_planner") if isinstance(heartbeat.get("bounded_planner"), Mapping) else {}
        out.update({
            "service_state": heartbeat.get("state"),
            "last_tick_at": last,
            "tick_age_seconds": None if age is None else round(age, 1),
            "tick_stale": age is not None and age > 600,
            "planner_last_error": (str(planner.get("last_error"))[:300]
                                   if planner.get("last_error") else None),
        })
    writer_socket = Path(plane.writer_socket) if getattr(plane, "writer_socket", None) else None
    out["writer_socket_present"] = bool(writer_socket and writer_socket.exists())
    out["files"] = {name: (_state_dir(plane) / name).is_file()
                    for name in ("core.sqlite", "service.json")}
    out["ticks"] = plane._ticks()
    out["state_label"] = (
        "没有心跳文件：本环境的调度服务还没有运行过" if not out["heartbeat_present"]
        else "调度超过 10 分钟没有推进，可能卡住" if out.get("tick_stale")
        else "调度正常推进")
    return out


PUBLICATION_STATUS_LABELS = {
    "skipped": "未进入文字定稿（原稿内容不足或无需处理）", "completed": "已定稿", "pending": "等待处理", "deferred": "延后", "settling": "收尾中",
    "superseded": "已被新版本取代", "exhausted": "重试次数用完", "parked": "暂缓", "retried": "已重试",
}


_publication_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def publication_summary(plane: Any) -> dict[str, Any]:
    key = str(plane.config.state_dir)
    now = time.monotonic()
    hit = _publication_cache.get(key)
    if hit is not None and now - hit[0] < PUBLICATION_TTL_SECONDS:
        return hit[1]
    value = _publication_summary(plane)
    _publication_cache[key] = (now, value)
    return value


def publication_last_run(plane: Any, work: Path) -> dict[str, Any] | None:
    """The worker's latest round (``worker-last-run.json``), with every product
    that gave up named -- company, kind, version, why -- and its retry.

    2026-10-09: the checkpoint said ``exhausted: 1`` and nothing else readable;
    the products directory counts every version ever exhausted (most of them
    long superseded).  This is the current round only.
    """

    record = _read_json(work / "worker-last-run.json")
    if not isinstance(record, Mapping):
        return None
    from ...research_publication_worker import (
        EXHAUSTED_STATUS, _hash, awaiting_owner, failure_reasons, retry_chain)
    try:
        from ...needs_human import PUBLICATION_KIND_LABELS
    except Exception:  # noqa: BLE001 - labels are a nicety
        PUBLICATION_KIND_LABELS = {}
    rows = record.get("exhausted_products")
    if not isinstance(rows, list):
        rows = [row for row in record.get("products") or ()
                if isinstance(row, Mapping) and row.get("status") == EXHAUSTED_STATUS]
    members = _company_labels(plane) if rows else {}
    root = work / "products"
    items = []
    for row in rows[:MAX_RETRY_ITEMS]:
        if not isinstance(row, Mapping) or not isinstance(row.get("identity"), Mapping):
            continue
        identity = dict(row["identity"])
        identity_hash = str(row.get("identity_hash") or _hash(identity))
        state = _read_json(root / f"{identity_hash}.json")
        state = state if isinstance(state, Mapping) else {}
        result = state.get("result") if isinstance(state.get("result"), Mapping) else {}
        try:
            chain = retry_chain(root, identity_hash)
        except (OSError, ValueError):
            chain = []
        reasons = failure_reasons(result) if result else []
        kind = str(identity.get("kind") or "")
        subject = identity.get("subject_ref")
        # The worker reopens some classes itself; then the page says so
        # instead of asking for a retry.
        auto_retry = bool(state) and not awaiting_owner(state, chain)
        items.append({
            "identity_hash": identity_hash, "product_hash": row.get("product_hash"),
            "kind": kind, "kind_label": PUBLICATION_KIND_LABELS.get(kind, "研究产物"),
            "subject_ref": subject, "company": _company_label(subject, members),
            "version_ref": identity.get("version_ref"),
            "failed_at": result.get("failed_at"),
            "reason": (str(reasons[0])[:400] if reasons else None),
            "retries": len(chain),
            "auto_retry": auto_retry,
            "current": state.get("product_hash") == row.get("product_hash"),
        })
    counts = {key: record.get(key) for key in (
        "completed", "pending", "deferred", "settling", "parked", "exhausted", "superseded", "skipped",
        "retried") if isinstance(record.get(key), int)}
    return {"checked_at": record.get("checked_at"), "counts": counts,
            "exhausted_items": items,
            "note": ("最近一轮里「重试次数用完」的报告逐份列出：每份都可以「重新进行语言质检」；"
                     "系统不会再自动重试它们。")}


def _publication_summary(plane: Any) -> dict[str, Any]:
    from ...final_text_state import publication_work_dir

    work = publication_work_dir(plane.config.state_dir)
    products = work / "products"
    if not products.is_dir():
        return {"available": True, "present": False, "counts": {},
                "note": "定稿发布服务还没有处理过任何研究报告。"}
    try:
        last_run = publication_last_run(plane, work)
    except Exception as exc:  # noqa: BLE001 - the counts below still stand
        last_run = {"available": False, "reason": f"最近一轮记录读不到：{type(exc).__name__}"}
    counts: dict[str, int] = {}
    scanned = 0
    latest = None
    unreadable = 0
    exhausted: list[dict[str, Any]] = []
    for path in products.iterdir():
        if path.suffix != ".json":
            continue
        scanned += 1
        if scanned > MAX_PUBLICATION_FILES:
            break
        value = _read_json(path)
        if not isinstance(value, Mapping):
            unreadable += 1
            continue
        status = str(value.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        mtime = path.stat().st_mtime
        identity = value.get("identity") if isinstance(value.get("identity"), Mapping) else {}
        if status == "exhausted" and isinstance(value.get("product_hash"), str):
            exhausted.append({"identity_hash": path.stem, "product_hash": value["product_hash"],
                              "kind": identity.get("kind"), "subject_ref": identity.get("subject_ref"),
                              "version_ref": identity.get("version_ref"), "mtime": mtime})
        latest = mtime if latest is None or mtime > latest else latest
    blocks = []
    try:
        from ...needs_human import publication_route_blocks
        blocks = publication_route_blocks(plane.config.state_dir) or []
    except Exception:  # noqa: BLE001 - optional
        blocks = []
    return {
        "available": True, "present": True,
        "counts": counts,
        "labels": {key: PUBLICATION_STATUS_LABELS.get(key, key) for key in counts},
        "scanned": min(scanned, MAX_PUBLICATION_FILES),
        "truncated": scanned > MAX_PUBLICATION_FILES,
        "unreadable": unreadable,
        "last_change_at": None if latest is None else _iso(datetime.fromtimestamp(latest, timezone.utc)),
        "route_blocks": len(blocks),
        "last_run": last_run,
        "pipeline": publication_pipeline_observability(plane),
        # The newest reports whose language review gave up: each can be sent
        # back through 「重新进行语言质检」 (the cockpit's language_retry).
        "exhausted_items": [
            {**{k: v for k, v in item.items() if k != "mtime"},
             "at": _iso(datetime.fromtimestamp(item["mtime"], timezone.utc))}
            for item in sorted(exhausted, key=lambda x: -x["mtime"])[:MAX_RETRY_ITEMS]],
        "note": ("下面的累计数含已被新版本取代的旧版本；「重试次数用完」「暂缓」的报告保持草稿状态，"
                 "需要管理员处理 AI 服务顺序或重新语言质检后才能定稿。"),
    }


def publication_pipeline_observability(plane: Any) -> dict[str, Any]:
    from ...final_text_state import publication_work_dir
    from ...research_publication_limits import observability,audit_limits
    work=publication_work_dir(plane.config.state_dir)
    value=observability(work/'publication-limits.sqlite3')
    value['audit']=audit_limits(work/'publication-limits.sqlite3')
    runtime_root=Path(getattr(plane.config,'core_db',Path(plane.config.state_dir)/'core.sqlite')).parent
    for root in (runtime_root/'research-language-reviews'/'ask',runtime_root/'publication-language-work'):
        extra=observability(root/'publication-limits.sqlite3')
        if extra['present']:
            value['present']=True;value['rows'].extend(extra['rows'])
            audit=audit_limits(root/'publication-limits.sqlite3')
            value['audit']['present']=True
            value['audit']['passed']=value['audit']['passed'] and audit['passed']
            value['audit']['violations'].extend(audit['violations'])
            value['audit']['legacy_overages'].extend(audit['legacy_overages'])
    labels={}
    products=work/'products'
    if products.is_dir():
        for path in list(products.glob('*.json'))[:MAX_PUBLICATION_FILES]:
            state=_read_json(path)
            if isinstance(state,Mapping): labels[state.get('product_hash')]=state.get('identity') or {}
    artifacts={}
    for row in value['rows']:
        key=row['source_hash']
        item=artifacts.setdefault(key,{'source_hash':key,'identity':labels.get(key,{}),'runs':0,
            'calls':0,'cost_micros':0,'prompt_bytes':0,'reasons':[],'stages':[]})
        if row['kind']=='run': item['runs']+=row['count']
        if row['kind']=='call': item['calls']+=row['count'];item['prompt_bytes']+=row['prompt_bytes']
        if row['kind']=='result' or row['reason']=='legacy_receipt': item['cost_micros']+=row['cost_micros']
        if row['kind'] in {'run','park'} and row['reason'] not in item['reasons']:item['reasons'].append(row['reason'])
        if row['kind']=='call':item['stages'].append({'stage':row['stage'],'calls':row['count'],'prompt_bytes':row['prompt_bytes']})
    config=_model_config(plane)
    budget_db=config.get('budget_db')
    ledger=_connect_ro(budget_db) if isinstance(budget_db,str) else None
    ledger_costs={}
    if ledger is not None:
        with closing(ledger):
            if ledger.execute("SELECT 1 FROM sqlite_master WHERE name='thesis_impact_day_admissions'").fetchone():
                since=datetime.now(timezone.utc)-timedelta(hours=24)
                records=ledger.execute("SELECT a.record_json,a.created_at,COALESCE(c.corrected_micros,s.actual_micros,a.reserved_micros) AS micros "
                    "FROM thesis_impact_day_admissions a LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                    "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id WHERE a.day>=?",(since.date().isoformat(),))
                for row in records:
                    if datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))<since:continue
                    record=json.loads(row['record_json']);binding=record.get('publication') or {}
                    source=binding.get('source_hash')
                    if source in artifacts:ledger_costs[source]=ledger_costs.get(source,0)+int(row['micros'])
    for source,item in artifacts.items():
        item['cost_basis']='day_ledger' if source in ledger_costs else 'stage_receipt'
        if source in ledger_costs:item['cost_micros']=ledger_costs[source]
    value['artifacts']=sorted(artifacts.values(),key=lambda item:(-item['runs'],-item['calls'],item['source_hash']))
    value['truncated']=len(value['artifacts'])>200
    value['artifacts']=value['artifacts'][:200]
    value['note']='滚动 24 小时，每份原稿最多运行 2 次、调用 6 次；不确定发送也占额度。费用优先取预算台账（含模型回退与未结算预留），旧记录用阶段回执。提示词字节数含独立核验必须保留的事实上下文。'
    return value


def incidents(plane: Any, health: Mapping[str, Any], jobs: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = [dict(incident, source="known") for incident in KNOWN_INCIDENTS]
    if health.get("tick_stale"):
        out.insert(0, {
            "id": "incident:live-tick-stale", "at": health.get("last_tick_at"),
            "title": "调度超过 10 分钟没有推进", "kind": "tick_stale", "kind_label": "调度停顿",
            "what": f"最近一次调度在 {health.get('last_tick_at')}。", "source": "live",
            "impact": "研究流程不会接新任务。",
            "watch": "先看「负责人放行任务」是否有执行中的重型操作；再按「运行控制 → 重启单个服务」处理。",
        })
    for lane in ((health.get("ticks") or {}).get("stalled_lanes") or []):
        if int(lane.get("longest_stall_run") or 0) >= 6:
            out.append({
                "id": f"incident:lane-stall:{lane.get('lane')}", "source": "live",
                "title": f"「{lane.get('lane_label')}」连续 {lane.get('longest_stall_run')} 次调度无进展",
                "kind": "lane_stall", "kind_label": "流程停滞",
                "what": f"统计窗口内 {lane.get('ticks')} 次调度中 {lane.get('stalls')} 次无进展。",
            })
    for job in (jobs.get("items") or []):
        if job.get("state") == "failed":
            out.append({
                "id": f"incident:{job['job_ref']}", "source": "live", "at": job.get("updated_at"),
                "title": f"负责人放行任务失败：{job.get('operation_label')}",
                "kind": "owner_door_failed", "kind_label": "放行任务失败",
                "what": job.get("error") or "没有记录失败原因",
            })
    return out


def health(plane: Any) -> dict[str, Any]:
    writer = _guard("写入与调度", lambda: writer_tick_health(plane))
    jobs = _guard("负责人放行任务", lambda: owner_door_jobs(plane))
    return {
        "available": True, "as_of": _iso(_now()),
        "environment": environment_identity(plane),
        "parity": _guard("一致性检查", lambda: parity(plane)),
        "ledger": _guard("失败台账", lambda: ledger_by_lane(plane)),
        "writer": writer,
        "publication": _guard("定稿发布服务", lambda: publication_summary(plane)),
        "incidents": _guard("事故记录", lambda: incidents(
            plane, writer if writer.get("available") else {}, jobs if jobs.get("available") else {})),
    }


def summary(plane: Any) -> dict[str, Any]:
    """The header: which environment, whether it runs, whether it is stopped."""

    writer = _guard("写入与调度", lambda: writer_tick_health(plane))
    levers = None
    try:
        from ...owner_levers import lever_state
        with plane._core() as core:
            state = lever_state(core, now=plane.clock())
        levers = {"emergency": state.get("emergency") is not None,
                  "paused_count": len(state.get("companies") or {}) + len(state.get("lanes") or {})}
    except Exception:  # noqa: BLE001 - a fresh Core has no lever table yet
        levers = {"emergency": False, "paused_count": 0, "unavailable": True}
    return {"available": True, "as_of": _iso(_now()),
            "environment": environment_identity(plane),
            "writer": writer, "levers": levers}


__all__ = [
    "KNOWN_INCIDENTS", "connections", "costs", "health", "runtime", "summary",
]
