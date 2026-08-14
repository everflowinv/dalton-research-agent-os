"""A small process-backed runtime adapter for Dalton Core.

The adapter is deliberately a protocol boundary, not a security sandbox.  It
starts one trusted fixture or runtime command in a temporary working
directory, gives it one JSON request on stdin, and accepts one JSON response
on stdout.  It has no Core store access and never receives an authority-path
or a broad parent environment.

The same-UID limitation is intentional: a process launched by the same OS
identity is not a hostile-process sandbox.  Production deployments still
need OS/container identity and resource isolation before executing unknown
code.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ModelInvocation, ResultEnvelope, RuntimeProfile, WorkOrder


class ProcessRuntimeError(RuntimeError):
    """Base error for process protocol, resource, and contract failures."""


class ProcessTimeout(ProcessRuntimeError):
    """The child did not finish before the configured wall-clock timeout."""


class OutputLimitExceeded(ProcessRuntimeError):
    """The child exceeded a configured stdout or stderr limit."""


class FrameTooLarge(OutputLimitExceeded):
    """The request or response JSON frame exceeded its maximum size."""


class ProcessProtocolError(ProcessRuntimeError):
    """The child did not emit exactly one valid JSON response frame."""


class ProcessExitError(ProcessRuntimeError):
    """The child exited unsuccessfully."""


class ProcessContractError(ProcessRuntimeError):
    """The child response is not a valid, matching pair of Core contracts."""


_DEFAULT_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
)
_FORBIDDEN_ENV_MARKERS = ("HOME", "CODEX", "OPENCLAW", "DATABASE", "DB_PATH")
_INVOCATION_IDENTITY_FIELDS = ("provider", "model", "model_family", "actor_ref")
_READ_CHUNK = 16 * 1024


def _as_contract(value: Any, cls: type[Any]) -> Any:
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        try:
            return cls.from_dict(value)
        except Exception as exc:  # normalize implementation-specific errors
            raise ProcessContractError(f"invalid {cls.__name__}: {exc}") from exc
    raise ProcessContractError(f"expected {cls.__name__} or mapping")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProcessContractError(f"contract is not JSON serializable: {exc}") from exc


def _validate_nonnegative_limits(values: Mapping[str, Any], label: str) -> None:
    for key, value in values.items():
        if key.startswith("max_") or key.endswith("_limit"):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ProcessContractError(f"{label} limit {key!r} is invalid")


def _validate_admission(work: WorkOrder, profile: RuntimeProfile) -> None:
    if work.runtime_profile_ref != profile.id:
        raise ProcessContractError("work order runtime_profile_ref does not match runtime profile")
    if work.schema_version not in profile.supported_input_versions:
        raise ProcessContractError("runtime profile does not support WorkOrder schema version")
    if work.schema_version not in profile.supported_result_versions:
        raise ProcessContractError("runtime profile does not support ResultEnvelope schema version")
    if len(work.requested_capabilities) != 1:
        raise ProcessContractError("one process invocation must request exactly one capability")
    if work.requested_capabilities[0] not in profile.capabilities:
        raise ProcessContractError("runtime profile does not support requested capability")
    if not isinstance(work.budget, Mapping) or not work.budget:
        raise ProcessContractError("work order budget must be a non-empty object")
    _validate_nonnegative_limits(work.budget, "work order")
    _validate_nonnegative_limits(profile.limits, "runtime profile")
    if not set(work.declared_side_effects).issubset(profile.side_effects):
        raise ProcessContractError("work order declares side effects outside runtime profile")
    for work_key, profile_key in (
        ("max_tokens", "max_tokens"),
        ("token_limit", "max_tokens"),
        ("max_cost", "max_cost"),
        ("cost_limit", "max_cost"),
        ("max_seconds", "max_seconds"),
        ("time_limit", "max_seconds"),
    ):
        if work_key in work.budget and profile_key in profile.limits:
            if work.budget[work_key] > profile.limits[profile_key]:
                raise ProcessContractError(
                    f"work order budget {work_key!r} exceeds runtime profile limit"
                )


def _validate_usage(usage: Mapping[str, Any], work_budget: Mapping[str, Any], profile_limits: Mapping[str, Any]) -> None:
    if not isinstance(usage, Mapping):
        raise ProcessContractError("invocation usage must be an object")
    aliases = {
        "tokens": ("max_tokens", "token_limit"),
        "input_tokens": ("max_input_tokens",),
        "output_tokens": ("max_output_tokens",),
        "cost": ("max_cost", "cost_limit"),
        "seconds": ("max_seconds", "time_limit"),
    }
    for used_name, limit_names in aliases.items():
        used = usage.get(used_name)
        if used is None:
            continue
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)) or used < 0:
            raise ProcessContractError(f"usage field {used_name!r} is invalid")
        for limit_name in limit_names:
            for limits, source in ((work_budget, "work order"), (profile_limits, "runtime profile")):
                if limit_name in limits and used > limits[limit_name]:
                    raise ProcessContractError(f"usage {used_name!r} exceeds {source} limit {limit_name!r}")


def _effective_timeout(
    adapter_timeout: float,
    work_budget: Mapping[str, Any],
    profile_limits: Mapping[str, Any],
) -> float:
    candidates = [adapter_timeout]
    for values in (work_budget, profile_limits):
        for key in ("max_seconds", "time_limit"):
            if key in values:
                candidates.append(float(values[key]))
    timeout = min(candidates)
    if timeout <= 0:
        raise ProcessContractError("process wall-clock budget must be positive")
    return timeout


def _safe_environment(
    *,
    keys: Sequence[str],
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment and reject authority-like names."""

    result: dict[str, str] = {}
    for key in keys:
        if not isinstance(key, str) or not key or any(marker in key.upper() for marker in _FORBIDDEN_ENV_MARKERS):
            raise ProcessRuntimeError(f"environment key is not allowed: {key!r}")
        if key in os.environ:
            result[key] = os.environ[key]
    for key, value in (extra or {}).items():
        if (
            not isinstance(key, str)
            or not key
            or any(marker in key.upper() for marker in _FORBIDDEN_ENV_MARKERS)
            or not isinstance(value, str)
        ):
            raise ProcessRuntimeError(f"environment override is not allowed: {key!r}")
        result[key] = value
    # These values make the fixture's JSON/text behavior deterministic without
    # importing any user shell configuration.
    result.setdefault("PYTHONIOENCODING", "utf-8")
    result.setdefault("PYTHONUNBUFFERED", "1")
    return result


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    process_group = os.name == "posix"
    try:
        if process_group:
            os.killpg(proc.pid, signal.SIGTERM)
        elif proc.poll() is None:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        if proc.poll() is None:
            proc.wait(timeout=0.25)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if process_group:
            # Descendants may keep running after the direct child exits, so
            # address the isolated process group even when proc.poll() is set.
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


