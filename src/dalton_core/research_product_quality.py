"""Product-specific score adapters; no model call or state mutation."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .store import content_hash


def held_claim_sources(core: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve only cited ClaimVersions; never infer a source from nearby text."""
    refs = set()
    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.startswith("claim-version:") and not any(c.isspace() for c in value):
            refs.add(value)
    walk(record)
    if core is None or not core.execute("SELECT 1 FROM sqlite_master WHERE name='claim_versions'").fetchone():
        return {}
    out = {}
    for ref in sorted(refs):
        row = core.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (ref,)).fetchone()
        if row:
            claim = json.loads(row[0])
            out[ref] = {"ref":ref, "kind":"claim", "text":claim.get("statement") or "", "period":claim.get("period")}
    return out


def _section(title: str, text: str, refs: list[Any], sources: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for ref in refs:
        row = ref if isinstance(ref, Mapping) else sources.get(str(ref))
        if isinstance(row, Mapping) and row.get("ref"):
            rows.append(dict(row))
    return {"title": title, "body": text,
            "claim_refs": [row["ref"] for row in rows if str(row["ref"]).startswith("claim-version:")],
            "source_refs": rows,
            "numbers": [{"text": row.get("text") or row.get("statement") or "",
                         "number_ref": row["ref"], "period": row.get("period")} for row in rows],
            "gaps": []}


def product_artefact(kind: str, record: Mapping[str, Any], *,
                     sources: Mapping[str, Any] | None = None, playbook: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from .research_quality_score import artefact, artefact_from_deliverable

    sources = dict(sources or {})
    if kind == "company_dossier":
        from .company_dossier import dossier_artefact
        return dossier_artefact(record)
    if kind in {"initial_screen", "investment_memo"}:
        return artefact_from_deliverable(record, playbook=playbook)
    sections = []
    if kind == "debate_map":
        for debate in record.get("debates") or []:
            for key, label in (("bull_position", "多方"), ("bear_position", "空方"),
                               ("market_position", "市场看法"), ("our_position", "本所判断")):
                position = debate.get(key) or {}
                sections.append(_section(f"{debate.get('question', '')} · {label}",
                    str(position.get("statement") or ""),
                    list(position.get("claim_refs") or position.get("refs") or []), sources))
    elif kind == "event_judgement":
        for key in ("because", "note"):
            if key == "because" or record.get(key):
                sections.append(_section(key, str(record.get(key) or ""), list(record.get("citations") or []), sources))
        change = record.get("forecast_change") or {}
        if change.get("because"):
            sections.append(_section("预测变更依据", change["because"], list(record.get("citations") or []), sources))
    else:
        raise ValueError(f"unsupported product quality kind: {kind}")
    return artefact(artefact_kind=kind, ref=str(record.get("id") or "draft:"+content_hash(record)),
                    hash=str(record.get("content_hash") or content_hash(record)),
                    subject_ref=record.get("company_ref") or record.get("subject_ref"),
                    sections=sections, expected_sections=[s["title"] for s in sections] or ["正文"])


def product_quality(kind: str, record: Mapping[str, Any], *, core: Any = None,
                    sources: Mapping[str, Any] | None = None, playbook: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from .research_quality_rubrics import rubric
    from .research_quality_score import run_deterministic

    art = product_artefact(kind, record, sources=sources, playbook=playbook)
    standard = rubric("initial_screen_strl" if kind == "initial_screen" and
                      str(record.get("template_ref") or "").startswith("contract:initial-screen-contract:strl") else kind)
    return {"rubric_ref": standard.rubric_ref, "rubric_hash": standard.content_hash,
            "target_ref": art["ref"], "target_hash": art["hash"], "recorded": False,
            "judge_status": "not_run", **run_deterministic(art, standard, core=core)}


def published_quality(core: Any, kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
    from .research_quality_rubrics import rubric
    name = "initial_screen_strl" if kind == "initial_screen" and str(record.get("template_ref") or "").startswith("contract:initial-screen-contract:strl") else kind
    standard = rubric(name)
    absent = {"recorded": False, "label": "尚未完成本版本的质量评审"}
    if not core.execute("SELECT 1 FROM sqlite_master WHERE name='research_quality_score_versions'").fetchone():
        return absent
    row = core.execute("SELECT record_json FROM research_quality_score_versions WHERE target_ref=? "
        "AND target_hash=? AND rubric_ref=? AND rubric_hash=? ORDER BY created_at DESC,version_number DESC LIMIT 1",
        (record["id"],record["content_hash"],standard.rubric_ref,standard.content_hash)).fetchone()
    if row is None:
        return absent
    score = json.loads(row[0])
    verifier = score.get("verifier") or {}
    judge = score.get("judge") or {}
    passed = bool((score.get("deterministic") or {}).get("passed"))
    verified = verifier.get("status") == "verified" and verifier.get("verdict") == "pass"
    return {"recorded":True, "score_ref":score.get("id"), "rubric_hash":standard.content_hash,
            "checks_passed":passed, "judge_status":judge.get("status") or "not_run", "verified":verified,
            "label": ("规则检查发现待复核问题" if not passed else "规则检查通过；独立评分已复核" if verified
                      else "规则检查通过；独立评分尚未复核")}
