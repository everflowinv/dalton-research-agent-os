"""D5: what "发布前语言审查" is for, and what it was being spent on.

``research-language-policy.json`` carries one flag, ``required``, and on the
live Core it is ``true``.  What that has come to mean is: every product this
system produces, at every stage of its life, goes through a translation and a
language check before anybody may read it -- including the versions that will
never be delivered, the ones a person is about to reject, and the intermediate
drafts that exist for ninety seconds on their way to a better one.  Half of the
last forty-eight hours' work orders were spent on it, and the language check
itself fails about a third of the time.

That is not what the flag is for.  Language review exists so that what reaches
a reader is written in good Chinese.  A version nobody will read does not need
to be well written; it needs to be *correct*, and the checks that establish
that -- the citation resolver, the number discipline, the independent verifier
-- are somewhere else entirely and are not affected by any of this.

So the flag keeps its name and gains a scope, and the scope's default is the
change: **review what is going to be delivered.**

* ``approved_final`` (the default, including for the legacy bare ``true``):
  the final version of a product that a person has approved, or that is being
  rendered for delivery.  Everything else is skipped.
* ``all``: what the flag used to mean.  An installation that wants every
  intermediate draft checked writes it explicitly, because that is an unusual
  thing to want and it should look unusual in the file.
* ``off``: no language review anywhere.

And one rule that is not about cost at all.  A product waiting for a person's
decision is **shown**, in its own words, whether or not the polish has run.
Hiding the body of a gate draft behind "正文正在检查文字表达，完成后会显示"
until a translation lands means the owner is asked to approve twelve answers
they cannot see; the honest version of that page shows the answers and says the
wording has not been polished yet.  Deciding on unpolished prose is fine.
Deciding on hidden prose is not a decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

SCHEMA_VERSION = "research-language-policy:0.1"
POLICY_FILE_NAME = "research-language-policy.json"

# The stages a product passes through, named by what the reader's relationship
# to it is.  Not by which lane produced it: the question "should this be
# polished" has the same answer for a dossier and for a gate draft, and it
# depends entirely on whether a person is about to read it as finished work.
STAGES: tuple[str, ...] = (
    # A version on its way to another version.  Nobody reads this on purpose.
    "intermediate",
    # A version sitting at a human checkpoint.  A person reads it, and decides
    # on it -- so it is *shown*, always, polished or not.
    "pending_decision",
    # A version a person declined.  It stays readable and is never polished:
    # paying to make rejected work read nicely is the clearest possible waste.
    "rejected",
    # The version that goes out: approved, exported, delivered, briefed.
    "approved_final",
)
DEFAULT_SCOPE = "approved_final"
SCOPES: tuple[str, ...] = ("off", "approved_final", "all")
# Which stages each scope covers.
SCOPE_STAGES: Mapping[str, frozenset[str]] = MappingProxyType({
    "off": frozenset(),
    "approved_final": frozenset({"approved_final"}),
    "all": frozenset(STAGES),
})

# What the page says when a body is shown before its polish has run.  Beside
# the text rather than instead of it.
UNPOLISHED_NOTE = "文字表达尚未润色，内容以研究系统的原话呈现。"


class ResearchLanguagePolicyError(ValueError):
    """The language policy file is not a language policy."""


def default_policy() -> dict[str, Any]:
    """What an installation gets when it has said nothing.

    ``required`` stays in the shape for every existing reader of this file, and
    it is ``True``: the thing an owner asked for -- deliverables that read well
    -- is still on.  What changed is ``scope``, and with it the answer to "how
    many versions does that cost".
    """

    return {"schema_version": SCHEMA_VERSION, "required": True,
            "scope": DEFAULT_SCOPE}


def normalise(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """One policy, with the legacy bare ``required`` read as the new default.

    A file that says only ``{"required": true}`` -- which is every installation
    shipped before this -- means ``approved_final`` now.  That is deliberately a
    change in meaning rather than a change the owner has to make: the old
    meaning is what cost half the budget, nobody chose it on purpose, and the
    installation that did want it can say ``"scope": "all"`` in one line.
    """

    out = default_policy()
    if value is None:
        return out
    if not isinstance(value, Mapping):
        raise ResearchLanguagePolicyError("语言审查策略必须是一个对象")
    allowed = {"schema_version", "required", "scope"}
    unknown = set(value) - allowed
    if unknown:
        raise ResearchLanguagePolicyError(
            "语言审查策略里有无法识别的项：" + "、".join(sorted(unknown)))
    if "schema_version" in value:
        if not isinstance(value["schema_version"], str):
            raise ResearchLanguagePolicyError("语言审查策略的 schema_version 必须是文字")
        out["schema_version"] = value["schema_version"]
    if "required" in value:
        if not isinstance(value["required"], bool):
            raise ResearchLanguagePolicyError("语言审查策略的 required 必须是是/否")
        out["required"] = value["required"]
    if "scope" in value:
        if value["scope"] not in SCOPES:
            raise ResearchLanguagePolicyError(
                "语言审查策略的 scope 只能是 " + "、".join(SCOPES))
        out["scope"] = value["scope"]
    if not out["required"]:
        out["scope"] = "off"
    return out


def load_policy(state_dir: Path | str | None) -> dict[str, Any]:
    """The policy beside the Core, or the default.

    An unreadable file is the default rather than an error, and this is the one
    place in this work package where that is the right call: language review is
    presentation.  A Core that cannot read its polish setting should still
    publish research.
    """

    if state_dir is None:
        return default_policy()
    path = Path(state_dir).expanduser() / POLICY_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_policy()
    try:
        return normalise(raw)
    except ResearchLanguagePolicyError:
        return default_policy()


def review_required_for(policy: Mapping[str, Any] | None, stage: str) -> bool:
    """Should this exact version be translated and language-checked?

    Fail-open rather than fail-closed, and on purpose: the failure mode of a
    wrong ``True`` here is a work order nobody needed, and the failure mode of
    a wrong ``False`` is prose that reads slightly worse.  Neither is a
    correctness claim about the research.
    """

    if stage not in STAGES:
        raise ResearchLanguagePolicyError(
            f"{stage!r} 不是产物的阶段；阶段只有 " + "、".join(STAGES))
    rules = normalise(policy) if not isinstance(policy, dict) or set(policy) != {
        "schema_version", "required", "scope"} else dict(policy)
    return stage in SCOPE_STAGES[str(rules["scope"])]


def hide_body_until_reviewed(policy: Mapping[str, Any] | None, stage: str) -> bool:
    """May a page hide a body because its language check has not run?

    Never at a human checkpoint, whatever the policy says.  This is the whole
    of D5's second half: the approvals page currently replaces the twelve
    answers of a gate draft with one sentence about text formatting, and asks
    the owner to approve them.
    """

    if stage == "pending_decision":
        return False
    return review_required_for(policy, stage)


__all__ = [
    "DEFAULT_SCOPE",
    "POLICY_FILE_NAME",
    "SCHEMA_VERSION",
    "SCOPES",
    "SCOPE_STAGES",
    "STAGES",
    "UNPOLISHED_NOTE",
    "ResearchLanguagePolicyError",
    "default_policy",
    "hide_body_until_reviewed",
    "load_policy",
    "normalise",
    "review_required_for",
]
