"""Cockpit v2: the analyst workbench served at ``/v2`` (ADR-0016).

The old cockpit stays at ``/``.  v2 reads the same ``/v1/cockpit/*`` payloads;
its own JSON lives under ``/v2/data/*`` only where an existing payload lacks a
field.  ``routes.register_v2_routes`` is the one place the HTTP surface learns
about v2; the administrator interface (``admin/``) and the financial charts
(``charts/``) join through their own ``routes.py`` modules.
"""
