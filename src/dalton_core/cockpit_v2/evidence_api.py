"""Cockpit v2: the source behind a conclusion -- 「查看原文与出处」 (batch ah).

Review IDs C10 / C13 / A03 (independent analyst UX review 2026-10-09) and the
financial review's 4.6: the evidence drawer showed a conclusion's statement,
its company and period and an internal ``claim-version:`` ref, and nothing an
analyst could check it against.  This module walks the chain the Core already
holds, read-only, and answers in the analyst's terms:

    conclusion (claim_versions)
      -> evidence (evidence_relations -> evidence_versions)
        -> SEC: the filed statement line (``statement-ingest:<id>#<ordinal>``
           in the lineage -> coverage_mission_statement_lines / _filings),
           with the EDGAR filing index as the original
        -> a document: the citation binding (transcript_claim_citation_bindings
           -> transcript_correction_set_versions -> document_ref), the cited
           bytes re-read from the raw spool and re-hashed, the title / house /
           date from document_provenance_records, the URL a fetch recorded.

``GET /v2/data/evidence/source?claim=<claim version ref>``
``GET /v2/data/evidence/document?claim=<claim version ref>&source=<index>``
``GET /v2/data/evidence/statement-line?line=<line id>``
    (or ``?company=…&accession=…&concept=…[&period_end=YYYY-MM-DD]``)

Every read is scoped to the active mission's companies.  Internal refs
(claim / evidence versions, document refs, bindings, XBRL ids) are only ever
inside ``technical`` -- the page shows them in a collapsed 「技术标识」.  A
data gap is a 200 ``status: "empty"`` whose ``empty`` names the state, what it
affects, who handles it and what the analyst can do (G12); only an unusable
request is a 400.  Nothing here writes, calls a model or reaches the network.
"""

from __future__ import annotations

from ..terminology import label as _term

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_VERSION = "cockpit-v2-evidence-0.1"
PREFIX = "/v2/data/evidence/"
SOURCE_PATH = PREFIX + "source"
DOCUMENT_PATH = PREFIX + "document"
STATEMENT_LINE_PATH = PREFIX + "statement-line"
JSON_TYPE = "application/json; charset=utf-8"
MAX_REF = 512
MAX_DOCUMENT_CHARS = 300_000
MAX_STATEMENT_ROWS = 800
PASSAGE_CONTEXT = 300
CITED_MAX_CHARS = 1200
SPOOL_DIRS = ("transcript-spool", "connector-spool", "raw-spool")

RELATION_ORDER = {"supports": 0, "qualifies": 1, "contradicts": 2}
RELATION_LABELS = {"supports": "支持", "qualifies": "补充限定", "contradicts": "反对"}
STATEMENT_LABELS = {"income": "利润表", "balance": "资产负债表", "cash": "现金流量表"}
FORM_LABELS = {"10-K": "年报 10-K", "10-Q": "季报 10-Q", "8-K": "临时公告 8-K", "20-F": "年报 20-F"}
TIER_LABELS = {"sell_side": "卖方研报", "management": "电话会纪要", "sales_note": "卖方销售快报",
               "other": "研究资料"}
SOURCE_LABELS = {"source:alphaengine": "卖方研报", "source:guidepoint": "专家访谈",
                 "source:public-web": "公开网页", "source:company-wiki": "公司知识库",
                 "source:sales-notes": "卖方销售快报", "source:sec-edgar": "SEC 申报文件"}
TYPE_LABELS = {"expert_network": "专家访谈", "public_web": "公开网页", "internal_wiki": "公司知识库",
               "sell_side_note": "卖方销售快报", "official_filing": "SEC 申报文件"}

_LINE_ID_RE = re.compile(r"^statement-ingest:[0-9A-Za-z]+#\d+$")
_FILING_RE = re.compile(r"^sec:filing:(\d{10}-\d{2}-\d{6})$")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HAN = re.compile(r"[一-鿿]")
BINDING_PREFIX = "transcript-claim-citation-binding:"


