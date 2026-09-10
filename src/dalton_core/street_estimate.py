"""P11b: what one broker published about one company, and who it was.

A ``StreetEstimateClaim`` is the second half of the consensus question and the
half nobody sells for free: the sell-side notes are already in this ledger,
they were bought and acquired under governance, and the price target on their
first page is the single most-quoted number in the industry. What has been
missing is the rule for reading them without lying about what was read.

Three rules, and the whole module is them.

**A broker's estimate is that broker's statement.** It is never a fact about
the company. The grade says so (``document_figure_grade.BROKER_RESEARCH``,
which is not admissible as a quantitative Claim anywhere), the basis says so
(``broker-estimate``), and the qualifier appended to every statement says so in
a sentence a human reads without following a reference. A price target filed as
if it were the company's own number would be the worst kind of error this
system can make, because every check downstream would pass.

**The digits are checked against the bytes.** Every figure goes through
``document_numeric_claim.verify_numeric_candidate`` against the exact quote it
was read from, exactly as a filed figure does. The extractor here is
deterministic rather than a model, which does not make the check redundant: a
regex that matches the wrong span is as wrong as a model that invents, and the
verification is what makes the difference visible.

**One broker is never a consensus.** Two independent houses publishing a target
within a window are a *range*; the same house twice is one house with two
notes, which is exactly the failure the debate map's counting rule was written
to prevent and it is repeated here rather than assumed. ``report_consensus``
refuses to build a block from fewer than two distinct brokers, and DXC -- whose
only two live targets are both TD Cowen's -- is the case that proves it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .document_figure_grade import BROKER_RESEARCH, basis_for, qualify
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
SOURCE_GRADE = BROKER_RESEARCH
BASIS = "broker-estimate"
NUMERIC_BASIS = BASIS
OBSERVER_REF = "core:street-estimate-extractor"
# The discovery spec kinds whose documents are broker research. A document of
# any other kind is refused rather than read: an earnings call transcript has
# no price target in it and an industry note belongs to no company.
SELL_SIDE_SPEC_REFS: tuple[str, ...] = ("sell-side-reports",)
# The AlphaEngine document codes that are sell-side research, for the lane that
# reads the library's own metadata rather than the discovery plan.
SELL_SIDE_DOCUMENT_CODES: tuple[str, ...] = (
    "foreignReport", "domesticReport", "sellSideReport", "researchReport",
)
EXTRACTION_METHODS = ("deterministic", "model")
BROKER_BASES = ("document_metadata", "page_text")
RATING_CODES = ("buy", "hold", "sell")
SCAN_OUTCOMES = ("recorded", "refused")

# The metric slots a street estimate may carry. Kebab-case metric refs, because
# ``document_numeric_claim`` requires the slot that was asked for to look like
# one.
TARGET_METRIC = "metric:price-target"
EPS_METRIC = "metric:eps-estimate"
REVENUE_METRIC = "metric:revenue-estimate"
ESTIMATE_METRICS = (TARGET_METRIC, EPS_METRIC, REVENUE_METRIC)

_SCHEMA_PATH = Path(__file__).with_name("street_estimate_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class StreetEstimateError(RuntimeError):
    """Base error for the street estimate authority."""


class StreetEstimateValidationError(StreetEstimateError, ValueError):
    """A request does not satisfy the closed contract."""


class StreetEstimateConflict(StreetEstimateError):
    """A request conflicts with the append-only table."""


# -- who published it -------------------------------------------------------
#
# Slugs match ``debate_map.DEBATE_POLICY["publishers"]`` where the two overlap,
# on purpose: "two TD notes are one source" has to mean the same thing in the
# debate map and in a consensus range, and a test asserts they have not drifted
# apart. Longest pattern first at match time, so "td securities" is not eaten
# by a shorter entry.
BROKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("td", ("td cowen", "td securities", "td bank")),
    ("wells-fargo", ("wells fargo",)),
    ("deutsche-bank", ("deutsche bank", "deutsche")),
    ("wolfe", ("wolfe research", "wolfe")),
    ("jpmorgan", ("j.p. morgan", "jp morgan", "jpmorgan", "j.p.morgan")),
    ("morgan-stanley", ("morgan stanley",)),
    ("hsbc", ("hsbc",)),
    ("rbc", ("rbc capital", "rbc")),
    ("bernstein", ("bernstein",)),
    ("ubs", ("ubs",)),
    ("barclays", ("barclays",)),
    ("citi", ("citigroup", "citi research", "citi")),
    ("goldman", ("goldman sachs", "goldman")),
    ("bofa", ("bofa", "bank of america", "merrill")),
    ("jefferies", ("jefferies",)),
    ("evercore", ("evercore",)),
    ("mizuho", ("mizuho",)),
    ("baird", ("robert w. baird", "baird")),
    ("stifel", ("stifel",)),
    ("needham", ("needham",)),
    ("guggenheim", ("guggenheim",)),
    ("keybanc", ("keybanc",)),
    ("piper", ("piper sandler", "piper")),
    ("raymond-james", ("raymond james",)),
    ("truist", ("truist",)),
    ("bmo", ("bmo capital", "bmo")),
    ("scotiabank", ("scotiabank",)),
    ("susquehanna", ("susquehanna",)),
    ("oppenheimer", ("oppenheimer",)),
    ("macquarie", ("macquarie",)),
    ("william-blair", ("william blair",)),
)

# -- what it said -----------------------------------------------------------
#
# Every house runs one of a handful of three-tier scales under different words.
# The words are normalised to three codes; the broker's own word is kept beside
# the code so the mapping is checkable rather than trusted.
#
# The two-letter forms are the dangerous part and are why this is a *per-scale*
# table rather than one global alias list. "UP" is Underperform at RBC and the
# most common two-letter string in English prose; "SP" is Sector Perform at RBC
# and "S&P" everywhere. They are admitted only for a house whose scale actually
# uses them, only in upper case, and only next to a rating cue -- see
# ``street_estimate_extraction``.
RATING_SCALES: Mapping[str, Mapping[str, str]] = {
    # Morgan Stanley, J.P. Morgan, Wells Fargo, Barclays, KeyBanc, Piper.
    # J.P. Morgan calls the middle rung Neutral and Morgan Stanley calls it
    # Equal-weight; they are the same rung of the same scale.
    "overweight": {
        "overweight": "buy", "ow": "buy",
        "equal weight": "hold", "equalweight": "hold", "ew": "hold",
        "neutral": "hold", "in line": "hold", "inline": "hold",
        "underweight": "sell", "uw": "sell",
    },
    # RBC, Wolfe, Bernstein, Evercore, Baird, Mizuho, Oppenheimer, BMO,
    # Macquarie, Raymond James.
    "outperform": {
        "outperform": "buy", "op": "buy", "strong buy": "buy",
        "market perform": "hold", "mp": "hold",
        "sector perform": "hold", "sp": "hold",
        "peer perform": "hold", "pp": "hold",
        "in line": "hold", "neutral": "hold",
        "underperform": "sell", "up": "sell",
    },
    # TD Cowen, Deutsche Bank, HSBC, Jefferies, Stifel, Needham, Truist.
    "buy-hold-sell": {
        "buy": "buy", "strong buy": "buy",
        "hold": "hold", "neutral": "hold",
        "sell": "sell", "reduce": "sell",
    },
    # UBS, Citi, Goldman, BofA, Guggenheim.
    "buy-neutral-sell": {
        "buy": "buy", "neutral": "hold", "sell": "sell",
    },
}

# Which scale each house runs. A house with no entry gets its target price
# recorded and its rating refused with a reason, which is the right trade: the
# number is unambiguous and the word is not.
BROKER_SCALES: Mapping[str, tuple[str, ...]] = {
    "morgan-stanley": ("overweight",),
    "jpmorgan": ("overweight",),
    "wells-fargo": ("overweight",),
    "barclays": ("overweight",),
    "keybanc": ("overweight",),
    "piper": ("overweight",),
    "rbc": ("outperform",),
    "wolfe": ("outperform",),
    "bernstein": ("outperform",),
    "evercore": ("outperform",),
    "baird": ("outperform",),
    "mizuho": ("outperform",),
    "oppenheimer": ("outperform",),
    "bmo": ("outperform",),
    "macquarie": ("outperform",),
    "raymond-james": ("outperform",),
    "td": ("buy-hold-sell",),
    "deutsche-bank": ("buy-hold-sell",),
    "hsbc": ("buy-hold-sell",),
    "jefferies": ("buy-hold-sell",),
    "stifel": ("buy-hold-sell",),
    "needham": ("buy-hold-sell",),
    "truist": ("buy-hold-sell",),
    "william-blair": ("buy-hold-sell",),
    "ubs": ("buy-neutral-sell",),
    "citi": ("buy-neutral-sell",),
    "goldman": ("buy-neutral-sell",),
    "bofa": ("buy-neutral-sell",),
    "guggenheim": ("buy-neutral-sell",),
}

# The cross-verification rule, as a record, so a stored range says which rule
# produced it. Shaped after ``metric_discovery``'s corroboration rule and for
# the same reason: one document is an anecdote.
CONSENSUS_POLICY: Mapping[str, Any] = {
    "policy_ref": "street-consensus-policy:p11b:0.1",
    # Two is the smallest number that is not one. It separates "the street
    # thinks" from "a broker thinks", and no smaller number can.
    "min_independent_brokers": 2,
    # Counted in distinct *houses*, never in notes. Two TD Cowen notes on DXC
    # published the same day carrying the same $11.00 target are one house
    # saying one thing, and counting them as two would manufacture a consensus
    # out of a single opinion.
    "counted_by": "broker",
    # How stale a target may be and still belong to the same range. A quarter:
    # long enough that a five-house universe has something to compare, short
    # enough that a target set before the last earnings report is not averaged
    # with one set after it.
    "window_days": 90,
    # The same house publishing twice inside the window contributes its newest
    # note and nothing else.
    "duplicate_broker": "newest_note_wins",
}
CONSENSUS_POLICY_REF: str = str(CONSENSUS_POLICY["policy_ref"])
CONSENSUS_POLICY_HASH: str = content_hash(CONSENSUS_POLICY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StreetEstimateValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise StreetEstimateValidationError(f"{name} must be lowercase SHA-256")
    return value


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise StreetEstimateValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _decimal_string(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or (
        _DECIMAL_RE.fullmatch(value) is None
    ):
        raise StreetEstimateValidationError(f"{name} must be a canonical decimal string")
    return value


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def broker_slug(name: Any) -> str | None:
    """The house a name belongs to, or None if it names no house we know.

    Longest pattern first, so "TD Securities (USA) LLC" is TD and not something
    a two-letter pattern happened to catch.
    """

    if not isinstance(name, str) or not name.strip():
        return None
    lowered = name.lower()
    best: tuple[int, str] | None = None
    for slug, patterns in BROKERS:
        for pattern in patterns:
            if pattern in lowered and (best is None or len(pattern) > best[0]):
                best = (len(pattern), slug)
    return None if best is None else best[1]


def rating_scales_for(broker: Any) -> tuple[str, ...]:
    """The rating scales this house is known to run."""

    if not isinstance(broker, str):
        return ()
    return tuple(BROKER_SCALES.get(broker, ()))


def normalise_rating(word: Any, *, broker: str) -> dict[str, Any]:
    """One broker's rating word, as one of three codes.

    Refused rather than guessed when the house's scale does not contain the
    word. "Overweight" in an RBC note is not RBC's rating -- RBC does not have
    that rung -- it is a sentence about somebody else's, and recording it would
    put another house's view under RBC's name.
    """

    text = _text(word, "rating")
    folded = re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("-", " "))
    folded = re.sub(r"\s+", " ", folded).strip()
    scales = rating_scales_for(broker)
    if not scales:
        raise StreetEstimateValidationError(
            f"no rating scale is declared for {broker!r}; its rating word "
            "cannot be normalised without inventing the scale"
        )
    for scale in scales:
        table = RATING_SCALES[scale]
        code = table.get(folded)
        if code is not None:
            return {"code": code, "as_named": text, "scale": scale}
    raise StreetEstimateValidationError(
        f"{text!r} is not a rung of {broker}'s scale ({', '.join(scales)}); "
        "a rating word from another house's scale is about another house"
    )


def qualified_statement(statement: str) -> str:
    """A street estimate's own sentence, with what its grade means appended."""

    return qualify(statement, SOURCE_GRADE)


