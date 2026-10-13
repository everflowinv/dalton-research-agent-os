"""Bounded in-process memoization of pure byte parsers, never authority reads.

Only immutable serialized values are retained. Pickle payloads originate from
closed builtin trees produced here, never from source bytes, files or a caller.
Restoring that private payload preserves aliases/types as deepcopy does.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import math
import pickle
import threading
from collections import OrderedDict
from pathlib import Path

# A model operation reads up to eight filings for each company, with separate
# HTML and XML results. Keep that bounded working set without cyclical eviction;
# serialized bytes, rather than entry count alone, set the memory ceiling.
MAX_ENTRIES = 256
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_VALUE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
_lock = threading.RLock()
_entries = OrderedDict()
_bytes = 0
_hits = _misses = 0


def clear():
    global _bytes, _hits, _misses
    with _lock:
        _entries.clear(); _bytes = _hits = _misses = 0


def cache_info():
    with _lock:
        return {"entries": len(_entries), "serialized_bytes": _bytes,
                "hits": _hits, "misses": _misses}


def _closed_tree(value):
    pending = [value]; seen = set()
    while pending:
        item = pending.pop(); kind = type(item)
        if kind in (str, int, bool, type(None)): continue
        if kind is float and math.isfinite(item): continue
        if kind not in (dict, list, tuple): return False
        if id(item) in seen: continue
        seen.add(id(item))
        if kind is dict:
            if any(type(key) is not str for key in item): return False
            pending.extend(item.values())
        else: pending.extend(item)
    return True


def cache_byte_parser(namespace, *, byte_arguments=("raw",), eligible=None):
    """Decorate a pure parser without retaining inputs, claims or permissions."""
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            global _bytes, _hits, _misses
            try:
                bound = signature.bind(*args, **kwargs); bound.apply_defaults()
            except TypeError:
                return function(*args, **kwargs)  # retain original error text
            values = bound.arguments
            if eligible is not None and not eligible(values):
                return function(*args, **kwargs)
            total = 0; inputs = []; options = []
            for name, value in values.items():
                if name in byte_arguments:
                    if value is None: inputs.append((name, None)); continue
                    if type(value) is not bytes: return function(*args, **kwargs)
                    total += len(value)
                    if total > MAX_INPUT_BYTES: return function(*args, **kwargs)
                    inputs.append((name, hashlib.sha256(value).hexdigest()))
                elif type(value) in (str, int, bool, type(None)):
                    if type(value) is str:
                        if len(value) > 4096: return function(*args, **kwargs)
                        try: token = hashlib.sha256(value.encode('utf-8')).hexdigest()
                        except UnicodeError: return function(*args, **kwargs)
                    elif type(value) is int:
                        if value.bit_length() > 64: return function(*args, **kwargs)
                        token = value
                    else: token = value
                    options.append((name, type(value).__name__, token))
                else: return function(*args, **kwargs)
            try:
                contract = hashlib.sha256(Path(function.__code__.co_filename).read_bytes()).hexdigest()
            except OSError:
                return function(*args, **kwargs)
            key = (function, namespace, contract, tuple(inputs), tuple(options))
            with _lock:
                payload = _entries.get(key)
                if payload is not None:
                    _entries.move_to_end(key); _hits += 1
                else: _misses += 1
            if payload is not None: return pickle.loads(payload)
            result = function(*args, **kwargs)  # failures never enter the cache
            if not _closed_tree(result): return result
            try:
                payload = pickle.dumps(result, protocol=5)
            except (pickle.PickleError, RecursionError, OverflowError):
                return result  # memoization cannot add a parser failure
            if len(payload) > MAX_VALUE_BYTES or len(payload) > MAX_TOTAL_BYTES:
                return result
            with _lock:
                previous = _entries.pop(key, None)
                if previous is not None: _bytes -= len(previous)
                _entries[key] = payload; _bytes += len(payload)
                while len(_entries) > MAX_ENTRIES or _bytes > MAX_TOTAL_BYTES:
                    _, removed = _entries.popitem(last=False); _bytes -= len(removed)
            return result
        return wrapped
    return decorate
