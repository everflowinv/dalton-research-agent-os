"""Batch C/D: when to redraft -- the owner's batching rule as thresholds.

Owner decision 4 (research capability diagnosis 2026-10-09): 「收集一批信息再
起草，之后累积了一些新信息再重新起草；单条材料到达只登记为待消化」.  A single
arrival is recorded (``evidence_index.material_arrivals``) and counted as
「待消化」; a redraft happens only when a threshold is crossed:

``required_material``   a new version of a required material: the next
                        quarter's transcript, earnings release (8-K Ex. 99.1)
                        or a new 10-K, newer than the one the last draft read;
``consensus_material``  ``consensus_table.consensus_delta`` says ``material``
                        under ``CONSENSUS_THRESHOLDS``;
``model_eps``           our model's NTM EPS moved more than ``model_eps_pct``;
``broker_batch``        at least ``min_broker_or_sales`` new broker reports or
                        sales notes about the company;
``misattribution``      an issuer-misattribution rewrite flag
                        (``product_rewrite_flags.needs_rewrite``) whose
                        replacement material has arrived;
``stale``               ``stale_days`` since the last draft and any new material;
``retired_citations``   a rewrite flag for any other retirement reason (a
                        section's valid-citation share fell below the
                        threshold) whose replacement material has arrived;
``stale_binding``       the product's own derived objects were computed from
                        an upstream version that has since been superseded
                        (``product_consistency.redraft_signals``, batch an);
``consistency_conflict`` an open high/medium cross-product conflict names
                        this product (classification, consensus source ...).

``should_redraft(...) -> (decision, reasons)`` with decision ``no`` /
``redraft`` / ``reopen_required`` (a trigger fired and the last draft's gate
has passed, so the lane must reopen it first -- :func:`auto_reopen` does that
through the gate-reopen authority, with the reasons recorded).

``evaluate_targets(...)`` is the call a product lane's target selection makes
instead of "nothing new since the last version".  Nothing here calls a model
or writes, except :func:`auto_reopen`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import material_sufficiency as ms

SCHEMA_VERSION = "redraft-policy:0.1"
POLICY_REF = "redraft-policy:owner-batching:v1"
DECISIONS: tuple[str, ...] = ("no", "redraft", "reopen_required")
#: Required-material kinds whose new version triggers a redraft on its own.
REQUIRED_KINDS: Mapping[str, str] = {"transcript": "新一季电话会纪要", "sec_exhibit": "新一季业绩新闻稿",
                                     "sec_10k": "新年报 10-K"}
BATCH_KINDS: tuple[str, ...] = ("broker_report", "sales_note")
DEFAULT_THRESHOLDS: Mapping[str, Any] = {
    "min_broker_or_sales": 3,
    "stale_days": 14,
    "model_eps_pct": "5",
    #: consensus_delta fractions: a mean moving this much is "material" for a redraft
    #: (looser than consensus_table's own change-detection defaults).
    "consensus": {"eps": "0.03", "revenue": "0.02", "target": "0.05"},
}
#: product -> the product_rewrite_flags product kinds that are its versions.
REWRITE_KINDS: Mapping[str, tuple[str, ...]] = {
    "initial_screen": ("initial_screen",), "investment_memo": ("investment_memo",),
    "earnings_preview": ("earnings_preview",), "dossier": ("company_dossier",), "debate_map": ("debate_map",),
    "event_judgement": ("event_judgement",), "industry_framework": ("industry_framework",), "ask": (),
}
REASON_LABELS: Mapping[str, str] = {
    "first_draft": "还没有起草过", "required_material": "必需材料出了新版本",
    "consensus_material": "一致预期变化超过阈值", "model_eps": "我们的模型 EPS 变化超过阈值",
    "broker_batch": "新研报/销售快报累计达到阈值", "misattribution": "发行人归属错误，替代材料已到",
    "misattribution_waiting": "发行人归属错误，等待替代材料", "stale": "距上次起草已久且有新材料",
    "pending_digest": "待消化", "gate_passed": "上一版已过闸，需要先重开",
    "retired_citations": "所引结论已退休且有效引用不足，替代材料已到",
    "retired_citations_waiting": "所引结论已退休且有效引用不足，等待替代材料",
    "stale_binding": "派生结果基于旧版本上游", "consistency_conflict": "与其他产品不一致",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _reason(code: str, detail: str, *, refs: Sequence[str] = (), fired: bool = True, **extra: Any) -> dict[str, Any]:
    return {"code": code, "label": REASON_LABELS.get(code, code), "detail": detail, "fired": fired,
            "refs": [ref for ref in refs if ref][:20], **extra}


def last_draft_from_version(record: Mapping[str, Any] | None, *, stage_status: str | None = None,
                            version_ref: str | None = None, created_at: str | None = None) -> dict[str, Any] | None:
    """The ``last_draft`` mapping from a stored product version record.

    Reads ``created_at``, the version ref and -- when the lane stored
    ``MaterialPack.record()`` under ``material_pack`` -- its watermark.
    """

    if record is None:
        return None
    pack = record.get("material_pack") if isinstance(record.get("material_pack"), Mapping) else None
    return {"version_ref": version_ref or record.get("id") or record.get("version_id"),
            "created_at": created_at or record.get("created_at"),
            "gate_status": stage_status,
            "watermark": (pack or {}).get("watermark") or record.get("material_watermark")}


def _thresholds(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    for key, value in (overrides or {}).items():
        if key == "consensus" and isinstance(value, Mapping):
            out["consensus"].update({k: str(v) for k, v in value.items()})
        else:
            out[key] = value
    return out


def _cached_read(cache: dict | None, key: tuple, read: Any) -> Any:
    if cache is None:
        return read()
    if key not in cache:
        cache[key] = read()
    return cache[key]


def should_redraft(state_dir: str | Path, company_ref: str, product: str, last_draft: Mapping[str, Any] | None, *,
                   now: datetime | str | None = None, thresholds: Mapping[str, Any] | None = None,
                   connection: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """``(decision, reasons)``: ``no`` / ``redraft`` / ``reopen_required``.

    ``last_draft``: ``{"created_at", "version_ref", "gate_status", "watermark"}``
    (see :func:`last_draft_from_version`); ``None`` means never drafted.
    ``reasons`` lists every trigger evaluated (``fired`` true/false) plus, when
    nothing fired, a ``pending_digest`` entry whose ``count`` is the number of
    arrivals waiting to be digested.
    """

    result = evaluate(state_dir, company_ref, product, last_draft, now=now, thresholds=thresholds,
                      connection=connection)
    return result["decision"], result["reasons"]


def evaluate(state_dir: str | Path, company_ref: str, product: str, last_draft: Mapping[str, Any] | None, *,
             now: datetime | str | None = None, thresholds: Mapping[str, Any] | None = None,
             connection: Any = None, _read_cache: dict | None = None,
             _pending_arrivals: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """:func:`should_redraft` with the working: counts, watermark, pending."""

    from . import evidence_index

    moment = _parse(now) if isinstance(now, str) else (now or _now())
    limits = _thresholds(thresholds)
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "policy_ref": POLICY_REF, "company_ref": company_ref,
                           "product": product, "evaluated_at": moment.isoformat(timespec="seconds"),
                           "thresholds": limits}
    if not last_draft or not last_draft.get("created_at"):
        out.update({"decision": "redraft", "reasons": [_reason("first_draft", "这家公司还没有这一产品的任何版本")],
                    "pending_count": 0})
        return out
    drafted = _parse(last_draft.get("created_at")) or moment
    mark = dict(last_draft.get("watermark") or {})
    after_id = int(mark.get("arrival_id") or 0)
    try:
        since = None if after_id else drafted.astimezone(timezone.utc).isoformat()
        arrivals = _cached_read(_read_cache, ('arrivals', str(state_dir), company_ref, after_id, since),
            lambda: evidence_index.material_arrivals(state_dir, company_ref=company_ref, after_id=after_id,
                include_backfill=True, limit=10000, since=since, newest_first=True))
        out['arrival_scan_complete'] = None  # bounded scan; a Python time refinement cannot prove completeness
        if len(arrivals) >= 9999:
            # A large report batch must not hide the latest required filing.
            required = _cached_read(_read_cache, ('required_arrivals', str(state_dir), company_ref, after_id, since),
                lambda: evidence_index.material_arrivals(state_dir, company_ref=company_ref, after_id=after_id,
                    include_backfill=True, limit=10000, since=since, newest_first=True, kinds=tuple(REQUIRED_KINDS)))
            arrivals = list({r['arrival_id']: r for r in [*arrivals, *required]}.values())
        arrivals = sorted(arrivals, key=lambda row: row['arrival_id'])
    except Exception:  # noqa: BLE001 - no index: nothing arrived
        arrivals = []
        out['arrival_scan_complete'] = False
    if _pending_arrivals is not None:
        arrivals = [dict(r) for r in _pending_arrivals if r.get('company_ref') == company_ref]
    if not after_id and _pending_arrivals is None:
        arrivals = [row for row in arrivals if (_parse(row.get('indexed_at')) or drafted) > drafted]
    # "Arrived since the draft" must be news, not backfill of old documents.
    floor = (drafted - timedelta(days=7)).date().isoformat()
    fresh = [row for row in arrivals if not row.get("backfill") or str(row.get("document_date") or "") >= floor]
    reasons: list[dict[str, Any]] = []
    own_conn = connection is None
    core = ms.open_core_ro(state_dir) if own_conn else connection
    try:
        identity = ms.company_identity(core, company_ref, state_dir)
        # 1. a new version of a required material
        latest = dict(mark.get("latest") or {})
        hits = []
        for kind, label in REQUIRED_KINDS.items():
            seen = (latest.get(kind) or {})
            seen_date = str(seen.get("date") or "") or floor
            candidates = [row for row in fresh if row.get("kind") == kind
                          and str(row.get("document_date") or "") > seen_date
                          and row.get("document_ref") != seen.get("document_ref")]
            if not candidates:
                continue
            own = _cached_read(_read_cache, ('own_documents', str(state_dir), company_ref, kind, moment.date()),
                lambda: {doc["document_ref"] for doc in ms.own_documents(
                    state_dir, company_ref, identity, [kind], as_of=moment.date().isoformat(), days=400)})
            mine = [row for row in candidates if row["document_ref"] in own]
            if mine:
                hits.append((label, mine))
        if hits:
            reasons.append(_reason("required_material", "；".join(
                f"{label}：{rows[-1].get('document_date')} {rows[-1].get('period') or ''}".strip() for label, rows in hits),
                refs=[row["document_ref"] for _, rows in hits for row in rows]))
        # 2. consensus moved materially
        reasons.append(_cached_read(_read_cache, ('consensus_reason', str(state_dir), company_ref, drafted.isoformat(),
            json.dumps(mark.get('consensus'), sort_keys=True), moment.isoformat(), json.dumps(limits['consensus'], sort_keys=True)),
            lambda: _consensus_reason(core, state_dir, company_ref, mark, drafted, moment, limits)))
        # 3. our model's EPS moved
        reasons.append(_cached_read(_read_cache, ('model_reason', str(state_dir), company_ref,
            json.dumps(mark.get('model'), sort_keys=True), str(limits['model_eps_pct'])),
            lambda: _model_reason(state_dir, company_ref, mark, limits)))
        # 4. a batch of new broker reports / sales notes
        batch = [row for row in fresh if row.get("kind") in BATCH_KINDS]
        docs = list(dict.fromkeys(row["document_ref"] for row in batch))
        enough = len(docs) >= int(limits["min_broker_or_sales"])
        reasons.append(_reason("broker_batch", f"上次起草后新增券商研报/销售快报 {len(docs)} 份（阈值 "
                                               f"{limits['min_broker_or_sales']}）", refs=docs, fired=enough,
                               count=len(docs)))
        # 5. issuer misattribution with replacement material present
        reasons.extend(_misattribution(core, company_ref, product, last_draft, arrivals))
        # 5b. batch an: stale upstream bindings and cross-product conflicts
        reasons.extend(_cached_read(_read_cache, ('consistency', str(state_dir), company_ref, product, last_draft.get('version_ref'), moment.isoformat()),
            lambda: _consistency(core, company_ref, product, last_draft, moment)))
        # 6. stale with any new material
        age = (moment - drafted).days
        stale = age >= int(limits["stale_days"]) and bool(fresh)
        reasons.append(_reason("stale", f"距上次起草 {age} 天（阈值 {limits['stale_days']}），其间新材料 {len(fresh)} 份",
                               fired=stale, days=age))
    finally:
        if own_conn and core is not None:
            core.close()
    fired = [reason for reason in reasons if reason["fired"]]
    pending = len({row["document_ref"] for row in fresh})
    if not fired:
        decision = "no"
        reasons.append(_reason("pending_digest", f"待消化材料 {pending} 份，均未达到重起草阈值", fired=False,
                               count=pending, by_kind=_by_kind(fresh)))
    elif last_draft.get("gate_status") == "gate_passed":
        decision = "reopen_required"
        reasons.append(_reason("gate_passed", "上一版已经过闸；按阈值需要重开再重写", fired=True))
    else:
        decision = "redraft"
    out.update({"decision": decision, "reasons": reasons, "pending_count": pending if decision == "no" else 0,
                "arrivals_since": pending, "by_kind": _by_kind(fresh),
                "watermark_arrival_id": after_id,
                "latest_arrival_id": max([int(row["arrival_id"]) for row in arrivals] or [after_id])})
    return out


def _by_kind(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    seen: set[str] = set()
    for row in rows:
        if row["document_ref"] in seen:
            continue
        seen.add(row["document_ref"])
        out[row["kind"]] = out.get(row["kind"], 0) + 1
    return out


def _consensus_reason(core: Any, state_dir: str | Path, company_ref: str, mark: Mapping[str, Any],
                      drafted: datetime, moment: datetime, limits: Mapping[str, Any]) -> dict[str, Any]:
    if core is None:
        return _reason("consensus_material", "没有数据库，无法比较一致预期", fired=False)
    try:
        from .consensus_table import consensus_delta, consensus_table_from

        current = consensus_table_from(core, company_ref, moment.date().isoformat(), state_dir=state_dir,
                                       today=moment.date().isoformat())
        baseline = mark.get("consensus")
        if not baseline:
            baseline = consensus_table_from(core, company_ref, drafted.date().isoformat(), state_dir=state_dir,
                                            today=drafted.date().isoformat())
        if current.get("status") != "ok" or not baseline or baseline.get("status") not in (None, "ok"):
            return _reason("consensus_material", "起草时或现在没有一致预期，无法比较", fired=False)
        delta = consensus_delta(baseline, current, thresholds=limits["consensus"])
    except Exception as exc:  # noqa: BLE001
        return _reason("consensus_material", f"一致预期比较失败：{type(exc).__name__}", fired=False)
    moved = [item for item in delta["changes"] if item["field"] == "mean"][:6]
    detail = (f"一致预期 {delta.get('from_as_of')} → {delta.get('to_as_of')}：最大均值变化 "
              f"{delta.get('max_abs_mean_pct') or '0'}（阈值 EPS {limits['consensus']['eps']}、收入 "
              f"{limits['consensus']['revenue']}、目标价 {limits['consensus']['target']}）")
    return _reason("consensus_material", detail, fired=bool(delta["material"]),
                   changes=[{k: item.get(k) for k in ("period", "metric", "from", "to", "pct", "kind")} for item in moved])


def _model_reason(state_dir: str | Path, company_ref: str, mark: Mapping[str, Any],
                  limits: Mapping[str, Any]) -> dict[str, Any]:
    before = (mark.get("model") or {}).get("ntm_eps")
    if before is None:
        return _reason("model_eps", "起草时没有记录我们的模型 EPS，无法比较", fired=False)
    baseline = mark.get("model") or {}
    if baseline.get("quarters"):
        from .research_product_consumption import model_baselines
        current = model_baselines(Path(state_dir), [company_ref]).get(company_ref)
        if not current:
            return _reason("model_eps", "现在没有可核对的同模型 EPS 投影", fired=False)
        if baseline.get('company_ref') != company_ref:
            return _reason("model_eps", "模型 EPS 基准属于其他公司", fired=False)
        if any(current.get(k) != baseline.get(k) for k in ('quarters', 'method', 'unit')):
            return _reason("model_eps", "模型预测期间或计算口径已更新，需要更新对应输入", fired=True,
                comparison_kind='model_basis_changed', refs=[baseline['version_ref'], current['version_ref']])
        now = {'status': 'available', 'value': current['ntm_eps']}
    else:
        current = None
        now = ms.our_eps(state_dir, company_ref)
    old, new = _dec(before), _dec(now.get("value")) if now.get("status") == "available" else None
    if old is None or new is None or old == 0:
        return _reason("model_eps", f"现在读不出模型 EPS（{now.get('reason')}）", fired=False)
    pct = (new - old) / abs(old) * 100
    limit = Decimal(str(limits["model_eps_pct"]))
    return _reason("model_eps", f"模型 NTM EPS {before} → {now.get('value')}（{pct.quantize(Decimal('0.1'))}%，阈值 ±{limit}%）",
                   fired=abs(pct) > limit, pct=format(pct.quantize(Decimal("0.01")), "f"),
                   refs=[] if current is None else [baseline['version_ref'], current['version_ref']])


def _misattribution(core: Any, company_ref: str, product: str, last_draft: Mapping[str, Any],
                    arrivals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if core is None:
        return []
    try:
        from .product_rewrite_flags import needs_rewrite

        flags = needs_rewrite(core, subject_ref=company_ref, arrivals=arrivals)
    except Exception:  # noqa: BLE001
        return []
    kinds = set(REWRITE_KINDS.get(product, ()))
    version = last_draft.get("version_ref")
    out = []
    ready: list[Mapping[str, Any]] = []
    waiting: list[Mapping[str, Any]] = []
    for flag in flags:
        if version and flag.get("product_version_ref") != version:
            continue
        if not version and flag.get("product_kind") not in kinds:
            continue
        if "issuer_misattribution" not in (flag.get("retirement_reasons") or ()):
            # Batch an: every other retirement reason, once the section's
            # valid-citation share fell under the threshold (the flag exists).
            (ready if flag.get("rewrite_ready") else waiting).append(flag)
            continue
        if flag.get("rewrite_ready"):
            out.append(_reason("misattribution", f"{flag.get('product_version_ref')} 引用了发行人归属错误的结论，替代材料已到",
                               refs=[flag.get("id"), *flag.get("arrived_material", [])]))
        else:
            out.append(_reason("misattribution_waiting", f"{flag.get('product_version_ref')} 引用了发行人归属错误的结论，"
                                                         "替代材料尚未到达，暂不重写", refs=[flag.get("id")], fired=False))
    if ready:
        sections = sum(len(flag.get("sections") or []) for flag in ready)
        out.append(_reason("retired_citations", f"{len(ready)} 份版本共 {sections} 节有效引用占比不足，替代材料已到",
                           refs=[ref for flag in ready for ref in (flag.get("id"), *flag.get("arrived_material", []))],
                           count=len(ready)))
    if waiting:
        sections = sum(len(flag.get("sections") or []) for flag in waiting)
        out.append(_reason("retired_citations_waiting", f"{len(waiting)} 份版本共 {sections} 节有效引用占比不足，"
                           "替代材料尚未到达，暂不重写", refs=[flag.get("id") for flag in waiting],
                           fired=False, count=len(waiting)))
    return out


def _consistency(core: Any, company_ref: str, product: str, last_draft: Mapping[str, Any],
                 moment: datetime) -> list[dict[str, Any]]:
    """Batch an: ``stale_binding`` and ``consistency_conflict`` triggers."""

    if core is None:
        return []
    try:
        from .product_consistency import redraft_signals

        signals = redraft_signals(core, company_ref, product, version_ref=last_draft.get("version_ref"),
                                  today=moment.date().isoformat())
    except Exception as exc:  # noqa: BLE001 - a failed read is not a trigger
        return [_reason("consistency_conflict", f"一致性检查失败：{type(exc).__name__}", fired=False)]
    return [_reason(item["code"], item["detail"], refs=item.get("refs") or (), fired=bool(item["fired"]),
                    count=item.get("count", 0)) for item in signals]


def evaluate_targets(state_dir: str | Path, product: str, last_drafts: Mapping[str, Mapping[str, Any] | None], *,
                     now: datetime | str | None = None, thresholds: Mapping[str, Any] | None = None,
                     connection: Any = None) -> dict[str, dict[str, Any]]:
    """Per company: ``{"decision", "reasons", "pending_count", "summary"}``.

    ``last_drafts`` maps company_ref -> ``last_draft_from_version(...)`` (or
    ``None`` when the company has no version yet).  A lane's target
    selection skips a company whose decision is ``no`` (with ``summary`` as
    the skip reason) and drafts one whose decision is ``redraft``; a
    ``reopen_required`` company is reopened by :func:`auto_reopen` (the
    reopen lane does this) before it can be redrafted.
    """

    out = {}
    for company_ref, last in last_drafts.items():
        try:
            result = evaluate(state_dir, company_ref, product, last, now=now, thresholds=thresholds,
                              connection=connection)
        except Exception as exc:  # noqa: BLE001 - one company's failure is a hold, not a crash
            result = {"decision": "no", "pending_count": 0,
                      "reasons": [_reason("pending_digest", f"阈值评估失败：{type(exc).__name__}: {exc}"[:300],
                                          fired=False, count=0)]}
        fired = [reason for reason in result["reasons"] if reason["fired"]]
        summary = ("；".join(f"{r['label']}：{r['detail']}" for r in fired) if fired else
                   f"待消化 {result.get('pending_count', 0)} 份新材料，未达重起草阈值")
        out[company_ref] = {"decision": result["decision"], "reasons": result["reasons"],
                            "pending_count": result.get("pending_count", 0), "summary": summary[:1000]}
    return out


# ---------------------------------------------------------------------------
# gate_passed is no longer terminal: the automatic reopen
# ---------------------------------------------------------------------------


def policy_assessment(connection: Any, state_dir: str | Path, company_ref: str, *,
                      now: datetime | str | None = None, thresholds: Mapping[str, Any] | None = None
                      ) -> dict[str, Any]:
    """A gate-reopen assessment built from the redraft thresholds.

    Same shape as ``deliverable_reopen.reopen_assessment`` (so the authority,
    the cockpit and the re-issued version's ``revision`` block read it
    unchanged): ``status`` ``reopen_proposed`` when ``should_redraft`` says
    ``reopen_required``, else ``no_flip`` / ``not_passed``.
    """

    from .deliverable_reopen import CHANGE_REASON_EVIDENCE, STAGE_REF, passed_version
    from .store import content_hash

    passed = passed_version(connection, company_ref=company_ref)
    if passed is None:
        return {"status": "not_passed", "company_ref": company_ref,
                "reason": "这家公司的初筛现在没有过闸的版本（未过闸，或已重开等待重出）"}
    record = passed["record"]
    last = last_draft_from_version(record, stage_status="gate_passed", version_ref=passed["version_id"],
                                   created_at=passed["created_at"])
    result = evaluate(state_dir, company_ref, "initial_screen", last, now=now, thresholds=thresholds,
                      connection=connection)
    fired = [reason for reason in result["reasons"] if reason["fired"] and reason["code"] != "gate_passed"]
    diff = [{"item_ref": f"policy:{reason['code']}", "label": reason["label"], "threshold": None,
             "was": {"value": None, "present": False, "mark": "缺"},
             "now": {"value": reason["detail"], "present": True, "mark": "有"},
             "flipped": True, "regressed": False, "refs": reason["refs"], "note": reason["detail"]}
            for reason in fired]
    refs = list(dict.fromkeys(ref for reason in fired for ref in reason["refs"]))[:12]
    if not refs and fired:
        refs = [f"{POLICY_REF}:{reason['code']}" for reason in fired]
    assessment = {
        "schema_version": SCHEMA_VERSION,
        "status": "reopen_proposed" if result["decision"] == "reopen_required" else "no_flip",
        "company_ref": company_ref, "deliverable_ref": passed["deliverable_ref"], "stage_ref": STAGE_REF,
        "stage_record_ref": passed["stage_record_ref"], "passed_version_ref": passed["version_id"],
        "passed_version_hash": passed["content_hash"], "passed_version_number": passed["version_number"],
        "passed_at": passed["passed_at"], "published_at": passed["created_at"],
        "mission_version_ref": passed["mission_version_ref"], "mission_version_hash": passed["mission_version_hash"],
        "policy_ref": POLICY_REF, "thresholds": {"redraft": result["thresholds"]},
        "diff": diff, "flipped": [entry["item_ref"] for entry in diff], "regressed": [], "evidence_refs": refs,
        "gate_recomputed": None, "change_reason": CHANGE_REASON_EVIDENCE,
        "redraft": {"decision": result["decision"], "reasons": result["reasons"]},
    }
    assessment["assessment_hash"] = content_hash({
        "company_ref": company_ref, "passed_version_ref": passed["version_id"], "policy_ref": POLICY_REF,
        "fired": sorted((reason["code"], tuple(reason["refs"])) for reason in fired)})
    return assessment


def owner_note(reasons: Sequence[Mapping[str, Any]]) -> str:
    fired = [reason for reason in reasons if reason.get("fired") and reason.get("code") != "gate_passed"]
    return ("按重起草阈值自动重开（owner 2026-10-09 决定 4：累积新材料后批量重写）：" +
            "；".join(f"{reason['label']}——{reason['detail']}" for reason in fired))[:2000]


def auto_reopen(authority: Any, connection: Any, state_dir: str | Path, company_ref: str, *,
                mission: Mapping[str, Any], now: datetime | str | None = None,
                thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Propose and accept a gate reopen when the thresholds say ``reopen_required``.

    Through ``GateReopenAuthority``: a proposal by the mission's automation
    principal, then ``decide_by_policy`` (approve, actor = that principal,
    the owner-visible note naming every trigger).  When a proposal for the
    current passed version is already waiting for a person, that one is
    accepted with the policy's reasons rather than asking a second question.
    A mission whose ``autonomy.auto_reopen`` is ``False`` only proposes.
    """

    from .deliverable_reopen import DeliverableReopenError
    from .initial_screen_reopen_hygiene import already_open_for_current

    assessment = policy_assessment(connection, state_dir, company_ref, now=now, thresholds=thresholds)
    if assessment["status"] != "reopen_proposed":
        return {"status": assessment["status"], "company_ref": company_ref,
                "reasons": (assessment.get("redraft") or {}).get("reasons") or [], "reason": assessment.get("reason")}
    actor = mission["autonomy"]["automation_principal"]
    note = owner_note(assessment["redraft"]["reasons"])
    waiting = already_open_for_current(connection, company_ref=company_ref, stage_ref=assessment["stage_ref"])
    try:
        if waiting is not None:
            proposal = authority.proposal(waiting["proposal_ref"])
        else:
            proposal = authority.propose(assessment=assessment, mission=mission, actor_ref=actor)
        if (mission.get("autonomy") or {}).get("auto_reopen") is False:
            return {"status": "proposed", "company_ref": company_ref, "proposal_ref": proposal["id"], "note": note}
        decision = authority.decide_by_policy(proposal_ref=proposal["id"], proposal_hash=proposal["content_hash"],
                                              policy_ref=POLICY_REF, note=note,
                                              reasons=assessment["redraft"]["reasons"], mission=mission,
                                              actor_ref=actor)
    except DeliverableReopenError as exc:
        return {"status": "failed", "company_ref": company_ref, "reason": f"{type(exc).__name__}: {exc}"[:500]}
    return {"status": "reopened", "company_ref": company_ref, "proposal_ref": proposal["id"],
            "decision_ref": decision["id"], "write_status": decision.get("status"), "note": note}


