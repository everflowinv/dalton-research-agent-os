"""P14f, second window: the calibration, within two days of the print.

The blueprint's sentence is "inline / better / worse and why, model updated,
five-word decision".  Three of those four are arithmetic and are done before
anything is paid for:

1. **``actualize_model``** writes the filed answer beside the estimate for the
   quarter that just closed.  It is the one near-mechanical version in the
   whole model layer, and it touches history only: the estimate stays where it
   is, marked ``superseded_by`` the actual that answered it, and *no forward
   period is rebased*.  Whether the print changes next year is a judgement and
   this module does not make it -- it emits an event and lets the judgement
   lane decide.
2. **The reconciliation rows** grade each metric against what we had: inside
   tolerance, notable, or an overturn candidate.  The three tiers are
   ``forecast_reconciliation``'s and are not restated here.
3. **``guidance_vs_actual``** asks the same question of the company's own
   number, through P12f's event table, via a function the dossier and the
   weekly brief can call by the same name.

Only the fourth needs a model: the prose, and one five-word decision per
thesis in play.  It is one bounded call and a second, independent one that
checks it, and everything it produces is a *proposal*:

* a ``ThesisRevisionCandidate`` per thesis the answer wants revised (ADR-0007
  -- it carries no authority to change anything),
* a ``ForecastRevisionProposal`` when a metric came in three percent or more
  away from what we had, which is ``forecast_reconciliation``'s human
  ``forecast_overturn`` checkpoint and not ours to settle,
* a ``ThesisReflection`` in both of those cases: what we expected, what
  happened, why, and what debate we may have missed, attached to the candidate
  so the person deciding sees the proposal and the account of our own miss in
  one place.

Nothing here commits a thesis, and nothing here revises a forward estimate.
The human decides once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .cockpit_model import CockpitModelError, lane_status_for, unwrap_json_object
from .earnings_preview import (
    VERIFIER_FINDING_CODES,
    VERIFIER_VERDICTS,
    _ref_list,
    _text,
    validate_verifier_output,
)
from .earnings_season import (
    CALIBRATION_EVENT_KIND,
    CALIBRATION_KIND,
    CALIBRATION_PURPOSE,
    MAX_THESES,
    EarningsSeasonValidationError,
    citable_refs,
    consensus_block,
    forecast_rows,
    guidance_vs_actual,
    idempotency_key_for,
    render_rows,
    reported_period,
)
from .event_judgement import DECISION_ACTIONS
from .mission_deliverable import GAP_MARKER, unsourced_numbers
from .research_playbook import DECISION_VOCABULARY
from .store import content_hash

TEMPLATE_REF = "template:earnings-calibration:p14f:v1"
MAX_SUMMARY_CHARS = 1400
MAX_ITEM_CHARS = 500
MAX_LINE_ITEMS = 10
MAX_CITATIONS = 20
MAX_PROMPT_CHARS = 28_000
MAX_DOCUMENT_SUMMARY = 1800

# What a line came in as.  The blueprint's three words, and they are the words
# the deliverable prints, so they are frozen here rather than left to prose.
VERDICTS: tuple[str, ...] = ("inline", "better", "worse", "unknown")
# The band that raises a human checkpoint.  Named from the reconciliation
# authority rather than restated as a number: the threshold is its contract.
OVERTURN_BAND = "overturn_candidate"
NOTABLE_BAND = "notable"

# The per-thesis action vocabulary.  A subset of P14a's, because a calibration
# is not the place to open a research task or rewrite a dossier: it says what
# the print did to the thesis and proposes the revision, and the judgement lane
# does the rest from the event this leaves behind.
THESIS_ACTIONS: tuple[str, ...] = ("no_change", "note", "revise_thesis")

OUTPUT_KEYS: frozenset[str] = frozenset({
    "summary", "lines", "theses", "reflection",
})
REFLECTION_KEYS: frozenset[str] = frozenset({
    "what_we_expected", "what_happened", "why", "citations", "missed_debates",
    "followup_tracking", "followup_research", "market_view_vs_ours",
    "convergence_pathway",
})


class CalibrationRefused(EarningsSeasonValidationError):
    """This occurrence may not be calibrated, and why."""


# ---------------------------------------------------------------------------
# 1. the model's history catches up with the filing
# ---------------------------------------------------------------------------


def actualize_for_report(
    forecast_models: Any,
    *,
    company_ref: str,
    input_table: Mapping[str, Any] | None,
    actor_ref: str,
) -> dict[str, Any]:
    """Write the filed answer beside the estimate, and change nothing else.

    Returns a small record of what happened rather than raising: a company
    whose filings have not landed yet is a normal Tuesday, and a calibration
    that could not actualise still has reconciliation rows and guidance to
    read.  The forward periods are untouched by construction --
    ``actualize_model`` does not touch them and this does not ask it to.
    """

    from .economic_invariants import EconomicInvariantRefused
    from .model_forecast_driver import (
        ForecastModelError,
        actualize_model,
        realised_ends,
    )

    prior = None
    try:
        prior = forecast_models.latest(company_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable chain is "no model"
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    if prior is None:
        return {"status": "unavailable", "reason": "this company has no forecast model"}
    if not input_table:
        return {"status": "unavailable", "reason": "no model input table for this company",
                "prior_version_ref": prior.get("id")}
    ends = realised_ends(prior, input_table)
    if not ends:
        return {"status": "idle", "reason": "no forecast quarter has been filed yet",
                "prior_version_ref": prior.get("id")}
    try:
        body = actualize_model(prior, input_table, actor_ref=actor_ref)
        if body is None:
            return {"status": "idle", "reason": "no forecast quarter has been filed yet",
                    "prior_version_ref": prior.get("id")}
        published = forecast_models.publish(body)
    except EconomicInvariantRefused as exc:
        # P17b: the actual arrived and the chain it lands in does not hold up.
        # Recorded ``unavailable`` with the reasons rather than published with
        # a filed quarter written into an impossible model.
        return {"status": "unavailable", "reason": "; ".join(exc.report.reasons),
                "prior_version_ref": prior.get("id")}
    except ForecastModelError as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}",
                "prior_version_ref": prior.get("id")}
    return {
        "status": published.get("status", "published"),
        "version_ref": published.get("id"),
        "prior_version_ref": prior.get("id"),
        "change_reason": published.get("change_reason"),
        "realised_ends": list(ends),
        "forward_periods_untouched": True,
    }


def input_table_for(missions: Any, company_ref: str) -> dict[str, Any] | None:
    """This company's model input table, or nothing, by name.

    P13ao owns both halves; this asks for them the way the forecast lane does
    and answers ``None`` rather than raising when either is missing, because a
    company with no specification is not an error in an earnings season.
    """

    try:
        from .company_model_inputs import ModelInputError, build_model_inputs
    except ImportError:  # pragma: no cover - the module is on main
        return None
    try:
        spec = missions.latest_company_model_spec(company_ref)
    except Exception:  # noqa: BLE001
        return None
    if spec is None:
        return None
    try:
        return build_model_inputs(missions, spec)
    except (ModelInputError, Exception):  # noqa: BLE001 - a table we cannot build is no table
        return None


# ---------------------------------------------------------------------------
# 2. the three tiers
# ---------------------------------------------------------------------------


def reconciliation_rows(
    reconciliations: Any,
    *,
    company_ref: str,
    period_end: str | None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Forecast-versus-actual rows for this company, narrowed to one quarter.

    The bands are the reconciliation authority's, unchanged: within tolerance,
    notable, overturn candidate.  A second opinion about where three percent
    falls would be a second contract.
    """

    try:
        rows = reconciliations.reconciliations(company_ref=company_ref)
    except Exception:  # noqa: BLE001 - a Core with no such table has no rows
        return []
    out = []
    for row in rows:
        period = row.get("period") or {}
        end = str(period.get("end") or period.get("period_end") or "")
        if period_end and end and end != str(period_end):
            continue
        out.append({
            "id": row.get("id"),
            "metric_ref": row.get("metric_ref"),
            "period_end": end,
            "forecast_value": row.get("forecast_value"),
            "actual_value": row.get("actual_value"),
            "unit": row.get("unit"),
            "deviation_percent": row.get("deviation_percent"),
            "direction": row.get("direction"),
            "band": row.get("band"),
            "human_checkpoint": row.get("human_checkpoint"),
            "checkpoint_status": row.get("checkpoint_status"),
            "forecast_line_version_ref": row.get("forecast_line_version_ref"),
            "claim_version_ref": row.get("claim_version_ref"),
        })
    return out[:limit]


