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

import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping

from .document_numeric_claim import (
    NumericCandidateError,
    verify_numeric_candidate,
)
from .research_verification import (
    FIGURE_ADMISSIBLE_GRADES as _ADMISSIBLE_GRADES,
    FIGURE_ADMISSION_VERIFIED_FIGURE,
    FIGURE_NUMERIC_VERIFIER_HASH,
    FIGURE_NUMERIC_VERIFIER_REF,
    MISSION_FIGURE_AUTHORITY_MODE,
    MISSION_FIGURE_SOURCE_VERIFIER_HASH,
    MISSION_FIGURE_SOURCE_VERIFIER_REF,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    STATEMENT_LINE_VERIFIER_HASH,
    STATEMENT_LINE_VERIFIER_REF,
    ResearchVerificationConflict,
    ResearchVerificationError,
    VerificationRejected,
    build_candidate_evidence,
    figure_candidate_numerics,
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_source_verification_material,
    validate_verification_bundle,
)
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

# Named in ``research_verification`` beside the other numeric verifiers,
# because a VerificationBundle keeps a closed list of who may have produced
# one and a verifier this module invented for itself could not sign anything.
FIGURE_VERIFIER_REF = FIGURE_NUMERIC_VERIFIER_REF
FIGURE_VERIFIER_HASH = FIGURE_NUMERIC_VERIFIER_HASH

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
                _numeric_candidate(held), {held["quote_id"]: held["citation_text"]})
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




# -- the Core chain a figure hangs from ------------------------------------

