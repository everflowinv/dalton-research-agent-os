"""Batch A (证据底座): every acquired document body, read back from disk.

The full-text evidence index (:mod:`evidence_index`) needs one question
answered for every document this Core ever acquired: *what does it say, and
what is it*.  Each acquisition child already answered half of that when it ran
-- it wrote a manifest naming the content-addressed spool object that holds the
bytes and the hash those bytes must have -- and the mission tables and the
provenance records answer the other half (whose document it is, which house
wrote it, when).  This module joins those answers, read-only:

* **AlphaEngine** (transcripts, broker reports, other library documents):
  ``acquisitions/<ticket>/manifest.json`` -> the assembled object, re-hashed
  against ``declared_content_sha256``; title, house, date and the document's
  own company list from ``document_provenance_records``.
* **Feeds** (sales notes, company wiki, prior research):
  ``feed-acquisitions-<feed>/<ticket>/manifest.json``, the same way.
* **Guidepoint** expert-call excerpts: ``guidepoint-acquire-runs/<ticket>``.
* **Public web fetches**, SEC filings among them:
  ``fetches/<ticket>/manifest.json`` and, beside an 8-K, the
  ``exhibit-manifest.json`` the exhibit re-detection fetched (Exhibit 99.1).
  The body bytes are re-hashed against ``body_sha256`` and rendered by the
  same renderer extraction uses (``public_web_extraction_source``); inline
  XBRL renders as the visible text it is, values included -- the hidden
  ``ix:header`` block (contexts and units, not prose) is dropped first.

Nothing here writes, calls a model or reaches the network.  A document whose
bytes are missing or do not hash to what its manifest declared is *skipped
with its reason*, never approximated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPOOL_DIRS = ("transcript-spool", "connector-spool", "raw-spool")
CORE_DB = "core.sqlite"

#: ``(family, ticket directory, manifest file name, source_ref or None)``.
#: ``None`` means the manifest names its own source.
ALPHAENGINE_DIR = "acquisitions"
GUIDEPOINT_DIR = "guidepoint-acquire-runs"
FETCH_DIR = "fetches"
FEED_PREFIX = "feed-acquisitions-"
FEED_SOURCE_REFS = {
    "feed-acquisitions-sales-notes": "source:sales-notes",
    "feed-acquisitions-company-wiki": "source:company-wiki",
    "feed-acquisitions-prior-research": "source:prior-research",
}

#: The closed kind vocabulary of an indexed document.
KINDS: tuple[str, ...] = (
    "sec_10k", "sec_10q", "sec_8k", "sec_exhibit", "sec_other",
    "transcript", "broker_report", "sales_note", "expert_call",
    "company_wiki", "prior_research", "web_page", "research_other",
)
KIND_LABELS: Mapping[str, str] = {
    "sec_10k": "年报 10-K", "sec_10q": "季报 10-Q", "sec_8k": "临时公告 8-K",
    "sec_exhibit": "SEC 申报附件", "sec_other": "SEC 申报文件",
    "transcript": "电话会纪要", "broker_report": "卖方研报", "sales_note": "卖方销售快报",
    "expert_call": "专家访谈", "company_wiki": "公司知识库", "prior_research": "本所既往研究",
    "web_page": "公开网页", "research_other": "研究资料",
}

#: The largest body this reader will render.  A Microsoft 10-K is 8.6 MB of
#: inline XBRL; anything much larger is not one document.
MAX_RAW_BYTES = 24_000_000
MAX_TEXT_CHARS = 6_000_000

_ACCESSION_PATH_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18})/([^/?#]+)$")
_IX_HEADER_RE = re.compile(rb"<ix:header\b.*?</ix:header>", re.IGNORECASE | re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]{3,300})\]\(")
_FIRST_LINE_SKIP_RE = re.compile(r"^\s*(?:\[cid:[^\]]*\]|_{5,}|-{5,}|={5,})\s*$")


class CorpusSkip(Exception):
    """This candidate is not an indexable document; the message says why."""


@dataclass(frozen=True)
class Candidate:
    """One manifest on disk that may name an acquired document body."""

    source_key: str
    family: str
    path: Path


@dataclass
class DocumentBody:
    """One acquired document, its exact text and what is known about it."""

    document_ref: str
    source_ref: str
    kind: str
    text: str
    text_sha256: str
    renderer: str
    title: str | None
    house: str | None
    document_date: str | None
    period: str | None
    url: str | None
    acquired_at: str | None
    filed_company_refs: list[str] = field(default_factory=list)
    named_companies: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def _ticket_dirs(root: Path) -> list[str]:
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(name for name in names if not name.startswith("."))


def iter_candidates(state_dir: str | Path) -> Iterator[Candidate]:
    """Every manifest that may name a body, cheapest to enumerate first.

    Listing directories is all this does; nothing is opened.  The caller's
    ledger decides which ones still need reading.
    """

    state = Path(state_dir)
    for name in _ticket_dirs(state / ALPHAENGINE_DIR):
        yield Candidate(f"alphaengine:{name}", "alphaengine",
                        state / ALPHAENGINE_DIR / name / "manifest.json")
    for feed_dir in sorted(p.name for p in state.glob(FEED_PREFIX + "*") if p.is_dir()):
        for name in _ticket_dirs(state / feed_dir):
            yield Candidate(f"{feed_dir}:{name}", "feed", state / feed_dir / name / "manifest.json")
    for name in _ticket_dirs(state / GUIDEPOINT_DIR):
        yield Candidate(f"guidepoint:{name}", "guidepoint",
                        state / GUIDEPOINT_DIR / name / "manifest.json")
    for name in _ticket_dirs(state / FETCH_DIR):
        yield Candidate(f"fetch:{name}", "fetch", state / FETCH_DIR / name / "manifest.json")
        yield Candidate(f"fetch-exhibit:{name}", "fetch_exhibit",
                        state / FETCH_DIR / name / "exhibit-manifest.json")
        yield Candidate(f"fetch-annual-exhibit:{name}", "fetch_annual_report_exhibit",
                        state / FETCH_DIR / name / "annual-report-exhibit-manifest.json")
        # Fixed bounded per-ticket attachment paths; full membership is re-proved
        # before any body can enter the corpus.
        for ordinal in range(8):
            path = state / FETCH_DIR / name / f'interim-attachment-{ordinal:03d}-manifest.json'
            if path.is_file():
                yield Candidate(f'fetch-interim:{name}:{ordinal}', 'fetch_interim_attachment', path)


# ---------------------------------------------------------------------------
# the reading context
# ---------------------------------------------------------------------------


def _open_core_readonly(state: Path) -> sqlite3.Connection | None:
    path = state / CORE_DB
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection | None, name: str) -> bool:
    if connection is None:
        return False
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def mission_universe(connection: sqlite3.Connection | None) -> tuple[list[dict[str, Any]], str | None]:
    """The active mission's universe and industry ref, or ``([], None)``."""

    if not (_table_exists(connection, "coverage_mission_pointer")
            and _table_exists(connection, "coverage_mission_versions")):
        return [], None
    try:
        row = connection.execute(
            "SELECT v.record_json FROM coverage_mission_pointer p JOIN coverage_mission_versions v "
            "ON v.mission_version_id=p.mission_version_id ORDER BY p.mission_ref LIMIT 1").fetchone()
    except sqlite3.Error:
        return [], None
    if row is None:
        return [], None
    try:
        mission = json.loads(row[0])
    except (TypeError, ValueError):
        return [], None
    universe = [dict(item) for item in mission.get("universe") or [] if isinstance(item, Mapping)]
    return universe, mission.get("industry_ref")