def tier_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How the quarter graded, and which rows a person has to look at."""

    counts: dict[str, int] = {}
    for row in rows:
        band = str(row.get("band") or "unknown")
        counts[band] = counts.get(band, 0) + 1
    overturn = [dict(row) for row in rows if row.get("band") == OVERTURN_BAND]
    return {
        "counts": counts,
        "overturn_candidates": overturn,
        "notable": [dict(row) for row in rows if row.get("band") == NOTABLE_BAND],
        # The one word that decides whether a ForecastRevisionProposal is owed.
        "overturn_fired": bool(overturn),
    }


def actual_rows_for_guidance(
    model_version: Mapping[str, Any] | None, period_end: str | None,
) -> list[dict[str, Any]]:
    """The actualised cells, in the shape P12f's pairing consumes.

    ``guidance_events`` is arithmetic over rows it is handed: a settled number
    wants a ref, a measure or label, a value, a unit and a period.  A model
    that has just been actualised holds exactly that for the quarter that
    closed, and handing it over is how "did the company make its own number"
    gets asked of our ledger rather than of a document.
    """

    rows: list[dict[str, Any]] = []
    for row in forecast_rows(model_version, period_end or "") or ():
        if row.get("kind") != "actual" or row.get("value") is None:
            continue
        rows.append({
            "ref": (row.get("refs") or [""])[0],
            "label": row.get("label"),
            "text": f"{row.get('label')} {row.get('value')}",
            "period": period_end,
            "period_end": period_end,
            "value": row.get("value"),
            "unit": row.get("unit"),
        })
    return rows


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def build_calibration_context(
    *,
    occurrence: Mapping[str, Any],
    mission: Mapping[str, Any],
    model_version: Mapping[str, Any] | None,
    actualisation: Mapping[str, Any] | None = None,
    reconciliations: Sequence[Mapping[str, Any]] = (),
    guidance_profile: Mapping[str, Any] | None = None,
    theses: Sequence[Mapping[str, Any]] = (),
    claims: Sequence[Mapping[str, Any]] = (),
    market_view: Sequence[Mapping[str, Any]] = (),
    source_keys: Sequence[str] = (),
    consensus_reader: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the one bounded call may see.

    Refuses outright when the occurrence's date is not confirmed: a
    calibration is written *about* a release, and an estimated date says a
    report was likely rather than that one happened.
    """

    if occurrence.get("window") != "calibration":
        raise CalibrationRefused("this occurrence has not opened a calibration window")
    if occurrence.get("date_confidence") != "confirmed":
        raise CalibrationRefused(
            "the report date is not confirmed; a calibration of a call nobody "
            "made is not a thin answer but a wrong one"
        )
    period = reported_period(model_version, occurrence["expected_date"])
    period_end = None if period is None else str(period.get("end"))
    rows = list(reconciliations)
    tiers = tier_counts(rows)
    guidance = guidance_vs_actual(guidance_profile, period_end)
    consensus = consensus_block(
        occurrence["company_ref"], period_end or "", reader=consensus_reader
    )
    numbers = [
        {
            "text": str(row.get("text") or row.get("statement") or ""),
            "claim_version_ref": str(row.get("ref") or ""),
            "period": row.get("period"),
        }
        for row in claims
        if str(row.get("ref") or "").startswith("claim-version:")
        and str(row.get("text") or row.get("statement") or "").strip()
    ]
    gaps: list[str] = []
    if not rows:
        gaps.append(
            f"{GAP_MARKER}：这一期还没有对账行，inline/better/worse 只能靠指引与已入库 Claim 判断"
        )
    if not guidance["available"]:
        gaps.append(f"{GAP_MARKER}：guidance {guidance['reason']}")
    if not consensus["available"]:
        gaps.append(f"{GAP_MARKER}：consensus {consensus['reason']}")
    return {
        "schema_version": "0.1",
        "window": "calibration",
        "occurrence": dict(occurrence),
        "company_ref": occurrence["company_ref"],
        "mission_ref": mission["id"],
        "period": period,
        "period_end": period_end,
        "forecast": forecast_rows(model_version, period_end) if period_end else [],
        "model_version_ref": (model_version or {}).get("id"),
        "actualisation": dict(actualisation or {}),
        "reconciliations": rows,
        "tiers": tiers,
        "guidance": guidance,
        "consensus": consensus,
        "theses": [
            {
                "ref": thesis.get("ref") or thesis.get("id"),
                "thesis_ref": thesis.get("thesis_ref"),
                "statement": thesis.get("statement"),
                "mechanism": thesis.get("mechanism"),
                "confidence": thesis.get("confidence"),
                "content_hash": thesis.get("content_hash"),
            }
            for thesis in list(theses)[:MAX_THESES]
        ],
        "claims": [
            {"ref": row.get("ref"), "text": row.get("text") or row.get("statement"),
             "period": row.get("period")}
            for row in list(claims)[:12]
        ],
        "numbers": numbers[:60],
        "market_view": [dict(row) for row in market_view][:8],
        "source_keys": sorted({str(key) for key in source_keys}),
        "source_refs": list(occurrence.get("source_refs") or ()),
        "gaps": gaps,
        "as_of": (now or datetime.now(timezone.utc)).date().isoformat(),
    }


