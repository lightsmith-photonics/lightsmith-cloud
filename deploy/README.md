# Deploying Lightsmith Cloud

Nothing in this repository creates cloud resources or billing accounts. This page covers how
Lightsmith Cloud fits together, how to run it with Docker, how much the GPU hosting options cost,
and a suggested pricing model.

## Architecture

```
Community ─JWT──►  API (CPU, N replicas) ──► Postgres (jobs, events, orgs, keys, usage)
studio  ──key──►    │  validate, estimate()   ├─► Redis (RQ queues, one per GPU tier; rate limits)
                    │  quota, admission       └─► S3-compatible bucket (results) or a shared volume
                    ▼
            GPU workers (one process per GPU, `lightsmith-worker`)
              └─ one sandboxed `python -I` solver child per job
                 (runtime cap, host-RSS cap, CUDA memory fraction, scrubbed env)
```

- **Admission.** The API runs `estimate()` in a sandboxed child on CPU. The resulting double-precision
  memory figure is an upper bound for GPU single precision. The API then routes the job to the
  smallest tier in `LIGHTSMITH_GPU_TIERS` that fits with 15% headroom. Before a worker starts a job it
  checks again against its own GPU memory (`AdmissionRejected`, not billed), then caps the child's
  CUDA allocator.
- **Metering.** Each job is billed on the wall time of its solver child: GPU-seconds × the tier's
  credit multiplier, or the CPU multiplier for CPU-only methods such as Mie. One usage record is
  written per job. Only credits beyond the plan's included ones are billed: each record sends at
  most one Stripe meter event, with the whole overage credits it adds and `identifier = usage_<id>`,
  so retries are idempotent. `lightsmith-server billing-sync` (cron) or `POST /v1/admin/billing/sync`
  resends events that failed. Stripe set-up and the quota rules are in the main README and API.md.
- **Failure handling.** Failures caused by the service are not billed: `WorkerCrashed`,
  `AdmissionRejected`, `ServerShutdown`, `WorkerLost` and `StorageError`. A reaper in the API marks
  jobs as `WorkerLost` if they have been running far past every plan's runtime cap.

## Running it

Everything runs from the repository root, which is the build context:

```bash
cp .env.example .env
```

In `.env`, set:
- `LIBRARY_SPEC`: the library release, wheel or git URL, see below.
- `POSTGRES_PASSWORD`, `LIGHTSMITH_API_KEY_PEPPER`, `LIGHTSMITH_ADMIN_TOKEN` and
  `PLATFORM_JWT_SECRET`.
- Stripe values, with **test** keys only.

Then build and start:

```bash
docker compose build
docker compose up -d
docker compose ps        # postgres, redis healthy; migrate exited 0; api, worker healthy
```

Without a GPU, use the CPU worker instead:

```bash
docker compose --profile cpu up -d postgres redis migrate api worker-cpu
```

**What compose starts.**

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine`, named volume | database |
| `redis` | `redis:7-alpine`, AOF | job queues and rate limits |
| `migrate` | API image, runs `lightsmith-server migrate` | Alembic upgrade, then exits. API and workers start only after it succeeds. |
| `api` | CPU image with CPU-only PyTorch, so validation and `estimate()` see the same backends as the workers | the API, on `127.0.0.1:8200` |
| `worker` | CUDA image | one GPU, reserved through `deploy.resources.reservations.devices` |
| `worker-cpu` (profile `cpu`) | CPU image | a worker for machines without a GPU |
| `s3` (profile `s3`) | `chrislusf/seaweedfs:3.97` (S3 gateway, dev only, no auth) | optional S3-compatible results bucket; production uses a managed bucket (S3, R2, ...) |

- Every app container runs as uid 10001 and has a healthcheck. The worker's is
  `lightsmith-worker healthcheck`, which checks that Redis is reachable and that this host's RQ
  worker is registered.
- Results are shared through the `results` volume unless `LIGHTSMITH_S3_BUCKET` is set.
- Hosted mode refuses SQLite and refuses to start on an out-of-date schema.

**The library dependency.** `LIBRARY_SPEC` is any pip requirement:
- A release from PyPI: `LIBRARY_SPEC=lightsmith==0.2.0`.
- A wheel copied into `./wheels`. Build it with `uv build --wheel <library checkout> --out-dir wheels`,
  then set `LIBRARY_SPEC=wheels/lightsmith-0.2.0-py3-none-any.whl`.
- A git URL: `LIBRARY_SPEC=lightsmith @ git+https://github.com/lightsmith-photonics/lightsmith@<tag>`.

Whichever it is, it is installed with `requirements/hosted.txt` as constraints. That file is the pinned
export of `uv.lock`; regenerate it after `uv lock` with the command at its top.

