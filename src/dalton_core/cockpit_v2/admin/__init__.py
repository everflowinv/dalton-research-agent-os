"""Cockpit v2 administrator interface (「管理员设置」).

Everything the 2026-10-08 analyst UX review moves out of the analyst's view:
AI service chains, data connections, run control, budgets, health and
failures, research project management.  See ``routes.py`` for the contract
with the v2 shell and ``data.py`` for the read-only sections.
"""

from .routes import admin_handler, dispatch, register_admin_routes

__all__ = ["admin_handler", "dispatch", "register_admin_routes"]
