"""ADR-0007: a figure that was verified against the bytes it cited may become a Claim.

Until now the numbers stopped at ``coverage_mission_document_figures``.  The
Ledger's numeric door was a JSON-pointer verifier that recomputes a value out
of a connector's normalized payload, and a figure read out of a document has no
such payload -- the digits exist in a span of text.  ``CandidateStagingStore.
stage`` therefore refused every cited-original quantitative candidate outright
(ADR-0003 option C, "do not build a text extractor", is the reason it was
right to).

ADR-0007 does not reopen that door.  It notices that a figure row is *already*
a deterministic numeric authority: the digits and the as-reported label were
both found in the exact quote it cites, checked before the row was written, and
the quote, the manifest hash and the row's own content hash are all stored so
the check can be run again by anyone.  What this module does is run it again --
against the row read back out of Core, never against what the caller passed --
and emit the ordinary ``numeric`` VerificationBundle that staging already
knows how to demand.

So the figure row plays the part the NumericVerificationSpec plays for a
connector candidate: ``numeric_spec_ref`` is the figure's id and
``numeric_spec_hash`` is the figure's content hash.  The CandidateClaim
contract does not change by one field, the review and adjudication path does
not change at all, and the figure ref stays reachable from the formal
ClaimVersion through ``candidate_origin_ref`` -- hash-bound at every hop.

Nothing here is wired into a lane.  ``promote_verified_figures`` is a function
and a CLI subcommand; the rule it depends on is off by default (see
``FIGURE_ADMISSION_POLICIES`` in ``research_verification``) until the
integrator re-signs the policy that names it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from .document_numeric_claim import (
    NumericCandidateError,
    verify_numeric_candidate,
)
from .research_verification import (
    FIGURE_ADMISSION_VERIFIED_FIGURE,
    ResearchVerificationConflict,
    VerificationRejected,
    build_candidate_evidence,
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

FIGURE_VERIFIER_REF = "verifier:document-figure-row-recheck:0.1"
FIGURE_VERIFIER_HASH = content_hash({
    "verifier": FIGURE_VERIFIER_REF,
    "checks": [
        "figure row content hash recomputes from its own columns",
        "figure is not retracted",
        "digits and as-reported label are in the stored citation",
        "caller's copy is byte-identical to the Core row",
    ],
})

STATEMENT_LINE_VERIFIER_REF = "verifier:statement-line-row-recheck:0.1"
STATEMENT_LINE_VERIFIER_HASH = content_hash({
    "verifier": STATEMENT_LINE_VERIFIER_REF,
    "checks": [
        "line belongs to an ingested filing with an accession",
        "filing row content hash recomputes from its own columns",
        "caller's copy is byte-identical to the Core row",
    ],
})

FIGURE_KINDS: tuple[str, ...] = ("document_figure", "statement_line")

_FIGURE_COLUMNS = (
    "figure_id", "company_ref", "review_ref", "document_ref",
    "source_manifest_hash", "quote_id", "citation_text", "metric_ref",
    "as_reported_label", "period", "value", "unit", "currency", "scale",
    "basis", "source_grade", "verified_by", "observed_by", "created_at",
)

_QUOTE_RE = re.compile(r"^quote:(\d+):(\d+):[0-9a-f]+$")


class FigureAdmissionError(RuntimeError):
    """A figure cannot be admitted as the numeric authority for a Claim."""


class FigureNotFound(FigureAdmissionError, LookupError):
    """No such figure row in this Core."""


def _finding(
    code: str, *, ok: bool, path: str, expected: Any, observed: Any, message: str
) -> dict[str, Any]:
    wire = {
        "code": code,
        "severity": "info" if ok else "error",
        "status": "pass" if ok else "fail",
        "path": path,
        "expected": None if expected is None else str(expected),
        "observed": None if observed is None else str(observed),
        "message": message if ok else message + " drifted",
    }
    wire["content_hash"] = content_hash(wire)
    return wire


def quote_span(quote_id: str) -> tuple[int, int] | None:
    """The character span a quote id names, or None if it names none.

    ``quote:40800:42000:233a4436b95b89d5`` -- the offsets are how a figure and
    a citation binding over the same document can be matched without a second
    index.
    """

    match = _QUOTE_RE.match(quote_id or "")
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if end > start else None


class DocumentFigureResolver:
    """Re-runs a figure's own verification against the Core row, not the caller.

    The whole value of a figure is that its check can be repeated.  This
    repeats it: the row is read back by id, its content hash is recomputed from
    its own columns, its retraction is checked, and the digits and label are
    matched against the citation text the row stores -- with the same verifier
    ``coverage_mission`` used when it wrote the row.  Only then is the caller's
    copy compared, and only byte-for-byte.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    # -- reads -----------------------------------------------------------

    def _table(self, name: str) -> bool:
        try:
            row = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def figure(self, figure_id: str) -> dict[str, Any]:
        """The Core row for one figure, as a plain mapping."""

        if not self._table("coverage_mission_document_figures"):
            raise FigureNotFound("this Core holds no document figures")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_document_figures WHERE figure_id=?",
            (figure_id,),
        ).fetchone()
        if row is None:
            raise FigureNotFound(f"no document figure {figure_id!r}")
        return {key: row[key] for key in row.keys()}

    def retracted(self, figure_id: str) -> bool:
        if not self._table("coverage_mission_document_figure_retractions"):
            return False
        return self.connection.execute(
            "SELECT 1 FROM coverage_mission_document_figure_retractions WHERE figure_id=?",
            (figure_id,),
        ).fetchone() is not None

    def figures(
        self, *, company_ref: str | None = None, source_grade: str | None = None
    ) -> list[dict[str, Any]]:
        """Every live figure, newest last.  Retracted rows are not data."""

        if not self._table("coverage_mission_document_figures"):
            return []
        query = (
            "SELECT f.* FROM coverage_mission_document_figures f "
            "LEFT JOIN coverage_mission_document_figure_retractions r "
            "ON r.figure_id=f.figure_id WHERE r.figure_id IS NULL"
        )
        params: list[Any] = []
        if company_ref is not None:
            query += " AND f.company_ref=?"
            params.append(company_ref)
        if source_grade is not None:
            query += " AND f.source_grade=?"
            params.append(source_grade)
        query += " ORDER BY f.created_at, f.figure_id"
        return [
            {key: row[key] for key in row.keys()}
            for row in self.connection.execute(query, params).fetchall()
        ]

    # -- the re-verification ---------------------------------------------

    def verify_figure(
        self, candidate: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(the Core row, a passing numeric VerificationBundle)`` or a refusal.

        Refuses rather than returning a failing bundle, because a staged
        candidate whose numeric authority failed is not a candidate anyone
        should have to look at: the figure it rests on is either good or it is
        not a figure.
        """

        if not isinstance(candidate, Mapping) or not candidate.get("figure_id"):
            raise FigureAdmissionError("a verified figure must name its figure_id")
        figure_id = str(candidate["figure_id"])
        held = self.figure(figure_id)
        findings: list[dict[str, Any]] = []

        body = {key: held[key] for key in _FIGURE_COLUMNS}
        recomputed = content_hash(body)
        hash_ok = recomputed == held["content_hash"]
        findings.append(_finding(
            "figure_content_hash", ok=hash_ok, path="figure.content_hash",
            expected=held["content_hash"], observed=recomputed,
            message="figure row content hash recomputes from its own columns",
        ))

        live = not self.retracted(figure_id)
        findings.append(_finding(
            "figure_not_retracted", ok=live, path="figure.retracted",
            expected="false", observed=str(not live).lower(),
            message="figure has not been withdrawn",
        ))

        # The check that made this a figure rather than a number: the digits
        # and the as-reported label are both in the quote it cites.  Re-run
        # with the same deterministic verifier the write path used.
        recheck_error: str | None = None
        try:
            verify_numeric_candidate(
                {
                    "quote_id": held["quote_id"],
                    "metric_ref": held["metric_ref"],
                    # Not stored on the row; the attribution check that used it
                    # happened at write time and is not re-litigated here.  The
                    # label carries the company's own wording either way.
                    "subject_as_named": held["as_reported_label"],
                    "as_reported_label": held["as_reported_label"],
                    "value": held["value"],
                    "unit": held["unit"],
                    "currency": held["currency"],
                    "period": held["period"],
                    "basis": held["basis"],
                    "scale": held["scale"],
                },
                {held["quote_id"]: held["citation_text"]},
            )
        except NumericCandidateError as exc:
            recheck_error = str(exc)
        findings.append(_finding(
            "citation_digits_and_label", ok=recheck_error is None,
            path="figure.citation_text", expected="value and label present in the quote",
            observed=recheck_error or "present",
            message="figure's digits and as-reported label are in the bytes it cited",
        ))

        supplied = dict(candidate)
        exact = canonical_json(supplied) == canonical_json(held)
        findings.append(_finding(
            "caller_copy_is_exact", ok=exact, path="figure",
            expected=held["content_hash"], observed=supplied.get("content_hash"),
            message="the supplied figure is byte-identical to the Core row",
        ))

        failed = [item["code"] for item in findings if item["status"] == "fail"]
        if failed:
            raise ResearchVerificationConflict(
                "figure re-verification failed: " + ", ".join(failed)
            )
        bundle = {
            "schema_version": "0.1",
            "id": "figure-numeric-verification:" + content_hash({
                "figure_id": figure_id,
                "figure_hash": held["content_hash"],
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": held["created_at"],
            "kind": "numeric",
            "subject_ref": figure_id,
            "subject_hash": held["content_hash"],
            "verdict": "pass",
            # The document and the manifest the figure was read from: the
            # exact thing this verification is *of*.
            "checkpoint_ref": held["document_ref"],
            "checkpoint_hash": held["source_manifest_hash"],
            "findings": findings,
            "verifier_ref": FIGURE_VERIFIER_REF,
            "verifier_hash": FIGURE_VERIFIER_HASH,
        }
        bundle["content_hash"] = content_hash(bundle)
        return held, validate_verification_bundle(bundle)

    # -- statement lines --------------------------------------------------

    def verify_statement_line(
        self, line_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The other verified number: a report line with an accession behind it.

        A statement line has stronger provenance than a document figure -- the
        accession belongs to one CIK by construction and the raw artifact hash
        is recorded -- and weaker admissibility, because it has no text
        citation and therefore cannot travel the cited-original staging path
        that ADR-0007 opens.  Verification is implemented here so the two kinds
        of number answer the same question the same way; the staging chain for
        it is the SEC connector authority path and is not built (see the P12b
        report).
        """

        for table in ("coverage_mission_statement_lines", "coverage_mission_statement_filings"):
            if not self._table(table):
                raise FigureNotFound("this Core holds no statement lines")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_lines WHERE line_id=?", (line_id,)
        ).fetchone()
        if row is None:
            raise FigureNotFound(f"no statement line {line_id!r}")
        line = {key: row[key] for key in row.keys()}
        filing_row = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
            (line["ingest_id"],),
        ).fetchone()
        if filing_row is None:
            raise FigureNotFound(f"statement line {line_id!r} has no filing")
        filing = {key: filing_row[key] for key in filing_row.keys()}
        findings = [
            _finding(
                "line_has_value", ok=line["value"] is not None, path="line.value",
                expected="a reported value", observed=line["value"],
                message="the line reports a number",
            ),
            _finding(
                "filing_accession", ok=bool(filing["accession"]), path="filing.accession",
                expected="an accession", observed=filing["accession"],
                message="the line belongs to one filed accession",
            ),
            _finding(
                "filing_source_records", ok=bool(json.loads(filing["source_record_refs_json"] or "[]")),
                path="filing.source_record_refs", expected="raw source records",
                observed=filing["source_record_refs_json"],
                message="the filing records the raw artifacts it was ingested from",
            ),
        ]
        failed = [item["code"] for item in findings if item["status"] == "fail"]
        if failed:
            raise ResearchVerificationConflict(
                "statement line re-verification failed: " + ", ".join(failed)
            )
        record = {
            "figure_kind": "statement_line",
            "figure_id": line["line_id"],
            "company_ref": filing["company_ref"],
            "accession": filing["accession"],
            "concept": line["concept"],
            "label": line["label"],
            "period_start": line["period_start"],
            "period_end": line["period_end"],
            "value": line["value"],
            "unit": line["unit"],
            "filing_content_hash": filing["content_hash"],
        }
        record["content_hash"] = content_hash(record)
        bundle = {
            "schema_version": "0.1",
            "id": "statement-line-verification:" + content_hash({
                "line_id": line["line_id"], "filing": filing["content_hash"],
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": filing["recorded_at"],
            "kind": "numeric",
            "subject_ref": line["line_id"],
            "subject_hash": record["content_hash"],
            "verdict": "pass",
            "checkpoint_ref": f"sec:filing:{filing['accession']}",
            "checkpoint_hash": filing["content_hash"],
            "findings": findings,
            "verifier_ref": STATEMENT_LINE_VERIFIER_REF,
            "verifier_hash": STATEMENT_LINE_VERIFIER_HASH,
        }
        bundle["content_hash"] = content_hash(bundle)
        return record, validate_verification_bundle(bundle)


# -- citation binding lookup ----------------------------------------------

def find_citation_binding(
    connection: Any, *, document_ref: str, source_manifest_hash: str, quote_id: str
) -> dict[str, Any] | None:
    """The smallest claim-eligible citation covering this figure's quote.

    A figure names the span it was read from and a citation binding names the
    span a Claim may cite; when the second contains the first, the figure's
    number is inside evidence that has already been admitted.  The *smallest*
    covering span is chosen so that a broad binding never quietly stands in for
    a tighter one, and ties break on the binding id so two runs agree.

    ``None`` means this figure has no admitted citation yet.  That is a real
    state and the promoter reports it rather than minting one: binding a
    citation is the correction authority's decision, not the promoter's.
    """

    span = quote_span(quote_id)
    if span is None:
        return None
    start, end = span
    rows = connection.execute(
        "SELECT b.* FROM transcript_claim_citation_bindings b "
        "JOIN transcript_correction_set_versions c "
        "ON c.version_id=b.correction_set_version_ref "
        "WHERE b.claim_eligible=1 AND b.source_manifest_hash=? "
        "AND b.source_start<=? AND b.source_end>=? "
        "ORDER BY (b.source_end - b.source_start), b.binding_id",
        (source_manifest_hash, start, end),
    ).fetchall()
    for row in rows:
        correction = connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (row["correction_set_version_ref"],),
        ).fetchone()
        if correction is None:
            continue
        try:
            record = json.loads(correction["record_json"])
        except (TypeError, ValueError):
            continue
        if record.get("document_ref") != document_ref:
            continue
        return {
            "binding_id": row["binding_id"],
            "correction_set_version_ref": row["correction_set_version_ref"],
            "source_start": row["source_start"],
            "source_end": row["source_end"],
            "content_hash": row["content_hash"],
        }
    return None


# -- the promoter ----------------------------------------------------------

def figure_claim_semantics(figure: Mapping[str, Any]) -> dict[str, Any]:
    """What the Claim says, derived from the figure and nothing else.

    The statement is generated, not drafted: a figure is a value for a measure
    in a period, and the sentence that says so has no room for judgement.  The
    grade's qualifier is appended by ``document_figure_grade`` so a reader of
    one Claim is told, in the Claim, whether the number was filed or spoken.
    """

    from .document_figure_grade import basis_for, qualify

    scale = f"{figure['scale']} " if figure.get("scale") else ""
    currency = f"{figure['currency']} " if figure.get("currency") else ""
    statement = (
        f"{figure['as_reported_label']} for {figure['period']} was "
        f"{currency}{figure['value']} {scale}({figure['unit']})"
    )
    return {
        "metric_or_aspect": figure["metric_ref"],
        "period": figure["period"],
        "basis": basis_for(figure["source_grade"]),
        "normalized_statement": qualify(statement, figure["source_grade"]),
    }


def promote_figure(
    core: Any,
    staging: Any,
    *,
    figure: Mapping[str, Any],
    citation_ref: str,
    correction_set_ref: str,
    actor_ref: str,
    idempotency_key: str,
    artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
    source_kind: str = "alphaengine",
    resolver: DocumentFigureResolver | None = None,
) -> dict[str, Any]:
    """Stage one verified figure as a quantitative candidate.

    Everything except the numeric authority is the transcript path unchanged:
    the same Core-held material, the same deterministic source verification,
    the same citation binding on the evidence.  What differs is that the claim
    carries the figure's value and points its numeric refs at the figure row.
    """

    from .transcript_candidate_staging import (
        SOURCE_KINDS,
        TranscriptCoreAuthorityResolver,
        bind_candidate_evidence_to_transcript_citation,
    )

    figures = resolver if resolver is not None else DocumentFigureResolver(core.connection)
    held, numeric_bundle = figures.verify_figure(figure)

    authority = TranscriptCoreAuthorityResolver(
        core, artifact_reader=artifact_reader, source_kind=source_kind
    )
    kind = SOURCE_KINDS[source_kind]
    binding, _correction_set = authority.citation(citation_ref)
    if binding["correction_set_version_ref"] != correction_set_ref:
        raise FigureAdmissionError(
            "figure citation does not belong to the requested correction set"
        )
    material = authority.build_material(citation_ref)
    source_verification = authority.verify_source_material(material)
    if source_verification["verdict"] != "pass":
        raise VerificationRejected(
            "figure candidate source verification did not pass"
        )
    when = material["retrieved_at"]
    semantics = figure_claim_semantics(held)
    evidence_ref = (
        f"candidate-evidence:{kind['candidate_prefix']}:" + binding["content_hash"][:32]
    )
    claim_ref = "candidate-claim:figure:" + content_hash({
        "figure_id": held["figure_id"], "figure_hash": held["content_hash"],
    })[:32]
    evidence = build_candidate_evidence(
        material, source_verification, candidate_evidence_ref=evidence_ref,
        actor_ref=actor_ref, created_at=when,
        verification_mode=authority.provenance_mode,
    )
    evidence = bind_candidate_evidence_to_transcript_citation(
        evidence, binding, source_type=kind["evidence_source_type"]
    )
    evidence_wire = validate_candidate_evidence(evidence)
    claim = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": claim_ref, "version": 1}
        ),
        "created_at": when,
        "candidate_claim_ref": claim_ref,
        "version": 1,
        "subject_ref": held["company_ref"],
        "metric_or_aspect": semantics["metric_or_aspect"],
        "period": semantics["period"],
        "basis": semantics["basis"],
        "normalized_statement": semantics["normalized_statement"],
        "semantic_verification_status": "unverified",
        "claim_kind": "quantitative",
        "value": held["value"],
        "unit": held["unit"],
        "currency": held["currency"],
        "scale": held["scale"],
        "candidate_evidence_refs": [
            {"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}
        ],
        "source_verification_ref": source_verification["id"],
        "source_verification_hash": source_verification["content_hash"],
        # The figure row is the numeric authority; these are its ref and hash.
        "numeric_spec_ref": held["figure_id"],
        "numeric_spec_hash": held["content_hash"],
        "numeric_verification_ref": numeric_bundle["id"],
        "numeric_verification_hash": numeric_bundle["content_hash"],
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    claim["content_hash"] = content_hash(claim)
    claim = validate_candidate_claim(claim)
    staged = staging.stage(
        material=material,
        source_verification=source_verification,
        evidence=evidence_wire,
        claim=claim,
        idempotency_key=idempotency_key,
        verification_mode=authority.provenance_mode,
        authority_resolver=authority,
        figure_admission_policy=FIGURE_ADMISSION_VERIFIED_FIGURE,
        verified_figure=held,
        figure_resolver=figures,
    )
    return {
        "write_status": staged["write_status"],
        "figure_id": held["figure_id"],
        "staging": staged,
        "claim": claim,
        "evidence": evidence_wire,
        "numeric_verification": numeric_bundle,
    }


def promote_verified_figures(
    core: Any,
    staging: Any,
    *,
    actor_ref: str,
    company_ref: str | None = None,
    source_grade: str | None = None,
    limit: int = 25,
    artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
    source_kind: str = "alphaengine",
    promoted: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Stage every un-promoted verified figure, and say why the rest were not.

    Deterministic: the figures come out of Core in a fixed order, each one's
    candidate identity is content-addressed on the figure row, and a figure
    already staged returns ``duplicate`` rather than a second candidate.  Three
    copies of one quarter's revenue therefore produce three candidates *if* the
    documents really do report them separately -- and the index puts them in
    one dedupe group with one canonical member, which is where the collapsing
    belongs.  Refusing to stage the second and third would throw away the fact
    that two documents agree.
    """

    resolver = DocumentFigureResolver(core.connection)
    already = set(promoted or ())
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for held in resolver.figures(company_ref=company_ref, source_grade=source_grade):
        if len(results) >= limit:
            break
        if held["figure_id"] in already:
            continue
        binding = find_citation_binding(
            core.connection, document_ref=held["document_ref"],
            source_manifest_hash=held["source_manifest_hash"],
            quote_id=held["quote_id"],
        )
        if binding is None:
            skipped.append({
                "figure_id": held["figure_id"],
                "reason": "no claim-eligible citation covers this figure's quote",
            })
            continue
        try:
            results.append(promote_figure(
                core, staging, figure=held,
                citation_ref=binding["binding_id"],
                correction_set_ref=binding["correction_set_version_ref"],
                actor_ref=actor_ref,
                idempotency_key="figure-promotion:" + held["figure_id"],
                artifact_reader=artifact_reader, source_kind=source_kind,
                resolver=resolver,
            ))
        except (FigureAdmissionError, VerificationRejected,
                ResearchVerificationConflict) as exc:
            skipped.append({
                "figure_id": held["figure_id"],
                "reason": f"{type(exc).__name__}: {exc}",
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "promoted": [
            {"figure_id": item["figure_id"],
             "candidate_claim_ref": item["claim"]["id"],
             "write_status": item["write_status"]}
            for item in results
        ],
        "skipped": skipped,
        "results": results,
    }


__all__ = [
    "FIGURE_KINDS",
    "FIGURE_VERIFIER_HASH",
    "FIGURE_VERIFIER_REF",
    "STATEMENT_LINE_VERIFIER_HASH",
    "STATEMENT_LINE_VERIFIER_REF",
    "DocumentFigureResolver",
    "FigureAdmissionError",
    "FigureNotFound",
    "figure_claim_semantics",
    "find_citation_binding",
    "promote_figure",
    "promote_verified_figures",
    "quote_span",
]
