"""Cooperative checkpoints for unpaid, discardable research display reads."""
from __future__ import annotations


class PreparationQuiesced(RuntimeError):
    """Discard an incomplete read; never interrupt a paid prepare operation."""


def checkpoint(stop_requested):
    if stop_requested is not None and stop_requested():
        raise PreparationQuiesced('cooperative stop during unpaid preparation')
