"""Verified extraction source for an already-fetched public web page (P9d-4c).

The fetch lane (P9d-4b) puts a page's original bytes into Core connector
authority and queues the document for human extraction.  This module is what
the review plane reads through: given the fetch manifest a child left beside
its ticket, it re-reads every Core receipt by ref *and* hash, proves the
bytes in the spool are exactly the ones that invocation recorded, and only
then renders them as deterministic text for bounded windows and quotes.

Two rules shape it:

* **Nothing is trusted but Core.**  The manifest names the invocation; the
  invocation names its call spec and profile; the envelope names the raw
  artifact and the fetched record; the spool object must hash to the body
  hash the record itself carries.  Any break is a refusal, never a
  best-effort render.
* **Rendering is deterministic and lossless about provenance.**  The same
  bytes always produce the same text, so a quote's offsets and hashes are
  stable.  ``text/html``, ``application/xhtml+xml`` and ``text/plain`` are
  rendered when they are UTF-8; JSON media are rendered as typed paths with exact
  number lexemes, never asserted to be an article; complete HTML documents with
  a JSON content type use an explicit mismatch renderer identity. ``application/pdf`` is rendered through
  ``pypdf`` when that optional extra is installed.  Anything else (an image,
  an unknown charset, a password-protected or malformed PDF, or a PDF when the
  extractor is not installed) is refused with its reason rather than decoded
  into garbage a human might quote.  The renderer identity carries the
  extractor version, so upgrading it invalidates old contexts instead of
  silently changing the text behind a citation.

The text is untrusted third-party content.  It is shown to a human for
reading and citation; it never becomes Evidence or a Claim here.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import re
import zlib
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from .public_web_core_fetch import validate_public_web_fetch_manifest
from .document_reading_limits import DEFAULT_READING_LIMITS
from .research_verification import ResearchVerificationConflict, ResearchVerificationError


SOURCE_REF = "source:public-web"
OPERATION = "fetch_get"
# Legacy rendering defaults; extraction can provide installed reading limits.
MAX_SOURCE_CHARS = 600_000
PDF_MEDIA_TYPE = "application/pdf"
TEXT_MEDIA_TYPES: frozenset[str] = frozenset({
    "text/html", "application/xhtml+xml", "text/plain",
})
RENDERABLE_MEDIA_TYPES: frozenset[str] = TEXT_MEDIA_TYPES | {PDF_MEDIA_TYPE, "application/json"}
# A research PDF is bounded: an earnings release is tens of pages, and a
# document longer than this is refused rather than partly rendered.
MAX_PDF_PAGES = 400
MAX_DECOMPRESSED_BYTES = DEFAULT_READING_LIMITS["max_decompressed_bytes"]
_UTF8_CHARSETS: frozenset[str] = frozenset({"", "utf-8", "utf8", "us-ascii", "ascii"})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class PublicWebSourceError(ResearchVerificationError):
    """The fetched page cannot be rendered as a verified extraction source."""


class PublicWebSourceConflict(ResearchVerificationConflict):
    """Core authority for the fetched page drifted from the manifest."""


def _require(condition: Any, message: str, *, conflict: bool = True) -> None:
    if not condition:
        raise (PublicWebSourceConflict if conflict else PublicWebSourceError)(message)


# ---------------------------------------------------------------------------
# deterministic rendering
# ---------------------------------------------------------------------------
# Zero-width characters are not whitespace, so collapsing would keep them:
# they would sit invisibly inside a human's citation.  They are dropped.
_ZERO_WIDTH = str.maketrans({char: None for char in "\u200b\u200c\u200d\u2060\ufeff"})


def _normalize_inline(value: str) -> str:
    """Collapse Unicode whitespace and drop zero-width characters."""

    return re.sub(r"\s+", " ", value.translate(_ZERO_WIDTH)).strip()


class _PublicWebHtmlParser(HTMLParser):
    """Collect visible block text from a public web page, deterministically."""

    _HIDDEN_TAGS = frozenset({
        "script", "style", "template", "noscript", "svg", "math", "iframe", "object", "canvas",
    })
    # Any of these ends the current block; nesting simply starts a new one.
    _BLOCK_TAGS = frozenset({
        "address", "article", "aside", "blockquote", "caption", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
        "td", "th", "thead", "title", "tr", "ul",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []
        self.blocks: list[str] = []

    def _flush(self) -> None:
        block = _normalize_inline(" ".join(self.parts))
        self.parts = []
        if block:
            self.blocks.append(block)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "br":
            self._flush()
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS:
            if self.hidden_depth:
                self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br" and not self.hidden_depth:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        self._flush()
        return "\n\n".join(self.blocks)


class _InlineXbrlFilingParser(_PublicWebHtmlParser):
    """Render an SEC inline-XBRL filing as a reader sees it (2026-10-09).

    Two differences from the generic page renderer, both specific to the
    inline-XBRL shape and so applied only when the markup carries an
    ``<ix:header>``:

    * The ``ix:header`` block -- hidden in every browser -- holds the XBRL
      contexts, units and hidden facts: thousands of ``us-gaap:...Member``
      names, CIKs and dates.  Rendered as visible text it was the first and
      largest block of every 10-K, so a directed read searching "segment" or
      "Americas" retrieved context definitions instead of the segment table.
      Live: two directed reads of EPAM's 10-K returned "只有 XBRL 元素名称、
      没有数值".
    * A table row is one line, its cells separated by `` | `` (a lone ``$``,
      ``%`` or ``)`` cell joined to its number), instead of one block per cell
      -- "Financial Services | $603,711 | $519,986 | $83,725 | 16.1%" rather
      than eleven blocks a reader has to re-assemble into a row.
    """

    def __init__(self) -> None:
        super().__init__()
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _close_cell(self) -> None:
        if self.row is not None and self.cell is not None:
            text = _normalize_inline(" ".join(self.cell))
            if text:
                self.row.append(text)
        self.cell = None

    def _close_row(self) -> None:
        self._close_cell()
        row, self.row = self.row, None
        if not row:
            return
        merged: list[str] = []
        for cell in row:
            if merged and (cell in {"%", ")", ")%"} or merged[-1] in {"$", "(", "($", "$("}):
                merged[-1] = merged[-1] + cell
            else:
                merged.append(cell)
        self.blocks.append(" | ".join(merged))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "ix:header":
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            if tag in self._HIDDEN_TAGS:
                self.hidden_depth += 1
            return
        if tag == "tr":
            self._close_row()
            self._flush()
            self.row = []
            return
        if self.row is not None:
            if tag in {"td", "th"}:
                self._close_cell()
                self.cell = []
            elif tag == "br" or tag in self._BLOCK_TAGS:
                if self.cell is not None:
                    self.cell.append(" ")
            elif tag in self._HIDDEN_TAGS:
                self.hidden_depth += 1
            return
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "ix:header":
            if self.hidden_depth:
                self.hidden_depth -= 1
            return
        if self.hidden_depth:
            if tag in self._HIDDEN_TAGS:
                self.hidden_depth -= 1
            return
        if tag == "tr" or (tag in {"table", "tbody", "thead"} and self.row is not None):
            self._close_row()
            return
        if self.row is not None:
            if tag in {"td", "th"}:
                self._close_cell()
            return
        super().handle_endtag(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.row is not None and not self.hidden_depth:
            if tag.lower() == "br" and self.cell is not None:
                self.cell.append(" ")
            return
        super().handle_startendtag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.row is not None:
            if data.strip():
                if self.cell is None:
                    self.cell = []
                self.cell.append(data)
            return
        super().handle_data(data)

    def text(self) -> str:
        self._close_row()
        return super().text()


def renderer_is_legacy_for_inline_xbrl(renderer: Any) -> bool:
    """Whether a stored renderer identity predates the inline-XBRL renderer.

    Any HTML text hashed under ``html-visible-blocks:0.1`` must be
    re-rendered with ``legacy=True`` to reproduce it; for a page without an
    ``ix:header`` the two renderings are identical, so this is always safe.
    """

    return isinstance(renderer, str) and renderer.split("|")[-1] == "html-visible-blocks:0.1"


#: The inline-XBRL filing renderer's identity.  The generic page renderer
#: (``html-visible-blocks:0.1``) is unchanged for every other page; a reader
#: that must reproduce a text hashed before 2026-10-09 renders with
#: ``legacy=True`` (see :func:`render_public_web_text_candidates`).
INLINE_XBRL_RENDERER = "html-ixbrl-filing:0.2"
_IX_HEADER_RE = re.compile(r"<ix:header[\s>]", re.IGNORECASE)


def is_inline_xbrl(decoded: str) -> bool:
    """True for an SEC inline-XBRL document (it carries an ``ix:header``)."""

    return _IX_HEADER_RE.search(decoded) is not None


def normalized_media_type(raw_media_type: Any) -> tuple[str, str]:
    """Split a recorded media type into ``(type, charset)``, both lowercase."""

    if not isinstance(raw_media_type, str) or not raw_media_type.strip():
        raise PublicWebSourceError("fetched page has no recorded media type")
    parts = [item.strip().lower() for item in raw_media_type.split(";")]
    charset = ""
    for item in parts[1:]:
        if item.startswith("charset="):
            charset = item.split("=", 1)[1].strip().strip('"')
    return parts[0], charset


def _render_pdf(raw: bytes, *, max_pdf_pages: int = MAX_PDF_PAGES) -> tuple[str, str]:
    """Extract PDF text through ``pypdf``; refuse rather than guess."""

    try:
        import pypdf
    except ImportError as exc:  # optional extra; absence is a refusal, not a guess
        raise PublicWebSourceError(
            "fetched page is a PDF but the PDF extractor is not installed; "
            "install the 'pdf' extra to render it"
        ) from exc
    empty_password = False
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            # Public PDFs can forbid editing while permitting reading without
            # a password. is_encrypted stays true even after this succeeds.
            if not reader.decrypt(""):
                raise PublicWebSourceError("fetched PDF requires a password and is not rendered")
            empty_password = True
        pages = reader.pages
        if len(pages) > max_pdf_pages:
            raise PublicWebSourceError(
                f"fetched PDF has {len(pages)} pages, above the configured {max_pdf_pages} page ceiling"
            )
        blocks: list[str] = []
        for page in pages:
            for chunk in re.split(r"\n\s*\n", page.extract_text() or ""):
                block = _normalize_inline(chunk)
                if block:
                    blocks.append(block)
    except PublicWebSourceError:
        raise
    except Exception as exc:  # malformed PDFs are refused with their reason
        raise PublicWebSourceError(f"fetched PDF could not be read: {type(exc).__name__}") from exc
    if not blocks:
        raise PublicWebSourceError("fetched PDF yielded no extractable text")
    # The extractor version is part of the renderer identity: upgrading it
    # changes the text, and a context bound to the old text must go stale.
    renderer = f"pdf-pypdf-{pypdf.__version__}:0.1"
    if empty_password:
        renderer += ":empty-password:0.1"
    return "\n\n".join(blocks), renderer


JSON_RENDERER = "json-structured-paths:0.1"
JSON_DECLARED_HTML_RENDERER = "json-declared-html-document:0.1"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 50_000
_JSON_MEDIA_RE = re.compile(r"^application/(?:json|[a-z0-9][a-z0-9!#$&^_.+-]*\+json)$")
_JSON_HTML_DOCUMENT_RE = re.compile(
    r"\A\s*<!doctype\s+html(?:\s[^>]*)?>\s*<html(?:\s[^>]*)?>[\s\S]*</html\s*>\s*\Z",
    re.IGNORECASE,
)


def is_json_media_type(media_type: str) -> bool:
    return bool(_JSON_MEDIA_RE.fullmatch(media_type))


def _is_complete_declared_json_html(decoded: str) -> bool:
    if not _JSON_HTML_DOCUMENT_RE.fullmatch(decoded):
        return False
    class DocumentRoot(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.opened = self.closed = self.invalid = False
        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag == "html":
                if self.opened or self.closed:
                    self.invalid = True
                self.opened = True
            elif not self.opened or self.closed:
                self.invalid = True
        def handle_endtag(self, tag: str) -> None:
            if tag == "html":
                if not self.opened or self.closed:
                    self.invalid = True
                self.closed = True
            elif not self.opened or self.closed:
                self.invalid = True
        def handle_data(self, data: str) -> None:
            if (not self.opened or self.closed) and data.strip():
                self.invalid = True
    parser = DocumentRoot()
    try:
        parser.feed(decoded)
        parser.close()
    except ValueError:
        return False
    # HTMLParser treats script/style text as data, so a fake </html> inside
    # their contents cannot satisfy the complete document-root requirement.
    return parser.opened and parser.closed and not parser.invalid


class _JsonNumber(str):
    """The exact finite JSON number lexeme; never convert through float."""


def _render_json(decoded: str, *, byte_count: int) -> str:
    if byte_count > MAX_JSON_BYTES:
        raise PublicWebSourceError("fetched JSON exceeds the structured byte limit")
    # Bound nesting before json.loads so hostile inputs cannot recurse first.
    depth = 0
    in_string = escaped = False
    for char in decoded:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PublicWebSourceError("fetched JSON exceeds the structured depth limit")
        elif char in "]}":
            depth -= 1

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PublicWebSourceError("fetched JSON has duplicate object keys")
            result[key] = value
        return result

    def nonfinite(value: str) -> Any:
        raise PublicWebSourceError("fetched JSON contains a non-finite number")

    try:
        value = json.loads(decoded, object_pairs_hook=object_pairs,
                           parse_int=_JsonNumber, parse_float=_JsonNumber,
                           parse_constant=nonfinite)
    except PublicWebSourceError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PublicWebSourceError("fetched JSON is malformed; it is not rendered") from exc
    if not isinstance(value, (dict, list)) or not value:
        raise PublicWebSourceError("fetched JSON contains no structured source payload")
    if isinstance(value, dict):
        status = value.get("status")
        if (value.get("error") not in (None, False, "", [], {})
                or value.get("errors") not in (None, False, "", [], {})
                or value.get("success") is False
                or (isinstance(status, str) and str(status).lower() in {"error", "failed", "failure"})
                or (isinstance(status, _JsonNumber) and re.fullmatch(r"[45]\d\d", status))):
            raise PublicWebSourceError("fetched JSON is an API error response, not source content")
        metadata_keys = {"meta", "metadata", "jsonapi", "links", "status", "headers",
                         "request", "count", "total", "pagination", "page", "success", "code", "message"}
        payload = [v for k, v in value.items() if k.lower() not in metadata_keys]
        if not payload or all(v in (None, "", [], {}) for v in payload):
            raise PublicWebSourceError("fetched JSON contains only API metadata, not source content")

    lines: list[str] = []
    nodes = 0
    def visit(item: Any, path: str) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise PublicWebSourceError("fetched JSON exceeds the structured node limit")
        if isinstance(item, dict):
            if not item:
                lines.append(f"{json.dumps(path, ensure_ascii=False)} [object]: {{}}")
            for key, child in item.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise PublicWebSourceError("fetched JSON has invalid Unicode") from exc
                visit(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(item, list):
            if not item:
                lines.append(f"{json.dumps(path, ensure_ascii=False)} [array]: []")
            for index, child in enumerate(item):
                visit(child, path + "/" + str(index))
        else:
            if isinstance(item, _JsonNumber):
                kind, text = "number", str(item)
            elif isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise PublicWebSourceError("fetched JSON has invalid Unicode") from exc
                kind, text = "string", json.dumps(item, ensure_ascii=False)
            elif item is None:
                kind, text = "null", "null"
            else:
                kind, text = "boolean", "true" if item else "false"
            lines.append(f"{json.dumps(path, ensure_ascii=False)} [{kind}]: {text}")
    visit(value, "")
    return "\n".join(lines)


def render_public_web_text(raw: bytes, *, raw_media_type: str,
                           max_source_chars: int = MAX_SOURCE_CHARS,
                           max_pdf_pages: int = MAX_PDF_PAGES,
                           max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES,
                           legacy: bool = False) -> dict[str, Any]:
    """Render fetched bytes as deterministic text plus how it was rendered.

    ``legacy`` reproduces the pre-2026-10-09 rendering of an inline-XBRL
    filing (header text included, one block per table cell).  Only readers
    that verify a text by the hash it was cited under need it.
    """

    if (type(max_source_chars) is not int or max_source_chars <= 0
            or type(max_pdf_pages) is not int or max_pdf_pages <= 0
            or type(max_decompressed_bytes) is not int or max_decompressed_bytes <= 0):
        raise PublicWebSourceError("rendering limits must be positive integers")
    if not isinstance(raw, bytes):
        raise PublicWebSourceError("fetched page bytes are unavailable")
    media_type, charset = normalized_media_type(raw_media_type)
    if media_type not in RENDERABLE_MEDIA_TYPES and not is_json_media_type(media_type):
        raise PublicWebSourceError(
            f"fetched page media type {media_type} cannot be rendered as text for review"
        )
    compressed = raw.startswith(b"\x1f\x8b")
    if compressed:
        # Original compressed bytes remain the receipt/hash authority. Decode
        # one verified gzip stream (including CRC), with an explicit byte cap
        # before parsing. Never quote a truncated compressed response.
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                decoded_bytes = stream.read(max_decompressed_bytes + 1)
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            raise PublicWebSourceError("fetched gzip content is incomplete or invalid") from exc
        if len(decoded_bytes) > max_decompressed_bytes:
            raise PublicWebSourceError(
                "fetched gzip content exceeds the configured decompressed byte limit")
        raw = decoded_bytes
    if media_type == PDF_MEDIA_TYPE:
        text, renderer = _render_pdf(raw, max_pdf_pages=max_pdf_pages)
        if compressed:
            renderer = "gzip:0.1|" + renderer
        truncated = len(text) > max_source_chars
        if truncated:
            text = text[:max_source_chars]
        return {
            "text": text, "renderer": renderer, "media_type": media_type,
            "truncated": truncated, "rendered_chars": len(text),
        }
    if charset not in _UTF8_CHARSETS:
        raise PublicWebSourceError(
            f"fetched page declares charset {charset}; only UTF-8 is rendered for review"
        )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicWebSourceError("fetched page is not valid UTF-8; it is not rendered") from exc
    declared_json_html = is_json_media_type(media_type) and _is_complete_declared_json_html(decoded)
    if is_json_media_type(media_type) and not declared_json_html:
        text = _render_json(decoded, byte_count=len(raw))
        renderer = JSON_RENDERER
    elif media_type == "text/plain":
        blocks = [_normalize_inline(block) for block in re.split(r"\n\s*\n", decoded)]
        text = "\n\n".join(block for block in blocks if block)
        renderer = "text-plain:0.1"
    else:
        inline_xbrl = not legacy and is_inline_xbrl(decoded)
        parser = _InlineXbrlFilingParser() if inline_xbrl else _PublicWebHtmlParser()
        parser.feed(decoded)
        parser.close()
        text = parser.text()
        renderer = INLINE_XBRL_RENDERER if inline_xbrl else "html-visible-blocks:0.1"
        if declared_json_html:
            renderer = JSON_DECLARED_HTML_RENDERER + "|" + renderer
    if compressed:
        renderer = "gzip:0.1|" + renderer
    truncated = len(text) > max_source_chars
    if truncated:
        text = text[:max_source_chars]
    return {
        "text": text,
        "renderer": renderer,
        "media_type": media_type,
        "truncated": truncated,
        "rendered_chars": len(text),
    }


def render_public_web_text_candidates(raw: bytes, *, raw_media_type: str,
                                     **limits: Any) -> list[dict[str, Any]]:
    """Every rendering a stored text hash may have been taken from, newest first.

    A Claim or extraction context cited before 2026-10-09 names the hash of
    the old inline-XBRL rendering; re-verifying it must still find that text.
    For every other document the two renderings are identical and one is
    returned.
    """

    current = render_public_web_text(raw, raw_media_type=raw_media_type, **limits)
    if current["renderer"].split("|")[-1] != INLINE_XBRL_RENDERER:
        return [current]
    return [current, render_public_web_text(
        raw, raw_media_type=raw_media_type, legacy=True, **limits)]


# ---------------------------------------------------------------------------
# receipt verification
# ---------------------------------------------------------------------------
def _one_row(connection: Any, query: str, params: tuple[Any, ...], label: str) -> Any:
    rows = connection.execute(query, params).fetchall()
    _require(len(rows) == 1, f"fetched page {label} authority is not exact")
    return rows[0]


def verified_public_web_source(
    core: Any, spool: Any, manifest: Mapping[str, Any], receipt_reader: Any, *,
    max_source_chars: int = MAX_SOURCE_CHARS, max_pdf_pages: int = MAX_PDF_PAGES,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES, legacy: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-verify one fetched page end to end and render it.

    ``legacy`` renders an inline-XBRL filing the pre-2026-10-09 way, for a
    caller re-verifying a text hashed under that renderer.

    Returns ``(manifest, rendering)`` where ``rendering`` carries the exact
    text plus how it was produced.  Every receipt is re-read from Core by the
    ref *and* hash the prior record names, so a manifest cannot point at
    another invocation's bytes.
    """

    manifest = validate_public_web_fetch_manifest(manifest)
    connection = core.connection

    invocation = receipt_reader.get_invocation(manifest["connector_invocation_ref"])
    _require(
        invocation is not None
        and invocation["content_hash"] == manifest["connector_invocation_hash"],
        "fetched page invocation authority drifted",
    )
    _require(
        invocation["connector_profile_ref"] == manifest["connector_profile_ref"]
        and invocation["connector_profile_hash"] == manifest["connector_profile_hash"],
        "fetched page invocation does not bind the manifest profile",
    )

    profile = receipt_reader.get_profile(invocation["connector_profile_ref"])
    _require(
        profile is not None and profile["content_hash"] == invocation["connector_profile_hash"],
        "fetched page profile authority drifted",
    )
    _require(
        profile["source_identity"]["source_ref"] == SOURCE_REF
        and profile["source_identity"]["source_type"] == "public_web"
        and profile["auth_mode"] == "none"
        and profile["credential_slot_refs"] == []
        and profile["allowed_hosts"] == [manifest["host"]]
        and profile["allowed_operations"] == [OPERATION],
        "fetched page profile is not the credential-free public-web fetch route for this host",
    )

    call = receipt_reader.get_call_spec(invocation["call_spec_ref"])
    _require(
        call is not None and call["content_hash"] == invocation["call_spec_hash"],
        "fetched page call spec authority drifted",
    )
    _require(
        call["operation"] == OPERATION and call["parameters"] == {"url_ref": manifest["url_ref"]},
        "fetched page call spec does not name the manifest URL ref",
    )

    attempt_row = _one_row(
        connection,
        "SELECT physical_attempt_id,reservation_ref,outcome,content_hash FROM "
        "connector_physical_attempts WHERE connector_invocation_ref=? AND outcome='succeeded'",
        (invocation["id"],),
        "physical attempt",
    )
    attempt = receipt_reader.get_physical_attempt(attempt_row["physical_attempt_id"])
    _require(
        attempt is not None and attempt["content_hash"] == attempt_row["content_hash"]
        and attempt["outcome"] == "succeeded",
        "fetched page physical attempt authority drifted",
    )

    usage_row = _one_row(
        connection,
        "SELECT usage_entry_id,content_hash FROM connector_usage_entries "
        "WHERE physical_attempt_ref=? AND NOT EXISTS (SELECT 1 FROM connector_usage_entries n "
        "WHERE n.correction_of_ref=connector_usage_entries.usage_entry_id)",
        (attempt_row["physical_attempt_id"],),
        "usage entry",
    )
    usage = receipt_reader.get_usage_entry(usage_row["usage_entry_id"])
    _require(
        usage is not None and usage["content_hash"] == usage_row["content_hash"],
        "fetched page usage authority drifted",
    )

    settlement_row = _one_row(
        connection,
        "SELECT settlement_id,state,content_hash FROM connector_quota_settlements "
        "WHERE reservation_ref=? AND NOT EXISTS (SELECT 1 FROM connector_quota_settlements n "
        "WHERE n.correction_of_ref=connector_quota_settlements.settlement_id)",
        (attempt_row["reservation_ref"],),
        "quota settlement",
    )
    settlement = receipt_reader.get_quota_settlement(settlement_row["settlement_id"])
    _require(
        settlement is not None and settlement["content_hash"] == settlement_row["content_hash"]
        and settlement["state"] == "consumed",
        "fetched page quota settlement is not a consumed reservation",
    )

    envelope = receipt_reader.get_source_envelope(manifest["source_envelope_ref"])
    _require(
        envelope is not None and envelope["content_hash"] == manifest["source_envelope_hash"],
        "fetched page source envelope authority drifted",
    )
    _require(
        envelope["connector_invocation_ref"] == invocation["id"]
        and envelope["source"] == SOURCE_REF
        and envelope["operation"] == OPERATION
        and envelope["status"] in {"complete", "partial"}
        and list(envelope["source_record_refs"]) == [manifest["document_ref"]]
        and envelope["raw_response_hash"] == manifest["raw_response_hash"]
        and envelope["raw_artifact_version_ref"] == manifest["raw_artifact_version_ref"],
        "fetched page source envelope does not bind the manifest record",
    )

    artifact = receipt_reader.get_artifact_version(manifest["raw_artifact_version_ref"])
    _require(
        artifact is not None
        and artifact["artifact_content_hash"] == manifest["raw_response_hash"],
        "fetched page raw artifact authority drifted",
    )

    raw = spool.read_object(manifest["raw_response_hash"])
    digest = hashlib.sha256(raw).hexdigest()
    _require(
        digest == manifest["body_sha256"] and digest == manifest["raw_response_hash"],
        "fetched page bytes differ from the recorded body hash",
    )
    _require(
        len(raw) == manifest["body_bytes"],
        "fetched page byte count differs from the manifest",
    )
    # The record ref itself names the body hash; a page whose ref disagrees
    # with its own bytes must never reach a human as a citable original.
    _require(
        manifest["document_ref"].endswith(f":body-sha256:{digest}"),
        "fetched page record ref does not name its own bytes",
    )

    rendering = render_public_web_text(raw, raw_media_type=manifest["raw_media_type"],
                                      max_source_chars=max_source_chars, max_pdf_pages=max_pdf_pages,
                                      max_decompressed_bytes=max_decompressed_bytes,
                                      legacy=legacy)
    return manifest, rendering


__all__ = [
    "MAX_PDF_PAGES",
    "MAX_SOURCE_CHARS",
    "PDF_MEDIA_TYPE",
    "OPERATION",
    "PublicWebSourceConflict",
    "PublicWebSourceError",
    "RENDERABLE_MEDIA_TYPES",
    "SOURCE_REF",
    "normalized_media_type",
    "INLINE_XBRL_RENDERER",
    "is_inline_xbrl",
    "renderer_is_legacy_for_inline_xbrl",
    "render_public_web_text",
    "render_public_web_text_candidates",
    "verified_public_web_source",
]
