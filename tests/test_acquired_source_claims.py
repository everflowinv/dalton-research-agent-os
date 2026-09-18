"""P13aq: the four sources that could be read and never counted.

P13ap opened the door: a Guidepoint excerpt, a sell-side sales note, a
company-wiki page and this fund's own prior research could all be read, quoted
and drafted from.  Every one of them was then closed ``dismissed`` at zero
formal writes, because the candidate chain bound AlphaEngine document lineage
or the public-web correction authority and these four had neither.  Live on
2026-09-18 that was 148 documents in the Hyperscaler workspace and 388 in
legacy -- the majority of the research material in the building, read at cost
and contributing nothing.

These cases drive the whole chain for each of the four, through the real
extraction child with no step mocked: acquisition -> content-addressed object
-> re-read and re-hashed original -> automation-verified raw span -> claim
citation -> staged candidate -> policy review -> committed Claim, with the
importance the claim index ranks it under.  And the two refusals that make the
chain a chain rather than a pipeline: an original whose bytes drifted cannot be
quoted, and an acquisition whose raw receipt drifted cannot be staged against
even though it still reads.

The harnesses come from ``test_corpus_document_reading`` because acquisition is
the expensive half and it already builds it -- one real lane tick per source,
through the real child.  Their own reading cases stay in their own module; see
``_own_cases_only``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.acquired_source_authority import (
    ACQUIRED_SOURCE_KINDS,
    EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE,
    INTERNAL_PRIOR_RESEARCH_EVIDENCE_SOURCE_TYPE,
    INTERNAL_WIKI_EVIDENCE_SOURCE_TYPE,
    SELL_SIDE_NOTE_EVIDENCE_SOURCE_TYPE,
)
from dalton_core.claim_index_tagging import ProvenanceResolver
from dalton_core.document_extraction import (
    STAGEABLE_SOURCE_REFS,
    DocumentExtractionService,
)
from dalton_core.research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
from dalton_core.store import content_hash
# Imported as a module, not by name: ``unittest`` collects every TestCase in
# this module's namespace, and importing the reading classes directly would
# run that module's whole suite again here.
from tests import test_corpus_document_reading as reading
from tests.test_corpus_document_reading import (
    run_hermetic_sweep,
    sign_document_qualitative_rule,
)
from tests.test_s1_human_feeds import ACN_NOTE


def _own_cases_only(cls):
    """Keep this module's cases; drop the reading module's inherited ones.

    These classes reuse the reading harness's acquisition -- a real lane tick
    through a real child -- and nothing else about it.  Re-running its cases
    here would run them twice and, worse, would run them against a Core whose
    governance policy this module has already changed.  unittest collects only
    callables, so ``None`` is how a name stops being a case.
    """

    for name in dir(cls):
        if name.startswith("test_") and getattr(
            getattr(cls, name), "__module__", None
        ) != __name__:
            setattr(cls, name, None)
    return cls


class _CommittedClaimChecks:
    """What a committed Claim from an acquired original has to look like."""

    EVIDENCE_SOURCE_TYPE = ""
    SOURCE_REF = ""
    IMPORTANCE = ""

    def assert_committed(self, core, summary) -> dict:
        self.assertEqual(summary["status"], "succeeded", summary)
        admitted = [item for item in summary["admitted"] if item["status"] == "admitted"]
        self.assertTrue(admitted, summary["admitted"])
        # One Evidence version and one Claim version per admitted suggestion.
        self.assertEqual(summary["formal_authority_writes"], 2 * len(admitted))
        entry = admitted[0]
        claim = json.loads(core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (entry["claim_version_ref"],),
        ).fetchone()["claim_json"])
        self.assertEqual(claim["claim_kind"], "qualitative")
        self.assertIsNone(claim["value"])
        self.assertEqual(claim["version"], 1)
        evidence = json.loads(core.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
            (entry["evidence_version_ref"],),
        ).fetchone()["evidence_json"])
        # What the cited original *is* -- the value the claim index ranks by.
        self.assertEqual(evidence["source_type"], self.EVIDENCE_SOURCE_TYPE)
        self.assertEqual(evidence["source_ref"], self.SOURCE_REF)
        # Two artifact refs: the acquisition's raw response and the exact
        # citation binding cut out of the re-read, re-hashed original.
        self.assertEqual(len(evidence["artifact_refs"]), 2)
        self.assertTrue(evidence["artifact_refs"][1]["ref"].startswith(
            "transcript-claim-citation-binding:"))
        self.assertEqual(entry["policy_rule_ref"], DOCUMENT_QUALITATIVE_RULE_REF)
        return {"claim": claim, "evidence": evidence, "entry": entry}

    def assert_importance(self, core, claim_version_ref) -> None:
        resolved = ProvenanceResolver(core.connection).resolve(claim_version_ref)
        self.assertEqual(resolved["importance"], self.IMPORTANCE, resolved)

    def assert_citation_is_an_exact_span(self, core, evidence, document_ref) -> dict:
        """The citation is an eligible raw span of this document's original."""

        binding = json.loads(core.connection.execute(
            "SELECT record_json FROM transcript_claim_citation_bindings WHERE binding_id=?",
            (evidence["artifact_refs"][1]["ref"],),
        ).fetchone()["record_json"])
        self.assertTrue(binding["claim_eligible"])
        self.assertEqual(binding["unresolved_correction_indexes"], [])
        correction = json.loads(core.connection.execute(
            "SELECT record_json FROM transcript_correction_set_versions WHERE version_id=?",
            (binding["correction_set_version_ref"],),
        ).fetchone()["record_json"])
        self.assertEqual(correction["document_ref"], document_ref)
        self.assertEqual(correction["review_scope"], "automation_verified_raw_span")
        # The acquisition manifest is what the whole chain rests on.
        self.assertEqual(binding["source_manifest_ref"], correction["source_manifest_ref"])
        return binding