def build_calibration_prompt(context: Mapping[str, Any]) -> str:
    occurrence = context["occurrence"]
    lines: list[str] = [
        "你在写一份业绩校准（earnings calibration）：这一季实际出来之后，"
        "对着我们之前的预测和公司自己的指引说清楚差在哪、为什么，以及这对 thesis 意味着什么。",
        "只输出 JSON，不要 markdown 代码块。",
        "",
        "规则：",
        "1. 只能引用下面出现过的 ref；引用没出现过的 ref，整条回答被拒绝。",
        "2. 正文的每一个数字都必须来自「可引用数字」表；我们的预测数字与对账数字不要写进正文。",
        f"3. 拿不到的东西写 {GAP_MARKER}，不要猜。",
        "4. 每一条 thesis 给一个五词决定："
        + "、".join(DECISION_VOCABULARY)
        + "；决定与动作必须相容（NO_CHANGE 不能配 revise_thesis）。",
        "5. 你提的是候选，不是修改：thesis 由人裁决，未来期的预测这一步一律不动。",
        "6. 和市场一致等于没有看法：说清我们和街上差在哪，什么可观测量会把市场拉过来；"
        "这一季打了我们的脸就说漏了什么，不要复述 thesis。",
        "",
        "输出（键固定）：",
        '{"summary": "<三到五句话>",',
        ' "lines": [{"line": "<科目>", "verdict": "inline|better|worse|unknown",'
        ' "because": "<为什么>", "refs": []}],',
        ' "theses": [{"thesis_ref": "<thesis 版本 ref>", "decision": "<五词之一>",'
        ' "action": "no_change|note|revise_thesis", "because": "<理由>",'
        ' "proposed_statement": "<改成什么，或 null>", "refs": []}],',
        ' "reflection": {"what_we_expected": "", "what_happened": "", "why": "",'
        ' "citations": [], "missed_debates": [{"question": "", "refs": []}],'
        ' "followup_tracking": [{"source_key": "", "interval_seconds": 0, "because": ""}],'
        ' "followup_research": [{"question": "", "wants": ""}],'
        ' "market_view_vs_ours": {"available": false, "our_direction": "", "summary": "", "refs": []},'
        ' "convergence_pathway": ""}}',
        "",
        f"公司：{context['company_ref']}",
        f"公布日：{occurrence['expected_date']}（confirmed）",
        f"本次业绩对应的期间：{context.get('period_end') or '未知'}",
        "",
        "模型历史已对齐（actualize）：" + str(context["actualisation"].get("status")),
        "未来期没有被改；要不要改由判断层决定。",
        "",
        "对账（三档由 forecast_reconciliation 给出，正文不要直接写这些数字）：",
    ]
    if context["reconciliations"]:
        lines.append(render_rows(
            context["reconciliations"],
            ("metric_ref", "period_end", "deviation_percent", "direction", "band"),
        ))
    else:
        lines.append(f"{GAP_MARKER}：这一期没有对账行")
    lines += ["", "公司自己的指引 vs 实际（P12f 事件表）："]
    if context["guidance"]["available"]:
        lines.append(render_rows(
            context["guidance"]["rows"],
            ("measure", "period", "guide_low", "guide_high", "actual", "verdict"),
        ))
    else:
        lines.append(f"available: false —— {context['guidance']['reason']}")
    lines += ["", "consensus："]
    if context["consensus"]["available"]:
        lines.append(render_rows(
            context["consensus"]["rows"], ("metric", "value", "unit", "period_end")
        ))
    else:
        lines.append(f"available: false —— {context['consensus']['reason']}")
    lines += ["", "在场的 thesis："]
    for thesis in context["theses"]:
        lines.append(
            f"- {thesis['ref']}　{thesis['statement']}"
            f"（机制：{thesis['mechanism']}；信心：{thesis['confidence']}）"
        )
    lines += ["", "市场怎么看（评级变化 / sales note / 大众）："]
    if context["market_view"]:
        for row in context["market_view"]:
            lines.append(f"- [{row.get('ref')}] {row.get('summary') or row.get('text')}")
    else:
        lines.append("available: false —— 这个 Core 没有可引用的市场看法")
    lines += ["", "可调频率的来源：" + "、".join(context["source_keys"] or ["（无）"])]
    lines += ["", "可引用数字（正文只能出现这里的数字）："]
    for row in context["numbers"]:
        lines.append(f"- [{row['claim_version_ref']}] {row['text']}")
    lines += ["", "可引用的其它 ref：",
              "、".join(sorted(citable_refs(context))[:40])]
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


