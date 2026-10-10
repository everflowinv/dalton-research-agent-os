"""Batch A (证据底座): passage-level full-text evidence index, search and citations.

The diagnosis of 2026-10-09 found the drafting steps reading only number-free
one-sentence Claims: ACN's ~390 documents and 5.1 M characters reached the
Initial Screen as 120 sentences.  This module is the base layer that fixes
that: every acquired document body (:mod:`evidence_corpus`) is cut into
~1.5 K-character passages with overlap, each passage carrying what an analyst
needs to cite it -- document, company, kind, house or speaker, date, fiscal
period, title, section heading, exact character range and content hash -- and
indexed in SQLite FTS5 inside ``document-index.sqlite``.

Four public surfaces, none of which calls a model or the network:

``index_tick(state_dir, ...)``
    Incremental and resumable.  Each call indexes at most ``max_documents``
    new manifests or ``max_seconds`` of work, newest acquisition first, and
    records every manifest it looked at in ``evidence_index_ledger``.  The
    ``document_index`` lane (:mod:`document_index_lane`) calls it every
    controller tick, which is both "index on acquisition completion" (a new
    manifest is picked up the next tick) and the bounded first-run backfill.
``evidence_search(state_dir, query, ...)``
    BM25 (title / heading / body weighted) x query-term coverage x recency x
    kind weight, with highlights.  ``passages_for_document`` and ``passage``
    read passages back by document or by citation ref.
``doc-passage:<document_ref>:<start>-<end>:<hash12>``
    The citation ref.  ``hash12`` is the first 12 hex digits of the SHA-256 of
    the exact cited text, so a ref whose span or text has been altered no
    longer resolves (:class:`PassageTampered`).  A ref may name any span
    *inside* an indexed passage, so a citation can be one sentence.
``material_arrivals``
    An append-only ledger: one row per (company, document, body hash) the
    first time the index holds it.  The analyst workflow is "collect a batch,
    then draft; redraft only after new material accumulates": this is the
    accumulating fact, and the threshold policy that reads it belongs to the
    batch that drafts, not here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .evidence_corpus import (
    KIND_LABELS,
    KINDS,
    Candidate,
    CorpusContext,
    CorpusSkip,
    DocumentBody,
    iter_candidates,
    load_document,
)

SCHEMA_VERSION = "evidence-index:0.1"
INDEX_DB = "document-index.sqlite"
_SCHEMA_PATH = Path(__file__).with_name("document_index_schema.sql")

CHUNK_CHARS = 1500
OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 300
#: The default per-tick bound.  A tick runs on the writer's thread, so it is
#: kept to a few seconds; the legacy backfill (~8 K manifests) completes in a
#: few hundred ticks and is resumable at any point.
TICK_MAX_DOCUMENTS = 40
TICK_MAX_SECONDS = 12.0
#: A manifest whose read raised something unexpected is retried this many times,
#: no sooner than this many seconds apart.
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 3600

#: How much one kind of source is worth when relevance is otherwise equal.
#: The owner's evidence order: what the company filed or said, then the
#: brokers who cover it, then the sales desk and the experts, then the web.
KIND_WEIGHTS: Mapping[str, float] = {
    "sec_exhibit": 1.0, "transcript": 1.0, "sec_8k": 0.9, "sec_10k": 0.95, "sec_10q": 0.95,
    "sec_other": 0.75, "broker_report": 0.9, "sales_note": 0.7, "expert_call": 0.75,
    "company_wiki": 0.6, "prior_research": 0.65, "web_page": 0.6, "research_other": 0.7,
}
RECENCY_HALF_LIFE_DAYS = 180.0
RECENCY_FLOOR = 0.35
MAX_QUERY_CHARS = 400
MAX_LIMIT = 100
SNIPPET_CHARS = 320

PASSAGE_REF_PREFIX = "doc-passage:"
_PASSAGE_REF_RE = re.compile(
    r"^doc-passage:(?P<doc>[^\s]+):(?P<start>\d{1,9})-(?P<end>\d{1,9}):(?P<hash>[0-9a-f]{12})$")
_CJK_CHAR_RE = re.compile(r"([㐀-䶿一-鿿豈-﫿])")
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


class EvidenceIndexError(Exception):
    """Base error of the evidence index."""


class PassageRefInvalid(EvidenceIndexError):
    """The string is not a ``doc-passage:`` ref."""


class PassageTampered(EvidenceIndexError):
    """The ref names an indexed span, but its text no longer hashes to the ref."""


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------


def index_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / INDEX_DB


def open_index(state_dir: str | Path) -> sqlite3.Connection:
    """The writable index, created with its schema if absent (owner-only file)."""

    path = index_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def open_index_readonly(state_dir: str | Path) -> sqlite3.Connection | None:
    """The index for reading, or ``None`` when this environment has none yet."""

    path = index_path(state_dir)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("SELECT 1 FROM evidence_passages LIMIT 1")
    except sqlite3.Error:
        connection.close()
        return None
    return connection


@contextmanager
def _readonly(state_dir: str | Path) -> Iterator[sqlite3.Connection | None]:
    connection = open_index_readonly(state_dir)
    try:
        yield connection
    finally:
        if connection is not None:
            connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fts_text(value: Any) -> str:
    """What the FTS table indexes: CJK characters one per token."""

    return _CJK_CHAR_RE.sub(r" \1 ", str(value or ""))


# ---------------------------------------------------------------------------
# chunking, headings, speakers
# ---------------------------------------------------------------------------


_SENTENCE_END_RE = re.compile(r"[.!?。！？；;](?=[\s\"”’)\]]|[A-Z一-鿿]|$)")


def _skip_ws(text: str, position: int) -> int:
    n = len(text)
    while position < n and text[position].isspace():
        position += 1
    return position


def _best_break(text: str, lo: int, hi: int, ideal: int) -> int | None:
    """The end of a passage near ``ideal``: paragraph, line, sentence, then space."""

    lo = max(lo, 0)
    hi = min(hi, len(text))
    if hi <= lo:
        return None
    window = text[lo:hi]
    for pattern in (r"\n\s*\n", r"\n", None, r"\s"):
        if pattern is None:
            positions = [lo + m.end() for m in _SENTENCE_END_RE.finditer(window)]
        else:
            positions = [lo + m.start() for m in re.finditer(pattern, window)]
        if positions:
            return min(positions, key=lambda pos: abs(pos - ideal))
    return None


def _next_start(text: str, lo: int, hi: int) -> int:
    """Where the overlapping next passage starts: a sentence or line start in [lo, hi)."""

    window = text[lo:hi]
    for pattern in (r"\n", None, r"\s"):
        if pattern is None:
            found = [lo + m.end() for m in _SENTENCE_END_RE.finditer(window)]
        else:
            found = [lo + m.end() for m in re.finditer(pattern, window)]
        found = [pos for pos in found if pos < hi]
        if found:
            return found[0]
    return lo


def chunk_spans(text: str, *, target: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS,
                min_chars: int = MIN_CHUNK_CHARS) -> list[tuple[int, int]]:
    """Character spans of ~``target`` chars, overlapping by ~``overlap``.

    Spans start and end on paragraph, line or sentence boundaries when one is
    near, never inside leading/trailing whitespace, and cover every non-space
    character of ``text``.
    """

    n = len(text)
    spans: list[tuple[int, int]] = []
    start = _skip_ws(text, 0)
    while start < n:
        if n - start <= target + min_chars // 2:
            end = n
        else:
            ideal = start + target
            end = _best_break(text, start + target - 350, start + target + 200, ideal) or ideal
            if end <= start + min_chars:
                end = ideal
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            break
        spans.append((start, end))
        if end >= len(text.rstrip()):
            break
        nxt = _next_start(text, max(end - overlap, start + 1), end)
        nxt = _skip_ws(text, max(nxt, start + 1))
        if nxt <= start:
            nxt = end
        start = nxt
    return spans


_SPEAKER_RE = re.compile(r"(?m)^[ \t]*发言人[ \t]*([^：:\n]{1,80})[：:]")
_EN_SPEAKER_RE = re.compile(
    r"(?m)^[ \t]*([A-Z][A-Za-z.'\-]+(?:[ \t]+[A-Z][A-Za-z.'\-]+){0,4})"
    r"(?:[ \t]*[-–—,][ \t]*[^:\n]{0,80})?:[ \t]")
_SEC_HEADING_RE = re.compile(r"(?im)^[ \t]*((?:ITEM[ \t]+\d{1,2}[A-C]?\.?|PART[ \t]+I{1,3}V?\b)[^\n]{0,120})$")
_MD_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,4}[ \t]+([A-Z0-9一-鿿][^\n]{1,99})$")
_LINE_RE = re.compile(r"(?m)^[ \t]*([^\n]{4,80})[ \t]*$")
_NOT_HEADING_END = tuple(".,;:!?。，；：、)")


def _title_like(line: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'&\-]*", line)
    if len(words) < 2 or line.endswith(_NOT_HEADING_END):
        return False
    if line.isupper():
        return True
    small = {"and", "of", "the", "to", "in", "on", "for", "a", "an", "vs", "with", "at", "by", "or"}
    capital = sum(1 for word in words if word[0].isupper() or word.lower() in small)
    return capital / len(words) >= 0.75 and sum(1 for w in words if w[0].isupper()) >= 2


def headings(text: str, kind: str) -> list[tuple[int, str, str | None]]:
    """``(offset, heading, speaker)`` markers, in order.

    A transcript's markers are its speaker turns; a filing's are its Items;
    anything else gets Markdown headings and short title-case lines set off by
    a blank line (a broker's section titles), conservatively.
    """

    marks: list[tuple[int, str, str | None]] = []
    if kind in ("transcript", "expert_call"):
        for match in _SPEAKER_RE.finditer(text):
            name = match.group(1).strip()
            marks.append((match.start(), f"发言人 {name}", name))
        if not marks:
            for match in _EN_SPEAKER_RE.finditer(text):
                name = match.group(1).strip()
                if name.lower() in {"note", "source", "disclaimer", "question", "answer"}:
                    continue
                marks.append((match.start(), name, name))
        return marks
    if kind.startswith("sec_"):
        for match in _SEC_HEADING_RE.finditer(text):
            marks.append((match.start(), re.sub(r"\s+", " ", match.group(1)).strip()[:120], None))
        return marks
    for match in _MD_HEADING_RE.finditer(text):
        marks.append((match.start(), match.group(1).strip(), None))
    for match in _LINE_RE.finditer(text):
        start = match.start()
        line = match.group(1).strip()
        if line.startswith("#"):
            continue
        # A section title stands alone: a blank line before it (or the start).
        if start != 0 and re.search(r"\n[ \t]*\n$", text[max(0, start - 12):start]) is None:
            continue
        if _title_like(line):
            marks.append((start, line[:120], None))
    marks.sort(key=lambda item: item[0])
    return marks


def _passage_heading(marks: Sequence[tuple[int, str, str | None]], start: int, end: int
                     ) -> tuple[str | None, list[str]]:
    heading = None
    speakers: list[str] = []
    for offset, label, speaker in marks:
        if offset <= start + 80:
            heading = label
            if speaker:
                speakers = [speaker]
        elif offset < end:
            if speaker and speaker not in speakers:
                speakers.append(speaker)
            if heading is None:
                heading = label
        else:
            break
    return heading, speakers


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM evidence_index_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT INTO evidence_index_meta(key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _epoch(connection: sqlite3.Connection, now: str) -> str:
    epoch = _meta(connection, "index_epoch")
    if epoch is None:
        _set_meta(connection, "index_epoch", now)
        _set_meta(connection, "schema_version", SCHEMA_VERSION)
        epoch = now
    return epoch


def _ledger(connection: sqlite3.Connection, key: str, status: str, *, body: DocumentBody | None = None,
            doc_id: int | None = None, reason: str | None = None, now: str) -> None:
    row = connection.execute("SELECT attempts FROM evidence_index_ledger WHERE source_key=?", (key,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    connection.execute(
        "INSERT INTO evidence_index_ledger(source_key,status,document_ref,text_sha256,doc_id,reason,attempts,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET status=excluded.status, "
        "document_ref=excluded.document_ref, text_sha256=excluded.text_sha256, doc_id=excluded.doc_id, "
        "reason=excluded.reason, attempts=excluded.attempts, updated_at=excluded.updated_at",
        (key, status, body.document_ref if body else None, body.text_sha256 if body else None, doc_id,
         (reason or "")[:500] or None, attempts, now))


def _arrival(connection: sqlite3.Connection, company_ref: str, body_row: Mapping[str, Any], *,
             now: str, epoch: str) -> int:
    acquired = body_row.get("acquired_at")
    backfill = 1 if acquired is None or str(acquired) < epoch else 0
    cursor = connection.execute(
        "INSERT OR IGNORE INTO material_arrivals(company_ref,document_ref,kind,period,document_date,acquired_at,"
        "indexed_at,digest,backfill) VALUES (?,?,?,?,?,?,?,?,?)",
        (company_ref, body_row["document_ref"], body_row["kind"], body_row.get("period"),
         body_row.get("document_date"), acquired, now, body_row["text_sha256"], backfill))
    return cursor.rowcount or 0


def index_document(connection: sqlite3.Connection, ctx: CorpusContext, body: DocumentBody, *,
                   source_key: str, now: str, epoch: str) -> tuple[int, int, int]:
    """Index one verified body.  Returns ``(doc_id, passages_added, arrivals_added)``.

    Idempotent by ``(document_ref, text_sha256)``: the same body under a second
    ticket adds nothing.  A *different* body under a known ref is indexed and
    the older one marked superseded (still citable by its refs).
    """

    existing = connection.execute(
        "SELECT doc_id FROM evidence_documents WHERE document_ref=? AND text_sha256=?",
        (body.document_ref, body.text_sha256)).fetchone()
    if existing is not None:
        doc_id = int(existing["doc_id"])
        added = 0
        for company_ref in body.filed_company_refs:
            added += _add_company(connection, doc_id, company_ref, "filed", now=now, epoch=epoch)
        return doc_id, 0, added
    spans = chunk_spans(body.text)
    marks = headings(body.text, body.kind)
    title_fts = fts_text(body.title)
    metadata = dict(body.metadata)
    cursor = connection.execute(
        "INSERT INTO evidence_documents(document_ref,text_sha256,source_ref,kind,title,house,document_date,period,url,"
        "renderer,text_chars,passage_count,source_key,acquired_at,indexed_at,superseded_by,metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
        (body.document_ref, body.text_sha256, body.source_ref, body.kind, body.title, body.house,
         body.document_date, body.period, body.url, body.renderer, len(body.text), len(spans), source_key,
         body.acquired_at, now, json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)))
    doc_id = int(cursor.lastrowid)
    connection.execute(
        "UPDATE evidence_documents SET superseded_by=? WHERE document_ref=? AND doc_id<>? AND superseded_by IS NULL",
        (doc_id, body.document_ref, doc_id))
    fixed_speakers = [str(s) for s in metadata.get("speakers") or () if s]
    named_count: dict[str, int] = {}
    for ordinal, (start, end) in enumerate(spans):
        text = body.text[start:end]
        heading, speakers = _passage_heading(marks, start, end)
        speakers = speakers or fixed_speakers
        named = ctx.companies_named(text)
        for ref in named:
            named_count[ref] = named_count.get(ref, 0) + 1
        cursor = connection.execute(
            "INSERT INTO evidence_passages(doc_id,ordinal,start_offset,end_offset,passage_sha256,heading,"
            "speakers_json,named_company_refs_json,text) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, ordinal, start, end, _sha(text), heading,
             json.dumps(speakers, ensure_ascii=False), json.dumps(named), text))
        passage_id = int(cursor.lastrowid)
        connection.execute("INSERT INTO evidence_passages_fts(rowid,title,heading,body) VALUES (?,?,?,?)",
                           (passage_id, title_fts, fts_text(heading), fts_text(text)))
        for ref in named:
            connection.execute("INSERT OR IGNORE INTO evidence_passage_companies(passage_id,company_ref) "
                               "VALUES (?,?)", (passage_id, ref))
    arrivals = 0
    for ref in body.filed_company_refs:
        arrivals += _add_company(connection, doc_id, ref, "filed", now=now, epoch=epoch)
    for ref in metadata.get("listed_company_refs") or ():
        arrivals += _add_company(connection, doc_id, ref, "listed", now=now, epoch=epoch)
    for ref, count in named_count.items():
        # A passing mention is searchable (passage-level) but is not "material
        # about this company" unless the text returns to it.
        arrival = count >= 2 or len(spans) <= 2
        arrivals += _add_company(connection, doc_id, ref, "named", now=now, epoch=epoch, arrival=arrival)
    return doc_id, len(spans), arrivals


def _add_company(connection: sqlite3.Connection, doc_id: int, company_ref: str, basis: str, *,
                 now: str, epoch: str, arrival: bool = True) -> int:
    connection.execute("INSERT OR IGNORE INTO evidence_document_companies(doc_id,company_ref,basis) VALUES (?,?,?)",
                       (doc_id, company_ref, basis))
    if not arrival:
        return 0
    row = connection.execute("SELECT document_ref,kind,period,document_date,acquired_at,text_sha256 "
                             "FROM evidence_documents WHERE doc_id=?", (doc_id,)).fetchone()
    return _arrival(connection, company_ref, dict(row), now=now, epoch=epoch)


def _sync_filed_companies(connection: sqlite3.Connection, ctx: CorpusContext, *, now: str, epoch: str) -> int:
    """Companies the mission filed an already-indexed document under since the last tick."""

    if ctx.core is None:
        return 0
    try:
        top = ctx.core.execute("SELECT MAX(rowid) FROM coverage_mission_discovered_documents").fetchone()[0]
    except sqlite3.Error:
        return 0
    if top is None:
        return 0
    last = int(_meta(connection, "discovered_rowid_seen") or 0)
    if top <= last:
        return 0
    added = 0
    rows = ctx.core.execute(
        "SELECT rowid, company_ref, document_ref FROM coverage_mission_discovered_documents "
        "WHERE rowid>? AND rowid<=? AND status='acquired'", (last, top)).fetchall()
    for row in rows:
        for doc in connection.execute("SELECT doc_id FROM evidence_documents WHERE document_ref=? "
                                      "AND superseded_by IS NULL", (row["document_ref"],)):
            exists = connection.execute(
                "SELECT 1 FROM evidence_document_companies WHERE doc_id=? AND company_ref=? AND basis='filed'",
                (doc["doc_id"], row["company_ref"])).fetchone()
            if exists is None:
                added += _add_company(connection, int(doc["doc_id"]), row["company_ref"], "filed",
                                      now=now, epoch=epoch)
    _set_meta(connection, "discovered_rowid_seen", str(top))
    return added


def _pending(connection: sqlite3.Connection, state_dir: Path, now_ts: float) -> list[tuple[float, Candidate]]:
    ledger = {row["source_key"]: (row["status"], row["attempts"], row["updated_at"])
              for row in connection.execute("SELECT source_key,status,attempts,updated_at FROM evidence_index_ledger")}
    pending: list[tuple[float, Candidate]] = []
    for candidate in iter_candidates(state_dir):
        seen = ledger.get(candidate.source_key)
        if seen is not None:
            status, attempts, updated = seen
            if status != "failed" or attempts >= MAX_ATTEMPTS:
                continue
            try:
                last = datetime.fromisoformat(updated).timestamp()
            except ValueError:
                last = 0.0
            if now_ts - last < RETRY_AFTER_SECONDS:
                continue
        try:
            mtime = candidate.path.stat().st_mtime
        except OSError:
            continue  # no manifest (yet): an acquisition in flight is not a document
        pending.append((mtime, candidate))
    pending.sort(key=lambda item: (-item[0], item[1].source_key))
    return pending


def index_tick(state_dir: str | Path, *, max_documents: int = TICK_MAX_DOCUMENTS,
               max_seconds: float = TICK_MAX_SECONDS, now: str | None = None,
               candidates: Iterable[Candidate] | None = None) -> dict[str, Any]:
    """Index up to ``max_documents`` new manifests within ``max_seconds``; resumable."""

    state = Path(state_dir)
    started = time.monotonic()
    stamp = now or _now()
    summary: dict[str, Any] = {"indexed": 0, "already_indexed": 0, "skipped": 0, "failed": 0,
                               "passages": 0, "arrivals": 0, "companies_synced": 0, "remaining": 0}
    connection = open_index(state)
    try:
        epoch = _epoch(connection, stamp)
        with CorpusContext(state) as ctx:
            connection.execute("BEGIN IMMEDIATE")
            try:
                summary["companies_synced"] = _sync_filed_companies(connection, ctx, now=stamp, epoch=epoch)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if candidates is None:
                pending = [candidate for _, candidate in _pending(connection, state, time.time())]
            else:
                pending = list(candidates)
            done = 0
            for candidate in pending:
                if done >= max_documents or time.monotonic() - started >= max_seconds:
                    break
                done += 1
                try:
                    body = load_document(ctx, candidate)
                except CorpusSkip as exc:
                    connection.execute("BEGIN IMMEDIATE")
                    _ledger(connection, candidate.source_key, "skipped", reason=str(exc), now=stamp)
                    connection.execute("COMMIT")
                    summary["skipped"] += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - one manifest's failure is not the tick's
                    connection.execute("BEGIN IMMEDIATE")
                    _ledger(connection, candidate.source_key, "failed",
                            reason=f"{type(exc).__name__}: {exc}", now=stamp)
                    connection.execute("COMMIT")
                    summary["failed"] += 1
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    known = connection.execute(
                        "SELECT 1 FROM evidence_documents WHERE document_ref=? AND text_sha256=?",
                        (body.document_ref, body.text_sha256)).fetchone() is not None
                    doc_id, passages, arrivals = index_document(connection, ctx, body,
                                                                source_key=candidate.source_key,
                                                                now=stamp, epoch=epoch)
                    _ledger(connection, candidate.source_key, "indexed", body=body, doc_id=doc_id, now=stamp)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                summary["already_indexed" if known else "indexed"] += 1
                summary["passages"] += passages
                summary["arrivals"] += arrivals
            summary["remaining"] = max(0, len(pending) - done)
    finally:
        connection.close()
    summary["seconds"] = round(time.monotonic() - started, 3)
    return summary


def index_acquired_manifest(state_dir: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Index one just-completed acquisition now (an acquisition lane's hook).

    The lane tick would pick it up anyway; this only removes the wait.
    """

    path = Path(manifest_path)
    state = Path(state_dir)
    ticket = path.parent.name
    family_dir = path.parent.parent.name
    if family_dir == "acquisitions":
        candidate = Candidate(f"alphaengine:{ticket}", "alphaengine", path)
    elif family_dir.startswith("feed-acquisitions-"):
        candidate = Candidate(f"{family_dir}:{ticket}", "feed", path)
    elif family_dir == "guidepoint-acquire-runs":
        candidate = Candidate(f"guidepoint:{ticket}", "guidepoint", path)
    elif family_dir == "fetches" and path.name == "exhibit-manifest.json":
        candidate = Candidate(f"fetch-exhibit:{ticket}", "fetch_exhibit", path)
    elif family_dir == "fetches":
        candidate = Candidate(f"fetch:{ticket}", "fetch", path)
    else:
        return {"status": "ignored", "reason": "不是已知的资料获取清单"}
    return index_tick(state, max_documents=1, max_seconds=60.0, candidates=[candidate])


def index_status(state_dir: str | Path) -> dict[str, Any]:
    """Counts and on-disk size, for the lane summary, the cockpit and the report."""

    path = index_path(state_dir)
    size = sum((path.parent / (path.name + suffix)).stat().st_size
               for suffix in ("", "-wal", "-shm") if (path.parent / (path.name + suffix)).is_file())
    with _readonly(state_dir) as connection:
        if connection is None:
            return {"exists": False, "size_bytes": size, "documents": 0, "passages": 0}
        counts = {
            "documents": connection.execute("SELECT COUNT(*) FROM evidence_documents").fetchone()[0],
            "passages": connection.execute("SELECT COUNT(*) FROM evidence_passages").fetchone()[0],
            "text_chars": connection.execute("SELECT COALESCE(SUM(text_chars),0) FROM evidence_documents").fetchone()[0],
            "arrivals": connection.execute("SELECT COUNT(*) FROM material_arrivals").fetchone()[0],
            "by_kind": {row[0]: row[1] for row in connection.execute(
                "SELECT kind, COUNT(*) FROM evidence_documents WHERE superseded_by IS NULL GROUP BY kind")},
            "ledger": {row[0]: row[1] for row in connection.execute(
                "SELECT status, COUNT(*) FROM evidence_index_ledger GROUP BY status")},
        }
    return {"exists": True, "size_bytes": size, **counts}


# ---------------------------------------------------------------------------
# passage refs
# ---------------------------------------------------------------------------


def make_passage_ref(document_ref: str, start: int, end: int, text: str) -> str:
    if not document_ref or any(ch.isspace() for ch in document_ref):
        raise PassageRefInvalid("document_ref must be a non-empty ref without whitespace")
    if not (0 <= start < end):
        raise PassageRefInvalid("a passage span is start < end")
    return f"{PASSAGE_REF_PREFIX}{document_ref}:{start}-{end}:{_sha(text)[:12]}"


def parse_passage_ref(ref: Any) -> dict[str, Any]:
    if not isinstance(ref, str) or len(ref) > 1024:
        raise PassageRefInvalid("not a doc-passage ref")
    match = _PASSAGE_REF_RE.match(ref)
    if match is None:
        raise PassageRefInvalid("not a doc-passage ref: doc-passage:<document_ref>:<start>-<end>:<hash12>")
    start, end = int(match.group("start")), int(match.group("end"))
    if start >= end:
        raise PassageRefInvalid("a passage span is start < end")
    return {"document_ref": match.group("doc"), "start": start, "end": end, "hash12": match.group("hash")}


def is_passage_ref(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith(PASSAGE_REF_PREFIX)


_PASSAGE_COLUMNS = (
    "p.passage_id, p.doc_id, p.ordinal, p.start_offset, p.end_offset, p.passage_sha256, p.heading, "
    "p.speakers_json, p.named_company_refs_json, p.text, d.document_ref, d.text_sha256, d.source_ref, d.kind, "
    "d.title, d.house, d.document_date, d.period, d.url, d.superseded_by, d.metadata_json")


def _doc_companies(connection: sqlite3.Connection, doc_id: int) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"filed": [], "listed": [], "named": []}
    for row in connection.execute("SELECT company_ref, basis FROM evidence_document_companies WHERE doc_id=? "
                                  "ORDER BY company_ref", (doc_id,)):
        found[row["basis"]].append(row["company_ref"])
    return found


def _passage_view(connection: sqlite3.Connection, row: Mapping[str, Any], *, start: int | None = None,
                  end: int | None = None) -> dict[str, Any]:
    pstart, pend = int(row["start_offset"]), int(row["end_offset"])
    start = pstart if start is None else start
    end = pend if end is None else end
    text = row["text"][start - pstart:end - pstart]
    companies = _doc_companies(connection, int(row["doc_id"]))
    named = json.loads(row["named_company_refs_json"] or "[]")
    company_refs = list(dict.fromkeys(companies["filed"] + companies["listed"] + named))
    kind = row["kind"]
    return {
        "passage_ref": make_passage_ref(row["document_ref"], start, end, text),
        "document_ref": row["document_ref"],
        "document_sha256": row["text_sha256"],
        "source_ref": row["source_ref"],
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "title": row["title"],
        "house": row["house"],
        "speakers": json.loads(row["speakers_json"] or "[]"),
        "date": row["document_date"],
        "period": row["period"],
        "heading": row["heading"],
        "url": row["url"],
        "company_refs": company_refs,
        "filed_company_refs": companies["filed"],
        "start": start,
        "end": end,
        "ordinal": int(row["ordinal"]),
        "text": text,
        "content_sha256": _sha(text),
        "superseded": row["superseded_by"] is not None,
    }


def _resolve(connection: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    parts = parse_passage_ref(ref)
    rows = connection.execute(
        f"SELECT {_PASSAGE_COLUMNS} FROM evidence_passages p JOIN evidence_documents d ON d.doc_id=p.doc_id "
        "WHERE d.document_ref=? AND p.start_offset<=? AND p.end_offset>=? "
        "ORDER BY d.superseded_by IS NOT NULL, (p.end_offset-p.start_offset) LIMIT 8",
        (parts["document_ref"], parts["start"], parts["end"])).fetchall()
    if not rows:
        return None
    for row in rows:
        if _sha(row["text"]) != row["passage_sha256"]:
            continue  # the stored passage itself was altered; never quote it
        pstart = int(row["start_offset"])
        cited = row["text"][parts["start"] - pstart:parts["end"] - pstart]
        if _sha(cited)[:12] == parts["hash12"]:
            return _passage_view(connection, row, start=parts["start"], end=parts["end"])
    raise PassageTampered(
        f"{ref}: the indexed text at this span no longer hashes to the ref; the citation cannot be trusted")


def passage(state_dir: str | Path, ref: str) -> dict[str, Any] | None:
    """The cited text and its metadata, verified; ``None`` when not indexed here.

    Raises :class:`PassageRefInvalid` for a malformed ref and
    :class:`PassageTampered` when the span exists but its text does not hash
    to the ref (the ref or the index was altered).
    """

    parse_passage_ref(ref)
    with _readonly(state_dir) as connection:
        if connection is None:
            return None
        return _resolve(connection, ref)


def passage_resolver(state_dir: str | Path):
    """``ref -> cited text or None``, one read-only connection per call.

    What :mod:`mission_deliverable` uses to decide whether a figure that cites
    a passage really appears in it.  A tampered or unknown ref resolves to
    ``None``.
    """

    def resolve(ref: str) -> str | None:
        try:
            found = passage(state_dir, ref)
        except EvidenceIndexError:
            return None
        return None if found is None else found["text"]

    return resolve


def passages_for_document(state_dir: str | Path, document_ref: str, *,
                          include_superseded: bool = False, limit: int = 2000) -> list[dict[str, Any]]:
    """Every passage of a document, in reading order (current body unless asked)."""

    with _readonly(state_dir) as connection:
        if connection is None:
            return []
        clause = "" if include_superseded else " AND d.superseded_by IS NULL"
        rows = connection.execute(
            f"SELECT {_PASSAGE_COLUMNS} FROM evidence_passages p JOIN evidence_documents d ON d.doc_id=p.doc_id "
            f"WHERE d.document_ref=?{clause} ORDER BY d.doc_id DESC, p.ordinal LIMIT ?",
            (document_ref, max(1, min(int(limit), 5000)))).fetchall()
        return [_passage_view(connection, row) for row in rows]


def verify_against_source(state_dir: str | Path, ref: str) -> dict[str, Any]:
    """Re-read the cited document from its spool bytes and re-check the span.

    The index is a projection; this is the audit that the projection still
    says what the acquired original says.
    """

    found = passage(state_dir, ref)
    if found is None:
        return {"status": "not_indexed"}
    with _readonly(state_dir) as connection:
        row = connection.execute(
            "SELECT d.source_key FROM evidence_documents d WHERE d.document_ref=? AND d.text_sha256=?",
            (found["document_ref"], found["document_sha256"])).fetchone()
    if row is None:
        return {"status": "not_indexed"}
    key = row["source_key"]
    family, _, rest = key.partition(":")
    state = Path(state_dir)
    if family == "alphaengine":
        candidate = Candidate(key, "alphaengine", state / "acquisitions" / rest / "manifest.json")
    elif family.startswith("feed-acquisitions-"):
        candidate = Candidate(key, "feed", state / family / rest / "manifest.json")
    elif family == "guidepoint":
        candidate = Candidate(key, "guidepoint", state / "guidepoint-acquire-runs" / rest / "manifest.json")
    elif family == "fetch-exhibit":
        candidate = Candidate(key, "fetch_exhibit", state / "fetches" / rest / "exhibit-manifest.json")
    else:
        candidate = Candidate(key, "fetch", state / "fetches" / rest / "manifest.json")
    try:
        with CorpusContext(state) as ctx:
            body = load_document(ctx, candidate)
    except CorpusSkip as exc:
        return {"status": "source_unavailable", "reason": str(exc)}
    if body.text_sha256 != found["document_sha256"]:
        return {"status": "source_differs", "reason": "原文重新渲染后与索引时的指纹不一致"}
    cited = body.text[found["start"]:found["end"]]
    if cited != found["text"]:
        return {"status": "source_differs", "reason": "原文该区间的文字与引用不一致"}
    return {"status": "verified", "document_sha256": body.text_sha256}


def documents(state_dir: str | Path, *, company_ref: str | None = None, kinds: Sequence[str] | None = None,
              date_from: str | None = None, date_to: str | None = None,
              bases: Sequence[str] = ("filed", "listed", "named"), include_superseded: bool = False,
              limit: int = 500) -> list[dict[str, Any]]:
    """Indexed documents (metadata only, no text), newest first.

    What the material-pack builder enumerates before it reads passages with
    :func:`passages_for_document`: per document its kind, house, date, period,
    title, size, the bases on which it is about ``company_ref`` and how many
    of its passages name that company (``company_passages``), so a caller can
    tell a company's own transcript from a conference call that mentions it
    once.  Read-only; ``[]`` when this environment has no index.
    """

    kinds = [kind for kind in (kinds or ()) if kind]
    unknown = [kind for kind in kinds if kind not in KINDS]
    if unknown:
        raise EvidenceIndexError(f"unknown kinds: {unknown}; known: {list(KINDS)}")
    day_from = _parse_day(date_from, "date_from")
    day_to = _parse_day(date_to, "date_to")
    where: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        where.append("d.superseded_by IS NULL")
    if kinds:
        where.append(f"d.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if day_from:
        where.append("d.document_date >= ?")
        params.append(day_from)
    if day_to:
        where.append("d.document_date <= ?")
        params.append(day_to)
    wanted = [basis for basis in bases if basis in ("filed", "listed", "named")]
    if company_ref:
        if not wanted:
            return []
        where.append(f"d.doc_id IN (SELECT doc_id FROM evidence_document_companies WHERE company_ref=? "
                     f"AND basis IN ({','.join('?' * len(wanted))}))")
        params.extend([company_ref, *wanted])
    sql = ("SELECT d.doc_id, d.document_ref, d.text_sha256, d.source_ref, d.kind, d.title, d.house, "
           "d.document_date, d.period, d.url, d.text_chars, d.passage_count, d.acquired_at, d.indexed_at, "
           "d.superseded_by FROM evidence_documents d"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY d.document_date IS NULL, d.document_date DESC, d.doc_id DESC LIMIT ?")
    with _readonly(state_dir) as connection:
        if connection is None:
            return []
        rows = connection.execute(sql, (*params, max(1, min(int(limit), 5000)))).fetchall()
        out = []
        for row in rows:
            item = {key: row[key] for key in row.keys() if key != "superseded_by"}
            item["kind_label"] = KIND_LABELS.get(row["kind"], row["kind"])
            item["date"] = row["document_date"]
            item["superseded"] = row["superseded_by"] is not None
            if company_ref:
                item["bases"] = [r[0] for r in connection.execute(
                    "SELECT basis FROM evidence_document_companies WHERE doc_id=? AND company_ref=? "
                    "ORDER BY basis", (row["doc_id"], company_ref))]
                item["company_passages"] = int(connection.execute(
                    "SELECT COUNT(*) FROM evidence_passages p JOIN evidence_passage_companies pc "
                    "ON pc.passage_id=p.passage_id WHERE p.doc_id=? AND pc.company_ref=?",
                    (row["doc_id"], company_ref)).fetchone()[0])
            out.append(item)
        return out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset("""
a an the of and or for to in on at by with from as is are was were be been it its this that these those
what why how when which who whom about into than then there their our your vs via per
的 了 和 与 及 在 是 对 吗 呢 么 为什么 什么 如何
""".split())
_TERM_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9$%][A-Za-z0-9$%.'\-]*")
_FY_RE = re.compile(r"^fy'?((?:19|20)?\d{2})$", re.I)
_Q_RE = re.compile(r"^(?:q([1-4])|([1-4])q)$", re.I)
_QUARTER_WORD = {1: "first", 2: "second", 3: "third", 4: "fourth"}


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _light_stem(word: str) -> str:
    lower = word.lower()
    for suffix in ("ings", "ing", "ed", "es", "s"):
        if len(lower) - len(suffix) >= 4 and lower.endswith(suffix):
            return lower[: -len(suffix)]
    return lower


def query_groups(query: str) -> list[dict[str, Any]]:
    """The query as OR-able groups: an FTS expression and a highlight pattern each."""

    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _TERM_RE.findall(query or ""):
        term = raw.strip(".'-")
        if not term:
            continue
        key = term.lower()
        if key in seen or key in _STOPWORDS:
            continue
        seen.add(key)
        if _CJK_RUN_RE.fullmatch(term):
            groups.append({"term": term, "fts": _phrase(" ".join(term)),
                           "pattern": re.compile(re.escape(term))})
            continue
        fy = _FY_RE.match(term)
        if fy:
            yy = fy.group(1)[-2:]
            yyyy = f"20{yy}"
            alternatives = [_phrase(f"fy{yy}"), _phrase(f"fy{yyyy}"), _phrase(f"fiscal {yyyy}"),
                            _phrase(f"fiscal year {yyyy}"), _phrase(f"fiscal{yy}")]
            groups.append({"term": term, "fts": "(" + " OR ".join(alternatives) + ")",
                           "pattern": re.compile(rf"(?<![A-Za-z0-9])(?:FY\s?'?(?:20)?{yy}|fiscal\s+(?:year\s+)?{yyyy})"
                                                 r"(?![0-9])", re.I)})
            continue
        quarter = _Q_RE.match(term)
        if quarter:
            number = int(quarter.group(1) or quarter.group(2))
            alternatives = [_phrase(f"q{number}"), _phrase(f"{number}q"),
                            _phrase(f"{_QUARTER_WORD[number]} quarter")]
            groups.append({"term": term, "fts": "(" + " OR ".join(alternatives) + ")",
                           "pattern": re.compile(rf"(?<![A-Za-z0-9])(?:Q{number}|{number}Q|{_QUARTER_WORD[number]}\s+quarter)",
                                                 re.I)})
            continue
        clean = re.sub(r"[^A-Za-z0-9]+", " ", term).strip()
        if not clean:
            continue
        words = clean.split()
        words[-1] = _light_stem(words[-1])
        body = r"[^A-Za-z0-9]+".join(re.escape(word) for word in words)
        groups.append({"term": term, "fts": _phrase(clean),
                       "pattern": re.compile(rf"(?<![A-Za-z0-9]){body}[A-Za-z]*", re.I)})
    return groups[:24]


def _parse_day(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise EvidenceIndexError(f"{name} must be YYYY-MM-DD") from exc


def _recency(document_date: str | None, reference: date) -> float:
    if not document_date:
        return 0.5
    try:
        day = date.fromisoformat(document_date[:10])
    except ValueError:
        return 0.5
    age = max(0, (reference - day).days)
    return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * math.pow(0.5, age / RECENCY_HALF_LIFE_DAYS)


def _highlights(text: str, groups: Sequence[Mapping[str, Any]]) -> tuple[list[list[int]], int]:
    spans: list[tuple[int, int, int]] = []
    hit = 0
    for index, group in enumerate(groups):
        found = False
        for match in group["pattern"].finditer(text):
            spans.append((match.start(), match.end(), index))
            found = True
            if len(spans) > 200:
                break
        hit += 1 if found else 0
    spans.sort()
    merged: list[list[int]] = []
    for start, end, _ in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged, hit


def _snippet(text: str, highlights: Sequence[Sequence[int]], groups_hit_at: Sequence[tuple[int, int]]
             ) -> str:
    if not highlights:
        return text[:SNIPPET_CHARS].strip()
    best_start, best_score = 0, -1
    for start, _ in highlights:
        window_start = max(0, start - SNIPPET_CHARS // 4)
        window_end = window_start + SNIPPET_CHARS
        score = len({group for pos, group in groups_hit_at if window_start <= pos < window_end})
        if score > best_score:
            best_start, best_score = window_start, score
    end = min(len(text), best_start + SNIPPET_CHARS)
    pieces = []
    cursor = best_start
    for start, stop in highlights:
        if stop <= best_start or start >= end:
            continue
        start, stop = max(start, best_start), min(stop, end)
        pieces.append(text[cursor:start])
        pieces.append("【" + text[start:stop] + "】")
        cursor = stop
    pieces.append(text[cursor:end])
    prefix = "…" if best_start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + re.sub(r"\s+", " ", "".join(pieces)).strip() + suffix


def evidence_search(state_dir: str | Path, query: str, *, company_ref: str | None = None,
                    kinds: Sequence[str] | None = None, date_from: str | None = None,
                    date_to: str | None = None, limit: int = 20, per_document: int = 3,
                    include_superseded: bool = False) -> dict[str, Any]:
    """Passages ranked by BM25 x term coverage x recency x kind weight.

    Returns ``{"status", "query", "results": [...], "total_candidates", ...}``.
    ``status`` is ``ok``, ``empty`` (nothing matched) or ``no_index`` (this
    environment has not built one yet).  Each result is a passage view (see
    :func:`passage`) plus ``score``, ``score_parts``, ``snippet`` (matches in
    【】) and ``highlights`` (offsets into ``text``).
    """

    if not isinstance(query, str) or not query.strip():
        raise EvidenceIndexError("query must be non-empty text")
    if len(query) > MAX_QUERY_CHARS:
        raise EvidenceIndexError(f"query is longer than {MAX_QUERY_CHARS} characters")
    limit = max(1, min(int(limit), MAX_LIMIT))
    per_document = max(1, int(per_document))
    kinds = [kind for kind in (kinds or ()) if kind]
    unknown = [kind for kind in kinds if kind not in KINDS]
    if unknown:
        raise EvidenceIndexError(f"unknown kinds: {unknown}; known: {list(KINDS)}")
    day_from = _parse_day(date_from, "date_from")
    day_to = _parse_day(date_to, "date_to")
    groups = query_groups(query)
    base = {"query": query, "company_ref": company_ref, "kinds": kinds, "date_from": day_from,
            "date_to": day_to, "terms": [group["term"] for group in groups], "results": [],
            "total_candidates": 0}
    if not groups:
        return {**base, "status": "empty", "reason": "查询里没有可检索的词"}
    match = " OR ".join(group["fts"] for group in groups)
    where = ["evidence_passages_fts MATCH ?"]
    params: list[Any] = [match]
    if not include_superseded:
        where.append("d.superseded_by IS NULL")
    if kinds:
        where.append(f"d.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if day_from:
        where.append("d.document_date >= ?")
        params.append(day_from)
    if day_to:
        where.append("d.document_date <= ?")
        params.append(day_to)
    if company_ref:
        where.append("(d.doc_id IN (SELECT doc_id FROM evidence_document_companies WHERE company_ref=? "
                     "AND basis IN ('filed','listed')) OR p.passage_id IN (SELECT passage_id FROM "
                     "evidence_passage_companies WHERE company_ref=?))")
        params.extend([company_ref, company_ref])
    candidate_cap = min(max(limit * 15, 150), 1000)
    reference = date.fromisoformat(day_to) if day_to else datetime.now(timezone.utc).date()
    with _readonly(state_dir) as connection:
        if connection is None:
            return {**base, "status": "no_index", "reason": "这个研究环境还没有建立全文证据索引"}
        try:
            rows = connection.execute(
                f"SELECT {_PASSAGE_COLUMNS}, bm25(evidence_passages_fts, 2.0, 1.5, 1.0) AS bm "
                "FROM evidence_passages_fts JOIN evidence_passages p ON p.passage_id=evidence_passages_fts.rowid "
                "JOIN evidence_documents d ON d.doc_id=p.doc_id "
                f"WHERE {' AND '.join(where)} ORDER BY bm LIMIT ?", (*params, candidate_cap)).fetchall()
        except sqlite3.OperationalError as exc:
            raise EvidenceIndexError(f"search failed: {exc}") from exc
        if not rows:
            return {**base, "status": "empty", "reason": "索引里没有与这些词匹配的段落"}
        best = max(-float(row["bm"]) for row in rows) or 1.0
        scored = []
        for row in rows:
            relevance = max(0.0, -float(row["bm"])) / best
            marks, hit = _highlights(row["text"], groups)
            coverage = hit / len(groups)
            recency = _recency(row["document_date"], reference)
            weight = KIND_WEIGHTS.get(row["kind"], 0.6)
            score = relevance * (0.35 + 0.65 * coverage) * recency * weight
            scored.append((score, row, marks, {"bm25": round(-float(row["bm"]), 4),
                                               "relevance": round(relevance, 4), "coverage": round(coverage, 4),
                                               "recency": round(recency, 4), "kind_weight": weight}))
        scored.sort(key=lambda item: (-item[0], item[1]["document_ref"], item[1]["ordinal"]))
        results = []
        per_doc: dict[int, int] = {}
        for score, row, marks, parts in scored:
            doc_id = int(row["doc_id"])
            if per_doc.get(doc_id, 0) >= per_document:
                continue
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
            view = _passage_view(connection, row)
            hits = []
            for index, group in enumerate(groups):
                for found in group["pattern"].finditer(row["text"]):
                    hits.append((found.start(), index))
            view.update({"score": round(score, 6), "score_parts": parts, "highlights": marks,
                         "snippet": _snippet(row["text"], marks, hits)})
            results.append(view)
            if len(results) >= limit:
                break
    return {**base, "status": "ok", "results": results, "total_candidates": len(rows)}


# ---------------------------------------------------------------------------
# rendering for the material pack
# ---------------------------------------------------------------------------


def render_passage_block(passages: Sequence[Mapping[str, Any]], *, start: int = 1,
                         max_chars_per_passage: int | None = None,
                         company_labels: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Numbered ``[P12]`` blocks for a prompt, and the tag -> ref map to check citations.

    Each block's header line: source kind, house or speaker, date, period,
    title and section; then the passage text.  A drafter cites ``[P12]``; the
    caller maps it back through ``tags`` to the ``doc-passage:`` ref, which is
    what a document stores.
    """

    lines: list[str] = []
    tags: dict[str, str] = {}
    number = start
    for item in passages:
        ref = item.get("passage_ref")
        if not isinstance(ref, str) or not ref:
            continue
        tag = f"P{number}"
        number += 1
        tags[tag] = ref
        kind = item.get("kind")
        who = []
        if item.get("house"):
            who.append(str(item["house"]))
        speakers = [str(s) for s in item.get("speakers") or () if s]
        if speakers:
            who.append("发言：" + "、".join(speakers[:3]))
        header = [f"[{tag}]", str(item.get("kind_label") or KIND_LABELS.get(kind, kind or "资料"))]
        if who:
            header.append(" / ".join(who))
        header.append(str(item.get("date") or "日期未知"))
        if item.get("period"):
            header.append(str(item["period"]))
        if item.get("title"):
            header.append(f"《{item['title']}》")
        if item.get("heading"):
            header.append(f"§{item['heading']}")
        if company_labels:
            names = [company_labels[ref] for ref in item.get("company_refs") or () if ref in company_labels]
            if names:
                header.append("涉及：" + "、".join(names[:5]))
        text = str(item.get("text") or "")
        if max_chars_per_passage is not None and len(text) > max_chars_per_passage:
            text = text[:max_chars_per_passage].rstrip() + "…"
        lines.append(" · ".join(header))
        lines.append(text.strip())
        lines.append("")
    return {"text": "\n".join(lines).rstrip() + ("\n" if lines else ""), "tags": tags,
            "next_number": number}


# ---------------------------------------------------------------------------
# material arrivals
# ---------------------------------------------------------------------------


def material_arrivals(state_dir: str | Path, *, company_ref: str | None = None, after_id: int = 0,
                      include_backfill: bool = False, kinds: Sequence[str] | None = None,
                      limit: int = 1000, since: str | None = None,
                      newest_first: bool = False) -> list[dict[str, Any]]:
    """Arrivals after ``after_id`` (a consumer's watermark), oldest first."""

    where = ["arrival_id > ?"]
    params: list[Any] = [int(after_id)]
    if company_ref:
        where.append("company_ref = ?")
        params.append(company_ref)
    if not include_backfill:
        where.append("backfill = 0")
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    since_time = None
    if since is not None:
        since_time = datetime.fromisoformat(since.replace('Z', '+00:00'))
        if since_time.tzinfo is None:
            raise ValueError('material arrival since must include a timezone')
        # A broad SQLite time comparison accepts Z and offsets. It can round
        # sub-millisecond values; refine strictly in Python after this scan.
        where.append('julianday(indexed_at) >= julianday(?)')
        params.append(since_time.astimezone(timezone.utc).isoformat())
    with _readonly(state_dir) as connection:
        if connection is None:
            return []
        rows = connection.execute(
            f"SELECT * FROM material_arrivals WHERE {' AND '.join(where)} ORDER BY arrival_id "
            f"{'DESC' if newest_first else 'ASC'} LIMIT ?",
            (*params, max(1, min(int(limit), 10000)))).fetchall()
        output = [dict(row) for row in rows]
        if since_time is not None:
            output = [row for row in output
                      if datetime.fromisoformat(row['indexed_at'].replace('Z', '+00:00')) > since_time]
        return output


def material_arrival_summary(state_dir: str | Path, company_ref: str, *, after_id: int = 0,
                             since: str | None = None, include_backfill: bool = False) -> dict[str, Any]:
    """What has accumulated for one company since a watermark: counts by kind.

    The policy question ("is this enough to redraft?") is the caller's; this
    only says what arrived.
    """

    rows = material_arrivals(state_dir, company_ref=company_ref, after_id=after_id,
                             include_backfill=include_backfill, limit=10000)
    if since:
        rows = [row for row in rows if str(row["indexed_at"]) >= since]
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    return {"company_ref": company_ref, "count": len(rows), "by_kind": by_kind,
            "latest_arrival_id": rows[-1]["arrival_id"] if rows else int(after_id),
            "documents": [{"document_ref": row["document_ref"], "kind": row["kind"], "period": row["period"],
                           "document_date": row["document_date"], "indexed_at": row["indexed_at"]}
                          for row in rows[-50:]]}


__all__ = [
    "CHUNK_CHARS", "EvidenceIndexError", "INDEX_DB", "KIND_WEIGHTS", "OVERLAP_CHARS", "PASSAGE_REF_PREFIX",
    "PassageRefInvalid", "PassageTampered", "chunk_spans", "documents", "evidence_search", "fts_text", "headings",
    "index_acquired_manifest", "index_document", "index_path", "index_status", "index_tick",
    "is_passage_ref", "make_passage_ref", "material_arrival_summary", "material_arrivals", "open_index",
    "open_index_readonly", "parse_passage_ref", "passage", "passage_resolver", "passages_for_document",
    "query_groups", "render_passage_block", "verify_against_source",
]
