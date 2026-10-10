"""The cockpit's 「基金 Excel 模板」 card (vision-gap #4, 2026-10-07).

The page side of ``fund_xlsx_template_registry``.  Reads open the Core
read-only and work on a Core that has never seen the registry (everything
then reads as the built-in template); every write goes back out through the
writer as the owner's ``human:`` principal (ADR-0006: the control process
holds no Core write handle), and each one leaves an append-only receipt.

- ``GET /v1/cockpit/fund-templates``: the registered templates, which company
  exports with which template and why, the industry and workspace defaults,
  and the receipts;
- ``POST /v1/cockpit/fund_template_register``: upload a workbook.  The
  workbook is parsed here, in a temporary directory, by the redacting importer
  (``fund_xlsx_template_import``); only the presentation contract -- never a
  value, formula or name from the workbook -- and the workbook's hash go to
  the writer.  The new version is inert until chosen;
- ``POST /v1/cockpit/fund_template_select``: choose (or withdraw) the template
  for a company, an industry classification, or the whole workspace.

Re-export is the existing download (``GET /v1/cockpit/export``), which now
applies the chosen template; ``template=<version ref>`` previews another one
for that download only.
"""

from __future__ import annotations

import base64
import binascii
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .fund_xlsx_template_registry import (
    BASIS_LABELS,
    BUILTIN_VERSION_REF,
    DEFAULT_SCOPE_REF,
    INDUSTRY_LABELS,
    VALUATION_BLOCKS,
    company_classification,
    resolve_template,
    selection_receipts,
    selection_state,
    template_versions,
)

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
#: The JSON body carrying a base64 workbook (4/3) plus the form fields.
MAX_UPLOAD_BODY_BYTES = MAX_UPLOAD_BYTES * 4 // 3 + 64 * 1024
SCOPE_LABELS = {"company": "公司", "industry": "行业分类", "default": "全局默认"}
OUTCOME_LABELS = {"applied": "已生效", "unchanged": "本来就是这个模板",
                  "was_not_selected": "本来就没有指定"}
BLOCK_LABELS = {"diluted_eps": "每股收益", "revenue": "收入", "operating_income": "营业利润",
                "net_income": "净利润"}
_IMPORT_REASONS = (
    ("source workbook must be a direct regular file", "上传的文件无法读取"),
    ("too large", "文件超过大小上限"),
    ("not a valid XLSX archive", "这不是一个有效的 .xlsx 文件"),
    ("unsupported external or executable content", "工作簿含外部链接或宏，不能作为模板"),
    ("workbook archive", "工作簿压缩结构不安全"),
    ("bound workbook sheet is missing", "填写的工作表名称在工作簿里找不到"),
    ("named styles", "工作簿缺少模板所需的命名样式（unit_header、period_header、"
                     "major_section 等，见参考模板）"),
    ("grid layout is unsupported", "财务表与驱动表需要冻结在 E2 并隐藏网格线"),
    ("row heights", "工作表默认行高不受支持"),
    ("column width", "列宽不受支持"),
    ("font", "字体不在允许范围（Arial / Calibri，6–24 号）"),
    ("fill", "只支持纯色填充"),
    ("border", "只支持细线（hair）边框"),
    ("alignment", "对齐方式不受支持"),
    ("number format", "数字格式不在允许范围"),
    ("color", "颜色需要是明确的 RGB 值"),
    ("sheet role binding", "请分别填写估值表、财务表、驱动表三个不同的工作表名"),
    ("template slug", "模板代号只能用小写字母、数字和连字符"),
)


def _cockpit() -> Any:
    from . import cockpit_plane

    return cockpit_plane


def _template_label(record: Mapping[str, Any]) -> str:
    return f"{record['display_name']}（第 {record['version']} 版）"


def _who(actor: Any) -> str:
    return "你" if str(actor or "").startswith("human:") else "系统"


