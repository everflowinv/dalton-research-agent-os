"""The one check every path-taking SQLite owner runs on its ``path``.

``sqlite3.connect(str(x))`` accepts any object: handed an open
``sqlite3.Connection`` instead of a file path, it quietly creates an empty
database named ``<sqlite3.Connection object at 0x...>`` in the working
directory and the caller fails later on a missing table.  Two such files were
committed to the repository root (7f24d085), left by a test that passed
``staging.connection`` to ``HumanReviewAuthority``.  A path is a ``str`` or an
``os.PathLike``; anything else is a caller bug and is refused before anything
touches the filesystem.
"""

from __future__ import annotations

import os
from typing import Any


def sqlite_path(value: Any, name: str = "path") -> str:
    """``value`` as a database path string, or ``TypeError``."""

    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        raise TypeError(
            f"{name} must be a database path (str or os.PathLike), not "
            f"{type(value).__module__}.{type(value).__qualname__}; pass an open "
            "connection through the connection= argument where one exists"
        )
    return value


__all__ = ["sqlite_path"]
