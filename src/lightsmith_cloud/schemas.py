"""Request and response models of the HTTP API (see API.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


# Job kinds. "optimize" is a solve of an `optimization` problem (inverse design, method "inverse");
# it streams one progress event per iteration. A plain "solve" of such a problem does the same.
JOB_KINDS = ("solve", "compare", "estimate", "optimize", "sweep")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


_SPEC = AliasChoices("simulation", "spec")


SPEC_DOC = ("Any lightsmith problem as JSON (see GET /v1/schema). Without `kind` it is a scattering "
            "Simulation; other kinds: layer_stack, periodic, mode, device, bands, circuit, optimization.")


class EstimateRequest(_In):
    simulation: dict[str, Any] = Field(description=SPEC_DOC + " Alias: `spec`.", validation_alias=_SPEC)
    method: str = Field("auto", max_length=64)
    device: str = Field("auto", max_length=16)


class DiscretizationRequest(_In):
    simulation: dict[str, Any] = Field(description=SPEC_DOC + " Alias: `spec`.", validation_alias=_SPEC)
    method: str = Field("auto", max_length=64, description="Method or backend name, or 'auto' (as for solve).")


class SweepParams(_In):
    path: list[str | int] = Field(min_length=1, max_length=16, description="JSON path of the swept number, e.g. "
                                  "[\"particles\", 0, \"shape\", \"radius_nm\"] or [\"options\", \"dda\", \"dipoles_per_diameter\"] "
                                  "(missing objects are created).")
    values: list[float] = Field(min_length=1, max_length=50, description="The values, solved in this order.")


class SupportsRequest(_In):
    simulation: dict[str, Any] = Field(description=SPEC_DOC + " Alias: `spec`.", validation_alias=_SPEC)


class JobRequest(_In):
    simulation: dict[str, Any] = Field(description=SPEC_DOC + " Alias: `spec`.", validation_alias=_SPEC)
    kind: Literal["solve", "compare", "estimate", "optimize", "sweep"] = Field(
        "solve", validation_alias=AliasChoices("kind", "operation"),
        description="solve | compare | estimate (run as a job) | optimize (an optimization problem) | sweep "
                    "(one solve per value of `sweep.path`). Alias: `operation`.")
    method: str | None = Field("auto", max_length=64, description="solve: method or backend name, or 'auto'.")
    methods: list[str] | None = Field(None, max_length=16, description="compare: methods (default: all applicable).")
    reference: str | None = Field(None, max_length=64, description="compare: reference method (default: Mie if run).")
    device: str = Field("auto", max_length=16, description="auto | cpu | gpu | cuda:N | torch-cpu")
    label: str | None = Field(None, max_length=200, description="Free text shown in job lists.")
    sweep: SweepParams | None = Field(None, description="sweep: the swept field and its values.")


class ErrorBody(BaseModel):
    error: str = Field(description="Error type, e.g. ValidationError, Timeout, MemoryLimitExceeded, Cancelled.")
    message: str


class JobCreated(BaseModel):
    job_id: str
    id: str = Field(description="Same as job_id.")
    status: JobStatus
    credits_estimate: float | None = None
    queue: str | None = None
    quota_warning: str | None = Field(None, description="Hosted: set when this job takes the org past the soft "
                                      "limit of its monthly credits or into billed overage.")


class Usage(BaseModel):
    runtime_s: float | None = None
    gpu_seconds: float | None = None
    credits: float | None = None


class JobView(BaseModel):
    job_id: str
    id: str = Field(description="Same as job_id.")
    kind: str = Field(description="solve | compare | estimate | optimize | sweep.")
    operation: str = Field(description="Same as kind.")
    sweep: dict[str, Any] | None = Field(None, description="sweep jobs: {path, values}.")
    problem_kind: str | None = Field(None, description="scattering | layer_stack | periodic | mode | device | bands | "
                                     "circuit | optimization.")
    status: JobStatus
    progress: float = Field(description="0..1, coarse (started, per-method done).")
    message: str | None = Field(None, description="Short human-readable state, or the error message.")
    stage: str | None = None
    method: str | None = None
    methods: list[str] | None = None
    reference: str | None = None
    device: str
    label: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: ErrorBody | None = None
    warnings: list[str] | None = None
    device_used: dict[str, Any] | None = None
    estimate: dict[str, Any] | None = None
    usage: Usage
    result: dict[str, Any] | None = Field(None, description="Result JSON (solve) or compare JSON; null until "
                                          "succeeded, or when larger than the inline limit (then use result_url).")
    result_kind: str | None = Field(None, description="The result model's `kind` (cross_sections, spectrum, modes, "
                                    "sparameters, bands, optimization, ...), or compare / estimate.")
    result_url: str | None = None
    result_bytes: int | None = None
    simulation: dict[str, Any] | None = Field(None, description="The validated spec, with include_spec=1.")


class JobList(BaseModel):
    jobs: list[JobView]
    next_before: datetime | None = Field(None, description="Pass as ?before= for the next page.")


class KeyCreate(_In):
    name: str = Field("", max_length=200)


class CheckoutRequest(_In):
    plan: Literal["pro", "team"]
    success_url: str = Field(max_length=2000)
    cancel_url: str = Field(max_length=2000)


class PlanChange(_In):
    plan: Literal["pro", "team"]


class CreditLimit(_In):
    monthly_credit_limit: int | None = Field(None, ge=0, le=10**9, description="Most credits the org may use per "
                                             "month (null: the plan's hard limit).")


class CapOverride(_In):
    credit_cap_override: int | None = Field(None, ge=0, le=10**10, description="Replaces the plan's monthly hard "
                                            "limit for this org (null: back to the plan's).")


class PortalRequest(_In):
    return_url: str = Field(max_length=2000)


class PlanUpdate(_In):
    plan: str = Field(max_length=32)