# -- the three local corpora ----------------------------------------------

class _FeedClaimHarness(_CommittedClaimChecks):
    """The reading harness's acquisition, carried through to a Claim."""

    STATEMENT = ""
    ASPECT = ""
    BASIS = ""

    def setUp(self) -> None:
        super().setUp()
        sign_document_qualitative_rule(self.core)

    def target_document_ref(self):
        return None

    def attributed_review(self):
        """The first waiting review whose document names its own company.

        P13i refuses to mint a Claim against a company a document never names,
        and these corpora hold industry pages and other companies' notes too.
        Picking the attributed one is the point of the case, not a way around
        the gate: the gate stays on and the other documents in the same tick
        are dismissed by it.
        """

        ref = self.target_document_ref()
        if ref is not None:
            return self.review(ref)
        service = DocumentExtractionService(self.writer())
        for row in self.missions.document_reviews(self.mission["id"], limit=100):
            if row["state"] != "awaiting_human_extraction":
                continue
            review = self.missions.document_review(row["review_id"])
            context = self.view(review)["context"]
            if service.document_names_subject(context).get("names_subject"):
                return review
        raise AssertionError("no waiting review names its own company")

    def sweep(self):
        review = self.attributed_review()
        context = self.view(review)["context"]
        return review, run_hermetic_sweep(
            self, state=self.state, root=self.root,
            quote_id=context["quotes"][0]["quote_id"],
            normalized_statement=self.STATEMENT, metric_or_aspect=self.ASPECT,
            basis=self.BASIS)

    def test_the_document_reaches_a_committed_claim(self) -> None:
        self.assertIn(self.SOURCE_REF, STAGEABLE_SOURCE_REFS)
        review, summary = self.sweep()
        bundle = self.assert_committed(self.core, summary)
        self.assert_importance(self.core, bundle["entry"]["claim_version_ref"])
        binding = self.assert_citation_is_an_exact_span(
            self.core, bundle["evidence"], review["document_ref"])
        manifest = self.manifest_for(review["document_ref"])["manifest"]
        self.assertEqual(binding["source_manifest_ref"], manifest["id"])
        self.assertEqual(binding["source_manifest_hash"], manifest["content_hash"])
        self.assertEqual(binding["source_content_hash"],
                         manifest["declared_content_sha256"])
        # The review closes as staged, not dismissed with a reason.
        resolved = {item["review_id"]: item for item in summary["resolved_reviews"]}
        self.assertEqual(resolved[review["review_id"]]["status"], "extraction_staged",
                         resolved[review["review_id"]])
        # And the read is recorded, so the lane never pays to read it again.
        proofs = [row["document_ref"] for row in self.core.connection.execute(
            "SELECT document_ref FROM document_read_completion_proofs")]
        self.assertIn(review["document_ref"], proofs)

    def test_a_drifted_acquisition_object_cannot_be_quoted(self) -> None:
        """The object is the document, so bytes that moved are another one."""

        review = self.attributed_review()
        manifest = self.manifest_for(review["document_ref"])["manifest"]
        declared = manifest["assembled_object"]
        path = self.spool_object_path(declared["content_hash"])
        # Same length, different bytes: the refusal has to be the hash, not a
        # size check that a careful forger would have matched.
        path.write_bytes(b"x" * declared["size_bytes"])
        service = DocumentExtractionService(self.writer())
        with self.assertRaises(Exception) as caught:
            service.view(review_id=review["review_id"],
                         expected_review_hash=content_hash(review),
                         offset=0, actor_ref=self.automation_principal())
        self.assertIn("do not hash to the spool object they claim",
                      str(caught.exception))

    def test_a_drifted_acquisition_receipt_cannot_be_staged_against(self) -> None:
        """Readable is not citable: the receipt the Claim rests on must hold.

        The raw connector response is not the text -- the note is markdown and
        the response is the host tool's JSON around it -- so a document whose
        receipt drifted still reads, and still drafts.  It must not commit: the
        candidate resolver re-hashes the acquisition's raw ArtifactVersion out
        of the spool, a mismatch is a failing finding, a failing finding is a
        rejected bundle, and a rejected bundle is a refused suggestion with its
        reason on the record.
        """

        review = self.attributed_review()
        self.spool_object_path(
            self.raw_artifact_hash(review["document_ref"])).write_bytes(b"{}")
        _review, summary = self.sweep()
        refused = [item for item in summary["admitted"]
                   if item["review_id"] == review["review_id"]]
        self.assertTrue(refused, summary["admitted"])
        self.assertEqual({item["status"] for item in refused}, {"rejected"})
        self.assertIn("raw_artifact_bytes", refused[0]["reason"])
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertEqual(
            self.core.connection.execute(
                "SELECT count(*) n FROM claim_versions").fetchone()["n"], 0)

    # -- where this harness keeps its bytes -------------------------------

    def automation_principal(self):
        return self.mission["autonomy"]["automation_principal"]

    def spool_object_path(self, digest):
        for root in (self.state / "connector-spool", self.state / "spool"):
            path = root / "connector-spool" / "objects" / digest[:2] / digest
            if path.is_file():
                return path
        raise AssertionError(f"no spool root holds {digest}")

    def raw_artifact_hash(self, document_ref):
        """The bytes the connector receipt for this acquisition recorded."""

        manifest = self.manifest_for(document_ref)["manifest"]
        row = self.core.connection.execute(
            "SELECT record_json FROM connector_source_envelopes "
            "WHERE connector_invocation_ref=?",
            (manifest["connector_invocation_ref"],),
        ).fetchone()
        return json.loads(row["record_json"])["raw_response_hash"]


