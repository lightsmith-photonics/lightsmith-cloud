"""The one place the scientific library (`lightsmith`) is imported.

The rest of the service uses ``osc`` from here, so a future library move or
rename stays a change to this file and the dependency in pyproject.toml.
"""

import lightsmith as osc

try:
    from lightsmith import problems
except ImportError:  # library versions before multi-kind problems
    problems = None


def inverse_optimizer():
    """``lightsmith.inverse.topopt.optimize`` (per-iteration callbacks), or None if unavailable.
    Imported lazily: it is only needed inside solver child processes."""
    try:
        from lightsmith.inverse.topopt import optimize
    except ImportError:
        return None
    return optimize


__all__ = ["osc", "problems", "inverse_optimizer"]