# -- the record -------------------------------------------------------------


def validate_estimate(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one broker's statement about one company.

    ``figures`` are the outputs of ``verify_numeric_candidate`` -- already
    checked against the bytes they cite -- and are re-checked here for shape so
    a caller cannot store a figure that never went through it.
    """

    if not isinstance(value, Mapping):
        raise StreetEstimateValidationError("a street estimate must be an object")
    required = {
        "company_ref", "document_ref", "spec_ref", "source_manifest_hash",
        "broker", "broker_as_named", "broker_basis", "analysts", "published_on",
        "subject_as_named", "rating", "target_price", "figures",
        "extraction_method",
    }
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown or missing:
        raise StreetEstimateValidationError(
            f"street estimate has invalid closed shape; missing={missing}, "
            f"unknown={unknown}"
        )
    wire: dict[str, Any] = {
        "company_ref": _text(value["company_ref"], "company_ref"),
        "document_ref": _text(value["document_ref"], "document_ref"),
        "spec_ref": _text(value["spec_ref"], "spec_ref"),
        "source_manifest_hash": _hash(
            value["source_manifest_hash"], "source_manifest_hash"
        ),
        "broker": _text(value["broker"], "broker"),
        "broker_as_named": _text(value["broker_as_named"], "broker_as_named"),
        "broker_basis": _text(value["broker_basis"], "broker_basis"),
        "published_on": _iso_date(value["published_on"], "published_on"),
        "subject_as_named": _text(value["subject_as_named"], "subject_as_named"),
        "extraction_method": _text(value["extraction_method"], "extraction_method"),
    }
    if wire["spec_ref"] not in SELL_SIDE_SPEC_REFS:
        raise StreetEstimateValidationError(
            f"{wire['spec_ref']!r} is not a sell-side research document kind; "
            f"the kinds a street estimate may be read from are "
            f"{list(SELL_SIDE_SPEC_REFS)}"
        )
    if wire["broker"] not in dict(BROKERS):
        raise StreetEstimateValidationError(
            f"{wire['broker']!r} is not a known house; an unattributed target "
            "is not a street estimate"
        )
    if wire["broker_basis"] not in BROKER_BASES:
        raise StreetEstimateValidationError(
            f"broker_basis must be one of {list(BROKER_BASES)}"
        )
    if wire["extraction_method"] not in EXTRACTION_METHODS:
        raise StreetEstimateValidationError(
            f"extraction_method must be one of {list(EXTRACTION_METHODS)}"
        )
    analysts = value["analysts"]
    if not isinstance(analysts, Sequence) or isinstance(analysts, (str, bytes)):
        raise StreetEstimateValidationError("analysts must be an array")
    wire["analysts"] = [_text(item, "analysts[]") for item in analysts]

    rating = value["rating"]
    if rating is None:
        wire["rating"] = None
    else:
        rating = dict(rating) if isinstance(rating, Mapping) else None
        if rating is None or set(rating) != {"code", "as_named", "scale", "quote_id"}:
            raise StreetEstimateValidationError(
                "rating must be exactly code/as_named/scale/quote_id, or null"
            )
        if rating["code"] not in RATING_CODES:
            raise StreetEstimateValidationError(
                f"rating.code must be one of {list(RATING_CODES)}"
            )
        if rating["scale"] not in RATING_SCALES:
            raise StreetEstimateValidationError("rating.scale is not a known scale")
        if rating["scale"] not in rating_scales_for(wire["broker"]):
            raise StreetEstimateValidationError(
                f"{wire['broker']} does not run the {rating['scale']} scale"
            )
        wire["rating"] = {
            "code": rating["code"],
            "as_named": _text(rating["as_named"], "rating.as_named"),
            "scale": rating["scale"],
            "quote_id": _text(rating["quote_id"], "rating.quote_id"),
        }

    target = value["target_price"]
    if target is None:
        wire["target_price"] = None
    else:
        target = dict(target) if isinstance(target, Mapping) else None
        if target is None or set(target) != {"value", "currency", "horizon", "quote_id"}:
            raise StreetEstimateValidationError(
                "target_price must be exactly value/currency/horizon/quote_id, or null"
            )
        currency = _text(target["currency"], "target_price.currency").upper()
        if _CURRENCY_RE.fullmatch(currency) is None:
            raise StreetEstimateValidationError(
                "target_price.currency must be an ISO 4217 code; a target with "
                "no currency is a number, not a price"
            )
        amount = _decimal_string(target["value"], "target_price.value")
        if Decimal(amount) <= 0:
            raise StreetEstimateValidationError("target_price.value must be positive")
        wire["target_price"] = {
            "value": amount,
            "currency": currency,
            "horizon": _optional_text(target["horizon"], "target_price.horizon"),
            "quote_id": _text(target["quote_id"], "target_price.quote_id"),
        }

    figures = value["figures"]
    if not isinstance(figures, Sequence) or isinstance(figures, (str, bytes)):
        raise StreetEstimateValidationError("figures must be an array")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, figure in enumerate(figures):
        rows.append(_validate_figure(figure, f"figures[{index}]", seen))
    if not rows:
        raise StreetEstimateValidationError(
            "a street estimate carries at least one verified figure; a note "
            "this system could read no number out of is not a street estimate"
        )
    wire["figures"] = rows
    if wire["target_price"] is not None and not any(
        row["metric_ref"] == TARGET_METRIC for row in rows
    ):
        raise StreetEstimateValidationError(
            "the target price is not among the verified figures; every number "
            "stored here goes through the digit check"
        )
    return wire


def _validate_figure(
    figure: Any, name: str, seen: set[tuple[str, str]]
) -> dict[str, Any]:
    if not isinstance(figure, Mapping):
        raise StreetEstimateValidationError(f"{name} must be an object")
    required = {
        "quote_id", "metric_ref", "subject_as_named", "as_reported_label", "value",
        "unit", "currency", "period", "basis", "scale", "schema_version",
        "claim_kind", "citation_text", "citation_hash", "content_hash",
    }
    # Present only when the label leant on this grade's alias table -- "PT"
    # rather than "Price Target". Optional, so a figure whose label names a
    # line on its own is unchanged, and checked when present, so a figure
    # cannot claim a concession granted to some other grade.
    if figure.get("label_grade") is not None:
        if figure["label_grade"] != SOURCE_GRADE:
            raise StreetEstimateValidationError(
                f"{name}.label_grade must be {SOURCE_GRADE!r}"
            )
        required = required | {"label_grade"}
    if set(figure) != required:
        raise StreetEstimateValidationError(
            f"{name} is not the output of verify_numeric_candidate; "
            f"missing={sorted(required - set(figure))}, "
            f"unknown={sorted(set(figure) - required)}"
        )
    if figure["basis"] != BASIS:
        raise StreetEstimateValidationError(
            f"{name}.basis must be {BASIS!r}: every number here is the "
            "broker's own, never the company's"
        )
    if figure["metric_ref"] not in ESTIMATE_METRICS:
        raise StreetEstimateValidationError(
            f"{name}.metric_ref must be one of {list(ESTIMATE_METRICS)}"
        )
    body = {key: figure[key] for key in figure if key != "content_hash"}
    if content_hash(body) != figure["content_hash"]:
        raise StreetEstimateValidationError(
            f"{name} content hash does not describe its own body"
        )
    identity = (str(figure["metric_ref"]), str(figure["period"]))
    if identity in seen:
        raise StreetEstimateValidationError(
            f"{name} repeats {identity[0]} for {identity[1]}"
        )
    seen.add(identity)
    return dict(figure)


class StreetEstimateStore:
    """Append-only broker statements, one row per note per company."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("StreetEstimateStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def estimate(self, estimate_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM street_estimates WHERE estimate_id=?",
            (_text(estimate_id, "estimate_id"),),
        ).fetchone()
        if row is None:
            raise StreetEstimateConflict(f"StreetEstimate {estimate_id} was not found")
        return self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        wire = json.loads(row["record_json"])
        if canonical_json(wire) != row["record_json"]:
            raise StreetEstimateConflict("street estimate record_json is not canonical")
        body = {key: wire[key] for key in wire if key != "content_hash"}
        if content_hash(body) != wire["content_hash"] or (
            wire["content_hash"] != row["content_hash"]
        ):
            raise StreetEstimateConflict("street estimate content hash drifted")
        if wire["id"] != row["estimate_id"]:
            raise StreetEstimateConflict("street estimate identity column drifted")
        return wire

    def estimates(
        self,
        company_ref: str,
        *,
        since: str | None = None,
        broker: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Notes held for one company, newest first."""

        query = "SELECT * FROM street_estimates WHERE company_ref=?"
        params: list[Any] = [_text(company_ref, "company_ref")]
        if since is not None:
            query += " AND published_on>=?"
            params.append(_iso_date(since, "since"))
        if broker is not None:
            query += " AND broker=?"
            params.append(_text(broker, "broker"))
        query += " ORDER BY published_on DESC, estimate_id ASC LIMIT ?"
        params.append(int(limit))
        return [self._decode(row) for row in self.connection.execute(query, params)]

    def scanned_document_refs(self, company_ref: str) -> set[str]:
        """Notes already read for this company, whatever came of reading them."""

        rows = self.connection.execute(
            "SELECT document_ref FROM street_estimate_document_scans WHERE company_ref=?",
            (_text(company_ref, "company_ref"),),
        ).fetchall()
        return {row["document_ref"] for row in rows}

    def refused_by(self, extractor_ref: str) -> set[str]:
        """Notes an older reader refused, so a fixed one can be given them again.

        The scan ledger exists so a note is never read twice, which would make
        every fix to the extractor unreachable if the ledger could not say
        *which* reader refused. This is the reverse query: everything refused by
        something other than the reader asking.
        """

        rows = self.connection.execute(
            "SELECT company_ref,document_ref FROM street_estimate_document_scans "
            "WHERE outcome='refused' AND extractor_ref<>?",
            (_text(extractor_ref, "extractor_ref"),),
        ).fetchall()
        return {row["document_ref"] for row in rows}

    def scans(self, company_ref: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM street_estimate_document_scans WHERE company_ref=? "
            "ORDER BY scanned_at, document_ref LIMIT ?",
            (_text(company_ref, "company_ref"), int(limit)),
        )]

    # -- writing -----------------------------------------------------------

    def record(
        self, estimate: Mapping[str, Any], *, observed_by: str = OBSERVER_REF
    ) -> dict[str, Any]:
        """Store one note's statement, or say this note is already held."""

        wire = validate_estimate(estimate)
        observed_by = _text(observed_by, "observed_by")
        estimate_id = _ref("street-estimate", {
            "company_ref": wire["company_ref"],
            "document_ref": wire["document_ref"],
        })
        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": estimate_id,
            "created_at": created_at,
            **wire,
            "source_grade": SOURCE_GRADE,
            "source_basis": basis_for(SOURCE_GRADE),
            # The sentence a person reads. It carries the qualifier so that a
            # reader who sees one row, out of context, is told in the row that
            # this is a broker's estimate.
            "statement": qualified_statement(_statement(wire)),
            "consensus_eligible": True,
            "observed_by": observed_by,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM street_estimates WHERE estimate_id=?", (estimate_id,)
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", **self._decode(existing)}
            target = wire["target_price"] or {}
            rating = wire["rating"] or {}
            cur.execute(
                "INSERT INTO street_estimates("
                "estimate_id,company_ref,document_ref,spec_ref,source_manifest_hash,"
                "broker,broker_as_named,broker_basis,analysts_json,published_on,"
                "rating,rating_as_named,rating_scale,target_value,target_currency,"
                "target_horizon,source_grade,extraction_method,record_json,"
                "content_hash,observed_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    estimate_id, wire["company_ref"], wire["document_ref"],
                    wire["spec_ref"], wire["source_manifest_hash"], wire["broker"],
                    wire["broker_as_named"], wire["broker_basis"],
                    canonical_json(wire["analysts"]), wire["published_on"],
                    rating.get("code"), rating.get("as_named"), rating.get("scale"),
                    target.get("value"), target.get("currency"), target.get("horizon"),
                    SOURCE_GRADE, wire["extraction_method"], canonical_json(record),
                    record["content_hash"], observed_by, created_at,
                ),
            )
            stored = self._decode(cur.execute(
                "SELECT * FROM street_estimates WHERE estimate_id=?", (estimate_id,)
            ).fetchone())
        if stored != record:
            raise StreetEstimateConflict("stored street estimate does not read back")
        return {"status": "fresh", **stored}

    def record_scan(
        self,
        *,
        company_ref: str,
        document_ref: str,
        outcome: str,
        extraction_method: str,
        extractor_ref: str,
        reason: str | None = None,
        estimate_id: str | None = None,
    ) -> dict[str, Any]:
        """Note that this document has been read, and what came of it."""

        company_ref = _text(company_ref, "company_ref")
        document_ref = _text(document_ref, "document_ref")
        if outcome not in SCAN_OUTCOMES:
            raise StreetEstimateValidationError(
                f"outcome must be one of {list(SCAN_OUTCOMES)}"
            )
        if extraction_method not in EXTRACTION_METHODS:
            raise StreetEstimateValidationError(
                f"extraction_method must be one of {list(EXTRACTION_METHODS)}"
            )
        extractor_ref = _text(extractor_ref, "extractor_ref")
        if outcome == "refused" and not reason:
            raise StreetEstimateValidationError(
                "a refused scan records why; a figure that silently never "
                "appears cannot be told from one nobody looked for"
            )
        scan_id = _ref("street-estimate-scan", {
            "company_ref": company_ref, "document_ref": document_ref,
        })
        now = _now()
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM street_estimate_document_scans WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "status": "duplicate"}
            cur.execute(
                "INSERT INTO street_estimate_document_scans("
                "scan_id,company_ref,document_ref,outcome,reason,estimate_id,"
                "extraction_method,extractor_ref,scanned_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (scan_id, company_ref, document_ref, outcome,
                 _optional_text(reason, "reason"),
                 _optional_text(estimate_id, "estimate_id"), extraction_method,
                 extractor_ref, now),
            )
        return {
            "status": "fresh", "scan_id": scan_id, "company_ref": company_ref,
            "document_ref": document_ref, "outcome": outcome, "reason": reason,
            "estimate_id": estimate_id, "extraction_method": extraction_method,
            "extractor_ref": extractor_ref, "scanned_at": now,
        }


