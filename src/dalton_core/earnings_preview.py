"""P14f, first window: the preview a month before the company reports.

What an analyst has on the desk the week before a print: our number for the
quarter, the street's number, the company's own guidance, the two or three
things worth listening for, and the sentence that says what would confirm the
thesis and what would break it.  One bounded call writes the prose.  Everything
it is prose *about* was computed before the call and is carried with its refs,
so the document can be replayed and every figure in it opened.

Three refusals are the whole design:

* **A citation of something the prompt never showed is a refusal of the whole
  answer.**  Not a repair: a preview with one invented ref removed is a preview
  whose remaining refs nobody has checked.
* **A figure with no live Claim behind it is a refusal.**  This is the
  deliverable authority's rule and it is checked here, before publishing, so
  the run records a refusal with a reason rather than raising out of a lane.
  It is also why our own model's numbers do not appear in the body: a forecast
  cell is not a Claim, and until the deliverable authority can carry a
  forecast-cell ref beside a figure, our number belongs in the document's
  summary line -- where it is written *with* its cell ref -- and the prose
  speaks about it in words.  The report names that as an integration to-do.
* **``available: false`` is an answer.**  There is no consensus authority on
  this Core yet.  A preview that inferred the street from four sell-side
  headlines would be worse than one that says it does not know, and the
  prompt says so out loud rather than leaving the model to work it out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .cockpit_model import (CockpitModelError, independent_model_call,
                            lane_status_for, unwrap_json_object)
from .earnings_season import (
    MAX_THESES,
    PREVIEW_KIND,
    PREVIEW_PURPOSE,
    PREVIEW_VERIFIER_PURPOSE,
    EarningsSeasonValidationError,
    bounded_text,
    citable_refs,
    consensus_block,
    forecast_rows,
    guidance_vs_actual,
    idempotency_key_for,
    ref_list,
    render_rows,
    reported_period,
    watch_list,
)
from .mission_deliverable import GAP_MARKER, unsourced_numbers
from .store import content_hash

TEMPLATE_REF = "template:earnings-preview:p14f:v1"
MAX_SUMMARY_CHARS = 1200
MAX_ITEM_CHARS = 400
MAX_WATCH_ITEMS = 6
MAX_SIGNAL_ITEMS = 4
MAX_CITATIONS = 16
MAX_PROMPT_CHARS = 24_000
# The document summary line is the one place a figure of ours may appear, and
# it appears with the ref of the cell it came from.  ``publish`` caps the
# field at 2000; this leaves room for the caveat.
MAX_DOCUMENT_SUMMARY = 1800

# The closed output.  A sixth key is a contract change.
OUTPUT_KEYS: frozenset[str] = frozenset({
    "summary", "what_to_watch", "confirms_thesis", "breaks_thesis", "citations",
})
VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "unsupported_claim", "missing_caveat", "wrong_period", "invented_number",
    "ok",
)


def _text(value: Any, name: str, *, maximum: int) -> str:
    return bounded_text(value, name, maximum=maximum)


def _ref_list(value: Any, name: str, *, limit: int = MAX_CITATIONS) -> list[str]:
    return ref_list(value, name, limit=limit)


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def build_preview_context(
    *,
    occurrence: Mapping[str, Any],
    mission: Mapping[str, Any],
    model_version: Mapping[str, Any] | None,
    guidance_profile: Mapping[str, Any] | None = None,
    theses: Sequence[Mapping[str, Any]] = (),
    debates: Sequence[Mapping[str, Any]] = (),
    claims: Sequence[Mapping[str, Any]] = (),
    consensus_reader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Everything the one bounded call is allowed to see, and nothing else.

    ``consensus_reader`` is P11b's ``latest_consensus`` by name; ``debates`` is
    P12c's ``open_debates`` output; ``guidance_profile`` is P12f's computed
    table.  All three are parameters rather than imports because each is owned
    elsewhere, and a preview that could not be written without all three would
    never be written at all.
    """

    period = reported_period(model_version, occurrence["expected_date"])
    period_end = None if period is None else str(period.get("end"))
    forecast = forecast_rows(model_version, period_end) if period_end else []
    consensus = consensus_block(
        occurrence["company_ref"], period_end or "", reader=consensus_reader
    )
    guidance = guidance_vs_actual(guidance_profile, period_end)
    drivers = list((model_version or {}).get("drivers") or ())[:6]
    watch = watch_list(debates=debates, theses=theses, drivers=drivers)
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
    if not forecast:
        gaps.append(
            f"{GAP_MARKER}：这家公司还没有覆盖该期的预测模型行，preview 里没有我们自己的数字"
        )
    if not consensus["available"]:
        gaps.append(f"{GAP_MARKER}：consensus {consensus['reason']}")
    if not guidance["available"]:
        gaps.append(f"{GAP_MARKER}：guidance {guidance['reason']}")
    return {
        "schema_version": "0.1",
        "window": "preview",
        "occurrence": dict(occurrence),
        "company_ref": occurrence["company_ref"],
        "mission_ref": mission["id"],
        "period": period,
        "period_end": period_end,
        "forecast": forecast,
        "model_version_ref": (model_version or {}).get("id"),
        "consensus": consensus,
        "guidance": guidance,
        "watch": watch,
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
        "source_refs": list(occurrence.get("source_refs") or ()),
        "gaps": gaps,
    }


