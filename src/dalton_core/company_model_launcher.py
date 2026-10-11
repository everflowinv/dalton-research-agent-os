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
    freezes_permission_control = True

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

    def financial_validation_contract_hash(self) -> str:
        from .company_model_cli import model_spec_validation_contract_hash

        return model_spec_validation_contract_hash()

    def call_budget_hash(self) -> str | None:
        """Shared policy contents are controls, even when their path is unchanged."""
        from .call_budget import budget_fingerprint
        from .company_model_cli import model_spec_call_budget

        config = self._validated_model_config()
        return None if config is None else budget_fingerprint(model_spec_call_budget(config))

    def permission_control_projection(self) -> dict[str, str | None]:
        """Runtime code whose change can authorize a held model Work anew."""

        from . import cockpit_model

        path = Path(cockpit_model.__file__).resolve()
        return {"cockpit_model_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "model_spec_call_budget_hash": self.call_budget_hash()}

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
                 expected_repair_policy_hash: str,
                 expected_financial_validation_contract_hash: str,
                 expected_call_budget_hash: str | None = None) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--company-ref", company_ref,
            "--expected-state-hash", expected_state_hash,
            "--expected-task-hash", expected_task_hash,
            "--expected-repair-policy-hash", expected_repair_policy_hash,
            "--expected-financial-validation-contract-hash",
            expected_financial_validation_contract_hash,
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        if expected_call_budget_hash is not None:
            command += ["--expected-call-budget-hash", expected_call_budget_hash]
        if self.model_config_path is not None:
            command += ["--model-config", str(self.model_config_path)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        return command

    def start(self, *, company_ref: str, state_hash: str,
              task_hash: str | None = None,
              repair_policy_hash: str | None = None,
              controlled_reentry: str | None = None,
              permission_control_key: str | None = None,
              expected_start_budget_hash: str | None = None) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref.strip():
            raise LaneChildRejected("a model specification run needs a company")
        if not isinstance(state_hash, str) or len(state_hash) != 64:
            raise LaneChildRejected("state_hash must be a sha256 digest")
        company_ref = company_ref.strip()
        if task_hash is None:
            from .company_model_spec import TASK_HASH
            task_hash = TASK_HASH
        if not isinstance(task_hash, str) or len(task_hash) != 64:
            raise LaneChildRejected("task_hash must be a sha256 digest")
        actual_repair_policy_hash = self.repair_policy_hash()
        if repair_policy_hash is None:
            repair_policy_hash = actual_repair_policy_hash
        if (
            not isinstance(repair_policy_hash, str)
            or len(repair_policy_hash) != 64
            or repair_policy_hash != actual_repair_policy_hash
        ):
            raise LaneChildRejected("repair policy hash changed before launch")
        if permission_control_key is not None:
            from .mission_model_spec_lane import business_key
            import re
            prefix = business_key(company_ref=company_ref, state_hash=state_hash,
                task_hash=task_hash, repair_policy_hash=repair_policy_hash,
                validation_hash=self.financial_validation_contract_hash()) + "|permission:"
            if (not isinstance(permission_control_key, str)
                    or not permission_control_key.startswith(prefix)
                    or not re.fullmatch("[a-f0-9]{16}", permission_control_key[len(prefix):])):
                raise LaneChildRejected("model specification permission binding differs")
        validation_hash = self.financial_validation_contract_hash()
        call_budget_hash = self.call_budget_hash()
        if permission_control_key is not None and call_budget_hash != expected_start_budget_hash:
            raise LaneChildRejected("model specification call budget changed before launch")
        digest = self._ticket_digest(
            company_ref=company_ref, state_hash=state_hash, task_hash=task_hash,
            repair_policy_hash=repair_policy_hash, validation_hash=validation_hash,
            call_budget_hash=call_budget_hash)
        return self.spawn(
            digest=digest,
            record={
                "company_ref": company_ref, "state_hash": state_hash,
                "task_hash": task_hash,
                "repair_policy_hash": repair_policy_hash,
                "financial_validation_contract_hash": validation_hash,
                "model_configured": self.configured,
                **({} if permission_control_key is None else {
                    "permission_control_key": permission_control_key}),
                **({} if call_budget_hash is None else {"call_budget_hash": call_budget_hash}),
            },
            _controlled_reentry=(None if controlled_reentry is None else
                                 (f"{self.TICKET_PREFIX}:{digest}", controlled_reentry)),
            company_ref=company_ref, expected_state_hash=state_hash,
            expected_task_hash=task_hash,
            expected_repair_policy_hash=repair_policy_hash,
            expected_financial_validation_contract_hash=validation_hash,
            expected_call_budget_hash=call_budget_hash,
        )

    def _ticket_digest(self, *, company_ref: str, state_hash: str,
                       task_hash: str, repair_policy_hash: str,
                       validation_hash: str, call_budget_hash: str | None = None) -> str:
        identity = f"{self.TICKET_PREFIX}|{company_ref}|{state_hash}|{task_hash}"
        identity += f"|repair:{repair_policy_hash}|financial-validation:{validation_hash}"
        if call_budget_hash is not None:
            identity += f"|call-budget:{call_budget_hash}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def controlled_budget_reentry(self, *, business_key: str,
                                  current_permission: str,
                                  mission: dict[str, Any], company_ref: str,
                                  state_hash: str, task_hash: str,
                                  repair_policy_hash: str) -> str | None:
        """Return the one reviewed no-send budget recovery, if present."""
        if self.scheduler_db is None or self.model_config_path is None:
            return None
        from .controlled_budget_reentry import approved_business_key
        try:
            config = self._validated_model_config()
            suffix = approved_business_key(
                self.scheduler_db, business_key=business_key,
                current_permission=current_permission, mission=mission,
                allowed_purposes={"model_spec"},
            ) if config and config.get("budget_db") else None
            if suffix is None:
                return None
            digest = self._ticket_digest(
                company_ref=company_ref, state_hash=state_hash, task_hash=task_hash,
                repair_policy_hash=repair_policy_hash,
                validation_hash=self.financial_validation_contract_hash(),
                call_budget_hash=self.call_budget_hash())
            ticket_ref = f"{self.TICKET_PREFIX}:{digest}"
            return None if self.controlled_reentry_claimed(ticket_ref, suffix) else suffix
        except (OSError, TypeError, ValueError):
            return None


__all__ = ["TICKET_PREFIX", "CompanyModelSpecLauncher"]
