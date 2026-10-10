"""Read published research without initializing or writing its authorities."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

from .company_dossier import (CompanyDossierAuthority, CompanyDossierError,
                              dossier_completeness, section_body)
from .coverage_mission import (CoverageMissionAuthority, CoverageMissionError,
                               validate_mission_stage_record)
from .debate_map import DebateMapAuthority, DebateMapError
from .industry_framework import (IndustryFrameworkAuthority, IndustryFrameworkError,
                                 deliverable_sections)
from .store import content_hash


def _comparison_table_display(title: Any, body: Any,
                              numbers: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Render every cell of the closed governed comparison TSV for the UI."""
    from .numeric_display import format_typed_value
    from .research_html_export import _structured_comparison_display
    from .cockpit_plane import claim_period_display_label

    checked = _structured_comparison_display(str(title or ""), body, numbers)
    if checked is None:
        return None
    raw = str(body or "")
    lines = raw.splitlines()
    boundary = next((index for index, line in enumerate(lines[1:], 1)
                     if not line or line.startswith("# ")), len(lines))
    cells = [line.split("\t") for line in lines[:boundary]]
    if not cells:
        return None
    metric_keys = {
        "revenue": ("营业收入", "amount"),
        "revenue_yoy_growth": ("营业收入同比增速", "percent"),
        "gross_margin": ("毛利率", "percent"),
        "operating_margin": ("营业利润率", "percent"),
        "营业收入": ("营业收入", "amount"),
        "营业收入同比增速": ("营业收入同比增速", "percent"),
        "毛利率": ("毛利率", "percent"),
        "营业利润率": ("营业利润率", "percent"),
    }
    headers = ["公司", "指标"] + [claim_period_display_label(value) or value
                                  for value in cells[0][2:]]
    rows = []
    for row in cells[1:]:
        label_kind = metric_keys.get(row[1])
        if label_kind is None:
            return None
        label, kind = label_kind
        shown = []
        for value in row[2:]:
            if value == "-":
                shown.append("—")
            elif kind == "amount" and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                shown.append(format_typed_value(
                    value, unit="usd", scale="one", currency="USD", metric="revenue"))
            elif kind == "percent" and re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", value):
                shown.append(format_typed_value(
                    value[:-1], unit="percent", scale="one", metric=row[1]))
            else:
                return None
        rows.append([row[0], label, *shown])
    return {"headers": headers, "rows": rows, "note": checked[0], "original": checked[1]}


def _reader(connection: sqlite3.Connection, authority: type) -> Any:
    instance = authority.__new__(authority)
    instance.connection = connection
    return instance


