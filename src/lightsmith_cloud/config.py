"""Service configuration, read from the environment.

Most variables use the ``LIGHTSMITH_`` prefix. A few shared names are read as is:
``DATABASE_URL``, ``REDIS_URL``, ``PLATFORM_JWT_SECRET``, ``STRIPE_SECRET_KEY``
and ``STRIPE_WEBHOOK_SECRET``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if os.environ.get("LIGHTSMITH_HOME"):
        return Path(os.environ["LIGHTSMITH_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Lightsmith" / "server"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lightsmith" / "server"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Lightsmith" / "server"


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


@dataclass(frozen=True)
class GpuTier:
    """A class of GPU workers. Jobs go to the smallest tier whose memory fits their estimate."""

    queue: str
    memory_bytes: int
    credit_multiplier: float


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIGHTSMITH_", extra="ignore", populate_by_name=True)

    # Branding is configuration: the product name appears in the API title, docs and health output.
    product_name: str = "Lightsmith"
    hosted_product_name: str = "Lightsmith Cloud"

    mode: Literal["local", "hosted"] = "local"
    host: str = "127.0.0.1"
    port: int = 8200
    data_dir: Path = Field(default_factory=default_data_dir)

    database_url: str | None = Field(None, validation_alias=_alias("LIGHTSMITH_DATABASE_URL", "DATABASE_URL"))
    # Hosted mode runs migrations as a separate step (`lightsmith-server migrate`) and needs Postgres.
    auto_migrate: bool = False
    allow_sqlite: bool = False  # tests only: lets hosted mode run on SQLite
    redis_url: str | None = Field(None, validation_alias=_alias("LIGHTSMITH_REDIS_URL", "REDIS_URL"))

    # Comma-separated. Dev defaults: web (3000) and studio (5173).
    cors_origins: str = ("http://localhost:3000,http://127.0.0.1:3000,"
                         "http://localhost:5173,http://127.0.0.1:5173,tauri://localhost,http://tauri.localhost")
    # Local mode only accepts these Host headers (blocks DNS-rebinding attacks on the unauthenticated API).
    allowed_hosts: str = "localhost,127.0.0.1,[::1],testserver"

    # Local worker pool: concurrent solver subprocesses (1 = one job on the GPU at a time).
    local_workers: int = Field(1, ge=1, le=64)

    # Request and spec limits (hosted plans can lower them further).
    max_body_bytes: int = Field(2_000_000, ge=1_000)
    max_spec_elements: int = 200_000  # JSON values in a spec, any problem kind
    max_particles: int = 1_000
    max_wavelengths: int = 5_000
    max_tabulated_points: int = 20_000
    max_shells: int = 50
    max_near_field_samples: int = 20_000_000
    max_far_field_samples: int = 2_000_000
    max_dipoles_per_diameter: int = 512
    max_grid_cells: int = 50_000_000
    max_compare_methods: int = 8

    # Sandboxed estimate process.
    estimate_timeout_s: float = 60.0
    estimate_memory_bytes: int = 4 * 2**30

    # Local-mode job caps (0 = unlimited). Hosted mode takes them from the plan.
    job_timeout_s: float = 0.0
    job_memory_bytes: int = 0
    max_log_lines: int = 500

    inline_result_max_bytes: int = 25_000_000

    # Results: S3-compatible object storage when a bucket is set, else the local filesystem.
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_prefix: str = "results/"
    s3_create_bucket: bool = False  # create the bucket at start-up if missing (dev stacks)

    # Auth (hosted).
    platform_jwt_secret: SecretStr | None = Field(
        None, validation_alias=_alias("LIGHTSMITH_PLATFORM_JWT_SECRET", "PLATFORM_JWT_SECRET"))
    jwt_audience: str | None = "compute"  # apps/web signs compute tokens with aud "compute"; "" disables
    jwt_issuer: str | None = None
    api_key_pepper: SecretStr | None = None
    admin_token: SecretStr | None = None
    # Requests per minute for local mode (0 = off). Hosted mode uses the plan's limit.
    rate_limit_per_minute: int = 0
    anonymous_rate_limit_per_minute: int = 120

    # Hosted GPU tiers, "queue:GiB:credit_multiplier,..." from smallest to largest.
    gpu_tiers: str = "gpu-24g:24:1.0,gpu-80g:80:3.0"
    cpu_credit_multiplier: float = 0.25
    # Hosted worker: memory of this worker's GPU (auto-detected when unset) and the tier queue(s) it serves.
    worker_gpu_memory_bytes: int | None = None
    worker_queues: str | None = None

    # Stripe, test mode only. Live keys are refused at start-up.
    stripe_secret_key: SecretStr | None = Field(
        None, validation_alias=_alias("LIGHTSMITH_STRIPE_SECRET_KEY", "STRIPE_SECRET_KEY"))
    stripe_webhook_secret: SecretStr | None = Field(
        None, validation_alias=_alias("LIGHTSMITH_STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET"))
    stripe_price_pro: str | None = None
    stripe_price_team: str | None = None
    stripe_price_metered: str | None = None
    stripe_meter_event_name: str = "lightsmith_credits"
    # Checkout/portal redirect URLs must start with one of these (no open redirects).
    public_web_url: str = "http://localhost:3000"

    @model_validator(mode="after")
    def _check(self):
        if self.stripe_secret_key is not None:
            key = self.stripe_secret_key.get_secret_value()
            if key and not key.startswith(("sk_test_", "rk_test_")):
                raise ValueError("STRIPE_SECRET_KEY must be a test-mode key (sk_test_/rk_test_); "
                                 "live keys are refused by this service")
        if self.mode == "hosted" and not self.allow_sqlite and not self.db_url.startswith("postgresql"):
            raise ValueError("hosted mode needs Postgres: set DATABASE_URL=postgresql+psycopg://...")
        if self.mode == "hosted" and not (self.api_key_pepper and self.api_key_pepper.get_secret_value()):
            raise ValueError("hosted mode needs LIGHTSMITH_API_KEY_PEPPER (random secret used to hash API keys)")
        return self

    @property
    def hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def service_name(self) -> str:
        return self.hosted_product_name if self.hosted else self.product_name

    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins)

    @property
    def allowed_host_list(self) -> list[str]:
        return _csv(self.allowed_hosts)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'compute.db').as_posix()}"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def tiers(self) -> list[GpuTier]:
        out = []
        for item in _csv(self.gpu_tiers):
            queue, gib, mult = (item.split(":") + ["1.0"])[:3]
            out.append(GpuTier(queue, int(float(gib) * 2**30), float(mult)))
        return sorted(out, key=lambda t: t.memory_bytes)

    def tier(self, queue: str) -> GpuTier | None:
        return next((t for t in self.tiers if t.queue == queue), None)
