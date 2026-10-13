"""Fixed operational timings; projection content and credentials never enter logs."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sys
import time
from typing import Iterator

_STAGES = frozenset({'build_snapshot', 'projection_write', 'static_render'})


def _emit(stage: str, status: str, started: float, error_type: str | None = None) -> None:
    try:
        value = {'event': 'projection_stage', 'stage': stage, 'status': status,
                 'elapsed_seconds': round(max(0.0, time.monotonic() - started), 6)}
        if error_type is not None:
            value['error_type'] = error_type
        sys.stderr.write(json.dumps(value, separators=(',', ':')) + '\n')
        sys.stderr.flush()
    except Exception:
        # This is diagnostics only. A broken sink must neither turn success
        # into failure nor replace an exception from the actual projection.
        pass


@contextmanager
def stage_timing(stage: str) -> Iterator[None]:
    """Two rows per fixed stage; never inspect arguments, result or error text."""
    if stage not in _STAGES:
        yield
        return
    started = time.monotonic()
    _emit(stage, 'started', started)
    try:
        yield
    except BaseException as exc:
        _emit(stage, 'error', started, type(exc).__name__)
        raise
    else:
        _emit(stage, 'completed', started)
