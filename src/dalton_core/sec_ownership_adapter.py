"""S5: read a Form 4, a 13D/G, a 144 or a 13F, and lose nothing on the way.

These four documents are XML that SEC's own forms produce, so unlike a 10-K
they can be read structurally rather than guessed at.  What makes them awkward
is not the parsing.  It is that every number on them is a number somebody will
later want to check, and three of the easy ways to hold a number would make
that impossible:

- **A float loses the filing.**  A Form 4 reporting ``1200.0000`` shares
  reported four decimal places because the plan it came from has fractional
  units; ``1200.0`` is a different statement.  So every figure here stays the
  text the filing contained, validated against a shape and never converted.
- **A 13F value has no fixed unit.**  Before the 2023 amendments SEC's own
  instruction was thousands of dollars; after, whole dollars; and filers were
  inconsistent across the boundary.  A parser that picked one is a parser that
  is wrong by a factor of a thousand for half the corpus.  So the unit is
  decided by the period *and* checked against the value-to-share ratio, and
  which of the two decided is recorded on the wire.
- **A row with no provenance is a rumour.**  Every row carries a
  ``record_hash`` over the row, the accession it was filed under and the
  SHA-256 of the exact bytes it was parsed from.  Two runs of the same filing
  produce the same hashes; a row that cannot be traced back to bytes cannot
  exist here at all.

**What this module refuses to do.**  It does not fetch.  It is handed text and
returns a wire; the child process owns the network and the spool.  It does not
interpret: no "this looks like a 10b5-1 sale", no netting of a director's
buys against their sells.  Those are judgements, the judgement layer's to make
from the events these produce.

The parsing rules follow ``company-filings-alert``'s ``edgar_enrich.py`` --
the only place in the OpenClaw workspace that reads 13D/G and 144 primary
documents -- and ``13f-tracker``'s value-unit heuristic, with the tag names
kept and the guesses removed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from .store import content_hash

SCHEMA_VERSION = "0.1"
PROVIDER_STATUS = 200

# A primary document is tens of kilobytes; an information table for a large
# manager is a few megabytes. Beyond this is not a filing.
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_TRANSACTIONS = 200
MAX_REPORTING_PERSONS = 40
MAX_NOTICES = 50
# A large manager's book. BlackRock files on the order of seven thousand
# lines; twelve thousand is the largest filing anyone has and a bound on what
# a malformed document can cost.
#
# Beyond it the read is *refused*, with the count and the ceiling in the
# reason, and the raw bytes stay in the spool. Truncating instead was the
# tempting repair and is the one thing that must not happen here: a book
# truncated at twelve thousand rows, compared against a prior quarter
# truncated at a different boundary, fabricates ``new`` and ``exit`` events
# for positions that never moved. A refusal an operator reads is recoverable;
# an invented divestment is not.
MAX_HOLDINGS = 12_000
MAX_FOOTNOTES = 40
MAX_TEXT_CHARS = 512

_NUMBER_RE = re.compile(r"^-?(0|[1-9][0-9]*)([.][0-9]+)?$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)

# The §16 transaction codes, so a reader of an event does not have to keep the
# table in their head. Not a filter: an unknown code is recorded as itself,
# because SEC adds codes and dropping a filing over one would be losing the
# fact to protect the vocabulary.
TRANSACTION_CODE_MEANINGS: Mapping[str, str] = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant or award",
    "D": "disposition to the issuer",
    "F": "shares withheld for tax",
    "M": "exercise or conversion of a derivative",
    "C": "conversion of a derivative",
    "E": "expiration of a short derivative position",
    "H": "expiration of a long derivative position",
    "G": "bona fide gift",
    "V": "transaction voluntarily reported early",
    "J": "other acquisition or disposition",
    "K": "equity swap or similar",
    "L": "small acquisition",
    "U": "disposition in a tender of shares",
    "W": "acquisition or disposition by will or the laws of descent",
    "X": "exercise of an in-the-money or at-the-money derivative",
    "I": "discretionary transaction",
}
# The 2022 amendments to Form 13F. Filings whose reporting period ends on or
# after this day report value in whole dollars; before it, in thousands.
THOUSANDS_RULE_LAST_DAY = "2022-12-31"


class SecOwnershipParseError(ValueError):
    """A document is not the shape the frozen contract can describe."""


# -- reading XML ------------------------------------------------------------


def _root(text: str, *, what: str) -> ElementTree.Element:
    """The document element, with entity declarations refused outright.

    ``ElementTree`` expands internal entities, so a filing carrying a nested
    entity definition is a memory exhaustion waiting for a parser to be polite
    to it. Nothing SEC serves for these forms has a DOCTYPE, so one is a
    refusal rather than something to sanitise around.
    """

    if not isinstance(text, str) or not text.strip():
        raise SecOwnershipParseError(f"{what} is empty")
    if len(text.encode("utf-8", "ignore")) > MAX_DOCUMENT_BYTES:
        raise SecOwnershipParseError(f"{what} exceeds the document ceiling")
    if _DOCTYPE_RE.search(text):
        raise SecOwnershipParseError(
            f"{what} carries a DOCTYPE; entity declarations are refused"
        )
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise SecOwnershipParseError(f"{what} is not well-formed XML: {exc}") from exc


def _tag(element: ElementTree.Element) -> str:
    """The local name, because SEC namespaces some of these forms and not others."""

    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else str(tag)


def _find(element: ElementTree.Element | None, *path: str) -> ElementTree.Element | None:
    """Walk by local name, so a namespace prefix cannot break a lookup."""

    node = element
    for name in path:
        if node is None:
            return None
        node = next((child for child in node if _tag(child) == name), None)
    return node


def _findall(element: ElementTree.Element | None, name: str) -> list[ElementTree.Element]:
    if element is None:
        return []
    return [child for child in element if _tag(child) == name]


def _descendants(element: ElementTree.Element | None, name: str) -> list[ElementTree.Element]:
    if element is None:
        return []
    return [node for node in element.iter() if _tag(node) == name]


def _text(element: ElementTree.Element | None, *path: str) -> str | None:
    """One element's text, collapsed, bounded, or None.

    ``<value>`` is unwrapped here because half of the ownership schema wraps a
    scalar in one and half does not, and a caller that had to know which would
    get it wrong in exactly the fields nobody tests.
    """

    node = _find(element, *path) if path else element
    if node is None:
        return None
    inner = _find(node, "value")
    if inner is not None:
        node = inner
    raw = "".join(node.itertext())
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if not collapsed:
        return None
    return collapsed[:MAX_TEXT_CHARS]


def _number(element: ElementTree.Element | None, *path: str) -> str | None:
    """A filed figure, as filed.

    Thousands separators, a currency sign and surrounding space are removed
    because they are typography rather than value; anything else that does not
    reduce to a plain decimal is a refusal, not a zero. A ``144`` reporting
    "approximately 5,000" is a filing this contract cannot describe, and
    quietly recording 5000 would be inventing a precision the filer declined.
    """

    raw = _text(element, *path)
    if raw is None:
        return None
    cleaned = raw.replace(",", "").replace("$", "").replace("+", "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("."):
        cleaned = "0" + cleaned
    if cleaned.startswith("-."):
        cleaned = "-0" + cleaned[1:]
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    # Leading zeros are how a form pads a field, not a different number.
    sign, body = ("-", cleaned[1:]) if cleaned.startswith("-") else ("", cleaned)
    if "." in body:
        whole, fraction = body.split(".", 1)
        whole = whole.lstrip("0") or "0"
        body = f"{whole}.{fraction}"
    else:
        body = body.lstrip("0") or "0"
    cleaned = sign + body
    if not _NUMBER_RE.match(cleaned):
        raise SecOwnershipParseError(
            f"{raw!r} is not a figure this contract can describe"
        )
    return cleaned


def _date(element: ElementTree.Element | None, *path: str) -> str | None:
    raw = _text(element, *path)
    if raw is None:
        return None
    candidate = raw[:10]
    if _DATE_RE.match(candidate):
        return candidate
    # SEC serves a handful of these as MM-DD-YYYY on older forms. Reordered
    # rather than refused, and only when it is unambiguous.
    match = re.match(r"^([0-9]{2})-([0-9]{2})-([0-9]{4})$", raw)
    if match:
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"
    raise SecOwnershipParseError(f"{raw!r} is not a date this contract can describe")


def _boolean(element: ElementTree.Element | None, *path: str) -> bool | None:
    raw = _text(element, *path)
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    return None


def _dfind(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    """The first element with this local name anywhere below (or at) here.

    The ownership forms nest their cover-page scalars differently in almost
    every schema version -- ``amendmentNo`` sits under ``formData`` in one and
    under ``formData/coverPageHeader`` in the next -- and a reader that walked
    a fixed path would return ``None`` for a field that is plainly there. The
    names are specific enough that the first match is the right one.
    """

    if element is None:
        return None
    if _tag(element) == name:
        return element
    return next((node for node in element.iter() if _tag(node) == name), None)


def _dtext(element: ElementTree.Element | None, name: str) -> str | None:
    return _text(_dfind(element, name))


def _dnumber(element: ElementTree.Element | None, name: str) -> str | None:
    return _number(_dfind(element, name))


def _ddate(element: ElementTree.Element | None, name: str) -> str | None:
    return _date(_dfind(element, name))


def _accession(value: Any) -> str:
    text = str(value or "").strip()
    if not _ACCESSION_RE.match(text):
        raise SecOwnershipParseError(f"{value!r} is not an accession number")
    return text


def _sha256(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", text):
        raise SecOwnershipParseError(f"{name} must be a SHA-256 hex digest")
    return text


def _refs(source_record_refs: Sequence[str] | None) -> list[str]:
    return [str(ref) for ref in (source_record_refs or [])]


def _bind(row: Mapping[str, Any], *, accession: str, artifact_hash: str) -> str:
    """One row's name: this row, in this filing, in these exact bytes.

    The accession alone would not do it -- a filing can be re-served with a
    corrected document under the same accession -- and the artifact hash alone
    would not say which filing the bytes were.
    """

    return content_hash({
        "accession": accession,
        "artifact_hash": artifact_hash,
        "row": {key: value for key, value in row.items() if key != "record_hash"},
    })


def _finish(wire: dict[str, Any]) -> dict[str, Any]:
    wire["content_hash"] = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    return wire


# -- Form 3 / 4 / 5 ---------------------------------------------------------


def _reporting_owner(node: ElementTree.Element) -> dict[str, Any]:
    identity = _find(node, "reportingOwnerId")
    relationship = _find(node, "reportingOwnerRelationship")
    name = _text(identity, "rptOwnerName")
    if not name:
        raise SecOwnershipParseError("a Form 4 reporting owner has no name")
    is_director = bool(_boolean(relationship, "isDirector"))
    is_officer = bool(_boolean(relationship, "isOfficer"))
    is_ten_percent = bool(_boolean(relationship, "isTenPercentOwner"))
    is_other = bool(_boolean(relationship, "isOther"))
    officer_title = _text(relationship, "officerTitle")
    roles: list[str] = []
    if is_director:
        roles.append("director")
    if is_officer:
        roles.append(f"officer:{officer_title}" if officer_title else "officer")
    if is_ten_percent:
        roles.append("ten_percent_owner")
    if is_other:
        roles.append(_text(relationship, "otherText") or "other")
    return {
        "owner_cik": _text(identity, "rptOwnerCik"),
        "owner_name": name,
        "is_director": is_director,
        "is_officer": is_officer,
        "is_ten_percent_owner": is_ten_percent,
        "is_other": is_other,
        "officer_title": officer_title,
        # A person is often two of these at once, so the joined word is a
        # convenience for a prompt and the four booleans stay the truth.
        "role": ", ".join(roles) if roles else "unspecified",
    }


def _form4_transaction(
    node: ElementTree.Element, *, table: str, holding: bool
) -> dict[str, Any]:
    amounts = _find(node, "transactionAmounts")
    coding = _find(node, "transactionCoding")
    post = _find(node, "postTransactionAmounts")
    nature = _find(node, "ownershipNature")
    underlying = _find(node, "underlyingSecurity")
    row = {
        "table": table,
        "security_title": _text(node, "securityTitle") or "",
        "transaction_date": _date(node, "transactionDate"),
        "deemed_execution_date": _date(node, "deemedExecutionDate"),
        "transaction_code": _text(coding, "transactionCode"),
        "transaction_form_type": _text(coding, "transactionFormType"),
        "equity_swap_involved": _boolean(coding, "equitySwapInvolved"),
        "shares": _number(amounts, "transactionShares"),
        "price_per_share": _number(amounts, "transactionPricePerShare"),
        "acquired_disposed": _text(amounts, "transactionAcquiredDisposedCode"),
        "shares_owned_following": (
            _number(post, "sharesOwnedFollowingTransaction")
            or _number(post, "valueOwnedFollowingTransaction")
        ),
        "direct_or_indirect": _text(nature, "directOrIndirectOwnership"),
        "nature_of_ownership": _text(nature, "natureOfOwnership"),
        "underlying_security_title": _text(underlying, "underlyingSecurityTitle"),
        "underlying_shares": _number(underlying, "underlyingSecurityShares"),
        "conversion_or_exercise_price": _number(node, "conversionOrExercisePrice"),
        "exercise_date": _date(node, "exerciseDate"),
        "expiration_date": _date(node, "expirationDate"),
        "footnote_refs": sorted({
            str(item.get("id"))
            for item in _descendants(node, "footnoteId")
            if item.get("id")
        }),
    }
    code = row["transaction_code"]
    if code is not None and not re.match(r"^[A-Z]$", code):
        raise SecOwnershipParseError(f"{code!r} is not a §16 transaction code")
    if row["acquired_disposed"] is not None and row["acquired_disposed"] not in {"A", "D"}:
        raise SecOwnershipParseError(
            f"{row['acquired_disposed']!r} is not an acquired/disposed code"
        )
    if row["direct_or_indirect"] is not None and row["direct_or_indirect"] not in {"D", "I"}:
        row["direct_or_indirect"] = None
    if holding:
        # A holding row on a Form 3 or a "no transactions" Form 4 reports a
        # position and no trade. Saying so with nulls is honest; inventing a
        # transaction code for it would put a trade in the ledger that nobody
        # made.
        row["transaction_code"] = row["transaction_code"] or None
    return row


def parse_form4(
    text: str,
    *,
    accession: str,
    artifact_hash: str,
    source_record_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One ``ownershipDocument`` -- who, what role, which trades, at what price."""

    accession = _accession(accession)
    artifact_hash = _sha256(artifact_hash, "artifact_hash")
    root = _root(text, what="Form 4 primary document")
    if _tag(root) != "ownershipDocument":
        raise SecOwnershipParseError(
            f"expected an ownershipDocument, found {_tag(root)!r}"
        )
    document_type = _text(root, "documentType") or ""
    base = document_type.split("/")[0].strip()
    if base not in {"3", "4", "5"}:
        raise SecOwnershipParseError(
            f"{document_type!r} is not a Form 3, 4 or 5 document type"
        )
    issuer = _find(root, "issuer")
    owners = [
        _reporting_owner(node) for node in _findall(root, "reportingOwner")
    ][:MAX_REPORTING_PERSONS]
    if not owners:
        raise SecOwnershipParseError("a Form 4 with no reporting owner is not a filing")

    transactions: list[dict[str, Any]] = []
    for table_tag, table in (
        ("nonDerivativeTable", "non_derivative"),
        ("derivativeTable", "derivative"),
    ):
        table_node = _find(root, table_tag)
        for name, holding in (
            ("nonDerivativeTransaction", False), ("derivativeTransaction", False),
            ("nonDerivativeHolding", True), ("derivativeHolding", True),
        ):
            for node in _findall(table_node, name):
                transactions.append(
                    _form4_transaction(node, table=table, holding=holding)
                )
    if len(transactions) > MAX_TRANSACTIONS:
        raise SecOwnershipParseError(
            f"{len(transactions)} rows exceeds the frozen ceiling of {MAX_TRANSACTIONS}"
        )
    for row in transactions:
        row["record_hash"] = _bind(row, accession=accession, artifact_hash=artifact_hash)

    footnotes = [
        note for note in (
            _text(node) for node in _descendants(_find(root, "footnotes"), "footnote")
        ) if note
    ][:MAX_FOOTNOTES]
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "accession": accession,
        "document_type": base,
        "is_amendment": document_type.strip().upper().endswith("/A"),
        "period_of_report": _date(root, "periodOfReport"),
        "date_of_original_submission": _date(root, "dateOfOriginalSubmission"),
        "issuer_cik": _text(issuer, "issuerCik"),
        "issuer_name": _text(issuer, "issuerName"),
        "issuer_trading_symbol": _text(issuer, "issuerTradingSymbol"),
        "reporting_owners": owners,
        "transactions": transactions,
        # A Form 3, or a Form 4 that reports only positions. Named on the wire
        # so a reader is not left to infer it from an empty transaction list,
        # which is also what a broken parse looks like.
        "holdings_only": all(row["transaction_code"] is None for row in transactions)
        if transactions else True,
        "footnotes": footnotes,
        "source_record_refs": _refs(source_record_refs),
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


