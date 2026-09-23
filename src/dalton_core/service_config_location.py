"""Where an environment's ``service.json`` is, given its state directory.

Every installation lays out ``<root>/state/<core>`` beside
``<root>/config/service.json``, so nine call sites derived the service config
by taking the state directory's grandparent.  All nine first called
``Path.resolve()``, and that is the bug this module exists to remove.

``resolve()`` answers "where is this directory *stored*".  The question the
layout asks is "which installation is this directory *part of*", and the two
stop agreeing the moment the state directory is a symlink.  On the legacy
environment ``~/Library/Application Support/Dalton/state`` points at
``/Volumes/EveSSD/Dalton/legacy-state``, because the state moved to an external
volume while the installation did not: the config, the LaunchAgents and the
owner's ``service.json`` all stayed in ``~/Library/Application Support/Dalton``.
Resolving first therefore derived ``/Volumes/EveSSD/Dalton/config/service.json``
-- a path that has never existed -- and every stage pinned in ``service.json``
read as ``unconfigured`` on legacy while reading correctly on a workspace
environment, whose state directory happens not to be a symlink.

So the rule here is **lexical first**: the installation root is the one the
caller named, not the one the filesystem stores the bytes under.  The resolved
derivation is still tried second, because a caller that has already resolved
its own state directory (several do, before the path ever reaches this module)
would otherwise lose an answer it used to get.  The first candidate that is a
file wins.

When neither candidate exists, the lexical one is returned: an absent service
config must stay absent -- reported at the path the installation would put it
-- rather than being quietly reported as configured from somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIRECTORY_NAME = "config"
SERVICE_CONFIG_NAME = "service.json"


def _candidate(directory: Path) -> Path | None:
    parents = directory.parents
    if len(parents) < 2:
        return None
    return parents[1] / CONFIG_DIRECTORY_NAME / SERVICE_CONFIG_NAME


def service_config_candidates(state_dir: str | Path) -> tuple[Path, ...]:
    """Every layout-derived service config path for ``state_dir``, best first.

    The lexical derivation comes first and is always present unless the state
    directory is too shallow to have a grandparent.  The symlink-resolved one
    follows when it differs.
    """

    given = Path(os.path.abspath(os.path.expanduser(str(state_dir))))
    try:
        resolved = given.resolve()
    except OSError:  # pragma: no cover - resolve() is lenient on every platform
        resolved = given
    found: list[Path] = []
    for directory in (given, resolved):
        candidate = _candidate(directory)
        if candidate is not None and candidate not in found:
            found.append(candidate)
    return tuple(found)


def service_config_path(state_dir: str | Path) -> Path:
    """The service configuration of the environment ``state_dir`` belongs to.

    Returns the first candidate that is a file, and the lexical candidate when
    none is, so that a genuinely missing ``service.json`` still reads missing.
    """

    candidates = service_config_candidates(state_dir)
    if not candidates:
        # A state directory with no grandparent has no installation root to
        # name.  Returning the unreachable path keeps every caller's
        # ``is_file()`` guard the single place absence is decided.
        return (Path(os.path.abspath(os.path.expanduser(str(state_dir))))
                / CONFIG_DIRECTORY_NAME / SERVICE_CONFIG_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


__all__ = [
    "CONFIG_DIRECTORY_NAME",
    "SERVICE_CONFIG_NAME",
    "service_config_candidates",
    "service_config_path",
]
