"""Generated broker capability bounds, separate from each owner's call budget.

These mirror the packaged plugin's request/config limit. Raising a capability
does not extend frozen WorkOrders or alter dollars, retries, or task defaults.
"""
MAX_COMPLETION_TIMEOUT_MS = 1_800_000
DEFAULT_PROFILE_TIMEOUT_MS = MAX_COMPLETION_TIMEOUT_MS
RETIRED_DEFAULT_PROFILE_TIMEOUTS_MS = frozenset({600_000, 900_000})
