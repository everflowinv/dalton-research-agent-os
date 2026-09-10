"""P12b: what an index entry says, decided by rule first and by model last.

Four things get decided about a Claim, and only one of them is a judgement.

``importance`` is not.  It is the kind of document the claim came from, and the
Core already knows that: an SEC filing, an earnings call transcript, a broker
note, a web page.  Asking a model would be asking it to re-derive a fact the
provenance chain holds exactly.

``as_of`` is not.  It is the date the statement is *about*, read off the claim's
period when the period says a date, and off the document otherwise.  Every
answer carries ``as_of_basis`` saying which, because "2026-09-08 = the quarter
this is about ended" and "2026-09-08 = we fetched the page that day" are
different facts and comparing them as if they were the same is how a 2023 news
item ends up looking current.

``dedupe_group_ref`` is not.  For a number it is subject x measure x period x
unit; for prose it is the same subject saying the same sentence.  No embeddings
-- similarity search is a frozen item in this repository and staying inside
exact matching is what makes the grouping explainable.

Only the ``aspect`` of a *qualitative* claim is a judgement, and only that is
asked of a model: a quantitative claim's aspect follows from the measure's
name, and an industry-subject claim's aspect is ``industry`` by construction.

The model call is batched -- many claims per call as a table, the way
``company_model_spec`` learned to send structure -- and verified: the reply may
only assign vocabulary words to row ids it was shown, exactly once each.
Anything else refuses the whole batch.  Not the offending row: the whole batch.
A reply that invented a row is a reply that was not reading the table, and
keeping the rows it happened to get right would be keeping the output of a
process we have just established was not doing the task.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .claim_aspect_vocabulary import (
    ASPECTS,
    DEFINITIONS,
    FALLBACK_ASPECT,
    INDUSTRY_ASPECT,
    is_aspect,
)
from .claim_index_authority import IMPORTANCE_RANK, IMPORTANCE_TIERS, current_entries
from .cockpit_model import register_purpose
from .model_configurations import register_model_config_name
from .document_figure_grade import FILED, GRADE_BY_SPEC
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
TASK_REF = "task:claim-index-aspect-tagging:0.1"

# P14-0's registry: a lane names its own model purpose from its own module
# rather than editing a set in cockpit_model. Registered at import because the
# child imports this module before it builds a WorkOrder.
PURPOSE = register_purpose("claim_index")

# INT1: the same registry for the other half of a lane that spends money. The
# tagger runs on its own configuration file, and a configuration the cap raise
# does not know about keeps naming a superseded budget policy version -- whose
# refusal reads like a budget message rather than the wiring mistake it is.
MODEL_CONFIG_NAME = register_model_config_name("claim-index-model-config.json")

# How many claims one model call is allowed to carry, and how much of each
# statement it sees.  Both are bounds on spend, not on ambition: the router
# reserves budget against prompt *bytes*, so a batch that doubles in size
# doubles the reservation whether or not it doubles the cost.  Forty claims at
# 400 characters is roughly 20KB -- the size ``company_model_spec`` found ran
# for $0.24 where 43KB was refused before it was made.
MAX_CLAIMS_PER_BATCH = 40
MAX_STATEMENT_CHARS = 400
MAX_PROMPT_BYTES = 40_000

# The cost bound for one batch, sized like the company-model lane's: a
# reservation generous enough to be admitted, not a prediction of the price.
MAX_COST_USD = 0.60
MAX_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 2_000
TIMEOUT_SECONDS = 180


class ClaimIndexTaggingError(ValueError):
    """The claim or the reply cannot be tagged."""


class ClaimIndexTaggingRefused(ClaimIndexTaggingError):
    """A model reply went outside what it was shown; the batch is refused."""


# -- importance ------------------------------------------------------------

# The discovery spec that found the document, mapped to what the document is
# worth.  The first two rows are not restated here: ``document_figure_grade``
# already froze which document kinds are the company publishing a number and
# which are someone saying one aloud, and that table is the authority.
SPEC_IMPORTANCE: Mapping[str, str] = {
    **{spec: ("filing" if grade == FILED else "management_statement")
       for spec, grade in GRADE_BY_SPEC.items()},
    # A broker note is a real source and is not the company.
    "sell-side-reports": "sell_side",
    # The web-search specs the live discovery plan runs.  All three return
    # press coverage; none of them is the company or its bank.
    "management-changes": "news",
    "industry-demand": "news",
    "competitive-landscape": "news",
    # W3: this fund's own earlier work on the company.  Above a broker note
    # and below management, and it is the one tier that can be downgraded for
    # age -- see ``STALE_AFTER_DAYS``.
    "prior-research": "internal_prior",
}

# The fallback when the provenance chain does not reach a discovery spec --
# an older claim, a hand-registered one, a document discovered before the
# spec was recorded.  Weaker than the spec because it says less: an
# authenticated transcript is management speaking, but a broker note read
# through the same connector is not, and only the spec tells them apart.
SOURCE_TYPE_IMPORTANCE: Mapping[str, str] = {
    "official_filing": "filing",
    "authenticated_transcript": "management_statement",
    "public_web": "news",
}


# -- aspect from a measure's name -----------------------------------------

# Ordered: the first pattern that matches wins, so the specific ones come
# before the general ones.  Only applied to quantitative claims -- a number's
# aspect follows from what it measures, and a model adds nothing.
QUANTITATIVE_ASPECT_RULES: tuple[tuple[str, str], ...] = (
    (r"guidance|outlook|guided|forecast|target", "guidance_style"),
    (r"margin|cost|expense|wage|salary|utilisation|utilization|attrition|"
     r"headcount|employees|capacity|subcontract", "supply_and_cost"),
    (r"buyback|repurchase|dividend|capex|capital expenditure|acquisition|"
     r"divestiture|leverage|debt|cash flow|fcf|free cash", "management_and_capital_allocation"),
    (r"market share|share of|win rate|pricing power|competitive", "competitive_position"),
    (r"pipeline|demand|discretionary|tam|addressable|end.market", "demand_drivers"),
    (r"revenue|bookings|backlog|orders|sales|billings|segment|geograph|"
     r"vertical|service line|mix", "segments_and_mix"),
    (r"eps|earnings per share|net income|operating income|profit",
     "management_and_capital_allocation"),
    (r"share price|stock price|multiple|valuation|p/e|ev/", "history_of_price_drivers"),
)

_COMPILED_RULES = tuple(
    (re.compile(pattern), aspect) for pattern, aspect in QUANTITATIVE_ASPECT_RULES
)

# -- staleness -------------------------------------------------------------

#: How old a prior view may be before it stops counting as current thinking.
#: The owner's number, and a policy rather than a law: it is on the tagger hash
#: so that changing it re-versions every entry it touched instead of silently
#: re-reading old answers under new rules.
#:
#: Six months is not arbitrary. Two quarters is two filings and two calls; a
#: view that has not been revisited across two reporting cycles is a view
#: nobody has checked against what the company has since said.
STALE_AFTER_DAYS = 180

#: What a tier becomes once it is stale. One rung, and named in a table rather
#: than computed from the ordering, because "downgrade by one" is an
#: implementation and "a stale internal view ranks with the sell side" is a
#: decision somebody should be able to argue with.
#:
#: Only ``internal_prior`` is in here, and deliberately. A 10-K from 2023 is
#: still the company publishing that number for that period -- it does not
#: become less filed with age. A prior *view* is the only kind of claim whose
#: whole content is "this is what we thought", and it is the only one for
#: which the passage of time is itself evidence against.
STALE_DOWNGRADE: Mapping[str, str] = {"internal_prior": "sell_side"}

#: The word appended to ``importance_basis`` when the downgrade fires. There is
#: no boolean column for it: ``importance_basis`` is already the field that
#: says why a claim has the weight it has, and the entry contract's field set
#: is closed, so a flag would be a schema change to say something the reason
#: string already says.
STALE_MARK = "may_be_stale"


def stale_importance(
    importance: str,
    importance_basis: str,
    *,
    as_of: str | None,
    as_of_basis: str,
    now: date,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> tuple[str, str]:
    """``(importance, importance_basis)`` after the age rule has had its say.

    A claim with no usable date is not aged -- it is already at the bottom of
    every ordering that matters, and inventing an age for it would be the same
    mistake the prior-research feed refuses to make at the other end.
    """

    if importance not in STALE_DOWNGRADE or as_of is None:
        return importance, importance_basis
    if as_of_basis == "unknown":
        return importance, importance_basis
    try:
        age = (now - date.fromisoformat(as_of)).days
    except ValueError:
        return importance, importance_basis
    if age < stale_after_days:
        return importance, importance_basis
    return (
        STALE_DOWNGRADE[importance],
        f"{importance_basis};{STALE_MARK}:{age}d>{stale_after_days}d:{importance}",
    )


RULE_TAGGER_REF = "rule:claim-index-deterministic:0.1"
# The hash of the rules themselves, so that changing a rule changes the tagger
# and every claim it touched gets a new entry version rather than silently
# keeping an answer produced by rules that no longer exist.
RULE_TAGGER_HASH = content_hash({
    "tagger": RULE_TAGGER_REF,
    "spec_importance": dict(SPEC_IMPORTANCE),
    "source_type_importance": dict(SOURCE_TYPE_IMPORTANCE),
    "quantitative_aspects": [list(item) for item in QUANTITATIVE_ASPECT_RULES],
    "importance_tiers": list(IMPORTANCE_TIERS),
    "stale_after_days": STALE_AFTER_DAYS,
    "stale_downgrade": dict(STALE_DOWNGRADE),
})

TASK_HASH = content_hash({
    "task": TASK_REF,
    "vocabulary": {word: DEFINITIONS[word] for word in ASPECTS},
    "output": "one line per shown row: <row id><TAB><aspect>",
    "authority": "assigns_only_shown_rows_only_vocabulary_words",
})


# -- text folding ----------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s%/.-]", re.UNICODE)


def fold(value: Any) -> str:
    """Lower-case, NFKC, collapse whitespace, drop decorative punctuation.

    Deterministic and total: two strings that differ only in how they were
    typed fold together, and nothing else does.  This is the whole of the
    "same sentence" test -- no stemming, no embedding, no similarity.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).lower()
    text = _PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


