"""Deterministic, read-only export of current governed research to safe HTML."""
from __future__ import annotations

import argparse, base64, hashlib, html, json, re, sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cockpit_research_library import research_library
from .store import content_hash

_SHA = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpeg"}

class ResearchHtmlExportError(RuntimeError): pass

def _esc(value: Any) -> str: return html.escape(str(value), quote=True)
def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def _current_mission(c: sqlite3.Connection, mission_ref: str | None) -> dict[str, Any]:
    sql = "SELECT v.record_json,v.content_hash FROM coverage_mission_pointer p JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id"
    args: tuple[Any, ...] = ()
    if mission_ref:
        sql += " WHERE p.mission_ref=?"; args = (mission_ref,)
    sql += " ORDER BY p.mission_ref LIMIT 1"
    row = c.execute(sql, args).fetchone()
    if row is None: raise ResearchHtmlExportError("current mission was not found")
    record = json.loads(row["record_json"])
    expected = content_hash({k:v for k,v in record.items() if k != "content_hash"})
    if record.get("content_hash") != row["content_hash"] or expected != row["content_hash"]:
        raise ResearchHtmlExportError("current mission integrity check failed")
    return record

def _typed_claims(connection: sqlite3.Connection, library: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    refs = set()
    for product in library.get("products") or []:
        for section in product.get("sections") or []:
            for number in section.get("numbers") or []:
                if isinstance(number, Mapping) and isinstance(number.get("claim_version_ref"), str):
                    refs.add(number["claim_version_ref"])
    claims = {}
    if not refs or not _has_table(connection, "claim_versions"):
        return claims
    for ref in sorted(refs):
        row = connection.execute(
            "SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?", (ref,)
        ).fetchone()
        if row is None:
            continue
        claim = json.loads(row["claim_json"])
        latest = connection.execute(
            "SELECT claim_version_id FROM claim_versions WHERE claim_ref=? "
            "ORDER BY version_number DESC,claim_version_id DESC LIMIT 1", (claim.get("claim_ref"),)
        ).fetchone()
        adjudication = connection.execute(
            "SELECT adjudicated_status FROM adjudication_versions WHERE claim_version_id=? "
            "ORDER BY version_number DESC,adjudication_version_id DESC LIMIT 1", (ref,)
        ).fetchone() if _has_table(connection, "adjudication_versions") else None
        status = adjudication[0] if adjudication is not None else "proposed"
        if (claim.get("id") == ref
                and claim.get("content_hash") == row["content_hash"]
                and content_hash({k: v for k, v in claim.items() if k != "content_hash"}) == row["content_hash"]
                and claim.get("claim_kind") == "quantitative"
                and latest is not None and latest[0] == ref
                and status not in {"rejected", "retired", "superseded"}):
            claims[ref] = {**claim, "authority_status": status}
    return claims


def _chart(numbers: Sequence[Mapping[str, Any]], claims: Mapping[str, Mapping[str, Any]], chart_id: str) -> str:
    series = []
    for item in numbers:
        if not isinstance(item, Mapping):
            continue
        claim = claims.get(item.get("claim_version_ref"))
        if claim is None:
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
        grain = "quarter" if re.fullmatch(r"(?:FY)?\d{4}Q[1-4]", period, re.I) else (
            "year" if re.fullmatch(r"FY?\d{2,4}[AE]?", period, re.I) else None)
        basis = claim.get("basis")
        estimate_kind = "estimate" if isinstance(basis, str) and basis.lower() in {
            "estimate", "forecast", "consensus-estimate"} else (
            "actual" if isinstance(basis, str) and basis.lower() in {
                "actual", "reported", "official-filing", "official-filing-xbrl"} else None)
        key = (claim.get("subject_ref"), claim.get("metric_or_aspect"), basis,
               claim.get("unit"), claim.get("scale"), claim.get("currency"), grain,
               estimate_kind)
        if not all(isinstance(part, str) and part for part in key[:4]) or grain is None or estimate_kind is None:
            continue
        series.append((item, claim, value, key))
    if (len(series) < 2 or len({row[3] for row in series}) != 1
            or len({(row[1]["id"], row[1]["period"]) for row in series}) != len(series)
            or len({row[1]["period"] for row in series}) != len(series)):
        return '<p class="unavailable">Chart unavailable: no comparable distinct-period typed Claim series.</p>'
    values = [row[2] for row in series]
    low, high = min(min(values), 0.0), max(max(values), 0.0)
    span = high - low or 1.0
    axis = 170 + ((0.0 - low) / span * 390)
    rows = []
    for index, (item, claim, value, _) in enumerate(series):
        y = 20 + index * 58
        point = 170 + ((value - low) / span * 390)
        x, width = min(axis, point), abs(point - axis)
        rows.append(
            f'<text x="0" y="{y + 14}" class="sl">{_esc(claim.get("period") or item.get("period") or "unknown")}</text>'
            f'<line x1="{axis:.2f}" x2="{axis:.2f}" y1="{y-2}" y2="{y+24}" class="axis"/>'
            f'<rect x="{x:.2f}" y="{y}" width="{width:.2f}" height="20"/>'
            f'<text x="570" y="{y+15}" class="sv">{_esc(claim["value"])} {_esc(claim.get("currency") or "")} {_esc(claim["unit"])} {_esc(claim.get("scale") or "base")}</text>'
        )
    height = 58 * len(series) + 35
    return (f'<figure><svg role="img" aria-labelledby="{_esc(chart_id)}-title" '
            f'viewBox="0 0 700 {height}"><title id="{_esc(chart_id)}-title">'
            f'Typed Claim series: {_esc(series[0][3][1])}</title>{"".join(rows)}</svg>'
            '<figcaption>Values come from hash-verified quantitative Claim versions with identical metric, unit, scale, and currency.</figcaption></figure>')


def _source_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        fields = [value.get(key) for key in ("ref", "claim_version_ref", "source_ref", "document_ref", "citation")]
        return " · ".join(str(item) for item in fields if isinstance(item, str) and item) or "structured source unavailable"
    return "source unavailable"


def _gap_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        fields = [value.get(key) for key in ("reason", "code", "detail")]
        return " · ".join(str(item) for item in fields if isinstance(item, str) and item) or "structured gap"
    return "unknown gap"

def _assets(asset_manifest: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if asset_manifest is None: return []
    if not isinstance(asset_manifest, Mapping) or set(asset_manifest) != {"schema_version","assets"} or asset_manifest["schema_version"] != "0.1" or not isinstance(asset_manifest["assets"], list):
        raise ResearchHtmlExportError("asset manifest has an invalid closed shape")
    out=[]
    for item in asset_manifest["assets"]:
        if not isinstance(item, Mapping) or set(item) != {"path","sha256","media_type","caption","source_refs"}: raise ResearchHtmlExportError("asset has an invalid closed shape")
        path=Path(item["path"]).expanduser().resolve(); media=item["media_type"]
        if media not in _IMAGE_TYPES or path.suffix.lower() not in {".png",".jpg",".jpeg"}: raise ResearchHtmlExportError("asset media type is not PNG/JPEG")
        raw=path.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
        if digest != item["sha256"] or not _SHA.fullmatch(digest): raise ResearchHtmlExportError("asset hash mismatch")
        if media == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ResearchHtmlExportError("asset bytes do not match PNG media type")
        if media in {"image/jpeg", "image/jpg"} and not (raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")):
            raise ResearchHtmlExportError("asset bytes do not match JPEG media type")
        refs=item["source_refs"]
        if not isinstance(refs,list) or not refs or any(not isinstance(x,str) or re.fullmatch(r"[a-z][a-z0-9_-]*:[^\s/?#]+", x) is None for x in refs): raise ResearchHtmlExportError("asset needs source refs")
        out.append({"data":f"data:{media};base64,{base64.b64encode(raw).decode()}","caption":str(item["caption"]),"refs":", ".join(refs),"sha256":digest})
    return out

def render_research_html(library: Mapping[str, Any], *, mission: Mapping[str, Any], claims: Mapping[str, Mapping[str, Any]] | None = None, assets: Sequence[Mapping[str,str]]=()) -> str:
    products=library.get("products") or []
    company_ref = library["company_ref"]
    member = next((row for row in mission.get("universe", []) if row.get("company_ref") == company_ref), {})
    company = " ".join(str(member.get(key)) for key in ("name", "ticker") if member.get(key)) or company_ref
    claims = claims or {}
    toc=[]; bodies=[]
    for pi,product in enumerate(products,1):
        anchor=f"product-{pi}"; toc.append(f'<li><a href="#{anchor}">{_esc(product.get("label") or product.get("kind"))}</a></li>')
        status=product.get("status","unknown"); binding=product.get("mission_binding","unknown")
        head=f'<section id="{anchor}"><h2>{_esc(product.get("label") or product.get("kind"))}</h2><p class="meta">status={_esc(status)} · mission_binding={_esc(binding)} · version={_esc(product.get("version_ref") or "unknown")} · hash={_esc(product.get("content_hash") or "unknown")}</p>'
        approval=product.get("approval") or {"status":"unknown"}
        head += f'<p class="approval">Human approval: {_esc(approval.get("status", "unknown"))} · decision {_esc(approval.get("decision_record_ref") or "none")} · actor {_esc(approval.get("actor_ref") or "unknown")} · at {_esc(approval.get("decided_at") or "unknown")}</p>'
        if product.get("reason"):
            head += f'<p class="unavailable">{_esc(product["reason"])}</p>'
        if product.get("gaps"):
            head += f'<p class="gaps">Product gaps: {_esc("; ".join(_gap_text(g) for g in product["gaps"]))}</p>'
        chunks=[]
        for si,section in enumerate(product.get("sections") or [],1):
            nums=section.get("numbers") or []; refs=section.get("sources") or []
            table=''.join(f'<tr><td>{_esc(n.get("period") or "unknown")}</td><td>{_esc(n.get("text") or "unknown")}</td><td><code>{_esc(n.get("claim_version_ref") or (n.get("cell") or {}).get("ref") or "unknown")}</code></td></tr>' for n in nums if isinstance(n,Mapping))
            chunks.append(f'<article><h3>{_esc(section.get("title") or "Untitled")}</h3><p class="prose">{_esc(section.get("body") or "Unknown / unavailable")}</p>{_chart(nums, claims, f"chart-{pi}-{si}") if nums else ""}{("<div class=\"tablewrap\"><table><thead><tr><th>Period</th><th>Value in authority text</th><th>Authority ref</th></tr></thead><tbody>"+table+"</tbody></table></div>") if table else ""}<p class="refs">Sources: {_esc(", ".join(_source_text(ref) for ref in refs) if refs else "unknown / unavailable")}</p><p class="gaps">Gaps: {_esc("; ".join(_gap_text(gap) for gap in (section.get("gaps") or [])) or "none recorded")}</p></article>')
        if not chunks: chunks=['<p class="unavailable">Unknown / unavailable: no current readable sections.</p>']
        bodies.append(head+''.join(chunks)+'</section>')
    figs=''.join(f'<figure><img src="{a["data"]}" alt="{_esc(a["caption"])}"><figcaption>{_esc(a["caption"])} · {_esc(a["refs"])} · user-supplied local figure · sha256 {_esc(a["sha256"])}</figcaption></figure>' for a in assets)
    css='''body{margin:0;background:#f5f5f7;color:#1d1d1f;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:980px;margin:auto;padding:48px 24px}h1{font-size:42px}h2{border-top:1px solid #ccc;padding-top:32px}article{background:white;border-radius:18px;padding:24px;margin:18px 0;box-shadow:0 2px 18px #0001}.meta,.refs,figcaption{color:#666;font-size:13px;overflow-wrap:anywhere}.approval{font-weight:700}.prose{white-space:pre-wrap}.unavailable,.gaps{background:#fff4ce;padding:10px;border-radius:8px}.tablewrap{overflow:auto}table{border-collapse:collapse;min-width:620px;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}svg{width:100%;height:auto}rect{fill:#147ce5}.sl,.sv{font-size:12px;fill:#333}img{max-width:100%;height:auto}@media(max-width:520px){main{padding:24px 14px}h1{font-size:32px}article{padding:16px}}@media print{body{background:#fff}article{box-shadow:none;border:1px solid #ddd;break-inside:avoid}nav{break-after:page}}'''
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+_esc(company)+' research</title><style>'+css+'</style></head><body><main><header><p>Dalton Research</p><h1>'+_esc(company)+'</h1><p>'+_esc(mission.get("title") or "Research report")+'</p><p class="meta">mission '+_esc(mission["id"])+' · '+_esc(mission["content_hash"])+'</p></header><nav aria-label="Contents"><h2>Contents</h2><ol>'+''.join(toc)+'</ol></nav>'+''.join(bodies)+('<section><h2>Figures</h2>'+figs+'</section>' if figs else '<section><h2>Figures</h2><p class="unavailable">No locally hashed, source-bound figure assets were supplied.</p></section>')+'</main></body></html>\n'

def export_research_html(core_db: str|Path, company_ref: str, output: str|Path, *, mission_ref: str|None=None, asset_manifest: str|Path|None=None, manifest_output: str|Path|None=None) -> dict[str,Any]:
    db=Path(core_db).expanduser().resolve(); uri = db.as_uri() + "?mode=ro"
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
    manifest=None if asset_manifest is None else json.loads(Path(asset_manifest).read_text())
    page=render_research_html(library, mission=mission, claims=claims, assets=_assets(manifest)); raw=page.encode()
    target=Path(output); target.write_bytes(raw)
    result={"schema_version":"0.1","company_ref":company_ref,"mission_version_ref":mission["id"],"mission_version_hash":mission["content_hash"],"html_file":target.name,"html_sha256":hashlib.sha256(raw).hexdigest(),"product_versions":[{"kind":p["kind"],"status":p["status"],"version_ref":p.get("version_ref"),"content_hash":p.get("content_hash"),"mission_binding":p.get("mission_binding","unknown"),"approval":(p.get("approval") or {}).get("status","unknown")} for p in library["products"]]}
    manifest_target = Path(manifest_output) if manifest_output is not None else target.with_suffix(target.suffix + ".manifest.json")
    manifest_target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--core-db',required=True); ap.add_argument('--company-ref',required=True); ap.add_argument('--output',required=True); ap.add_argument('--mission-ref'); ap.add_argument('--asset-manifest'); ap.add_argument('--manifest-output')
    print(json.dumps(export_research_html(**vars(ap.parse_args(argv))),sort_keys=True))
if __name__=='__main__': main()
