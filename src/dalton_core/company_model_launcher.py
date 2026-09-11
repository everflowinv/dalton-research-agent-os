"""P13am: launch the company-model-specification child, one at a time.

The child decides how one company should be modelled; this decides when one
runs and keeps the ticket.

The run is named by the company *and the disclosure it is about to reason over*
-- so asking twice about a company that has filed nothing new is the same
ticket rather than a second child paying for the same judgement. The child
would replay it for free anyway, but a ticket per tick would still leave a
directory of runs that all say "unchanged".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .store import content_hash

TICKET_PREFIX = "company-model-spec-run"


class CompanyModelSpecLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.company_model_cli`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "company-model-spec-runs"
    CHILD_MODULE = "dalton_core.company_model_cli"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = (
            None if model_config_path is None
            else Path(model_config_path).expanduser().resolve()
        )
        self.scheduler_db = (
            None if scheduler_db is None
            else Path(scheduler_db).expanduser().resolve()
        )

    @property
    def configured(self) -> bool:
        """Whether a model is wired. Without one the child answers 'gated'."""

        return self.model_config_path is not None

    def repair_policy_hash(self) -> str:
        from .company_model_cli import structured_output_repair_config
        config = self._validated_model_config()
        return content_hash(structured_output_repair_config(config))

    def numeric_context_policy(self) -> dict[str, int]:
        from .company_model_state import model_spec_numeric_context_config

        return model_spec_numeric_context_config(self._validated_model_config())

    def state_projection_config(self) -> dict[str, Any]:
        """One validated config read for the exact state the parent selects."""

        from .company_model_state import (
            model_spec_numeric_context_config, model_spec_prompt_byte_limit,
        )

        config = self._validated_model_config()
        return {
            "numeric_context_policy": model_spec_numeric_context_config(config),
            "prompt_byte_limit": model_spec_prompt_byte_limit(config),
        }

    def _validated_model_config(self) -> dict[str, Any] | None:
        from .document_extraction import validate_model_config

        if self.model_config_path is None:
            return None
        return validate_model_config(json.loads(
            self.model_config_path.read_text(encoding="utf-8")
        ))

    def _command(self, *, ticket_dir: Path, company_ref: str,
                 expected_state_hash: str, expected_task_hash: str,
                 expected_repair_policy_hash: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--company-ref", company_ref,
            "--expected-state-hash", expected_state_hash,
            "--expected-task-hash", expected_task_hash,
            "--expected-repair-policy-hash", expected_repair_policy_hash,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        return command

    def start(self, *, company_ref: str, state_hash: str,
              task_hash: str | None = None,
              repair_policy_hash: str | None = None) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a model specification run needs a company")
        if not isinstance(state_hash, str) or len(state_hash) != 64:
            raise LaneChildRejected("state_hash must be a sha256 digest")
        company_ref = company_ref.strip()
        if task_hash is None:
            from .company_model_spec import TASK_HASH
            task_hash = TASK_HASH
        identity = f"{self.TICKET_PREFIX}|{company_ref}|{state_hash}"
        if not isinstance(task_hash, str) or len(task_hash) != 64:
            raise LaneChildRejected("task_hash must be a sha256 digest")
        identity += f"|{task_hash}"
        actual_repair_policy_hash = self.repair_policy_hash()
        if repair_policy_hash is None:
            repair_policy_hash = actual_repair_policy_hash
        if (
            not isinstance(repair_policy_hash, str)
            or len(repair_policy_hash) != 64
            or repair_policy_hash != actual_repair_policy_hash
        ):
            raise LaneChildRejected("repair policy hash changed before launch")
        identity += f"|repair:{repair_policy_hash}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "state_hash": state_hash,
                "task_hash": task_hash,
                "repair_policy_hash": repair_policy_hash,
                "model_configured": self.configured,
            },
            company_ref=company_ref, expected_state_hash=state_hash,
            expected_task_hash=task_hash,
            expected_repair_policy_hash=repair_policy_hash,
        )


__all__ = ["TICKET_PREFIX", "CompanyModelSpecLauncher"]