__all__ = [
    "BATCH_KINDS", "DECISIONS", "DEFAULT_THRESHOLDS", "POLICY_REF", "REASON_LABELS", "REQUIRED_KINDS", "SCHEMA_VERSION",
    "auto_reopen", "evaluate", "evaluate_targets", "last_draft_from_version", "owner_note", "policy_assessment",
    "should_redraft",
]


URGENT_UNIT_REASONS = frozenset({'required_material', 'consensus_material', 'model_eps',
    'misattribution', 'retired_citations', 'stale_binding', 'consistency_conflict'})


def _unit_payload(record: Mapping[str, Any], product: str, unit: str) -> Any:
    if product == 'dossier':
        if unit in ('industry_classification', 'variant_view'):
            return record.get(unit)
        return next((x for x in record.get('sections', []) if x.get('aspect') == unit), None)
    if unit.startswith('causal_chain:'):
        return next((x for x in record.get('sections', [])
                     if x.get('link_index') == int(unit.split(':')[1])), None)
    return record.get({'characteristics': 'industry_characteristics'}.get(unit, unit))


def unit_drafted_at(connection: Any, prior: Mapping[str, Any] | None, product: str, unit: str) -> str | None:
    """An exact unit's own timestamp; bounded legacy ancestry, never head age."""
    if not prior:
        return None
    wanted = _unit_payload(prior, product, unit)
    record = dict(prior)
    seen = set()
    for _ in range(64):
        if _unit_payload(record, product, unit) != wanted:
            return None
        stamped = (record.get('drafted_at') or {}).get(unit)
        if _parse(stamped):
            return str(stamped)
        ref = record.get('prior_version_ref') or record.get('computed_from_version_ref')
        if not ref or ref in seen:
            return None
        seen.add(ref)
        table = 'company_dossier_versions' if product == 'dossier' else 'industry_framework_versions'
        try:
            row = connection.execute(f'SELECT record_json FROM {table} WHERE version_id=?', (ref,)).fetchone()
        except Exception:  # old synthetic readers do not prove ancestry
            return None
        if not row:
            return None
        record = json.loads(row[0])
    return None


