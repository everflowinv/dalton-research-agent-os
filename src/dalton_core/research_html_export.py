"""Deterministic, read-only export of current governed research to safe HTML."""
from __future__ import annotations

import argparse, base64, hashlib, html, json, re, sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cockpit_research_library import research_library
from .store import content_hash

_SHA = re.compile(r"^[0-9a-f]{64}$")
_NUM = re.compile(r"(?<![\w.])(?P<prefix>[$€£]?)(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>%|bps|x|[BMK])?(?!\w)", re.I)
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

def _number_point(item: Mapping[str, Any]) -> tuple[str, float] | None:
    text = item.get("text")
    if not isinstance(text, str): return None
    matches = list(_NUM.finditer(text))
    if not matches: return None
    m = matches[-1]
    unit = (m.group("prefix") or "") + (m.group("suffix") or "")
    if not unit: return None
    value = float(m.group("value").replace(",", ""))
    return unit.lower(), value

def _chart(numbers: Sequence[Mapping[str, Any]], chart_id: str) -> str:
    points = [(item, _number_point(item)) for item in numbers]
    points = [(item, point) for item, point in points if point is not None]
    units = {point[0] for _, point in points}
    if len(points) < 2 or len(units) != 1:
        return '<p class="unavailable">Chart unavailable: fewer than two traceable values with one compatible unit.</p>'
    values = [point[1] for _, point in points]; lo=min(0,min(values)); hi=max(values)
    span = hi-lo or 1; width=640; height=58*len(points)+35
    rows=[]
    for i,(item,(_,value)) in enumerate(points):
        y=20+i*58; w=max(1,(value-lo)/span*420)
        rows.append(f'<text x="0" y="{y+14}" class="sl">{_esc(item.get("period") or "value")}</text><rect x="170" y="{y}" width="{w:.2f}" height="20"/><text x="{180+w:.2f}" y="{y+15}" class="sv">{_esc(item["text"])}</text>')
    return f'<figure><svg role="img" aria-labelledby="{_esc(chart_id)}-title" viewBox="0 0 {width} {height}"><title id="{_esc(chart_id)}-title">Traceable quantitative values</title>{"".join(rows)}</svg><figcaption>Inline chart from the cited number rows below; values with incompatible or absent units are excluded.</figcaption></figure>'

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
        if not isinstance(refs,list) or not refs or any(not isinstance(x,str) or not x for x in refs): raise ResearchHtmlExportError("asset needs source refs")
        out.append({"data":f"data:{media};base64,{base64.b64encode(raw).decode()}","caption":str(item["caption"]),"refs":", ".join(refs),"sha256":digest})
    return out

