"""Authenticated Cockpit downloads; render only to a temporary directory."""

from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def export_download(core_db: str | Path, company_ref: str, format: str,
                    *, mission_ref: str, company_label: str | None = None) -> dict[str, Any]:
    if format not in {"html", "xlsx"}:
        raise ValueError("unsupported research export format")
    suffix = hashlib.sha256(company_ref.encode()).hexdigest()[:12]
    label = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", company_label or "").strip(" ._")[:80]
    label = label or f"公司_{suffix}"
    kind = "研究报告" if format == "html" else "财务模型"
    date = datetime.now().astimezone().date().isoformat()
    filename = f"Dalton_{label}_{kind}_{date}.{format}"
    with tempfile.TemporaryDirectory(prefix="dalton-cockpit-export-") as name:
        target = Path(name) / filename
        if format == "html":
            from .research_html_export import export_research_html

            manifest = export_research_html(
                core_db, company_ref, target, mission_ref=mission_ref)
            media_type = "text/html;charset=utf-8"
        else:
            from .fund_xlsx_export import export_company_workbook

            manifest = export_company_workbook(
                core_db, company_ref, target, mission_ref=mission_ref)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        raw = target.read_bytes()
    return {
        "filename": filename,
        "media_type": media_type,
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "manifest": manifest,
    }