`POST /v1/discretization` needs a library with `lightsmith.discretization()`, which came after the
0.2.0 release; with 0.2.0 that endpoint answers `501 NotImplemented` until the next release (or use
a wheel or git URL of the library's `main`).

**One worker per GPU.** On a multi-GPU host, run one `worker` container per GPU, each reserving one
device (`device_ids: ["<i>"]` in compose). Each worker detects its GPU's memory and
serves the largest tier that fits, plus every smaller tier after it. Set `LIGHTSMITH_WORKER_QUEUES` to
override.

**Stripe setup (test mode).** `lightsmith-server stripe-setup` creates the meter, the prices and the
portal configuration in a Stripe *test* account and prints the `LIGHTSMITH_STRIPE_*` lines for `.env`;
the [main README](../README.md#stripe-billing-test-mode-only) has the full procedure, including the
webhook endpoint (`/v1/billing/webhook`) and its events. The metered prices have no free tier, because
this service sends Stripe only the credits beyond the plan's included ones. The service refuses
`sk_live_` keys and `livemode: true` events.

**Production checklist.**
- Postgres with backups.
- Run `lightsmith-server migrate` (the `migrate` service) on every deploy before the API and
  workers. Add a revision for every model change.
- An S3-compatible bucket with lifecycle expiry for results.
- TLS termination in front of the API.
- More than one API replica, using the Redis rate limiter.
- Log shipping.
- Alerting on queue depth and on the `credits_unreported_to_stripe` field of `/v1/admin/usage`.

## GPU hosting options

All prices are **estimates** in USD per GPU-hour: list on-demand prices, US regions where stated,
before tax. They were **checked 2026-11-01** on the provider pages named. Some are marked as coming
from an aggregator because the official page could not be read. Re-check before deciding.

### On-demand / VM-style (long-running Docker worker polling Redis works as is)

| Provider | GPU | $/GPU-h (est.) | Billing | Source |
|---|---|---|---|---|
| RunPod Pods (community / secure) | L4 | 0.44 / 0.59 | per second | runpod.io/pricing |
| | RTX 4090 | 0.34 / 0.89 | | |
| | RTX 5090 | 0.69 / 1.19 | | |
| | L40S | 0.79 / 1.09 | | |
| | A100 80GB SXM | 1.39 / 1.79 | | |
| | H100 SXM | 2.69 / 3.99 | | |
| Lambda (1x) | A10 / A100 40GB / H100 PCIe | 1.29 / 1.99 / 3.29 | not stated | lambda.ai/pricing |
| Vast.ai marketplace | RTX 4090 | ~0.15–0.52 | per second | aggregators (Aug–Sep 2026), unverified |
| CoreWeave | L40S / A100 / H100 | 2.25 / 2.70 / 6.16 | 8-GPU nodes only | coreweave.com/pricing |
| DigitalOcean GPU Droplets | RTX 4000 Ada / L40S / H100 | 0.76 / 1.57 / 4.41 | per second, 5-min minimum | digitalocean.com/pricing/gpu-droplets |
| Koyeb | L4 / L40S / A100 / H100 | 0.70 / 1.20 / 1.60 / 2.50 | per second, can scale to zero | koyeb.com/pricing |
| Hetzner dedicated | RTX PRO 4000 Blackwell 24GB | €214/month + €209 setup (≈ €0.29/h at 100% use) | monthly | hetzner.com/dedicated-rootserver/matrix-gpu |
| | RTX PRO 6000 96GB | €999/month + €999 setup | monthly | |
| AWS | L4 (g6.xlarge) / A10G (g5.xlarge) | 0.81 / 1.01 | per second | instances.vantage.sh |
| | A100 40GB / H100 (8-GPU nodes ÷ 8) | 2.74 / 6.88 | | |
| GCP | L4 (g2-standard-4) / A100 40GB | ~0.71 / ~3.67 | per second | aggregator (Holori) |
| Azure | A100 (NC24ads v4) | ~3.67 | per minute | azurespeed.com, cloudprice.net |

### Serverless GPU (per-second billing, scale to zero)

| Provider | GPU | $/GPU-h (est.) | Notes | Source |
|---|---|---|---|---|
| Modal | L4 / A10 / L40S / A100 80 / H100 | 0.80 / 1.10 / 1.95 / 2.50 / 3.95 | Plus CPU (~$0.047/core-h) and RAM (~$0.008/GiB-h). Load time and the default 60 s idle window are billed. $30/month credit. | modal.com/pricing |
| RunPod Serverless (flex) | 24GB L4-class / L40S / A100 / H100 | 0.75 / 1.75 / 2.72 / 5.39 | Billed per second, rounded up. Idle window defaults to 5 s. Bring your own Docker image. | runpod.io/product/serverless |
| Beam | RTX 4090 | ~0.69 | Billed per millisecond. Any base image. | beam.cloud/pricing |
| Baseten | L4 / A10G / A100 / H100 | 0.85 / 1.21 / 4.00 / 6.50 | Billed per minute. | baseten.co/pricing |
| Replicate | L40S / A100 80 / H100 | 3.51 / 5.04 / 5.49 | Private deployments bill setup and idle time. | replicate.com/pricing |
| Google Cloud Run GPU | L4 | ~0.84 + CPU/RAM | Unverified. Minimum 4 vCPU / 16 GiB. | Cloud Run docs + third-party mirror |

### What this means

- **Serverless costs about 1.3–2× more per GPU-hour.** An L4 on Modal costs about $1.05/h once
  CPU and RAM are included, against $0.44–0.81/h on-demand. In exchange, nothing is billed while
  the queue is empty.
- **The break-even point is roughly 30–55% average utilisation of a GPU.** That is about 7–13 busy
  hours a day. The lower end of the range applies once you add serverless cold-start and idle
  overhead for short jobs. Below the break-even, serverless or scale-to-zero is cheaper; above it,
  always-on workers are.
- **Cold start and idle time weigh heavily on short jobs.** Container start plus CUDA/PyTorch
  initialisation takes about 10–60 s. Most Lightsmith jobs are seconds to minutes long.
- **The Redis-queue design fits long-running workers directly.** This covers RunPod Pods, Koyeb,
  Hetzner, Vast and the hyperscalers. On serverless platforms that can't see Redis queue depth, add
  a small dispatcher that triggers one serverless invocation per queued job, which then runs
  `lightsmith_cloud.worker.run_job(job_id)`.
- **Marketplace capacity is cheap but less reliable** (Vast, RunPod community). It is acceptable
  here because jobs are idempotent and failures of the service are not billed.

### Suggested path (owner decision)

1. **Launch.** Run one always-on 24 GB Blackwell- or Ada-class worker, which matches the dev RTX
   5070 and the CUDA 12.8 image. Two options:
   - Hetzner RTX PRO 4000 (~€214/month, fixed cost, EU).
   - A RunPod secure-cloud 4090 or 5090 pod (~$0.89–1.19/h, stop it when idle).

   Put the API, Postgres and Redis on a small CPU VM, or use managed Postgres and Redis.
2. **Burst.** Add a queue-depth autoscaler that starts RunPod or Koyeb pods for the `gpu-24g` and
   `gpu-80g` queues and stops them after N idle minutes. Keep 80 GB capacity (A100 80GB, about
   $1.39–1.79/h on RunPod) on demand only.
3. **Scale.** Once steady utilisation passes ~50%, reserve capacity or move baseline load to
   dedicated servers.

## Suggested pricing model (estimates, owner decision)

The unit of account is **1 credit = 1 GPU-second on the 24 GB base tier**. The 80 GB tier uses a
3× multiplier, and CPU-only solves (e.g. Mie) use 0.25×. Credits map directly to cost, which keeps
the price easy to explain.

| | Free | Pro | Team |
|---|---|---|---|
| Price / month | $0 | $29 | $129 |
| Included credits | 1,800 (30 GPU-min) | 36,000 (10 GPU-h) | 180,000 (50 GPU-h) |
| Overage | blocked | $0.0005/credit ($1.80/GPU-h) | $0.0004/credit ($1.44/GPU-h) |
| Max runtime / job | 5 min | 1 h | 4 h |
| Concurrent jobs | 1 | 4 | 16 |
| Max job memory | 16 GB | 72 GB | 72 GB |

These limits and prices are encoded in `src/lightsmith_cloud/billing.py` (`PLANS`, with a monthly
hard limit of 10x the included credits on paid plans). `lightsmith-server stripe-setup` creates the
matching Stripe test-mode prices; Stripe holds the amounts actually charged.

**Rationale, with all cost figures estimated as above.**
- A 24 GB GPU-hour costs about $0.45–1.05.
- At $1.80/h, overage keeps a gross margin of roughly 40–75% after cold start, idle time and
  failed jobs that are not billed.
- A Free user who uses every credit costs about $0.25–0.50/month.
- Pro's included 10 GPU-h cost about $5–10, so the $29 plan stays profitable at full use.
- The 3× multiplier puts the 80 GB tier at $5.40/h, against an A100 80GB cost of about
  $1.4–2.7/h.

**Revisit after the first month of real usage data.** Use
`GET /v1/admin/usage` and `/v1/admin/usage/daily`.