# ---------------------------------------------------------------------------
# the closed output
# ---------------------------------------------------------------------------


def _lines(value: Any, permitted: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LINE_ITEMS:
        raise EarningsSeasonValidationError(
            f"lines must be a list of 1..{MAX_LINE_ITEMS} entries"
        )
    rows = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "line", "verdict", "because", "refs"
        }:
            raise EarningsSeasonValidationError(
                "each line is exactly line, verdict, because and refs"
            )
        if item["verdict"] not in VERDICTS:
            raise EarningsSeasonValidationError(
                f"verdict must be one of {list(VERDICTS)}"
            )
        refs = _ref_list(item["refs"], "lines[].refs")
        stray = sorted(set(refs) - permitted)
        if stray:
            raise EarningsSeasonValidationError(
                f"a line cites refs that were not shown: {stray}"
            )
        rows.append({
            "line": _text(item["line"], "lines[].line", maximum=120),
            "verdict": item["verdict"],
            "because": _text(item["because"], "lines[].because", maximum=MAX_ITEM_CHARS),
            "refs": refs,
        })
    return rows


def _theses(value: Any, context: Mapping[str, Any], permitted: set[str]) -> list[dict[str, Any]]:
    known = {str(thesis["ref"]): thesis for thesis in context.get("theses") or ()}
    if not isinstance(value, list) or len(value) > MAX_THESES:
        raise EarningsSeasonValidationError(
            f"theses must be a list of at most {MAX_THESES} entries"
        )
    if known and not value:
        raise EarningsSeasonValidationError(
            "a calibration that says nothing about a thesis in play has not "
            "done the work"
        )
    rows = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "thesis_ref", "decision", "action", "because", "proposed_statement", "refs"
        }:
            raise EarningsSeasonValidationError(
                "each thesis entry is exactly thesis_ref, decision, action, because, "
                "proposed_statement and refs"
            )
        ref = _text(item["thesis_ref"], "theses[].thesis_ref", maximum=512)
        if ref not in known:
            raise EarningsSeasonValidationError(
                f"theses names a thesis that was not shown: {ref}"
            )
        if ref in seen:
            raise EarningsSeasonValidationError(
                f"theses names the same thesis twice: {ref}"
            )
        seen.add(ref)
        decision = item["decision"]
        if decision not in DECISION_VOCABULARY:
            raise EarningsSeasonValidationError(
                f"decision must be one of {list(DECISION_VOCABULARY)}"
            )
        action = item["action"]
        if action not in THESIS_ACTIONS:
            raise EarningsSeasonValidationError(
                f"action must be one of {list(THESIS_ACTIONS)}"
            )
        # P14a's frozen compatibility table, consumed rather than restated:
        # "NO_CHANGE, therefore revise the thesis" is a contract-level refusal.
        if action not in DECISION_ACTIONS[decision]:
            raise EarningsSeasonValidationError(
                f"{decision} is not compatible with {action}"
            )
        proposed = item["proposed_statement"]
        if proposed is not None:
            proposed = _text(proposed, "theses[].proposed_statement", maximum=1200)
        if action == "revise_thesis" and proposed is None and decision != "THESIS_BROKEN":
            raise EarningsSeasonValidationError(
                "a revision candidate that proposes no new statement has to be a "
                "THESIS_BROKEN, otherwise it proposes nothing"
            )
        refs = _ref_list(item["refs"], "theses[].refs")
        stray = sorted(set(refs) - permitted)
        if stray:
            raise EarningsSeasonValidationError(
                f"a thesis entry cites refs that were not shown: {stray}"
            )
        if action != "no_change" and not refs:
            raise EarningsSeasonValidationError(
                "a thesis entry that proposes anything must cite what it rests on"
            )
        rows.append({
            "thesis_ref": ref,
            "decision": decision,
            "action": action,
            "because": _text(item["because"], "theses[].because", maximum=1200),
            "proposed_statement": proposed,
            "refs": refs,
        })
    return rows


