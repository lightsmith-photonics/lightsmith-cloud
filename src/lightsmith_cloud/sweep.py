"""Sweep jobs: one problem solved for each value of one numeric field.

The field is a JSON path into the problem (``["particles", 0, "shape", "radius_nm"]``) or into a backend's
options (``["options", "dda", "dipoles_per_diameter"]``; missing objects are created). Every swept problem is a
complete problem the library validates and solves on its own; the job runs them in one worker, in order.
"""

from __future__ import annotations

import copy
from typing import Any

MAX_VALUES = 50


def with_value(spec: dict, path: list, value: float) -> dict:
    """A copy of `spec` with `value` at `path`, creating missing objects (or lists, before an index)."""
    out = copy.deepcopy(spec)
    node: Any = out
    for i, key in enumerate(path[:-1]):
        nxt = path[i + 1]
        if isinstance(node, list):
            if not isinstance(key, int) or not -len(node) <= key < len(node):
                raise ValueError(f"sweep path {path!r}: no index {key!r} at {path[:i]!r}")
            node = node[key]
            continue
        if not isinstance(node, dict):
            raise ValueError(f"sweep path {path!r}: {path[:i]!r} is not an object")
        if not isinstance(node.get(key), (dict, list)):
            node[key] = [] if isinstance(nxt, int) else {}
        node = node[key]
    last = path[-1]
    if isinstance(node, list):
        if not isinstance(last, int) or not -len(node) <= last < len(node):
            raise ValueError(f"sweep path {path!r}: no index {last!r}")
    elif not isinstance(node, dict):
        raise ValueError(f"sweep path {path!r}: the parent is not an object")
    node[last] = value
    return out


def swept_specs(spec: dict, path: list, values: list[float]) -> list[dict]:
    if not path:
        raise ValueError("sweep path is empty")
    if not 1 <= len(values) <= MAX_VALUES:
        raise ValueError(f"a sweep takes 1 to {MAX_VALUES} values, got {len(values)}")
    return [with_value(spec, path, v) for v in values]
