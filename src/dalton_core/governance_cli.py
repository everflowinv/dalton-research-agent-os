"""Ephemeral human governance client.

The human credential exists in the writer principal file only for one RPC and
is removed in ``finally``.  The token is never printed, logged, or placed in a
process argument.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .writer_client import WriterClient
from .writer_server import (
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    load_principals,
    replace_token_config,
)


class GovernanceCliError(RuntimeError):
    pass


def _params(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GovernanceCliError("governance parameter file is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise GovernanceCliError("governance parameters must be a JSON object")
    return dict(value)


def ephemeral_call(token_config: str | Path, socket_path: str | Path, *, actor_ref: str, operation: str, params: Mapping[str, Any]) -> Any:
    if operation not in HUMAN_GOVERNANCE_OPERATIONS:
        raise GovernanceCliError("operation is not a human governance operation")
    if not actor_ref.startswith("human:"):
        raise GovernanceCliError("actor_ref must use the human: namespace")
    config = Path(token_config).expanduser().resolve()
    socket = Path(socket_path).expanduser().resolve()
    lock_path = config.with_name(f".{config.name}.governance.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        principals = load_principals(config)
        if any(principal.resolved_actor_ref.startswith("human:") for principal in principals.values()):
            raise GovernanceCliError("another ephemeral human principal is already active")
        token = secrets.token_urlsafe(48)
        principal_id = f"governance-{uuid.uuid4().hex}"
        human = Principal(
            principal_id=principal_id,
            token=token,
            operations=HUMAN_GOVERNANCE_OPERATIONS,
            unrestricted=False,
            actor_ref=actor_ref,
        )
        replace_token_config(config, [*principals.values(), human])
        try:
            return WriterClient(str(socket), token).call(operation, dict(params))
        finally:
            current = load_principals(config)
            current.pop(principal_id, None)
            replace_token_config(config, list(current.values()))
            mode = stat.S_IMODE(config.stat().st_mode)
            if mode != 0o600:
                raise GovernanceCliError("token config lost owner-only permissions")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one ephemeral Dalton human-governance operation")
    parser.add_argument("--token-config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--operation", choices=sorted(HUMAN_GOVERNANCE_OPERATIONS), required=True)
    parser.add_argument("--params", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = ephemeral_call(
        args.token_config,
        args.socket,
        actor_ref=args.actor,
        operation=args.operation,
        params=_params(args.params),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
