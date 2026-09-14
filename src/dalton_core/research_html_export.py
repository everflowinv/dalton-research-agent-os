"""Deterministic, read-only export of current governed research to safe HTML."""

from __future__ import annotations

import argparse, base64, hashlib, html, json, os, re, sqlite3, tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cockpit_research_library import research_library
from .store import content_hash
from .numeric_display import format_typed_value
from .research_gap_display import display_metadata_text, gap_display_text

_SHA = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpeg"}
_PRODUCT_LABELS = {
    "initial_screen": "初步筛选", "dossier": "公司档案", "debate_map": "争议地图",
    "industry_framework": "行业框架", "investment_memo": "投资备忘录",
    "conviction_call": "投资判断",
}

_APPROVAL_LABELS = {
    "approved": "已通过",
    "rejected": "未通过",
    "pending_human_decision": "等待人工审批",
    "historical": "历史版本，当前审批不适用",
    "not_applicable": "无需人工审批",
    "unknown": "状态未确认",
}
_REASON_LABELS = {
    "not_drafted_this_run": "本轮尚未起草",
}
_METRIC_LABELS = {
    "revenue": "营业收入",
    "revenues": "营业收入",
    "revenue_yoy_growth": "营业收入同比增速",
    "operating_margin": "营业利润率",
    "gross_margin": "毛利率",
    "diluted_eps": "稀释每股收益",
    "free_cash_flow": "自由现金流",
    "cost_structure": "成本结构",
    "unit_economics": "单位经济性",
    "volume": "业务量",
    "price": "实现价格",
    "pricing": "定价",
    "retention": "客户留存",
}

def _metric_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key in _METRIC_LABELS:
        return _METRIC_LABELS[key]
    # Unknown machine keys stay in governed structured data and manifests; the
    # reader-facing chart title must not expose an internal identifier.
    if re.fullmatch(r"[a-z][a-z0-9_]*", key):
        return "其他结构化指标"
    return str(value or "未命名指标")

def _display_reason(value: Any) -> str:
    text = str(value or "")
    return _REASON_LABELS.get(text, text)


def _display_metric_terms(value: Any) -> str:
    return display_metadata_text(value or "暂无可核验内容")


def _period_label(value: Any) -> str:
    # Import lazily so the standalone export remains independent of the HTTP
    # plane during module initialization.
    from .cockpit_plane import claim_period_display_label
    return claim_period_display_label(value) or "期间未注明"


def _number_text(item: Mapping[str, Any],
                 claims: Mapping[str, Mapping[str, Any]]) -> tuple[str, str | None]:
    """Present the exact SEC auto-template as Chinese structured data.

    Other text may be a genuine quotation and remains byte-for-byte visible.
    """
    raw = str(item.get("text") or "")
    claim = claims.get(item.get("claim_version_ref"))
    if not isinstance(claim, Mapping):
        return _display_metric_terms(raw), None
    try:
        from .forecast_reconciliation import (
            ForecastReconciliationValidationError, parse_company_facts_claim,
        )
        parsed = parse_company_facts_claim(claim)
    except (ForecastReconciliationValidationError, KeyError, TypeError, ValueError):
        return _display_metric_terms(raw), None
    amount = lambda value: format_typed_value(
        value, unit="usd", scale="one", currency=parsed["currency"], metric="revenue")
    growth = format_typed_value(
        parsed["growth_percent"], unit="percent", scale="one", metric="revenue_yoy_growth")
    direction = "增长" if not str(parsed["growth_percent"]).startswith("-") else "下降"
    shown = (f'{parsed["entity"]}在{_period_label(claim.get("period"))}披露'
             f'{_metric_label(parsed["label"])}{amount(parsed["current"])}，'
             f'同比{direction}{growth.lstrip("-")}；上年同期{amount(parsed["prior"])}。')
    return shown, raw


def _section_title(value: Any) -> str:
    text = str(value or "未命名章节")
    replacements = {
        "S3 核心 Thesis（简版）": "S3 核心投资逻辑（简版）",
        "S3 核心Thesis（简版）": "S3 核心投资逻辑（简版）",
        "核心 Thesis（简版）": "核心投资逻辑（简版）",
        "核心Thesis（简版）": "核心投资逻辑（简版）",
        "S4 风险与 Anti-thesis（简版）": "S4 风险与反向观点（简版）",
        "S4 风险与Anti-thesis（简版）": "S4 风险与反向观点（简版）",
    }
    return replacements.get(text, text)


