"""Core-hosted Guidepoint ``search_library`` (S2).

Guidepoint is the expert-network library the Deep Insight Gate keeps asking
for: filings say what a company reported, transcripts of operators say why.
``guidepoint_core`` froze the identity and the owner signed one governance
record per operation (P13ae).  What was still missing is the part that spends
a call: a governed executor, an adapter that speaks the host bridge's dialect,
and a normalizer that turns one ranked page of Q&A excerpts into discoverable
documents with stable refs.

This module is the ``search_library`` twin of ``alphaengine_core_search`` and
``public_web_core_search``, and it is deliberately the same shape: one
governed capability, one connector profile / price / rate policy, and a
single-call executor that leaves the usual Core-held connector authority
behind it (``ConnectorInvocation``, physical attempt, usage, cost, quota
settlement, raw ``ArtifactVersion`` and a ``SourceEnvelope`` whose
``source_record_refs`` are ``guidepoint-excerpt:sha256:<hash>`` refs).

Three things are Guidepoint's own.

**A discovered document is an excerpt, not a transcript.**  The upstream tool
returns ranked Q&A excerpts with a link back to Guidepoint360; there is no
operation that reads a whole transcript, and this lane does not pretend
otherwise (see ``guidepoint_operation_narrowing``).  So the unit of discovery
is the excerpt, and its ref has to be stable across searches that surface the
same passage from different queries -- otherwise the same expert answer enters
the ledger once per query that found it.  The ref is derived from the four
fields that identify a passage rather than a search: transcript name, call
date, respondent, and the exact question asked.

**There is no cursor.**  The frozen inventory contract declares cursor
pagination because that is the shape of the template; the upstream tool takes
``size`` and returns one ranked page.  A non-null cursor is refused at the
adapter rather than silently dropped, because dropping it would make a
second page look like a repeat of the first.

**Verbatim quotation is licensed at twenty words.**  The excerpt text is what
we are licensed to *read*, so the whole excerpt is stored -- truncating the
evidence would leave the claim layer reasoning about a fragment while the
citation pointed at a passage nobody kept.  What the licence bounds is what
may be *reproduced*, so a ``quote_policy`` travels with every excerpt and
``verify_guidepoint_quote`` is the gate a citation has to pass.  The claim
modules are not touched; they call this helper, or they do not quote.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alphaengine_core_acquisition import (
    RESOLVER_REF,
    RUNNER_ACTOR_REF,
    RUNNER_RUNTIME_REF,
    VISIBILITY_SCOPES,
)
from .alphaengine_core_search import register_chained_profile
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorStore
from .connector_authority_port import (
    ConnectorAuthorityPort,
    ConnectorCompletionReceiptReader,
)
from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
from .connector_quota_policy import (
    apply_governed_quota_to_limits,
    governed_daily_quota,
)
from .connector_runner import (
    RunnerValidationError,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .credential_authority import CredentialAuthorityStore
from .guidepoint_core import (
    CREDENTIAL_SLOT_REF,
    SEARCH_CAPABILITY_ID,
    SEARCH_KIND,
    SEARCH_OPERATION,
    SIDE_EFFECT,
    TEMPLATE_KEY,
    TRANSCRIPT_CAPABILITY_ID,
    TRANSCRIPT_KIND,
    TRANSCRIPT_OPERATION,
    GuidepointError,
    guidepoint_contract,
    guidepoint_permissions,
    guidepoint_schema_hash,
    guidepoint_source_hash,
)
from .live_mcp_connector import (
    OPENCLAW_GUIDEPOINT_BRIDGE_HASH,
    LiveMcpRunnerAdmissionGate,
    build_live_mcp_transport_plan,
    validate_live_mcp_adapter_request,
)
from .mcp_managed_runner import validate_mcp_managed_transport_observation
from .observability import ObservabilityStore
from .openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    HostToolInvocationResult,
    OpenClawConnectorBridgeError,
    _parse_mcp_body,
)
from .raw_spool import RawSpool
from .research_context import build_compiled_connector_plan
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash


OPERATION = SEARCH_OPERATION
TOOL_NAME = "search_library"
SOURCE_REF = "source:guidepoint"
ADAPTER_REF = "mcp-target:guidepoint"
ADAPTER_PACKAGE = "openclaw-guidepoint-live-adapter:0.1"
CREDENTIAL_AUTHORITY_REF = "credential-authority:host:guidepoint"
SEARCH_PROFILE_REF = "connector-profile:guidepoint-search-library:v1"
SEARCH_RATE_POLICY_REF = "connector-rate-policy:guidepoint-search-library"
SEARCH_PRICE_RATE_REF = "connector-price-rate:guidepoint-search-library:calls"
SEARCH_PLANNER_REF = "planner:dalton-core-guidepoint-search:0.1"
SEARCH_ROUTING_POLICY_REF = "routing:dalton-core-guidepoint-search:0.1"

# One ranked page of at most 20 excerpts.  The upstream ``size`` ceiling is
# larger; a bigger window is a new profile version, not a bigger constant.
SEARCH_MAX_RECORDS = 20
SEARCH_MAX_RESPONSE_BYTES = 512_000
TRAILING_WINDOW = timedelta(hours=24)

# The only document type this lane can ask for.  Guidepoint's library holds
# one kind of thing -- compliance-reviewed expert call transcripts -- and
# naming it keeps the plan spec the same shape as AlphaEngine's.
SEARCH_DOCUMENT_TYPES: tuple[str, ...] = ("expert_call_transcript",)

# The licence rule, carried as data so it travels with the evidence.
MAX_VERBATIM_WORDS = 20
QUOTE_POLICY: Mapping[str, Any] = {"max_verbatim_words": MAX_VERBATIM_WORDS}

EXCERPT_REF_PREFIX = "guidepoint-excerpt:sha256:"
_EXCERPT_REF_RE = re.compile(r"^guidepoint-excerpt:sha256:[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SPEC_FIELDS = frozenset({"query", "filters", "cursor"})
# ``geography`` is in the frozen input schema because the template shares one
# filter object across libraries; Guidepoint has no market facet to route it
# to, so asking for one is refused rather than ignored.
_FILTER_FIELDS = frozenset({"company", "date_from", "date_to", "document_type", "industry"})
MAX_EXCERPT_CHARS = 20_000


class GuidepointSearchError(RuntimeError):
    """A search request, governance record or authority binding is invalid."""


class GuidepointQuotePolicyError(RuntimeError):
    """A proposed verbatim quotation exceeds or misquotes the licensed excerpt."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GuidepointSearchError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise GuidepointSearchError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