@_own_cases_only
class SalesNoteClaimTests(_FeedClaimHarness, reading.SalesNotesReadingTests):
    """A broker's note becomes a ``sell_side`` Claim."""

    EVIDENCE_SOURCE_TYPE = SELL_SIDE_NOTE_EVIDENCE_SOURCE_TYPE
    SOURCE_REF = "source:sales-notes"
    IMPORTANCE = "sell_side"
    STATEMENT = "The note described cautious client decision-making."
    ASPECT = "aspect:client-decisions"
    BASIS = "fixture sell-side commentary"

    def target_document_ref(self):
        return ACN_NOTE


@_own_cases_only
class CompanyWikiClaimTests(_FeedClaimHarness, reading.CompanyWikiReadingTests):
    """A page of the fund's own wiki becomes an ``internal_prior`` Claim."""

    EVIDENCE_SOURCE_TYPE = INTERNAL_WIKI_EVIDENCE_SOURCE_TYPE
    SOURCE_REF = "source:company-wiki"
    IMPORTANCE = "internal_prior"
    STATEMENT = "The page recorded how the team framed the account."
    ASPECT = "aspect:account-framing"
    BASIS = "fixture internal wiki page"


@_own_cases_only
class PriorResearchClaimTests(_FeedClaimHarness, reading.PriorResearchReadingTests):
    """This fund's own earlier work becomes an ``internal_prior`` Claim."""

    EVIDENCE_SOURCE_TYPE = INTERNAL_PRIOR_RESEARCH_EVIDENCE_SOURCE_TYPE
    SOURCE_REF = "source:prior-research"
    IMPORTANCE = "internal_prior"
    STATEMENT = "The earlier work set out how the position was framed then."
    ASPECT = "aspect:prior-view"
    BASIS = "fixture prior research"