def render_research_html(library: Mapping[str, Any], *, mission: Mapping[str, Any], assets: Sequence[Mapping[str,str]]=()) -> str:
    products=library.get("products") or []; company=library["company_ref"]
    toc=[]; bodies=[]
    for pi,product in enumerate(products,1):
        anchor=f"product-{pi}"; toc.append(f'<li><a href="#{anchor}">{_esc(product.get("label") or product.get("kind"))}</a></li>')
        status=product.get("status","unknown"); binding=product.get("mission_binding","unknown")
        head=f'<section id="{anchor}"><h2>{_esc(product.get("label") or product.get("kind"))}</h2><p class="meta">status={_esc(status)} · mission_binding={_esc(binding)} · version={_esc(product.get("version_ref") or "unknown")} · hash={_esc(product.get("content_hash") or "unknown")}</p>'
        approval=product.get("approval") or {"status":"unknown"}
        head+=f'<p class="approval">Human approval: {_esc(approval.get("status","unknown"))}</p>'
        chunks=[]
        for si,section in enumerate(product.get("sections") or [],1):
            nums=section.get("numbers") or []; refs=section.get("sources") or []
            table=''.join(f'<tr><td>{_esc(n.get("period") or "unknown")}</td><td>{_esc(n.get("text") or "unknown")}</td><td><code>{_esc(n.get("claim_version_ref") or (n.get("cell") or {}).get("ref") or "unknown")}</code></td></tr>' for n in nums if isinstance(n,Mapping))
            chunks.append(f'<article><h3>{_esc(section.get("title") or "Untitled")}</h3><p>{_esc(section.get("body") or "Unknown / unavailable")}</p>{_chart(nums,f"chart-{pi}-{si}") if nums else ""}{("<div class=\"tablewrap\"><table><thead><tr><th>Period</th><th>Value in authority text</th><th>Authority ref</th></tr></thead><tbody>"+table+"</tbody></table></div>") if table else ""}<p class="refs">Sources: {_esc(", ".join(refs) if refs else "unknown / unavailable")}</p><p class="gaps">Gaps: {_esc("; ".join(section.get("gaps") or []) or "none recorded")}</p></article>')
        if not chunks: chunks=['<p class="unavailable">Unknown / unavailable: no current readable sections.</p>']
        bodies.append(head+''.join(chunks)+'</section>')
    figs=''.join(f'<figure><img src="{a["data"]}" alt="{_esc(a["caption"])}"><figcaption>{_esc(a["caption"])} · {_esc(a["refs"])} · sha256 {_esc(a["sha256"])}</figcaption></figure>' for a in assets)
    css='''body{margin:0;background:#f5f5f7;color:#1d1d1f;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:980px;margin:auto;padding:48px 24px}h1{font-size:42px}h2{border-top:1px solid #ccc;padding-top:32px}article{background:white;border-radius:18px;padding:24px;margin:18px 0;box-shadow:0 2px 18px #0001}.meta,.refs,figcaption{color:#666;font-size:13px;overflow-wrap:anywhere}.approval{font-weight:700}.unavailable,.gaps{background:#fff4ce;padding:10px;border-radius:8px}.tablewrap{overflow:auto}table{border-collapse:collapse;min-width:620px;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}svg{width:100%;height:auto}rect{fill:#147ce5}.sl,.sv{font-size:12px;fill:#333}img{max-width:100%;height:auto}@media(max-width:520px){main{padding:24px 14px}h1{font-size:32px}article{padding:16px}}@media print{body{background:#fff}article{box-shadow:none;border:1px solid #ddd;break-inside:avoid}nav{break-after:page}}'''
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+_esc(company)+' research</title><style>'+css+'</style></head><body><main><header><p>Dalton Research</p><h1>'+_esc(company)+'</h1><p>'+_esc(mission.get("title") or "Research report")+'</p><p class="meta">mission '+_esc(mission["id"])+' · '+_esc(mission["content_hash"])+'</p></header><nav aria-label="Contents"><h2>Contents</h2><ol>'+''.join(toc)+'</ol></nav>'+''.join(bodies)+('<section><h2>Figures</h2>'+figs+'</section>' if figs else '<section><h2>Figures</h2><p class="unavailable">No locally hashed, source-bound figure assets were supplied.</p></section>')+'</main></body></html>\n'

def export_research_html(core_db: str|Path, company_ref: str, output: str|Path, *, mission_ref: str|None=None, asset_manifest: str|Path|None=None, manifest_output: str|Path|None=None) -> dict[str,Any]:
    db=Path(core_db).expanduser().resolve(); uri=f"file:{db.as_posix()}?mode=ro"
    c=sqlite3.connect(uri,uri=True); c.row_factory=sqlite3.Row
    try: mission=_current_mission(c,mission_ref); library=research_library(c,mission,company_ref)
    finally: c.close()
    manifest=None if asset_manifest is None else json.loads(Path(asset_manifest).read_text())
    page=render_research_html(library,mission=mission,assets=_assets(manifest)); raw=page.encode()
    target=Path(output); target.write_bytes(raw)
    result={"schema_version":"0.1","company_ref":company_ref,"mission_version_ref":mission["id"],"mission_version_hash":mission["content_hash"],"html_file":target.name,"html_sha256":hashlib.sha256(raw).hexdigest(),"product_versions":[{"kind":p["kind"],"status":p["status"],"version_ref":p.get("version_ref"),"content_hash":p.get("content_hash"),"mission_binding":p.get("mission_binding","unknown"),"approval":(p.get("approval") or {}).get("status","unknown")} for p in library["products"]]}
    manifest_target = Path(manifest_output) if manifest_output is not None else target.with_suffix(target.suffix + ".manifest.json")
    manifest_target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--core-db',required=True); ap.add_argument('--company-ref',required=True); ap.add_argument('--output',required=True); ap.add_argument('--mission-ref'); ap.add_argument('--asset-manifest'); ap.add_argument('--manifest-output')
    print(json.dumps(export_research_html(**vars(ap.parse_args(argv))),sort_keys=True))
if __name__=='__main__': main()