# ---------------------------------------------------------------------------
# frozen contract identity
# ---------------------------------------------------------------------------
def guidepoint_search_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and the frozen ``search_library`` contract."""

    try:
        template, contract = guidepoint_contract(OPERATION)
    except GuidepointError as exc:
        raise GuidepointSearchError(str(exc)) from exc
    if template["transport"]["target_ref"] != ADAPTER_REF:
        raise GuidepointSearchError("Guidepoint host-tool transport target drifted")
    return template, contract


def guidepoint_search_schema_hash() -> str:
    """The hash the owner-approved v1 record binds; derived, never retyped."""

    return guidepoint_schema_hash(OPERATION)


def guidepoint_search_adapter_hash() -> str:
    return content_hash(
        {
            "target_ref": ADAPTER_REF,
            "package": ADAPTER_PACKAGE,
            "bridge_hash": OPENCLAW_GUIDEPOINT_BRIDGE_HASH,
            "operation": OPERATION,
        }
    )


class GuidepointSearchGovernance(ConnectorGovernance):
    """Generic governance narrowed to the Guidepoint search capability.

    The narrowing is the point: the shipped ``guidepoint-get-transcript``
    record is a valid governance record for a different capability, and
    loading it here would otherwise publish a descriptor whose schema hash
    binds an operation this lane never calls.
    """

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            super().__init__(value)
        except ConnectorGovernanceError as exc:
            raise GuidepointSearchError(str(exc)) from exc
        if self.capability_id != SEARCH_CAPABILITY_ID:
            raise GuidepointSearchError(
                "governance capability_id is not the Guidepoint search connector"
            )
        if self.wire["expected_schema_hash"] != guidepoint_search_schema_hash():
            raise GuidepointSearchError(
                "governance expected_schema_hash is not the frozen search_library contract"
            )
        if self.wire["expected_source_hash"] != guidepoint_source_hash():
            raise GuidepointSearchError(
                "governance expected_source_hash is not the frozen Guidepoint source"
            )

    @classmethod
    def load(cls, path: str | Path) -> "GuidepointSearchGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _require_approved(self) -> None:
        try:
            super()._require_approved()
        except ConnectorGovernanceError as exc:
            raise GuidepointSearchError(str(exc)) from exc


# ---------------------------------------------------------------------------
# the operation the upstream does not have
# ---------------------------------------------------------------------------
GUIDEPOINT_NARROWING_ID = "connector-operation-narrowing:guidepoint-get-transcript:v1"
NARROWING_SCHEMA_VERSION = "0.1"


def guidepoint_operation_narrowing(
    *, proposed_by: str, created_at: str = "2026-09-09T00:00:00+00:00"
) -> dict[str, Any]:
    """A proposal that ``get_transcript`` is not available upstream.

    The owner approved two Guidepoint records in P13ae.  One of them names an
    operation the host bridge cannot serve: OpenClaw's Guidepoint MCP exposes
    ``search_library`` and event tools, and nothing that reads a whole
    transcript.  The vendor skill is explicit that reconstructing one from
    excerpts is out of scope.

    This is a *new* artifact rather than an edit, for the reason every
    approval in this codebase is immutable: the approved v1 record's hash is
    what the owner signed, and the packaged template's ``get_transcript``
    contract is what that hash is derived from.  Removing either would make
    the signed record unverifiable -- the record would stop meaning "I
    approved this exact contract" and start meaning "I approved something that
    no longer exists".  So v1 stays exactly as it is, and this says what
    should happen to it.

    Nothing consumes this automatically.  It is the thing the owner reads
    before deciding to withdraw the approval or leave it dormant.
    """

    if not isinstance(proposed_by, str) or not proposed_by.startswith("human:"):
        raise GuidepointSearchError("a narrowing must be proposed by a human: principal")
    base = {
        "schema_version": NARROWING_SCHEMA_VERSION,
        "id": GUIDEPOINT_NARROWING_ID,
        "status": "proposed",
        "created_at": _wire_time(_parse_time(created_at, "created_at")),
        "proposed_by": proposed_by,
        "source_ref": SOURCE_REF,
        "template_ref": guidepoint_search_contract()[0]["id"],
        "operation": TRANSCRIPT_OPERATION,
        "availability": "not_available_upstream",
        "capability_id": TRANSCRIPT_CAPABILITY_ID,
        "supersedes_ref": f"connector-governance:{TRANSCRIPT_KIND}:v1",
        "evidence": [
            "OpenClaw's Guidepoint MCP bridge exposes search_library, search_events, "
            "register_event, get_user_registrations and cancel_registration; no "
            "operation returns a whole transcript.",
            "The vendor skill declares full-transcript retrieval, per-transcript "
            "summarization and advisor-name lookup out of scope, and forbids "
            "reassembling a transcript from excerpts.",
            "The licence bounds verbatim reproduction at twenty words, which a "
            "full-transcript operation could not honour.",
        ],
        "owner_actions": [
            f"withdraw approval:connector-governance:{TRANSCRIPT_KIND}:v1, or leave it "
            "dormant knowing no lane can publish its capability",
            "keep capability:dalton:connector:guidepoint-get-transcript unpublished; "
            "the search lane refuses that record at load time",
            "revisit if Guidepoint ships a document-read operation, which would need "
            "its own contract, its own schema hash and a fresh approval",
        ],
        "remaining_read_path": (
            "reference_url on each excerpt links to the full transcript on "
            "Guidepoint360, which a human opens under their own Guidepoint session; "
            "Dalton never fetches it."
        ),
    }
    return _with_hash(base)


def write_guidepoint_operation_narrowing(
    path: str | Path, *, proposed_by: str
) -> dict[str, Any]:
    record = guidepoint_operation_narrowing(proposed_by=proposed_by)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(record) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return record


# ---------------------------------------------------------------------------
# search spec
# ---------------------------------------------------------------------------
def validate_guidepoint_search_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one ``search_library`` parameter object (closed shape)."""

    if not isinstance(value, Mapping) or set(value) != _SPEC_FIELDS:
        raise GuidepointSearchError(
            "Guidepoint search spec must have exactly query, filters and cursor"
        )
    query = value["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise GuidepointSearchError(
            "Guidepoint search query must be non-empty text (<=400 chars)"
        )
    filters = value["filters"]
    if not isinstance(filters, Mapping) or not set(filters) <= _FILTER_FIELDS:
        raise GuidepointSearchError("Guidepoint search filters have an unknown field")
    cleaned: dict[str, Any] = {}
    for name in sorted(filters):
        item = filters[name]
        if item is None:
            continue
        if not isinstance(item, str) or not item:
            raise GuidepointSearchError(f"Guidepoint search filter {name} must be non-empty text")
        cleaned[name] = item
    if cleaned.get("document_type") not in SEARCH_DOCUMENT_TYPES:
        raise GuidepointSearchError("Guidepoint search document_type is not an expert transcript")
    if ("date_from" in cleaned) != ("date_to" in cleaned):
        raise GuidepointSearchError("Guidepoint search requires both date bounds or neither")
    if "date_from" in cleaned:
        for name in ("date_from", "date_to"):
            if _DATE_RE.fullmatch(cleaned[name]) is None:
                raise GuidepointSearchError(f"Guidepoint search {name} must be YYYY-MM-DD")
        if cleaned["date_from"] > cleaned["date_to"]:
            raise GuidepointSearchError("Guidepoint search date window is reversed")
    cursor = value["cursor"]
    if cursor is not None:
        # The frozen contract declares cursor pagination because the template
        # is shared; this tool has no second page to ask for.
        raise GuidepointSearchError(
            "Guidepoint search_library has no upstream cursor; a second page is a new query"
        )
    return {"query": query.strip(), "filters": cleaned, "cursor": None}


def guidepoint_search_spec_hash(spec: Mapping[str, Any]) -> str:
    return content_hash(
        {"operation": OPERATION, "parameters": validate_guidepoint_search_spec(spec)}
    )


def count_recent_guidepoint_search_calls(
    connection: Any, *, as_of: datetime | None = None
) -> int:
    """Trailing-24h ``search_library`` invocations against the Core profile.

    Quota awareness before a call is spent, not after: the coordinator reads
    this and stops, rather than discovering the ceiling as a rate-limited
    provider error the mission has already paid a tick for.
    """

    now = as_of or datetime.now(timezone.utc)
    window_start = (now - TRAILING_WINDOW).isoformat(timespec="microseconds")
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_invocations "
        "WHERE connector_profile_ref=? AND created_at >= ?",
        (SEARCH_PROFILE_REF, window_start),
    ).fetchone()
    return int(row[0])