class ResearchHtmlExportError(RuntimeError):
    pass


def _atomic_owner_write(path: Path, data: bytes) -> None:
    path = path.expanduser().resolve()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _current_mission(c: sqlite3.Connection, mission_ref: str | None) -> dict[str, Any]:
    sql = "SELECT v.record_json,v.content_hash FROM coverage_mission_pointer p JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id"
    args: tuple[Any, ...] = ()
    if mission_ref:
        sql += " WHERE p.mission_ref=?"
        args = (mission_ref,)
    sql += " ORDER BY p.mission_ref LIMIT 1"
    row = c.execute(sql, args).fetchone()
    if row is None:
        raise ResearchHtmlExportError("current mission was not found")
    record = json.loads(row["record_json"])
    expected = content_hash({k: v for k, v in record.items() if k != "content_hash"})
    if (
        record.get("content_hash") != row["content_hash"]
        or expected != row["content_hash"]
    ):
        raise ResearchHtmlExportError("current mission integrity check failed")
    return record


def _typed_claims(
    connection: sqlite3.Connection, library: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    refs = set()
    for product in library.get("products") or []:
        for section in product.get("sections") or []:
            for number in section.get("numbers") or []:
                if isinstance(number, Mapping) and isinstance(
                    number.get("claim_version_ref"), str
                ):
                    refs.add(number["claim_version_ref"])
    claims = {}
    if not refs or not _has_table(connection, "claim_versions"):
        return claims
    for ref in sorted(refs):
        row = connection.execute(
            "SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?",
            (ref,),
        ).fetchone()
        if row is None:
            continue
        claim = json.loads(row["claim_json"])
        latest = connection.execute(
            "SELECT claim_version_id FROM claim_versions WHERE claim_ref=? "
            "ORDER BY version_number DESC,claim_version_id DESC LIMIT 1",
            (claim.get("claim_ref"),),
        ).fetchone()
        adjudication = (
            connection.execute(
                "SELECT adjudicated_status FROM adjudication_versions WHERE claim_version_id=? "
                "ORDER BY version_number DESC,adjudication_version_id DESC LIMIT 1",
                (ref,),
            ).fetchone()
            if _has_table(connection, "adjudication_versions")
            else None
        )
        status = adjudication[0] if adjudication is not None else "proposed"
        retired = False
        if _has_table(connection, "claim_retirement_decisions") and _has_table(
            connection, "claim_retirement_challenges"
        ):
            retired = (
                connection.execute(
                    "SELECT 1 FROM claim_retirement_decisions d "
                    "JOIN claim_retirement_challenges c ON c.challenge_id=d.challenge_ref "
                    "WHERE c.claim_version_ref=? AND d.decision='retired' LIMIT 1",
                    (ref,),
                ).fetchone()
                is not None
            )
        if (
            claim.get("id") == ref
            and claim.get("content_hash") == row["content_hash"]
            and content_hash({k: v for k, v in claim.items() if k != "content_hash"})
            == row["content_hash"]
            and claim.get("claim_kind") == "quantitative"
            and latest is not None
            and latest[0] == ref
            and status not in {"rejected", "retired", "retracted", "superseded"}
            and not retired
        ):
            claims[ref] = {**claim, "authority_status": status}
    return claims


def _chart(
    numbers: Sequence[Mapping[str, Any]],
    claims: Mapping[str, Mapping[str, Any]],
    chart_id: str,
    *,
    subject_ref: str,
) -> str:
    series = []
    for item in numbers:
        if not isinstance(item, Mapping):
            continue
        claim = claims.get(item.get("claim_version_ref"))
        if claim is None or claim.get("subject_ref") != subject_ref:
            continue
        try:
            value = float(claim["value"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not __import__("math").isfinite(value):
            continue
        period = claim.get("period")
        if not isinstance(period, str) or not period:
            continue
        grain = (
            "quarter"
            if re.fullmatch(r"(?:FY)?\d{4}Q[1-4]", period, re.I)
            else (
                "year"
                if re.fullmatch(r"(?:FY)?\d{2,4}[AE]?", period, re.I)
                else None
            )
        )
        basis = claim.get("basis")
        estimate_kind = (
            "estimate"
            if isinstance(basis, str)
            and basis.lower() in {"estimate", "forecast", "consensus-estimate"}
            else (
                "actual"
                if isinstance(basis, str)
                and basis.lower()
                in {"actual", "reported", "official-filing", "official-filing-xbrl"}
                else None
            )
        )
        key = (
            claim.get("subject_ref"),
            claim.get("metric_or_aspect"),
            claim.get("unit"),
            claim.get("scale"),
            claim.get("currency"),
            grain,
        )
        if (
            not all(isinstance(part, str) and part for part in key[:3])
            or grain is None
            or estimate_kind is None
        ):
            continue
        series.append((item, claim, value, key, estimate_kind))
    if (
        len(series) < 2
        or len({row[3] for row in series}) != 1
        or len({(row[1]["id"], row[1]["period"]) for row in series}) != len(series)
        or len({row[1]["period"] for row in series}) != len(series)
    ):
        return '<p class="unavailable">暂无可比图表：当前没有口径一致且期间不同的结构化数据序列。</p>'
    values = [row[2] for row in series]
    low, high = min(min(values), 0.0), max(max(values), 0.0)
    span = high - low or 1.0
    axis = 170 + ((0.0 - low) / span * 390)
    rows = []
    for index, (item, claim, value, _, estimate_kind) in enumerate(series):
        y = 20 + index * 58
        point = 170 + ((value - low) / span * 390)
        x, width = min(axis, point), abs(point - axis)
        rows.append(
            f'<text x="0" y="{y + 14}" class="sl">{_esc(claim.get("period") or item.get("period") or "未知期间")} · {_esc({"actual": "已披露", "estimate": "预测"}[estimate_kind])}</text>'
            f'<line x1="{axis:.2f}" x2="{axis:.2f}" y1="{y-2}" y2="{y+24}" class="axis"/>'
            f'<rect class="{_esc(estimate_kind)}" x="{x:.2f}" y="{y}" width="{width:.2f}" height="20"/>'
            f'<text x="570" y="{y+15}" class="sv">{_esc(format_typed_value(claim["value"], unit=claim["unit"], scale=claim.get("scale"), currency=claim.get("currency"), metric=claim.get("metric_or_aspect") or ""))}</text>'
        )
    height = 58 * len(series) + 35
    return (
        f'<figure><svg role="img" aria-labelledby="{_esc(chart_id)}-title" '
        f'viewBox="0 0 700 {height}"><title id="{_esc(chart_id)}-title">'
        f'结构化数据序列：{_esc(_metric_label(series[0][3][1]))}</title>{"".join(rows)}</svg>'
        "<figcaption>已披露值与预测值分别标注；所有数值来自哈希已核验、公司归属一致且指标、单位、刻度和币种相同的定量结论版本。</figcaption></figure>"
    )


def _source_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        fields = [
            value.get(key)
            for key in (
                "ref",
                "claim_version_ref",
                "source_ref",
                "document_ref",
                "citation",
            )
        ]
        return (
            " · ".join(str(item) for item in fields if isinstance(item, str) and item)
            or "结构化来源不可用"
        )
    return "来源不可用"


def _gap_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping):
        fields = [value.get(key) for key in ("reason", "code", "detail")]
        text = (
            " · ".join(str(item) for item in fields if isinstance(item, str) and item)
            or "结构化待补项"
        )
    else:
        text = "未说明的待补项"
    text = text.replace("the model call did not succeed", "模型调用未成功")
    # These occur inside a known missing-information field, not in quoted
    # source prose. Keep product and company names untouched.
    for source, shown in {
        "utilization": "利用率", "margin": "利润率",
        "segment profit": "分部利润", "bookings": "订单额",
    }.items():
        text = re.sub(rf"(?<![A-Za-z_]){re.escape(source)}(?![A-Za-z_])", shown,
                      text, flags=re.IGNORECASE)
    return _display_metric_terms(gap_display_text(_display_reason(text)))


def _assets(asset_manifest: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if asset_manifest is None:
        return []
    if (
        not isinstance(asset_manifest, Mapping)
        or set(asset_manifest) != {"schema_version", "assets"}
        or asset_manifest["schema_version"] != "0.1"
        or not isinstance(asset_manifest["assets"], list)
    ):
        raise ResearchHtmlExportError("asset manifest has an invalid closed shape")
    out = []
    for item in asset_manifest["assets"]:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "sha256",
            "media_type",
            "caption",
            "source_refs",
        }:
            raise ResearchHtmlExportError("asset has an invalid closed shape")
        path = Path(item["path"]).expanduser().resolve()
        media = item["media_type"]
        if media not in _IMAGE_TYPES or path.suffix.lower() not in {
            ".png",
            ".jpg",
            ".jpeg",
        }:
            raise ResearchHtmlExportError("asset media type is not PNG/JPEG")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item["sha256"] or not _SHA.fullmatch(digest):
            raise ResearchHtmlExportError("asset hash mismatch")
        if media == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ResearchHtmlExportError("asset bytes do not match PNG media type")
        if media in {"image/jpeg", "image/jpg"} and not (
            raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")
        ):
            raise ResearchHtmlExportError("asset bytes do not match JPEG media type")
        refs = item["source_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(x, str)
                or re.fullmatch(r"[a-z][a-z0-9_-]*:[^\s/?#]+", x) is None
                for x in refs
            )
        ):
            raise ResearchHtmlExportError("asset needs source refs")
        out.append(
            {
                "data": f"data:{media};base64,{base64.b64encode(raw).decode()}",
                "caption": str(item["caption"]),
                "refs": ", ".join(refs),
                "sha256": digest,
                "media_type": media,
                "source_refs": list(refs),
            }
        )
    return out


def render_research_html(
    library: Mapping[str, Any],
    *,
    mission: Mapping[str, Any],
    claims: Mapping[str, Mapping[str, Any]] | None = None,
    assets: Sequence[Mapping[str, str]] = (),
) -> str:
    products = library.get("products") or []
    company_ref = library["company_ref"]
    member = next(
        (
            row
            for row in mission.get("universe", [])
            if row.get("company_ref") == company_ref
        ),
        {},
    )
    company = (
        " ".join(str(member.get(key)) for key in ("name", "ticker") if member.get(key))
        or company_ref
    )
    claims = claims or {}
    toc = []
    bodies = []
    for pi, product in enumerate(products, 1):
        anchor = f"product-{pi}"
        product_label = _PRODUCT_LABELS.get(
            str(product.get("kind")), product.get("label") or product.get("kind"))
        toc.append(
            f'<li><a href="#{anchor}">{_esc(product_label)}</a></li>'
        )
        status = product.get("status", "unknown")
        binding = product.get("mission_binding", "unknown")
        status_label = {"available": "已发布", "missing": "尚未发布", "invalid": "记录无效"}.get(status, "状态未确认")
        binding_label = {"current": "当前研究任务", "historical": "历史研究任务",
                         "unknown": "研究任务绑定未确认"}.get(binding, "研究任务绑定未确认")
        identity = (f'版本 {_esc(product.get("version_ref") or "未知")} · '
                    f'原文哈希 {_esc(product.get("content_hash") or "未知")}')
        localization = product.get("localization") or {}
        if localization:
            identity += f' · 中文呈现 {_esc(localization.get("content_hash") or "未知")}'
        head = f'<section id="{anchor}"><h2>{_esc(product_label)}</h2><p class="meta">{_esc(status_label)} · {_esc(binding_label)}</p><details class="identity"><summary>查看版本与完整哈希</summary><code>{identity}</code></details>'
        completeness = product.get("completeness")
        if completeness:
            partial = completeness.get("status") == "partial"
            label = "部分档案，仍在起草" if partial else "所有单元已起草"
            head += (
                f'<p class="{"unavailable" if partial else "meta"}">'
                f'{label}：{_esc(completeness.get("drafted_units"))}/'
                f'{_esc(completeness.get("total_units"))} 单元。'
                '起草进度不代表资料已更新或研究质量已验收。</p>'
            )
        approval = product.get("approval") or {"status": "unknown"}
        approval_label = _APPROVAL_LABELS.get(
            approval.get("status", "unknown"), "状态未确认")
        approval_detail = " · ".join(str(x) for x in (approval.get("decision_record_ref"),
                                    approval.get("actor_ref"), approval.get("decided_at")) if x)
        head += f'<p class="approval">人工审批：{_esc(approval_label)}{(" · " + _esc(approval_detail)) if approval_detail else ""}</p>'
        if product.get("reason"):
            head += f'<p class="unavailable">{_esc(_display_reason(product["reason"]))}</p>'
        if product.get("gaps"):
            head += f'<p class="gaps">产物待补资料：{_esc("；".join(_gap_text(g) for g in product["gaps"]))}</p>'
        chunks = []
        for si, section in enumerate(product.get("sections") or [], 1):
            nums = section.get("numbers") or []
            refs = section.get("sources") or []
            number_rows = [(_period_label(n.get("period")), *_number_text(n, claims))
                           for n in nums if isinstance(n, Mapping)]
            table = "".join(f'<tr><td>{_esc(period)}</td><td>{_esc(shown)}</td></tr>'
                            for period, shown, _ in number_rows)
            technical_refs = list(refs)
            for number in nums:
                if not isinstance(number, Mapping):
                    continue
                ref = number.get("claim_version_ref") or (number.get("cell") or {}).get("ref")
                if ref and ref not in technical_refs:
                    technical_refs.append(ref)
            original_templates = [raw for _, _, raw in number_rows if raw]
            technical_text = ", ".join(_source_text(ref) for ref in technical_refs) if technical_refs else "暂无来源"
            if original_templates:
                technical_text += "\n结构化记录原文：\n" + "\n".join(original_templates)
            chunks.append(
                f'<article><h3>{_esc(_section_title(section.get("title")))}</h3><p class="prose">{_esc(_display_metric_terms(section.get("body") or "暂无可核验内容"))}</p>{_chart(nums, claims, f"chart-{pi}-{si}", subject_ref=product.get("subject_ref")) if nums else ""}{("<div class=\"tablewrap\"><table><thead><tr><th>期间</th><th>结构化数据</th></tr></thead><tbody>"+table+"</tbody></table></div>") if table else ""}<details class="refs"><summary>技术详情与来源（{len(technical_refs)}）</summary><code>{_esc(technical_text)}</code></details><p class="gaps">待补资料：{_esc("；".join(_gap_text(gap) for gap in (section.get("gaps") or [])) or "当前未记录待补项")}</p></article>'
            )
        if not chunks:
            chunks = [
                f'<p class="unavailable">{_esc(product.get("display_reason") or "暂无可阅读的当前章节。")}</p>'
            ]
        bodies.append(head + "".join(chunks) + "</section>")
    figs = "".join(
        f'<figure><img src="{a["data"]}" alt="{_esc(a["caption"])}"><figcaption>{_esc(a["caption"])} · {_esc(a["refs"])} · 用户提供的本地图表 · sha256 {_esc(a["sha256"])}</figcaption></figure>'
        for a in assets
    )
    css = """body{margin:0;background:#f5f5f7;color:#1d1d1f;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:980px;margin:auto;padding:48px 24px}h1{font-size:42px}h2{border-top:1px solid #ccc;padding-top:32px}article{background:white;border-radius:18px;padding:24px;margin:18px 0;box-shadow:0 2px 18px #0001}.meta,.refs,figcaption{color:#666;font-size:13px;overflow-wrap:anywhere}.approval{font-weight:700}.prose{white-space:pre-wrap}.unavailable,.gaps{background:#fff4ce;padding:10px;border-radius:8px}.tablewrap{overflow:auto}table{border-collapse:collapse;min-width:620px;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}svg{width:100%;height:auto}rect.actual{fill:#147ce5}rect.estimate{fill:#8e8e93}.sl,.sv{font-size:12px;fill:#333}img{max-width:100%;height:auto}@media(max-width:520px){main{padding:24px 14px}h1{font-size:32px}article{padding:16px}}@media print{body{background:#fff}article{box-shadow:none;border:1px solid #ddd;break-inside:avoid}nav{break-after:page}}"""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + _esc(company)
        + " 研究报告</title><style>"
        + css
        + "</style></head><body><main><header><p>Dalton 研究报告</p><h1>"
        + _esc(company)
        + "</h1><p>"
        + _esc(mission.get("title") or "研究报告")
        + '</p><details class="identity"><summary>查看研究任务版本</summary><code>'
        + _esc(mission["id"])
        + " · "
        + _esc(mission["content_hash"])
        + '</code></details></header><nav aria-label="目录"><h2>目录</h2><ol>'
        + "".join(toc)
        + "</ol></nav>"
        + "".join(bodies)
        + (
            "<section><h2>图表</h2>" + figs + "</section>"
            if figs
            else '<section><h2>图表</h2><p class="unavailable">未提供带本地哈希和来源绑定的图表。</p></section>'
        )
        + "</main></body></html>\n"
    )


