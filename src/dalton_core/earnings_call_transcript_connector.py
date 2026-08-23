"""Fail-closed semantic gate for public earnings-call transcript artifacts.

The public-web connector still owns URL discovery, SSRF controls and raw-byte
authority.  This module adds only the document-specific checks needed before
one successful ``fetch_get`` can be labelled as an earnings-call transcript.
It never treats a search answer, snippet, HEAD response or caller-provided
parsed transcript as source authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from .connector_runner import RunnerConflict, RunnerValidationError
from .public_web_connector import (
    PublicWebFetchAdapter,
    canonical_public_web_url,
)
from .store import canonical_json, content_hash


TRANSCRIPT_PARSER_REF = "parser:earnings-call-transcript-html:0.1"
TRANSCRIPT_PARSER_HASH = content_hash(
    {
        "parser_ref": TRANSCRIPT_PARSER_REF,
        "input": "exact successful public-web fetch_get bytes",
        "required_identity": [
            "issuer_ref", "ticker", "company_name", "fiscal_year",
            "fiscal_quarter", "source_role",
        ],
        "required_document_markers": [
            "earnings_call", "company", "fiscal_year", "fiscal_quarter",
            "questions_and_answers",
        ],
        "minimum_visible_characters": 5_000,
        "minimum_paragraphs": 20,
        "caller_parsed_transcript_accepted": False,
    }
)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSCRIPT_MARKER_RE = re.compile(
    r"(?:earnings|results).{0,40}(?:call|conference)|"
    r"(?:call|conference).{0,40}(?:transcript|remarks)",
    re.IGNORECASE,
)
_QA_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"questions?\s*(?:and|&)\s*answers?",
        r"question-and-answer",
        r"\bq\s*&\s*a\b",
        r"operator instructions",
        r"(?:first|next) question",
        r"open (?:the )?(?:call|line) (?:to|for) questions",
    )
)
_QUARTER_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth"}
_PAYWALL_MARKERS = (
    "upgrade to unlock",
    "premium subscription required",
    "subscribe to unlock this transcript",
)
_PARAMETER_FIELDS = {
    "url_ref", "issuer_ref", "ticker", "company_name", "fiscal_year",
    "fiscal_quarter", "source_role",
}
_PROJECTION_FIELDS = {
    "schema_version", "id", "parser_ref", "parser_hash", "issuer_ref",
    "ticker", "company_name", "fiscal_year", "fiscal_quarter",
    "source_role", "discovery_url_ref", "canonical_url", "url_hash",
    "raw_body_hash", "title", "normalized_text_hash",
    "visible_character_count", "paragraph_count", "qa_marker",
    "content_hash",
}


class EarningsCallTranscriptError(ValueError):
    """The fetched document is not a valid bound earnings-call transcript."""


class EarningsCallTranscriptConflict(EarningsCallTranscriptError):
    """The document, URL or expected issuer/period authority drifted."""


def _text(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise RunnerValidationError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise RunnerValidationError(f"{name} length is invalid")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool) or not isinstance(value, int)
        or value < minimum or value > maximum
    ):
        raise RunnerValidationError(
            f"{name} must be an integer in {minimum}..{maximum}"
        )
    return value


def validate_earnings_call_transcript_parameters(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PARAMETER_FIELDS:
        raise RunnerValidationError(
            "earnings-call transcript parameters have an invalid closed shape"
        )
    wire = json.loads(canonical_json(value))
    wire["url_ref"] = _text(wire["url_ref"], "url_ref")
    if not wire["url_ref"].startswith("public-web-url:sha256:"):
        raise RunnerValidationError("url_ref lacks public-web discovery authority")
    suffix = wire["url_ref"].removeprefix("public-web-url:sha256:")
    if _HASH_RE.fullmatch(suffix) is None:
        raise RunnerValidationError("url_ref hash is invalid")
    wire["issuer_ref"] = _text(wire["issuer_ref"], "issuer_ref")
    wire["ticker"] = _text(wire["ticker"], "ticker", maximum=10).upper()
    if _TICKER_RE.fullmatch(wire["ticker"]) is None:
        raise RunnerValidationError("ticker is invalid")
    wire["company_name"] = _text(
        wire["company_name"], "company_name", maximum=200
    )
    wire["fiscal_year"] = _integer(
        wire["fiscal_year"], "fiscal_year", minimum=1900, maximum=2200
    )
    wire["fiscal_quarter"] = _integer(
        wire["fiscal_quarter"], "fiscal_quarter", minimum=1, maximum=4
    )
    if wire["source_role"] not in {
        "issuer_primary", "third_party_transcript",
    }:
        raise RunnerValidationError("source_role is invalid")
    return wire


def _normalize_visible_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _TranscriptHtmlParser(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4"})
    _HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})
    _VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.title_depth = 0
        self.active_block: str | None = None
        self.block_depth = 0
        self.block_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.title_parts: list[str] = []
        self.blocks: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "title":
            self.title_depth += 1
        if self.active_block is not None and tag not in self._VOID_TAGS:
            self.block_depth += 1
        elif tag in self._BLOCK_TAGS:
            self.active_block = tag
            self.block_depth = 1
            self.block_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN_TAGS:
            if self.hidden_depth:
                self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if self.active_block is not None:
            self.block_depth -= 1
            if self.block_depth == 0:
                block = _normalize_visible_text(" ".join(self.block_parts))
                if block:
                    self.blocks.append((self.active_block, block))
                self.active_block = None
                self.block_parts = []

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        value = data.strip()
        if not value:
            return
        self.visible_parts.append(value)
        if self.title_depth:
            self.title_parts.append(value)
        if self.active_block is not None:
            self.block_parts.append(value)


def _quarter_matches(text: str, quarter: int) -> bool:
    patterns = (
        rf"\bq\s*{quarter}\b",
        rf"\bquarter\s*{quarter}\b",
        rf"\b{_QUARTER_WORDS[quarter]}\s+(?:fiscal\s+)?quarter\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _identity_matches(
    text: str, canonical_url: str, parameters: Mapping[str, Any]
) -> bool:
    company = parameters["company_name"].casefold()
    ticker = parameters["ticker"].casefold()
    haystack = text.casefold()
    if company not in haystack:
        return False
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])", haystack)
        is not None
        or re.search(
            rf"(?:^|/){re.escape(ticker)}(?:/|$)",
            urlsplit(canonical_url).path.casefold(),
        )
        is not None
    )


def build_earnings_call_transcript_projection(
    raw_body: bytes,
    *,
    canonical_url: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one exact HTML body to an issuer, fiscal period and Q&A document."""

    if not isinstance(raw_body, bytes) or not raw_body:
        raise EarningsCallTranscriptError("transcript body must be non-empty bytes")
    if b"\x00" in raw_body:
        raise EarningsCallTranscriptError("transcript body contains NUL bytes")
    try:
        html = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EarningsCallTranscriptError(
            "transcript HTML must be UTF-8"
        ) from exc
    canonical = canonical_public_web_url(canonical_url)
    params = validate_earnings_call_transcript_parameters(parameters)
    parser = _TranscriptHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise EarningsCallTranscriptError("transcript HTML parsing failed") from exc
    visible = _normalize_visible_text(" ".join(parser.visible_parts))
    lowered = visible.casefold()
    if any(marker in lowered for marker in _PAYWALL_MARKERS):
        raise EarningsCallTranscriptError(
            "paywalled or locked transcript body cannot form evidence"
        )
    if len(visible) < 5_000:
        raise EarningsCallTranscriptError("transcript visible text is too short")
    paragraphs = [text for tag, text in parser.blocks if tag == "p"]
    if len(paragraphs) < 20:
        raise EarningsCallTranscriptError("transcript has too few paragraphs")
    if re.search(r"\boperator\b", visible, re.IGNORECASE) is None:
        raise EarningsCallTranscriptError("transcript lacks an operator section")
    heading_candidates = [
        text for tag, text in parser.blocks if tag in {"h1", "h2", "h3"}
    ]
    title_candidates = heading_candidates + [
        _normalize_visible_text(" ".join(parser.title_parts))
    ]
    title = next(
        (item for item in title_candidates if _TRANSCRIPT_MARKER_RE.search(item)),
        "",
    )
    if not title or _TRANSCRIPT_MARKER_RE.search(visible) is None:
        raise EarningsCallTranscriptError(
            "document is not labelled as an earnings call or transcript"
        )
    if not _identity_matches(visible, canonical, params):
        raise EarningsCallTranscriptConflict(
            "transcript company/ticker identity does not match the request"
        )
    if re.search(rf"\b{params['fiscal_year']}\b", visible) is None:
        raise EarningsCallTranscriptConflict(
            "transcript fiscal year does not match the request"
        )
    if not _quarter_matches(visible, params["fiscal_quarter"]):
        raise EarningsCallTranscriptConflict(
            "transcript fiscal quarter does not match the request"
        )
    qa_match = next(
        (
            match
            for pattern in _QA_MARKERS
            if (match := pattern.search(visible)) is not None
        ),
        None,
    )
    if qa_match is None:
        raise EarningsCallTranscriptError(
            "transcript lacks a detectable questions-and-answers section"
        )
    raw_hash = hashlib.sha256(raw_body).hexdigest()
    url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    normalized_hash = hashlib.sha256(visible.encode("utf-8")).hexdigest()
    identity = {
        "issuer_ref": params["issuer_ref"],
        "ticker": params["ticker"],
        "fiscal_year": params["fiscal_year"],
        "fiscal_quarter": params["fiscal_quarter"],
        "url_hash": url_hash,
        "raw_body_hash": raw_hash,
        "parser_hash": TRANSCRIPT_PARSER_HASH,
    }
    base = {
        "schema_version": "0.1",
        "id": "earnings-call-transcript-projection:" + content_hash(identity),
        "parser_ref": TRANSCRIPT_PARSER_REF,
        "parser_hash": TRANSCRIPT_PARSER_HASH,
        "issuer_ref": params["issuer_ref"],
        "ticker": params["ticker"],
        "company_name": params["company_name"],
        "fiscal_year": params["fiscal_year"],
        "fiscal_quarter": params["fiscal_quarter"],
        "source_role": params["source_role"],
        "discovery_url_ref": params["url_ref"],
        "canonical_url": canonical,
        "url_hash": url_hash,
        "raw_body_hash": raw_hash,
        "title": title,
        "normalized_text_hash": normalized_hash,
        "visible_character_count": len(visible),
        "paragraph_count": len(paragraphs),
        "qa_marker": qa_match.group(0),
    }
    return validate_earnings_call_transcript_projection(
        {**base, "content_hash": content_hash(base)}
    )