def guidepoint_daily_call_ceiling() -> int:
    quota = governed_daily_quota(TEMPLATE_KEY, OPERATION)
    return int(quota["daily_unit_limit"]) * int(quota["max_physical_calls_per_unit"])


# ---------------------------------------------------------------------------
# excerpts: the discovered document
# ---------------------------------------------------------------------------
def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _identity_text(value: str) -> str:
    """Normalized for the purpose of *identifying* a passage, not storing it.

    NFKC folds the cosmetic Unicode variants a transcript pipeline emits --
    full-width punctuation, compatibility forms, non-breaking spaces -- which
    would otherwise give the same expert answer two refs and put it in the
    ledger twice. The stored ``excerpt_text`` is left exactly as returned:
    what is quoted must be what the provider sent.
    """

    return _normalize_space(unicodedata.normalize("NFKC", value))


def _respondent_name(value: Any) -> str:
    """One stable name for the expert who answered.

    The tool returns either a string or an object with name, title and
    company.  Title and company change between calls with the same person, so
    only the name takes part in the identity -- an excerpt should not get a
    second ref because an expert changed jobs.
    """

    if isinstance(value, str):
        name = _identity_text(value)
    elif isinstance(value, Mapping):
        name = _identity_text(str(value.get("full_name") or value.get("name") or ""))
    else:
        name = ""
    if not name:
        raise RunnerValidationError("Guidepoint excerpt has no respondent")
    return name


def guidepoint_excerpt_ref(
    *, transcript_name: str, date: str, respondent: str, question: str
) -> str:
    """The stable ref of one Q&A excerpt.

    Two searches that surface the same passage must produce the same ref, or
    the ledger records the same expert answer once per query that found it.
    The identity is the passage: which transcript, which call, who answered,
    and the exact question -- hashed, because a question is a sentence and a
    ref is not a place to keep one.
    """

    identity = {
        "transcript_name": _identity_text(transcript_name),
        "date": date,
        "respondent": _identity_text(respondent),
        "question_sha256": hashlib.sha256(
            _identity_text(question).casefold().encode("utf-8")
        ).hexdigest(),
    }
    return EXCERPT_REF_PREFIX + content_hash(identity)


def _excerpt_text(question: str, answer: str) -> str:
    """The licensed passage, kept whole.

    The excerpt is what Guidepoint returned and what the subscription licenses
    us to read; the twenty-word bound is on reproduction, not on reading, so
    nothing is truncated here.  ``quote_policy`` is what carries the bound
    forward.
    """

    return f"Q: {_normalize_space(question)}\n\nA: {answer.strip()}"


