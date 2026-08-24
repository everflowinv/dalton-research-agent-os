"""Research-review planes embedded in the owner-only Dalton Cockpit.

This module has no HTTP server or deployment entry point.  The Cockpit owns
the shared Tailscale identity, session, and CSRF shell while this module keeps
research candidate promotion and transcript correction admission on their
separate writer principals.  It never receives the Core database path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .alphaengine_document_acquisition import (
    validate_alphaengine_document_acquisition_manifest,
)
from .governance_cli import ephemeral_call
from .research_review import HumanReviewAuthority
from .research_trajectory import build_research_trajectory_projection
from .store import content_hash
from .writer_client import WriterClient
from .writer_server import load_principals


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKET_FILENAME = "review-packet.json"
_MANIFEST_FILENAME = "source-manifest.json"
_MAX_REVIEW_FILE_BYTES = 2_000_000


class ResearchReviewControlError(RuntimeError):
    pass


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchReviewControlError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _string(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ResearchReviewControlError(f"{name} must be lowercase SHA-256")
    return value


def _absolute_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ResearchReviewControlError(f"{name} must be an absolute path")
    return Path(value)


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ResearchReviewControlError(f"{name} must be 1..{maximum}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _subject_for_login(login: str) -> str:
    digest = hashlib.sha256(login.encode("utf-8")).hexdigest()[:32]
    return f"human:tailscale-{digest}"


@dataclass(frozen=True, slots=True)
class ResearchReviewControlConfig:
    """Review-only paths nested under the single Cockpit config."""

    candidate_staging_path: Path
    transcript_review_directory: Path
    reconcile_interval_seconds: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResearchReviewControlConfig":
        expected = {
            "candidate_staging_path", "transcript_review_directory",
            "reconcile_interval_seconds",
        }
        if set(raw) != expected:
            raise ResearchReviewControlError(
                "embedded research review config has an invalid closed shape"
            )
        return cls(
            candidate_staging_path=_absolute_path(
                raw["candidate_staging_path"], "candidate_staging_path"
            ),
            transcript_review_directory=_absolute_path(
                raw["transcript_review_directory"],
                "transcript_review_directory",
            ),
            reconcile_interval_seconds=_positive_int(
                raw["reconcile_interval_seconds"],
                "reconcile_interval_seconds",
                86400,
            ),
        )


GovernanceCall = Callable[..., Any]


class ResearchReviewControlPlane:
    """Candidate and transcript review consumers for the shared Cockpit."""

    def __init__(
        self,
        config: ResearchReviewControlConfig,
        *,
        writer_socket: Path,
        token_config: Path,
        authority: HumanReviewAuthority | None = None,
        writer: WriterClient | None = None,
        governance_call: GovernanceCall | None = None,
    ) -> None:
        self.config = config
        self.writer_socket = Path(writer_socket)
        self.token_config = Path(token_config)
        self.authority = authority or HumanReviewAuthority(
            config.candidate_staging_path
        )
        if writer is None:
            principal = load_principals(self.token_config).get(
                "research-review-control"
            )
            if principal is None:
                raise ResearchReviewControlError(
                    "research-review-control principal is unavailable"
                )
            writer = WriterClient(str(self.writer_socket), principal.token, timeout=30)
        self.writer = writer
        self._governance_call = governance_call or ephemeral_call

    def close(self) -> None:
        self.authority.close()

    def view(self, login: str) -> dict[str, Any]:
        reviewer = _subject_for_login(login)
        items = []
        for item in self.authority.list_candidates(limit=200):
            claim = item["claim"]
            evidence = item["evidence"]
            decision = item["decision"]
            items.append({
                "candidate_claim_ref": claim["id"],
                "candidate_claim_hash": claim["content_hash"],
                "subject_ref": claim["subject_ref"],
                "metric_or_aspect": claim["metric_or_aspect"],
                "period": claim["period"],
                "basis": claim["basis"],
                "normalized_statement": claim["normalized_statement"],
                "value": claim["value"],
                "unit": claim["unit"],
                "currency": claim["currency"],
                "scale": claim["scale"],
                "source_type": evidence["source_type"],
                "source_ref": evidence["source_ref"],
                "source_envelope_ref": evidence["source_envelope_ref"],
                "artifact_refs": evidence["artifact_refs"],
                "decision": decision,
                "commit_state": item.get("commit_state"),
            })
        return {"as_of": _now(), "reviewer_ref": reviewer, "items": items}

    def _candidate(
        self, candidate_claim_ref: str, candidate_claim_hash: str
    ) -> dict[str, Any]:
        matches = [
            item for item in self.authority.list_candidates(limit=500)
            if item["claim"]["id"] == candidate_claim_ref
        ]
        if (
            len(matches) != 1
            or matches[0]["claim"]["content_hash"] != candidate_claim_hash
        ):
            raise ResearchReviewControlError("candidate is unavailable or changed")
        return matches[0]

    def record(self, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
        expected = {
            "request_id", "candidate_claim_ref", "candidate_claim_hash",
            "verdict", "rationale", "findings", "proposed_revisions",
        }
        if set(value) != expected:
            raise ResearchReviewControlError(
                "request body has an invalid closed shape"
            )
        request_id = _string(value["request_id"], "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ResearchReviewControlError("request_id has an invalid shape")
        candidate_claim_ref = _string(
            value["candidate_claim_ref"], "candidate_claim_ref"
        )
        candidate_claim_hash = _hash(
            value["candidate_claim_hash"], "candidate_claim_hash"
        )
        verdict = value["verdict"]
        if verdict not in {"accept", "revise", "reject"}:
            raise ResearchReviewControlError("verdict is invalid")
        rationale = _string(value["rationale"], "rationale")
        findings = value["findings"]
        if (
            not isinstance(findings, list)
            or any(not isinstance(item, str) or not item for item in findings)
        ):
            raise ResearchReviewControlError(
                "findings must be an array of strings"
            )
        revisions = value["proposed_revisions"]
        if revisions is not None and not isinstance(revisions, Mapping):
            raise ResearchReviewControlError(
                "proposed_revisions must be an object or null"
            )
        candidate = self._candidate(candidate_claim_ref, candidate_claim_hash)
        claim = candidate["claim"]
        reviewer_ref = _subject_for_login(login)
        digest = content_hash({
            "request_id": request_id,
            "candidate_claim_ref": candidate_claim_ref,
            "candidate_claim_hash": candidate_claim_hash,
            "reviewer_ref": reviewer_ref,
        })[:32]
        result = self.authority.decide(
            candidate_claim_ref=candidate_claim_ref,
            candidate_claim_hash=candidate_claim_hash,
            verdict=verdict,
            reviewed_semantics={
                field: claim[field]
                for field in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            rationale=rationale,
            findings=findings,
            reviewer_ref=reviewer_ref,
            source_event_ref=f"research-review:{digest}",
            idempotency_key=f"research-review:{digest}",
            created_at=_now(),
            proposed_revisions=revisions,
        )
        if verdict == "accept":
            reconciled = self.reconcile(decision_ref=result["decision_ref"])
            result["commit_state"] = (
                "committed" if reconciled["committed"] else "pending"
            )
        return result

    def reconcile(
        self, *, decision_ref: str | None = None, limit: int = 20
    ) -> dict[str, int]:
        pending = self.authority.pending_commits(limit=limit)
        if decision_ref is not None:
            pending = [
                item for item in pending
                if item["decision"]["id"] == decision_ref
            ]
        committed = failed = 0
        for bundle in pending:
            decision = bundle["decision"]
            try:
                result = self.writer.commit_reviewed_candidate(
                    **bundle,
                    idempotency_key=f"reviewed-ledger:{decision['id']}",
                )
                self.authority.record_commit_result(
                    decision["id"], created_at=_now(), ledger_result=result
                )
                committed += 1
            except Exception as exc:
                code = getattr(exc, "code", None)
                error_code = (
                    code if isinstance(code, str) and code else "writer_rejected"
                )
                self.authority.record_commit_result(
                    decision["id"], created_at=_now(), error_code=error_code
                )
                failed += 1
        return {"checked": len(pending), "committed": committed, "failed": failed}

    @staticmethod
    def _secure_json(path: Path, name: str) -> dict[str, Any]:
        try:
            resolved = path.resolve(strict=True)
            info = path.lstat()
            if path.is_symlink() or info.st_size > _MAX_REVIEW_FILE_BYTES:
                raise ResearchReviewControlError(f"{name} is unsafe")
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except ResearchReviewControlError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchReviewControlError(f"{name} is unavailable") from exc
        if not isinstance(value, dict):
            raise ResearchReviewControlError(f"{name} must be an object")
        return value

    def _review_files(self) -> list[tuple[Path, Path]]:
        root = self.config.transcript_review_directory.resolve()
        if not root.is_dir():
            return []
        packet_paths = sorted(root.glob(f"*/{_PACKET_FILENAME}"))
        direct = root / _PACKET_FILENAME
        if direct.is_file():
            packet_paths.insert(0, direct)
        pairs = []
        for packet_path in packet_paths:
            manifest_path = packet_path.with_name(_MANIFEST_FILENAME)
            if not manifest_path.is_file():
                raise ResearchReviewControlError(
                    "transcript review packet has no sibling source manifest"
                )
            if (
                not packet_path.resolve().is_relative_to(root)
                or not manifest_path.resolve().is_relative_to(root)
            ):
                raise ResearchReviewControlError(
                    "transcript review packet escaped its configured directory"
                )
            pairs.append((packet_path, manifest_path))
        return pairs

    @staticmethod
    def _validate_packet(
        packet: Mapping[str, Any], manifest_value: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        wire = dict(packet)
        asserted_hash = _hash(wire.pop("content_hash", None), "packet.content_hash")
        if asserted_hash != content_hash(wire):
            raise ResearchReviewControlError("transcript review packet hash drifted")
        packet_wire = {**wire, "content_hash": asserted_hash}
        required = {
            "schema_version", "id", "created_at", "source",
            "proposed_correction_set", "candidate_claim", "research_targets",
            "review_contract", "formal_authority_counts",
            "production_activated", "forbidden_inputs", "content_hash",
        }
        if set(packet_wire) != required or packet_wire["schema_version"] != "0.1":
            raise ResearchReviewControlError(
                "transcript review packet has an invalid closed shape"
            )
        packet_ref = _string(packet_wire["id"], "packet.id")
        if not packet_ref.startswith("transcript-review-packet:"):
            raise ResearchReviewControlError("transcript review packet id is invalid")
        if packet_wire["production_activated"] is not False:
            raise ResearchReviewControlError("review packet activated production")
        counts = packet_wire["formal_authority_counts"]
        if counts != {
            "claim_versions": 0, "evidence_versions": 0, "thesis_versions": 0
        }:
            raise ResearchReviewControlError(
                "review packet already contains formal authority"
            )
        if packet_wire["forbidden_inputs"] != [
            "metadata.main_point", "metadata.question_answer"
        ]:
            raise ResearchReviewControlError(
                "review packet summary-field policy drifted"
            )
        manifest = validate_alphaengine_document_acquisition_manifest(
            manifest_value
        )
        source = packet_wire["source"]
        if not isinstance(source, Mapping) or {
            "document_ref", "manifest_ref", "manifest_hash", "content_chars",
            "content_sha256", "page_count", "physical_calls", "title",
            "lineage_path", "summary_fields_allowed",
        } != set(source):
            raise ResearchReviewControlError("review packet source is invalid")
        if (
            source["manifest_ref"] != manifest["id"]
            or source["manifest_hash"] != manifest["content_hash"]
            or source["document_ref"] != manifest["document_ref"]
            or source["content_sha256"]
            != manifest["assembled_object"]["content_hash"]
            or source["content_chars"] != manifest["content_chars"]
            or source["page_count"] != len(manifest["pages"])
            or source["physical_calls"] != manifest["physical_calls"]
            or source["summary_fields_allowed"] is not False
        ):
            raise ResearchReviewControlError(
                "review packet and source manifest lineage disagree"
            )
        correction_set = packet_wire["proposed_correction_set"]
        if not isinstance(correction_set, Mapping) or set(correction_set) != {
            "correction_set_ref", "review_scope", "corrections", "actor_ref",
            "human_review_required", "unresolved_overlap_with_formal_citation",
        }:
            raise ResearchReviewControlError(
                "proposed correction set has an invalid closed shape"
            )
        if (
            correction_set["actor_ref"] is not None
            or correction_set["human_review_required"] is not True
            or correction_set["review_scope"] not in {
                "targeted_flags", "full_document"
            }
        ):
            raise ResearchReviewControlError(
                "proposed correction set bypassed human review"
            )
        corrections = correction_set["corrections"]
        if not isinstance(corrections, list) or not corrections:
            raise ResearchReviewControlError("review packet has no corrections")
        previous_end = 0
        for item in corrections:
            if not isinstance(item, Mapping) or set(item) != {
                "source_start", "source_end", "source_sha256", "source_text",
                "correction_kind", "disposition", "replacement_text",
                "rationale", "evidence_bindings",
            }:
                raise ResearchReviewControlError(
                    "review packet correction has an invalid closed shape"
                )
            start = item["source_start"]
            end = item["source_end"]
            text = item["source_text"]
            if (
                isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or start < previous_end or end <= start
                or not isinstance(text, str) or len(text) != end - start
                or hashlib.sha256(text.encode("utf-8")).hexdigest()
                != item["source_sha256"]
            ):
                raise ResearchReviewControlError(
                    "review packet correction span drifted"
                )
            previous_end = end
        claim = packet_wire["candidate_claim"]
        if not isinstance(claim, Mapping):
            raise ResearchReviewControlError("candidate claim is invalid")
        citation = claim.get("citation")
        if not isinstance(citation, Mapping) or set(citation) != {
            "source_start", "source_end", "source_sha256", "raw_span"
        }:
            raise ResearchReviewControlError("candidate citation is invalid")
        if (
            hashlib.sha256(citation["raw_span"].encode("utf-8")).hexdigest()
            != citation["source_sha256"]
        ):
            raise ResearchReviewControlError("candidate citation span drifted")
        overlap = sum(
            citation["source_start"] < item["source_end"]
            and citation["source_end"] > item["source_start"]
            and item["disposition"] == "unresolved"
            for item in corrections
        )
        if overlap != correction_set["unresolved_overlap_with_formal_citation"]:
            raise ResearchReviewControlError(
                "review packet correction overlap drifted"
            )
        contract = packet_wire["review_contract"]
        if (
            not isinstance(contract, Mapping)
            or contract.get("authorization") != "explicit_human_review"
            or contract.get("source") != "tailscale_review"
            or contract.get("allowed_verdicts") != ["accept", "revise", "reject"]
        ):
            raise ResearchReviewControlError("review contract drifted")
        return packet_wire, manifest

    def _packets(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        packets = []
        seen: set[str] = set()
        for packet_path, manifest_path in self._review_files():
            packet, manifest = self._validate_packet(
                self._secure_json(packet_path, "transcript review packet"),
                self._secure_json(manifest_path, "source manifest"),
            )
            if packet["id"] in seen:
                raise ResearchReviewControlError(
                    "duplicate transcript review packet id"
                )
            seen.add(packet["id"])
            packets.append((packet, manifest))
        return packets

    def _packet(
        self, packet_ref: str, packet_hash: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        matches = [
            pair for pair in self._packets() if pair[0]["id"] == packet_ref
        ]
        if len(matches) != 1 or matches[0][0]["content_hash"] != packet_hash:
            raise ResearchReviewControlError(
                "transcript review packet is unavailable or changed"
            )
        return matches[0]

    def _transcript_state(
        self, packet: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        correction = packet["proposed_correction_set"]
        citation = packet["candidate_claim"]["citation"]
        state = self.writer.transcript_correction_review_state(
            source_manifest=manifest,
            correction_set_ref=correction["correction_set_ref"],
            source_start=citation["source_start"],
            source_end=citation["source_end"],
        )
        if not isinstance(state, Mapping) or set(state) != {
            "status", "correction_set", "citation_binding", "claim_eligible",
        }:
            raise ResearchReviewControlError(
                "transcript review writer returned an invalid state"
            )
        published = state.get("correction_set")
        if isinstance(published, Mapping):
            _string(published.get("id"), "published correction set id")
            _hash(
                published.get("content_hash"),
                "published correction set content_hash",
            )
        expected_corrections = [
            {key: item[key] for key in item if key != "source_text"}
            for item in correction["corrections"]
        ]
        if published is not None and (
            not isinstance(published, Mapping)
            or published.get("correction_set_ref")
            != correction["correction_set_ref"]
            or published.get("source_manifest_ref") != manifest["id"]
            or published.get("source_manifest_hash")
            != manifest["content_hash"]
            or published.get("source_content_hash")
            != manifest["assembled_object"]["content_hash"]
            or published.get("review_scope") != correction["review_scope"]
            or published.get("corrections") != expected_corrections
        ):
            raise ResearchReviewControlError(
                "published correction state disagrees with the review packet"
            )
        binding = state.get("citation_binding")
        if isinstance(binding, Mapping):
            _string(binding.get("id"), "citation binding id")
            _hash(binding.get("content_hash"), "citation binding content_hash")
        if binding is not None and (
            published is None
            or not isinstance(binding, Mapping)
            or binding.get("correction_set_version_ref")
            != published.get("id")
            or binding.get("correction_set_version_hash")
            != published.get("content_hash")
            or binding.get("source_manifest_ref") != manifest["id"]
            or binding.get("source_manifest_hash")
            != manifest["content_hash"]
            or binding.get("source_content_hash")
            != manifest["assembled_object"]["content_hash"]
            or binding.get("source_start") != citation["source_start"]
            or binding.get("source_end") != citation["source_end"]
            or binding.get("claim_eligible") is not state.get("claim_eligible")
        ):
            raise ResearchReviewControlError(
                "citation state disagrees with the review packet"
            )
        status = state.get("status")
        eligible = state.get("claim_eligible")
        valid_state = (
            status == "pending_human_review"
            and published is None
            and binding is None
            and eligible is False
        ) or (
            status == "correction_published"
            and published is not None
            and binding is None
            and eligible is False
        ) or (
            status in {"claim_eligible", "citation_blocked"}
            and published is not None
            and binding is not None
            and eligible is (status == "claim_eligible")
        )
        if not valid_state:
            raise ResearchReviewControlError(
                "transcript review writer returned a contradictory state"
            )
        return dict(state)

    def transcript_view(self, login: str) -> dict[str, Any]:
        items = []
        for packet, manifest in self._packets():
            correction = packet["proposed_correction_set"]
            state = self._transcript_state(packet, manifest)
            items.append({
                "packet_ref": packet["id"],
                "packet_hash": packet["content_hash"],
                "source": packet["source"],
                "candidate_claim": packet["candidate_claim"],
                "proposed_correction_set": correction,
                "research_targets": packet["research_targets"],
                "review_contract": packet["review_contract"],
                "state": state,
            })
        return {
            "as_of": _now(),
            "reviewer_ref": _subject_for_login(login),
            "items": items,
        }

    def _trajectory_candidate(
        self, packet: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        candidate_ref = packet["candidate_claim"]["candidate_claim_ref"]
        matches = []
        for item in self.authority.list_candidates(limit=500):
            claim = item["claim"]
            if (
                claim.get("candidate_claim_ref") == candidate_ref
                or claim.get("id") == candidate_ref
            ):
                matches.append(item)
        if not matches:
            return None
        parent_refs = {
            item["claim"].get("prior_version_ref")
            for item in matches
            if item["claim"].get("prior_version_ref") is not None
        }
        leaves = [
            item for item in matches if item["claim"]["id"] not in parent_refs
        ]
        if len(leaves) != 1:
            raise ResearchReviewControlError(
                "trajectory candidate lineage is ambiguous"
            )
        current = dict(leaves[0])
        decision = current.get("decision")
        current["commit_event"] = (
            None
            if decision is None
            else self.authority.commit_event(decision["id"])
        )
        event = current["commit_event"]
        if event is not None and event.get("state") != current.get("commit_state"):
            raise ResearchReviewControlError(
                "trajectory candidate commit state drifted"
            )
        return current

    def trajectory_view(self, login: str) -> dict[str, Any]:
        items = []
        for packet, manifest in self._packets():
            state = self._transcript_state(packet, manifest)
            items.append(build_research_trajectory_projection(
                packet=packet,
                manifest=manifest,
                transcript_state=state,
                candidate=self._trajectory_candidate(packet),
            ))
        return {
            "as_of": _now(),
            "viewer_ref": _subject_for_login(login),
            "projection_only": True,
            "items": items,
        }

    def record_transcript(
        self, login: str, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        expected = {
            "request_id", "packet_ref", "packet_hash", "action",
        }
        if set(value) != expected:
            raise ResearchReviewControlError(
                "transcript review request has an invalid closed shape"
            )
        request_id = _string(value["request_id"], "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ResearchReviewControlError("request_id has an invalid shape")
        packet_ref = _string(value["packet_ref"], "packet_ref")
        packet_hash = _hash(value["packet_hash"], "packet_hash")
        if value["action"] != "publish_and_bind":
            raise ResearchReviewControlError("transcript review action is invalid")
        packet, manifest = self._packet(packet_ref, packet_hash)
        correction = packet["proposed_correction_set"]
        citation = packet["candidate_claim"]["citation"]
        actor_ref = _subject_for_login(login)
        corrections = [
            {key: item[key] for key in item if key != "source_text"}
            for item in correction["corrections"]
        ]
        try:
            correction_set = self._governance_call(
                self.token_config,
                self.writer_socket,
                actor_ref=actor_ref,
                operation="publish_transcript_correction_set",
                params={
                    "correction_set_ref": correction["correction_set_ref"],
                    "source_manifest": manifest,
                    "review_scope": correction["review_scope"],
                    "corrections": corrections,
                    "prior_version_ref": None,
                },
            )
            binding = self._governance_call(
                self.token_config,
                self.writer_socket,
                actor_ref=actor_ref,
                operation="bind_transcript_claim_citation",
                params={
                    "correction_set_version_ref": correction_set["id"],
                    "correction_set_version_hash": correction_set["content_hash"],
                    "source_manifest": manifest,
                    "source_start": citation["source_start"],
                    "source_end": citation["source_end"],
                },
            )
        except Exception as exc:
            raise ResearchReviewControlError(
                "transcript review writer rejected the admission"
            ) from exc
        if not binding.get("claim_eligible"):
            raise ResearchReviewControlError(
                "reviewed transcript citation remains ineligible"
            )
        return {
            "status": "claim_eligible",
            "packet_ref": packet_ref,
            "packet_hash": packet_hash,
            "reviewer_ref": actor_ref,
            "correction_set_ref": correction_set["id"],
            "correction_set_hash": correction_set["content_hash"],
            "citation_binding_ref": binding["id"],
            "citation_binding_hash": binding["content_hash"],
            "claim_eligible": True,
        }


__all__ = [
    "ResearchReviewControlConfig", "ResearchReviewControlError",
    "ResearchReviewControlPlane", "_subject_for_login",
]
