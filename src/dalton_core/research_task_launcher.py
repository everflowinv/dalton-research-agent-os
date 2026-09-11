"""P14e: the child that admits research tasks, one run at a time.

Admission is two authority writes per inquiry -- a backlog question and a loop
version -- against a mission, a mandate, a plan and the probe catalogue.  It is
not slow, but it is a multi-table write against Core, and the four lanes that
already do that shape of work do it out of process for the same reason: a
writer request thread is abandoned after 30 seconds, and a lane that occupies
one is a lane the controller reports as dark.

Everything about the ticket machinery is ``LaneChildLauncher``'s (P13aj): one
child at a time, owner-only tickets, and a restart under a running child that
settles as *orphaned* rather than guessing success from a stray summary file.
What is left here is this lane's own: its prefix, its directory and its
command.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME as DOCUMENT_DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME as DOCUMENT_VERIFIER_MODEL_CONFIG_NAME,
)
from .store import canonical_json

TICKET_PREFIX = "research-task"
TICKETS_DIRNAME = "research-tasks"


class ResearchTaskLauncher(LaneChildLauncher):
    """Run one configured ad-hoc or directed-only admission pass."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = TICKETS_DIRNAME
    CHILD_MODULE = "dalton_core.research_task_cli"

    def __init__(
        self,
        *,
        max_admissions_per_tick: int = 1,
        retired_templates: Sequence[str] = (),
        task_budget: dict[str, int] | None = None,
        config_path: str | Path | None = None,
        planner_scheduler_db: str | Path | None = None,
        planner_model_config_path: str | Path | None = None,
        document_draft_model_config_path: str | Path | None = None,
        document_verifier_model_config_path: str | Path | None = None,
        directed_only: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.retired_templates = tuple(str(item) for item in retired_templates)
        if (
            isinstance(max_admissions_per_tick, bool)
            or not isinstance(max_admissions_per_tick, int)
            or max_admissions_per_tick < 1
        ):
            raise LaneChildRejected("max_admissions_per_tick must be a positive integer")
        self.max_admissions_per_tick = max_admissions_per_tick
        from .research_task import validate_task_budget
        self.task_budget = validate_task_budget({} if task_budget is None else task_budget)
        if not isinstance(directed_only, bool):
            raise LaneChildRejected("directed_only must be boolean")
        self.directed_only = directed_only
        self.config_path = None if config_path is None else Path(config_path)
        self.planner_scheduler_db = (
            None if planner_scheduler_db is None
            else Path(planner_scheduler_db).expanduser().resolve()
        )
        self.planner_model_config_path = (
            None if planner_model_config_path is None
            else Path(planner_model_config_path).expanduser().resolve()
        )
        self.document_draft_model_config_path = (
            Path(document_draft_model_config_path).expanduser().resolve()
            if document_draft_model_config_path is not None
            else self.state_dir / DOCUMENT_DRAFT_MODEL_CONFIG_NAME
        )
        self.document_verifier_model_config_path = (
            Path(document_verifier_model_config_path).expanduser().resolve()
            if document_verifier_model_config_path is not None
            else self.state_dir / DOCUMENT_VERIFIER_MODEL_CONFIG_NAME
        )

    @staticmethod
    def _file_digest(path: Path | None) -> str | None:
        if path is None or not path.is_file() or path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def configuration(self) -> dict[str, Any]:
        from .mission_research_task_lane import lane_configuration
        from .research_task import default_planner_cost_usd
        settings = (lane_configuration(self.config_path) if self.config_path is not None else {
            "max_admissions_per_tick": self.max_admissions_per_tick,
            "retired_templates": self.retired_templates,
            "task_budget": self.task_budget,
        })
        authority = {
            "planner_scheduler_db": (
                None if self.planner_scheduler_db is None
                else str(self.planner_scheduler_db)
            ),
            "planner_model_config_path": (
                None if self.planner_model_config_path is None
                else str(self.planner_model_config_path)
            ),
            "document_draft_model_config_path": str(
                self.document_draft_model_config_path
            ),
            "document_verifier_model_config_path": str(
                self.document_verifier_model_config_path
            ),
            "planner_model_config_hash": self._file_digest(
                self.planner_model_config_path
            ),
            "document_draft_model_config_hash": self._file_digest(
                self.document_draft_model_config_path
            ),
            "document_verifier_model_config_hash": self._file_digest(
                self.document_verifier_model_config_path
            ),
        }
        return {
            **settings,
            "directed_only": self.directed_only,
            "planner_cost_usd": str(default_planner_cost_usd(self.state_dir)),
            "document_authority": authority,
        }

    def configuration_signature(self) -> str:
        return hashlib.sha256(canonical_json(self.configuration()).encode()).hexdigest()

    def _command(self, *, ticket_dir: Path, **kwargs: Any) -> list[str]:
        settings = kwargs.get("configuration") or self.configuration()
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--summary-dir", str(ticket_dir),
            "--max-admissions", str(settings["max_admissions_per_tick"]),
            "--task-budget", canonical_json(settings["task_budget"]),
            "--quiet",
        ] + [
            argument
            for template_ref in settings["retired_templates"]
            for argument in ("--retired-template", template_ref)
        ]
        if self.planner_scheduler_db is not None:
            command += ["--planner-scheduler-db", str(self.planner_scheduler_db)]
        if self.planner_model_config_path is not None:
            command += ["--planner-model-config", str(self.planner_model_config_path)]
        command += [
            "--mission-document-draft-model-config",
            str(self.document_draft_model_config_path),
            "--mission-document-verifier-model-config",
            str(self.document_verifier_model_config_path),
        ]
        if self.directed_only:
            command.append("--directed-only")
        return command

    def start(self, *, plan_ref: str, signature: str) -> dict[str, Any]:
        """Start one admission pass for one plan.

        The digest is the plan and the signature, so re-running against an
        unchanged plan is the same ticket rather than a new directory every
        five minutes.
        """

        configuration = self.configuration()
        digest = hashlib.sha256(
            canonical_json({
                "plan_ref": plan_ref, "signature": signature,
                "configuration": configuration,
            }).encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={"plan_ref": plan_ref, "signature": signature, "configuration": configuration},
            configuration=configuration,
        )


__all__ = ["TICKETS_DIRNAME", "TICKET_PREFIX", "ResearchTaskLauncher"]