class CorpusContext:
    """One pass's read-only view of a state directory.

    Opened once per indexing tick: the spool readers, the read-only Core, the
    mission's name table, and caches of the per-document lookups.
    """

    def __init__(self, state_dir: str | Path, *, core: sqlite3.Connection | None = None) -> None:
        from .raw_spool import MultiRootRawSpoolReader, RawSpoolReader

        self.state = Path(state_dir)
        roots = []
        for name in SPOOL_DIRS:
            if not (self.state / name).is_dir():
                continue
            try:
                roots.append(RawSpoolReader(self.state / name))
            except Exception:  # noqa: BLE001 - a missing root is simply not read
                continue
        self.spool = MultiRootRawSpoolReader(roots) if roots else None
        self._own_core = core is None
        self.core = core if core is not None else _open_core_readonly(self.state)
        self.universe, self.industry_ref = mission_universe(self.core)
        self.ticker_to_company: dict[str, str] = {}
        for member in self.universe:
            ticker = str(member.get("ticker") or "").strip().upper()
            company_ref = member.get("company_ref")
            if ticker and isinstance(company_ref, str):
                self.ticker_to_company[ticker] = company_ref
        self.name_table = self._names()
        self._discovered: dict[str, list[dict[str, Any]]] | None = None
        self._discovered_by_ticket: dict[str, list[dict[str, Any]]] | None = None
        self._release_urls: dict[str, str] | None = None

    def close(self) -> None:
        if self._own_core and self.core is not None:
            self.core.close()
            self.core = None

    def __enter__(self) -> "CorpusContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- names ---------------------------------------------------------------
    def _names(self) -> dict[str, tuple[str, ...]]:
        if not self.universe:
            return {}
        from .document_subject import subject_names
        from .mission_company_names import mission_name_table

        try:
            table = mission_name_table(self.universe)
        except Exception:  # noqa: BLE001 - a malformed universe names nobody
            table = {}
        return {ticker: subject_names(ticker, table) for ticker in self.ticker_to_company}

    def companies_named(self, text: str) -> list[str]:
        """Covered company refs whose names this text uses (``document_subject`` rule)."""

        if not text or not self.name_table:
            return []
        from .document_subject import _mentions

        found = []
        for ticker, names in self.name_table.items():
            if names and _mentions(text, names):
                found.append(self.ticker_to_company[ticker])
        return found

    def company_label(self, company_ref: str) -> str | None:
        """The first name the mission's name table gives a covered company."""

        for ticker, ref in self.ticker_to_company.items():
            if ref == company_ref:
                names = [name for name in self.name_table.get(ticker, ()) if name.upper() != ticker]
                return names[0] if names else ticker
        return None

    def companies_for_tickers(self, tickers: Sequence[Any]) -> list[str]:
        found = []
        for ticker in tickers or ():
            ref = self.ticker_to_company.get(str(ticker or "").strip().upper())
            if ref and ref not in found:
                found.append(ref)
        return found

    def companies_for_names(self, names: Sequence[Any]) -> list[str]:
        """The covered companies a document's own company list names."""

        text = " | ".join(str(name) for name in names or () if name)
        return self.companies_named(text)

    # -- bytes ---------------------------------------------------------------
    def read_verified(self, digest: Any, *, max_bytes: int = MAX_RAW_BYTES) -> bytes:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CorpusSkip("清单没有登记可核对的内容指纹")
        if self.spool is None:
            raise CorpusSkip("这台机器上没有原始文件库")
        try:
            raw = self.spool.read_object(digest)
        except Exception as exc:  # noqa: BLE001 - pruned or never stored here
            raise CorpusSkip(f"原始文件库里找不到这份文件（{type(exc).__name__}）") from exc
        if len(raw) > max_bytes:
            raise CorpusSkip("文件超过索引读取上限")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise CorpusSkip("原始文件与清单登记的指纹不一致，拒绝索引")
        return raw

    # -- mission lookups -----------------------------------------------------
    def _load_discovered(self) -> None:
        self._discovered, self._discovered_by_ticket = {}, {}
        if not _table_exists(self.core, "coverage_mission_discovered_documents"):
            return
        try:
            rows = self.core.execute(
                "SELECT company_ref, source_ref, document_ref, ticket_ref, discovery_ref, status, created_at "
                "FROM coverage_mission_discovered_documents").fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            item = dict(row)
            self._discovered.setdefault(item["document_ref"], []).append(item)
            if item.get("ticket_ref"):
                self._discovered_by_ticket.setdefault(item["ticket_ref"], []).append(item)

    def discovered(self, document_ref: str) -> list[dict[str, Any]]:
        if self._discovered is None:
            self._load_discovered()
        return list(self._discovered.get(document_ref, ()))

    def discovered_by_ticket(self, ticket_ref: str) -> list[dict[str, Any]]:
        if self._discovered_by_ticket is None:
            self._load_discovered()
        return list(self._discovered_by_ticket.get(ticket_ref, ()))

    def provenance(self, document_ref: str) -> dict[str, Any] | None:
        if not _table_exists(self.core, "document_provenance_records"):
            return None
        try:
            row = self.core.execute(
                "SELECT record_json FROM document_provenance_records WHERE document_ref=?",
                (document_ref,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def release_exhibit_urls(self) -> dict[str, str]:
        """Exhibit URL -> fiscal period, from the earnings-release reads."""

        if self._release_urls is None:
            self._release_urls = {}
            if _table_exists(self.core, "earnings_release_reads"):
                try:
                    for row in self.core.execute(
                            "SELECT exhibit_url, period FROM earnings_release_reads "
                            "WHERE exhibit_url IS NOT NULL"):
                        self._release_urls[row["exhibit_url"]] = row["period"]
                except sqlite3.Error:
                    pass
        return self._release_urls

    def sec_identity(self, accession: str, discovery_ref: str | None) -> dict[str, Any] | None:
        from .acquired_sec_filings import _statement_ledger_row, _submissions_row

        if self.core is None:
            return None
        try:
            identity = (_submissions_row(self.state, self.core, discovery_ref, accession)
                        or _statement_ledger_row(self.core, accession))
        except Exception:  # noqa: BLE001 - unattributable is a skip, not a failure
            return None
        if identity is None:
            return None
        report_date = None
        if _table_exists(self.core, "coverage_mission_statement_filings"):
            try:
                row = self.core.execute(
                    "SELECT report_date FROM coverage_mission_statement_filings WHERE accession=? "
                    "ORDER BY recorded_at DESC LIMIT 1", (accession,)).fetchone()
                report_date = row["report_date"] if row else None
            except sqlite3.Error:
                report_date = None
        return {**identity, "report_date": report_date}


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------


def _day(value: Any) -> str | None:
    text = str(value or "")
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def _first_line(text: str, *, maximum: int = 200) -> str | None:
    for line in text.splitlines()[:40]:
        line = line.strip()
        if not line or _FIRST_LINE_SKIP_RE.match(line) or len(line) < 4:
            continue
        return line[:maximum]
    return None


_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bQ([1-4])\s*(?:FY\s?)?((?:19|20)\d{2})\b", re.I), "q_year"),
    (re.compile(r"\b((?:19|20)\d{2})\s*Q([1-4])\b", re.I), "year_q"),
    (re.compile(r"\b([1-4])Q\s?(?:FY)?\s?'?(\d{2})\b", re.I), "nq_yy"),
    (re.compile(r"\bQ([1-4])\s?(?:FY)?\s?'?(\d{2})\b", re.I), "q_yy"),
    (re.compile(r"\b(first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?((?:19|20)\d{2})\b", re.I), "word_q"),
    (re.compile(r"\bFY\s?'?((?:19|20)?\d{2})\b", re.I), "fy"),
    (re.compile(r"\bfiscal\s+(?:year\s+)?((?:19|20)\d{2})\b", re.I), "fiscal"),
)
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def period_label(text: Any) -> str | None:
    """A fiscal-period label as the text writes it: ``FY2026-Q4`` or ``FY2027``.

    The quarter is the document's own numbering; no calendar mapping is
    attempted, so a calendar-year issuer's "Q2 2026" stays ``FY2026-Q2``.
    """

    if not isinstance(text, str) or not text:
        return None
    for pattern, shape in _PERIOD_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        if shape == "q_year":
            return f"FY{match.group(2)}-Q{match.group(1)}"
        if shape == "year_q":
            return f"FY{match.group(1)}-Q{match.group(2)}"
        if shape in ("nq_yy", "q_yy"):
            return f"FY20{match.group(2)}-Q{match.group(1)}"
        if shape == "word_q":
            return f"FY{match.group(2)}-Q{_QUARTER_WORDS[match.group(1).lower()]}"
        year = match.group(1)
        return f"FY{year if len(year) == 4 else '20' + year}"
    return None


_FORM_TEXT_RE = re.compile(r"\bFORM\s+(10-K(?:/A)?|10-Q(?:/A)?|8-K(?:/A)?|DEF\s?14A|S-8|S-3|S-4|11-K|SD|"
                           r"SC\s?13[DG](?:/A)?|20-F|6-K|4|3|144)\b")
_ISSUER_TEXT_RE = re.compile(r"\n\s*([A-Z][A-Za-z0-9.,&' -]{2,80}?)\s*\n+\s*\(Exact name of registrant", re.I)


def _form_from_text(text: str) -> str:
    """The form a filing's cover page names, when no ledger says it (e.g. ``FORM 10-Q``)."""

    match = _FORM_TEXT_RE.search(text[:6000])
    return re.sub(r"\s+", " ", match.group(1)).upper() if match else ""


def _issuer_from_text(text: str) -> str | None:
    match = _ISSUER_TEXT_RE.search(text[:8000])
    return match.group(1).strip() if match else None


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusSkip("文件不是 UTF-8 文字，未索引") from exc


def render_fetched_body(raw: bytes, media_type: str) -> tuple[str, str]:
    """The text a fetched body is indexed as, and the renderer that made it.

    The extraction renderer, unchanged, over the body with the inline-XBRL
    ``ix:header`` (hidden contexts/units) removed.  A page whose declared
    charset the renderer refuses is re-tried as UTF-8 when its bytes are UTF-8.
    """

    from .public_web_extraction_source import PublicWebSourceError, render_public_web_text

    stripped, removed = _IX_HEADER_RE.subn(b"", raw) if b"<ix:header" in raw else (raw, 0)
    renderer_suffix = "+strip-ix-header" if removed else ""
    media = media_type or "text/html"
    try:
        rendered = render_public_web_text(stripped, raw_media_type=media, max_source_chars=MAX_TEXT_CHARS,
                                          max_decompressed_bytes=MAX_RAW_BYTES)
    except PublicWebSourceError as exc:
        if "charset" not in str(exc):
            raise CorpusSkip(f"无法把这份网页渲染成文字：{exc}") from exc
        base = media.split(";", 1)[0].strip() or "text/html"
        try:
            stripped.decode("utf-8")
        except UnicodeDecodeError:
            stripped = stripped.decode("latin-1").encode("utf-8")
            renderer_suffix += "+latin1"
        try:
            rendered = render_public_web_text(stripped, raw_media_type=f"{base}; charset=utf-8",
                                              max_source_chars=MAX_TEXT_CHARS,
                                              max_decompressed_bytes=MAX_RAW_BYTES)
        except PublicWebSourceError as retry:
            raise CorpusSkip(f"无法把这份网页渲染成文字：{retry}") from retry
    return rendered["text"], f"{rendered['renderer']}{renderer_suffix}"


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# per-family loaders
# ---------------------------------------------------------------------------


_SPEC_KINDS = {
    "earnings-call-transcripts": "transcript",
    "sell-side-reports": "broker_report",
    "expert-network-transcripts": "expert_call",
    "sales-notes": "sales_note",
}


def _alphaengine(ctx: CorpusContext, manifest: Mapping[str, Any]) -> DocumentBody:
    if manifest.get("status") != "complete":
        raise CorpusSkip("AlphaEngine 获取没有完成")
    document_ref = str(manifest.get("document_ref") or "")
    if not document_ref.startswith("alphaengine-doc:"):
        raise CorpusSkip("清单没有 AlphaEngine 文档编号")
    digest = (manifest.get("assembled_object") or {}).get("content_hash")
    raw = ctx.read_verified(digest)
    if digest != manifest.get("declared_content_sha256"):
        raise CorpusSkip("拼装后的正文与声明的指纹不一致")
    text = _decode(raw)
    provenance = ctx.provenance(document_ref) or {}
    spec = provenance.get("spec_ref")
    tier = provenance.get("provenance_tier")
    kind = _SPEC_KINDS.get(spec) or {"management": "transcript", "sell_side": "broker_report",
                                     "expert": "expert_call"}.get(tier) or "research_other"
    title = provenance.get("title") or _first_line(text)
    house = provenance.get("broker") or provenance.get("sources")
    named = [str(name) for name in provenance.get("named_companies") or () if name]
    filed = sorted({row["company_ref"] for row in ctx.discovered(document_ref)})
    return DocumentBody(
        document_ref=document_ref, source_ref="source:alphaengine", kind=kind, text=text,
        text_sha256=_text_sha(text), renderer="alphaengine-assembled:0.1", title=title,
        house=house, document_date=_day(provenance.get("published_at")) or _day(manifest.get("created_at")),
        period=period_label(title), url=None, acquired_at=manifest.get("created_at"),
        filed_company_refs=filed, named_companies=named,
        metadata={"spec_ref": spec, "provenance_tier": tier, "authors": provenance.get("authors")},
    )


def _feed(ctx: CorpusContext, candidate: Candidate, manifest: Mapping[str, Any]) -> DocumentBody:
    if manifest.get("status") != "complete":
        raise CorpusSkip("资料获取没有完成")
    document_ref = str(manifest.get("document_ref") or "")
    if not document_ref:
        raise CorpusSkip("清单没有文档编号")
    digest = (manifest.get("assembled_object") or {}).get("content_hash")
    raw = ctx.read_verified(digest)
    if digest != manifest.get("declared_content_sha256"):
        raise CorpusSkip("正文与声明的指纹不一致")
    text = _decode(raw)
    feed_dir = candidate.source_key.split(":", 1)[0]
    source_ref = manifest.get("source_ref") or FEED_SOURCE_REFS.get(feed_dir) or "source:feed"
    kind = {"source:sales-notes": "sales_note", "source:company-wiki": "company_wiki",
            "source:prior-research": "prior_research"}.get(source_ref, "research_other")
    provenance = ctx.provenance(document_ref) or {}
    title = provenance.get("title") or manifest.get("title") or _first_line(text)
    filed = set(row["company_ref"] for row in ctx.discovered(document_ref))
    filed.update(ctx.companies_for_tickers(manifest.get("subject_tickers") or ()))
    return DocumentBody(
        document_ref=document_ref, source_ref=source_ref, kind=kind, text=text,
        text_sha256=_text_sha(text), renderer="feed-assembled:0.1", title=title,
        house=provenance.get("broker"),
        document_date=_day(manifest.get("doc_date")) or _day(provenance.get("published_at"))
        or _day(manifest.get("created_at")),
        period=period_label(title), url=None, acquired_at=manifest.get("created_at"),
        filed_company_refs=sorted(filed),
        named_companies=[str(n) for n in provenance.get("named_companies") or () if n],
        metadata={"doc_kind": manifest.get("doc_kind"), "evidence_tier": manifest.get("evidence_tier"),
                  "origin_ref": manifest.get("origin_ref")},
    )


def _guidepoint(ctx: CorpusContext, manifest: Mapping[str, Any]) -> DocumentBody:
    if manifest.get("status") != "complete":
        raise CorpusSkip("专家访谈摘录获取没有完成")
    document_ref = str(manifest.get("document_ref") or "")
    if not document_ref:
        raise CorpusSkip("清单没有文档编号")
    digest = (manifest.get("excerpt_object") or {}).get("content_hash")
    raw = ctx.read_verified(digest)
    if digest != manifest.get("declared_content_sha256"):
        raise CorpusSkip("摘录与声明的指纹不一致")
    text = _decode(raw)
    attribution = str(manifest.get("source_attribution_markdown") or "")
    match = _MD_LINK_RE.search(attribution)
    title = match.group(1) if match else _first_line(text)
    respondent = manifest.get("respondent")
    filed = sorted({row["company_ref"] for row in ctx.discovered(document_ref)})
    return DocumentBody(
        document_ref=document_ref, source_ref="source:guidepoint", kind="expert_call", text=text,
        text_sha256=_text_sha(text), renderer="guidepoint-excerpt:0.1", title=title,
        house="Guidepoint", document_date=_day(manifest.get("excerpt_date")) or _day(manifest.get("created_at")),
        period=period_label(title), url=manifest.get("reference_url"), acquired_at=manifest.get("created_at"),
        filed_company_refs=filed, named_companies=[],
        metadata={"respondent": respondent, "speakers": [respondent] if respondent else [],
                  "excerpt_ordinal": manifest.get("excerpt_ordinal"),
                  "quote_policy": manifest.get("quote_policy")},
    )


_FORM_KINDS = {"10-K": "sec_10k", "10-K/A": "sec_10k", "10-Q": "sec_10q", "10-Q/A": "sec_10q",
               "8-K": "sec_8k", "8-K/A": "sec_8k", "6-K": "sec_6k", "6-K/A": "sec_6k",
               "20-F": "sec_20f", "20-F/A": "sec_20f", "40-F": "sec_40f", "40-F/A": "sec_40f"}


def _fetch(ctx: CorpusContext, candidate: Candidate, manifest: Mapping[str, Any]) -> DocumentBody:
    annual = candidate.family == "fetch_annual_report_exhibit"
    interim = candidate.family == 'fetch_interim_attachment'
    exhibit = candidate.family in {"fetch_exhibit", "fetch_annual_report_exhibit", 'fetch_interim_attachment'}
    ticket_ref = "public-web-fetch:" + candidate.path.parent.name
    attachment = None
    if annual:
        from types import SimpleNamespace
        from .connector_authority_port import ReadOnlyConnectorReceiptReader
        from .sec_annual_report_exhibit import verified_exhibit_source
        parents = [r for r in ctx.discovered_by_ticket(ticket_ref)
                   if r['source_ref'] == 'source:sec-edgar' and r['status'] == 'acquired'
                   and str(r['document_ref']).startswith('sec:filing:')]
        if not parents or ctx.core is None:
            raise CorpusSkip('年报附件没有已获取的母申报来源')
        try:
            for parent in parents:
                exact = verified_exhibit_source(SimpleNamespace(connection=ctx.core),ctx.spool,ctx.state,
                    ticket_ref=ticket_ref,parent_document_ref=parent['document_ref'],company_ref=parent['company_ref'],
                    receipt_reader=ReadOnlyConnectorReceiptReader(ctx.core),max_source_chars=MAX_TEXT_CHARS)
                if exact is None or exact['manifest'] != manifest:
                    raise CorpusSkip('年报附件清单与已取得来源不一致')
                attachment = exact
        except Exception as exc:
            raise CorpusSkip('年报附件来源无法复核：'+str(exc)) from exc
    if interim:
        from types import SimpleNamespace
        from .connector_authority_port import ReadOnlyConnectorReceiptReader
        from .sec_interim_attachments import verified_sources
        parents = [r for r in ctx.discovered_by_ticket(ticket_ref)
                   if r['source_ref'] == 'source:sec-edgar' and r['status'] == 'acquired'
                   and str(r['document_ref']).startswith('sec:filing:')]
        if not parents or ctx.core is None:
            raise CorpusSkip('外国披露附件没有已取得的原申报')
        exacts = []
        try:
            for parent in parents:
                sources = verified_sources(SimpleNamespace(connection=ctx.core), ctx.spool, ctx.state,
                    ticket_ref=ticket_ref, parent_document_ref=parent['document_ref'], company_ref=parent['company_ref'],
                    receipt_reader=ReadOnlyConnectorReceiptReader(ctx.core), max_source_chars=MAX_TEXT_CHARS)
                choices = [item for item in sources if item['manifest'] == manifest]
                if len(choices) != 1: raise CorpusSkip('外国披露附件清单不属于完整原件集合')
                exacts.append(choices[0])
        except Exception as exc:
            raise CorpusSkip('外国披露附件来源无法复核：' + str(exc)) from exc
        attachment = exacts[0]
    if int(manifest.get("body_bytes") or 0) > MAX_RAW_BYTES:
        raise CorpusSkip("网页正文超过索引读取上限")
    digest = manifest.get("body_sha256")
    raw = ctx.read_verified(digest)
    text, renderer = render_fetched_body(raw, str(manifest.get("raw_media_type") or "text/html"))
    if not text.strip():
        raise CorpusSkip("网页渲染后没有可读文字")
    url = str(manifest.get("canonical_url") or manifest.get("final_url") or "") or None
    rows = ctx.discovered_by_ticket(ticket_ref)
    if annual or interim: rows = parents
    filed = sorted({row["company_ref"] for row in rows})
    mission_ref = next((row["document_ref"] for row in rows), None)
    host = str(manifest.get("host") or "")
    sec_match = _ACCESSION_PATH_RE.search((url or "").split("?", 1)[0]) if host.endswith("sec.gov") else None
    acquired_at = manifest.get("created_at")
    if sec_match is not None:
        digits = sec_match.group(2)
        accession = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
        filename = sec_match.group(3)
        sec_row = next((row for row in rows if str(row["document_ref"]).startswith("sec:filing:")), None)
        identity = ctx.sec_identity(accession, sec_row["discovery_ref"] if sec_row else None) or {}
        form = str(identity.get("form") or "").upper() or _form_from_text(text)
        issuer = (identity.get("issuer_name") or (ctx.company_label(filed[0]) if filed else None)
                  or _issuer_from_text(text))
        filed_date = identity.get("filing_date")
        if exhibit:
            release_period = ctx.release_exhibit_urls().get(url or "")
            exhibit_no = None if interim else "13" if annual else ("99.1" if release_period is not None or re.search(r"ex[-_]?99|exhibit99|99-?1", filename, re.I) else None)
            document_ref = attachment['document_ref'] if interim else f"sec:exhibit:{accession}:{filename}"
            kind = 'sec_6k' if interim else "sec_exhibit"
            title = " ".join(part for part in (issuer, form or "8-K", f"Exhibit {exhibit_no}" if exhibit_no else "exhibit",
                                                filed_date) if part)
            period = None if interim else (f"PE{identity['report_date']}" if annual and identity.get('report_date') else release_period or period_label(text[:3000]))
        else:
            primary = identity.get("primary_document")
            document_ref = (f"sec:filing:{accession}" if not primary or primary == filename
                            else f"sec:exhibit:{accession}:{filename}")
            kind = _FORM_KINDS.get(form, "sec_other") if document_ref.startswith("sec:filing:") else "sec_exhibit"
            title = " ".join(part for part in (issuer, form or "SEC filing", filed_date) if part)
            period = (f"PE{identity['report_date']}" if identity.get("report_date") and kind in ("sec_10k", "sec_10q")
                      else period_label(text[:3000]))
        if not filed and identity.get("issuer_name"):
            filed = ctx.companies_for_names([identity["issuer_name"]])
        return DocumentBody(
            document_ref=document_ref, source_ref="source:sec-edgar", kind=kind, text=text,
            text_sha256=_text_sha(text), renderer=renderer, title=title or filename, house=issuer,
            document_date=_day(filed_date) or _day(acquired_at), period=period, url=url,
            acquired_at=acquired_at, filed_company_refs=filed, named_companies=[],
            metadata={"accession": accession, "form": form or None, "filename": filename,
                      "body_sha256": digest, "fetch_document_ref": manifest.get("document_ref"),
                      "mission_document_ref": mission_ref, "exhibit": exhibit,
                      **({('interim_attachment' if interim else 'annual_report_attachment'): attachment['binding']} if attachment else {}),
                      **({'financial_periods_inferred': False} if interim else {})},
        )
    document_ref = mission_ref or str(manifest.get("document_ref") or "")
    if not document_ref:
        raise CorpusSkip("网页清单没有文档编号")
    title = _first_line(text)
    created = min((row["created_at"] for row in rows if row.get("created_at")), default=None)
    return DocumentBody(
        document_ref=document_ref, source_ref="source:web-search" if rows else "source:public-web",
        kind="web_page", text=text, text_sha256=_text_sha(text), renderer=renderer, title=title,
        house=host or None, document_date=_day(acquired_at) or _day(created), period=period_label(title),
        url=url, acquired_at=acquired_at, filed_company_refs=filed, named_companies=[],
        metadata={"body_sha256": digest, "fetch_document_ref": manifest.get("document_ref"),
                  "date_basis": "fetched_at"},
    )


def load_document(ctx: CorpusContext, candidate: Candidate) -> DocumentBody:
    """The document a candidate manifest names, verified; or :class:`CorpusSkip`."""

    if not candidate.path.is_file():
        raise CorpusSkip("没有完成的清单")
    try:
        manifest = json.loads(candidate.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusSkip(f"清单无法读取：{type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise CorpusSkip("清单不是对象")
    if candidate.family == "alphaengine":
        body = _alphaengine(ctx, manifest)
    elif candidate.family == "feed":
        body = _feed(ctx, candidate, manifest)
    elif candidate.family == "guidepoint":
        body = _guidepoint(ctx, manifest)
    elif candidate.family in ("fetch", "fetch_exhibit", "fetch_annual_report_exhibit", 'fetch_interim_attachment'):
        body = _fetch(ctx, candidate, manifest)
    else:
        raise CorpusSkip(f"未知的资料类型 {candidate.family}")
    if len(body.text) > MAX_TEXT_CHARS:
        raise CorpusSkip("正文超过索引上限")
    if not body.text.strip():
        raise CorpusSkip("正文为空")
    # The document's own company list names covered companies too.
    for ref in ctx.companies_for_names(body.named_companies):
        if ref not in body.filed_company_refs:
            body.metadata.setdefault("listed_company_refs", []).append(ref)
    return body


def candidate_chars(candidate: Candidate) -> int | None:
    """What a manifest says its body measures, without reading the body."""

    try:
        manifest = json.loads(candidate.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    value = manifest.get("content_chars") or manifest.get("body_bytes")
    return int(value) if isinstance(value, int) else None


__all__ = [
    "Candidate", "CorpusContext", "CorpusSkip", "DocumentBody", "KINDS", "KIND_LABELS",
    "candidate_chars", "iter_candidates", "load_document", "mission_universe", "period_label",
    "render_fetched_body",
]