# -- period -> a date ------------------------------------------------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# The shape the SEC lane writes: an explicit start and end, which is a period
# and not a label at all.  Live, every quantitative claim uses it, and reading
# the first year out of it instead -- as a bare-year rule would -- collapsed
# three different Accenture quarters into "2026" and merged them.
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})$")
_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER_RE = re.compile(r"\bq([1-4])\s*(?:fy)?\s*(\d{2,4})\b")
_QUARTER_SUFFIX_RE = re.compile(r"\b([1-4])q\s*(?:fy)?\s*(\d{2,4})\b")
_YEAR_QUARTER_RE = re.compile(r"\b(\d{4})\s*q([1-4])\b")
_HALF_RE = re.compile(r"\bh([12])\s*(?:fy)?\s*(\d{2,4})\b")
# Anchored, unlike the quarter and half patterns.  "Q2 2026 guidance" places a
# figure in the calendar; "revenue in 2025 and 2026" does not, and a bare-year
# rule that searched would have answered anyway.
_YEAR_RE = re.compile(r"^(?:fy|fiscal(?:\s+year)?)?\s*((?:19|20)\d{2})$")

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_MONTH_END = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def _four_digit_year(raw: str) -> int | None:
    year = int(raw)
    if len(raw) == 2:
        year += 2000
    if 1990 <= year <= 2099:
        return year
    return None