# -- SC 13D / SC 13G --------------------------------------------------------


_PURPOSE_ITEM_RE = re.compile(
    r"item\s*4[.:\s].*?(?=item\s*5[.:\s])", re.IGNORECASE | re.DOTALL
)


def purpose_text_digest(text: str | None) -> tuple[str | None, int | None]:
    """The hash of Item 4, and how long it was.

    Item 4 is where a 13D says whether the stake is passive or whether the
    filer intends to talk to the board, and it is prose -- sometimes pages of
    it. Hashing it makes "the purpose changed in this amendment" answerable
    without this connector becoming a second copy of the filing, which is the
    thing a connector that stores other people's documents eventually is.
    """

    if not isinstance(text, str) or not text.strip():
        return None, None
    collapsed = re.sub(r"\s+", " ", text).strip()
    return content_hash({"purpose_text": collapsed}), len(collapsed)


def parse_beneficial_ownership(
    text: str,
    *,
    accession: str,
    artifact_hash: str,
    form_type: str,
    purpose_text: str | None = None,
    source_record_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A 13D or 13G primary document -- who holds how much, and which amendment.

    The tag names are ``edgar_enrich.py``'s: ``reportingPersonName`` with
    ``filingPersonName`` as the fallback shape older filings use,
    ``classPercent``, ``amendmentNo``,
    ``reportingPersonBeneficiallyOwnedAggregateNumberOfShares``,
    ``eventDateRequiresFilingThisStatement``.

    ``form_type`` is supplied by the caller because it comes from the filings
    index, not from the document: the primary document of a 13G amendment does
    not always say it is one, and the index always does.
    """

    accession = _accession(accession)
    artifact_hash = _sha256(artifact_hash, "artifact_hash")
    declared = str(form_type or "").strip().upper()
    is_amendment = declared.endswith("/A")
    base = declared[:-2] if is_amendment else declared
    if base not in {"SC 13D", "SC 13G"}:
        raise SecOwnershipParseError(f"{form_type!r} is not a 13D or 13G form type")
    root = _root(text, what="13D/G primary document")

    people: list[dict[str, Any]] = []
    # The modern schema nests one block per filer; the older one is flat and
    # repeats the person tags at the top level. Both are read, and the nested
    # shape wins when both are present rather than being merged into a filing
    # that reports each person twice.
    blocks = _descendants(root, "reportingPerson") or _descendants(root, "filingPerson")
    if blocks:
        for node in blocks:
            name = (
                _dtext(node, "reportingPersonName")
                or _dtext(node, "filingPersonName")
                or _dtext(node, "name")
            )
            if not name:
                continue
            people.append(_ownership_person(node, name))
    if not people:
        names = [
            value for value in (
                _text(node) for node in _descendants(root, "reportingPersonName")
            ) if value
        ] or [
            value for value in (
                _text(node) for node in _descendants(root, "filingPersonName")
            ) if value
        ]
        seen: list[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        for name in seen:
            people.append(
                _ownership_person(root if len(seen) == 1 else None, name)
            )
    if not people:
        raise SecOwnershipParseError(
            "a 13D/G with no reporting person is not a filing this can describe"
        )
    people = people[:MAX_REPORTING_PERSONS]
    for person in people:
        person["record_hash"] = _bind(
            person, accession=accession, artifact_hash=artifact_hash
        )

    amendment_no = _dtext(root, "amendmentNo") or _dtext(root, "amendmentNumber")
    if amendment_no is not None:
        digits = amendment_no.strip().lstrip("0") or "0"
        amendment_no = digits if re.match(r"^[0-9]{1,4}$", digits) else None
    cusip = _dtext(root, "cusipNumber") or _dtext(root, "cusip")
    if cusip is not None:
        squashed = re.sub(r"[^0-9A-Za-z]", "", cusip).upper()
        cusip = squashed if _CUSIP_RE.match(squashed) else None
    digest, chars = purpose_text_digest(
        purpose_text if purpose_text is not None
        else _dtext(root, "purposeOfTransaction")
    )
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "accession": accession,
        "form_type": base,
        "is_amendment": is_amendment,
        # An amendment that does not number itself is not "amendment zero" --
        # it is an amendment whose number the filing did not carry, and the
        # difference is the whole of "which amendment is this".
        "amendment_no": amendment_no,
        "subject_company_cik": _dtext(root, "issuerCik") or _dtext(root, "cik"),
        "subject_company_name": _dtext(root, "issuerName")
        or _dtext(root, "nameOfIssuer"),
        "security_class_title": _dtext(root, "securitiesClassTitle")
        or _dtext(root, "titleOfClass"),
        "cusip": cusip,
        "event_date": _ddate(root, "eventDateRequiresFilingThisStatement"),
        "date_of_signature": _ddate(root, "signatureDate"),
        "reporting_persons": people,
        "purpose_text_hash": digest,
        "purpose_text_chars": chars,
        "source_record_refs": _refs(source_record_refs),
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


def _ownership_person(
    node: ElementTree.Element | None, name: str
) -> dict[str, Any]:
    """One filer's cover-page row.

    ``node`` is the filer's own block where the schema has one, and the whole
    document where it does not. When it is the whole document *and* there is
    more than one filer, the caller passes ``None`` so that the numbers stay
    absent: the flat shape has one set of figures and attributing them to
    three people would report the stake three times.
    """

    return {
        "reporting_person_name": name,
        "reporting_person_cik": _dtext(node, "reportingPersonCik")
        or _dtext(node, "filingPersonCik"),
        "citizenship": _dtext(node, "citizenshipOrPlaceOfOrganization"),
        "person_type": _dtext(node, "typeOfReportingPerson"),
        "sole_voting_power": _dnumber(node, "soleVotingPower"),
        "shared_voting_power": _dnumber(node, "sharedVotingPower"),
        "sole_dispositive_power": _dnumber(node, "soleDispositivePower"),
        "shared_dispositive_power": _dnumber(node, "sharedDispositivePower"),
        "aggregate_shares": _dnumber(
            node, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares"
        ) or _dnumber(node, "aggregateAmountBeneficiallyOwned"),
        "percent_of_class": _dnumber(node, "classPercent")
        or _dnumber(node, "percentOfClass"),
    }


# -- Form 144 ---------------------------------------------------------------


def parse_form144(
    text: str,
    *,
    accession: str,
    artifact_hash: str,
    filing_date: str | None = None,
    source_record_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A notice of proposed sale: who, how many, worth what, and when.

    A 144 is an *intention*, not a trade. It says an affiliate has told the
    SEC they may sell; the sale may be smaller, later or never. Nothing here
    calls it a sale, and the event it produces says ``planned``.
    """

    accession = _accession(accession)
    artifact_hash = _sha256(artifact_hash, "artifact_hash")
    root = _root(text, what="Form 144 primary document")
    # ``is not None`` rather than truthiness: an Element with no children is
    # falsy, so ``_dfind(...) or root`` silently discards an empty
    # ``<issuerInfo/>`` -- and ElementTree warns that the truth test is going
    # away entirely.
    found_issuer = _dfind(root, "issuerInfo")
    issuer = root if found_issuer is None else found_issuer

    notices: list[dict[str, Any]] = []
    blocks = _descendants(root, "securitiesToBeSold") or _descendants(
        root, "securitiesInformation"
    )
    seller_fallback = _dtext(
        root, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"
    )
    for node in blocks or [root]:
        seller = (
            _dtext(node, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
            or _dtext(node, "sellerName")
            or seller_fallback
        )
        if not seller:
            continue
        found_acquisition = _dfind(node, "acquiredInfo")
        acquisition = node if found_acquisition is None else found_acquisition
        row = {
            "seller_name": seller,
            "relationship_to_issuer": _dtext(node, "relationshipToIssuer")
            or _dtext(root, "relationshipToIssuer"),
            "security_class_title": _dtext(node, "securityClassTitle")
            or _dtext(root, "securityClassTitle"),
            "shares_to_be_sold": _dnumber(node, "noOfUnitsSold")
            or _dnumber(node, "numberOfUnitsSold"),
            "aggregate_market_value": _dnumber(node, "aggregateMarketValue"),
            "shares_outstanding": _dnumber(node, "noOfUnitsOutstanding"),
            "approx_sale_date": _ddate(node, "approxSaleDate"),
            "exchange_name": _dtext(node, "nameOfExchange")
            or _dtext(root, "securitiesExchangeName"),
            "broker_name": _dtext(node, "brokerOrMarketmakerDetails")
            or _dtext(root, "nameOfBroker"),
            "acquisition_date": _ddate(acquisition, "dateAcquired"),
            "nature_of_acquisition": _dtext(
                acquisition, "natureOfAcquisitionTransaction"
            ),
            "payment_date": _ddate(acquisition, "paymentDate"),
        }
        notices.append(row)
    if not notices:
        raise SecOwnershipParseError(
            "a Form 144 with no named seller is not a filing this can describe"
        )
    notices = notices[:MAX_NOTICES]
    for row in notices:
        row["record_hash"] = _bind(row, accession=accession, artifact_hash=artifact_hash)
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "accession": accession,
        "issuer_cik": _dtext(issuer, "issuerCik") or _dtext(root, "issuerCik"),
        "issuer_name": _dtext(issuer, "issuerName") or _dtext(root, "issuerName"),
        "filing_date": _ddate(root, "filingDate") if filing_date is None else (
            filing_date if _DATE_RE.match(str(filing_date)) else None
        ),
        "notices": notices,
        # Whether the filer also reported sales they already made. It matters
        # because a 144 that follows three others is a programme, not a one-off,
        # and the event says which.
        "securities_sold_past_3_months": bool(
            _descendants(
                _dfind(root, "securitiesSoldInPast3Months"), "securitiesSoldDetails"
            )
        ),
        "source_record_refs": _refs(source_record_refs),
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


# -- Form 13F ---------------------------------------------------------------


def quarter_of(period_end: str | None) -> str | None:
    """``2026-06-30`` -> ``2026Q2``. A 13F period always ends a quarter."""

    if not isinstance(period_end, str) or not _DATE_RE.match(period_end):
        return None
    year, month = int(period_end[:4]), int(period_end[5:7])
    return f"{year}Q{(month - 1) // 3 + 1}"


# Below this, a book's median value-to-share ratio can only be thousands: it
# would otherwise be a book whose median holding trades under five cents.
# Above it, only whole dollars: a thousands-reported book would have to have a
# median holding over twenty thousand dollars a share.  Between the two the
# ratio says nothing -- a dollar stock in whole dollars and a thousand-dollar
# stock in thousands land in the same place -- and pretending otherwise is how
# a heuristic becomes a factor-of-a-thousand error.
DECISIVE_THOUSANDS_BELOW = Decimal("0.05")
DECISIVE_USD_ABOVE = Decimal("20")
# ``PRN`` rows report a principal amount, not a share count, so their
# value-to-amount ratio is cents on the dollar of face value and is near 1 for
# every bond in every filing.  Mixed into the median they drag a book of
# ordinary equities towards the ambiguous band for no reason at all.
SHARE_AMOUNT_TYPE = "SH"


def _median_value_per_share(holdings: Sequence[Mapping[str, Any]]) -> Decimal | None:
    """Median value-to-share ratio over the *share* rows, or None."""

    ratios: list[Decimal] = []
    for row in holdings:
        if row.get("shares_or_principal_type") != SHARE_AMOUNT_TYPE:
            continue
        shares, value = row.get("shares_or_principal_amount"), row.get("value_as_filed")
        if not shares or not value:
            continue
        try:
            share_count, amount = Decimal(shares), Decimal(value)
        except InvalidOperation:
            continue
        if share_count > 0 and amount > 0:
            ratios.append(amount / share_count)
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def decide_value_unit(
    holdings: Sequence[Mapping[str, Any]], period_end: str | None
) -> tuple[str, str]:
    """Whether a 13F's values are dollars or thousands, and what decided it.

    Two independent answers, and both are reported. The *rule* is SEC's own:
    the 2022 amendments moved Form 13F from thousands to whole dollars for
    periods after 2022. The *check* is ``13f-tracker``'s and works because a
    share price is a share price: median(value / shares) lands near a plausible
    price when the values are dollars and near a thousandth of one when they
    are thousands.

    The rule decides unless the ratio *decisively* contradicts it, and then the
    ratio wins and says so -- filers were inconsistent across the boundary, and
    a number wrong by a factor of a thousand is the single worst thing this
    parser could emit.

    "Decisively" is the whole of the repair.  The original test was
    ``median < 1 means thousands``, taken from ``13f-tracker``, and it is wrong
    for any book whose holdings trade under a dollar: a genuine sub-$1 position
    reported in whole dollars has a ratio of 0.4, and that rule would have
    multiplied it by a thousand.  So the ratio only speaks outside the band
    where the two readings overlap, and inside it the basis is ``ambiguous`` --
    the period rule still decides the number, and the wire says the check did
    not confirm it rather than implying that it did.
    """

    by_rule = (
        "thousands"
        if isinstance(period_end, str) and period_end <= THOUSANDS_RULE_LAST_DAY
        else "usd"
    )
    rule_basis = "pre_2023_rule" if by_rule == "thousands" else "post_2023_rule"
    median = _median_value_per_share(holdings)
    if median is None:
        return by_rule, rule_basis
    if median < DECISIVE_THOUSANDS_BELOW:
        by_ratio = "thousands"
    elif median > DECISIVE_USD_ABOVE:
        by_ratio = "usd"
    else:
        return by_rule, "ambiguous"
    if by_ratio == by_rule:
        return by_rule, rule_basis
    return by_ratio, "ratio_heuristic"


def _scale(value: str | None, unit: str) -> str | None:
    if value is None:
        return None
    if unit != "thousands":
        return value
    try:
        scaled = Decimal(value) * 1000
    except InvalidOperation:  # pragma: no cover - _number already validated it
        return None
    text = format(scaled.normalize(), "f")
    return "0" if text in {"-0", "0E+0"} else text


def parse_form13f(
    table_text: str,
    *,
    accession: str,
    artifact_hash: str,
    holder_cik: str,
    primary_text: str | None = None,
    quarter: str | None = None,
    source_record_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """An institution's whole book for one quarter, and what unit it is in.

    Two documents: the cover page (``primary_doc.xml``) names the manager, the
    period and the totals; the information table lists the positions. The table
    is the one that must be present -- a 13F-NT has no table and reports that a
    different filer holds the positions -- so the cover page is optional and
    what it carries is used where it is there.
    """

    accession = _accession(accession)
    artifact_hash = _sha256(artifact_hash, "artifact_hash")
    cik = str(holder_cik or "").strip()
    if not re.match(r"^[0-9]{1,10}$", cik):
        raise SecOwnershipParseError(f"{holder_cik!r} is not a CIK")
    cik = cik.zfill(10)

    cover = _root(primary_text, what="13F cover page") if primary_text else None
    cover_page = _dfind(cover, "coverPage")
    summary = _dfind(cover, "summaryPage")
    declared_form = _dtext(cover, "submissionType") if cover is not None else None
    declared_form = (declared_form or "13F-HR").strip().upper()
    is_amendment = declared_form.endswith("/A")
    base_form = declared_form[:-2] if is_amendment else declared_form
    if base_form not in {"13F-HR", "13F-NT"}:
        raise SecOwnershipParseError(f"{declared_form!r} is not a Form 13F submission")
    holder_name = (
        _dtext(_dfind(cover_page, "filingManager"), "name")
        or _dtext(cover_page, "filingManagerName")
        or _dtext(cover, "companyName")
    ) if cover is not None else None
    period_end = _ddate(cover_page, "reportCalendarOrQuarter")

    table_root = _root(table_text, what="13F information table")
    holdings: list[dict[str, Any]] = []
    for node in _descendants(table_root, "infoTable"):
        issuer = _text(node, "nameOfIssuer")
        raw_cusip = _text(node, "cusip")
        if not issuer or not raw_cusip:
            continue
        squashed = re.sub(r"[^0-9A-Za-z]", "", raw_cusip).upper()
        if not _CUSIP_RE.match(squashed):
            raise SecOwnershipParseError(f"{raw_cusip!r} is not a CUSIP")
        amounts = _find(node, "shrsOrPrnAmt")
        voting = _find(node, "votingAuthority")
        figi = _text(node, "figi")
        if figi is not None and not re.match(r"^[0-9A-Z]{12}$", figi.upper()):
            figi = None
        elif figi is not None:
            figi = figi.upper()
        share_type = _text(amounts, "sshPrnamtType")
        holdings.append({
            "name_of_issuer": issuer,
            "title_of_class": _text(node, "titleOfClass"),
            "cusip": squashed,
            "figi": figi,
            "value_as_filed": _number(node, "value"),
            "value_usd": None,
            "shares_or_principal_amount": _number(amounts, "sshPrnamt"),
            "shares_or_principal_type": (
                share_type.upper() if share_type and share_type.upper() in {"SH", "PRN"}
                else None
            ),
            "put_call": _text(node, "putCall"),
            "investment_discretion": _text(node, "investmentDiscretion"),
            "other_managers": sorted({
                value for value in (
                    _text(item) for item in _descendants(node, "otherManager")
                ) if value
            }),
            "voting_authority_sole": _number(voting, "Sole") or _number(voting, "sole"),
            "voting_authority_shared": _number(voting, "Shared") or _number(voting, "shared"),
            "voting_authority_none": _number(voting, "None") or _number(voting, "none"),
        })
    if len(holdings) > MAX_HOLDINGS:
        raise SecOwnershipParseError(
            f"this information table has {len(holdings)} rows and the frozen "
            f"ceiling is {MAX_HOLDINGS}; the read is refused rather than "
            "truncated, because a book truncated here and compared against a "
            "prior quarter truncated elsewhere invents holdings changes. The "
            "raw bytes are in the spool."
        )
    unit, basis = decide_value_unit(holdings, period_end)
    for row in holdings:
        row["value_usd"] = _scale(row["value_as_filed"], unit)
        row["record_hash"] = _bind(row, accession=accession, artifact_hash=artifact_hash)

    entry_total = _dtext(summary, "tableEntryTotal")
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "accession": accession,
        "form_type": base_form,
        "is_amendment": is_amendment,
        "amendment_type": _dtext(cover_page, "amendmentType"),
        "holder_cik": cik,
        "holder_name": holder_name or f"cik:{cik}",
        "report_calendar_or_quarter": period_end,
        "quarter": quarter or quarter_of(period_end),
        "value_unit": unit,
        "value_unit_basis": basis,
        "table_entry_total": (
            int(entry_total) if isinstance(entry_total, str) and entry_total.isdigit()
            else None
        ),
        "table_value_total_as_filed": _dnumber(summary, "tableValueTotal"),
        "holdings": holdings,
        "source_record_refs": _refs(source_record_refs),
        "next_cursor": None,
        "provider_status": PROVIDER_STATUS,
    })


# -- what changed between two quarters --------------------------------------

HOLDING_ACTIONS: tuple[str, ...] = ("new", "exit", "add", "trim", "unchanged")


def position_ref(record_hashes: Sequence[str]) -> str:
    """One name for the filed rows a position was summed from."""

    return content_hash({"record_hashes": sorted(record_hashes)})


def aggregate_holdings(
    holdings: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, Any], dict[str, Any]]:
    """One row per ``(cusip, put_call)``, with the filed rows summed.

    A manager may report the same security on several lines -- one per
    sub-adviser, one per fund, one per discretion category -- and a filing
    that does so is not unusual. Keying a dict on ``(cusip, put_call)``
    therefore kept whichever line happened to be last and silently threw the
    rest away, so a position split across three lines was compared against
    a third of itself and produced a fabricated ``trim``.

    Summed rather than de-duplicated, because the lines are *parts* of one
    position and the sum is the position. Every filed row's ``record_hash``
    is kept, so the aggregate can still be taken back to each line it came
    from.
    """

    found: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in holdings:
        key = (row["cusip"], row.get("put_call"))
        held = found.get(key)
        if held is None:
            found[key] = {
                "cusip": row["cusip"],
                "put_call": row.get("put_call"),
                "name_of_issuer": row["name_of_issuer"],
                "title_of_class": row.get("title_of_class"),
                "shares_or_principal_amount": row.get("shares_or_principal_amount"),
                "shares_or_principal_type": row.get("shares_or_principal_type"),
                "value_as_filed": row.get("value_as_filed"),
                "value_usd": row.get("value_usd"),
                "line_count": 1,
                "record_hashes": [row["record_hash"]],
            }
            continue
        held["line_count"] += 1
        held["record_hashes"].append(row["record_hash"])
        for field in (
            "shares_or_principal_amount", "value_as_filed", "value_usd",
        ):
            held[field] = _sum_filed(held.get(field), row.get(field))
    for row in found.values():
        row["record_hashes"] = sorted(row["record_hashes"])
        row["record_hash"] = position_ref(row["record_hashes"])
    return found


