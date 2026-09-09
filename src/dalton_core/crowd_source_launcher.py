"""S3: launch the crowd-source children, one at a time, one per source.

Three sources, three launchers, and one base class holding the part that is
the same. What differs between them is the child module and its argv; what is
identical is the discipline: a run is named by what it asks for, so asking
twice for the same thing is the same ticket rather than a second child doing
identical work, and a run that was always going to be refused is refused here
rather than after a process exists.

**Governance is per operation, so the path is per operation too.** A schema
hash binds one operation, so `search_posts` and `get_post` are separate
approvals and a launcher holds a map, not a path. Asking for an operation the
map does not cover is a refusal that names the operation, because "no record
for this" and "record not approved" are different problems for whoever has to
fix them.

**The credential grant is a path, never a value.** It is handed to the child
as a file name; nothing in this process opens it except to check that it
covers the slots, and what it contains is refs and an expiry. The tool path is
the same shape of thing: this Core does not know where a host tool lives, so
the operator says, and a networked run with no tool configured refuses with
that sentence rather than guessing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .employee_reviews_core import OPERATION as BLIND_OPERATION
from .lane_child_launcher import LaneChildLauncher, LaneChildRejected
from .xreach_core import OPERATIONS as XREACH_OPERATIONS
from .xueqiu_core import OPERATIONS as XUEQIU_OPERATIONS

LIVE_MODE_ARGS: tuple[str, ...] = ("--allow-network",)
MAX_HANDLE_CHARS = 64
MAX_QUERY_CHARS = 200


class CrowdSourceLauncher(LaneChildLauncher):
    """Shared machinery for the three crowd-source children."""

    SOURCE_REF = ""
    OPERATIONS: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_paths: Mapping[str, str | Path],
        credential_grant_path: str | Path | None = None,
        tool: str | None = None,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        governance_loader: Callable[[Path], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.governance_paths = {
            str(operation): Path(path).expanduser().resolve()
            for operation, path in dict(governance_paths).items()
        }
        self.credential_grant_path = (
            Path(credential_grant_path).expanduser().resolve()
            if credential_grant_path else None
        )
        self.tool = tool
        self.mode_args = tuple(str(item) for item in mode_args)
        self._governance_loader = governance_loader

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def governance_path(self, operation: str) -> Path:
        try:
            return self.governance_paths[operation]
        except KeyError as exc:
            raise LaneChildRejected(
                f"no governance record is configured for {operation!r}"
            ) from exc

    def load_governance(self, operation: str) -> Any:
        from .connector_governance import ConnectorGovernance

        loader = self._governance_loader or ConnectorGovernance.load
        return loader(self.governance_path(operation))

    def _operation(self, operation: str) -> str:
        if operation not in self.OPERATIONS:
            raise LaneChildRejected(
                f"{operation!r} is not an operation of this connector"
            )
        return operation

    def _child_args(self, operation: str, params: Mapping[str, Any]) -> list[str]:
        """The lane-specific part of the command line."""

        raise NotImplementedError

    def _command(self, *, ticket_dir: Path, operation: str,
                 params: Mapping[str, Any]) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path(operation)),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        command += self._child_args(operation, params)
        if self.tool:
            command += ["--tool", self.tool]
        if self.credential_grant_path:
            command += ["--credential-grant", str(self.credential_grant_path)]
        return command + list(self.mode_args)

    def start(self, *, operation: str, actor_ref: str, **params: Any) -> dict[str, Any]:
        operation = self._operation(operation)
        if not isinstance(actor_ref, str) or not actor_ref.startswith(
            ("human:", "automation:")
        ):
            raise LaneChildRejected("actor must use the human: or automation: namespace")
        cleaned = self._validate(operation, params)
        # Refuse here rather than after a process exists: the child would reach
        # the same conclusion, having cost a spawn and a ticket to say so.
        governance = self.load_governance(operation)
        if not getattr(governance, "approved", False):
            raise LaneChildRejected(
                f"the {operation} governance record is not approved"
            )
        identity = "|".join(
            f"{key}={cleaned[key]}" for key in sorted(cleaned)
        )
        digest = hashlib.sha256(
            f"{self.TICKET_PREFIX}|{operation}|{identity}|{governance.content_hash}"
            .encode("utf-8")
        ).hexdigest()[:24]
        return self.spawn(
            digest=digest,
            record={
                "source_ref": self.SOURCE_REF,
                "operation": operation,
                "parameters": dict(cleaned),
                "actor_ref": actor_ref,
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "transport": "host-tool" if self.networked else "fixture",
            },
            operation=operation, params=cleaned,
        )

    def _validate(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _bounded_text(value: Any, *, name: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LaneChildRejected(f"a run needs a {name}")
        text = value.strip()
        if len(text) > limit:
            raise LaneChildRejected(f"{name} is longer than {limit} characters")
        return text

    @staticmethod
    def _since(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise LaneChildRejected("since must be YYYY-MM-DD")
        return text


class XueqiuLauncher(CrowdSourceLauncher):
    """Spawn ``dalton_core.xueqiu_cli`` against the writer's state."""

    TICKET_PREFIX = "xueqiu-run"
    TICKETS_DIRNAME = "xueqiu-runs"
    CHILD_MODULE = "dalton_core.xueqiu_cli"
    SOURCE_REF = "source:xueqiu"
    OPERATIONS = XUEQIU_OPERATIONS

    def __init__(self, *, fallback_tool: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fallback_tool = fallback_tool

    def _validate(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if operation == "search_posts":
            return {
                "query": self._bounded_text(
                    params.get("query"), name="query", limit=MAX_QUERY_CHARS),
                "since": self._since(params.get("since")) or "",
            }
        if operation == "get_post":
            return {"post_ref": self._bounded_text(
                params.get("post_ref"), name="post_ref", limit=MAX_HANDLE_CHARS)}
        return {"limit": int(params.get("limit") or 20),
                "stock_type": int(params.get("stock_type") or 10)}

    def _child_args(self, operation: str, params: Mapping[str, Any]) -> list[str]:
        args = ["--operation", operation]
        if operation == "search_posts":
            args += ["--query", params["query"]]
            if params.get("since"):
                args += ["--since", params["since"]]
        elif operation == "get_post":
            args += ["--post-ref", params["post_ref"]]
        else:
            args += ["--limit", str(params["limit"]),
                     "--stock-type", str(params["stock_type"])]
        return args

    def _command(self, *, ticket_dir: Path, operation: str,
                 params: Mapping[str, Any]) -> list[str]:
        command = super()._command(
            ticket_dir=ticket_dir, operation=operation, params=params)
        if operation == "hot_rank" and self.fallback_tool:
            command += ["--fallback-tool", self.fallback_tool]
        return command


class XreachLauncher(CrowdSourceLauncher):
    """Spawn ``dalton_core.xreach_cli`` against the writer's state."""

    TICKET_PREFIX = "xreach-run"
    TICKETS_DIRNAME = "xreach-runs"
    CHILD_MODULE = "dalton_core.xreach_cli"
    SOURCE_REF = "source:x"
    OPERATIONS = XREACH_OPERATIONS

    def _validate(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        since = self._since(params.get("since")) or ""
        if operation == "user_timeline":
            handle = self._bounded_text(
                params.get("handle"), name="handle", limit=MAX_HANDLE_CHARS)
            return {"handle": handle.lstrip("@"), "since": since}
        if operation == "search":
            return {"query": self._bounded_text(
                params.get("query"), name="query", limit=MAX_QUERY_CHARS),
                "since": since}
        return {"post_ref": self._bounded_text(
            params.get("post_ref"), name="post_ref", limit=MAX_HANDLE_CHARS)}

    def _child_args(self, operation: str, params: Mapping[str, Any]) -> list[str]:
        args = ["--operation", operation]
        if operation == "user_timeline":
            args += ["--handle", params["handle"]]
        elif operation == "search":
            args += ["--query", params["query"]]
        else:
            args += ["--post-ref", params["post_ref"]]
        if params.get("since"):
            args += ["--since", params["since"]]
        return args


class EmployeeReviewsLauncher(CrowdSourceLauncher):
    """Spawn ``dalton_core.employee_reviews_cli`` against the writer's state.

    No credential grant is passed even when one is configured: this connector's
    auth boundary is ``none``, and handing it a grant would imply otherwise.
    """

    TICKET_PREFIX = "employee-reviews-run"
    TICKETS_DIRNAME = "employee-reviews-runs"
    CHILD_MODULE = "dalton_core.employee_reviews_cli"
    SOURCE_REF = "source:blind"
    OPERATIONS = (BLIND_OPERATION,)

    def _validate(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        pages = params.get("pages") or 2
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 20:
            raise LaneChildRejected("pages must be 1..20")
        return {
            "employer_slug": self._bounded_text(
                params.get("employer_slug"), name="employer_slug",
                limit=MAX_HANDLE_CHARS),
            "since": self._since(params.get("since")) or "",
            "pages": pages,
        }

    def _child_args(self, operation: str, params: Mapping[str, Any]) -> list[str]:
        args = ["--employer-slug", params["employer_slug"],
                "--pages", str(params["pages"])]
        if params.get("since"):
            args += ["--since", params["since"]]
        return args

    def _command(self, *, ticket_dir: Path, operation: str,
                 params: Mapping[str, Any]) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path(operation)),
            "--summary-dir", str(ticket_dir), "--quiet",
        ]
        command += self._child_args(operation, params)
        return command + list(self.mode_args)


LAUNCHER_BY_SOURCE = {
    "xueqiu": XueqiuLauncher,
    "x": XreachLauncher,
    "employee-reviews": EmployeeReviewsLauncher,
}


class CrowdSourceLaunchers:
    """The three launchers as one object, because a lane arrives on one kwarg.

    A ``LaneSpec`` names a single ``init_kwarg``, and the writer stores and
    closes whatever arrives on it. This lane has three children, so what
    arrives is this: a mapping the coordinator reads and a ``close`` the writer
    calls.

    **The seam.** The coordinator does not care that these are launchers. What
    it needs from each entry is ``SOURCE_REF``, ``start(operation=...,
    actor_ref=..., **params)`` returning a ticket with an ``id``, and
    ``status(ticket_ref)`` returning ``{"status": ..., "summary": ...}``. A
    shared host-tool runner that records a ConnectorInvocation and a
    SourceEnvelope around the same children satisfies that contract, and
    swapping it in is a change to this class and to nothing else. The tests
    inject a fake through the same door.
    """

    def __init__(self, **by_source: Any) -> None:
        self.by_source = {name: launcher for name, launcher in by_source.items()
                          if launcher is not None}

    def __bool__(self) -> bool:
        return bool(self.by_source)

    def close(self) -> None:
        for launcher in self.by_source.values():
            launcher.close()


__all__ = [
    "LAUNCHER_BY_SOURCE",
    "CrowdSourceLaunchers",
    "LIVE_MODE_ARGS",
    "MAX_HANDLE_CHARS",
    "MAX_QUERY_CHARS",
    "CrowdSourceLauncher",
    "EmployeeReviewsLauncher",
    "XreachLauncher",
    "XueqiuLauncher",
]