def templates_view(plane: Any) -> dict[str, Any]:
    cockpit = _cockpit()
    with plane._core() as core:
        versions = template_versions(core)
        state = selection_state(core)
        receipts = selection_receipts(core, limit=40)
        try:
            mission = plane._mission(core)
        except cockpit.CockpitMissionMissing:
            mission = None
        members = {} if mission is None else plane._members(mission)
        companies = []
        for ref in members:
            classification = company_classification(core, ref)
            resolved = resolve_template(core, ref, classification=classification)
            chosen = state["companies"].get(ref)
            companies.append({
                "company_ref": ref, "label": plane._label(members, ref),
                "classification": classification,
                "classification_label": INDUSTRY_LABELS.get(classification or "", "未分类"),
                "template_version_ref": resolved["template"]["id"],
                "template_label": _template_label(resolved["template"]),
                "basis": resolved["basis"], "basis_label": BASIS_LABELS[resolved["basis"]],
                "company_choice_ref": None if chosen is None else chosen["template_version_ref"],
            })
    by_ref = {record["id"]: record for record in versions}
    latest_by_slug: dict[str, str] = {}
    for record in versions:
        latest_by_slug[record["template_slug"]] = record["id"]
    used_by: dict[str, list[str]] = {}
    for company in companies:
        used_by.setdefault(company["template_version_ref"], []).append(company["label"])
    default_receipt = state["default"]
    default_ref = (BUILTIN_VERSION_REF if default_receipt is None
                   else default_receipt["template_version_ref"])
    templates = []
    for record in versions:
        mapping = record["mapping"]
        templates.append({
            "version_ref": record["id"], "version_hash": record["content_hash"],
            "slug": record["template_slug"], "version": record["version"],
            "label": _template_label(record), "display_name": record["display_name"],
            "description": record.get("description"),
            "origin": record["origin"],
            "origin_label": "系统内置" if record["origin"] == "builtin" else "你上传登记",
            "industries": [{"value": item, "label": INDUSTRY_LABELS[item]}
                           for item in record.get("industries") or []],
            "created_at": record["created_at"], "who": _who(record["actor_ref"]),
            "reason": record.get("reason"),
            "source_filename": (record.get("source") or {}).get("filename"),
            "sheet_names": mapping["sheet_names"],
            "valuation_blocks": [BLOCK_LABELS[block]
                                 for block in mapping["valuation_annual_blocks"]],
            "used_by": used_by.get(record["id"], []),
            "is_default": record["id"] == default_ref,
            "is_latest": latest_by_slug[record["template_slug"]] == record["id"],
            "technical": {"version_ref": record["id"], "content_hash": record["content_hash"],
                          "style_ref": record["style_ref"], "style_hash": record["style_hash"],
                          "workbook_sha256": (record.get("source") or {}).get("workbook_sha256"),
                          "mapping": mapping},
        })
    industries = []
    for value, label in INDUSTRY_LABELS.items():
        receipt = state["industries"].get(value)
        template = None if receipt is None else by_ref.get(receipt["template_version_ref"])
        industries.append({"value": value, "label": label,
                           "template_version_ref": None if template is None else template["id"],
                           "template_label": None if template is None else _template_label(template)})
    return {
        "enabled": True,
        "as_of": cockpit._iso(plane.clock()),
        "templates": templates,
        "companies": companies,
        "industries": industries,
        "default": {"template_version_ref": default_ref,
                    "template_label": _template_label(by_ref[default_ref]),
                    "chosen_by_owner": default_receipt is not None},
        "receipts": [_receipt_view(item, by_ref, members, plane) for item in receipts],
        "limits": {"max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024)},
        "mission_ref": None if mission is None else mission["mission_ref"],
    }


def _receipt_view(receipt: Mapping[str, Any], by_ref: Mapping[str, Mapping[str, Any]],
                  members: Mapping[str, Any], plane: Any) -> dict[str, Any]:
    kind, ref = receipt.get("scope_kind"), receipt.get("scope_ref") or ""
    target = {"company": plane._label(members, ref) if ref in members else ref.rsplit(":", 1)[-1],
              "industry": INDUSTRY_LABELS.get(ref, ref), "default": "全局默认"}.get(kind, ref)
    template = by_ref.get(str(receipt.get("template_version_ref")))
    return {
        "id": receipt.get("id"), "at": receipt.get("created_at"),
        "scope_label": SCOPE_LABELS.get(kind, kind), "target": target,
        "action_label": "指定模板" if receipt.get("action") == "select" else "取消指定",
        "template_label": None if template is None else _template_label(template),
        "outcome_label": OUTCOME_LABELS.get(receipt.get("outcome"), receipt.get("outcome")),
        "reason": receipt.get("reason"), "who": _who(receipt.get("actor_ref")),
        "technical": {"receipt_ref": receipt.get("id"), "scope_ref": ref,
                      "template_version_ref": receipt.get("template_version_ref")},
    }


def _readable_import_error(message: str) -> str:
    for needle, text in _IMPORT_REASONS:
        if needle in message:
            return text
    return f"工作簿不符合模板要求（{message}）"


def _sheet_names(value: Any, name: str, *, required: bool) -> dict[str, str] | None:
    cockpit = _cockpit()
    if value is None and not required:
        return None
    if not isinstance(value, Mapping):
        raise cockpit.CockpitError("请填写估值表、财务表、驱动表三个工作表名")
    out = {}
    for role in ("valuation", "financials", "driver"):
        text = value.get(role)
        if not isinstance(text, str) or not text.strip():
            if required:
                raise cockpit.CockpitError("请填写估值表、财务表、驱动表三个工作表名")
            continue
        out[role] = text.strip()[:31]
    return out or None


def register(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """「上传并登记模板」: import in a temporary directory, register via the writer."""

    cockpit = _cockpit()
    from .cockpit_owner_levers import _reason, _request_id
    from .fund_xlsx_template_import import (
        FundXlsxTemplateImportError, import_fund_xlsx_template_candidate,
    )

    reason = _reason(value)
    request_id = _request_id(value)
    slug = str(value.get("template_slug") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?", slug):
        raise cockpit.CockpitError("模板代号只能用小写字母、数字和连字符，例如 owner-tmt")
    display_name = str(value.get("display_name") or "").strip()
    if not display_name or len(display_name) > 80:
        raise cockpit.CockpitError("请给模板起一个名字（80 字以内）")
    filename = Path(str(value.get("filename") or "template.xlsx")).name[:160]
    if not filename.lower().endswith(".xlsx"):
        raise cockpit.CockpitError("请上传 .xlsx 文件（不支持 .xls 或带宏的 .xlsm）")
    try:
        raw = base64.b64decode(str(value.get("content_base64") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise cockpit.CockpitError("上传的文件内容无法读取，请重新选择文件") from exc
    if not raw:
        raise cockpit.CockpitError("请选择要上传的工作簿")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise cockpit.CockpitError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限")
    sheets = _sheet_names(value.get("sheets"), "sheets", required=True)
    output_names = _sheet_names(value.get("output_sheet_names"), "output_sheet_names",
                                required=False)
    industries = value.get("industries") or []
    if not isinstance(industries, list) or not all(item in INDUSTRY_LABELS for item in industries):
        raise cockpit.CockpitError("适用行业只能从列出的五类里选")
    with tempfile.TemporaryDirectory(prefix="dalton-fund-template-") as name:
        path = Path(name).resolve() / "upload.xlsx"
        path.write_bytes(raw)
        try:
            candidate = import_fund_xlsx_template_candidate(
                path, sheets=sheets, template_slug=slug)
        except FundXlsxTemplateImportError as exc:
            raise cockpit.CockpitError(
                "这个工作簿不能登记为模板：" + _readable_import_error(str(exc))) from exc
    params: dict[str, Any] = {
        "candidate": candidate, "display_name": display_name,
        "description": str(value.get("description") or "").strip()[:600] or None,
        "industries": sorted(set(industries)), "source_filename": filename,
        "sheet_bindings": sheets, "reason": reason,
        "idempotency_key": f"cockpit-fund-template:{request_id}",
    }
    if output_names:
        params["mapping"] = {"sheet_names": output_names}
    result = plane._governance(login, "register_fund_xlsx_template", params,
                               failure="模板没有登记")
    template = result.get("template") or {}
    plane.journal.record_event(
        kind="lever", title=f"登记了基金 Excel 模板：{display_name}（第 {template.get('version')} 版）",
        detail=reason, login=login,
        refs={"template_version_ref": template.get("id"),
              "workbook_sha256": candidate["source"]["workbook_sha256"]})
    return {"status": result.get("status"), "template_version_ref": template.get("id"),
            "version": template.get("version"),
            "message": (f"已登记「{display_name}」第 {template.get('version')} 版。"
                        "登记本身不改变任何公司的导出；在下方为公司、行业或全局选择它之后才会使用。")}


def select(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """「设为……的模板」 / 「取消指定」."""

    cockpit = _cockpit()
    from .cockpit_owner_levers import _reason, _request_id

    kind = value.get("scope_kind")
    if kind not in SCOPE_LABELS:
        raise cockpit.CockpitError("请选择是给公司、行业分类还是全局指定模板")
    scope_ref = DEFAULT_SCOPE_REF if kind == "default" else str(value.get("scope_ref") or "").strip()
    if not scope_ref:
        raise cockpit.CockpitError("请选择公司或行业分类")
    if kind == "industry" and scope_ref not in INDUSTRY_LABELS:
        raise cockpit.CockpitError("行业分类只能从列出的五类里选")
    params: dict[str, Any] = {"scope_kind": kind, "scope_ref": scope_ref,
                              "reason": _reason(value),
                              "idempotency_key": f"cockpit-fund-template-select:{_request_id(value)}"}
    version_ref = value.get("template_version_ref")
    if version_ref:
        params["template_version_ref"] = str(version_ref)
        params["template_version_hash"] = cockpit._sha(value.get("template_version_hash"),
                                                       "template_version_hash")
    result = plane._governance(login, "select_fund_xlsx_template", params,
                               failure="模板选择没有生效")
    receipt = result.get("receipt") or {}
    title = ("指定了基金 Excel 模板" if version_ref else "取消了指定的基金 Excel 模板") + \
        f"：{SCOPE_LABELS[kind]} {scope_ref.rsplit(':', 1)[-1] if kind == 'company' else INDUSTRY_LABELS.get(scope_ref, '全局')}"
    plane.journal.record_event(kind="lever", title=title, detail=params["reason"], login=login,
                               refs={"receipt_ref": receipt.get("id"),
                                     "template_version_ref": version_ref})
    return {"status": result.get("status"), "receipt": receipt,
            "message": OUTCOME_LABELS.get(receipt.get("outcome"), "已记录")}


__all__ = ["MAX_UPLOAD_BODY_BYTES", "MAX_UPLOAD_BYTES", "register", "select", "templates_view"]
