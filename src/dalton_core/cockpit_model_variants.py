"""Public model-choice metadata from exact registered and broker identities.

This is display projection, not routing authority. A broker thinking preset is
not a claim about a call: explicit per-purpose controls take precedence.
"""
from collections.abc import Mapping
from typing import Any
from .call_budget import THINKING_LEVELS


def broker_choice_metadata(config: Any, catalogue: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    def mapping(value):
        return value if isinstance(value, Mapping) else {}
    plugins = mapping(mapping(config).get('plugins'))
    entry = mapping(mapping(plugins.get('entries')).get('dalton-openclaw-model-broker'))
    profiles = mapping(entry.get('config')).get('profiles')
    if not isinstance(profiles, list):
        return {}
    result = {}
    seen = set()
    for raw in profiles:
        if not isinstance(raw, Mapping) or not isinstance(raw.get('id'), str):
            continue
        identity = raw['id']
        if identity in seen:
            result.pop(identity, None)
            continue
        seen.add(identity)
        registered = catalogue.get(identity)
        if not isinstance(registered, Mapping):
            continue
        provider, model = registered.get('provider'), registered.get('model')
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        canonical = f'{provider}/{model}'
        if raw.get('model') != canonical:
            continue
        direct = raw.get('thinkingLevel')
        controlled = mapping(raw.get('providerControls')).get('thinkingLevel')
        if (direct is not None and (not isinstance(direct, str) or direct not in THINKING_LEVELS)
                or controlled is not None and (not isinstance(controlled, str) or controlled not in THINKING_LEVELS)
                or direct is not None and controlled is not None and direct != controlled):
            continue
        value = {'broker_model_ref': canonical}
        preset = direct if direct is not None else controlled
        if preset is not None:
            value['broker_thinking_preset'] = preset
        result[identity] = value
    return result