def validate_earnings_call_transcript_projection(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_FIELDS:
        raise EarningsCallTranscriptError(
            "transcript projection has an invalid closed shape"
        )
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != "0.1":
        raise EarningsCallTranscriptError("unsupported transcript projection version")
    params = validate_earnings_call_transcript_parameters({
        "url_ref": wire["discovery_url_ref"],
        "issuer_ref": wire["issuer_ref"],
        "ticker": wire["ticker"],
        "company_name": wire["company_name"],
        "fiscal_year": wire["fiscal_year"],
        "fiscal_quarter": wire["fiscal_quarter"],
        "source_role": wire["source_role"],
    })
    for key, expected in params.items():
        target = "discovery_url_ref" if key == "url_ref" else key
        if wire[target] != expected:
            raise EarningsCallTranscriptConflict(
                "transcript projection parameters are not canonical"
            )
    if (
        wire["parser_ref"] != TRANSCRIPT_PARSER_REF
        or wire["parser_hash"] != TRANSCRIPT_PARSER_HASH
    ):
        raise EarningsCallTranscriptConflict("transcript parser authority drifted")
    canonical = canonical_public_web_url(wire["canonical_url"])
    if canonical != wire["canonical_url"]:
        raise EarningsCallTranscriptConflict("transcript URL is not canonical")
    for name in (
        "url_hash", "raw_body_hash", "normalized_text_hash", "content_hash",
    ):
        if not isinstance(wire[name], str) or _HASH_RE.fullmatch(wire[name]) is None:
            raise EarningsCallTranscriptError(f"{name} is invalid")
    if wire["url_hash"] != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
        raise EarningsCallTranscriptConflict("transcript URL hash drifted")
    for name, minimum in (("visible_character_count", 5_000), ("paragraph_count", 20)):
        if isinstance(wire[name], bool) or not isinstance(wire[name], int) or wire[name] < minimum:
            raise EarningsCallTranscriptError(f"{name} is invalid")
    for name in ("id", "title", "qa_marker"):
        _text(wire[name], name, maximum=1_000)
    identity = {
        "issuer_ref": wire["issuer_ref"],
        "ticker": wire["ticker"],
        "fiscal_year": wire["fiscal_year"],
        "fiscal_quarter": wire["fiscal_quarter"],
        "url_hash": wire["url_hash"],
        "raw_body_hash": wire["raw_body_hash"],
        "parser_hash": wire["parser_hash"],
    }
    if wire["id"] != "earnings-call-transcript-projection:" + content_hash(identity):
        raise EarningsCallTranscriptConflict("transcript projection id drifted")
    expected_hash = content_hash(
        {key: item for key, item in wire.items() if key != "content_hash"}
    )
    if wire["content_hash"] != expected_hash:
        raise EarningsCallTranscriptConflict("transcript projection hash drifted")
    return wire


def _normalize_host(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value:
        raise RunnerValidationError("approved transcript host is invalid")
    return value.strip().lower().rstrip(".")


def _host_set(value: Sequence[str], name: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RunnerValidationError(f"{name} must be a host sequence")
    hosts = frozenset(_normalize_host(host) for host in value)
    if not hosts:
        raise RunnerValidationError(f"{name} must not be empty")
    return hosts


class EarningsCallTranscriptFetchAdapter(PublicWebFetchAdapter):
    """Fetch and label only an approved, identity-bound transcript page."""

    def __init__(
        self,
        *,
        url_authority_resolver: Any,
        approved_issuer_hosts: Mapping[str, Sequence[str]],
        approved_third_party_hosts: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        if not isinstance(approved_issuer_hosts, Mapping):
            raise RunnerValidationError("approved_issuer_hosts must be a mapping")
        self.approved_issuer_hosts = {
            _text(issuer_ref, "issuer_ref"): _host_set(
                hosts, f"approved_issuer_hosts[{issuer_ref}]"
            )
            for issuer_ref, hosts in approved_issuer_hosts.items()
        }
        if isinstance(approved_third_party_hosts, (str, bytes)) or not isinstance(
            approved_third_party_hosts, Sequence
        ):
            raise RunnerValidationError(
                "approved_third_party_hosts must be a host sequence"
            )
        self.approved_third_party_hosts = frozenset(
            _normalize_host(host) for host in approved_third_party_hosts
        )
        super().__init__(
            url_authority_resolver=url_authority_resolver,
            source_identity={
                "source_ref": "source:company-earnings-call-transcript",
                "source_type": "public_web",
                "source_version": "proposal-2026-08-23",
            },
            allowed_operations=("fetch_get",),
            **kwargs,
        )

    def _url_ref(self, wire: Mapping[str, Any]) -> str:
        return validate_earnings_call_transcript_parameters(
            wire["parameters"]
        )["url_ref"]

    def _successful_source_record_ref(
        self,
        wire: Mapping[str, Any],
        *,
        canonical_final_url: str,
        response_body: bytes,
        method: str,
    ) -> str:
        if method != "GET":
            raise RunnerValidationError(
                "earnings-call transcript authority requires fetch_get"
            )
        parameters = validate_earnings_call_transcript_parameters(
            wire["parameters"]
        )
        host = urlsplit(canonical_final_url).hostname or ""
        if parameters["source_role"] == "issuer_primary":
            allowed = self.approved_issuer_hosts.get(parameters["issuer_ref"], ())
        else:
            allowed = self.approved_third_party_hosts
        if host not in allowed:
            raise EarningsCallTranscriptConflict(
                "transcript host is not approved for the declared source role"
            )
        projection = build_earnings_call_transcript_projection(
            response_body,
            canonical_url=canonical_final_url,
            parameters=parameters,
        )
        return (
            "earnings-call-transcript:"
            f"{parameters['source_role']}:ticker:{parameters['ticker']}:"
            f"fy:{parameters['fiscal_year']}:q:{parameters['fiscal_quarter']}:"
            f"url-sha256:{projection['url_hash']}:"
            f"body-sha256:{projection['raw_body_hash']}:"
            f"projection-sha256:{projection['content_hash']}"
        )


__all__ = [
    "EarningsCallTranscriptConflict",
    "EarningsCallTranscriptError",
    "EarningsCallTranscriptFetchAdapter",
    "TRANSCRIPT_PARSER_HASH",
    "TRANSCRIPT_PARSER_REF",
    "build_earnings_call_transcript_projection",
    "validate_earnings_call_transcript_parameters",
    "validate_earnings_call_transcript_projection",
]