def guidepoint_excerpt_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize one ``search_library`` payload into excerpt records.

    Raises ``RunnerValidationError`` (the adapter's vocabulary) so a malformed
    provider payload becomes a failed attempt rather than an exception that
    escapes the runner.
    """

    if not isinstance(payload, Mapping):
        raise RunnerValidationError("Guidepoint search payload must be an object")
    rows = payload.get("data")
    if rows is None:
        rows = payload.get("results")
    if not isinstance(rows, list):
        raise RunnerValidationError("Guidepoint search payload lacks a data array")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RunnerValidationError(f"Guidepoint search result[{index}] must be an object")
        transcript_name = row.get("transcript_name")
        date = row.get("date")
        question = row.get("question")
        answer = row.get("answer")
        for value, name in (
            (transcript_name, "transcript_name"), (date, "date"),
            (question, "question"), (answer, "answer"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise RunnerValidationError(f"Guidepoint excerpt lacks {name}")
        if _DATE_RE.fullmatch(date[:10]) is None:
            raise RunnerValidationError("Guidepoint excerpt date is not YYYY-MM-DD")
        respondent = _respondent_name(row.get("respondent"))
        inquirer = row.get("inquirer")
        inquirer = _normalize_space(inquirer) if isinstance(inquirer, str) else None
        reference_url = row.get("reference_url")
        if reference_url is not None and (
            not isinstance(reference_url, str) or not reference_url.startswith("https://")
        ):
            raise RunnerValidationError("Guidepoint excerpt reference_url must be https")
        attribution = row.get("source_attribution")
        markdown = None
        if isinstance(attribution, Mapping):
            candidate = attribution.get("markdown")
            markdown = candidate if isinstance(candidate, str) and candidate else None
        context = row.get("context")
        context = context if isinstance(context, str) and context.strip() else None
        text = _excerpt_text(question, answer)
        if len(text) > MAX_EXCERPT_CHARS:
            raise RunnerValidationError("Guidepoint excerpt exceeds the bounded excerpt size")
        ref = guidepoint_excerpt_ref(
            transcript_name=transcript_name, date=date[:10],
            respondent=respondent, question=question,
        )
        if ref in seen:
            # The same passage twice in one ranked page is a provider bug, not
            # two documents; refusing keeps the envelope's refs unique, which
            # the discovery ledger requires.
            raise RunnerValidationError("Guidepoint search returned a duplicate excerpt")
        seen.add(ref)
        records.append(
            {
                "excerpt_ref": ref,
                "transcript_name": _normalize_space(transcript_name),
                "date": date[:10],
                "respondent": respondent,
                "inquirer": inquirer,
                "question": _normalize_space(question),
                "answer": answer.strip(),
                "context": context,
                "reference_url": reference_url,
                "source_attribution_markdown": markdown,
                "excerpt_text": text,
                "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "quote_policy": dict(QUOTE_POLICY),
            }
        )
    if len(records) > SEARCH_MAX_RECORDS:
        raise RunnerValidationError("Guidepoint search exceeded max_records")
    return records


def guidepoint_json_rpc_from_raw(raw_response: bytes) -> dict[str, Any]:
    """The JSON-RPC envelope inside one recorded raw response.

    The Guidepoint proxy answers ``tools/call`` over ``text/event-stream``, so
    the bytes in the spool are SSE frames, not a JSON document.  The first
    smoke query against the live proxy found that the hard way: the search
    succeeded, twenty excerpt refs landed in the envelope, and re-reading the
    artifact then failed with "not strict UTF-8 JSON" -- which would have made
    every acquisition and every verification fail on real data while passing
    on a fixture that happened to be plain JSON.

    ``_parse_mcp_body`` is the bridge's own reader and accepts both framings.
    Reusing it is deliberate: a second parser here is a second thing that can
    disagree with the transport about what the bytes said.
    """

    if not isinstance(raw_response, bytes):
        raise RunnerValidationError("Guidepoint raw response must be bytes")
    try:
        rpc = _parse_mcp_body(raw_response)
    except OpenClawConnectorBridgeError as exc:
        raise RunnerValidationError(
            "Guidepoint raw response is not a readable MCP body"
        ) from exc
    if (
        rpc.get("jsonrpc") != "2.0"
        or not isinstance(rpc.get("id"), str)
        or not rpc["id"]
        or not isinstance(rpc.get("result"), Mapping)
        or rpc.get("error") is not None
    ):
        raise RunnerValidationError(
            "Guidepoint raw response is not a successful JSON-RPC result"
        )
    return rpc


def guidepoint_excerpts_from_raw_response(raw_response: bytes) -> list[dict[str, Any]]:
    """Recover the excerpts of one search from its exact recorded bytes.

    Acquisition and extraction both re-derive from the immutable raw artifact
    rather than from anything a prior step wrote down; that is what makes an
    excerpt's text provable rather than asserted.
    """

    rpc = guidepoint_json_rpc_from_raw(raw_response)
    return guidepoint_excerpt_records(_tool_text_payload(rpc["result"]))


def guidepoint_provider_request_id(raw_response: bytes) -> str:
    return guidepoint_json_rpc_from_raw(raw_response)["id"]


# ---------------------------------------------------------------------------
# the licence rule, enforced where a citation is made
# ---------------------------------------------------------------------------
# Scripts that do not put spaces between words.  Counting a Chinese sentence
# by splitting on spaces returns 1 no matter how long it is, which turns the
# licence gate into a no-op for exactly the transcripts most likely to be read
# in this workspace.  Each codepoint in these ranges counts as one word, which
# is the convention publishers use for CJK extract limits and is in any case
# the conservative direction.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    # CJK punctuation counts too. It is not a word in any language, but a
    # licence gate that discounts it can be walked past by a quotation made
    # entirely of clauses, and over-counting only ever refuses more.
    (0x3000, 0x303F),    # CJK symbols and punctuation
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xAC00, 0xD7AF),    # Hangul syllables
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x20000, 0x2FA1F),  # CJK Extensions B-F and Compatibility Supplement
    (0xFF01, 0xFF60),    # Fullwidth forms
)


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _CJK_RANGES)


def count_verbatim_words(text: str) -> int:
    """Words for licence purposes, in a text that may not use spaces.

    Space-separated runs count once each; every CJK, Kana or Hangul codepoint
    counts on its own.  A mixed sentence is therefore counted the way a reader
    would: the Latin words as words and the Chinese characters as characters.
    """

    total = 0
    for token in _normalize_space(text).split(" "):
        if not token:
            continue
        cjk = sum(1 for char in token if _is_cjk(char))
        remainder = len(token) - cjk
        # A token that is purely CJK contributes its characters; a token with
        # any Latin left in it contributes those characters as one word more.
        total += cjk + (1 if remainder else 0)
    return total


def verify_guidepoint_quote(
    quote: str,
    *,
    excerpt: Mapping[str, Any],
    max_verbatim_words: int | None = None,
) -> dict[str, Any]:
    """Admit one verbatim quotation of a Guidepoint excerpt, or refuse it.

    Two refusals, both licence-shaped.  A quotation longer than the policy's
    word ceiling is refused because reproducing more than that is outside what
    the subscription permits.  A quotation that is not present verbatim in the
    excerpt is refused because a citation that misquotes its source is worse
    than no citation -- it launders a paraphrase into an expert's mouth.

    The ceiling can only ever be lowered.  ``max_verbatim_words`` and the
    excerpt's own ``quote_policy`` are both clamped to
    ``MAX_VERBATIM_WORDS``, because a gate whose limit the caller supplies is
    not a gate: the licence is a fact about the subscription, not a parameter
    of the call site, and the manifest validator's refusal of a loose policy
    would otherwise be bypassable by anyone holding a dict.

    The claim and citation modules are not modified for this.  They call this
    helper before attaching a Guidepoint quote, and the refusal is an
    exception, not a flag somebody can forget to read.
    """

    if not isinstance(excerpt, Mapping) or "excerpt_text" not in excerpt:
        raise GuidepointQuotePolicyError("a quote must be verified against an excerpt record")
    policy = excerpt.get("quote_policy") or QUOTE_POLICY
    requested = (
        max_verbatim_words
        if max_verbatim_words is not None
        else policy.get("max_verbatim_words", MAX_VERBATIM_WORDS)
    )
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise GuidepointQuotePolicyError("the verbatim word ceiling must be an integer")
    if requested < 1:
        raise GuidepointQuotePolicyError("the verbatim word ceiling must be positive")
    ceiling = min(requested, MAX_VERBATIM_WORDS)
    if not isinstance(quote, str) or not quote.strip():
        raise GuidepointQuotePolicyError("a quotation must be non-empty text")
    normalized = _normalize_space(quote)
    count = count_verbatim_words(normalized)
    if count > ceiling:
        raise GuidepointQuotePolicyError(
            f"Guidepoint licence permits at most {ceiling} verbatim words; "
            f"this quotation is {count}"
        )
    haystack = _normalize_space(str(excerpt["excerpt_text"]))
    if normalized not in haystack:
        raise GuidepointQuotePolicyError(
            "quotation is not verbatim in the cited Guidepoint excerpt"
        )
    return {
        "quote": normalized,
        "word_count": count,
        "max_verbatim_words": ceiling,
        "excerpt_ref": excerpt.get("excerpt_ref"),
        "citation_markdown": excerpt.get("source_attribution_markdown"),
    }


def guidepoint_excerpts_in_authority(
    connection: Any,
    excerpt_refs: Sequence[str],
    *,
    exclude_invocation_ref: str | None = None,
) -> list[str]:
    """Subset of excerpt refs an *earlier* search already put into authority.

    Guidepoint has no second read: the excerpt text arrives inside the search
    that returned it, so an excerpt is in authority as soon as any completed
    search envelope holds it.  Which is why the search that just ran has to be
    excluded -- without that, every excerpt would report as already held by the
    very call that discovered it, and the discovery ledger would queue nothing.
    """

    present: list[str] = []
    for ref in excerpt_refs:
        if _EXCERPT_REF_RE.fullmatch(ref) is None:
            raise GuidepointSearchError("ref is not a guidepoint-excerpt:sha256 ref")
        query = (
            "SELECT 1 FROM connector_source_envelopes e "
            "JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref "
            "JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref "
            "WHERE c.operation=? AND e.record_json LIKE ? "
            "AND e.status IN ('complete','partial')"
        )
        params: list[Any] = [OPERATION, f'%"{ref}"%']
        if exclude_invocation_ref is not None:
            query += " AND e.connector_invocation_ref<>?"
            params.append(exclude_invocation_ref)
        row = connection.execute(query + " LIMIT 1", params).fetchone()
        if row is not None:
            present.append(ref)
    return present


# ---------------------------------------------------------------------------
# live adapter + rehearsal handle
# ---------------------------------------------------------------------------
def _tool_text_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    if not isinstance(content, list):
        raise RunnerValidationError("Guidepoint MCP result lacks content blocks")
    text_blocks = [
        item.get("text") for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    if len(text_blocks) != 1 or not isinstance(text_blocks[0], str):
        raise RunnerValidationError("Guidepoint MCP result requires one text block")
    try:
        payload = json.loads(text_blocks[0])
    except json.JSONDecodeError as exc:
        raise RunnerValidationError("Guidepoint MCP text is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise RunnerValidationError("Guidepoint MCP payload must be an object")
    return dict(payload)


def guidepoint_tool_arguments(request: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the frozen parameter object into the upstream tool's arguments.

    The frozen contract is Dalton's; ``query`` / ``keywords`` / ``size`` /
    ``start_date`` / ``end_date`` are Guidepoint's.  The translation is here,
    once, so that what the mission plans and what the ledger records is the
    governed shape rather than a vendor dialect.
    """

    wire = validate_live_mcp_adapter_request(request)
    if wire["operation"] != OPERATION or wire["tool_name"] != TOOL_NAME:
        raise RunnerValidationError("Guidepoint adapter serves search_library only")
    parameters = wire["parameters"]
    filters = parameters.get("filters") or {}
    if parameters.get("cursor") is not None:
        raise RunnerValidationError("Guidepoint search_library has no upstream cursor")
    if filters.get("document_type") not in SEARCH_DOCUMENT_TYPES:
        raise RunnerValidationError("Guidepoint document_type is not mapped")
    arguments: dict[str, Any] = {
        "query": parameters["query"],
        "size": min(int(wire["max_records"]), SEARCH_MAX_RECORDS),
    }
    keywords = [filters[name] for name in ("company", "industry") if filters.get(name)]
    if keywords:
        arguments["keywords"] = keywords
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if (date_from is None) != (date_to is None):
        raise RunnerValidationError("Guidepoint search requires both date bounds")
    if date_from is not None:
        arguments["start_date"] = date_from
        arguments["end_date"] = date_to
    return arguments


class GuidepointLiveAdapter:
    """Normalize exact Guidepoint MCP results into Dalton's frozen output.

    Structured output is only what the frozen contract declares -- the
    excerpt refs, a null cursor and the provider status.  The excerpts
    themselves stay inside the immutable raw artifact, and every later reader
    re-derives them from those bytes.
    """

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any,
    ) -> dict[str, Any]:
        wire = validate_live_mcp_adapter_request(request)
        invoke = getattr(credential_handle, "invoke", None)
        if not callable(invoke):
            raise RunnerValidationError("host-owned Guidepoint handle lacks invoke")
        arguments = guidepoint_tool_arguments(wire)
        try:
            invocation = invoke(
                wire["tool_name"],
                arguments,
                call_ref=wire["credential_use_ref"],
                deadline_at=wire["deadline_at"],
                max_response_bytes=wire["max_response_bytes"],
            )
        except BridgeRateLimited as exc:
            return self._failure(
                wire, outcome="rate_limited", code="rate_limited", message=str(exc),
                retryable=True, provider_status=429, retry_after_ms=exc.retry_after_ms,
            )
        except BridgePermissionDenied as exc:
            return self._failure(
                wire, outcome="failed", code="permission_denied", message=str(exc),
                retryable=False, provider_status=403, retry_after_ms=None,
            )
        if not isinstance(invocation, HostToolInvocationResult):
            raise RunnerValidationError("host-owned Guidepoint handle returned another type")
        if len(invocation.raw_response) > wire["max_response_bytes"]:
            raise RunnerValidationError("Guidepoint raw response exceeds byte limit")
        result = invocation.result
        if result.get("isError") is True:
            message = "Guidepoint MCP tool returned an error"
            content = result.get("content")
            if isinstance(content, list):
                parts = [
                    str(item.get("text")) for item in content
                    if isinstance(item, Mapping) and item.get("type") == "text"
                ]
                if parts:
                    message = "\n".join(parts)[:1000]
            lowered = message.lower()
            permission = any(word in lowered for word in ("permission", "login", "auth", "token"))
            return self._failure(
                wire,
                outcome="failed",
                code="permission_denied" if permission else "source_error",
                message=message,
                retryable=not permission,
                provider_status=403 if permission else 502,
                retry_after_ms=None,
            )
        # The bytes go to the sink before anything reads them. A payload this
        # adapter refuses is still a response the provider sent, and the raw
        # artifact is the only place a human can find out what it actually
        # said; discarding it because parsing failed destroys the evidence for
        # the failure. The sink is aborted by the executor when the attempt
        # does not succeed, so an unread artifact is not promoted either.
        raw_sink.write(invocation.raw_response)
        payload = _tool_text_payload(result)
        excerpts = guidepoint_excerpt_records(payload)
        size = int(arguments["size"])
        if len(excerpts) > size:
            raise RunnerValidationError("Guidepoint returned more excerpts than size")
        refs = [item["excerpt_ref"] for item in excerpts]
        structured = {
            "source_record_refs": refs,
            "next_cursor": None,
            "provider_status": 200,
        }
        # A page that came back exactly full is not evidence that the library
        # held exactly that much. There is no cursor to ask for the rest, so
        # the honest word is ``partial``: the query saw the top of a ranked
        # list whose depth it cannot know, and a downstream reader that treats
        # ``complete`` as "this is everything Guidepoint has on the subject"
        # would be wrong in precisely the cases where the subject is rich.
        saturated = bool(refs) and len(refs) >= int(wire["max_records"])
        source_status = (
            "empty" if not refs else "partial" if saturated else "complete"
        )
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": "succeeded",
            "provider_request_id": invocation.request_id,
            "provider_status_code": 200,
            "retry_after_ms": None,
            "structured_output": structured,
            "source_record_refs": refs,
            "cursor": None,
            "provider_usage": None,
            "source_status": source_status,
            "completeness": "ranked",
            "error": None,
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )

    @staticmethod
    def _failure(
        wire: Mapping[str, Any],
        *,
        outcome: str,
        code: str,
        message: str,
        retryable: bool,
        provider_status: int,
        retry_after_ms: int | None,
    ) -> dict[str, Any]:
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": outcome,
            "provider_request_id": None,
            "provider_status_code": provider_status,
            "retry_after_ms": retry_after_ms,
            "structured_output": None,
            "source_record_refs": [],
            "cursor": None,
            "provider_usage": None,
            "source_status": None,
            "completeness": None,
            "error": {"code": code, "message": message[:1000], "retryable": retryable},
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )


