"""Cooperative cancellation for disposable projections; never authority writers."""
from __future__ import annotations
import sqlite3
from typing import Callable

class ProjectionCancelled(RuntimeError):
    """The controller stopped before publishing this derived projection."""

def check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise ProjectionCancelled("dashboard projection cancelled during controller stop")

def install_cancel_handler(connection: sqlite3.Connection,
                           cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None:
        # Only query-only source connections and the private projection tempfile
        # use this handler. A controller stop must not interrupt authority writes.
        connection.set_progress_handler(lambda: int(cancel_requested()), 1000)