def _sum_filed(one: Any, two: Any) -> str | None:
    """Two filed figures added, or None when neither side has one.

    A line with no figure does not zero a line that has one: an absent number
    is a number the filer did not give, and reading it as zero would report a
    position smaller than the filing does.
    """

    left, right = _decimal_or_none(one), _decimal_or_none(two)
    if left is None and right is None:
        return None
    total = (left or Decimal(0)) + (right or Decimal(0))
    text = format(total.normalize(), "f")
    return "0" if text in {"-0", "0E+0"} else text



def compare_holdings(
    current: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """This quarter against last, matched on CUSIP and option type.

    ``prior`` is allowed to be absent and that case is *named* rather than
    guessed at: an institution whose previous filing this Core has not read is
    not an institution that built its entire book this quarter, and reporting
    every position as ``new`` would put a hundred false "established a stake"
    events into the ledger the first time the lane runs.

    Matched on ``(cusip, put_call)`` because a put on a name and the name
    itself are different positions that share a CUSIP, and netting them is how
    a bearish position becomes a bullish one in a summary.
    """

    if prior is None:
        return {
            "status": "prior_absent",
            "reason": (
                "no prior quarter for this holder is held, so nothing here is a "
                "change; the positions are reported as a first reading"
            ),
            "prior_quarter": None,
            "changes": [],
            "first_reading_count": len(aggregate_holdings(current.get("holdings") or [])),
        }
    before = aggregate_holdings(prior.get("holdings") or [])
    after = aggregate_holdings(current.get("holdings") or [])
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=lambda item: (item[0], item[1] or "")):
        old, new = before.get(key), after.get(key)
        old_shares = _decimal_or_none((old or {}).get("shares_or_principal_amount"))
        new_shares = _decimal_or_none((new or {}).get("shares_or_principal_amount"))
        if old is None:
            action = "new"
        elif new is None:
            action = "exit"
        elif old_shares is None or new_shares is None:
            action = "unchanged" if old_shares == new_shares else "add"
        elif new_shares > old_shares:
            action = "add"
        elif new_shares < old_shares:
            action = "trim"
        else:
            action = "unchanged"
        delta = None
        if old_shares is not None and new_shares is not None:
            delta = format((new_shares - old_shares).normalize(), "f")
        elif new_shares is not None and old is None:
            delta = format(new_shares.normalize(), "f")
        elif old_shares is not None and new is None:
            delta = format((-old_shares).normalize(), "f")
        source = new if new is not None else old
        hashes = list((new if new is not None else old)["record_hashes"])
        changes.append({
            "cusip": key[0],
            "put_call": key[1],
            "name_of_issuer": source["name_of_issuer"],
            "title_of_class": source.get("title_of_class"),
            "action": action,
            "shares": (new or {}).get("shares_or_principal_amount"),
            "prior_shares": (old or {}).get("shares_or_principal_amount"),
            "share_change": delta,
            "value_usd": (new or {}).get("value_usd"),
            "prior_value_usd": (old or {}).get("value_usd"),
            # Every filed row this position was summed from, and one name for
            # the set of them. A split position is one change, and the change
            # can still be taken back to each row it came from.
            "record_hashes": hashes,
            "record_hash": position_ref(hashes),
        })
    return {
        "status": "compared",
        "reason": None,
        "prior_quarter": prior.get("quarter"),
        "changes": changes,
        "first_reading_count": 0,
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


__all__ = [
    "HOLDING_ACTIONS",
    "aggregate_holdings",
    "position_ref",
    "MAX_DOCUMENT_BYTES",
    "MAX_HOLDINGS",
    "MAX_NOTICES",
    "MAX_REPORTING_PERSONS",
    "MAX_TRANSACTIONS",
    "PROVIDER_STATUS",
    "SCHEMA_VERSION",
    "DECISIVE_THOUSANDS_BELOW",
    "DECISIVE_USD_ABOVE",
    "SHARE_AMOUNT_TYPE",
    "THOUSANDS_RULE_LAST_DAY",
    "TRANSACTION_CODE_MEANINGS",
    "SecOwnershipParseError",
    "compare_holdings",
    "decide_value_unit",
    "parse_beneficial_ownership",
    "parse_form144",
    "parse_form13f",
    "parse_form4",
    "purpose_text_digest",
    "quarter_of",
]
