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
  stable.  Only ``text/html``, ``application/xhtml+xml`` and ``text/plain``
  in UTF-8 are rendered; anything else (PDF, image, unknown charset) is
  refused with its reason rather than decoded into garbage a human might
  quote.

The text is untrusted third-party content.  It is shown to a human for
reading and citation; it never becomes Evidence or a Claim here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from .public_web_core_fetch import validate_public_web_fetch_manifest
from .research_verification import ResearchVerificationConflict, ResearchVerificationError


SOURCE_REF = "source:public-web"
OPERATION = "fetch_get"
# The Cockpit control plane bounds a window offset to < 600000, so a longer
# page would have unreachable windows; the excess is dropped and declared.
MAX_SOURCE_CHARS = 600_000
RENDERABLE_MEDIA_TYPES: frozenset[str] = frozenset({
    "text/html", "application/xhtml+xml", "text/plain",
})
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


def render_public_web_text(raw: bytes, *, raw_media_type: str) -> dict[str, Any]:
    """Render fetched bytes as deterministic text plus how it was rendered."""

    if not isinstance(raw, bytes):
        raise PublicWebSourceError("fetched page bytes are unavailable")
    media_type, charset = normalized_media_type(raw_media_type)
    if media_type not in RENDERABLE_MEDIA_TYPES:
        raise PublicWebSourceError(
            f"fetched page media type {media_type} cannot be rendered as text for review"
        )
    if charset not in _UTF8_CHARSETS:
        raise PublicWebSourceError(
            f"fetched page declares charset {charset}; only UTF-8 is rendered for review"
        )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicWebSourceError("fetched page is not valid UTF-8; it is not rendered") from exc
    if media_type == "text/plain":
        blocks = [_normalize_inline(block) for block in re.split(r"\n\s*\n", decoded)]
        text = "\n\n".join(block for block in blocks if block)
        renderer = "text-plain:0.1"
    else:
        parser = _PublicWebHtmlParser()
        parser.feed(decoded)
        parser.close()
        text = parser.text()
        renderer = "html-visible-blocks:0.1"
    truncated = len(text) > MAX_SOURCE_CHARS
    if truncated:
        text = text[:MAX_SOURCE_CHARS]
    return {
        "text": text,
        "renderer": renderer,
        "media_type": media_type,
        "truncated": truncated,
        "rendered_chars": len(text),
    }


# ---------------------------------------------------------------------------
# receipt verification
# ---------------------------------------------------------------------------
def _one_row(connection: Any, query: str, params: tuple[Any, ...], label: str) -> Any:
    rows = connection.execute(query, params).fetchall()
    _require(len(rows) == 1, f"fetched page {label} authority is not exact")
    return rows[0]


def verified_public_web_source(
    core: Any, spool: Any, manifest: Mapping[str, Any], receipt_reader: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-verify one fetched page end to end and render it.

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

    rendering = render_public_web_text(raw, raw_media_type=manifest["raw_media_type"])
    return manifest, rendering


__all__ = [
    "MAX_SOURCE_CHARS",
    "OPERATION",
    "PublicWebSourceConflict",
    "PublicWebSourceError",
    "RENDERABLE_MEDIA_TYPES",
    "SOURCE_REF",
    "normalized_media_type",
    "render_public_web_text",
    "verified_public_web_source",
]