# -- Guidepoint ------------------------------------------------------------

@_own_cases_only
class GuidepointClaimTests(_CommittedClaimChecks, reading.GuidepointReadingTests):
    """An expert excerpt becomes a Claim, ranked ``other`` until asked."""

    EVIDENCE_SOURCE_TYPE = EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE
    SOURCE_REF = "source:guidepoint"
    IMPORTANCE = "other"

    def setUp(self) -> None:
        # The packaged Guidepoint fixture is deliberately anonymised -- "Former
        # Delivery Director, Large US IT Services Firm" -- so P13i refuses to
        # attribute any of it to a company, which is the right answer for it
        # and makes it useless for this case.  One excerpt that does name the
        # company it is about, acquired through exactly the same child.
        import unittest.mock as mock

        from tests import test_guidepoint_search_lane as search_fixtures

        rows = [dict(row) for row in search_fixtures.ROWS]
        rows[0] = {
            **rows[0],
            "question": "How are Accenture's clients approving discretionary work?",
            "answer": (
                "Synthetic rehearsal text. Accenture's clients moved into shorter "
                "phased programmes rather than multi-year transformations, and "
                "approvals now sit two levels higher than a year ago."
            ),
        }
        patch = mock.patch.object(search_fixtures, "ROWS", rows)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()
        sign_document_qualitative_rule(self.core)

    def sweep(self):
        _service, view = self.view()
        return run_hermetic_sweep(
            self, state=self.state, root=self.root,
            quote_id=view["context"]["quotes"][0]["quote_id"],
            normalized_statement="The expert described how buyers were deciding.",
            metric_or_aspect="aspect:buying-behaviour",
            basis="fixture expert call excerpt",
            spool_dir=self.state / "connector-spool",
        )

    def test_the_excerpt_reaches_a_committed_claim(self) -> None:
        self.assertIn(self.SOURCE_REF, STAGEABLE_SOURCE_REFS)
        summary = self.sweep()
        bundle = self.assert_committed(self.core, summary)
        self.assert_importance(self.core, bundle["entry"]["claim_version_ref"])
        binding = self.assert_citation_is_an_exact_span(
            self.core, bundle["evidence"], self.document_ref)
        self.assertEqual(binding["source_manifest_ref"], self.manifest["id"])
        self.assertEqual(binding["source_content_hash"],
                         self.manifest["declared_content_sha256"])
        # The excerpt is one of the twenty records the search returned, and
        # that search's envelope is what the Claim's evidence names.
        envelope = json.loads(self.core.connection.execute(
            "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
            (bundle["evidence"]["source_envelope_ref"],),
        ).fetchone()["record_json"])
        self.assertEqual(envelope["operation"], "search_library")
        self.assertIn(self.document_ref, envelope["source_record_refs"])
        self.assertEqual(bundle["evidence"]["source_envelope_ref"],
                         self.manifest["source_envelope_ref"])

    def test_a_drifted_excerpt_object_cannot_be_quoted(self) -> None:
        declared = self.manifest["excerpt_object"]
        path = (self.state / "connector-spool" / "connector-spool" / "objects"
                / declared["content_hash"][:2] / declared["content_hash"])
        path.write_bytes(b"x" * declared["size_bytes"])
        with self.assertRaises(Exception) as caught:
            self.view()
        self.assertIn("excerpt", str(caught.exception).lower())


# -- the two sources that already had one, unchanged -----------------------