def _reflection(value: Any, context: Mapping[str, Any], permitted: set[str],
                thesis_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """P14a's reflection shape, checked by P14a's rules.

    Carried on this call rather than bought with a second pair: a calibration
    *is* "what we expected, what happened and why", so asking for it twice
    would be paying twice for the same paragraph.
    """

    if not isinstance(value, Mapping):
        raise EarningsSeasonValidationError("reflection must be an object")
    extra = sorted(set(value) - REFLECTION_KEYS)
    if extra:
        raise EarningsSeasonValidationError(
            f"the reflection returned keys the contract does not have: {extra}"
        )
    missing = sorted(REFLECTION_KEYS - set(value))
    if missing:
        raise EarningsSeasonValidationError(
            f"the reflection omitted required keys: {missing}"
        )
    citations = _ref_list(value["citations"], "reflection.citations", limit=MAX_CITATIONS)
    stray = sorted(set(citations) - permitted)
    if stray:
        raise EarningsSeasonValidationError(
            f"the reflection cites refs that were not shown: {stray}"
        )
    if not citations:
        raise EarningsSeasonValidationError(
            "a reflection with no citation is an opinion about nothing"
        )
    debates = value["missed_debates"]
    if not isinstance(debates, list) or len(debates) > 4:
        raise EarningsSeasonValidationError("missed_debates must be a list of at most 4")
    checked_debates = []
    for row in debates:
        if not isinstance(row, Mapping) or set(row) != {"question", "refs"}:
            raise EarningsSeasonValidationError(
                "each missed debate is exactly question and refs"
            )
        refs = _ref_list(row["refs"], "missed_debates[].refs")
        stray = sorted(set(refs) - permitted)
        if stray:
            raise EarningsSeasonValidationError(
                f"a missed debate cites refs that were not shown: {stray}"
            )
        checked_debates.append({
            "question": _text(row["question"], "missed_debates[].question", maximum=500),
            "refs": refs,
        })
    tracking = value["followup_tracking"]
    if not isinstance(tracking, list) or len(tracking) > 4:
        raise EarningsSeasonValidationError("followup_tracking must be a list of at most 4")
    known_sources = set(context.get("source_keys") or ())
    if tracking and not known_sources:
        raise EarningsSeasonValidationError(
            "a tracking follow-up cannot be checked without the cadence policy's "
            "source keys; refusing rather than accepting an unverifiable proposal"
        )
    checked_tracking = []
    for row in tracking:
        if not isinstance(row, Mapping) or set(row) != {
            "source_key", "interval_seconds", "because"
        }:
            raise EarningsSeasonValidationError(
                "each tracking follow-up is exactly source_key, interval_seconds "
                "and because"
            )
        if row["source_key"] not in known_sources:
            raise EarningsSeasonValidationError(
                f"followup_tracking names a source with no baseline cadence: "
                f"{row['source_key']!r}"
            )
        interval = row["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 60:
            raise EarningsSeasonValidationError(
                "followup_tracking.interval_seconds must be an integer of at least 60"
            )
        checked_tracking.append({
            "source_key": str(row["source_key"]),
            "interval_seconds": interval,
            "because": _text(row["because"], "followup_tracking[].because", maximum=400),
        })
    research = value["followup_research"]
    if not isinstance(research, list) or len(research) > 4:
        raise EarningsSeasonValidationError("followup_research must be a list of at most 4")
    checked_research = []
    for row in research:
        if not isinstance(row, Mapping) or set(row) != {"question", "wants"}:
            raise EarningsSeasonValidationError(
                "each research follow-up is exactly question and wants"
            )
        checked_research.append({
            "question": _text(row["question"], "followup_research[].question", maximum=400),
            "wants": _text(row["wants"], "followup_research[].wants", maximum=400),
        })
    market = value["market_view_vs_ours"]
    if not isinstance(market, Mapping) or set(market) != {
        "available", "our_direction", "summary", "refs"
    }:
        raise EarningsSeasonValidationError(
            "market_view_vs_ours is exactly available, our_direction, summary and refs"
        )
    if not isinstance(market["available"], bool):
        raise EarningsSeasonValidationError("market_view_vs_ours.available is a boolean")
    market_refs = _ref_list(market["refs"], "market_view_vs_ours.refs")
    if not market["available"] and market_refs:
        # P14a's refusal, and for its reason: with no consensus authority, the
        # honest answer is that we do not hold the street's view -- not a view
        # inferred from four headlines and then cited.
        raise EarningsSeasonValidationError(
            "market_view_vs_ours says it is unavailable and then cites something"
        )
    stray = sorted(set(market_refs) - permitted)
    if stray:
        raise EarningsSeasonValidationError(
            f"market_view_vs_ours cites refs that were not shown: {stray}"
        )
    return {
        "thesis_refs": [row["thesis_ref"] for row in thesis_rows],
        "what_we_expected": _text(
            value["what_we_expected"], "reflection.what_we_expected", maximum=1200),
        "what_happened": _text(
            value["what_happened"], "reflection.what_happened", maximum=1200),
        "why": _text(value["why"], "reflection.why", maximum=1200),
        "citations": citations,
        "missed_debates": checked_debates,
        "followup_tracking": checked_tracking,
        "followup_research": checked_research,
        "market_view_vs_ours": {
            "available": market["available"],
            "our_direction": _text(
                market["our_direction"], "market_view_vs_ours.our_direction", maximum=64),
            "summary": _text(
                market["summary"], "market_view_vs_ours.summary", maximum=800),
            "refs": market_refs,
        },
        "convergence_pathway": _text(
            value["convergence_pathway"], "reflection.convergence_pathway", maximum=800),
    }


def validate_calibration_output(
    value: Any, context: Mapping[str, Any]
) -> dict[str, Any]:
    """The closed shape.  Refused whole; never repaired."""

    if not isinstance(value, Mapping):
        raise EarningsSeasonValidationError("the model did not return an object")
    extra = sorted(set(value) - OUTPUT_KEYS)
    if extra:
        raise EarningsSeasonValidationError(
            f"the calibration returned keys the contract does not have: {extra}"
        )
    missing = sorted(OUTPUT_KEYS - set(value))
    if missing:
        raise EarningsSeasonValidationError(
            f"the calibration omitted required keys: {missing}"
        )
    permitted = citable_refs(context) | {
        str(row["claim_version_ref"]) for row in context.get("numbers") or ()
    } | {str(row.get("ref")) for row in context.get("market_view") or () if row.get("ref")}
    summary = _text(value["summary"], "summary", maximum=MAX_SUMMARY_CHARS)
    lines = _lines(value["lines"], permitted)
    theses = _theses(value["theses"], context, permitted)
    reflection = _reflection(value["reflection"], context, permitted, theses)
    body = calibration_body(
        {"summary": summary, "lines": lines, "theses": theses, "reflection": reflection},
        context,
    )
    stray_numbers = unsourced_numbers(body, context.get("numbers") or [])
    if stray_numbers:
        raise EarningsSeasonValidationError(
            "the calibration prints figures with no live Claim behind them: "
            f"{stray_numbers[:5]}；写 {GAP_MARKER} 而不是猜一个数字"
        )
    return {
        "summary": summary, "lines": lines, "theses": theses,
        "reflection": reflection,
    }


def calibration_body(output: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    parts = [output["summary"], "", "逐条对账："]
    for row in output["lines"]:
        parts.append(f"- {row['line']}：{row['verdict']}——{row['because']}")
    parts += ["", "这对 thesis 意味着什么（候选，人裁决）："]
    for row in output["theses"]:
        proposed = row.get("proposed_statement")
        parts.append(
            f"- {row['decision']} / {row['action']}：{row['because']}"
            + (f"　改成：{proposed}" if proposed else "")
        )
    reflection = output.get("reflection") or {}
    if reflection:
        parts += [
            "", "自我反思：",
            f"- 我们预期：{reflection['what_we_expected']}",
            f"- 实际发生：{reflection['what_happened']}",
            f"- 为什么：{reflection['why']}",
        ]
        for row in reflection.get("missed_debates") or ():
            parts.append(f"- 可能漏掉的 debate：{row['question']}")
        market = reflection.get("market_view_vs_ours") or {}
        if market.get("available"):
            parts.append(f"- 我们 vs 市场：{market.get('summary')}")
        else:
            parts.append("- 我们 vs 市场：available:false，这个 Core 拿不到街上的看法")
        parts.append(f"- 市场向我们靠拢的路径：{reflection['convergence_pathway']}")
    parts.append("")
    parts.append("未来期的预测在这一步没有被修改；要不要改由判断层就本次校准事件决定。")
    for gap in context.get("gaps") or ():
        parts.append(f"- {gap}")
    return "\n".join(parts)


def document_summary(context: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    """The figures, each beside its ref.  Same rule as the preview."""

    occurrence = context.get("occurrence") or {}
    parts = [
        f"{context['company_ref']} {occurrence.get('expected_date')} calibration",
        f"期间 {context.get('period_end') or '未知'}",
        "对账 " + "/".join(
            f"{band}:{count}" for band, count in
            sorted((context.get("tiers") or {}).get("counts", {}).items())
        ) if (context.get("tiers") or {}).get("counts") else "对账 无行",
    ]
    for row in context.get("reconciliations") or ():
        parts.append(
            f"{row['metric_ref']} 实际={row['actual_value']} 预测={row['forecast_value']}"
            f" 偏差={row['deviation_percent']}% [{row['id']}]"
        )
    actualisation = context.get("actualisation") or {}
    if actualisation.get("version_ref"):
        parts.append(f"模型已对齐 [{actualisation['version_ref']}]")
    parts.append("未来期未改")
    return "｜".join(parts)[:MAX_DOCUMENT_SUMMARY]


# ---------------------------------------------------------------------------
# the two calls
# ---------------------------------------------------------------------------


def _provenance(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
        "purpose": CALIBRATION_PURPOSE,
    }


def draft_calibration(
    context: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call: the prose, one decision per thesis, and the reflection."""

    prompt = build_calibration_prompt(context)
    try:
        call = model.call(
            purpose=CALIBRATION_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the model call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "prompt_chars": len(prompt), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_calibration_output(unwrap_json_object(call["text"]), context)
    except EarningsSeasonValidationError as exc:
        return {"status": "refused", "reason": str(exc),
                "prompt_chars": len(prompt), "model": provenance}
    return {"status": "drafted", "prompt_chars": len(prompt), "model": provenance,
            **validated}


def build_calibration_verifier_prompt(
    context: Mapping[str, Any], draft: Mapping[str, Any]
) -> str:
    lines = [
        "你在核验一份业绩校准，不是重写它。只输出 JSON。",
        "",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<一句话>"}]}',
        "",
        "拒绝的理由：说了材料里没有的事（unsupported_claim）、该带的说明没带（missing_caveat）、"
        "写错了期间（wrong_period）、编了一个数字（invented_number）。都没有就 pass。",
        "",
        f"公司：{context['company_ref']}　期间：{context.get('period_end')}",
        "",
        "校准正文：",
        calibration_body(draft, context),
        "",
        "对账事实（正文必须与这些一致）：",
        render_rows(
            context.get("reconciliations") or [],
            ("metric_ref", "deviation_percent", "direction", "band"),
        ),
        "",
        "可引用数字：",
    ]
    for row in context.get("numbers") or ():
        lines.append(f"- [{row['claim_version_ref']}] {row['text']}")
    # The reflection's own citations and its two follow-up lists.  They are
    # part of what was produced and part of what a person will read beside the
    # candidate, so a verifier that never saw them could only ever verify half
    # the answer.
    reflection = draft.get("reflection") or {}
    lines += ["", "反思引用：" + "、".join(reflection.get("citations") or ["（无）"])]
    market = reflection.get("market_view_vs_ours") or {}
    lines.append(
        f"我们 vs 市场：available={str(market.get('available')).lower()}"
        f"　{market.get('summary') or ''}"
        f"　refs={'、'.join(market.get('refs') or []) or '（无）'}"
    )
    lines.append("市场靠拢路径：" + str(reflection.get("convergence_pathway") or ""))
    for row in reflection.get("missed_debates") or ():
        lines.append(f"可能漏掉的 debate：{row['question']}　refs={'、'.join(row['refs'])}")
    for row in reflection.get("followup_tracking") or ():
        lines.append(
            f"建议调频：{row['source_key']} → {row['interval_seconds']}s　{row['because']}")
    for row in reflection.get("followup_research") or ():
        lines.append(f"建议专项研究：{row['question']}　要什么：{row['wants']}")
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def verify_calibration(
    context: Mapping[str, Any],
    draft: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    family_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """The second call.  Fails closed on independence, exactly as P14a does."""

    if draft.get("status") != "drafted":
        return {"status": "skipped", "reason": "there is no calibration to verify"}
    producer = family_resolver((draft.get("model") or {}).get("route_decision_ref"))
    if producer is None:
        return {"status": "refused", "model": None,
                "independence": {"producer_family": None, "verifier_family": None,
                                 "predicate": "model_family_ne"},
                "reason": "the model family behind the calibration could not be "
                          "resolved; an unverifiable independence claim is not "
                          "independence"}
    prompt = build_calibration_verifier_prompt(context, draft)
    try:
        call = model.call(
            purpose=CALIBRATION_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the verifier call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"), "model": None}
    provenance = _provenance(call)
    verifier = family_resolver(provenance.get("route_decision_ref"))
    independence = {"producer_family": producer, "verifier_family": verifier,
                    "predicate": "model_family_ne"}
    if verifier is None:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": "the model family behind the verifier could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    if verifier == producer:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": f"model_family_not_independent: both calls ran on {producer}"}
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]))
    except EarningsSeasonValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance,
                "independence": independence}
    return {
        "status": "verified",
        "drafted_hash": content_hash({
            "summary": draft["summary"],
            "theses": [
                {"thesis_ref": row["thesis_ref"], "decision": row["decision"]}
                for row in draft["theses"]
            ],
        }),
        "independence": independence,
        "model": provenance,
        **validated,
    }


# ---------------------------------------------------------------------------
# the event, and the effects that go through the judgement machinery
# ---------------------------------------------------------------------------


def calibration_event_payload(
    context: Mapping[str, Any], output: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What the judgement lane is told, and it is one sentence's worth.

    The forward periods were not revised.  That is the whole reason this event
    exists: the calibration settled the history and graded the quarter, and
    whether next year moves is a decision for the layer that makes decisions.
    """

    tiers = context.get("tiers") or {}
    counts = tiers.get("counts") or {}
    strongest = None
    if output:
        ranked = sorted(
            output.get("theses") or (),
            key=lambda row: DECISION_VOCABULARY.index(row["decision"]),
        )
        strongest = ranked[-1]["decision"] if ranked else None
    return {
        "occurrence_ref": context["occurrence"]["occurrence_ref"],
        "period_end": context.get("period_end"),
        "model_version_ref": (context.get("actualisation") or {}).get("version_ref")
        or context.get("model_version_ref"),
        "reconciliation_count": len(context.get("reconciliations") or ()),
        "overturn_candidates": len(tiers.get("overturn_candidates") or ()),
        "notable": int(counts.get(NOTABLE_BAND, 0)),
        "within_tolerance": int(counts.get("within_tolerance", 0)),
        "decision": strongest,
        # Said in the payload rather than left to be inferred.  A reader of
        # this event is being told that the forward view is unreviewed.
        "forward_estimates_revised": False,
    }


def emit_calibration_event(
    *,
    context: Mapping[str, Any],
    output: Mapping[str, Any] | None,
    record_event: Callable[..., Any],
    mission: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """Hand the judgement lane the one fact it needs: this quarter is settled.

    ``record_event`` is a parameter for the reason C1 made it one: P14a owns
    that module and this one must not decide on its behalf what an event is
    worth.
    """

    occurrence = context["occurrence"]
    refs = [ref for ref in occurrence.get("source_refs") or () if ref]
    refs.append(occurrence["event_ref"])
    for row in context.get("reconciliations") or ():
        if row.get("id"):
            refs.append(str(row["id"]))
    version_ref = (context.get("actualisation") or {}).get("version_ref")
    if version_ref:
        refs.append(str(version_ref))
    return record_event(
        company_ref=context["company_ref"],
        kind=CALIBRATION_EVENT_KIND,
        occurred_at=f"{context['as_of']}T00:00:00+00:00",
        source_refs=list(dict.fromkeys(refs))[:12],
        payload=calibration_event_payload(context, output),
        mission=mission,
        actor_ref=actor_ref,
    )


def strongest_decision(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """The judgement this calibration records against its own event.

    The five words are ordered by how much they change, so the strongest is
    the one furthest along the vocabulary.  A calibration with nothing to say
    about a thesis is a ``NO_CHANGE`` that still gets written down: the weekly
    meeting's question is "why did you not change your mind", and an empty
    ledger cannot answer it.
    """

    if not rows:
        return "NO_CHANGE", "note"
    ranked = sorted(rows, key=lambda row: DECISION_VOCABULARY.index(row["decision"]))
    strongest = ranked[-1]
    action = strongest["action"]
    if action not in DECISION_ACTIONS[strongest["decision"]]:  # pragma: no cover
        action = sorted(DECISION_ACTIONS[strongest["decision"]])[0]
    return strongest["decision"], action


def apply_calibration_effects(
    judgements: Any,
    *,
    event: Mapping[str, Any],
    context: Mapping[str, Any],
    output: Mapping[str, Any],
    verification: Mapping[str, Any],
    mission: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """Everything this window is allowed to write, and it is all proposals.

    In order, because the ledger's own foreign keys say so: the judgement
    first (a candidate and a reflection both reference it), then the
    reflection, then one candidate per thesis with the reflection attached,
    then a forecast revision proposal for every metric that came in at or
    beyond the overturn threshold.

    A ``ForecastRevisionProposal`` here is not a fallback for a missing grant.
    It is the shape the reconciliation contract already requires: three
    percent or more raises ``forecast_overturn``, which is a human checkpoint,
    and automation that settled it would be settling the one thing the
    checkpoint exists for.
    """

    decision, action = strongest_decision(output["theses"])
    judgement_body = {
        "decision": decision,
        "action": action,
        "driver_refs": [],
        "thesis_refs": [row["thesis_ref"] for row in output["theses"]],
        "because": output["summary"],
        "citations": sorted({
            ref for row in output["lines"] + output["theses"] for ref in row["refs"]
        }),
        "note": None,
        "research_question": None,
        "forecast_change": None,
        "model": output.get("model"),
    }
    tiers = context.get("tiers") or {}
    revising = [row for row in output["theses"] if row["action"] == "revise_thesis"]
    owed = bool(revising) or bool(tiers.get("overturn_fired"))
    effect = {
        "window": "calibration",
        "occurrence_ref": context["occurrence"]["occurrence_ref"],
        "candidates": [],
        "forecast_proposals": [],
        "reflection_ref": None,
        "reflection_owed": owed,
        "forward_estimates_revised": False,
    }
    recorded = judgements.record(
        event=event, judgement=judgement_body, verification=verification,
        effect=effect, mission=mission, actor_ref=actor_ref,
    )
    if recorded.get("status") == "duplicate":
        return {"status": "duplicate", "judgement_ref": recorded["id"],
                "reason": "this calibration event has already been judged",
                **{key: effect[key] for key in
                   ("candidates", "forecast_proposals", "reflection_ref")}}
    judgement_ref = recorded["id"]
    reflection_ref = None
    if owed:
        written = judgements.record_reflection(
            judgement=recorded, event=event, reflection=output["reflection"],
            verification=verification, mission=mission, actor_ref=actor_ref,
        )
        reflection_ref = written["id"]
    known = {str(thesis["ref"]): thesis for thesis in context.get("theses") or ()}
    candidates = []
    for row in revising:
        thesis = known[row["thesis_ref"]]
        candidates.append(judgements.record_thesis_candidate(
            judgement_ref=judgement_ref,
            thesis={"id": thesis["ref"], "content_hash": thesis["content_hash"],
                    "thesis_ref": thesis.get("thesis_ref")},
            company_ref=context["company_ref"],
            decision=row["decision"],
            because=row["because"],
            evidence_refs=row["refs"] or [event["id"]],
            falsifier_ref=None,
            proposed_statement=row.get("proposed_statement"),
            proposed_confidence=None,
            mission=mission,
            actor_ref=actor_ref,
            reflection_ref=reflection_ref,
        ))
    proposals = []
    for row in tiers.get("overturn_candidates") or ():
        proposals.append(judgements.record_forecast_proposal(
            judgement_ref=judgement_ref,
            company_ref=context["company_ref"],
            # The version the actual was written into, not the one that held
            # the estimate: a person opening the proposal wants the model as it
            # stands now, with the filed figure beside the miss.
            model_version_ref=(
                (context.get("actualisation") or {}).get("version_ref")
                or context.get("model_version_ref")
            ),
            change={
                "driver_ref": str(row.get("metric_ref")),
                "period_end": str(row.get("period_end") or context.get("period_end") or ""),
                "value": str(row.get("actual_value")),
            },
            decision=decision,
            because=(
                f"{row.get('metric_ref')} 实际与预测差 {row.get('deviation_percent')}%，"
                f"越过 forecast_reconciliation 的 overturn 档；"
                f"{output['summary'][:300]}"
            ),
            evidence_refs=[str(row.get("id")), event["id"]],
            mission=mission,
            actor_ref=actor_ref,
            reason="the reconciliation raised forecast_overturn, which is a human "
                   "checkpoint; automation proposes and does not settle it",
        ))
    return {
        "status": "applied",
        "judgement_ref": judgement_ref,
        "decision": decision,
        "action": action,
        "reflection_ref": reflection_ref,
        "candidates": [item["id"] for item in candidates],
        "forecast_proposals": [item["id"] for item in proposals],
        "forward_estimates_revised": False,
    }


def publish_calibration(
    deliverables: Any,
    *,
    context: Mapping[str, Any],
    output: Mapping[str, Any],
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    occurrence = context["occurrence"]
    section = {
        "title": f"{occurrence['event_kind']} calibration {occurrence['expected_date']}",
        "body": calibration_body(output, context),
        "claim_refs": [
            ref for ref in output["reflection"]["citations"]
            if ref.startswith("claim-version:")
        ],
        "numbers": list(context.get("numbers") or ()),
        "gaps": list(context.get("gaps") or ()),
    }
    return deliverables.publish(
        kind=CALIBRATION_KIND,
        subject_ref=occurrence["company_ref"],
        mission=mission,
        playbook=playbook,
        template_ref=TEMPLATE_REF,
        sections=[section],
        summary=document_summary(context, output),
        gaps=list(context.get("gaps") or ()),
        model_invocation_refs=[
            ref for ref in [(output.get("model") or {}).get("invocation_ref")] if ref
        ],
        actor_ref=actor_ref,
        idempotency_key=idempotency_key_for("calibration", occurrence),
    )


__all__ = [
    "CalibrationRefused",
    "NOTABLE_BAND",
    "OUTPUT_KEYS",
    "OVERTURN_BAND",
    "REFLECTION_KEYS",
    "TEMPLATE_REF",
    "THESIS_ACTIONS",
    "VERDICTS",
    "actual_rows_for_guidance",
    "actualize_for_report",
    "apply_calibration_effects",
    "build_calibration_context",
    "build_calibration_prompt",
    "build_calibration_verifier_prompt",
    "calibration_body",
    "calibration_event_payload",
    "document_summary",
    "draft_calibration",
    "emit_calibration_event",
    "input_table_for",
    "publish_calibration",
    "reconciliation_rows",
    "strongest_decision",
    "tier_counts",
    "validate_calibration_output",
    "verify_calibration",
]