def _unit_pending(state: Path, binding: Mapping[str, Any], baseline: str, cache: dict) -> list[dict[str, Any]]:
    from . import evidence_index as ei, research_product_consumption as consumed
    rows = []
    for company in binding['company_refs']:
        source = _cached_read(cache, ('unit_arrivals', str(state), company, baseline),
            lambda: ei.material_arrivals(state, company_ref=company, include_backfill=True,
                since=baseline, newest_first=True, limit=10000))
        # A capped report scan must not hide an important filing.
        required = _cached_read(cache, ('unit_required', str(state), company, baseline),
            lambda: ei.material_arrivals(state, company_ref=company, include_backfill=True,
                since=baseline, newest_first=True, limit=10000, kinds=tuple(REQUIRED_KINDS)))
        rows.extend({x['arrival_id']: x for x in [*source, *required]}.values())
    return consumed.pending_arrivals(state, binding, rows)


def unit_gate(state_dir: str | Path, connection: Any, *, product: str, target: str,
              company_refs: Sequence[str], unit: str, prior: Mapping[str, Any] | None,
              input_snapshot: Mapping[str, Any], first: bool = False, revise: bool = False,
              correction: bool = False, binding_changed: bool = False,
              now: datetime | str | None = None, read_cache: dict | None = None) -> dict[str, Any]:
    """The shared policy for one concrete unit, before any model admission.

    A unit's input receipts consume only exact read originals. Legacy unit
    dates measure age only; unknown historical inputs remain pending rather
    than being declared read from a publication timestamp. The same verified semantic input cannot create a new paid occasion.
    """
    from . import research_product_consumption as consumed
    state = Path(state_dir)
    binding = consumed.scope(product, target, unit, company_refs)
    moment = _parse(now) if isinstance(now, str) else now or _now()
    latest = consumed.latest(state, binding)
    drafted = unit_drafted_at(connection, prior, product, unit)
    baseline = (latest or {}).get('snapshot', {}).get('baseline_at') or ('1970-01-01T00:00:00+00:00' if drafted else None)
    out = {'policy_ref': POLICY_REF, 'scope': binding, 'allowed': False,
           'label': '新材料尚未达到更新条件', 'pending_count': 0,
           'next_review_at': None, 'baseline_at': baseline, 'reasons': []}
    # The semantic input includes the actual mission/constitution/policy and
    # method bindings. A changed binding gets its first verified read, but a
    # no-new-evidence result must not keep retrying against the older formal
    # product. A later real binding change produces a different input hash.
    if consumed.has_input(state, binding, input_snapshot['input_hash']) and not (revise or correction):
        out.update(label='这些材料已完成核验，等待新材料', reasons=[{'code': 'verified_input_unchanged', 'fired': False}])
        if baseline:
            out['pending_count'] = len({r['document_ref'] for r in _unit_pending(state, binding, baseline,
                read_cache if read_cache is not None else {})})
        return out
    if first or revise or correction or binding_changed:
        out.update(allowed=True, label='首次起草或明确修订', reasons=[{
            'code': 'first_draft' if first else 'explicit_revision' if revise else 'retired_citations' if correction else 'binding_changed', 'fired': True}])
        return out
    if not baseline:
        out.update(label='尚无法确认这一部分上次更新的时间', reasons=[{'code': 'unit_time_unproved', 'fired': False}])
        return out
    cache = read_cache if read_cache is not None else {}
    try:
        pending = _unit_pending(state, binding, baseline, cache)
    except Exception as exc:
        out.update(label='材料记录暂无法核对', reasons=[{'code': 'material_scan_unproved', 'fired': False,
                                                      'detail': str(exc)[:200]}])
        return out
    age_start = (latest or {}).get('snapshot', {}).get('captured_at') or drafted
    results = [evaluate(state, company, product, {'created_at': age_start,
        'version_ref': (prior or {}).get('id'), 'watermark': {'model':
            (latest or {}).get('snapshot', {}).get('policy_model_baselines', {}).get(company) or {}}},
        connection=connection, now=moment, _read_cache=cache, _pending_arrivals=pending) for company in company_refs]
    reasons = [r for result in results for r in result['reasons']]
    out.update(allowed=any(x['decision'] != 'no' for x in results), reasons=reasons,
               pending_count=len({x['document_ref'] for x in pending}))
    out['next_review_at'] = (_parse(age_start) + timedelta(days=int(DEFAULT_THRESHOLDS['stale_days']))).isoformat()
    if out['allowed']:
        out['label'] = '材料已达到更新条件'
    return out


