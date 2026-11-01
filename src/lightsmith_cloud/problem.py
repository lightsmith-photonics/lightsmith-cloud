"""Problem parsing across library versions.

lightsmith accepts several problem kinds (``scattering``, ``layer_stack``,
``periodic``, ``mode``, ``device``, ``bands``, ``circuit``) through
``lightsmith.problems.parse_problem``; a spec without ``kind`` is a v0.1
scattering ``Simulation``. Older library versions only have ``Simulation``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel

from .lib import osc, problems

if problems is not None:
    PROBLEM_KINDS = problems.PROBLEM_KINDS
    parse_problem = problems.parse_problem
    problem_json_schema = problems.problem_json_schema
else:  # library 0.1: scattering only
    PROBLEM_KINDS = ("scattering",)

    def parse_problem(obj: Any) -> BaseModel:
        return osc.Simulation.model_validate(obj)

    def problem_json_schema() -> dict:
        return osc.json_schema()


def kind_of(problem: BaseModel) -> str:
    return getattr(problem, "kind", "scattering")


def n_wavelengths(problem: BaseModel) -> int:
    w = getattr(problem, "wavelengths", None)
    if w is None:
        return 1
    if isinstance(w, osc.WavelengthRange):
        return w.num
    return len(np.atleast_1d(w))


def backends_for(problem: BaseModel) -> list:
    kind = kind_of(problem)
    return [b for b in osc.all_backends() if kind in getattr(b, "problem_kinds", ("scattering",))]
