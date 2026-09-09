"""Which model configuration files this Core installs, as a registry.

Every lane that spends money on a model reads a configuration file out of the
state directory, and every one of those files names the thesis-impact day
budget policy version it accounts against.  Raising the day cap appends a new
policy version and has to repoint all of them; a configuration left out keeps
naming a superseded version and its lane keeps being refused, with a refusal
that reads like a budget message rather than a wiring mistake.

That list lived in ``scripts/raise_day_budget_cap.py`` as a tuple, and it had
already been wrong once: the deliverable-drafting configuration was added by
P13ad and only noticed at P13am, after a cap raise had repointed two of three.
A list maintained in a script no lane module can reach will be wrong again, so
it lives here and a lane registers its own.

Names, not paths: they are all files directly in the live state directory, and
the caller is the one that knows where that is.
"""

from __future__ import annotations

import re


class ModelConfigurationError(RuntimeError):
    """The configuration name is not a plain state-directory JSON file name."""


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.json$")
# The seed: the three configurations this Core installs today.
_SEED_NAMES = (
    "document-extraction-model-config.json",
    "research-planner-model-config.json",
    # P13am: the deliverable-drafting configuration, which the company model
    # specification lane also runs on. It was left out when it was added, so
    # the last cap raise repointed two of three configurations and this one
    # would have kept naming a superseded policy version.
    "initial-screen-model-config.json",
)
_NAMES: list[str] = list(_SEED_NAMES)


def register_model_config_name(name: str) -> str:
    """Add one model configuration file to the set a cap raise repoints.

    Registration order is preserved and a repeat is a no-op, so the report a
    cap raise prints stays stable across a re-registration.
    """

    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ModelConfigurationError(
            "a model configuration name is a lowercase .json file name"
        )
    if name not in _NAMES:
        _NAMES.append(name)
    return name


def model_config_names() -> tuple[str, ...]:
    """Every registered model configuration file name, in registration order."""

    return tuple(_NAMES)


__all__ = [
    "ModelConfigurationError",
    "model_config_names",
    "register_model_config_name",
]