def lane_probe(state_dir: str | Path, connection: Any, *, product: str, target: str,
               company_refs: Sequence[str], prior: Mapping[str, Any] | None,
               now: datetime | str | None = None, light_only: bool = False) -> dict[str, Any]:
    """Bounded read-only wake-up token; ordinary cadence and budgets stay external."""
    from .store import content_hash
    from . import research_product_consumption as consumed, evidence_index as ei
    state = Path(state_dir)
    moment = _parse(now) if isinstance(now, str) else now or _now()
    stamps = dict((prior or {}).get('drafted_at') or {})
    units = list(stamps) or ['__first__']
    cache: dict = {}
    tokens = []; counts = []; due = []; urgent = []
    authority_tokens = []
    from .financial_period import _refs
    for company in company_refs:
        refs = _refs(company)
        heads = []
        for table, suffix in (('forecast_model_versions', 'ORDER BY version_number DESC LIMIT 1'),
                              ('consensus_estimate_versions', 'ORDER BY as_of DESC,version_number DESC LIMIT 2')):
            try:
                rows = connection.execute(f'SELECT version_id FROM {table} WHERE company_ref IN '
                    f"({','.join('?' for _ in refs)}) {suffix}", refs).fetchall()
            except Exception:
                rows = []
            heads.append([row[0] for row in rows])
        authority_tokens.append([company, heads])
    if light_only:
        arrivals = []
        with ei._readonly(state) as index:
            if index is not None:
                for company in company_refs:
                    row = index.execute('SELECT MAX(arrival_id) FROM material_arrivals WHERE company_ref=?', (company,)).fetchone()
                    arrivals.append([company, int(row[0] or 0)])
        for unit in units:
            binding = consumed.scope(product, target, unit, company_refs)
            receipt = consumed.latest(state, binding)
            age = (receipt or {}).get('snapshot', {}).get('captured_at') or unit_drafted_at(connection, prior, product, unit)
            deadline = None if not age else _parse(age) + timedelta(days=int(DEFAULT_THRESHOLDS['stale_days']))
            if deadline:
                due.append(deadline.isoformat())
            tokens.append([unit, (receipt or {}).get('content_hash'), bool(deadline and moment >= deadline)])
        return {'fingerprint': content_hash({'scope': [product, target, sorted(set(company_refs))],
                'version_ref': (prior or {}).get('id'), 'arrivals': arrivals, 'authorities': authority_tokens, 'units': tokens}),
                'urgent': False, 'urgent_reasons': [], 'pending_count': None,
                'next_review_at': min(due) if due else None, 'label': '正在核对新材料'}
    for unit in units:
        binding = consumed.scope(product, target, unit, company_refs)
        receipt = consumed.latest(state, binding)
        drafted = unit_drafted_at(connection, prior, product, unit)
        baseline = (receipt or {}).get('snapshot', {}).get('baseline_at') or ('1970-01-01T00:00:00+00:00' if drafted else None)
        if not baseline:
            # First/unknown timestamps don't invent a new policy allowance.
            rows = [r for company in company_refs for r in _cached_read(cache, ('latest', str(state), company),
                lambda: ei.material_arrivals(state, company_ref=company, newest_first=True, limit=1, include_backfill=True))]
            tokens.append([unit, max([r['arrival_id'] for r in rows] or [0]), None])
            continue
        pending = _unit_pending(state, binding, baseline, cache)
        age_start = (receipt or {}).get('snapshot', {}).get('captured_at') or drafted
        deadline = _parse(age_start) + timedelta(days=int(DEFAULT_THRESHOLDS['stale_days']))
        results = [evaluate(state, company, product, {'created_at': age_start,
            'version_ref': (prior or {}).get('id'), 'watermark': {'model':
                (receipt or {}).get('snapshot', {}).get('policy_model_baselines', {}).get(company) or {}}},
            connection=connection, now=moment, _read_cache=cache, _pending_arrivals=pending) for company in company_refs]
        fired = [r['code'] for result in results for r in result['reasons'] if r['fired']]
        fired_proof = [{k: v for k, v in r.items() if k not in ('label', 'detail', 'days')}
            for result in results for r in result['reasons'] if r['fired']]
        urgent.extend(code for code in fired if code in URGENT_UNIT_REASONS)
        counts.append(len({r['document_ref'] for r in pending}))
        if pending:
            due.append(deadline.isoformat())
        tokens.append([unit, max([r['arrival_id'] for r in pending] or [0]),
                       (receipt or {}).get('content_hash'), bool(pending) and moment >= deadline, fired_proof])
    return {'fingerprint': content_hash({'scope': [product, target, sorted(set(company_refs))], 'units': tokens, 'important_authorities': authority_tokens if urgent else []}),
            'urgent': bool(urgent), 'urgent_reasons': sorted(set(urgent)),
            'pending_count': max(counts or [0]), 'next_review_at': min(due) if due else None,
            'label': '材料已达到重要更新条件' if urgent else '新材料尚未达到更新条件'}


