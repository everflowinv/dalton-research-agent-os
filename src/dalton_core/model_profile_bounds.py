"""WP-A/A3: the input a model endpoint can actually take, as opposed to claims.

A profile declares ``limits.max_input_tokens``, and routing has always refused
a candidate whose estimated input exceeds it.  That check is only as good as the
number, and the number comes from the broker catalog -- which reports the
*model's* context window, not the *transport's* ceiling.

The Antigravity Flash profiles were conservatively limited to 30,000 bytes
following the 2026-09-15 failures. On 2026-09-22, agy 1.2.8 carried 175,282
UTF-8 bytes with beginning/middle/end sentinels intact at both low and high
effort. A direct 269,100-byte call still silently truncated at 191,985 bytes.
Use 170,000 for Dalton, leaving room under the gateway's 190,000-byte limit
for host framing. This is a transport bound, not a quality certification.
Evidence: docs/reports/environment-repair-2026-09-22.md.

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

#: profile id -> the largest UTF-8 prompt, in bytes, that the endpoint's
#: transport has been observed to carry.  Routing uses this as an early skip
#: when its caller supplies bytes; the OpenClaw adapter measures the actual
#: prompt again because older callers use token estimates in that field.
#: Each entry names what measured it.
MEASURED_INPUT_BOUNDS: Mapping[str, int] = {
    # Full gateway probes, agy 1.2.8, 2026-09-22; retain framing headroom.
    "profile:gemini-3-8-flash-antigravity": 170_000,
    "profile:gemini-3-8-flash-antigravity-high": 170_000,
}


def measured_input_bound(profile_id: Any) -> int | None:
    """The measured transport ceiling for this profile id, if one is known."""

    if not isinstance(profile_id, str):
        return None
    return MEASURED_INPUT_BOUNDS.get(profile_id)


def actual_prompt_bytes(prompt: str) -> int:
    """Size of the exact text the broker serializes as its prompt."""

    if not isinstance(prompt, str):
        raise TypeError("model prompt must be text")
    try:
        return len(prompt.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("model prompt must be valid UTF-8") from exc


def exceeds_actual_prompt_bound(profile: Mapping[str, Any], prompt: str) -> bool:
    """Whether the exact UTF-8 prompt exceeds this endpoint's measured bound."""

    bound = measured_input_bound(profile.get("id"))
    return bound is not None and actual_prompt_bytes(prompt) > bound


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
    "actual_prompt_bytes",
    "effective_input_bound",
    "exceeds_actual_prompt_bound",
    "exceeds_input_bound",
    "input_bound_message",
    "measured_input_bound",
]
