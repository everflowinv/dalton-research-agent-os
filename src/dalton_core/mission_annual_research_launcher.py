"""Writer-owned child launcher for an admitted annual-research workflow."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .store import content_hash


TICKET_PREFIX = "mission-annual-research"
TICKETS_DIRNAME = "mission-annual-research-runs"


class MissionAnnualResearchLauncher(LaneChildLauncher):
    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = TICKETS_DIRNAME
    CHILD_MODULE = "dalton_core.mission_annual_research_cli"

    def __init__(
        self,
        *,
        staging_path: str | Path,
        web_fetch_governance_path: str | Path,
        spool_dir: str | Path,
        draft_model_config_path: str | Path | None = None,
        verifier_model_config_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.staging_path = Path(staging_path).expanduser().resolve()
        self.web_fetch_governance_path = Path(
            web_fetch_governance_path
        ).expanduser().resolve()
        self.spool_dir = Path(spool_dir).expanduser().resolve()
        self.draft_model_config_path = (
            None if draft_model_config_path is None
            else Path(draft_model_config_path).expanduser().resolve()
        )
        self.verifier_model_config_path = (
            None if verifier_model_config_path is None
            else Path(verifier_model_config_path).expanduser().resolve()
        )

    def configuration(self) -> dict[str, Any]:
        def exact(path: Path) -> dict[str, str]:
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise LaneChildRejected(f"required annual runtime file is unavailable: {path}") from exc
            return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

        from .annual_report_runtime import DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME

        draft = self.draft_model_config_path or self.state_dir / DRAFT_MODEL_CONFIG_NAME
        verifier = self.verifier_model_config_path or self.state_dir / VERIFIER_MODEL_CONFIG_NAME
        if not self.staging_path.is_file():
            raise LaneChildRejected(
                f"candidate staging authority is unavailable: {self.staging_path}"
            )
        return {
            # Candidate staging is the run's mutable output authority.  Its
            # location is configuration; its changing bytes cannot be part of
            # the ticket identity or a successful replay would mint a new run.
            "staging": {"path": str(self.staging_path)},
            "web_fetch_governance": exact(self.web_fetch_governance_path),
            "draft_model_config": exact(draft),
            "verifier_model_config": exact(verifier),
            "spool_dir": str(self.spool_dir),
        }

    def configuration_signature(self) -> str:
        return content_hash(self.configuration())

    def _command(self, *, ticket_dir: Path, **kwargs: Any) -> list[str]:
        admission_ref = kwargs["admission_ref"]
        admission_hash = kwargs["admission_hash"]
        configuration = kwargs["configuration"]
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--staging", configuration["staging"]["path"],
            "--web-fetch-governance", configuration["web_fetch_governance"]["path"],
            "--spool-dir", configuration["spool_dir"],
            "--annual-draft-model-config", configuration["draft_model_config"]["path"],
            "--annual-verifier-model-config", configuration["verifier_model_config"]["path"],
            "--admission-ref", admission_ref,
            "--expected-admission-hash", admission_hash,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        return command

    def start(self, *, admission_ref: str, admission_hash: str) -> dict[str, Any]:
        if not isinstance(admission_ref, str) or not admission_ref.startswith(
            "mission-annual-research-admission:"
        ):
            raise LaneChildRejected("admission_ref is not a mission annual admission")
        if (
            not isinstance(admission_hash, str)
            or len(admission_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in admission_hash)
        ):
            raise LaneChildRejected("admission_hash must be lowercase SHA-256")
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
                "terminal annual ticket requires controlled re-entry"
            )
        return self.spawn(
            digest=digest,
            record={**signature, "configuration_hash": content_hash(configuration)},
            admission_ref=admission_ref,
            admission_hash=admission_hash,
            configuration=configuration,
        )

    def resume(
        self, *, admission_ref: str, admission_hash: str,
        prior_ticket_ref: str, authorization: str,
    ) -> dict[str, Any]:
        """Re-enter the same ticket only under a caller's exact authority.

        ``LaneChildLauncher`` archives the prior summary and admits only one
        controlled re-entry marker for this authorization.  A changed runtime
        configuration produces a different ticket and is therefore refused.
        """

        configuration = self.configuration()
        signature = {
            "admission_ref": admission_ref,
            "admission_hash": admission_hash,
            "configuration": configuration,
        }
        digest = content_hash(signature)[:24]
        expected = f"{self.TICKET_PREFIX}:{digest}"
        if prior_ticket_ref != expected:
            raise LaneChildRejected(
                "controlled annual re-entry changed ticket identity"
            )
        prior = self.status(prior_ticket_ref)
        if (
            prior.get("status") == "running"
            or prior.get("admission_ref") != admission_ref
            or prior.get("admission_hash") != admission_hash
            or prior.get("configuration") != configuration
            or prior.get("configuration_hash") != content_hash(configuration)
        ):
            raise LaneChildRejected(
                "controlled annual re-entry lost its exact prior ticket"
            )
        return self.spawn(
            digest=digest,
            record={**signature, "configuration_hash": content_hash(configuration)},
            _controlled_reentry=(prior_ticket_ref, authorization),
            admission_ref=admission_ref,
            admission_hash=admission_hash,
            configuration=configuration,
        )

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
            raise LaneChildRejected("mission annual child summary authority drifted")
        return ticket


__all__ = [
    "MissionAnnualResearchLauncher", "TICKET_PREFIX", "TICKETS_DIRNAME",
]
