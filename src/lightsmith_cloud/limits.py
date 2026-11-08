"""Validate and bound user specs before anything runs.

Specs are data: they are parsed by lightsmith's pydantic models (which
forbid unknown fields) and never evaluated. Any problem kind is accepted. On top of that, these checks cap
the sizes that drive memory and run time, so that a small JSON body cannot
ask for a huge computation, and reject fields that would make the server read
files or reach the network.
"""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
from .lib import osc
from pydantic import BaseModel

from .config import Settings
from .errors import ApiError
from .problem import PROBLEM_KINDS, backends_for, kind_of, n_wavelengths, parse_problem

DEVICE_RE = re.compile(r"^(auto|cpu|gpu|cuda(:\d{1,2})?|torch-cpu)$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _too_large(msg: str) -> ApiError:
    return ApiError(422, "SpecTooLarge", msg)


def parse_simulation(raw: Any) -> BaseModel:
    """Validate a spec of any problem kind with lightsmith's own models."""
    if not isinstance(raw, dict):
        raise ApiError(422, "ValidationError", "simulation must be a JSON object (see GET /v1/schema)")
    try:
        return parse_problem(raw)
    except Exception as e:  # pydantic.ValidationError, or a material lookup error
        details = None
        if hasattr(e, "errors"):
            details = [{"loc": [str(x) for x in err.get("loc", ())], "msg": err.get("msg"), "type": err.get("type")}
                       for err in e.errors(include_url=False, include_input=False)][:50]
        raise ApiError(422, "ValidationError", str(e)[:4000], details=details) from None


def _count(x: Any, depth: int = 0) -> int:
    if depth > 40:
        raise ApiError(422, "SpecTooLarge", "spec is nested too deeply")
    if isinstance(x, dict):
        return 1 + sum(_count(v, depth + 1) for v in x.values())
    if isinstance(x, list):
        return 1 + sum(_count(v, depth + 1) for v in x)
    return 1


def check_limits(problem: BaseModel, settings: Settings, spec: dict | None = None) -> None:
    """Size checks for every problem kind, plus detailed ones for scattering specs."""
    s = settings
    spec = spec if spec is not None else problem.model_dump(mode="json")
    if _count(spec) > s.max_spec_elements:
        raise _too_large(f"spec has more than {s.max_spec_elements} JSON values")
    n_wl = n_wavelengths(problem)
    if n_wl > s.max_wavelengths:
        raise _too_large(f"{n_wl} wavelengths; the limit is {s.max_wavelengths}")
    _check_options(getattr(problem, "options", {}) or {}, s)
    if isinstance(problem, osc.Simulation):
        _check_scattering(problem, s)
    elif kind_of(problem) == "emission":
        _check_emission(problem, s)


def _check_material(m, s: Settings) -> None:
    if isinstance(m, osc.Tabulated) and len(m.wavelength_nm) > s.max_tabulated_points:
        raise _too_large(f"tabulated material with {len(m.wavelength_nm)} points; "
                         f"the limit is {s.max_tabulated_points}")


def _check_particles(particles: list, s: Settings) -> None:
    if len(particles) > s.max_particles:
        raise _too_large(f"{len(particles)} particles; the limit is {s.max_particles}")
    for p in particles:
        shells = getattr(p.shape, "shells", [])
        if len(shells) > s.max_shells:
            raise _too_large(f"{len(shells)} shells on one particle; the limit is {s.max_shells}")
        for m in p.region_materials():
            _check_material(m, s)


def _check_emission(problem: BaseModel, s: Settings) -> None:
    _check_particles(problem.particles, s)
    rp = problem.radiation_pattern
    if rp is not None:
        n = len(rp.theta_deg) * len(rp.phi_deg) * len(rp.wavelengths_nm)
        if n > s.max_far_field_samples:
            raise _too_large(f"radiation pattern asks for {n} samples; the limit is {s.max_far_field_samples}")


def _check_scattering(sim: osc.Simulation, s: Settings) -> None:
    _check_particles(sim.particles, s)
    if sim.substrate is not None:
        _check_material(sim.substrate.material, s)
    nf, ff = sim.outputs.near_field, sim.outputs.far_field
    if nf is not None and nf.points**2 * len(nf.wavelengths_nm) > s.max_near_field_samples:
        raise _too_large(f"near field asks for {nf.points}^2 x {len(nf.wavelengths_nm)} samples; "
                         f"the limit is {s.max_near_field_samples}")
    if ff is not None:
        n = len(ff.theta_deg) * len(ff.phi_deg) * len(ff.wavelengths_nm)
        if n > s.max_far_field_samples:
            raise _too_large(f"far field asks for {n} samples; the limit is {s.max_far_field_samples}")
    _check_lattice(sim, s)


def _ints(v: Any) -> list[int]:
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [int(v)]
    if isinstance(v, list):
        return [int(x) for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return []


def _npd_floor(sim: osc.Simulation) -> int:
    """Smallest dipoles-per-diameter any DDA run of this spec would use (the library's floor is 16)."""
    npd = 16
    for opts in sim.options.values():
        for key in ("dipoles_per_diameter", "extrapolate"):
            npd = max([npd, *_ints(opts.get(key))])
    return npd


def _check_options(options: dict, s: Settings) -> None:
    for key, opts in options.items():
        if not NAME_RE.match(key):
            raise ApiError(422, "ValidationError", f"invalid options key {key!r}")
        for name in ("dipoles_per_diameter", "extrapolate"):
            vals = _ints(opts.get(name))
            if vals and max(vals) > s.max_dipoles_per_diameter:
                raise _too_large(f"options.{key}.{name} = {max(vals)}; the limit is {s.max_dipoles_per_diameter}")
        if len(_ints(opts.get("extrapolate"))) > 6:
            raise _too_large(f"options.{key}.extrapolate has more than 6 entries")
        if _ints(opts.get("orientations")) and max(_ints(opts.get("orientations"))) > 4096:
            raise _too_large(f"options.{key}.orientations is above 4096")


def _check_lattice(sim: osc.Simulation, s: Settings) -> None:
    """Lower bound on the DDA voxel grid from the bounding box; estimate() itself allocates it."""
    lo = np.min([np.asarray(p.position_nm) - p.shape.bounding_half_extent() for p in sim.particles], axis=0)
    hi = np.max([np.asarray(p.position_nm) + p.shape.bounding_half_extent() for p in sim.particles], axis=0)
    diam = min(2 * p.shape.bounding_half_extent().max() for p in sim.particles)
    d0 = diam / _npd_floor(sim)
    cells = math.prod(max(1, round(float(x) / d0)) for x in (hi - lo))
    if cells > s.max_grid_cells:
        raise _too_large(f"the particles span a volume of at least {cells:.3g} lattice cells at the minimum DDA "
                         f"resolution; the limit is {s.max_grid_cells:.3g}. Move particles closer together or "
                         "split the problem")


def check_device(device: str) -> str:
    device = (device or "auto").lower()
    if not DEVICE_RE.match(device):
        raise ApiError(422, "ValidationError", f"unknown device {device!r}; use auto, cpu, gpu, cuda:N or torch-cpu")
    return device


def resolve_methods(problem: BaseModel, kind: str, method: str | None, methods: list[str] | None,
                    settings: Settings) -> tuple[str | None, list[str] | None]:
    """Check method names and that a backend can run the problem; returns (method, methods)."""
    pkind = kind_of(problem)

    def check(name: str) -> None:
        if not NAME_RE.match(name):
            raise ApiError(422, "ValidationError", f"invalid method name {name!r}")
        try:
            b = osc.get_backend(name, kind=pkind) if PROBLEM_KINDS != ("scattering",) else osc.get_backend(name)
        except KeyError as e:
            raise ApiError(422, "UnknownMethod", str(e).strip("'\"")) from None
        if pkind not in getattr(b, "problem_kinds", ("scattering",)):
            raise ApiError(422, "UnsupportedProblem",
                           f"backend {b.name!r} solves {list(b.problem_kinds)}, not {pkind!r}")
        for ok, why in (b.available(), b.supports(problem)):
            if not ok:
                raise ApiError(422, "UnsupportedProblem", f"backend {b.name!r} cannot run this problem: {why}")

    if kind == "solve":
        method = method or "auto"
        if method == "auto":
            try:
                osc.choose_method(problem)
            except ValueError as e:
                raise ApiError(422, "UnsupportedProblem", str(e)) from None
        else:
            check(method)
        return method, None
    if not methods:
        methods = [b.name for b in backends_for(problem) if b.available()[0] and b.supports(problem)[0]]
        if not methods:
            raise ApiError(422, "UnsupportedProblem", f"no available backend supports this {pkind} problem")
    if len(methods) > settings.max_compare_methods:
        raise _too_large(f"{len(methods)} methods; the limit is {settings.max_compare_methods}")
    for m in methods:
        check(m)
    return None, list(methods)
