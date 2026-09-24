"""Derive a model family only when the lineage is certain.

Why this exists
---------------
Family is a safety property. ``model_router.independent_families`` treats two
profiles as independent exactly when their families differ and neither is
``unclassified:*``; the document lane and the publication worker refuse to run
when a verifier cannot be independent of every producer. Before this module,
the catalog projection reset a curated profile's family to
``unclassified:<provider>`` whenever its broker route changed at all -- so the
routine ``claude-opus-5`` -> ``claude-opus-5-5`` point upgrade (same gateway,
same Claude 5 generation) made every Claude producer "of unknown lineage" and
the verifier preflight failed everywhere.

The opposite mistake is worse, so the rules are narrow:

* A wrong *same* family is merely strict (two models that could have been
  independent are not).  A wrong *different* family is a false independence:
  a verifier would be allowed to check its own lineage.  Assigning a new model
  to its predecessor's family is exactly that risk when the new model belongs
  to a different generation, so the predecessor's family is never simply
  carried over.  The family is always derived from the *new* route.

Derivation rules, in order (the first that applies decides)
-------------------------------------------------------------
1. An owner metadata declaration for the exact provider/model (handled by the
   caller; it always wins).
2. The profile's curated route is unchanged: its curated family (caller).
3. Exact curated route: some curated endpoint in ``model_deployment`` has the
   identical ``provider`` and ``model``; every such endpoint must agree on one
   family.  (E.g. ``antigravity-cli-gateway/gemini-3.8-flash`` is curated as
   ``google-gemini-3``, so a second profile on that route is too.)
4. Lineage rule: exactly one rule in ``LINEAGE_RULES`` matches, where a rule is
   ``(family, providers, pattern)``: the provider must be one the curated
   catalog already uses for that family, and the *whole* model id must match
   the pattern (``re.fullmatch``, case-sensitive).  Patterns only admit ids of
   the same generation the curated family names -- the Claude 5 generation,
   OpenAI's ``gpt-5.5`` / ``gpt-5.6`` / ``gpt-6`` (distinct families, as the
   curated catalog keeps them), Gemini 3.x, Grok 4.x, GLM 5.2 / 5.3, ...  Any
   unexpected suffix (a date stamp, an unknown variant, an upper-case letter)
   does not match.  If two rules match with different families, that is
   ambiguity and yields no family.
5. Otherwise ``unclassified:<provider>`` -- never independent -- until the owner
   declares the family on the models page.

Every family a rule can produce is a family the curated catalog already uses,
and the tests check that every curated endpoint derives its own curated family.
"""

from __future__ import annotations

import re
from typing import Iterable

from .model_deployment import _ENDPOINTS

_CLAUDE_GATEWAYS = frozenset({"claude-cli-gateway"})
_GOOGLE_PROVIDERS = frozenset({"google", "antigravity-cli-gateway"})

# (family, providers, full-match pattern over the provider's model id)
LINEAGE_RULES: tuple[tuple[str, frozenset[str], re.Pattern[str]], ...] = (
    # Claude 5 generation: claude-opus-5, claude-opus-5-5, claude-fable-5-1 ...
    ("anthropic-claude-5", _CLAUDE_GATEWAYS,
     re.compile(r"claude-(?:opus|sonnet|fable|haiku)-5(?:-[0-9]{1,2})?")),
    # OpenAI: the curated catalog keeps each GPT release its own family.
    ("openai-gpt-5.5", frozenset({"openai"}), re.compile(r"gpt-5\.5(?:-[a-z]+)?")),
    ("openai-gpt-5.6", frozenset({"openai"}), re.compile(r"gpt-5\.6(?:-[a-z]+)?")),
    ("openai-gpt-6", frozenset({"openai"}), re.compile(r"gpt-6(?:-[a-z]+)?")),
    # Gemini 3.x (not the moving "gemini-flash-latest" alias, which is curated
    # as its own family and only matched exactly by rule 3).
    ("google-gemini-3", _GOOGLE_PROVIDERS,
     re.compile(r"gemini-3(?:\.[0-9]{1,2})?(?:-(?:pro|flash|lite|preview))*")),
    ("xai-grok-4", frozenset({"xai"}),
     re.compile(r"grok-4(?:\.[0-9]{1,2})?(?:-(?:beta|latest|reasoning|non))*")),
    ("xai-grok-build", frozenset({"xai"}), re.compile(r"grok-build-0(?:\.[0-9]{1,2})?")),
    ("zhipu-glm-5.3", frozenset({"zai"}), re.compile(r"glm-5\.3(?:-flash)?")),
    ("zhipu-glm-5.2", frozenset({"qwen"}), re.compile(r"glm-5\.2")),
    ("qwen-3.8", frozenset({"qwen"}), re.compile(r"qwen3\.8-(?:max|plus|flash)")),
    ("deepseek-v4", frozenset({"deepseek", "qwen"}),
     re.compile(r"deepseek-v4-(?:flash|pro)(?:-[0-9]{4})?")),
)


def _curated_routes(
    endpoints: Iterable[dict] = _ENDPOINTS,
) -> dict[tuple[str, str], set[str]]:
    routes: dict[tuple[str, str], set[str]] = {}
    for endpoint in endpoints:
        routes.setdefault(
            (endpoint["provider"], endpoint["model"]), set()
        ).add(endpoint["family"])
    return routes


_CURATED_ROUTES = _curated_routes()
_CURATED_FAMILIES = frozenset(
    family for families in _CURATED_ROUTES.values() for family in families
)


def derive_model_family(provider: str, model: str) -> str | None:
    """The certain family of ``provider/model``, or ``None`` if not certain."""

    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    exact = _CURATED_ROUTES.get((provider, model))
    if exact is not None:
        return next(iter(exact)) if len(exact) == 1 else None
    matched = {
        family
        for family, providers, pattern in LINEAGE_RULES
        if provider in providers and pattern.fullmatch(model)
    }
    if len(matched) != 1:
        return None
    family = next(iter(matched))
    # Belt and braces: a rule may only ever name a family the owner curated.
    return family if family in _CURATED_FAMILIES else None


def family_for_route(provider: str, model: str) -> str:
    """Derived family, or the never-independent ``unclassified:<provider>``."""

    return derive_model_family(provider, model) or f"unclassified:{provider}"


__all__ = ["LINEAGE_RULES", "derive_model_family", "family_for_route"]
