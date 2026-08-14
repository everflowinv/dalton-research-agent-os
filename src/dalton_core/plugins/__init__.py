"""Built-in Dalton service plugins.

Plugins consume disposable projections or service events.  They never receive
writer credentials and are not allowed to mutate authority databases.
"""

from .static_dashboard import StaticDashboardPlugin

__all__ = ["StaticDashboardPlugin"]