def _month_end(year: int, month: int) -> str:
    day = _MONTH_END[month]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        day = 29
    return date(year, month, day).isoformat()


def period_as_of(period: Any) -> tuple[str | None, str]:
    """The date a period is about, and what that date was read off.

    A structured period (Ledger 0.2) carries its own end and is used as given.
    A label is parsed only in shapes that place a figure unambiguously in the
    calendar -- ``2026-06-30``, ``Q2 2026``, ``2Q26``, ``H1 2026``, ``FY2025``,
    ``2025``.  Live, most qualitative periods are ``current`` or ``not
    specified``, which place a figure nowhere and get no date at all.

    A fiscal-year label resolves to the *calendar* year end.  Accenture's
    fiscal 2025 ended in August, not December, so this is an approximation --
    an honest one, because ``as_of_basis`` says ``period_label`` and a reader
    who needs the exact fiscal close has the filing.  P12a can sharpen it with
    the company's own statement calendar; guessing per company here would put
    a second, unciteable fiscal calendar in the repository.
    """

    if isinstance(period, Mapping):
        end = period.get("end")
        if isinstance(end, str) and len(end) >= 10:
            try:
                date.fromisoformat(end[:10])
            except ValueError:
                return None, "unknown"
            return end[:10], "period_end"
        return None, "unknown"
    text = fold(period)
    if not text:
        return None, "unknown"
    span = _RANGE_RE.match(text)
    if span is not None:
        try:
            start, end = date.fromisoformat(span.group(1)), date.fromisoformat(span.group(2))
        except ValueError:
            return None, "unknown"
        if start > end:
            return None, "unknown"
        return end.isoformat(), "period_end"
    iso = _ISO_RE.match(text)
    if iso is not None:
        try:
            return date.fromisoformat(text).isoformat(), "period_label"
        except ValueError:
            return None, "unknown"
    year_month = _YEAR_MONTH_RE.match(text)
    if year_month is not None:
        year, month = int(year_month.group(1)), int(year_month.group(2))
        if 1 <= month <= 12:
            return _month_end(year, month), "period_label"
        return None, "unknown"
    for pattern, quarter_first in (
        (_QUARTER_RE, True), (_QUARTER_SUFFIX_RE, True), (_YEAR_QUARTER_RE, False),
    ):
        match = pattern.search(text)
        if match is None:
            continue
        quarter = int(match.group(1) if quarter_first else match.group(2))
        year = _four_digit_year(match.group(2) if quarter_first else match.group(1))
        if year is None:
            continue
        month, day = _QUARTER_END[quarter]
        return date(year, month, day).isoformat(), "period_label"
    half = _HALF_RE.search(text)
    if half is not None:
        year = _four_digit_year(half.group(2))
        if year is not None:
            month, day = (6, 30) if half.group(1) == "1" else (12, 31)
            return date(year, month, day).isoformat(), "period_label"
    year_only = _YEAR_RE.match(text)
    if year_only is not None:
        year = _four_digit_year(year_only.group(1))
        if year is not None:
            return date(year, 12, 31).isoformat(), "period_label"
    return None, "unknown"