class FakeGuidepointHandle:
    """Rehearsal stand-in returning canned excerpts in Guidepoint's exact shape.

    ``sse`` reproduces the framing the live proxy actually uses -- it answers
    ``tools/call`` over ``text/event-stream``, so the bytes that reach the
    spool are SSE frames.  A fixture that only ever produced plain JSON is how
    a lane passes its whole suite and then cannot re-read a single live
    artifact.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]], *, sse: bool = False) -> None:
        self.rows = [dict(item) for item in rows]
        self.sse = bool(sse)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, tool_name, arguments, *, call_ref, deadline_at, max_response_bytes):
        del deadline_at, max_response_bytes
        if tool_name != TOOL_NAME:
            raise RuntimeError("fake handle serves search_library only")
        self.calls.append({"arguments": dict(arguments), "call_ref": call_ref})
        size = int(arguments.get("size", SEARCH_MAX_RECORDS))
        payload = {"data": self.rows[:size], "total": len(self.rows[:size])}
        result = {"content": [{"type": "text", "text": canonical_json(payload)}]}
        request_id = f"provider-request:fake-guidepoint:{len(self.calls)}"
        body = canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result})
        raw = (
            f"event: message\ndata: {body}\n\n" if self.sse else body
        ).encode("utf-8")
        return HostToolInvocationResult(
            request_id=request_id, raw_response=raw, result=result
        )


# ---------------------------------------------------------------------------
# governed search
# ---------------------------------------------------------------------------
class GuidepointCoreSearch:
    """Run one governed ``search_library`` call into Core connector authority."""

    def __init__(
        self,
        *,
        store: DaltonStore,
        connectors: ConnectorStore,
        observability: ObservabilityStore,
        journal: RunnerJournal,
        scheduler: Scheduler,
        catalog: CapabilityCatalog,
        spool: RawSpool,
        governance: GuidepointSearchGovernance,
        host_handle: Any,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        grant_seconds: int = 900,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("Guidepoint search requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("Guidepoint search requires the Core ConnectorStore")
        if (
            type(observability) is not ObservabilityStore
            or observability.connection is not store.connection
        ):
            raise TypeError("Guidepoint search requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("Guidepoint search requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("Guidepoint search requires an exact RawSpool")
        if not isinstance(governance, GuidepointSearchGovernance):
            raise TypeError("Guidepoint search requires GuidepointSearchGovernance")
        if not callable(getattr(host_handle, "invoke", None)):
            raise TypeError("host_handle must expose invoke")
        for name, value in (("lease_seconds", lease_seconds), ("grant_seconds", grant_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GuidepointSearchError(f"{name} must be a positive integer")
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.journal = journal
        self.scheduler = scheduler
        self.catalog = catalog
        self.spool = spool
        self.governance = governance
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.grant_seconds = grant_seconds
        self.credentials = CredentialAuthorityStore(
            store, handle_resolver=lambda _grant: host_handle, clock=self.clock
        )
        self.template, self.contract = guidepoint_search_contract()
        self.adapter = GuidepointLiveAdapter()
        self.receipts = ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )
        self.authority_port = ConnectorAuthorityPort(
            connectors=connectors, observability=observability, scheduler=scheduler,
            receipt_reader=self.receipts,
        )
        self._authorities: dict[str, Any] | None = None

    # -- governed authorities ------------------------------------------------
    def _descriptor_spec(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "id": SEARCH_CAPABILITY_ID,
            "version": 1,
            "created_at": self.governance.effective_from,
            "kind": "connector",
            "name": SEARCH_KIND,
            "label": "Core-hosted Guidepoint search_library",
            "summary": (
                "Run one ranked Guidepoint expert-transcript search through the "
                "host-owned local MCP proxy into Core connector authority; results "
                "are Q&A excerpts under a twenty-word verbatim quotation licence"
            ),
            "aliases": ["guidepoint search", "expert transcript search"],
            "tags": [
                "connector", "guidepoint", "expert-network", "read-only",
                "mcp-managed", "search", "discovery",
            ],
            "intent_examples": [
                "find what operators say about IT services demand for a covered company",
            ],
            "source": {
                "type": "dalton",
                "namespace": "connector",
                "source_ref": self.template["id"],
                "source_version": "0.1",
            },
            "contract": {
                "mode": "typed_call",
                "input_schema_ref": self.contract["input_schema_ref"],
                "output_schema_ref": self.contract["output_schema_ref"],
                "instruction_ref": None,
                "adapter_ref": ADAPTER_REF,
            },
            "permissions": guidepoint_permissions(),
            "eligibility": {
                "state": "ready",
                "visibility_scopes": list(VISIBILITY_SCOPES),
                "policy_ref": self.governance.policy_ref,
                "valid_until": None,
            },
            "source_hash": guidepoint_source_hash(),
            "schema_hash": guidepoint_search_schema_hash(),
        }

    def ensure_governed_authorities(self) -> dict[str, Any]:
        if self._authorities is not None:
            return self._authorities
        self.governance._require_approved()
        spec = self._descriptor_spec()
        try:
            descriptor = self.catalog.describe(
                SEARCH_CAPABILITY_ID, visibility_scopes=list(VISIBILITY_SCOPES)
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise GuidepointSearchError(
                "published Guidepoint search capability differs from governed spec"
            )
        binding = {
            "binding_ref": "runner-binding:guidepoint-search-library:0.1",
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": guidepoint_search_adapter_hash(),
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": guidepoint_source_hash(),
            "operation": OPERATION,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "required_permissions": guidepoint_permissions(),
            "side_effects": [SIDE_EFFECT],
            "rate_policy_ref": SEARCH_RATE_POLICY_REF,
        }
        manifest_base = {
            "schema_version": "0.1",
            "id": "runner-environment:dalton-core-guidepoint-search-library:0.1",
            "created_at": self.governance.effective_from,
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "resolver_ref": RESOLVER_REF,
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:guidepoint-search-library:0.1",
            "package_manifest_hash": content_hash(
                {
                    "package": ADAPTER_PACKAGE,
                    "bridge_hash": OPENCLAW_GUIDEPOINT_BRIDGE_HASH,
                    "adapter_hash": guidepoint_search_adapter_hash(),
                }
            ),
            "bindings": [binding],
        }
        manifest = validate_runner_environment_manifest(_with_hash(manifest_base))
        profile_wire = {
            "schema_version": "0.1",
            "id": SEARCH_PROFILE_REF,
            "created_at": self.governance.effective_from,
            "connector_ref": self.template["connector_ref"],
            # version / prior_version_ref are assigned by register_chained_profile:
            # search_library and get_transcript share one connector chain.
            "version": None,
            "prior_version_ref": None,
            "capability_id": SEARCH_CAPABILITY_ID,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": dict(self.template["source_identity"]),
            "source_hash": guidepoint_source_hash(),
            "schema_hash": guidepoint_search_schema_hash(),
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": ADAPTER_REF,
            "adapter_hash": guidepoint_search_adapter_hash(),
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "runner_environment_hash": manifest["content_hash"],
            "allowed_operations": [OPERATION],
            "allowed_hosts": [],
            "auth_mode": "mcp_managed",
            "credential_slot_refs": [CREDENTIAL_SLOT_REF],
            "input_schema_refs": {OPERATION: self.contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: self.contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: self.contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: self.contract["output_schema_hash"]},
            "pagination": {
                "mode": self.contract["pagination"]["mode"],
                "cursor_field": self.contract["pagination"]["cursor_field"],
                "max_pages": self.contract["pagination"]["max_pages"],
            },
            "completeness": {OPERATION: self.contract["completeness_ceiling"]},
            "max_response_bytes": SEARCH_MAX_RESPONSE_BYTES,
            "max_records": SEARCH_MAX_RECORDS,
            "timeout_ms": 120_000,
            "access_policy_ref": "policy:access:guidepoint",
            "retention_policy_ref": "policy:retention:licensed-expert-transcripts",
            "terms_policy_ref": "policy:terms:guidepoint-verbatim-20-words",
            "network_policy": None,
        }
        profile = register_chained_profile(
            self.connectors, profile_wire,
            idempotency_key="guidepoint-search-library:profile:v1",
        )
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{SEARCH_PRICE_RATE_REF}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": SEARCH_PRICE_RATE_REF,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "meter": "calls",
                "unit_quantity": 1,
                # The Guidepoint subscription is the owner's and its price is
                # not per call; Core meters calls without asserting a unit
                # price it cannot see.
                "unit_price_micros": 0,
                "rounding_mode": "ceiling",
                "currency": "USD",
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "source_ref": "pricing:guidepoint:subscription-included",
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key="guidepoint-search-library:price:v1",
        )
        quota = governed_daily_quota(TEMPLATE_KEY, OPERATION)
        price_book = {"price_rate_refs": [price["id"]], "required_price_meters": ["calls"]}
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{SEARCH_RATE_POLICY_REF}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": SEARCH_RATE_POLICY_REF,
                "quota_scope_ref": "connector-quota-scope:guidepoint:search_library",
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "window_seconds": quota["window_seconds"],
                "reset_timezone": quota["reset_timezone"],
                "max_concurrency": 1,
                "quota_currency": "USD",
                **price_book,
                "price_book_hash": content_hash(price_book),
                "limits": apply_governed_quota_to_limits(
                    quota,
                    max_response_bytes=profile["max_response_bytes"],
                    max_records=profile["max_records"],
                ),
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key="guidepoint-search-library:rate-policy:v1",
        )
        self._authorities = {
            "descriptor": descriptor,
            "binding": binding,
            "manifest": manifest,
            "profile": profile,
            "price": price,
            "rate_policy": rate_policy,
        }
        return self._authorities

    # -- one search ----------------------------------------------------------
    def build_request(
        self, spec: Mapping[str, Any], *, created_at: str | None = None
    ) -> dict[str, Any]:
        """Bind one validated spec to a creation time; the pair is the identity."""

        parameters = validate_guidepoint_search_spec(spec)
        created = created_at or _wire_time(self.clock())
        _parse_time(created, "created_at")
        identity = {"operation": OPERATION, "parameters": parameters, "created_at": created}
        return {
            "operation": OPERATION,
            "parameters": parameters,
            "created_at": created,
            "query_hash": guidepoint_search_spec_hash(parameters),
            "request_hash": content_hash(identity),
        }

    def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute (or durably replay) one search; return the discovery receipt."""

        if not isinstance(request, Mapping) or set(request) != {
            "operation", "parameters", "created_at", "query_hash", "request_hash",
        }:
            raise GuidepointSearchError("Guidepoint search request has an invalid closed shape")
        rebuilt = self.build_request(request["parameters"], created_at=request["created_at"])
        if rebuilt != dict(request):
            raise GuidepointSearchError("Guidepoint search request identity drifted")
        parameters = rebuilt["parameters"]
        created_at = rebuilt["created_at"]
        suffix = rebuilt["request_hash"][:20]
        authorities = self.ensure_governed_authorities()
        profile = authorities["profile"]
        descriptor = authorities["descriptor"]
        binding = authorities["binding"]
        manifest = authorities["manifest"]

        work_id = f"work:guidepoint-search:{suffix}"
        call_id = f"connector-call:guidepoint-search:{suffix}"
        invocation_id = f"connector-invocation:guidepoint-search:{suffix}"
        artifact_ref = f"artifact:guidepoint-search:{suffix}:raw"
        request_id = f"connector-runner-request:guidepoint-search:{suffix}"
        grant_id = f"credential-grant:guidepoint-search:{suffix}"

        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question=(
                "Search the Guidepoint expert-transcript library for excerpts into "
                "Core connector authority"
            ),
            requested_capabilities=(SEARCH_CAPABILITY_ID,),
            runtime_profile_ref=RUNNER_RUNTIME_REF,
            budget={"max_seconds": self.lease_seconds},
            idempotency_key=work_id,
            declared_side_effects=(SIDE_EFFECT,),
            status="ready",
            input_refs=(call_id,),
        )
        work_hash = content_hash(work.to_dict())
        compiled = build_compiled_connector_plan(
            task_ref=work.id,
            task_hash=work_hash,
            planner_ref=SEARCH_PLANNER_REF,
            planner_hash=content_hash({"planner": SEARCH_PLANNER_REF}),
            routing_policy_ref=SEARCH_ROUTING_POLICY_REF,
            routing_policy_hash=content_hash({"routing": OPERATION}),
            step_specs=[
                {
                    "source_ref": profile["source_identity"]["source_ref"],
                    "source_hash": profile["source_hash"],
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "operation": OPERATION,
                    "parameters": parameters,
                    "input_schema_ref": self.contract["input_schema_ref"],
                    "input_schema_hash": self.contract["input_schema_hash"],
                    "output_schema_ref": self.contract["output_schema_ref"],
                    "output_schema_hash": self.contract["output_schema_hash"],
                    "completeness_required": self.contract["completeness_ceiling"],
                    "depends_on": [],
                    "fallback_step_refs": [],
                    "max_attempts": 1,
                }
            ],
            created_at=created_at,
        )
        step = compiled["steps"][0]
        # P13ae/P13af: the bridge is resolved by (source_ref, operation).
        # AlphaEngine also exposes search_library, and this is the pair that
        # keeps a Guidepoint call off AlphaEngine's tool.
        transport = build_live_mcp_transport_plan(compiled, step)
        resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: self.adapter},
            {binding["binding_ref"]: lambda value, expected=parameters: value == expected},
        )
        gate = LiveMcpRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=resolver,
            visibility_scopes=list(VISIBILITY_SCOPES),
            clock=self.clock,
            credential_authority=self.credentials,
            transport_plans=[transport],
            compiled_plans=[compiled],
        )
        executor = ConnectorTransportExecutor(
            gate=gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority_port,
            connector_reader=self.connectors,
            clock=self.clock,
        )

        try:
            stored = self.journal.request(request_id)
        except RunnerJournalNotFound:
            stored = None
        replayed = False
        if stored is not None:
            latest = self.journal.latest(request_id)
            if latest["state"] != "responded":
                raise GuidepointSearchError(
                    f"Guidepoint runner request {request_id} is incomplete at durable "
                    f"state {latest['state']}; run transport recovery first"
                )
            replayed = True
            response = executor.execute(stored, scheduler_lease_token="replay")
        else:
            call = self.connectors.register_call_spec(
                {
                    "schema_version": "0.1",
                    "id": call_id,
                    "created_at": created_at,
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "connector_profile_ref": profile["id"],
                    "operation": OPERATION,
                    "parameters": parameters,
                    "query_hash": content_hash(
                        {"operation": OPERATION, "parameters": parameters}
                    ),
                },
                idempotency_key=f"{call_id}:register",
            )
            execution = ExecutionInvocation(
                schema_version="0.1",
                id=invocation_id,
                created_at=created_at,
                kind=ExecutionKind.CONNECTOR,
                work_order_ref=work.id,
                profile_ref=profile["id"],
                capability=SEARCH_CAPABILITY_ID,
                input_refs=(call["id"],),
                output_refs=(artifact_ref,),
                started_at=created_at,
                completed_at=None,
                side_effects=(),
                runtime_ref=profile["runner_runtime_ref"],
                actor_ref=profile["runner_actor_ref"],
                environment_hash=profile["runner_environment_hash"],
            )
            lease = self.catalog.prepare(
                work,
                capability_id=SEARCH_CAPABILITY_ID,
                revision_ref=descriptor.revision_ref,
                catalog_epoch=descriptor.catalog_epoch,
                descriptor_hash=descriptor.content_hash,
                source_hash=descriptor.source_hash,
                schema_hash=descriptor.schema_hash,
                policy_ref=self.governance.policy_ref,
                policy_hash=self.governance.policy_hash(),
                principal_ref=self.governance.principal_ref,
                visibility_scopes=list(VISIBILITY_SCOPES),
                ttl_seconds=self.lease_seconds,
            )
            invocation = self.connectors.register_invocation(
                {
                    "schema_version": "0.1",
                    "id": invocation_id,
                    "created_at": created_at,
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "capability_lease_ref": lease.id,
                    "capability_lease_hash": lease.content_hash,
                    "descriptor_revision_ref": descriptor.revision_ref,
                    "catalog_epoch": descriptor.catalog_epoch,
                    "logical_invocation_key": "connector-logical:" + content_hash(
                        {
                            "work_order_ref": work.id,
                            "work_order_hash": work_hash,
                            "connector_profile_hash": profile["content_hash"],
                            "call_spec_hash": call["content_hash"],
                        }
                    ),
                },
                execution=execution,
                idempotency_key=f"{invocation_id}:register",
            )
            enqueued = self.scheduler.enqueue(work)
            if enqueued.get("status") == "conflict":
                raise GuidepointSearchError(
                    f"scheduler already holds another WorkOrder for {work.id}"
                )
            claim = self.scheduler.claim(
                RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=self.lease_seconds
            )
            if claim is None:
                raise GuidepointSearchError(
                    f"WorkOrder {work.id} is not claimable; it may already be complete "
                    "without a durable runner response"
                )
            now = self.clock()
            grant_base = {
                "schema_version": "0.1",
                "id": grant_id,
                "created_at": _wire_time(now),
                "expires_at": _wire_time(now + timedelta(seconds=self.grant_seconds)),
                "authority_ref": CREDENTIAL_AUTHORITY_REF,
                "grant_kind": "mcp_managed",
                "target_ref": ADAPTER_REF,
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "capability_lease_ref": lease.id,
                "capability_lease_hash": lease.content_hash,
                "adapter_ref": ADAPTER_REF,
                "adapter_hash": guidepoint_search_adapter_hash(),
                "principal_ref": self.governance.principal_ref,
                # A slot ref, never material: GUIDEPOINT_CLIENT_ID and
                # GUIDEPOINT_CLIENT_SECRET stay in the OpenClaw workspace and
                # the local proxy is what holds the refreshed token.
                "credential_slot_refs": [CREDENTIAL_SLOT_REF],
                "allowed_operations": [OPERATION],
                "max_calls": 1,
            }
            self.credentials.register_grant(
                _with_hash(grant_base), idempotency_key=f"{grant_id}:register"
            )
            runner_request = _with_hash(
                {
                    "schema_version": "0.2",
                    "id": request_id,
                    "created_at": created_at,
                    "connector_invocation_ref": invocation["id"],
                    "connector_invocation_hash": invocation["content_hash"],
                    "execution_ref": invocation["execution_ref"],
                    "execution_hash": invocation["execution_hash"],
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "scheduler_attempt_number": int(
                        claim["lease"].get("attempt_number")
                        or claim["attempt"]["attempt_number"]
                    ),
                    "scheduler_lease_revision_ref": claim["lease"]["id"],
                    "scheduler_lease_hash": claim["lease"]["content_hash"],
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "capability_lease_ref": lease.id,
                    "capability_lease_hash": lease.content_hash,
                    "principal_ref": self.governance.principal_ref,
                    "runner_runtime_ref": profile["runner_runtime_ref"],
                    "runner_actor_ref": profile["runner_actor_ref"],
                    "runner_environment_hash": profile["runner_environment_hash"],
                    "transport_plan_ref": transport["id"],
                    "transport_plan_hash": transport["content_hash"],
                    "compiled_connector_plan_ref": compiled["id"],
                    "compiled_connector_plan_hash": compiled["content_hash"],
                    "compiled_step_ref": step["id"],
                    "compiled_step_hash": step["content_hash"],
                    "idempotency_key": request_id,
                }
            )
            response = executor.execute(
                runner_request, scheduler_lease_token=claim["lease_token"]
            )
        return self._receipt(rebuilt, response, profile, replayed=replayed)

    def _receipt(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        profile: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        invocation = self.receipts.get_invocation(response["connector_invocation_ref"])
        if invocation["content_hash"] != response["connector_invocation_hash"]:
            raise GuidepointSearchError("Guidepoint invocation authority drifted")
        base: dict[str, Any] = {
            "operation": OPERATION,
            "parameters": dict(request["parameters"]),
            "created_at": request["created_at"],
            "query_hash": request["query_hash"],
            "request_hash": request["request_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "runner_response_ref": response["id"],
            "outcome": response["outcome"],
            "replayed": replayed,
            "provider_calls": 0 if replayed else 1,
            "source_envelope_ref": None,
            "source_envelope_hash": None,
            "raw_artifact_version_ref": None,
            "document_refs": [],
            "next_cursor": None,
            "source_status": None,
            "quote_policy": dict(QUOTE_POLICY),
        }
        if response["outcome"] != "succeeded" or response["source_envelope_ref"] is None:
            return base
        source = self.receipts.get_source_envelope(response["source_envelope_ref"])
        if source is None or source["content_hash"] != response["source_envelope_hash"]:
            raise GuidepointSearchError("Guidepoint source envelope authority drifted")
        if (
            source["connector_invocation_ref"] != invocation["id"]
            or source["operation"] != OPERATION
            or source["source"] != profile["source_identity"]["source_ref"]
        ):
            raise GuidepointSearchError("Guidepoint source envelope does not bind the call")
        document_refs: list[str] = []
        for ref in source["source_record_refs"]:
            if _EXCERPT_REF_RE.fullmatch(ref) is None:
                raise GuidepointSearchError(
                    "Guidepoint source record is not a guidepoint-excerpt ref"
                )
            document_refs.append(ref)
        base.update({
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "raw_artifact_version_ref": source["raw_artifact_version_ref"],
            "document_refs": document_refs,
            "next_cursor": source["cursor"],
            "source_status": source["status"],
        })
        return base

    def excerpts(self, source_envelope_ref: str) -> list[dict[str, Any]]:
        """Rebuild the excerpts of one completed search from its raw bytes.

        Nothing is stored here.  The excerpts are derived from the exact raw
        artifact the envelope names, which is also how acquisition and
        extraction recover them -- so no step downstream is trusting a
        transcription somebody made along the way.
        """

        source = self.receipts.get_source_envelope(source_envelope_ref)
        if source is None:
            raise GuidepointSearchError("Guidepoint source envelope was not found")
        raw = self.spool.read_object(source["raw_response_hash"])
        records = guidepoint_excerpts_from_raw_response(raw)
        if [item["excerpt_ref"] for item in records] != list(source["source_record_refs"]):
            raise GuidepointSearchError(
                "Guidepoint raw artifact does not reproduce the envelope's excerpt refs"
            )
        return records


__all__ = [
    "ADAPTER_REF",
    "CREDENTIAL_SLOT_REF",
    "EXCERPT_REF_PREFIX",
    "FakeGuidepointHandle",
    "GUIDEPOINT_NARROWING_ID",
    "GuidepointCoreSearch",
    "GuidepointLiveAdapter",
    "GuidepointQuotePolicyError",
    "GuidepointSearchError",
    "GuidepointSearchGovernance",
    "MAX_VERBATIM_WORDS",
    "OPERATION",
    "QUOTE_POLICY",
    "SEARCH_CAPABILITY_ID",
    "SEARCH_DOCUMENT_TYPES",
    "SEARCH_KIND",
    "SEARCH_MAX_RECORDS",
    "SEARCH_PROFILE_REF",
    "SOURCE_REF",
    "TOOL_NAME",
    "count_recent_guidepoint_search_calls",
    "count_verbatim_words",
    "guidepoint_daily_call_ceiling",
    "guidepoint_excerpt_records",
    "guidepoint_excerpt_ref",
    "guidepoint_excerpts_from_raw_response",
    "guidepoint_excerpts_in_authority",
    "guidepoint_json_rpc_from_raw",
    "guidepoint_operation_narrowing",
    "guidepoint_provider_request_id",
    "guidepoint_search_adapter_hash",
    "guidepoint_search_contract",
    "guidepoint_search_schema_hash",
    "guidepoint_search_spec_hash",
    "guidepoint_tool_arguments",
    "validate_guidepoint_search_spec",
    "verify_guidepoint_quote",
    "write_guidepoint_operation_narrowing",
]
