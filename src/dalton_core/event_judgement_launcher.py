"""P14a: launch the judgement child, one at a time.

Named by the selected mission-scoped event group and configuration. Completed
tickets are adopted after restart so their result can settle into the durable
failure ledger before a retry is considered.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected

TICKET_PREFIX = "event-judgement-run"


class EventJudgementLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.event_judgement_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "event-judgement-runs"
    CHILD_MODULE = "dalton_core.event_judgement_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        judge_model_config: str | Path,
        verifier_model_config: str | Path,
        policy_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.judge_model_config = Path(judge_model_config).expanduser().resolve()
        self.verifier_model_config = Path(verifier_model_config).expanduser().resolve()
        self.policy_path = None if policy_path is None else Path(policy_path).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db)
        self._adopted_finished: set[str] = set()

    @property
    def configured(self) -> bool:
        """Both configurations, or nothing.

        A judge with no independent verifier is the one shape this lane must
        never take: it would produce decisions that look verified in the
        record because the field exists, and are not.
        """

        return self.judge_model_config.is_file() and self.verifier_model_config.is_file()

    def configuration_signature(self) -> str:
        """Changing a Cockpit route makes an unjudged batch retryable."""
        from .cockpit_model import verifier_provider_contract_fingerprint

        digest = hashlib.sha256(b"event-configuration:1")
        for path in (self.judge_model_config, self.verifier_model_config, self.policy_path):
            if path is None:
                digest.update(b"\0absent")
            else:
                digest.update(b"\0file:")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0provider-contracts:")
        digest.update(verifier_provider_contract_fingerprint(
            "event_judgement_verifier", "thesis_reflection_verifier"
        ).encode("ascii"))
        return digest.hexdigest()

    def _command(self, *, ticket_dir: Path, company_ref: str,
                 event_refs: tuple[str, ...], event_group_hash: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--judge-model-config", str(self.judge_model_config),
            "--verifier-model-config", str(self.verifier_model_config),
            "--company-ref", company_ref,
            "--event-group-hash", event_group_hash,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        for event_ref in event_refs:
            command += ["--event-ref", event_ref]
        if self.policy_path is not None:
            command += ["--tracking-policy", str(self.policy_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler", str(self.scheduler_db)]
        return command

    def start(self, *, batch_ref: str, company_ref: str,
              event_refs: tuple[str, ...], event_group_hash: str,
              group_key: str) -> dict[str, Any]:
        if not isinstance(batch_ref, str) or not batch_ref.strip():
            raise LaneChildRejected("a judgement run needs a batch ref")
        if not self.configured:
            raise LaneChildRejected(
                "the judgement lane needs a judge and a verifier configuration"
            )
        if (not all(isinstance(value, str) and value.strip()
                    for value in (company_ref, event_group_hash, group_key))
                or not event_refs
                or any(not isinstance(ref, str) or not ref for ref in event_refs)):
            raise LaneChildRejected("a judgement run needs a company and event group")
        if (len(event_group_hash) != 64
                or any(char not in "0123456789abcdef" for char in event_group_hash)):
            raise LaneChildRejected("event_group_hash must be a sha256 digest")
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{batch_ref.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        ticket_id = f"{self.TICKET_PREFIX}:{digest}"
        # A writer may restart after the child finished but before the next
        # tick settled it into the durable failure ledger. Adopt that exact
        # ticket instead of truncating its log and paying for the group again.
        if (self._current is None and ticket_id not in self._adopted_finished
                and self._ticket_path(ticket_id).is_file()):
            adopted = self.status(ticket_id)
            if adopted.get("status") != "running":
                self._adopted_finished.add(ticket_id)
            return adopted
        return self.spawn(
            digest=digest,
            record={"batch_ref": batch_ref.strip(), "company_ref": company_ref,
                    "event_refs": list(event_refs), "event_group_hash": event_group_hash,
                    "group_key": group_key},
            company_ref=company_ref, event_refs=event_refs,
            event_group_hash=event_group_hash,
        )


__all__ = ["TICKET_PREFIX", "EventJudgementLauncher"]