def build_preview_prompt(context: Mapping[str, Any]) -> str:
    occurrence = context["occurrence"]
    caveat = occurrence.get("date_caveat") or ""
    lines: list[str] = [
        "你在写一份业绩前瞻（earnings preview）。只输出 JSON，不要 markdown 代码块。",
        "",
        "规则：",
        "1. 只能引用下面出现过的 ref；引用没出现过的 ref，整条回答被拒绝。",
        "2. 正文里出现的每一个数字都必须来自「可引用数字」表；我们自己的预测数字不要写进正文，"
        "用「高于/低于/落在指引区间内」这样的措辞说它，具体数字由文档摘要带 ref 给出。",
        f"3. 拿不到的东西写 {GAP_MARKER}，不要猜。consensus 标着 available:false 时，"
        "明说这个 Core 没有街上的数字，不要从新闻里推一个出来。",
        "4. 和市场一致等于没有看法：说清我们和街上/指引差在哪，以及什么可观测量会把差距收掉。",
    ]
    if caveat:
        lines.append(
            f"5. 这场业绩的日期还没被公司确认（{caveat}，{occurrence['date_confidence']}）。"
            "摘要与正文都要带上这句话。"
        )
    lines += [
        "",
        "输出（键固定，多一个少一个都会被拒绝）：",
        '{"summary": "<两到四句话>",',
        ' "what_to_watch": [{"question": "<听什么>", "why": "<为什么这次重要>", "refs": []}],',
        ' "confirms_thesis": [{"observable": "<看到什么算 thesis 被证实>", "thesis_ref": "<thesis 版本 ref>", "refs": []}],',
        ' "breaks_thesis": [{"observable": "<看到什么算 thesis 被打破>", "thesis_ref": "<thesis 版本 ref>", "refs": []}],',
        ' "citations": ["<这份前瞻依据的 ref>"]}',
        "",
        f"公司：{context['company_ref']}",
        f"预计公布日：{occurrence['expected_date']}（{occurrence['date_confidence']}）"
        + (f"　{caveat}" if caveat else ""),
        f"本次业绩对应的期间：{context.get('period_end') or '未知'}",
        "",
    ]
    lines.append("我们的预测（正文不要直接写这些数字）：")
    if context["forecast"]:
        lines.append(render_rows(
            context["forecast"], ("label", "kind", "value", "unit", "status")
        ))
    else:
        lines.append(f"{GAP_MARKER}：没有覆盖该期的预测行")
    lines.append("")
    lines.append("consensus：")
    if context["consensus"]["available"]:
        lines.append(render_rows(
            context["consensus"]["rows"], ("metric", "value", "unit", "period_end")
        ))
    else:
        lines.append(f"available: false —— {context['consensus']['reason']}")
    lines.append("")
    lines.append("公司自己的指引（P12f 的事件表）：")
    if context["guidance"]["available"]:
        lines.append(render_rows(
            context["guidance"]["rows"],
            ("measure", "period", "guide_low", "guide_high", "guide_unit", "verdict"),
        ))
    else:
        lines.append(f"available: false —— {context['guidance']['reason']}")
    lines.append("")
    lines.append(f"值得听的（来源：{context['watch']['source']}）：")
    for row in context["watch"]["rows"]:
        lines.append(f"- [{row['source']}] {row['question']}　refs={','.join(row['refs'])}")
    lines.append("")
    lines.append("在场的 thesis：")
    for thesis in context["theses"]:
        lines.append(
            f"- {thesis['ref']}　{thesis['statement']}"
            f"（机制：{thesis['mechanism']}；信心：{thesis['confidence']}）"
        )
    lines.append("")
    lines.append("可引用数字（正文只能出现这里的数字）：")
    for row in context["numbers"]:
        lines.append(f"- [{row['claim_version_ref']}] {row['text']}")
    lines.append("")
    lines.append("可引用的其它 ref：")
    lines.append("、".join(sorted(citable_refs(context))[:40]))
    prompt = "\n".join(lines)
    return prompt[:MAX_PROMPT_CHARS]


