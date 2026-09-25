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
from decimal import Decimal
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


# 2026-09-24: what a CLI-gateway call costs that its prompt does not show.
# A ``*-cli-gateway`` provider runs a vendor CLI behind the broker, and the CLI
# wraps Dalton's prompt in its own system prompt and tool definitions on every
# call.  Measured from the live broker journal (2026-09-24, last 1,000 calls):
#
# * claude-cli-gateway: cacheWriteTokens ~= 24,300 + 0.4 x prompt bytes
#   (25,534 at 3,068 bytes; 33,924 at 23,686 bytes), plus a constant 2,991
#   cacheReadTokens and 2 uncached input tokens.  Metered cost 0.2739862 USD
#   for 32,250 written + 2,991 read + 769 output tokens on a 4 / 20 USD per
#   million card: the cache write is billed at twice the input rate (the
#   one-hour cache-write price), within 0.3 %.
# * antigravity-cli-gateway: ~13,500 input tokens beyond the prompt.
# * muse-cli-gateway: ~26,000 input tokens beyond the prompt.
#
# The profile schema is closed and content-hashed and has no field for this,
# so the provider name -- already on every profile -- selects the overhead and
# these constants carry it: 32,000 tokens covers the largest fixed part seen
# (claude, ~27,300 including the cache read) with headroom.  Defined here, in
# the module both the router and the adapter already read, so the cockpit's
# cost ceiling and every WorkOrder's token ceiling use the one number.
CLI_GATEWAY_PROVIDER_SUFFIX = "-cli-gateway"
CLI_GATEWAY_SYSTEM_PROMPT_TOKENS = 32_000
CLI_GATEWAY_CACHE_WRITE_MULTIPLIER = Decimal(2)
# 2026-09-25: how many provider tokens one prompt byte is taken to cost when a
# CLI-gateway call is sized *before* it is sent.  Measured: 0.41 on the claude
# gateway (the slope above) and 0.34 on the refused mission document draft
# (122,918 bytes; 65,831 written + 2,991 read + 2 input tokens, less the
# ~27,300 fixed part).  One half is conservative for both.
CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_NUMERATOR = 1
CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_DENOMINATOR = 2
#: The route rejection reason for a CLI-gateway profile whose call, sized with
#: its hidden prefix, would not fit the WorkOrder's own token budget.
CLI_GATEWAY_BUDGET_SKIP_REASON = "cli_gateway_token_budget_exceeded"


def is_cli_gateway_profile(profile: Mapping[str, Any]) -> bool:
    """True when the profile is served by a vendor CLI behind the broker."""

    provider = profile.get("provider") if isinstance(profile, Mapping) else None
    return isinstance(provider, str) and provider.endswith(CLI_GATEWAY_PROVIDER_SUFFIX)


def gateway_overhead_tokens(profile: Mapping[str, Any]) -> int:
    """Provider tokens this profile spends on every call beyond Dalton's prompt."""

    return CLI_GATEWAY_SYSTEM_PROMPT_TOKENS if is_cli_gateway_profile(profile) else 0


def provider_token_ceilings(
    limits: Mapping[str, Any], profile: Mapping[str, Any],
) -> dict[str, int]:
    """The token ceilings provider telemetry is held to on this profile.

    ``limits`` is a WorkOrder budget or a profile's own limits: both size
    *Dalton's* prompt.  What the provider reports -- input including cache
    reads and writes, and a total that includes them -- also carries the CLI
    gateway's hidden prefix, so the input and total ceilings are raised by
    exactly that prefix and nothing else.  The output ceiling is unchanged:
    the gateway adds no output.
    """

    overhead = gateway_overhead_tokens(profile)
    return {
        "max_input_tokens": int(limits["max_input_tokens"]) + overhead,
        "max_output_tokens": int(limits["max_output_tokens"]),
        "max_total_tokens": int(limits["max_total_tokens"]) + overhead,
    }


def cli_gateway_preflight_tokens(
    profile: Mapping[str, Any], prompt: str, max_output_tokens: int,
) -> dict[str, int] | None:
    """Size one CLI-gateway call before it is sent, in provider tokens.

    ``None`` for any other profile: their telemetry carries no hidden prefix,
    and the caller's own estimate already governs them.
    """

    if not is_cli_gateway_profile(profile):
        return None
    prompt_tokens = -(-actual_prompt_bytes(prompt)
                      * CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_NUMERATOR
                      // CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_DENOMINATOR)
    provider_input = prompt_tokens + CLI_GATEWAY_SYSTEM_PROMPT_TOKENS
    return {
        "input_tokens": provider_input,
        "total_tokens": provider_input + int(max_output_tokens),
    }


def cli_gateway_budget_refusal(
    profile: Mapping[str, Any], prompt: str, max_output_tokens: int,
    budgets: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Why a CLI-gateway call would break one of ``budgets``, or ``None``.

    ``budgets`` maps a name ("WorkOrder", "profile") to token limits.  The
    estimate and the ceiling are measured the way the adapter measures the
    provider's telemetry after the call, so a call this admits is not one the
    adapter pays for and then refuses on its size alone.
    """

    estimate = cli_gateway_preflight_tokens(profile, prompt, max_output_tokens)
    if estimate is None:
        return None
    for source, limits in budgets.items():
        ceilings = provider_token_ceilings(limits, profile)
        if estimate["input_tokens"] > ceilings["max_input_tokens"]:
            return (
                f"CLI gateway call is estimated at {estimate['input_tokens']} provider "
                f"input tokens, above the {source} ceiling of "
                f"{ceilings['max_input_tokens']} including the gateway prefix"
            )
        if estimate["total_tokens"] > ceilings["max_total_tokens"]:
            return (
                f"CLI gateway call is estimated at {estimate['total_tokens']} provider "
                f"total tokens, above the {source} ceiling of "
                f"{ceilings['max_total_tokens']} including the gateway prefix"
            )
    return None


__all__ = [
    "CLI_GATEWAY_BUDGET_SKIP_REASON",
    "CLI_GATEWAY_CACHE_WRITE_MULTIPLIER",
    "CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_DENOMINATOR",
    "CLI_GATEWAY_PROMPT_TOKENS_PER_BYTE_NUMERATOR",
    "CLI_GATEWAY_PROVIDER_SUFFIX",
    "CLI_GATEWAY_SYSTEM_PROMPT_TOKENS",
    "cli_gateway_budget_refusal",
    "cli_gateway_preflight_tokens",
    "gateway_overhead_tokens",
    "is_cli_gateway_profile",
    "provider_token_ceilings",
    "INPUT_BOUND_SKIP_REASON",
    "MEASURED_INPUT_BOUNDS",
    "actual_prompt_bytes",
    "effective_input_bound",
    "exceeds_actual_prompt_bound",
    "exceeds_input_bound",
    "input_bound_message",
    "measured_input_bound",
]