def _sections(kind: str, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    if kind == "dossier":
        blocks = [(s["aspect"], s) for s in record.get("sections", [])]
        blocks += [(key, record[key]) for key in ("industry_classification", "variant_view")
                   if record.get(key)]
        return [{"title": title, "body": section_body(block),
                 "sources": list(block.get("sources") or []),
                 "gaps": list(block.get("gaps") or []) +
                         ([block["reason"]] if block.get("reason") else [])}
                for title, block in blocks]
    if kind == "debate_map":
        sections = []
        for debate in record.get("debates", []):
            for key, label in (("bull_position", "多方"), ("bear_position", "空方"),
                               ("market_position", "市场看法"), ("our_position", "我们的判断")):
                position = debate.get(key) or {}
                sections.append({"title": f"{debate['question']} · {label}",
                                 "body": position.get("statement") or "尚未形成判断",
                                 "sources": list(position.get("claim_refs") or position.get("refs") or []),
                                 "position": ({key: position.get(key) for key in
                                               ("available", "lean", "state", "side")
                                               if key in position}),
                                 "gaps": [], "debate_status": debate.get("status"),
                                 "last_shift_reason": debate.get("last_shift_reason"),
                                 "localization_role": ("declared_absence"
                                     if (key=="market_position" and position.get("available") is False
                                         or key=="our_position" and position.get("state")=="none_yet")
                                     else "narrative")})
        return sections
    sections = deliverable_sections(record) if kind == "industry_framework" else record.get("sections", [])
    projected = []
    for s in sections:
        gaps = list(s.get("gaps") or [])
        row = {"title":s["title"],"body":s.get("body") or "",
               "sources":list(s.get("claim_refs") or []),
               "numbers":list(s.get("numbers") or []),"gaps":gaps}
        if kind=="industry_framework" and s["title"]=="gaps" and not row["body"]:
            if not gaps: continue  # An optional appendix with no open gaps.
            row.update(body="\n".join(gaps),localization_role="gap_appendix")
        projected.append(row)
    return projected


_CN_DIGITS = "零一二三四五六七八九十"
_HAN = re.compile(r"[\u3400-\u9fff]")
_CAUSAL_CHAIN = re.compile(r"causal_chain:(\d+)")
_MACHINE_TITLE = re.compile(r"[a-z][a-z0-9_]*(?::\d+)?")
_SECTION_TITLES = {
    "industry_characteristics": "行业特征",
    "long_term_drivers": "长期驱动因素",
    "short_term_drivers": "短期驱动因素",
    "cross_company_comparison": "公司比较",
}


def _chinese_ordinal(number: int) -> str:
    if 0 < number <= 10:
        return _CN_DIGITS[number]
    if 10 < number < 20:
        return "十" + _CN_DIGITS[number - 10]
    return str(number)


def _first_clause(text: Any, maximum: int = 24) -> str:
    """The opening clause of a Chinese body, short enough for a heading."""

    for line in str(text or "").splitlines():
        line = line.strip().lstrip("#-*· ").strip()
        if not line or not _HAN.search(line):
            continue
        clause = re.split(r"[。；;，,：:！？!?]", line, maxsplit=1)[0].strip()
        if clause:
            return clause if len(clause) <= maximum else clause[:maximum] + "…"
    return ""


def causal_chain_title(index: int, title: Any = None, body: Any = None) -> str:
    """「因果链一 · <its own Chinese title>」 for the industry framework's ``causal_chain:N``.

    The framework record names every link in Chinese (``sections[].title``);
    the deliverable projection keeps the machine id ``causal_chain:N`` so its
    hash and the language-review receipts bound to it stay unchanged.  The
    heading a reader sees is the link's own title, else the first clause of
    its body, numbered from one.
    """

    head = f"因果链{_chinese_ordinal(index + 1)}"
    named = str(title or "").strip()
    if named and _HAN.search(named) and not _CAUSAL_CHAIN.fullmatch(named):
        return f"{head} · {named}"
    clause = _first_clause(body)
    return f"{head} · {clause}" if clause else head


def _section_label(kind: str, record: Mapping[str, Any], raw_title: Any,
                   body: Any) -> str | None:
    """A reader-facing heading for a section whose title is a machine id."""

    title = str(raw_title or "")
    chain = _CAUSAL_CHAIN.fullmatch(title)
    if kind == "industry_framework" and chain:
        index = int(chain.group(1))
        named = next((s.get("title") for s in record.get("sections") or []
                      if isinstance(s, Mapping) and s.get("link_index") == index), None)
        return causal_chain_title(index, named, body)
    return _SECTION_TITLES.get(title)


def version_label(version: Any, version_ref: Any = None) -> str:
    """「第 N 版」 from the record's own number, else the ref's trailing number.

    Never 「第 ? 版」: a ref with no number reads as 「版本 <short id>」.
    """

    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        return f"第 {version} 版"
    if isinstance(version, str) and version.strip().isdigit() and int(version) > 0:
        return f"第 {int(version)} 版"
    ref = str(version_ref or "").strip()
    if ref:
        tail = ref.rsplit(":", 1)[-1]
        if tail.isdigit() and int(tail) > 0:
            return f"第 {int(tail)} 版"
        return f"版本 {tail[:8]}"
    return "版本未标明"


RETIRED_CITATION_NOTICE = "引用的 {count} 条结论已退役，这部分内容待重新核对"


def retired_citation_notice(count: int) -> str | None:
    """The one sentence a product shows when it cites Claims retired since."""

    return RETIRED_CITATION_NOTICE.format(count=count) if count > 0 else None


def _retired_sources(sources: Any, retired: set[str]) -> list[str]:
    """Retired Claim refs among a section's sources, which are refs or source rows."""

    if not retired:
        return []
    refs = []
    for source in sources or []:
        ref = source.get("ref") if isinstance(source, Mapping) else source
        if isinstance(ref, str) and ref in retired and ref not in refs:
            refs.append(ref)
    return refs


def research_library(connection: sqlite3.Connection, mission: Mapping[str, Any],
                     company_ref: str, *, localize: bool = True) -> dict[str, Any]:
    if company_ref not in {m["company_ref"] for m in mission["universe"]}:
        raise ValueError("company is outside the current research mission")
    products = []
    # Display-only facts kept beside the products, attached after the
    # localization lookup: that lookup hashes the whole product, so a field
    # added before it would orphan every language-review receipt.
    display: list[dict[str, Any]] = []
    for kind, label, table in (
        ("dossier", "公司档案", "company_dossier_versions"),
        ("debate_map", "核心争议", "debate_map_versions"),
        ("industry_framework", "行业框架", "industry_framework_versions"),
        ("initial_screen", "初步筛选", "mission_deliverable_versions"),
        ("investment_memo", "投资备忘录", "mission_deliverable_versions"),
    ):
        subject_ref = mission["industry_ref"] if kind == "industry_framework" else company_ref
        item: dict[str, Any] = {"kind": kind, "label": label, "status": "missing",
                                "subject_ref": subject_ref,
                                "sections": [], "gaps": []}
        products.append(item)
        shown: dict[str, Any] = {}
        display.append(shown)
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)).fetchone():
            item["reason"] = "尚未发布此类研究产物"
            continue
        try:
            if kind == "dossier":
                record = _reader(connection, CompanyDossierAuthority).latest(company_ref)
            elif kind == "debate_map":
                record = _reader(connection, DebateMapAuthority).current(company_ref)
            elif kind == "industry_framework":
                record = _reader(connection, IndustryFrameworkAuthority).latest(mission["industry_ref"])
            else:
                row = connection.execute(
                    "SELECT v.record_json,v.content_hash FROM mission_deliverable_pointer p "
                    "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
                    "WHERE v.kind=? AND v.subject_ref=? ORDER BY v.created_at DESC LIMIT 1",
                    (kind, company_ref),
                ).fetchone()
                record = None if row is None else json.loads(row["record_json"])
                if record is not None and not (
                    record.get("content_hash") == row["content_hash"] ==
                    content_hash({k: v for k, v in record.items() if k != "content_hash"})
                ):
                    raise ValueError("published document integrity check failed")
            if record is None:
                item["reason"] = "尚未发布此类研究产物"
                continue
            bound = record.get("mission_version_ref") or (record.get("bindings") or {}).get("mission_version_ref")
            item.update(status="available", version_ref=record["id"],
                        content_hash=record["content_hash"], created_at=record["created_at"],
                        mission_version_ref=bound,
                        mission_binding="current" if bound == mission["id"] else "historical",
                        sections=_sections(kind, record), gaps=list(record.get("gaps") or []))
            if kind in {"debate_map", "industry_framework"}:
                from .research_source_projection import PROJECTION_VERSION
                item["projection_contract"] = PROJECTION_VERSION
            shown["version"] = record.get("version")
            if kind in {"dossier", "debate_map", "initial_screen", "investment_memo"}:
                from .research_product_quality import published_quality
                shown["quality"] = published_quality(connection, "company_dossier" if kind == "dossier" else kind, record)
            shown["version_label"] = version_label(record.get("version"), record["id"])
            shown["section_labels"] = [
                _section_label(kind, record, section.get("title"), section.get("body"))
                for section in item["sections"]]
            if kind == "dossier":
                item["completeness"] = dossier_completeness(record)
            if kind == "investment_memo":
                item["approval"] = _memo_approval(
                    connection, mission, company_ref, record)
            else:
                item["approval"] = {"status": "not_applicable"}
        except (ValueError, KeyError, TypeError, sqlite3.Error,
                CompanyDossierError, DebateMapError, IndustryFrameworkError,
                CoverageMissionError) as exc:
            item.update(status="invalid", reason=str(exc), sections=[], gaps=[])
            shown.clear()
    result = {"company_ref": company_ref, "industry_ref": mission["industry_ref"],
              "mission_version_ref": mission["id"], "products": products}
    if localize:
        from .research_localization_store import localize_library
        from .research_gap_display import display_metadata_text, gap_display_text
        from .financial_display import source_display_label
        from .numeric_display import format_prose_machine_numbers

        def _numbers(text: Any) -> Any:
            return format_prose_machine_numbers(text) if isinstance(text, str) and text else text
        from .terminology import label as _term
        # Display-only fields follow the exact-source receipt lookup. Raw
        # snapshots and the source hashes used by paid reviews stay stable.
        result = localize_library(connection, result)
        from .claim_retirement import retired_claim_version_refs

        retired = retired_claim_version_refs(connection)
        for product, shown in zip(result["products"], display):
            _attach_display(product, shown)
            # Batch am: the product's analyst name from the terminology
            # registry; ``label`` stays as hashed (localization receipts).
            product["display_label"] = _term("report_kind", str(product.get("kind") or ""),
                                             str(product.get("label") or ""))
            product["display_gaps"] = [_numbers(gap_display_text(gap)) for gap in product.get("gaps", [])]
            # Display only, after the hash-bound overlay: a published product
            # is never edited, but a reader is told when it rests on Claims
            # retired since it was written.  Its own reopen path (the dossier
            # redrafts the unit) replaces it; until then this is the notice.
            product_retired: set[str] = set()
            for section in product.get("sections", []):
                comparison = _comparison_table_display(
                    section.get("title"), section.get("body"),
                    [row for row in section.get("numbers", []) if isinstance(row, Mapping)])
                section["display_body"] = _numbers(display_metadata_text(
                    comparison["note"] if comparison is not None else section.get("body") or ""))
                if comparison is not None:
                    section["display_comparison"] = {
                        "headers": comparison["headers"], "rows": comparison["rows"]}
                    section["display_body_technical"] = comparison["original"]
                section["display_gaps"] = [_numbers(gap_display_text(gap)) for gap in section.get("gaps", [])]
                # 2026-10-08 (DEFECT-04/05): a cited filed figure reads as an
                # account name, a period and an amount; the stored text (with
                # its XBRL id) stays in ``sources`` for 技术详情.
                shown_sources = [source_display_label(source)
                                 for source in section.get("sources") or []]
                if any(shown_sources):
                    section["display_sources"] = shown_sources
                cited = _retired_sources(section.get("sources"), retired)
                if cited:
                    from .product_rewrite_flags import HISTORICAL_CITATION_LABEL, HISTORICAL_CITATION_NOTE

                    product_retired.update(cited)
                    section["retired_sources"] = cited
                    section["display_gaps"].append(retired_citation_notice(len(cited)))
                    # Batch an: every retirement reason, every product kind --
                    # until rewritten the section reads as 历史引用.
                    section["citation_status_label"] = HISTORICAL_CITATION_LABEL
                    section["citation_status_note"] = HISTORICAL_CITATION_NOTE
            if product_retired:
                product["retired_citation_count"] = len(product_retired)
                product["display_gaps"].append(retired_citation_notice(len(product_retired)))
        from .legacy_gate_disposition import legacy_gate_dispositions
        result["legacy_gate_history"] = legacy_gate_dispositions(connection, company_ref)
        return result
    from .legacy_gate_disposition import legacy_gate_dispositions
    result["legacy_gate_history"] = legacy_gate_dispositions(connection, company_ref)
    return result