# ---------------------------------------------------------------------------
# the closed output
# ---------------------------------------------------------------------------


def _signal_rows(
    value: Any, name: str, context: Mapping[str, Any], permitted: set[str],
    *, limit: int, fields: tuple[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > limit:
        raise EarningsSeasonValidationError(
            f"{name} must be a list of at most {limit} entries"
        )
    known = {str(thesis["ref"]) for thesis in context.get("theses") or ()}
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != set(fields) | {"refs"}:
            raise EarningsSeasonValidationError(
                f"each {name} entry is exactly {', '.join(fields)} and refs"
            )
        row: dict[str, Any] = {
            field: _text(item[field], f"{name}[].{field}", maximum=MAX_ITEM_CHARS)
            for field in fields
        }
        if "thesis_ref" in fields and row["thesis_ref"] not in known:
            raise EarningsSeasonValidationError(
                f"{name} names a thesis that was not shown: {row['thesis_ref']}"
            )
        refs = _ref_list(item["refs"], f"{name}[].refs")
        stray = sorted(set(refs) - permitted)
        if stray:
            raise EarningsSeasonValidationError(
                f"{name} cites refs that were not shown: {stray}"
            )
        row["refs"] = refs
        rows.append(row)
    return rows


def validate_preview_output(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape.  Refused whole; never repaired."""

    if not isinstance(value, Mapping):
        raise EarningsSeasonValidationError("the model did not return an object")
    extra = sorted(set(value) - OUTPUT_KEYS)
    if extra:
        raise EarningsSeasonValidationError(
            f"the preview returned keys the contract does not have: {extra}"
        )
    missing = sorted(OUTPUT_KEYS - set(value))
    if missing:
        raise EarningsSeasonValidationError(f"the preview omitted required keys: {missing}")
    permitted = citable_refs(context) | {
        str(row["claim_version_ref"]) for row in context.get("numbers") or ()
    }
    summary = _text(value["summary"], "summary", maximum=MAX_SUMMARY_CHARS)
    watch = _signal_rows(
        value["what_to_watch"], "what_to_watch", context, permitted,
        limit=MAX_WATCH_ITEMS, fields=("question", "why"),
    )
    if not watch:
        raise EarningsSeasonValidationError(
            "a preview with nothing to watch for is not a preview"
        )
    confirms = _signal_rows(
        value["confirms_thesis"], "confirms_thesis", context, permitted,
        limit=MAX_SIGNAL_ITEMS, fields=("observable", "thesis_ref"),
    )
    breaks = _signal_rows(
        value["breaks_thesis"], "breaks_thesis", context, permitted,
        limit=MAX_SIGNAL_ITEMS, fields=("observable", "thesis_ref"),
    )
    if context.get("theses") and not breaks:
        raise EarningsSeasonValidationError(
            "a preview that cannot say what would break the thesis has not "
            "engaged with it"
        )
    citations = _ref_list(value["citations"], "citations")
    stray = sorted(set(citations) - permitted)
    if stray:
        raise EarningsSeasonValidationError(
            f"citations names refs that were not shown: {stray}"
        )
    if not citations:
        raise EarningsSeasonValidationError(
            "a preview with no citation is an opinion about nothing"
        )
    body = preview_body(
        {"summary": summary, "what_to_watch": watch,
         "confirms_thesis": confirms, "breaks_thesis": breaks},
        context,
    )
    stray_numbers = unsourced_numbers(body, context.get("numbers") or [])
    if stray_numbers:
        raise EarningsSeasonValidationError(
            "the preview prints figures with no live Claim behind them: "
            f"{stray_numbers[:5]}；写 {GAP_MARKER} 而不是猜一个数字"
        )
    caveat = (context.get("occurrence") or {}).get("date_caveat") or ""
    if caveat and caveat not in summary:
        # Checked against the model's own paragraph rather than the assembled
        # body: ``preview_body`` appends the caveat itself, so checking the
        # body would pass every answer and prove nothing.  A preview written
        # against a date the company has not confirmed has to say so where a
        # reader starts reading.
        raise EarningsSeasonValidationError(
            f"the report date is not confirmed and the preview's summary does "
            f"not say so（{caveat}）"
        )
    return {
        "summary": summary,
        "what_to_watch": watch,
        "confirms_thesis": confirms,
        "breaks_thesis": breaks,
        "citations": citations,
    }


def preview_body(output: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    """The document's one section, assembled from the closed output.

    Deterministic: the same output produces the same body, so the version chain
    can be replayed and a body cannot drift from the answer it came from.
    """

    occurrence = context.get("occurrence") or {}
    parts = [output["summary"], "", "值得听的："]
    for row in output["what_to_watch"]:
        parts.append(f"- {row['question']}——{row['why']}")
    if output["confirms_thesis"]:
        parts += ["", "看到这些算 thesis 被证实："]
        parts += [f"- {row['observable']}" for row in output["confirms_thesis"]]
    if output["breaks_thesis"]:
        parts += ["", "看到这些算 thesis 被打破："]
        parts += [f"- {row['observable']}" for row in output["breaks_thesis"]]
    for gap in context.get("gaps") or ():
        parts.append(f"- {gap}")
    caveat = occurrence.get("date_caveat")
    if caveat:
        parts += ["", f"（{caveat}：公布日期来自 {occurrence.get('date_confidence')} 的来源，"
                      "公司尚未确认，本前瞻按预期日期写。）"]
    return "\n".join(parts)


def document_summary(context: Mapping[str, Any]) -> str:
    """Our number, the street's and the company's, each beside its ref.

    The deliverable's section body cannot carry a figure whose source is a
    forecast cell -- the authority's number check knows Claims and nothing else
    -- so the comparison the preview exists for is written here, where each
    figure is followed by the ref it came from.  Not a workaround for the rule
    but an application of it: every number in this line has a ref, and the ref
    is the thing a reader opens.
    """

    occurrence = context.get("occurrence") or {}
    parts = [
        f"{context['company_ref']} {occurrence.get('expected_date')} preview"
        f"（{occurrence.get('date_confidence')}）",
        f"期间 {context.get('period_end') or '未知'}",
    ]
    if context.get("model_version_ref"):
        parts.append(f"我们的模型 [{context['model_version_ref']}]")
    for row in context.get("forecast") or ():
        if row.get("value") is None:
            # An unavailable cell is a gap, not a figure.  Printing "None"
            # beside a ref would be the one thing this line exists to stop.
            parts.append(f"我们 {row['label']}={GAP_MARKER} [{(row['refs'] or [''])[0]}]")
            continue
        parts.append(
            f"我们 {row['label']}={row['value']}{row['unit'] or ''}"
            f" [{(row['refs'] or [''])[0]}]"
        )
    if (context.get("consensus") or {}).get("available"):
        for row in context["consensus"]["rows"]:
            parts.append(
                f"街上 {row['metric']}={row['value']}{row['unit'] or ''}"
                f" [{(row['refs'] or [''])[0]}]"
            )
    else:
        parts.append("街上 available:false")
    if (context.get("guidance") or {}).get("available"):
        for row in context["guidance"]["rows"]:
            parts.append(
                f"指引 {row['measure']} {row['guide_low']}~{row['guide_high']}"
                f"{row['guide_unit'] or ''} [{(row['refs'] or [''])[0]}]"
            )
    else:
        parts.append("指引 available:false")
    if occurrence.get("date_caveat"):
        parts.append(str(occurrence["date_caveat"]))
    return "｜".join(parts)[:MAX_DOCUMENT_SUMMARY]


# ---------------------------------------------------------------------------
# the one bounded call, and the second one that checks it
# ---------------------------------------------------------------------------


def _provenance(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
        "purpose": PREVIEW_PURPOSE,
    }


def draft_preview(
    context: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call, checked against the closed schema before it is believed."""

    prompt = build_preview_prompt(context)
    try:
        call = model.call(
            purpose=PREVIEW_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the model call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "prompt_chars": len(prompt), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_preview_output(unwrap_json_object(call["text"]), context)
    except EarningsSeasonValidationError as exc:
        return {"status": "refused", "reason": str(exc),
                "prompt_chars": len(prompt), "model": provenance}
    return {"status": "drafted", "prompt_chars": len(prompt), "model": provenance,
            **validated}


def build_preview_verifier_prompt(
    context: Mapping[str, Any], draft: Mapping[str, Any]
) -> str:
    lines = [
        "你在核验一份业绩前瞻，不是重写它。只输出 JSON。",
        "",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<一句话>"}]}',
        "",
        "拒绝的理由只有四种：说了材料里没有的事（unsupported_claim）、"
        "日期未确认却没带 caveat（missing_caveat）、写错了期间（wrong_period）、"
        "编了一个数字（invented_number）。都没有就 pass，findings 里写一条 ok。",
        "",
        f"公司：{context['company_ref']}　期间：{context.get('period_end')}",
        f"日期确认状态：{(context.get('occurrence') or {}).get('date_confidence')}"
        f"　caveat：{(context.get('occurrence') or {}).get('date_caveat') or '无'}",
        "",
        "前瞻正文：",
        preview_body(draft, context),
        "",
        "引用：" + "、".join(draft.get("citations") or ()),
        "",
        "可引用数字：",
    ]
    for row in context.get("numbers") or ():
        lines.append(f"- [{row['claim_version_ref']}] {row['text']}")
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def validate_verifier_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EarningsSeasonValidationError("the verifier did not return an object")
    if set(value) != {"verdict", "findings"}:
        raise EarningsSeasonValidationError(
            "the verifier returns exactly a verdict and findings"
        )
    verdict = value["verdict"]
    if verdict not in VERIFIER_VERDICTS:
        raise EarningsSeasonValidationError(
            f"verdict must be one of {list(VERIFIER_VERDICTS)}"
        )
    findings = value["findings"]
    if not isinstance(findings, list) or not 1 <= len(findings) <= 6:
        raise EarningsSeasonValidationError("findings must be a list of 1..6 entries")
    checked = []
    for item in findings:
        if not isinstance(item, Mapping) or set(item) != {"code", "detail"}:
            raise EarningsSeasonValidationError(
                "each finding is exactly a code and a detail"
            )
        if item["code"] not in VERIFIER_FINDING_CODES:
            raise EarningsSeasonValidationError(
                f"finding code must be one of {list(VERIFIER_FINDING_CODES)}"
            )
        checked.append({
            "code": item["code"],
            "detail": _text(item["detail"], "finding.detail", maximum=300),
        })
    return {"verdict": verdict, "findings": checked}


def verify_preview(
    context: Mapping[str, Any],
    draft: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    family_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """P14a's rule, applied to this window: fail closed on independence.

    A verifier that might be the producer is not one, so an unresolvable or
    identical model family is a refusal rather than a pass.
    """

    if draft.get("status") != "drafted":
        return {"status": "skipped", "reason": "there is no preview to verify"}
    producer = family_resolver((draft.get("model") or {}).get("route_decision_ref"))
    if producer is None:
        return {"status": "refused", "model": None,
                "independence": {"producer_family": None, "verifier_family": None,
                                 "predicate": "model_family_ne"},
                "reason": "the model family behind the preview could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    prompt = build_preview_verifier_prompt(context, draft)
    try:
        call = independent_model_call(
            model,
            producer_route_decision_refs=[
                (draft.get("model") or {}).get("route_decision_ref")
            ],
            purpose=PREVIEW_VERIFIER_PURPOSE, request_id=request_id, prompt=prompt,
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
            "summary": draft["summary"], "citations": draft["citations"],
        }),
        "independence": independence,
        "model": provenance,
        **validated,
    }


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------


def publish_preview(
    deliverables: Any,
    *,
    context: Mapping[str, Any],
    draft: Mapping[str, Any],
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """One version per occurrence on the company's preview chain."""

    occurrence = context["occurrence"]
    claim_refs = [
        ref for ref in draft["citations"] if ref.startswith("claim-version:")
    ]
    section = {
        "title": f"{occurrence['event_kind']} preview {occurrence['expected_date']}",
        "body": preview_body(draft, context),
        "claim_refs": claim_refs,
        "numbers": list(context.get("numbers") or ()),
        "gaps": list(context.get("gaps") or ()),
    }
    return deliverables.publish(
        kind=PREVIEW_KIND,
        subject_ref=occurrence["company_ref"],
        mission=mission,
        playbook=playbook,
        template_ref=TEMPLATE_REF,
        sections=[section],
        summary=document_summary(context),
        gaps=list(context.get("gaps") or ()),
        model_invocation_refs=[
            ref for ref in [(draft.get("model") or {}).get("invocation_ref")] if ref
        ],
        actor_ref=actor_ref,
        idempotency_key=idempotency_key_for("preview", occurrence),
    )


__all__ = [
    "MAX_CITATIONS",
    "MAX_SIGNAL_ITEMS",
    "MAX_WATCH_ITEMS",
    "OUTPUT_KEYS",
    "TEMPLATE_REF",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "build_preview_context",
    "build_preview_prompt",
    "build_preview_verifier_prompt",
    "document_summary",
    "draft_preview",
    "preview_body",
    "publish_preview",
    "validate_preview_output",
    "validate_verifier_output",
    "verify_preview",
]