def _statement(wire: Mapping[str, Any]) -> str:
    parts = [f"{wire['broker_as_named']} rates {wire['subject_as_named']}"]
    if wire["rating"] is not None:
        parts.append(f"{wire['rating']['as_named']}")
    if wire["target_price"] is not None:
        target = wire["target_price"]
        horizon = f" ({target['horizon']})" if target["horizon"] else ""
        parts.append(
            f"with a price target of {target['value']} {target['currency']}{horizon}"
        )
    parts.append(f"as of {wire['published_on']}")
    return " ".join(parts)


# -- the cross-verification rule --------------------------------------------


def report_consensus(
    estimates: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    window_days: int | None = None,
    metric: str = TARGET_METRIC,
    period: str | None = None,
) -> dict[str, Any] | None:
    """A range from the reports, or None when the reports do not make one.

    The rule is ``metric_discovery``'s, moved one field across: corroboration
    is counted in distinct *houses*, never in notes. Two TD Cowen notes on DXC
    published the same day carrying the same $11.00 target are one house saying
    one thing once; counting them twice would manufacture a consensus out of a
    single opinion, and DXC is exactly the company where that would have
    happened.

    Returns None -- not an error -- when fewer than two houses qualify. A
    company the street has one published view on has one published view, and
    the honest record of that is the single ``StreetEstimate`` that already
    exists.
    """

    as_of = _iso_date(as_of, "as_of")
    window = int(CONSENSUS_POLICY["window_days"] if window_days is None else window_days)
    if window <= 0:
        raise StreetEstimateValidationError("window_days must be positive")
    horizon = (date.fromisoformat(as_of) - _timedelta_days(window)).isoformat()
    newest: dict[str, Mapping[str, Any]] = {}
    for item in estimates:
        if not isinstance(item, Mapping):
            raise StreetEstimateValidationError("each estimate must be an object")
        published = str(item.get("published_on") or "")
        if not published or published < horizon or published > as_of:
            continue
        target = item.get("target_price")
        if metric != TARGET_METRIC or not isinstance(target, Mapping):
            continue
        if period is not None and target.get("horizon") != period:
            continue
        broker = str(item.get("broker") or "")
        if not broker:
            continue
        held = newest.get(broker)
        # Strict, so a house that published twice on one day keeps the first
        # note the caller listed rather than the last. Either is defensible;
        # what matters is that it is the same one every time, because the rows
        # this range is reported alongside are chosen by the same rule.
        if held is None or str(held["published_on"]) < published:
            newest[broker] = item
    if len(newest) < int(CONSENSUS_POLICY["min_independent_brokers"]):
        return None
    currencies = {
        str(item["target_price"]["currency"]) for item in newest.values()
    }
    if len(currencies) != 1:
        # A range that averages a dollar target with a euro one is not a range.
        return None
    values = [Decimal(str(item["target_price"]["value"])) for item in newest.values()]
    # Four decimal places and no trailing zeros. ``Decimal.normalize`` alone
    # would turn 214 into 2.14E+2, which is a price nobody prints.
    mean = (sum(values) / len(values)).quantize(Decimal("0.0001"))
    mean_text = f"{mean:f}".rstrip("0").rstrip(".") or "0"
    return {
        "metric": metric,
        "period": period or "current",
        "as_of": as_of,
        "window_days": window,
        "broker_count": len(newest),
        "brokers": sorted(newest),
        "low": str(min(values)),
        "high": str(max(values)),
        "mean": mean_text,
        "currency": next(iter(currencies)),
        "estimate_refs": sorted(str(item["id"]) for item in newest.values()),
        "document_refs": sorted(str(item["document_ref"]) for item in newest.values()),
        "policy_ref": CONSENSUS_POLICY_REF,
        "policy_hash": CONSENSUS_POLICY_HASH,
    }


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


__all__ = [
    "BASIS",
    "BROKERS",
    "BROKER_BASES",
    "BROKER_SCALES",
    "CONSENSUS_POLICY",
    "CONSENSUS_POLICY_HASH",
    "CONSENSUS_POLICY_REF",
    "EPS_METRIC",
    "ESTIMATE_METRICS",
    "EXTRACTION_METHODS",
    "NUMERIC_BASIS",
    "OBSERVER_REF",
    "RATING_CODES",
    "RATING_SCALES",
    "REVENUE_METRIC",
    "SCAN_OUTCOMES",
    "SCHEMA_VERSION",
    "SELL_SIDE_DOCUMENT_CODES",
    "SELL_SIDE_SPEC_REFS",
    "SOURCE_GRADE",
    "TARGET_METRIC",
    "StreetEstimateConflict",
    "StreetEstimateError",
    "StreetEstimateStore",
    "StreetEstimateValidationError",
    "broker_slug",
    "normalise_rating",
    "qualified_statement",
    "rating_scales_for",
    "report_consensus",
    "validate_estimate",
]