class EvidenceRequestError(ValueError):
    """The request itself is unusable (HTTP 400)."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _json(status: int, value: Mapping[str, Any]) -> tuple[int, str, bytes]:
    return status, JSON_TYPE, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _param(query: Mapping[str, Any], name: str, what: str, *, required: bool = True) -> str | None:
    raw = query.get(name)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise EvidenceRequestError(f"缺少参数 {name}：{what}")
        return None
    if not isinstance(raw, str) or len(raw) > MAX_REF:
        raise EvidenceRequestError(f"参数 {name} 不对或太长")
    return raw.strip()


def _table(core: sqlite3.Connection, name: str) -> bool:
    return core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _lang(text: str) -> str:
    return "zh" if _HAN.search(text or "") else "en"


def _day(value: Any) -> str | None:
    text = str(value or "")
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def _empty_parts(state: str, impact: str, owner: str, action: str) -> dict[str, Any]:
    return {"state": state, "impact": impact, "owner": owner, "action": action}


def _empty_reason(parts: Mapping[str, str]) -> str:
    return (f"当前状态：{parts['state']}；影响：{parts['impact']}；"
            f"谁处理：{parts['owner']}；你可以：{parts['action']}。")


def _envelope(kind: str) -> dict[str, Any]:
    return {"kind": kind, "schema_version": SCHEMA_VERSION, "status": "ok",
            "empty": None, "empty_reason": None}


def _empty(payload: dict[str, Any], parts: Mapping[str, str]) -> dict[str, Any]:
    payload["status"] = "empty"
    payload["empty"] = dict(parts)
    payload["empty_reason"] = _empty_reason(parts)
    return payload


NO_CORE = _empty_parts("这个研究环境还没有数据库，研究还没有开始", "还没有可以核对的结论和原文",
                       "研究开始后系统会自动收集资料并记录出处", f"先在「{_term('nav', 'settings')}」里设定研究目标")
NO_MISSION = _empty_parts("这个研究环境还没有发布研究目标", "没有覆盖公司，也就没有结论和原文",
                          "发布研究目标后系统会开始收集资料", f"在「{_term('nav', 'settings')}」里设定研究目标")
NOT_IN_MISSION = _empty_parts("这条结论不在当前研究项目中", "无法显示它的原文出处",
                              "无需处理：可能是旧链接，或这条结论已被撤回",
                              f"回到公司页的「{_term('company_section', 'evidence')}」重新打开")
READ_FAILED = _empty_parts("暂时读不出这条结论的出处", "这次看不到原文",
                           "通常是数据正在更新，稍后会恢复；一直如此请联系管理员",
                           "稍后刷新再试")


# ---------------------------------------------------------------------------
# the reading context
# ---------------------------------------------------------------------------


class _Context:
    """One request's read-only Core, mission scope, spool and URL lookup."""

    def __init__(self, context: Any) -> None:
        from .charts_api import _reader

        self.reader = _reader(context)
        self.plane = context.get("plane") if isinstance(context, Mapping) else context
        self.state_dir = Path(self.reader.core_db).parent
        self._spool: Any = None
        self._urls: Mapping[str, Mapping[str, Any]] | None = None
        self._texts: dict[str, tuple[str | None, str | None]] = {}

    # -- scope -------------------------------------------------------------
    def scope(self, core: sqlite3.Connection, mission: Mapping[str, Any]) -> None:
        self.members = self.reader.members(mission)
        self.industry_ref = mission.get("industry_ref")

    def in_scope(self, subject_ref: Any) -> bool:
        return isinstance(subject_ref, str) and (
            subject_ref in self.members or (bool(self.industry_ref) and subject_ref == self.industry_ref))

    def company(self, company_ref: Any) -> str:
        """「Accenture ACN」 -- the ticker once when there is no other name; never a CIK."""

        member = self.members.get(company_ref) if isinstance(company_ref, str) else None
        if member is None:
            return "行业" if company_ref and company_ref == self.industry_ref else "未登记代码的公司"
        ticker = str(member.get("ticker") or "").strip()
        name = str(member.get("name") or "").strip()
        if not ticker:
            return name or "未登记代码的公司"
        return f"{name} {ticker}" if name and name.upper() != ticker.upper() else ticker

    # -- raw spool ---------------------------------------------------------
    def spool(self) -> Any:
        if self._spool is None:
            from ..raw_spool import MultiRootRawSpoolReader, RawSpoolReader

            roots = []
            for name in SPOOL_DIRS:
                path = self.state_dir / name
                if not path.is_dir():
                    continue
                try:
                    roots.append(RawSpoolReader(path))
                except Exception:  # noqa: BLE001 - a missing root is simply not read
                    continue
            self._spool = MultiRootRawSpoolReader(roots) if roots else False
        return self._spool or None

    def text(self, digest: Any) -> tuple[str | None, str | None]:
        """(text, None) or (None, the reason in Chinese)."""

        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            return None, "这份资料没有登记可核对的存档内容"
        if digest in self._texts:
            return self._texts[digest]
        spool = self.spool()
        if spool is None:
            result: tuple[str | None, str | None] = (None, "这台机器上没有资料存档，原文暂不可用")
        else:
            try:
                raw = spool.read_object(digest)
            except Exception:  # noqa: BLE001 - pruned or never stored here
                raw = None
            if raw is None:
                result = (None, "存档里找不到这份资料的原文（可能已按保留期清理），原文暂不可用")
            elif hashlib.sha256(raw).hexdigest() != digest:
                result = (None, "存档内容与登记时的指纹不一致，为避免误导，不显示原文")
            else:
                try:
                    result = (raw.decode("utf-8"), None)
                except UnicodeDecodeError:
                    result = (None, "这份资料不是可直接阅读的文字，原文暂不可用")
        self._texts[digest] = result
        return result

    def rendered_web_text(self, core: sqlite3.Connection, evidence: Mapping[str, Any],
                          digest: Any) -> str | None:
        """A fetched page's cited text: its stored raw body rendered the way
        extraction rendered it, accepted only if the rendering hashes to the
        digest the citation was bound to (``public_web_extraction_source``)."""

        envelope_ref = evidence.get("source_envelope_ref")
        if not isinstance(envelope_ref, str) or not _table(core, "connector_source_envelopes"):
            return None
        row = core.execute("SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                           (envelope_ref,)).fetchone()
        if row is None:
            return None
        envelope = json.loads(row["record_json"])
        raw_hash = envelope.get("raw_response_hash")
        media = None
        if _table(core, "observability_artifact_versions_v2"):
            artifact = core.execute("SELECT record_json FROM observability_artifact_versions_v2 WHERE version_id=?",
                                    (envelope.get("raw_artifact_version_ref"),)).fetchone()
            if artifact is not None:
                media = json.loads(artifact["record_json"]).get("media_type")
        spool = self.spool()
        if spool is None or not isinstance(raw_hash, str) or not isinstance(media, str):
            return None
        try:
            from ..public_web_extraction_source import render_public_web_text_candidates

            raw = spool.read_object(raw_hash)
            if hashlib.sha256(raw).hexdigest() != raw_hash:
                return None
            # 2026-10-09: either rendering an inline-XBRL filing was cited under.
            texts = [item.get("text") for item in render_public_web_text_candidates(
                raw, raw_media_type=media)]
        except Exception:  # noqa: BLE001 - not renderable here is "no original", not a failure
            return None
        text = next((item for item in texts if isinstance(item, str)
                     and hashlib.sha256(item.encode("utf-8")).hexdigest() == digest), None)
        if text is None:
            return None
        self._texts[digest] = (text, None)
        return text

    def url(self, document_ref: Any) -> str | None:
        if not isinstance(document_ref, str) or not document_ref:
            return None
        if self._urls is None:
            self._urls = {}
            getter = getattr(self.plane, "_url_map", None)
            if callable(getter):
                try:
                    self._urls = getter() or {}
                except Exception:  # noqa: BLE001 - no URL is a missing link, not a failure
                    self._urls = {}
        info = self._urls.get(document_ref) or {}
        found = re.match(r"^public-web-document:url-sha256:([0-9a-f]{64}):", document_ref)
        if not info and found:
            # A fetched page's record names its URL by hash; discovery and the
            # fetch tickets key the URL by ``public-web-url:sha256:<hash>``.
            info = self._urls.get(f"public-web-url:sha256:{found.group(1)}") or {}
        url = info.get("canonical_url") or info.get("url")
        return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


# ---------------------------------------------------------------------------
# display pieces
# ---------------------------------------------------------------------------


def _value_display(value: Any, currency: Any, unit: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        from ..financial_display import _amount

        return _amount(str(value), currency if isinstance(currency, str) else None,
                       unit if isinstance(unit, str) else None)
    except Exception:  # noqa: BLE001 - the stored text is still true
        return str(value)


def _concept_label(concept: Any) -> str | None:
    from ..financial_display import concept_label

    try:
        return concept_label(concept)
    except Exception:  # noqa: BLE001
        return None


def _humanize_concept(concept: Any) -> str | None:
    """``us-gaap:CostOfRevenue`` -> ``Cost Of Revenue`` (never the raw id)."""

    text = str(concept or "")
    if ":" not in text:
        return None
    name = text.split(":", 1)[1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip()
    return words or None


def _period_text(start: Any, end: Any) -> str:
    from .charts_api import _period_text as period

    return period(start, end) if start else (f"截至{period(None, end)}" if end else "期间未记录")


def _sec_url(cik: Any, accession: str) -> str | None:
    digits = re.sub(r"\D", "", str(cik or ""))
    if not digits or not _ACCESSION_RE.fullmatch(accession or ""):
        return None
    return (f"https://www.sec.gov/Archives/edgar/data/{int(digits)}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _cik_of(company_ref: Any) -> str | None:
    found = re.match(r"^company:sec-cik:(\d+)$", str(company_ref or ""))
    return found.group(1) if found else None


def _visible_text(text: str | None) -> str | None:
    """A visible string with any XBRL id replaced by its account name."""

    if text is None:
        return None
    from ..financial_display import financial_display_text

    try:
        return financial_display_text(text)
    except Exception:  # noqa: BLE001
        return text


# ---------------------------------------------------------------------------
# one source: an SEC statement line or a cited document
# ---------------------------------------------------------------------------


def _line_row(core: sqlite3.Connection, line_id: str) -> dict[str, Any] | None:
    if not (_table(core, "coverage_mission_statement_lines")
            and _table(core, "coverage_mission_statement_filings")):
        return None
    row = core.execute(
        "SELECT l.line_id, l.ingest_id, l.statement, l.ordinal, l.concept, l.label, l.level, "
        "l.dimension_member, l.period_start, l.period_end, l.value, l.unit, "
        "f.company_ref, f.cik, f.entity_name, f.accession, f.form, f.filed, f.report_date "
        "FROM coverage_mission_statement_lines l JOIN coverage_mission_statement_filings f "
        "ON f.ingest_id=l.ingest_id WHERE l.line_id=?", (line_id,)).fetchone()
    return None if row is None else dict(row)


def _line_label(row: Mapping[str, Any]) -> str:
    account = _concept_label(row.get("concept"))
    filed = str(row.get("label") or "").strip()
    if account and filed and filed.casefold() != account.casefold() and not _HAN.search(filed):
        return f"{account}（{filed}）"
    return account or filed or _humanize_concept(row.get("concept")) or "报表行"


def _sec_source(ctx: _Context, row: Mapping[str, Any]) -> dict[str, Any]:
    statement = STATEMENT_LABELS.get(str(row.get("statement")), "财务报表")
    label = _line_label(row)
    period = _period_text(row.get("period_start"), row.get("period_end"))
    value = _value_display(row.get("value"), None, row.get("unit"))
    form = str(row.get("form") or "")
    url = _sec_url(row.get("cik") or _cik_of(row.get("company_ref")), str(row.get("accession") or ""))
    location = f"{statement} 第 {row.get('ordinal')} 行：{label}"
    links = []
    if url:
        links.append({"kind": "original", "label": f"在 SEC EDGAR 打开原始申报（{form or '申报文件'}）",
                      "url": url})
    return {
        "source_kind": "sec_statement_line",
        "document_type_label": FORM_LABELS.get(form, "SEC 申报文件"),
        "title": f"{row.get('entity_name') or ''} {FORM_LABELS.get(form, form)}".strip(),
        "house": "美国证券交易委员会（SEC）申报",
        "published_at": _day(row.get("filed")),
        "filing": {"form": form or None, "accession": row.get("accession"), "filed": _day(row.get("filed")),
                   "report_date": _day(row.get("report_date")), "entity_name": row.get("entity_name"),
                   "period_label": period},
        "location": {"label": location, "statement_label": statement,
                     "line_ordinal": row.get("ordinal"),
                     "line_label_as_filed": row.get("label"), "concept_label": label,
                     "start": None, "end": None, "line_number": row.get("ordinal")},
        "passage": {"text": f"{label}（{period}）：{value or '申报值未记录'}",
                    "highlight": None, "speaker": None, "lang": "zh", "kind": "filed_line"},
        "passage_unavailable_reason": None,
        "links": links,
        "_internal": {"kind": "sec", "line_id": row.get("line_id"), "ingest_id": row.get("ingest_id"),
                      "statement": row.get("statement"), "url": url},
        "technical": {"document_ref": f"sec:filing:{row.get('accession')}", "accession": row.get("accession"),
                      "concept": row.get("concept"), "statement_line_ref": row.get("line_id")},
    }


def _binding(core: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    if not _table(core, "transcript_claim_citation_bindings"):
        return None
    row = core.execute(
        "SELECT correction_set_version_ref, source_content_hash, source_start, source_end "
        "FROM transcript_claim_citation_bindings WHERE binding_id=?", (ref,)).fetchone()
    if row is None:
        return None
    document_ref = None
    if _table(core, "transcript_correction_set_versions"):
        correction = core.execute("SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
                                  (row["correction_set_version_ref"],)).fetchone()
        if correction is not None:
            try:
                document_ref = json.loads(correction["record_json"]).get("document_ref")
            except (TypeError, ValueError):
                document_ref = None
    return {"ref": ref, "digest": row["source_content_hash"], "start": int(row["source_start"]),
            "end": int(row["source_end"]), "document_ref": document_ref}


def _provenance(core: sqlite3.Connection, document_ref: Any) -> dict[str, Any]:
    if not isinstance(document_ref, str) or not _table(core, "document_provenance_records"):
        return {}
    row = core.execute("SELECT broker, title, published_at, provenance_tier, record_json "
                       "FROM document_provenance_records WHERE document_ref=?", (document_ref,)).fetchone()
    if row is None:
        return {}
    try:
        record = json.loads(row["record_json"] or "{}")
    except (TypeError, ValueError):
        record = {}
    return {"house": row["broker"], "title": row["title"], "published_at": _day(row["published_at"]),
            "tier": row["provenance_tier"], "spec_ref": record.get("spec_ref"),
            "url": record.get("url") or record.get("canonical_url")}


def _document_type(evidence: Mapping[str, Any], provenance: Mapping[str, Any]) -> str:
    spec = str(provenance.get("spec_ref") or "")
    if "transcript" in spec:
        return "电话会纪要"
    if provenance.get("tier") in TIER_LABELS:
        return TIER_LABELS[provenance["tier"]]
    source = str(evidence.get("source_ref") or "")
    if source in SOURCE_LABELS and source != "source:alphaengine":
        return SOURCE_LABELS[source]
    kind = str(evidence.get("source_type") or "")
    if kind == "authenticated_transcript":
        return "卖方研报或电话会纪要"
    return TYPE_LABELS.get(kind) or SOURCE_LABELS.get(source) or "研究资料"


def _trim_context(text: str, start: int, end: int) -> tuple[int, int]:
    lo = max(0, start - PASSAGE_CONTEXT)
    if lo > 0:
        space = text.find(" ", lo, start)
        newline = text.find("\n", lo, start)
        cut = min([i for i in (space, newline) if i >= 0], default=-1)
        lo = cut + 1 if cut >= 0 else lo
    hi = min(len(text), end + PASSAGE_CONTEXT)
    if hi < len(text):
        cut = max(text.rfind(" ", end, hi), text.rfind("\n", end, hi))
        hi = cut if cut > end else hi
    return lo, hi


def _document_source(core: sqlite3.Connection, ctx: _Context, evidence: Mapping[str, Any],
                     binding: Mapping[str, Any]) -> dict[str, Any]:
    from ..claim_support_context import cited_passage

    document_ref = binding.get("document_ref")
    provenance = _provenance(core, document_ref)
    doc_type = _document_type(evidence, provenance)
    title = provenance.get("title")
    text, reason = ctx.text(binding.get("digest"))
    if text is None and str(evidence.get("source_ref")) == "source:public-web":
        rendered = ctx.rendered_web_text(core, evidence, binding.get("digest"))
        if rendered is not None:
            text, reason = rendered, None
    passage = None
    location: dict[str, Any] = {"label": None, "statement_label": None, "line_ordinal": None,
                                "line_label_as_filed": None, "concept_label": None,
                                "start": binding.get("start"), "end": binding.get("end"), "line_number": None}
    if text is not None:
        start, end = binding["start"], binding["end"]
        if not 0 <= start < end <= len(text):
            text, reason = None, "登记的引用位置超出了存档原文，为避免误导，不显示原文"
        else:
            cited = cited_passage(text, start, end, max_chars=CITED_MAX_CHARS)
            s, e = cited["start"], cited["end"]
            lo, hi = _trim_context(text, s, e)
            line_number = text.count("\n", 0, s) + 1
            location.update({"start": s, "end": e, "line_number": line_number,
                             "label": f"原文第 {s + 1:,}–{e:,} 字（第 {line_number:,} 行）"})
            shown = text[lo:hi]
            passage = {"text": shown, "highlight": [s - lo, e - lo], "speaker": cited.get("speaker"),
                       "lang": _lang(text[s:e]), "kind": "excerpt",
                       "truncated_before": lo > 0, "truncated_after": hi < len(text)}
    if location["label"] is None and binding.get("start") is not None:
        location["label"] = f"原文第 {binding['start'] + 1:,}–{binding['end']:,} 字"
    url = ctx.url(document_ref) or (provenance.get("url") if str(provenance.get("url") or "").startswith(
        ("http://", "https://")) else None)
    links = []
    if url:
        links.append({"kind": "original", "label": f"打开原始网页（{doc_type}）" if doc_type == "公开网页"
                      else f"打开原文出处（{doc_type}）", "url": url})
    house = provenance.get("house")
    published = provenance.get("published_at")
    if title is None and str(evidence.get("source_ref")) == "source:sales-notes":
        try:
            from ..claim_support_context import sales_note_facts

            facts = sales_note_facts(core, ctx.spool(), evidence.get("source_envelope_ref"))
        except Exception:  # noqa: BLE001 - no header is no title
            facts = {}
        title, house, published = facts.get("title"), house or facts.get("house"), published or facts.get("date")
    if title is None and text is not None:
        # The document's own first line (a page title, an interview heading).
        head = text.splitlines()[:30]
        named = next((m.group(1).strip().strip('"') for m in (
            re.match(r"^\s*title\s*:\s*(.+)$", line, re.I) for line in head) if m), "")
        if head and head[0].strip() == "---":
            closing = next((i for i, line in enumerate(head[1:], 1) if line.strip() == "---"), 0)
            head = head[closing + 1:]
        first = named or next((line.strip().lstrip("#").strip() for line in head
                               if re.search(r"[A-Za-z一-鿿]", line)), "")
        if 0 < len(first) <= 120:
            title = first
    if title is None and doc_type == "公开网页" and url:
        title = re.sub(r"^https?://", "", url)[:120]
    return {
        "source_kind": "document",
        "document_type_label": doc_type,
        "title": title or f"一份{doc_type}（未记录标题）",
        "house": house,
        "published_at": published,
        "filing": None,
        "location": location,
        "passage": passage,
        "passage_unavailable_reason": None if passage else reason,
        "links": links,
        "_internal": {"kind": "document", "digest": binding.get("digest"), "start": location["start"],
                      "end": location["end"], "url": url, "readable": text is not None},
        "technical": {"document_ref": document_ref, "binding_ref": binding.get("ref")},
    }


_CONCEPT_RECORD_RE = re.compile(r"^sec:company-concept:(\d{10}-\d{2}-\d{6}):((?:us-gaap|dei|ifrs-full|srt):[A-Za-z0-9]+):(\S+)$")


def _filing_row(core: sqlite3.Connection, accession: str, company_ref: Any = None) -> dict[str, Any] | None:
    if not _table(core, "coverage_mission_statement_filings"):
        return None
    row = core.execute(
        "SELECT ingest_id, company_ref, cik, entity_name, accession, form, filed, report_date "
        "FROM coverage_mission_statement_filings WHERE accession=? ORDER BY rowid LIMIT 1",
        (accession,)).fetchone()
    return None if row is None else dict(row)


def _concept_records(core: sqlite3.Connection, evidence: Mapping[str, Any],
                     lineage: list[str]) -> list[tuple[str, str, str]]:
    """``sec:company-concept:<accession>:<concept>:<frame>`` records behind an
    SEC company-facts envelope (batch ah: the XBRL figures that have no
    statement-line lineage)."""

    envelope = str(evidence.get("source_envelope_ref") or "") or next(
        (item for item in lineage if item.startswith("source-envelope:")), "")
    if not envelope or not _table(core, "connector_source_envelopes"):
        return []
    row = core.execute("SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
                       (envelope,)).fetchone()
    if row is None:
        return []
    try:
        refs = json.loads(row["record_json"]).get("source_record_refs") or []
    except (TypeError, ValueError):
        return []
    return [m.groups() for m in (_CONCEPT_RECORD_RE.fullmatch(str(ref)) for ref in refs) if m]


def _concept_line(core: sqlite3.Connection, accession: str, concept: str,
                  period: Any) -> dict[str, Any] | None:
    """The filed statement line (no dimension) of that filing, concept and period, when exactly one."""

    if not (_table(core, "coverage_mission_statement_lines") and _table(core, "coverage_mission_statement_filings")):
        return None
    start, _, end = str(period or "").partition("..")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end or ""):
        return None
    rows = core.execute(
        "SELECT l.line_id FROM coverage_mission_statement_lines l JOIN coverage_mission_statement_filings f "
        "ON f.ingest_id=l.ingest_id WHERE f.accession=? AND l.concept=? AND l.dimension_member IS NULL "
        "AND l.period_end=? AND (l.period_start=? OR l.period_start IS NULL)",
        (accession, concept, end, start or None)).fetchall()
    return _line_row(core, rows[0]["line_id"]) if len(rows) == 1 else None


def _source_view(core: sqlite3.Connection, ctx: _Context, evidence: Mapping[str, Any],
                 claim: Mapping[str, Any] | None = None) -> dict[str, Any]:
    lineage = [str(item) for item in evidence.get("source_lineage") or ()]
    binding_ref = next((str(item.get("ref")) for item in evidence.get("artifact_refs") or ()
                        if isinstance(item, Mapping) and str(item.get("ref", "")).startswith(BINDING_PREFIX)),
                       None) or next((item for item in lineage if item.startswith(BINDING_PREFIX)), None)
    if binding_ref:
        binding = _binding(core, binding_ref)
        if binding is not None:
            return _document_source(core, ctx, evidence, binding)
    line_id = next((item for item in lineage if _LINE_ID_RE.fullmatch(item)), None)
    if line_id:
        row = _line_row(core, line_id)
        if row is not None:
            return _sec_source(ctx, row)
    claim = claim or {}
    records = _concept_records(core, evidence, lineage) if str(evidence.get("source_ref")) == "source:sec-edgar" else []
    if records:
        accession, concept, frame = records[0]
        row = _concept_line(core, accession, concept, claim.get("period"))
        if row is not None:
            view = _sec_source(ctx, row)
            view["technical"] = {**view.get("technical", {}), "xbrl_frame": frame}
            return view
        filing = _filing_row(core, accession) or {}
        url = _sec_url(filing.get("cik") or _cik_of(claim.get("subject_ref")), accession)
        form = str(filing.get("form") or "")
        account = _concept_label(concept) or _humanize_concept(concept) or "申报科目"
        return {"source_kind": "sec_xbrl", "document_type_label": FORM_LABELS.get(form, "SEC 申报文件"),
                "title": (f"{filing.get('entity_name') or ''} {FORM_LABELS.get(form, form)}".strip()
                          or f"SEC 申报文件（申报编号 {accession}）"),
                "house": "美国证券交易委员会（SEC）申报", "published_at": _day(filing.get("filed")),
                "filing": {"form": form or None, "accession": accession, "filed": _day(filing.get("filed")),
                           "report_date": _day(filing.get("report_date")), "entity_name": filing.get("entity_name"),
                           "period_label": None},
                "location": {"label": f"申报的 XBRL 数据：{account}", "statement_label": None, "line_ordinal": None,
                             "line_label_as_filed": None, "concept_label": account, "start": None, "end": None,
                             "line_number": None},
                "passage": None,
                "passage_unavailable_reason": "这一数字取自申报的 XBRL 结构化数据，没有对应的报表段落；可打开原始申报核对",
                "links": ([{"kind": "original", "label": f"在 SEC EDGAR 打开原始申报（{form or '申报文件'}）", "url": url}]
                          if url else []),
                "_internal": {"kind": "filing", "accession": accession},
                "technical": {"document_ref": f"sec:filing:{accession}", "accession": accession,
                              "concept": concept, "xbrl_frame": frame}}
    accession = next((m.group(1) for m in map(_FILING_RE.fullmatch, lineage) if m), None)
    source_ref = str(evidence.get("source_ref") or "")
    if accession:
        filing = _filing_row(core, accession) or {}
        url = _sec_url(filing.get("cik") or _cik_of(claim.get("subject_ref")), accession)
        return {"source_kind": "document", "document_type_label": "SEC 申报文件",
                "title": f"SEC 申报文件（申报编号 {accession}）", "house": "美国证券交易委员会（SEC）申报",
                "published_at": None, "filing": {"form": None, "accession": accession, "filed": None,
                                                 "report_date": None, "entity_name": None, "period_label": None},
                "location": {"label": None}, "passage": None,
                "passage_unavailable_reason": "这条证据没有登记引用的报表行或段落",
                "links": ([{"kind": "original", "label": "在 SEC EDGAR 打开原始申报", "url": url}] if url else []),
                "_internal": {"kind": "filing", "accession": accession},
                "technical": {"document_ref": f"sec:filing:{accession}", "accession": accession}}
    market = source_ref.startswith("market-price-series-version:") or any(
        item.startswith("market-price-series-version:") for item in lineage)
    return {"source_kind": "market_data" if market else "unknown",
            "document_type_label": "行情数据" if market else (SOURCE_LABELS.get(source_ref) or "研究资料"),
            "title": "公开行情数据（收盘价序列）" if market else "来源未登记标题",
            "house": None, "published_at": _day(evidence.get("retrieved_at")), "filing": None,
            "location": {"label": None}, "passage": None,
            "passage_unavailable_reason": ("行情数据没有可以引用的原文段落" if market
                                           else "这条证据没有登记可以打开的原文"),
            "links": [], "_internal": {"kind": "none"}, "technical": {}}


# ---------------------------------------------------------------------------
# the claim and its sources
# ---------------------------------------------------------------------------


def _retired(core: sqlite3.Connection) -> set[str]:
    try:
        from ..claim_retirement import retired_claim_version_refs

        return retired_claim_version_refs(core)
    except Exception:  # noqa: BLE001 - an older Core has no retirement table
        return set()


def _claim(core: sqlite3.Connection, ctx: _Context, ref: str) -> dict[str, Any] | None:
    if not _table(core, "claim_versions"):
        return None
    row = core.execute("SELECT claim_json, created_at FROM claim_versions WHERE claim_version_id=?",
                       (ref,)).fetchone()
    if row is None or ref in _retired(core):
        return None
    claim = json.loads(row["claim_json"])
    if not ctx.in_scope(claim.get("subject_ref")):
        return None
    claim["_created_at"] = row["created_at"]
    return claim


def _claim_view(ctx: _Context, claim: Mapping[str, Any]) -> dict[str, Any]:
    from ..cockpit_plane import claim_period_display_label, claim_statement_display
    from ..financial_display import statement_display

    original = str(claim.get("normalized_statement") or "")
    shown = claim_statement_display(original) or statement_display(original) or original
    period = claim.get("period")
    # The stored wording beside the display only when it is another language
    # (an English conclusion shown in Chinese, C11); a Chinese restatement of
    # a Chinese record (XBRL ids, raw amounts) belongs under 技术标识.
    other_language = shown != original and _lang(original) != _lang(shown)
    return {
        "statement": shown, "statement_original": original if other_language else None,
        "statement_stored": original if shown != original and not other_language else None,
        "statement_lang": _lang(shown), "original_lang": _lang(original),
        "period_label": claim_period_display_label(period) or (str(period) if period else "期间未注明"),
        "company": ctx.company(claim.get("subject_ref")), "company_ref": claim.get("subject_ref"),
        "kind": claim.get("claim_kind"),
        "kind_label": {"quantitative": "定量结论", "qualitative": "定性结论"}.get(claim.get("claim_kind"), "结论"),
        "value_display": _value_display(claim.get("value"), claim.get("currency"), claim.get("unit")),
        "recorded_at": claim.get("_created_at") or claim.get("created_at"),
        "prior_version": bool(claim.get("prior_version_ref")),
    }


def _sources(core: sqlite3.Connection, ctx: _Context, claim_ref: str,
             claim: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not (_table(core, "evidence_relations") and _table(core, "evidence_versions")):
        return []
    rows = core.execute(
        "SELECT r.relation, r.evidence_version_id, e.evidence_json FROM evidence_relations r "
        "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
        "WHERE r.claim_version_id=? ORDER BY r.created_at, r.relation_id", (claim_ref,)).fetchall()
    out = []
    for row in sorted(rows, key=lambda r: RELATION_ORDER.get(r["relation"], 9)):
        evidence = json.loads(row["evidence_json"])
        view = _source_view(core, ctx, evidence, claim)
        view["relation"] = row["relation"]
        view["relation_label"] = RELATION_LABELS.get(row["relation"], "相关")
        view["technical"] = {"evidence_version_ref": row["evidence_version_id"],
                             "source_ref": evidence.get("source_ref"), **view.get("technical", {})}
        out.append(view)
    return out


def _public(source: Mapping[str, Any], claim_ref: str, index: int) -> dict[str, Any]:
    view = {key: value for key, value in source.items() if key != "_internal"}
    internal = source.get("_internal") or {}
    if internal.get("kind") == "sec" or internal.get("readable"):
        view["links"] = list(view.get("links") or []) + [{
            "kind": "reader", "label": "查看纪要 / 资料全文" if internal.get("kind") != "sec" else "查看申报文件原件（整张报表）",
            "href": f"{DOCUMENT_PATH}?claim={claim_ref}&source={index}"}]
    view["index"] = index
    return view


def _with_scope(payload: dict[str, Any], ctx: _Context, core: sqlite3.Connection | None
                ) -> dict[str, Any] | None:
    """The empty payload when there is nothing to scope to, else ``None``."""

    if core is None:
        return _empty(payload, NO_CORE)
    mission = ctx.reader.mission(core)
    if mission is None:
        return _empty(payload, NO_MISSION)
    ctx.scope(core, mission)
    return None


def source_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    ref = _param(query, "claim", "要看哪一条结论的出处？")
    payload = _envelope("source")
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
        claim = _claim(core, ctx, ref)
        if claim is None:
            return _empty(payload, NOT_IN_MISSION)
        sources = [_public(item, ref, i) for i, item in enumerate(_sources(core, ctx, ref, claim))]
    available = any(item.get("passage") or any(link["kind"] == "original" for link in item.get("links") or ())
                    for item in sources)
    view = _claim_view(ctx, claim)
    stored = view.pop("statement_stored", None)
    payload.update({
        "claim": view, "sources": sources,
        "available_original": available,
        "button_label": "查看原文与出处" if available else "查看结论记录",
        "technical": {"claim_ref": ref, "claim_lineage_ref": claim.get("claim_ref"),
                      "prior_version_ref": claim.get("prior_version_ref"),
                      "statement_stored": stored},
    })
    if not sources:
        payload["sources_empty"] = _empty_parts(
            "这条结论没有登记可打开的证据记录", "只能看到结论本身，看不到原文",
            "系统入库时没有绑定原文；后续补充资料时会重新核对", f"按公司和期间在「{_term('company_section', 'evidence')}」里查找相关的申报原文")
        payload["sources_empty_reason"] = _empty_reason(payload["sources_empty"])
    return payload


def document_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    ref = _param(query, "claim", "要读哪一条结论引用的原文？")
    raw_index = _param(query, "source", "第几条来源？", required=False) or "0"
    if not raw_index.isdigit() or int(raw_index) > 999:
        raise EvidenceRequestError("参数 source 应该是来源的序号（从 0 开始）")
    index = int(raw_index)
    payload = _envelope("document")
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
        claim = _claim(core, ctx, ref)
        if claim is None:
            return _empty(payload, NOT_IN_MISSION)
        sources = _sources(core, ctx, ref, claim)
        if index >= len(sources):
            return _empty(payload, _empty_parts("这条结论没有这一条来源", "看不到对应的原文",
                                                "无需处理", "回到出处面板重新选择来源"))
        source = sources[index]
        internal = source["_internal"]
        payload.update({key: source.get(key) for key in (
            "document_type_label", "title", "house", "published_at", "filing", "relation", "relation_label")})
        payload["location_label"] = (source.get("location") or {}).get("label")
        payload["links"] = [link for link in source.get("links") or () if link["kind"] == "original"]
        payload["claim"] = {"statement": _claim_view(ctx, claim)["statement"], "company": ctx.company(
            claim.get("subject_ref"))}
        payload["technical"] = {"claim_ref": ref, **(source.get("technical") or {})}
        if internal.get("kind") == "sec":
            rows = core.execute(
                "SELECT line_id, ordinal, concept, label, level, period_start, period_end, value, unit "
                "FROM coverage_mission_statement_lines WHERE ingest_id=? AND statement=? "
                "ORDER BY ordinal LIMIT ?", (internal["ingest_id"], internal["statement"],
                                             MAX_STATEMENT_ROWS)).fetchall()
            payload["statement_label"] = STATEMENT_LABELS.get(internal["statement"], "财务报表")
            payload["rows"] = [{"ordinal": row["ordinal"], "label": _line_label(dict(row)), "level": row["level"],
                                "period_label": _period_text(row["period_start"], row["period_end"]),
                                "value_display": _value_display(row["value"], None, row["unit"]),
                                "cited": row["line_id"] == internal["line_id"]} for row in rows]
            payload["text"] = None
            return payload
        if internal.get("kind") != "document" or not internal.get("readable"):
            return _empty(payload, _empty_parts(
                source.get("passage_unavailable_reason") or "这份资料没有可阅读的存档原文",
                "只能看到出处信息，看不到全文", "无需处理；有原始链接时可直接打开原始出处",
                "用上面的原始链接打开，或回到出处面板"))
        text, _reason = ctx.text(internal["digest"])
        start, end = internal["start"], internal["end"]
        offset = 0
        if len(text) > MAX_DOCUMENT_CHARS and start is not None and end > MAX_DOCUMENT_CHARS:
            offset = max(0, start - MAX_DOCUMENT_CHARS // 2)
        shown = text[offset:offset + MAX_DOCUMENT_CHARS]
        payload.update({
            "text": shown, "text_offset": offset, "text_length": len(text),
            "truncated": offset > 0 or offset + MAX_DOCUMENT_CHARS < len(text),
            "highlight": None if start is None else {"start": start - offset, "end": end - offset},
            "lang": _lang(shown[:4000]), "rows": None,
        })
    return payload


def _claims_citing(core: sqlite3.Connection, ctx: _Context, line_id: str) -> list[str]:
    if not (_table(core, "evidence_relations") and _table(core, "evidence_versions")):
        return []
    retired = _retired(core)
    rows = core.execute(
        "SELECT DISTINCT r.claim_version_id, c.claim_json FROM evidence_versions e "
        "JOIN evidence_relations r ON r.evidence_version_id=e.evidence_version_id "
        "JOIN claim_versions c ON c.claim_version_id=r.claim_version_id "
        "WHERE instr(e.evidence_json, ?) > 0", (json.dumps(line_id),)).fetchall()
    out = []
    for row in rows:
        if row["claim_version_id"] in retired:
            continue
        if ctx.in_scope(json.loads(row["claim_json"]).get("subject_ref")):
            out.append(row["claim_version_id"])
    return sorted(out)


def statement_line_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    line = _param(query, "line", "要看哪一行申报数据？", required=False)
    if line and line.startswith("statement-line:"):
        line = line.split("statement-line:", 1)[1]
    company = accession = concept = period_end = None
    if not line:
        company = _param(query, "company", "要看哪一家公司的申报行？")
        accession = _param(query, "accession", "要看哪一份申报文件（申报编号）？")
        concept = _param(query, "concept", "要看哪一个报表科目？")
        period_end = _param(query, "period_end", "", required=False)
        if not _ACCESSION_RE.fullmatch(accession):
            raise EvidenceRequestError("参数 accession 应该是 SEC 申报编号，例如 0001467373-26-000032")
    payload = _envelope("statement_line")
    ctx = _Context(context)
    not_found = _empty_parts("申报文件里没有找到这一行", "无法打开这根柱子对应的报表原文",
                             "无需处理：这一期的报表可能还没有入库", f"换一个报告期，或在「{_term('company_section', 'evidence')}」里按公司查看申报原文")
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
        if not (_table(core, "coverage_mission_statement_lines")
                and _table(core, "coverage_mission_statement_filings")):
            return _empty(payload, not_found)
        if line:
            ids = [line]
        else:
            sql = ("SELECT l.line_id FROM coverage_mission_statement_lines l "
                   "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
                   "WHERE f.company_ref=? AND f.accession=? AND l.concept=? "
                   "AND (l.dimension_member IS NULL OR l.dimension_member='')")
            params: list[Any] = [company, accession, concept]
            if period_end:
                sql += " AND l.period_end=?"
                params.append(period_end)
            ids = [row[0] for row in core.execute(sql + " ORDER BY l.ordinal LIMIT 12", params).fetchall()]
        lines = []
        for line_id in ids:
            row = _line_row(core, line_id)
            if row is None or not ctx.in_scope(row.get("company_ref")):
                continue
            view = _sec_source(ctx, row)
            view.pop("_internal", None)
            view["company_ref"] = row.get("company_ref")
            view["claims"] = _claims_citing(core, ctx, line_id)
            lines.append(view)
        if not lines:
            return _empty(payload, not_found)
    payload["lines"] = lines
    payload["company"] = ctx.company(lines[0]["company_ref"])
    return payload


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

Builder = Callable[[Mapping[str, Any], Any], dict[str, Any]]


def _handler(kind: str, builder: Builder) -> Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]:
    def handle(query: Mapping[str, Any], context: Any) -> tuple[int, str, bytes]:
        from .charts_api import ChartRequestError

        try:
            return _json(200, builder(query or {}, context))
        except EvidenceRequestError as exc:
            return _json(400, {"error": "evidence_request", "message": str(exc), "display_message": str(exc)})
        except ChartRequestError:
            return _json(200, _empty(_envelope(kind), NO_CORE))
        except Exception as exc:  # noqa: BLE001 - never a 5xx: an honest empty state
            payload = _empty(_envelope(kind), READ_FAILED)
            payload["technical_reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return _json(200, payload)
    handle.__name__ = f"evidence_{kind}"
    return handle


def evidence_routes() -> dict[str, Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]]:
    return {SOURCE_PATH: _handler("source", source_payload),
            DOCUMENT_PATH: _handler("document", document_payload),
            STATEMENT_LINE_PATH: _handler("statement_line", statement_line_payload)}


def register(router: Any) -> None:
    for path, handler in evidence_routes().items():
        router.add_get(path, handler)


__all__ = ["DOCUMENT_PATH", "EvidenceRequestError", "SCHEMA_VERSION", "SOURCE_PATH",
           "STATEMENT_LINE_PATH", "document_payload", "evidence_routes", "register",
           "source_payload", "statement_line_payload"]
