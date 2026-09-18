"""Writer-owned child launcher for one admitted source-neutral document run."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .document_research_inventory import CONFIG_FILENAME as DOCUMENT_CONFIG_NAME
from .lane_child_launcher import (
    LaneChildConflict, LaneChildLauncher, LaneChildRejected,
)
from .mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
)
from .store import content_hash


TICKET_PREFIX = "mission-document-research"
TICKETS_DIRNAME = "mission-document-research-runs"


class MissionDocumentResearchLauncher(LaneChildLauncher):
    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = TICKETS_DIRNAME
    CHILD_MODULE = "dalton_core.mission_document_research_cli"

    def __init__(
        self,
        *,
        staging_path: str | Path,
        planner_scheduler_db: str | Path,
        planner_model_config_path: str | Path,
        draft_model_config_path: str | Path | None = None,
        verifier_model_config_path: str | Path | None = None,
        document_config_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.staging_path = Path(staging_path).expanduser().resolve()
        self.planner_scheduler_db = Path(planner_scheduler_db).expanduser().resolve()
        self.planner_model_config_path = Path(
            planner_model_config_path
        ).expanduser().resolve()
        self.draft_model_config_path = (
            self.state_dir / DRAFT_MODEL_CONFIG_NAME
            if draft_model_config_path is None
            else Path(draft_model_config_path).expanduser().resolve()
        )
        self.verifier_model_config_path = (
            self.state_dir / VERIFIER_MODEL_CONFIG_NAME
            if verifier_model_config_path is None
            else Path(verifier_model_config_path).expanduser().resolve()
        )
        self.document_config_path = (
            self.state_dir / DOCUMENT_CONFIG_NAME
            if document_config_path is None
            else Path(document_config_path).expanduser().resolve()
        )

    @staticmethod
    def _exact(path: Path) -> dict[str, str]:
        if path.is_symlink():
            raise LaneChildRejected(f"required document runtime file is a symlink: {path}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise LaneChildRejected(
                f"required document runtime file is unavailable: {path}"
            ) from exc
        return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

    def configuration(self) -> dict[str, Any]:
        if not self.staging_path.is_file():
            raise LaneChildRejected(
                f"candidate staging authority is unavailable: {self.staging_path}"
            )
        if not self.planner_scheduler_db.is_file():
            raise LaneChildRejected(
                f"planner Scheduler authority is unavailable: {self.planner_scheduler_db}"
            )
        return {
            "staging": {"path": str(self.staging_path)},
            "planner_scheduler_db": {"path": str(self.planner_scheduler_db)},
            "planner_model_config": self._exact(self.planner_model_config_path),
            "document_config": self._exact(self.document_config_path),
            "draft_model_config": self._exact(self.draft_model_config_path),
            "verifier_model_config": self._exact(self.verifier_model_config_path),
        }

    def configuration_signature(self) -> str:
        return content_hash(self.configuration())

    def _command(self, *, ticket_dir: Path, **kwargs: Any) -> list[str]:
        configuration = kwargs["configuration"]
        return [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--staging", configuration["staging"]["path"],
            "--planner-scheduler-db", configuration["planner_scheduler_db"]["path"],
            "--planner-model-config", configuration["planner_model_config"]["path"],
            "--document-research-config", configuration["document_config"]["path"],
            "--mission-document-draft-model-config",
            configuration["draft_model_config"]["path"],
            "--mission-document-verifier-model-config",
            configuration["verifier_model_config"]["path"],
            "--admission-ref", kwargs["admission_ref"],
            "--expected-admission-hash", kwargs["admission_hash"],
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]

    @staticmethod
    def _validate_admission(admission_ref: str, admission_hash: str) -> None:
        if not isinstance(admission_ref, str) or not admission_ref.startswith(
            "mission-document-research-admission:"
        ):
            raise LaneChildRejected("admission_ref is not a mission document admission")
        if (
            not isinstance(admission_hash, str)
            or len(admission_hash) != 64
            or any(character not in "0123456789abcdef" for character in admission_hash)
        ):
            raise LaneChildRejected("admission_hash must be lowercase SHA-256")

    def start(self, *, admission_ref: str, admission_hash: str) -> dict[str, Any]:
        self._validate_admission(admission_ref, admission_hash)
        configuration = self.configuration()
        signature = {
            "admission_ref": admission_ref,
            "admission_hash": admission_hash,
            "configuration": configuration,
        }
        digest = content_hash(signature)[:24]
        ticket_ref = f"{self.TICKET_PREFIX}:{digest}"
        if self._ticket_path(ticket_ref).is_file():
            prior = self.status(ticket_ref)
            if prior["status"] == "running":
                return prior
            raise LaneChildRejected(
                "terminal document research ticket requires controlled re-entry"
            )
        return self.spawn(
            digest=digest,
            record={**signature, "configuration_hash": content_hash(configuration)},
            admission_ref=admission_ref,
            admission_hash=admission_hash,
            configuration=configuration,
        )

    def resume(
        self,
        *,
        admission_ref: str,
        admission_hash: str,
        prior_ticket_ref: str,
        authorization: str,
    ) -> dict[str, Any]:
        self._validate_admission(admission_ref, admission_hash)
        configuration = self.configuration()
        signature = {
            "admission_ref": admission_ref,
            "admission_hash": admission_hash,
            "configuration": configuration,
        }
        digest = content_hash(signature)[:24]
        expected = f"{self.TICKET_PREFIX}:{digest}"
        record = {**signature, "configuration_hash": content_hash(configuration)}
        rebound_from = None
        if prior_ticket_ref != expected:
            # The ticket identity is a digest of the admission *and* this
            # launcher's configuration, so a release that moves a config file
            # or changes its bytes renames every held admission's ticket.  Ten
            # live admissions were stuck on exactly that: nothing about the
            # work had changed, only the name of the run that had done it.
            # The admission is the thing that must not move; the ticket is
            # bookkeeping.  So rebind when the admission is identical, and
            # refuse only when it is not.
            try:
                prior = self.status(prior_ticket_ref)
            except LookupError as exc:
                raise LaneChildRejected(
                    "controlled document research re-entry changed ticket identity"
                ) from exc
            if (
                prior.get("admission_ref") != admission_ref
                or prior.get("admission_hash") != admission_hash
            ):
                raise LaneChildRejected(
                    "controlled document research re-entry changed ticket identity"
                )
            if prior.get("status") == "running":
                raise LaneChildConflict(
                    f"{self.TICKET_PREFIX} child is already running"
                )
            rebound_from = prior_ticket_ref
            prior_ticket_ref = expected
        reentry = self._controlled_reentry_plan(
            rebound_from or prior_ticket_ref, authorization, admission_ref)
        if rebound_from is not None and not self._ticket_path(
            prior_ticket_ref
        ).is_file():
            # Rebinding onto a ticket identity that has never run.  The prior
            # ticket still owns the one-shot re-entry claim for this exact
            # authorization, so claim it there -- the archive of the prior
            # summary and log stays where the prior run is -- and then start
            # the new identity as a fresh run.
            if reentry is not None:
                self.claim_controlled_reentry(rebound_from, authorization)
            ticket = self.spawn(
                digest=digest,
                record={**record, "rebound_from_ticket_ref": rebound_from},
                admission_ref=admission_ref,
                admission_hash=admission_hash,
                configuration=configuration,
            )
            return {**ticket, "rebound_from_ticket_ref": rebound_from}
        prior = self.status(prior_ticket_ref)
        if (
            prior.get("status") == "running"
            or prior.get("admission_ref") != admission_ref
            or prior.get("admission_hash") != admission_hash
            or prior.get("configuration") != configuration
            or prior.get("configuration_hash") != content_hash(configuration)
        ):
            raise LaneChildRejected(
                "controlled document research re-entry lost its exact prior ticket"
            )
        ticket = self.spawn(
            digest=digest,
            record=record if rebound_from is None else {
                **record, "rebound_from_ticket_ref": rebound_from},
            _controlled_reentry=(
                None if reentry is None else (prior_ticket_ref, authorization)),
            admission_ref=admission_ref,
            admission_hash=admission_hash,
            configuration=configuration,
        )
        if rebound_from is None:
            return ticket
        return {**ticket, "rebound_from_ticket_ref": rebound_from}

    def controlled_reentry_consumed(self, ticket_ref: str, authorization: str) -> bool:
        """Whether this authorization's claim ever actually started a child.

        ``controlled_reentry_claimed`` answers "was the marker taken", which is
        the right question for the marker's own uniqueness and the wrong one
        for "has this admission had its automatic attempt".
        """

        from .lane_reentry_claim import claim_consumed

        return claim_consumed(self, ticket_ref, authorization)

    def _controlled_reentry_plan(self, claim_ticket_ref: str, authorization: str,
                                 admission_ref: str) -> str | None:
        """Decide what the one-shot marker for this authorization still allows.

        ``"claim"`` -- nothing has been claimed; take the marker as usual.
        ``None`` -- the marker exists but nothing ever ran under it (or the
        owner has granted one more), so complete that attempt instead of
        re-claiming a name that is already taken.  Otherwise the attempt has
        really happened and this refuses, exactly as it always did.
        """

        from .lane_reentry_claim import (
            claim_consumed, consume_grant, marker, read_claim,
            record_systemic_completion,
        )

        if read_claim(self, claim_ticket_ref, authorization) is None:
            return "claim"
        if not claim_consumed(self, claim_ticket_ref, authorization):
            # Claimed by an attempt that was refused before any child existed,
            # or by one that started and died on a systemic condition.  Either
            # way it bought nothing and this admission still has its one
            # attempt -- but the systemic completion is itself one-shot, so
            # write it down before spending it.
            record_systemic_completion(self, claim_ticket_ref, authorization)
            return None
        if consume_grant(self, admission_ref, marker(authorization)) is None:
            raise LaneChildRejected("controlled reentry was already attempted")
        return None

    def authorize_controlled_reentry(self, admission_ref: str, *, actor_ref: str,
                                     granted_at: str) -> dict[str, Any]:
        """Record one owner grant of one further controlled re-entry."""

        from .lane_reentry_claim import write_grant

        if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
            raise LaneChildRejected("a controlled re-entry grant needs a human actor")
        if not isinstance(admission_ref, str) or not admission_ref.startswith(
            "mission-document-research-admission:"
        ):
            raise LaneChildRejected(
                "admission_ref is not a mission document research admission")
        try:
            return write_grant(self, admission_ref, actor_ref=actor_ref,
                               granted_at=granted_at)
        except ValueError as exc:
            raise LaneChildRejected(str(exc)) from exc

    def status(self, ticket_ref: str) -> dict[str, Any]:
        ticket = super().status(ticket_ref)
        summary = ticket.get("summary")
        if summary is None:
            return ticket
        required = {
            "schema_version", "created_at", "admission_ref", "admission_hash",
            "status", "outcomes", "error", "content_hash",
        }
        body = dict(summary)
        asserted = body.pop("content_hash", None)
        if (
            set(summary) != required
            or asserted != content_hash(body)
            or summary.get("admission_ref") != ticket.get("admission_ref")
            or summary.get("admission_hash") != ticket.get("admission_hash")
        ):
            raise LaneChildRejected("mission document child summary authority drifted")
        return ticket


__all__ = ["MissionDocumentResearchLauncher", "TICKET_PREFIX", "TICKETS_DIRNAME"]
