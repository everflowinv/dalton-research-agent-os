"""Bounded, best-effort timings for one store discovery operation.

Only fixed source/stage names and durations leave the process. No authority,
query, request, result or exception message is inspected.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
import time

MAX_LINES_PER_OPERATION = 64
_SOURCES = frozenset(("source:alphaengine", "source:sec-edgar", "source:web-search"))
_STAGES = frozenset(("first_mission_preflight", "coordinator", "settle", "carry", "held", "acquire", "search"))
_active = ContextVar("source_discovery_diagnostics", default=None)


class DiagnosticBudget:
    def __init__(self):
        self.remaining = MAX_LINES_PER_OPERATION


@contextmanager
def diagnostic_scope(budget):
    token = _active.set(budget)
    try:
        yield
    finally:
        _active.reset(token)


def _emit(record):
    try:
        sys.stderr.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        # Diagnostics must never change dispatch, settlement or exceptions.
        pass


@contextmanager
def phase(source_ref, stage):
    budget = _active.get()
    if (budget is None or budget.remaining < 2 or source_ref not in _SOURCES
            or stage not in _STAGES):
        yield
        return
    # Reserve the pair before nested phases: a started phase always has room
    # for its completion, including after a caller has abandoned the RPC.
    budget.remaining -= 2
    base = {"event": "source_discovery_phase", "source_ref": source_ref, "stage": stage}
    started = time.monotonic()
    _emit({**base, "status": "started"})
    error = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        _emit({**base, "status": "error" if error else "completed",
               "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
               **({"error_type": error} if error else {})})