class ProcessRuntimeAdapter:
    """Execute a command through the Dalton process envelope.

    ``command`` is an argv sequence and is never interpreted by a shell.  A
    child receives only the serialized ``WorkOrder`` and ``RuntimeProfile``;
    it cannot access the parent Core object through this adapter.
    """

    protocol_version = "0.1"

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout_seconds: float = 5.0,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 256 * 1024,
        max_frame_bytes: int = 512 * 1024,
        env_keys: Sequence[str] = _DEFAULT_ENV_KEYS,
        env: Mapping[str, str] | None = None,
        invocation_identity: Mapping[str, str],
    ) -> None:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argv sequence")
        self.command = tuple(os.fspath(part) for part in command)
        if any(not part for part in self.command):
            raise ValueError("command arguments must be non-empty")
        if isinstance(timeout_seconds, bool) or not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        for name, value in (
            ("max_stdout_bytes", max_stdout_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_frame_bytes", max_frame_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_frame_bytes > max_stdout_bytes:
            raise ValueError("max_frame_bytes cannot exceed max_stdout_bytes")
        self.timeout_seconds = float(timeout_seconds)
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_frame_bytes = max_frame_bytes
        self.env_keys = tuple(env_keys)
        self.env = dict(env or {})
        if not isinstance(invocation_identity, Mapping) or set(invocation_identity) != set(_INVOCATION_IDENTITY_FIELDS):
            raise ValueError(
                "invocation_identity must contain provider, model, model_family and actor_ref"
            )
        if any(
            not isinstance(invocation_identity[field], str) or not invocation_identity[field]
            for field in _INVOCATION_IDENTITY_FIELDS
        ):
            raise ValueError("invocation_identity values must be non-empty strings")
        self.invocation_identity = dict(invocation_identity)

    @classmethod
    def formatter(cls, **kwargs: Any) -> "ProcessRuntimeAdapter":
        """Return an adapter for the bundled deterministic formatter fixture."""

        from .formatter_worker import formatter_worker_command

        kwargs.setdefault(
            "invocation_identity",
            {
                "provider": "dalton-fixture",
                "model": "deterministic-formatter-v1",
                "model_family": "dalton-deterministic",
                "actor_ref": "fixture:formatter",
            },
        )
        return cls(formatter_worker_command(), **kwargs)

    def _read_child(
        self,
        request: bytes,
        cwd: str,
        timeout_seconds: float,
    ) -> tuple[bytes, bytes, int]:
        if len(request) > self.max_frame_bytes:
            raise FrameTooLarge("request JSON frame exceeds max_frame_bytes")
        child_env = _safe_environment(keys=self.env_keys, extra=self.env)
        try:
            proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=child_env,
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ProcessRuntimeError(f"could not start runtime process: {exc}") from exc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        selector = selectors.DefaultSelector()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        request_offset = 0
        stdout = bytearray()
        stderr = bytearray()
        started = time.monotonic()
        try:
            while selector.get_map():
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    _terminate(proc)
                    raise ProcessTimeout(f"runtime exceeded {timeout_seconds:.3f}s")
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.data == "stdin":
                        try:
                            written = os.write(
                                key.fileobj.fileno(),
                                request[request_offset : request_offset + _READ_CHUNK],
                            )
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        request_offset += written
                        if request_offset >= len(request):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fileobj.fileno(), _READ_CHUNK)
                    except OSError as exc:
                        _terminate(proc)
                        raise ProcessRuntimeError(f"could not read runtime {key.data}: {exc}") from exc
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = stdout if key.data == "stdout" else stderr
                    target.extend(chunk)
                    limit = self.max_stdout_bytes if key.data == "stdout" else self.max_stderr_bytes
                    if len(target) > limit:
                        _terminate(proc)
                        raise OutputLimitExceeded(f"runtime {key.data} exceeded configured output limit")
                    if key.data == "stdout" and len(target) > self.max_frame_bytes:
                        _terminate(proc)
                        raise FrameTooLarge("response JSON frame exceeds max_frame_bytes")
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                _terminate(proc)
                raise ProcessTimeout(f"runtime exceeded {timeout_seconds:.3f}s")
            try:
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                _terminate(proc)
                raise ProcessTimeout(f"runtime exceeded {timeout_seconds:.3f}s") from exc
            return bytes(stdout), bytes(stderr), returncode
        finally:
            selector.close()
            if proc.poll() is None:
                _terminate(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    def _decode_response(
        self,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
        work: WorkOrder,
        profile: RuntimeProfile,
    ) -> tuple[ModelInvocation, ResultEnvelope]:
        if returncode != 0:
            detail = stderr.decode("utf-8", "replace")[:2048]
            raise ProcessExitError(f"runtime exited with code {returncode}: {detail}")
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProcessProtocolError("runtime stdout is not UTF-8") from exc
        if not text.strip():
            raise ProcessProtocolError("runtime emitted an empty response")
        try:
            decoder = json.JSONDecoder()
            value, end = decoder.raw_decode(text.lstrip())
            if text.lstrip()[end:].strip():
                raise ValueError("additional data after JSON frame")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProcessProtocolError(f"runtime must emit exactly one JSON frame: {exc}") from exc
        if not isinstance(value, Mapping) or set(value) != {"protocol_version", "invocation", "result"}:
            raise ProcessProtocolError("runtime response envelope has unexpected fields")
        if value["protocol_version"] != self.protocol_version:
            raise ProcessProtocolError("runtime response protocol version is unsupported")
        try:
            invocation = ModelInvocation.from_dict(value["invocation"])
            result = ResultEnvelope.from_dict(value["result"])
        except Exception as exc:
            raise ProcessContractError(f"runtime returned invalid Core contracts: {exc}") from exc
        capability = work.requested_capabilities[0]
        if invocation.work_order_ref != work.id or result.work_order_ref != work.id:
            raise ProcessContractError("runtime response work order reference mismatch")
        if invocation.schema_version != work.schema_version or result.schema_version != work.schema_version:
            raise ProcessContractError("runtime response schema version mismatch")
        if invocation.profile_ref != profile.id or invocation.runtime_ref != profile.id:
            raise ProcessContractError("runtime response runtime profile reference mismatch")
        if invocation.environment_hash != profile.environment_hash:
            raise ProcessContractError("runtime response environment hash mismatch")
        for field, expected in self.invocation_identity.items():
            if getattr(invocation, field) != expected:
                raise ProcessContractError(
                    f"runtime response {field} does not match trusted adapter identity"
                )
        if invocation.capability != capability:
            raise ProcessContractError("runtime response capability mismatch")
        if result.invocation_ref != invocation.id:
            raise ProcessContractError("result envelope invocation reference mismatch")
        if invocation.input_refs != work.input_refs:
            raise ProcessContractError("runtime response input reference mismatch")
        if invocation.output_refs != result.artifact_refs:
            raise ProcessContractError(
                "runtime invocation output_refs and result artifact_refs must match"
            )
        _validate_usage(invocation.usage, work.budget, profile.limits)
        if not set(invocation.side_effects).issubset(work.declared_side_effects):
            raise ProcessContractError("invocation side effects exceed work-order declaration")
        if not set(result.actual_side_effects).issubset(work.declared_side_effects):
            raise ProcessContractError("result side effects exceed work-order declaration")
        if tuple(invocation.side_effects) != tuple(result.actual_side_effects):
            raise ProcessContractError("invocation and result side effects disagree")
        return invocation, result

    def execute(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        runtime_profile: RuntimeProfile | Mapping[str, Any],
    ) -> tuple[ModelInvocation, ResultEnvelope]:
        work = _as_contract(work_order, WorkOrder)
        profile = _as_contract(runtime_profile, RuntimeProfile)
        _validate_admission(work, profile)
        declared_identity = profile.metadata.get("invocation_identity")
        if declared_identity != self.invocation_identity:
            raise ProcessContractError(
                "runtime profile invocation_identity does not match trusted adapter identity"
            )
        request = _canonical(
            {
                "protocol_version": self.protocol_version,
                "work_order": work.to_dict(),
                "runtime_profile": profile.to_dict(),
            }
        ) + b"\n"
        timeout_seconds = _effective_timeout(
            self.timeout_seconds,
            work.budget,
            profile.limits,
        )
        with tempfile.TemporaryDirectory(prefix="dalton-runtime-") as cwd:
            stdout, stderr, returncode = self._read_child(
                request,
                cwd,
                timeout_seconds,
            )
        return self._decode_response(stdout, stderr, returncode, work, profile)

    run = execute


__all__ = [
    "FrameTooLarge",
    "OutputLimitExceeded",
    "ProcessContractError",
    "ProcessExitError",
    "ProcessProtocolError",
    "ProcessRuntimeAdapter",
    "ProcessRuntimeError",
    "ProcessTimeout",
]