def _manifest_claims(
    library: Mapping[str, Any], claims: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, str]]:
    """List only claim versions eligible for the exact product that cites them."""
    refs: dict[str, str] = {}
    for product in library.get("products") or []:
        subject_ref = product.get("subject_ref")
        for section in product.get("sections") or []:
            for number in section.get("numbers") or []:
                if not isinstance(number, Mapping):
                    continue
                ref = number.get("claim_version_ref")
                claim = claims.get(ref)
                if claim is not None and claim.get("subject_ref") == subject_ref:
                    refs[ref] = claim["content_hash"]
    return [
        {"version_ref": ref, "content_hash": refs[ref]}
        for ref in sorted(refs)
    ]


def export_research_html(
    core_db: str | Path,
    company_ref: str,
    output: str | Path,
    *,
    mission_ref: str | None = None,
    asset_manifest: str | Path | None = None,
    manifest_output: str | Path | None = None,
) -> dict[str, Any]:
    db = Path(core_db).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    manifest_target = (
        Path(manifest_output).expanduser().resolve()
        if manifest_output is not None
        else target.with_suffix(target.suffix + ".manifest.json")
    )
    manifest_path = (
        None if asset_manifest is None else Path(asset_manifest).expanduser().resolve()
    )
    protected = {db}
    if manifest_path is not None:
        protected.add(manifest_path)
    if target == manifest_target or target in protected or manifest_target in protected:
        raise ResearchHtmlExportError("output paths collide with an input or each other")
    uri = db.as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        mission = _current_mission(c, mission_ref)
        library = research_library(c, mission, company_ref)
        claims = _typed_claims(c, library)
        c.rollback()
    finally:
        c.close()
    manifest = None if manifest_path is None else json.loads(manifest_path.read_text())
    if manifest is not None:
        asset_paths = {
            Path(item["path"]).expanduser().resolve()
            for item in manifest.get("assets", [])
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        if target in asset_paths or manifest_target in asset_paths:
            raise ResearchHtmlExportError("output paths collide with an input or each other")
    assets = _assets(manifest)
    page = render_research_html(library, mission=mission, claims=claims, assets=assets)
    raw = page.encode()
    result = {
        "schema_version": "0.2",
        "company_ref": company_ref,
        "mission_version_ref": mission["id"],
        "mission_version_hash": mission["content_hash"],
        "html_file": target.name,
        "html_sha256": hashlib.sha256(raw).hexdigest(),
        "source_claims": _manifest_claims(library, claims),
        "assets": [
            {
                "sha256": asset["sha256"],
                "media_type": asset["media_type"],
                "source_refs": asset["source_refs"],
            }
            for asset in assets
        ],
        "product_versions": [
            {
                "kind": p["kind"],
                "status": p["status"],
                "version_ref": p.get("version_ref"),
                "content_hash": p.get("content_hash"),
                "mission_binding": p.get("mission_binding", "unknown"),
                "approval": (p.get("approval") or {}).get("status", "unknown"),
                **({"localization": p["localization"]} if p.get("localization") else {}),
                **({"completeness": p["completeness"]}
                   if p.get("completeness") is not None else {}),
            }
            for p in library["products"]
        ],
    }
    manifest_bytes = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_owner_write(target, raw)
    _atomic_owner_write(manifest_target, manifest_bytes)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-db", required=True)
    ap.add_argument("--company-ref", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mission-ref")
    ap.add_argument("--asset-manifest")
    ap.add_argument("--manifest-output")
    print(json.dumps(export_research_html(**vars(ap.parse_args(argv))), sort_keys=True))


if __name__ == "__main__":
    main()