def apply_unit_gates(state_dir: str | Path, connection: Any, *, product: str, target: str,
                     company_refs: Sequence[str], prior: Mapping[str, Any] | None,
                     plan: dict[str, Any], inputs: Mapping[str, Mapping[str, Any]],
                     revise: Sequence[str] = (), binding_changed: bool = False,
                     now: datetime | str | None = None) -> None:
    """Annotate the actual capped inputs and apply batching to ready units."""
    from . import research_product_consumption as consumed
    cache: dict = {}
    for unit, entry in plan.items():
        if entry.get('status') not in ('ready', 'draftable') or unit not in inputs:
            continue
        value = inputs[unit]
        material = value.get('parse_input', {}).get('material') if isinstance(value.get('parse_input'), Mapping) else value.get('material')
        binding = consumed.scope(product, target, unit, company_refs)
        captured = consumed.snapshot(Path(state_dir), connection, binding,
                                     material=material, input_data=value, read_cache=cache)
        held = _unit_payload(prior or {}, product, unit)
        first = not held or held.get('status') in ('unavailable', 'not_drafted_this_run')
        if product == 'dossier' and unit == 'industry_classification':
            first = not held or not held.get('sources')
        gate = unit_gate(state_dir, connection, product=product, target=target,
            company_refs=company_refs, unit=unit, prior=prior, input_snapshot=captured,
            first=first, revise=unit in revise,
            correction=bool(entry.get('retired_refs') or entry.get('retired_citation_dropped')),
            binding_changed=binding_changed, now=now, read_cache=cache)
        captured['baseline_at'] = gate['baseline_at']
        entry['_input_snapshot'] = captured
        entry['redraft_policy'] = gate
        entry['stale'] = gate['allowed']
        if gate['allowed']:
            entry['new_refs'] = max(1, int(entry.get('new_refs') or 0))
        else:
            entry['reason'] = 'new_material_pending' if gate['pending_count'] else 'verified_input_unchanged'
            entry['detail'] = gate['label']
