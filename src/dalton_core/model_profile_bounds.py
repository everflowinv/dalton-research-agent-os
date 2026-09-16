"""WP-A/A3: the input a model endpoint can actually take, as opposed to claims.

A profile declares ``limits.max_input_tokens``, and routing has always refused
a candidate whose estimated input exceeds it.  That check is only as good as the
number, and the number comes from the broker catalog -- which reports the
*model's* context window, not the *transport's* ceiling.

``profile:gemini-3-8-flash-antigravity`` and its ``-high`` sibling are the live
case.  The agy CLI the broker drives them through truncates its own request
around 30,000 characters, so anything larger fails inside the gateway.  The
catalog reports 983,040.  The owner corrected the two profiles by hand on
2026-09-15 (registered at 28,000) and the next catalog sync registered a new
version at 983,040 again, because a sync writes what the catalog says.
Meanwhile debate_map and plan prompts sit at a p50 of 29,434 bytes and a p90 of
30,796: 2 of 207 calls succeeded, and until WP-A/A1 every one of the other 205
was settled at the chain's reserved ceiling.

So the measured ceiling is kept *here*, beside the code that enforces it,
rather than in a row a sync can overwrite.  The effective bound is the smaller
of the two -- a catalog that tightens a limit is believed, a catalog that
loosens one below a measured transport failure is not -- and which of the two
bound the call is carried with it, because "why was this model skipped" must
name its evidence.

Adding to :data:`MEASURED_INPUT_BOUNDS` is how a newly-measured transport
ceiling is declared.  Removing an entry is how one is retired once the gateway
is fixed; the profile's own declared limit then governs again on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The skip/rejection reason a candidate carries when the prompt is too big for
#: the endpoint's measured transport ceiling.
INPUT_BOUND_SKIP_REASON = "input_bound_exceeded"

#: profile id -> the largest input, in the same unit routing estimates in
#: (bytes of prompt, which every Dalton caller passes as
#: ``estimated_input_tokens``), that the endpoint's transport has been observed
#: to carry.  Each entry names what measured it.
MEASURED_INPUT_BOUNDS: Mapping[str, int] = {
    # agy CLI truncates its own request frame; measured 2026-09-15 against
    # debate_map/plan prompts, 2 of 207 calls above 30k succeeded.
    "profile:gemini-3-8-flash-antigravity": 30_000,
    "profile:gemini-3-8-flash-antigravity-high": 30_000,
}


def measured_input_bound(profile_id: Any) -> int | None:
    """The measured transport ceiling for this profile id, if one is known."""

    if not isinstance(profile_id, str):
        return None
    return MEASURED_INPUT_BOUNDS.get(profile_id)


def effective_input_bound(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The input ceiling routing must hold this profile to, and where it came from.

    ``source`` is ``measured`` when the observed transport ceiling is the
    binding one and ``declared`` when the profile's own limit is -- so a route
    decision's rejection can be argued with rather than merely obeyed.
    """

    declared = (profile.get("limits") or {}).get("max_input_tokens")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared <= 0:
        declared = None
    measured = measured_input_bound(profile.get("id"))
    if measured is None:
        return {"bound": declared, "source": "declared", "declared": declared,
                "measured": None}
    if declared is None or measured < declared:
        return {"bound": measured, "source": "measured", "declared": declared,
                "measured": measured}
    return {"bound": declared, "source": "declared", "declared": declared,
            "measured": measured}


def exceeds_input_bound(profile: Mapping[str, Any], estimated_input: int) -> bool:
    """Whether a prompt this size is past what this endpoint can actually take."""

    bound = effective_input_bound(profile)["bound"]
    return bound is not None and int(estimated_input) > int(bound)


def input_bound_message(profile: Mapping[str, Any], estimated_input: int) -> str:
    """One sentence, for the owner, about why a model was skipped unasked."""

    resolved = effective_input_bound(profile)
    origin = ("实测传输上限" if resolved["source"] == "measured"
              else "档案申报的输入上限")
    return (
        f"{profile.get('id')} 的{origin}是 {resolved['bound']}，本次提示词约 "
        f"{int(estimated_input)}，超出后调用必然失败，"
        "因此直接跳过该模型：不发起调用、不预留预算。"
    )


__all__ = [
    "INPUT_BOUND_SKIP_REASON",
    "MEASURED_INPUT_BOUNDS",
    "effective_input_bound",
    "exceeds_input_bound",
    "input_bound_message",
    "measured_input_bound",
]
