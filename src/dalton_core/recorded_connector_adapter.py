"""Offline-only adapter for deterministic Connector transport fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .connector_runner import validate_adapter_transport_observation
from .store import canonical_json, content_hash


class RecordedAdapterError(Exception):
    pass


class RecordedFixtureAdapter:
    """Replay one operator-selected fixture without sockets or dynamic import."""

    def __init__(
        self,
        fixture_path: str | Path,
        *,
        advance_clock: Callable[[float], None] | None = None,
    ) -> None:
        self._fixture_path = Path(fixture_path).resolve()
        self._fixture_root = self._fixture_path.parent
        try:
            fixture = json.loads(self._fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordedAdapterError("recorded fixture is unreadable") from exc
        expected = {"name", "behavior", "advance_seconds", "raw_file", "observation"}
        if not isinstance(fixture, Mapping) or set(fixture) != expected:
            raise RecordedAdapterError("recorded fixture has an open or incomplete shape")
        if fixture["behavior"] not in {"return", "raise"}:
            raise RecordedAdapterError("recorded fixture behavior is invalid")
        if (
            isinstance(fixture["advance_seconds"], bool)
            or not isinstance(fixture["advance_seconds"], (int, float))
            or fixture["advance_seconds"] < 0
        ):
            raise RecordedAdapterError("advance_seconds must be non-negative")
        if fixture["raw_file"] is not None and not isinstance(fixture["raw_file"], str):
            raise RecordedAdapterError("raw_file must be a relative name or null")
        if fixture["observation"] is not None and not isinstance(
            fixture["observation"], Mapping
        ):
            raise RecordedAdapterError("observation must be an object or null")
        self._fixture = json.loads(canonical_json(fixture))
        self._advance_clock = advance_clock

    @property
    def fixture_name(self) -> str:
        return str(self._fixture["name"])

    def __call__(self, request: Mapping[str, Any], raw_sink: Any) -> dict[str, Any]:
        raw_file = self._fixture["raw_file"]
        if raw_file is not None:
            candidate = (self._fixture_root / raw_file).resolve()
            if candidate.parent != self._fixture_root:
                raise RecordedAdapterError("raw fixture path escapes fixture root")
            raw_sink.write(candidate.read_bytes())
        advance = float(self._fixture["advance_seconds"])
        if advance and self._advance_clock is not None:
            self._advance_clock(advance)
        if self._fixture["behavior"] == "raise":
            raise RecordedAdapterError(f"recorded adapter crash: {self.fixture_name}")
        observation = dict(self._fixture["observation"])
        observation["request_hash"] = request["content_hash"]
        observation["content_hash"] = content_hash(observation)
        return validate_adapter_transport_observation(observation)


__all__ = ["RecordedAdapterError", "RecordedFixtureAdapter"]