def period_key(period: Any) -> str:
    """A stable text key for a period, structured or not."""

    if isinstance(period, Mapping):
        return canonical_json(period)
    return "" if period is None else str(period)


# -- provenance ------------------------------------------------------------

def _url_hash(document_ref: str) -> str | None:
    """The url digest shared by the two shapes a web document ref takes.

    A discovered web document is ``public-web-url:sha256:<url>`` and the
    citation that quotes it is ``public-web-document:url-sha256:<url>:body-
    sha256:<body>``.  Same page, two refs, and the url digest is what they
    agree on.
    """

    for prefix in ("public-web-url:sha256:", "public-web-document:url-sha256:"):
        if document_ref.startswith(prefix):
            return document_ref[len(prefix):].split(":", 1)[0]
    return None


def _table_exists(connection: Any, name: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


class ProvenanceResolver:
    """Claim -> evidence -> document -> discovery spec, read once per Core.

    The maps are small (a few thousand rows live) and are read whole because
    every claim in a batch needs the same three of them; walking them per claim
    would be a join per claim against tables that fit in memory.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._document_by_correction: dict[str, str] = {}
        self._spec_by_document: dict[str, str] = {}
        self._spec_by_url: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        connection = self.connection
        if _table_exists(connection, "transcript_correction_set_versions"):
            for row in connection.execute(
                "SELECT version_id, record_json FROM transcript_correction_set_versions"
            ):
                try:
                    record = json.loads(row["record_json"])
                except (TypeError, ValueError):
                    continue
                document_ref = record.get("document_ref")
                if isinstance(document_ref, str) and document_ref:
                    self._document_by_correction[row["version_id"]] = document_ref
        if not (
            _table_exists(connection, "coverage_mission_discovered_documents")
            and _table_exists(connection, "coverage_mission_source_discoveries")
        ):
            return
        spec_by_discovery = {
            row["record_id"]: row["spec_ref"]
            for row in connection.execute(
                "SELECT record_id, spec_ref FROM coverage_mission_source_discoveries"
            )
        }
        for row in connection.execute(
            "SELECT document_ref, discovery_ref FROM coverage_mission_discovered_documents"
        ):
            spec = spec_by_discovery.get(row["discovery_ref"])
            if not spec:
                continue
            self._spec_by_document[row["document_ref"]] = spec
            digest = _url_hash(row["document_ref"])
            if digest is not None:
                self._spec_by_url[digest] = spec

    def spec_for_document(self, document_ref: str | None) -> str | None:
        if not document_ref:
            return None
        spec = self._spec_by_document.get(document_ref)
        if spec is not None:
            return spec
        digest = _url_hash(document_ref)
        return None if digest is None else self._spec_by_url.get(digest)

    def evidence_for_claim(self, claim_version_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT e.evidence_json FROM evidence_relations r "
            "JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id "
            "WHERE r.claim_version_id=?",
            (claim_version_ref,),
        ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            try:
                found.append(json.loads(row["evidence_json"]))
            except (TypeError, ValueError):
                continue
        return found

    def _published_at(self, source_envelope_ref: Any) -> str | None:
        if not isinstance(source_envelope_ref, str) or not source_envelope_ref:
            return None
        if not _table_exists(self.connection, "connector_source_envelopes"):
            return None
        row = self.connection.execute(
            "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
            (source_envelope_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            return None
        published = record.get("published_at")
        return published if isinstance(published, str) and published else None

    def resolve(self, claim_version_ref: str) -> dict[str, Any]:
        """Everything the rules need to know about where a claim came from."""

        best: dict[str, Any] = {
            "importance": "other",
            "importance_basis": "no evidence relation",
            "document_ref": None,
            "spec_ref": None,
            "published_at": None,
            "retrieved_at": None,
        }
        for evidence in self.evidence_for_claim(claim_version_ref):
            document_ref = None
            for item in evidence.get("source_lineage") or []:
                if item in self._document_by_correction:
                    document_ref = self._document_by_correction[item]
            spec_ref = self.spec_for_document(document_ref)
            if spec_ref is not None and spec_ref in SPEC_IMPORTANCE:
                importance = SPEC_IMPORTANCE[spec_ref]
                basis = f"discovery_spec:{spec_ref}"
            else:
                source_type = evidence.get("source_type")
                importance = SOURCE_TYPE_IMPORTANCE.get(str(source_type), "other")
                basis = f"evidence_source_type:{source_type}"
            retrieved = evidence.get("retrieved_at")
            candidate = {
                "importance": importance,
                "importance_basis": basis,
                "document_ref": document_ref,
                "spec_ref": spec_ref,
                "published_at": self._published_at(evidence.get("source_envelope_ref")),
                "retrieved_at": retrieved if isinstance(retrieved, str) else None,
            }
            # A claim supported by two documents takes the stronger one: the
            # weaker source did not make the claim less well sourced.
            if IMPORTANCE_RANK[candidate["importance"]] < IMPORTANCE_RANK[best["importance"]]:
                best = candidate
            elif best["retrieved_at"] is None and candidate["retrieved_at"] is not None \
                    and candidate["importance"] == best["importance"]:
                best = candidate
        return best


# -- the deterministic pass -----------------------------------------------

def quantitative_aspect(metric_or_aspect: Any) -> str:
    """The aspect a measure's name implies, or ``other``.

    ``other`` here means "these rules do not recognise this measure", which is
    a fact about the rules and is meant to be read as one: a live distribution
    with a lot of ``other`` quantitative claims is the signal to add a row to
    the table above, not to send numbers to a model.
    """

    folded = fold(metric_or_aspect)
    for pattern, aspect in _COMPILED_RULES:
        if pattern.search(folded):
            return aspect
    return FALLBACK_ASPECT


def is_industry_subject(subject_ref: Any) -> bool:
    return isinstance(subject_ref, str) and subject_ref.startswith("industry:")


def dedupe_group_key(claim: Mapping[str, Any]) -> str:
    """What makes two claims the same fact.

    For a number: the same company, the same measure, the same **period as the
    claim states it**, the same basis and the same unit.  This is the ACN
    complaint precisely -- one quarter's revenue present as three Claims and
    cited three times side by side in the same Initial Screen paragraph.

    Three things this key deliberately is *not*, each of which merged claims
    that are not the same fact:

    * **not the resolved ``as_of``.**  ``FY2025`` and ``Q4 2025`` both resolve
      to 2025-12-31 and are different figures; the period key keeps them apart.
      ``as_of`` orders a group, it does not define one.
    * **not period-free.**  When the period does not parse, the claim gets its
      own group and is never merged with anything.  The alternative -- falling
      back to the date the evidence was fetched -- would have grouped every
      undated claim about one company and one measure that arrived on the same
      day, which live is 1,462 of 2,170 claims, and with ``canonical_only``
      defaulting to true those distinct facts would simply stop being returned.
    * **not basis-free.**  GAAP and non-GAAP operating margin for one quarter
      are two numbers a reader must see side by side, not one with the other
      hidden behind it.

    For prose: the same company saying the same sentence.  Exact text after
    folding case and whitespace, and nothing cleverer; two differently worded
    statements of the same idea stay two claims, which is the conservative
    error.  Merging them would need a similarity model, and similarity search
    is frozen here until the exact layer is proven.
    """

    subject = claim.get("subject_ref") or ""
    if claim.get("claim_kind") == "quantitative":
        resolved, _basis = period_as_of(claim.get("period"))
        if resolved is None:
            # The period places this figure nowhere -- "current", "ongoing",
            # "not specified" -- so it is only ever the same fact as itself and
            # gets a group of one. Keyed on the claim's own identity, so that
            # two undated numbers can never be merged by accident.
            identity = (
                claim.get("claim_version_ref") or claim.get("claim_ref")
                or canonical_json({
                    "metric": fold(claim.get("metric_or_aspect")),
                    "basis": fold(claim.get("basis")),
                    "unit": fold(claim.get("unit")),
                    "period": fold(period_key(claim.get("period"))),
                    "statement": fold(claim.get("normalized_statement")),
                    "value": str(claim.get("value")),
                })
            )
            return "|".join((
                "quant-ungrouped", str(subject),
                hashlib.sha256(str(identity).encode("utf-8")).hexdigest(),
            ))
        # The period **as the claim states it**, not the date it resolves to:
        # FY2025 and Q4 2025 both land on 2025-12-31 and are different figures.
        return "|".join((
            "quant", str(subject), fold(claim.get("metric_or_aspect")),
            fold(period_key(claim.get("period"))),
            fold(claim.get("basis")) or "-", fold(claim.get("unit")) or "-",
        ))
    statement = fold(claim.get("normalized_statement"))
    return "|".join((
        "qual", str(subject),
        hashlib.sha256(statement.encode("utf-8")).hexdigest(),
    ))


def rule_tags(
    claim: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    now: date | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Everything the rules can decide about one claim.

    ``aspect`` is ``None`` when only a model can answer -- a qualitative claim
    about a company.  Every other field is always decided here.

    ``now`` is the day the age rule is measured against.  It defaults to today
    rather than being required, so that every existing caller keeps working;
    it is a parameter at all so that a test can stand on a fixed date and so
    that a backfill can age a claim against the day it is re-tagging for.
    """

    as_of, as_of_basis = period_as_of(claim.get("period"))
    if as_of is None:
        published = provenance.get("published_at")
        retrieved = provenance.get("retrieved_at")
        if isinstance(published, str) and len(published) >= 10:
            as_of, as_of_basis = published[:10], "document_published_at"
        elif isinstance(retrieved, str) and len(retrieved) >= 10:
            as_of, as_of_basis = retrieved[:10], "evidence_retrieved_at"
    try:
        if as_of is not None:
            date.fromisoformat(as_of)
    except ValueError:
        as_of, as_of_basis = None, "unknown"
    if as_of is None:
        as_of_basis = "unknown"

    if is_industry_subject(claim.get("subject_ref")):
        aspect: str | None = INDUSTRY_ASPECT
        aspect_source: str | None = "rule"
    elif claim.get("claim_kind") == "quantitative":
        aspect, aspect_source = quantitative_aspect(claim.get("metric_or_aspect")), "rule"
    else:
        aspect, aspect_source = None, None
    importance, importance_basis = stale_importance(
        provenance.get("importance", "other"),
        provenance.get("importance_basis", "unknown"),
        as_of=as_of,
        as_of_basis=as_of_basis,
        now=now or date.today(),
        stale_after_days=stale_after_days,
    )
    return {
        "aspect": aspect,
        "aspect_source": aspect_source,
        "as_of": as_of,
        "as_of_basis": as_of_basis,
        "importance": importance,
        "importance_basis": importance_basis,
        "dedupe_group_key": dedupe_group_key(claim),
        "period_key": period_key(claim.get("period")) or "-",
        "tagger_ref": RULE_TAGGER_REF,
        "tagger_hash": RULE_TAGGER_HASH,
    }


# -- the model pass --------------------------------------------------------

def build_batch(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_claims: int = MAX_CLAIMS_PER_BATCH,
    max_statement_chars: int = MAX_STATEMENT_CHARS,
    max_prompt_bytes: int = MAX_PROMPT_BYTES,
) -> list[dict[str, Any]]:
    """The claims one call will carry, each with the short id it is shown as.

    Bounded twice: by count and by the size of the prompt the batch would
    produce.  The second bound is the one that matters, because a batch of
    long statements is a bigger reservation than a batch of short ones and the
    router refuses on the reservation.
    """

    batch: list[dict[str, Any]] = []
    for row in rows:
        if len(batch) >= max_claims:
            break
        statement = str(row.get("normalized_statement") or "").strip()
        item = {
            "row_id": str(len(batch) + 1),
            "claim_version_ref": row["claim_version_ref"],
            "subject_ref": row.get("subject_ref") or "",
            "metric_or_aspect": str(row.get("metric_or_aspect") or "").strip(),
            "period": period_key(row.get("period")),
            "statement": statement[:max_statement_chars],
        }
        trial = batch + [item]
        if len(build_prompt(trial).encode("utf-8")) > max_prompt_bytes and batch:
            break
        batch = trial
    return batch


def _row_line(item: Mapping[str, Any]) -> str:
    def clean(value: Any) -> str:
        return _WHITESPACE_RE.sub(" ", str(value or "")).replace("\t", " ").strip()

    return "\t".join((
        item["row_id"], clean(item["subject_ref"]), clean(item["metric_or_aspect"]),
        clean(item["period"]), clean(item["statement"]),
    ))


def build_prompt(batch: Sequence[Mapping[str, Any]]) -> str:
    """The tagging prompt: two tables and a rule about what may come back."""

    return (
        "You file research claims into a company dossier.\n\n"
        "Each claim below is one statement this system holds about one "
        "subject. Decide which section of the dossier it belongs in.\n\n"
        "SECTIONS is one section per row, tab separated -- <aspect>\\t"
        "<what belongs there>:\n"
        f"{_vocabulary_block()}\n\n"
        "CLAIMS is one claim per row, tab separated:\n"
        "  <row id>\\t<subject>\\t<measure or aspect>\\t<period>\\t<statement>\n\n"
        "Rules:\n"
        "* Answer for every row you were shown, once each, and for no other "
        "row. Do not invent a row id.\n"
        "* Use only the aspect words in SECTIONS, copied exactly.\n"
        "* Choose the section the claim is *about*, not the section of the "
        "document it came from.\n"
        "* Use 'other' only when the claim is genuinely none of the eleven; "
        "it is an answer, not a way of saying you are unsure.\n"
        "* Return the answer table and nothing else: one line per row, "
        "<row id><TAB><aspect>. No prose, no header, no code fence.\n\n"
        f"CLAIMS:\n{chr(10).join(_row_line(item) for item in batch)}\n"
    )


def _vocabulary_block() -> str:
    from .claim_aspect_vocabulary import vocabulary_table

    return vocabulary_table()


def prompt_tagger(work_order_ref: str, prompt: str) -> tuple[str, str]:
    """The tagger ref and hash a model-assigned aspect is recorded under.

    The ref is the WorkOrder -- the exact, replayable call -- and the hash
    binds the task definition and the bytes that were actually sent, so a
    prompt change is visible without re-reading the call.
    """

    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return (
        f"model:{work_order_ref}",
        content_hash({"task_hash": TASK_HASH, "prompt_sha256": digest}),
    )


def aspects_from_response(
    batch: Sequence[Mapping[str, Any]], text: Any
) -> dict[str, str]:
    """The reply, or a refusal of the whole batch.

    Verified against the batch, not merely parsed: exactly the shown row ids,
    each once, each with a vocabulary word.  A reply that names a row that was
    not shown is not a partially correct reply; it is evidence the reply was
    not produced from the table, so nothing in it is kept.
    """

    if not isinstance(text, str) or not text.strip():
        raise ClaimIndexTaggingRefused("model returned no tagging table")
    shown = {item["row_id"]: item["claim_version_ref"] for item in batch}
    if not shown:
        raise ClaimIndexTaggingError("an empty batch cannot be tagged")
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    assigned: dict[str, str] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [part for part in re.split(r"[\s,|]+", line) if part]
        if len(parts) != 2:
            raise ClaimIndexTaggingRefused(
                f"tagging row is not '<row id><TAB><aspect>': {line!r}"
            )
        row_id, aspect = parts
        if row_id not in shown:
            raise ClaimIndexTaggingRefused(
                f"tagging names row {row_id!r}, which was not in the batch"
            )
        if row_id in assigned:
            raise ClaimIndexTaggingRefused(f"tagging assigns row {row_id!r} twice")
        if not is_aspect(aspect):
            raise ClaimIndexTaggingRefused(
                f"{aspect!r} is not one of the aspect words the batch listed"
            )
        assigned[row_id] = aspect
    missing = sorted(set(shown) - set(assigned), key=int)
    if missing:
        raise ClaimIndexTaggingRefused(
            "tagging left rows unanswered: " + ", ".join(missing)
        )
    return {shown[row_id]: aspect for row_id, aspect in assigned.items()}


def pending_claims(
    store: Any,
    *,
    subject_refs: Iterable[str] | None = None,
    limit: int = 200,
    snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Claims with no current index entry, or one for an older claim version.

    Computed from the Ledger every time rather than written down and drained.
    A queue of "claims still to tag" would be a second place the truth lives,
    and the truth is already exactly "the latest version of every claim, minus
    what the index already covers".
    """

    snapshot = snapshot if snapshot is not None else store.claim_index_snapshot()
    versions = {
        row["claim_version_id"]: row
        for row in snapshot.get("claim_versions") or []
    }
    wanted = None if subject_refs is None else set(subject_refs)
    entries = current_entries(store.connection)
    pending: list[dict[str, Any]] = []
    for claim_ref, version_ref in sorted(
        (snapshot.get("latest_claim_version_refs") or {}).items()
    ):
        row = versions.get(version_ref)
        if row is None:
            continue
        claim = row["claim"]
        if wanted is not None and claim.get("subject_ref") not in wanted:
            continue
        existing = entries.get(version_ref)
        if existing is not None and existing["claim_version_hash"] == row["content_hash"]:
            continue
        pending.append({
            "claim_ref": claim_ref,
            "claim_version_ref": version_ref,
            "claim_version_hash": row["content_hash"],
            "claim_created_at": row["created_at"],
            "subject_ref": claim.get("subject_ref"),
            "metric_or_aspect": claim.get("metric_or_aspect"),
            "period": claim.get("period"),
            "claim_kind": claim.get("claim_kind"),
            "unit": claim.get("unit"),
            # Part of the dedupe key: GAAP and non-GAAP operating margin for
            # one quarter are two numbers, not one hiding the other.
            "basis": claim.get("basis"),
            "value": claim.get("value"),
            "normalized_statement": claim.get("normalized_statement"),
        })
        if len(pending) >= limit:
            break
    return pending


__all__ = [
    "MAX_CLAIMS_PER_BATCH",
    "MAX_COST_USD",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PROMPT_BYTES",
    "MAX_STATEMENT_CHARS",
    "QUANTITATIVE_ASPECT_RULES",
    "RULE_TAGGER_HASH",
    "RULE_TAGGER_REF",
    "SOURCE_TYPE_IMPORTANCE",
    "SPEC_IMPORTANCE",
    "TASK_HASH",
    "TASK_REF",
    "TIMEOUT_SECONDS",
    "ClaimIndexTaggingError",
    "ClaimIndexTaggingRefused",
    "ProvenanceResolver",
    "aspects_from_response",
    "build_batch",
    "build_prompt",
    "dedupe_group_key",
    "fold",
    "is_industry_subject",
    "pending_claims",
    "period_as_of",
    "period_key",
    "prompt_tagger",
    "quantitative_aspect",
    "rule_tags",
]
