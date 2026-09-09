"""Writer-owned launcher for out-of-process Guidepoint search runs (S2).

One search is one child process, for the same reason every other connector
lane spawns one: ``ConnectorTransportExecutor`` bounds each provider call with
a ``SIGALRM`` watchdog, and only a process main thread may own one.  The writer
starts the child, records a ticket and returns; a later tick reads the ticket
back.

The ticket machinery is ``LaneChildLauncher`` (P13aj) rather than a fifth copy
of it.  What this subclass owns is what is genuinely Guidepoint's: refusing to
launch against a governance record the owner has not approved, refusing to
launch a networked run against a rehearsal record, and pinning the plan the
child is given to the exact plan the coordinator decided from.

Nothing secret is handed down.  ``GUIDEPOINT_CLIENT_ID`` and
``GUIDEPOINT_CLIENT_SECRET`` live in the OpenClaw workspace and the local
LaunchAgent proxy is what refreshes the token; the child is given an endpoint
on loopback and a credential *slot* ref, and would not know what to do with a
secret if it were handed one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from .guidepoint_search import GuidepointSearchError, GuidepointSearchGovernance
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildError,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
    secure_dir,
)
from .store import canonical_json


CLI_MODULE = "dalton_core.guidepoint_cli"
DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8943/mcp"
LIVE_MODE_ARGS: tuple[str, ...] = ("--allow-network",)
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


class GuidepointLaunchRejected(LaneChildRejected):
    """The Guidepoint search was refused before any process started."""


class GuidepointSearchLauncher(LaneChildLauncher):
    """Spawn ``dalton_core.guidepoint_cli search`` against the writer's state."""

    TICKET_PREFIX = "guidepoint-search-run"
    TICKETS_DIRNAME = "guidepoint-search-runs"
    CHILD_MODULE = CLI_MODULE

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        plan_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        mcp_endpoint: str = DEFAULT_MCP_ENDPOINT,
        fake_search_file: str | Path | None = None,
        spool_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.plan_path = Path(plan_path).expanduser().resolve()
        self.mode_args = tuple(str(item) for item in mode_args)
        self.mcp_endpoint = mcp_endpoint
        self.fake_search_file = (
            None if fake_search_file is None
            else Path(fake_search_file).expanduser().resolve()
        )
        self.spool_dir = None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        if self.networked and self.fake_search_file is not None:
            raise LaneChildError(
                "a networked Guidepoint launcher cannot also serve a fixture"
            )
        if not self.networked and self.fake_search_file is None:
            raise LaneChildError(
                "a rehearsal Guidepoint launcher needs --fake-search-file content"
            )

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    @property
    def plan(self) -> dict[str, Any]:
        """The plan on disk, re-read each time it is asked for.

        Not cached: the file is the authority, and a writer that has been up
        for a week should not be deciding from a plan that was replaced on
        Tuesday.  Reading a small JSON file once a tick costs nothing.
        """

        from .mission_guidepoint_lane import load_guidepoint_discovery_plan

        return load_guidepoint_discovery_plan(self.plan_path)

    # -- approval first ----------------------------------------------------
    def load_governance(self) -> GuidepointSearchGovernance:
        """Approval before anything is spawned, not inside the child.

        The child re-checks too -- it must, because it is the process that
        would spend the call -- but a lane that only checks downstream reports
        a refusal as a failed run, and a refusal is not a failure.
        """

        try:
            governance = GuidepointSearchGovernance.load(self.governance_path)
        except FileNotFoundError as exc:
            raise GuidepointLaunchRejected(
                "Guidepoint connector governance record is missing; owner approval is required"
            ) from exc
        except (GuidepointSearchError, ValueError) as exc:
            raise GuidepointLaunchRejected(
                f"Guidepoint connector governance record is invalid: {exc}"
            ) from exc
        if not governance.approved:
            raise GuidepointLaunchRejected(
                "Guidepoint connector governance record is not approved; "
                "owner approval is required"
            )
        if _HUMAN_RE.fullmatch(governance.approved_by) is None:
            raise GuidepointLaunchRejected(
                "Guidepoint governance record must be approved by a human principal"
            )
        return governance

    # -- command -----------------------------------------------------------
    def _command(
        self,
        *,
        ticket_dir: Path,
        spec_ref: str,
        company_ref: str,
        requested_by: str,
        mission_version_ref: str,
        mission_version_hash: str,
        as_of: str,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", CLI_MODULE, "search",
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--discovery-plan", str(self.plan_path),
            "--spec-ref", spec_ref,
            "--company-ref", company_ref,
            "--requested-by", requested_by,
            "--mission-version-ref", mission_version_ref,
            "--mission-version-hash", mission_version_hash,
            "--as-of", as_of,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        if self.fake_search_file is not None:
            command += ["--fake-search-file", str(self.fake_search_file)]
        else:
            command += ["--mcp-endpoint", self.mcp_endpoint]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        command += list(self.mode_args)
        return command

    # -- one run -----------------------------------------------------------
    def start(
        self,
        *,
        plan: Mapping[str, Any],
        spec_ref: str,
        company_ref: str,
        parameters: Mapping[str, Any],
        query_hash: str,
        requested_by: str,
        mission_version_ref: str,
        mission_version_hash: str,
        as_of: date | str,
    ) -> dict[str, Any]:
        if not isinstance(requested_by, str) or (
            _HUMAN_RE.fullmatch(requested_by) is None
            and _AUTOMATION_RE.fullmatch(requested_by) is None
        ):
            raise GuidepointLaunchRejected(
                "Guidepoint search requester must use the human: or automation: namespace"
            )
        for name, value in (("spec_ref", spec_ref), ("company_ref", company_ref)):
            if not isinstance(value, str) or not value:
                raise GuidepointLaunchRejected(f"{name} must be non-empty text")
        if not isinstance(query_hash, str) or len(query_hash) != 64:
            raise GuidepointLaunchRejected("query_hash must be SHA-256 hex")
        # The child re-reads the plan from disk; if the coordinator decided
        # from a different one, the search the mission authorized and the
        # search the child runs are not the same search.
        on_disk = _plan_hash(self.plan_path)
        if plan.get("content_hash") != on_disk:
            raise GuidepointLaunchRejected(
                "the coordinator's plan is not the plan on disk; refusing to launch"
            )
        as_of_text = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        governance = self.load_governance()
        digest = hashlib.sha256(
            canonical_json(
                {
                    "plan_hash": on_disk,
                    "spec_ref": spec_ref,
                    "company_ref": company_ref,
                    "query_hash": query_hash,
                    "as_of": as_of_text,
                    "governance_hash": governance.content_hash,
                    "mission_version_hash": mission_version_hash,
                }
            ).encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "source_ref": "source:guidepoint",
                "plan_ref": plan["id"],
                "plan_hash": on_disk,
                "spec_ref": spec_ref,
                "company_ref": company_ref,
                "parameters": dict(parameters),
                "query_hash": query_hash,
                "requested_by": requested_by,
                "mission_version_ref": mission_version_ref,
                "mission_version_hash": mission_version_hash,
                "as_of": as_of_text,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "loopback-mcp" if self.networked else "fixture",
            },
            spec_ref=spec_ref,
            company_ref=company_ref,
            requested_by=requested_by,
            mission_version_ref=mission_version_ref,
            mission_version_hash=mission_version_hash,
            as_of=as_of_text,
        )


def _plan_hash(path: Path) -> str:
    from .mission_guidepoint_lane import load_guidepoint_discovery_plan

    return load_guidepoint_discovery_plan(path)["content_hash"]


__all__ = [
    "CLI_MODULE",
    "DEFAULT_MCP_ENDPOINT",
    "LIVE_MODE_ARGS",
    "GuidepointLaunchRejected",
    "GuidepointSearchLauncher",
    "LaneChildConflict",
    "LaneChildError",
    "LaneChildTicketNotFound",
    "secure_dir",
]