class UnchangedSourceKindTests(unittest.TestCase):
    """AlphaEngine and public-web keep the exact chain they had.

    P13aq adds source kinds; it must not move the two that existed.  These are
    the constants an AlphaEngine or public-web candidate's identity is built
    from, frozen so a change to the shared table fails here rather than showing
    up as a different candidate ref in a live Ledger.
    """

    def test_the_two_original_kinds_are_byte_identical(self) -> None:
        from dalton_core.research_verification import (
            PUBLIC_WEB_CORE_AUTHORITY_MODE,
            PUBLIC_WEB_SOURCE_VERIFIER_HASH,
            PUBLIC_WEB_SOURCE_VERIFIER_REF,
            TRANSCRIPT_CORE_AUTHORITY_MODE,
            TRANSCRIPT_SOURCE_VERIFIER_HASH,
            TRANSCRIPT_SOURCE_VERIFIER_REF,
        )
        from dalton_core.transcript_candidate_staging import (
            SOURCE_KIND_MODES,
            SOURCE_KINDS,
        )

        self.assertEqual(SOURCE_KINDS["alphaengine"], {
            "document_prefix": "alphaengine-doc:", "source_ref": "source:alphaengine",
            "operation": "get_document",
            "material_prefix": "source-material:transcript-core:",
            "bundle_prefix": "verification-bundle:transcript-core-source:",
            "candidate_prefix": "transcript",
            "evidence_source_type": "authenticated_transcript",
        })
        self.assertEqual(SOURCE_KINDS["public_web"], {
            "document_prefix": "public-web-document:", "source_ref": "source:public-web",
            "operation": "fetch_get",
            "material_prefix": "source-material:public-web-core:",
            "bundle_prefix": "verification-bundle:public-web-core-source:",
            "candidate_prefix": "public-web", "evidence_source_type": "public_web",
            "record_binding": "exact",
        })
        self.assertEqual(SOURCE_KIND_MODES["alphaengine"], (
            TRANSCRIPT_CORE_AUTHORITY_MODE,
            (TRANSCRIPT_SOURCE_VERIFIER_REF, TRANSCRIPT_SOURCE_VERIFIER_HASH)))
        self.assertEqual(SOURCE_KIND_MODES["public_web"], (
            PUBLIC_WEB_CORE_AUTHORITY_MODE,
            (PUBLIC_WEB_SOURCE_VERIFIER_REF, PUBLIC_WEB_SOURCE_VERIFIER_HASH)))

    def test_the_two_original_verifier_identities_are_frozen(self) -> None:
        from dalton_core.research_verification import (
            PUBLIC_WEB_SOURCE_VERIFIER_HASH,
            TRANSCRIPT_SOURCE_VERIFIER_HASH,
        )

        rules = [
            "persisted-citation-eligibility", "correction-set-lineage",
            "core-source-envelope", "core-invocation-execution",
            "core-raw-artifact", "alphaengine-document-digest-binding",
            "profile-source-type", "schema", "citation-projection", "time-order",
        ]
        self.assertEqual(TRANSCRIPT_SOURCE_VERIFIER_HASH, content_hash({
            "ref": "verifier:transcript-core-authority-source:0.1", "rules": rules}))
        self.assertEqual(PUBLIC_WEB_SOURCE_VERIFIER_HASH, content_hash({
            "ref": "verifier:public-web-core-authority-source:0.1", "rules": rules}))

    def test_an_acquired_kind_needs_its_manifest_and_spool(self) -> None:
        """A resolver without them is refused at construction, not later."""

        from dalton_core.store import DaltonStore
        from dalton_core.transcript_candidate_staging import (
            TranscriptCoreAuthorityError,
            TranscriptCoreAuthorityResolver,
        )

        with tempfile.TemporaryDirectory() as temp:
            core = DaltonStore(str(Path(temp) / "core.sqlite"))
            try:
                for kind in ACQUIRED_SOURCE_KINDS:
                    with self.assertRaises(TranscriptCoreAuthorityError):
                        TranscriptCoreAuthorityResolver(core, source_kind=kind)
                # And the two that existed still need neither.
                for kind in ("alphaengine", "public_web"):
                    TranscriptCoreAuthorityResolver(core, source_kind=kind)
            finally:
                core.close()

    def test_an_evidence_type_cannot_be_moved_to_another_source(self) -> None:
        """The label says what the original is, so it may not be relabelled."""

        from dalton_core.transcript_correction import (
            TranscriptCorrectionValidationError,
            bind_candidate_evidence_to_transcript_citation,
        )
        from tests.test_acquired_source_authority import candidate_evidence_stub

        evidence = candidate_evidence_stub(source_ref="source:company-wiki")
        with self.assertRaises(TranscriptCorrectionValidationError) as caught:
            bind_candidate_evidence_to_transcript_citation(
                evidence, {}, source_type=EXPERT_NETWORK_EVIDENCE_SOURCE_TYPE)
        self.assertIn("does not match the acquired source", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