def _attach_display(product: dict[str, Any], shown: Mapping[str, Any]) -> None:
    """Version label and machine-id section headings, after localization."""

    if not shown:
        return
    product["version"] = shown.get("version")
    if "quality" in shown:
        product["quality"] = shown["quality"]
    product["version_label"] = shown.get("version_label")
    labels = shown.get("section_labels") or []
    sections = product.get("sections") or []
    if len(labels) != len(sections):
        return
    for section, label in zip(sections, labels):
        # A causal-chain heading is always the record's own numbered title;
        # any other section keeps a reviewed Chinese title and is only
        # relabelled while its title is still the machine key.
        if label and (label.startswith("因果链")
                      or _MACHINE_TITLE.fullmatch(str(section.get("title") or ""))):
            section["label"] = label


def _memo_approval(connection: sqlite3.Connection, mission: Mapping[str, Any],
                   company_ref: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Human decision for these exact memo bytes, never publication status."""
    if record.get("mission_version_ref") != mission.get("id"):
        return {"status": "historical", "decision_record_ref": None}
    state = _reader(connection, CoverageMissionAuthority).current_stage_state(
        mission["mission_ref"], company_ref)
    stage = state.get("stages", {}).get("investment_memo") or {}
    record_ref = stage.get("record_ref")
    if stage.get("status") not in {"gate_passed", "gate_failed"} or not record_ref:
        return {"status": "pending_human_decision", "decision_record_ref": None}
    row = connection.execute(
        "SELECT * FROM coverage_mission_stage_records WHERE record_id=?",
        (record_ref,),
    ).fetchone()
    if row is None:
        raise ValueError("memo stage decision record is missing")
    decision = validate_mission_stage_record(json.loads(row["record_json"]))
    columns = {
        "id": "record_id", "mission_version_ref": "mission_version_ref",
        "company_ref": "company_ref", "stage_ref": "stage_ref", "status": "status",
        "actor_ref": "actor_ref", "created_at": "created_at", "content_hash": "content_hash",
    }
    if (any(decision[key] != row[column] for key, column in columns.items())
            or decision["id"] != record_ref
            or decision["mission_version_ref"] != mission["id"]
            or decision["mission_version_hash"] != mission["content_hash"]
            or decision["company_ref"] != company_ref
            or decision["stage_ref"] != "investment_memo"
            or decision["status"] != stage["status"]
            or not decision["actor_ref"].startswith("human:")):
        raise ValueError("memo stage decision authority binding drifted")
    if record["id"] not in (decision.get("evidence_refs") or []):
        return {"status": "pending_human_decision", "decision_record_ref": None,
                "reason": "current stage decision belongs to another memo version"}
    return {
        "status": ("approved" if stage["status"] == "gate_passed" else "rejected"),
        "decision_record_ref": record_ref,
        "actor_ref": decision.get("actor_ref"),
        "decided_at": decision.get("created_at"),
    }