class MissionFigureAuthorityResolver(DocumentFigureResolver):
    """The `mission_figure_authority` provenance mode (ADR-0007).

    The material is the figure row.  Everything around it is re-derived from
    Core, and this is the whole chain:

        figure -> mission document review -> discovered document -> the
        discovery that found it -> the connector SourceEnvelope that
        enumerated the document -> the raw ArtifactVersion behind it

    Every hop exists in the live Core today for all five surviving figures --
    checked before this was written, because a provenance mode that only works
    on a fixture is not a provenance mode.  The envelope is the search that
    *named* this document, not a fetch of it: what binds the digits to the
    bytes is the figure's own citation, re-checked here, and what the envelope
    adds is that the document is a record this connector really returned.

    No transcript citation binding is involved, which is the point.  A figure
    from a SEC filing has no correction set and never will, and requiring one
    was what left twelve verified numbers with nowhere to go.
    """

    provenance_mode = MISSION_FIGURE_AUTHORITY_MODE
    verifier = (MISSION_FIGURE_SOURCE_VERIFIER_REF, MISSION_FIGURE_SOURCE_VERIFIER_HASH)

    # -- the chain --------------------------------------------------------

    def chain(self, figure: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve every hop, or say which one is missing."""

        review = self._row(
            "coverage_mission_document_reviews", "review_id", figure["review_ref"],
            "mission document review")
        if review["document_ref"] != figure["document_ref"]:
            raise FigureAdmissionError(
                "the figure's review names another document")
        discovered = self._row(
            "coverage_mission_discovered_documents", "record_id",
            review["discovered_document_ref"], "discovered document")
        if discovered["document_ref"] != figure["document_ref"]:
            raise FigureAdmissionError(
                "the discovered document names another document")
        discovery = self._row(
            "coverage_mission_source_discoveries", "record_id",
            discovered["discovery_ref"], "source discovery")
        envelope_row = self._row(
            "connector_source_envelopes", "source_envelope_id",
            discovery["source_envelope_ref"], "connector SourceEnvelope")
        if envelope_row["content_hash"] != discovery["source_envelope_hash"]:
            raise FigureAdmissionError(
                "the discovery's SourceEnvelope hash is not the one Core holds")
        envelope = json.loads(envelope_row["record_json"])
        artifact_row = self.connection.execute(
            "SELECT v.version_id, v.content_hash FROM observability_artifact_version_index i "
            "JOIN observability_artifact_versions_v2 v ON v.version_id=i.version_id "
            "WHERE i.version_id=?",
            (envelope.get("raw_artifact_version_ref"),),
        ).fetchone()
        if artifact_row is None:
            raise FigureNotFound(
                "the SourceEnvelope's raw ArtifactVersion is not in this Core")
        return {
            "review": review, "discovered": discovered, "discovery": discovery,
            "envelope": envelope, "envelope_hash": envelope_row["content_hash"],
            "artifact_ref": artifact_row["version_id"],
            "artifact_hash": artifact_row["content_hash"],
        }

    def _row(self, table: str, column: str, identifier: Any, name: str) -> dict[str, Any]:
        if not self._table(table):
            raise FigureNotFound(f"this Core holds no {name}s")
        row = self.connection.execute(
            f"SELECT * FROM {table} WHERE {column}=?", (identifier,)
        ).fetchone()
        if row is None:
            raise FigureNotFound(f"{name} {identifier!r} is not in this Core")
        return {key: row[key] for key in row.keys()}

    # -- the material -----------------------------------------------------

    @staticmethod
    def figure_projection(figure: Mapping[str, Any]) -> dict[str, Any]:
        """What the material asserts about the number, and nothing else.

        Deliberately not the whole row: the projection is the number, the
        measure, the period, the words the filer used and the citation it was
        read from.  ``observed_by`` and ``review_ref`` are provenance and are
        checked through the chain instead.
        """

        return {
            "figure_id": figure["figure_id"],
            "figure_content_hash": figure["content_hash"],
            "company_ref": figure["company_ref"],
            "document_ref": figure["document_ref"],
            "metric_ref": figure["metric_ref"],
            "as_reported_label": figure["as_reported_label"],
            "period": figure["period"],
            "value": figure["value"],
            "unit": figure["unit"],
            "currency": figure["currency"],
            "scale": figure["scale"],
            "basis": figure["basis"],
            "source_grade": figure["source_grade"],
            "quote_id": figure["quote_id"],
            "citation_hash": content_hash({
                "quote_id": figure["quote_id"], "raw_text": figure["citation_text"],
            }),
            "source_manifest_hash": figure["source_manifest_hash"],
            "verified_by": figure["verified_by"],
        }

    def build_material(self, figure_id: str) -> dict[str, Any]:
        """The figure row as an AuthoritySourceVerificationMaterial 0.2."""

        figure = self.figure(figure_id)
        chain = self.chain(figure)
        envelope = chain["envelope"]
        payload = self.figure_projection(figure)
        lineage = [
            envelope["source"], envelope["id"], chain["artifact_ref"],
            figure["document_ref"], figure["figure_id"],
        ]
        base = {
            "schema_version": "0.2",
            "id": "mission-figure-material:" + content_hash({
                "figure_id": figure["figure_id"],
                "figure_hash": figure["content_hash"],
                "envelope": chain["envelope_hash"],
            })[:32],
            "created_at": figure["created_at"],
            "source_envelope_ref": envelope["id"],
            "source_envelope_hash": chain["envelope_hash"],
            "artifact_ref": chain["artifact_ref"],
            "artifact_hash": chain["artifact_hash"],
            "source_ref": envelope["source"],
            "source_type": "official_filing",
            "operation": envelope["operation"],
            "provenance_mode": self.provenance_mode,
            # The explicit provenance edge: this material exists because of
            # exactly this figure row, at exactly this hash.
            "authority_resolution_ref": figure["figure_id"],
            "authority_resolution_hash": figure["content_hash"],
            "source_record_refs": [figure["document_ref"]],
            "next_cursor": envelope.get("cursor"),
            "normalized_payload": payload,
            "normalized_payload_hash": hashlib.sha256(
                canonical_json(payload).encode("utf-8")).hexdigest(),
            "source_schema_hash": envelope["source_schema_hash"],
            "source_content_hash": figure["source_manifest_hash"],
            "source_lineage": lineage,
            "published_at": envelope.get("published_at"),
            "updated_at": envelope.get("updated_at"),
            "as_of": envelope.get("as_of"),
            "retrieved_at": envelope["retrieved_at"],
            "completeness": "enumerated",
            "status": "complete",
        }
        base["content_hash"] = content_hash(base)
        return validate_source_verification_material(base)

    # -- the deterministic verifier ---------------------------------------

    def verify_source_material(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """Re-derive the whole chain from Core and emit a bundle.

        ``CandidateStagingStore.stage(verification_mode="mission_figure_authority")``
        calls this and requires the caller's bundle to be byte-identical, the
        same contract the transcript mode has.
        """

        material_wire = validate_source_verification_material(material)
        if material_wire.get("provenance_mode") != self.provenance_mode:
            raise VerificationRejected(
                f"the mission figure verifier requires {self.provenance_mode} material")
        findings: list[dict[str, Any]] = []

        def check(code: str, observed: Any, expected: Any, path: str, message: str) -> None:
            ok = observed == expected
            findings.append(_finding(
                code, ok=ok, path=path,
                expected=canonical_json(expected) if isinstance(expected, (dict, list)) else expected,
                observed=canonical_json(observed) if isinstance(observed, (dict, list)) else observed,
                message=message))

        figure = self.figure(material_wire["authority_resolution_ref"])
        body = {key: figure[key] for key in _FIGURE_COLUMNS}
        check("figure_content_hash", content_hash(body), figure["content_hash"],
              "figure.content_hash", "figure row hash recomputes from its own columns")
        check("figure_authority_hash", material_wire["authority_resolution_hash"],
              figure["content_hash"], "material.authority_resolution_hash",
              "material binds the exact figure row")
        check("figure_not_retracted", self.retracted(figure["figure_id"]), False,
              "figure.retracted", "figure has not been withdrawn")
        check("figure_grade_is_filed", figure["source_grade"] in _ADMISSIBLE_GRADES, True,
              "figure.source_grade",
              "figure is a company-filed document, not an earnings call")
        try:
            verify_numeric_candidate(
                _numeric_candidate(figure), {figure["quote_id"]: figure["citation_text"]})
            digits = "present"
        except NumericCandidateError as exc:
            digits = str(exc)
        check("citation_digits_and_label", digits, "present", "figure.citation_text",
              "figure's digits and as-reported label are in the bytes it cited")
        check("quote_names_a_span", quote_span(figure["quote_id"]) is not None, True,
              "figure.quote_id", "the quote id names a span of the original")
        check("citation_hash", material_wire["normalized_payload"].get("citation_hash"),
              content_hash({"quote_id": figure["quote_id"],
                            "raw_text": figure["citation_text"]}),
              "material.normalized_payload.citation_hash",
              "material binds the exact citation the digits were checked against")
        check("figure_projection", material_wire["normalized_payload"],
              self.figure_projection(figure), "material.normalized_payload",
              "material projection equals the Core figure row")

        chain = self.chain(figure)
        envelope = chain["envelope"]
        check("review_binds_document", chain["review"]["document_ref"],
              figure["document_ref"], "review.document_ref",
              "the mission review names this document")
        check("discovered_binds_document", chain["discovered"]["document_ref"],
              figure["document_ref"], "discovered_document.document_ref",
              "the discovered document names this document")
        check("source_envelope_hash", material_wire["source_envelope_hash"],
              chain["envelope_hash"], "material.source_envelope_hash",
              "SourceEnvelope hash is exact Core authority")
        check("envelope_names_the_document",
              figure["document_ref"] in (envelope.get("source_record_refs") or []),
              True, "source.source_record_refs",
              "the SourceEnvelope enumerated this document")
        check("artifact_ref", material_wire["artifact_ref"], chain["artifact_ref"],
              "material.artifact_ref", "raw ArtifactVersion ref is exact")
        check("artifact_hash", material_wire["artifact_hash"], chain["artifact_hash"],
              "material.artifact_hash", "raw ArtifactVersion hash is exact")
        check("raw_response_hash_equals_artifact", envelope.get("raw_response_hash"),
              self._artifact_content_hash(chain["artifact_ref"]),
              "source.raw_response_hash",
              "SourceEnvelope raw hash equals the ArtifactVersion bytes hash")
        for field, value in (
            ("source_ref", envelope.get("source")),
            ("operation", envelope.get("operation")),
            ("source_schema_hash", envelope.get("source_schema_hash")),
            ("retrieved_at", envelope.get("retrieved_at")),
        ):
            check(f"material_{field}", material_wire[field], value,
                  f"material.{field}", f"material {field} equals SourceEnvelope")
        check("source_content_hash", material_wire["source_content_hash"],
              figure["source_manifest_hash"], "material.source_content_hash",
              "material binds the manifest of the original the figure was read from")
        check("source_lineage", material_wire["source_lineage"],
              [envelope["source"], envelope["id"], chain["artifact_ref"],
               figure["document_ref"], figure["figure_id"]],
              "material.source_lineage",
              "material lineage is source, envelope, artifact, document, figure")

        verdict = "reject" if any(
            item["status"] == "fail" and item["severity"] == "error" for item in findings
        ) else "pass"
        base = {
            "schema_version": "0.1",
            "id": "mission-figure-source-verification:" + content_hash({
                "subject": material_wire["id"],
                "figure": figure["content_hash"],
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": material_wire["retrieved_at"],
            "kind": "source",
            "subject_ref": material_wire["id"],
            "subject_hash": material_wire["content_hash"],
            "verdict": verdict,
            # The figure row is the checkpoint of this mode: it is the record
            # that was admitted, and the thing this verification is *of*.
            "checkpoint_ref": figure["figure_id"],
            "checkpoint_hash": figure["content_hash"],
            "findings": findings,
            "verifier_ref": self.verifier[0],
            "verifier_hash": self.verifier[1],
        }
        base["content_hash"] = content_hash(base)
        return validate_verification_bundle(base)

    def _artifact_content_hash(self, artifact_ref: str) -> str | None:
        row = self.connection.execute(
            "SELECT record_json FROM observability_artifact_versions_v2 WHERE version_id=?",
            (artifact_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["record_json"]).get("artifact_content_hash")
        except (TypeError, ValueError):
            return None


def _numeric_candidate(figure: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "quote_id": figure["quote_id"],
        "metric_ref": figure["metric_ref"],
        # Not stored on the row; the attribution check that used it happened at
        # write time and is not re-litigated here. The label carries the
        # company's own wording either way.
        "subject_as_named": figure["as_reported_label"],
        "as_reported_label": figure["as_reported_label"],
        "value": figure["value"],
        "unit": figure["unit"],
        "currency": figure["currency"],
        "period": figure["period"],
        "basis": figure["basis"],
        "scale": figure["scale"],
    }


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
    actor_ref: str,
    idempotency_key: str,
    resolver: MissionFigureAuthorityResolver | None = None,
) -> dict[str, Any]:
    """Stage one company-filed figure as a quantitative candidate.

    No citation binding, no correction set, no transcript: the material is the
    figure row and its Core chain, which is what ADR-0007 decided and what lets
    a SEC-filing figure move at all.

    A spoken figure is refused here as well as in the staging store.  Two
    guards for one rule is on purpose: the store's is the contract and this one
    is the sentence a caller reads.
    """

    figures = resolver if resolver is not None else MissionFigureAuthorityResolver(
        core.connection)
    held, numeric_bundle = figures.verify_figure(figure)
    if held["source_grade"] not in _ADMISSIBLE_GRADES:
        raise FigureAdmissionError(
            f"{held['source_grade']} figures stay qualitative (ADR-0007): "
            "management said this number, the company did not publish it"
        )
    material = figures.build_material(held["figure_id"])
    source_verification = figures.verify_source_material(material)
    if source_verification["verdict"] != "pass":
        failed = [
            item["code"] for item in source_verification["findings"]
            if item["severity"] == "error" and item["status"] == "fail"
        ]
        raise VerificationRejected(
            "mission figure authority verification rejected: " + ", ".join(failed))

    when = material["retrieved_at"]
    semantics = figure_claim_semantics(held)
    numerics = figure_candidate_numerics(held)
    evidence_ref = "candidate-evidence:mission-figure:" + held["content_hash"][:32]
    claim_ref = "candidate-claim:mission-figure:" + content_hash({
        "figure_id": held["figure_id"], "figure_hash": held["content_hash"],
    })[:32]
    evidence = validate_candidate_evidence(build_candidate_evidence(
        material, source_verification, candidate_evidence_ref=evidence_ref,
        actor_ref=actor_ref, created_at=when,
        verification_mode=MISSION_FIGURE_AUTHORITY_MODE,
    ))
    claim = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": claim_ref, "version": 1}),
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
        # Built from the row through one mapping, never read off it field by
        # field: a percentage has no scale word and the contract has no null
        # scale, and reading held["scale"] straight was a crash.
        "value": numerics["value"],
        "unit": numerics["unit"],
        "currency": numerics["currency"],
        "scale": numerics["scale"],
        "candidate_evidence_refs": [
            {"ref": evidence["id"], "hash": evidence["content_hash"]}],
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
        evidence=evidence,
        claim=claim,
        idempotency_key=idempotency_key,
        verification_mode=MISSION_FIGURE_AUTHORITY_MODE,
        figure_admission_policy=FIGURE_ADMISSION_VERIFIED_FIGURE,
        verified_figure=held,
        figure_resolver=figures,
    )
    return {
        "write_status": staged["write_status"],
        "figure_id": held["figure_id"],
        "staging": staged,
        "material": material,
        "source_verification": source_verification,
        "claim": claim,
        "evidence": evidence,
        "numeric_verification": numeric_bundle,
    }


def promote_verified_figures(
    core: Any,
    staging: Any,
    *,
    actor_ref: str,
    company_ref: str | None = None,
    limit: int = 25,
    promoted: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Stage every un-promoted company-filed figure, and say why the rest were not.

    Deterministic: the figures come out of Core in a fixed order, each
    candidate's identity is content-addressed on the figure row, and a figure
    already staged returns ``duplicate`` rather than a second candidate.

    **Spoken figures are never swept.**  They are listed in ``skipped`` with
    the reason, because ADR-0007 kept ADR-0003's finding about transcripts and
    a sweep that silently walked past half its input would hide that.

    Three copies of one quarter's revenue therefore produce three candidates
    *if* the documents really do report them separately -- and the index puts
    them in one dedupe group with one canonical member, which is where the
    collapsing belongs.  Refusing to stage the second and third would throw
    away the fact that two documents agree.
    """

    resolver = MissionFigureAuthorityResolver(core.connection)
    already = set(promoted or ())
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    held_figures = [
        item for item in resolver.figures(company_ref=company_ref)
        if item["figure_id"] not in already
    ]
    truncated = 0
    for index, held in enumerate(held_figures):
        if len(results) >= limit:
            truncated = len(held_figures) - index
            break
        if held["source_grade"] not in _ADMISSIBLE_GRADES:
            skipped.append({
                "figure_id": held["figure_id"],
                "reason": (
                    f"{held['source_grade']} stays qualitative under ADR-0007; "
                    "management said this number, the company did not publish it"
                ),
            })
            continue
        try:
            results.append(promote_figure(
                core, staging, figure=held, actor_ref=actor_ref,
                idempotency_key="figure-promotion:" + held["figure_id"],
                resolver=resolver,
            ))
        except (FigureAdmissionError, ResearchVerificationError) as exc:
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
        # A sweep that stopped at its bound and did not say so reads as
        # "everything is done".
        "truncated": truncated,
        "results": results,
    }


__all__ = [
    "FIGURE_KINDS",
    "FIGURE_VERIFIER_HASH",
    "FIGURE_VERIFIER_REF",
    "MISSION_FIGURE_AUTHORITY_MODE",
    "MISSION_FIGURE_SOURCE_VERIFIER_HASH",
    "MISSION_FIGURE_SOURCE_VERIFIER_REF",
    "MISSION_VERIFIED_FIGURE_RULE_REF",
    "STATEMENT_LINE_VERIFIER_HASH",
    "STATEMENT_LINE_VERIFIER_REF",
    "DocumentFigureResolver",
    "FigureAdmissionError",
    "FigureNotFound",
    "MissionFigureAuthorityResolver",
    "figure_claim_semantics",
    "promote_figure",
    "promote_verified_figures",
    "quote_span",
]
