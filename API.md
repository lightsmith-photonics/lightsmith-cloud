# Lightsmith API — v1

This API is served both by the local Lightsmith server and by Lightsmith Cloud. The
machine-readable contract is [`openapi.json`](openapi.json). Regenerate it with
`uv run lightsmith-server --openapi openapi.json`. A running server also serves it at
`/openapi.json` and `/docs`. FastAPI leaves the WebSocket out of OpenAPI, so it is documented here
only.

| | Local (`lightsmith-server`) | Lightsmith Cloud (`LIGHTSMITH_MODE=hosted`) |
|---|---|---|
| Base URL | `http://127.0.0.1:8200` | wherever it is deployed |
| Auth | none: one implicit user; only `localhost`/`127.0.0.1` Host headers are accepted | `Authorization: Bearer <api key or platform JWT>` (or `X-API-Key: <api key>`) |
| Execution | in-process worker pool, one solver subprocess per job | Redis/RQ queue per GPU tier, separate GPU workers |
| Storage | SQLite (migrated automatically) + files | Postgres (migrated by `lightsmith-server migrate`) + S3-compatible bucket |
| Extras | — | `credits_estimate`, quotas, usage, billing, admin |

**CORS.** In dev, requests are allowed from web (`http://localhost:3000`), studio
(`http://localhost:5173`) and Tauri (`tauri://localhost`, `http://tauri.localhost`). Set
`LIGHTSMITH_CORS_ORIGINS` to change this.

## Conventions

- **Content type.** Every request and response body is JSON, apart from the WebSocket stream.
- **Specs: any problem kind.** `simulation` (alias `spec`) is any problem the scientific library
  accepts. `GET /v1/schema` describes all of them. `kind` selects one of `scattering` (the default
  when `kind` is missing), `layer_stack`, `periodic`, `mode`, `device`, `bands`, `circuit` or `optimization`. Units
  are nm, wavelengths are vacuum wavelengths, and the time convention is `exp(-iωt)`. Unknown fields
  are rejected.
- **Non-finite numbers.** JSON has no NaN or Inf, so these are returned as `null`.
- **Errors.** Every error is `{"error": "<Type>", "message": "<text>"}`, sometimes with extra fields
  such as `details: [{loc, msg, type}]` or `plan`. HTTP status codes:

  | Status | When |
  |---|---|
  | 401 | `Unauthorized` (with `WWW-Authenticate: Bearer`) |
  | 402 | `QuotaExceeded` |
  | 403 | `Forbidden` |
  | 404 | `NotFound`, including another org's jobs |
  | 409 | `NoResult`, `AlreadySubscribed`, `NoActiveSubscription` |
  | 413 | `PayloadTooLarge`, `SpecTooLarge` |
  | 422 | `ValidationError`, `SpecTooLarge`, `UnknownMethod`, `UnsupportedProblem`, `TooLarge`, `TooLargeForPlan`, `Timeout` / `MemoryLimitExceeded` (estimate) |
  | 429 | `RateLimited` (with `Retry-After`), `ConcurrencyLimit` |
  | 502 | `BillingError` (Stripe refused or failed a call; the message is Stripe's) |
  | 503 | `QueueUnavailable`, `BillingDisabled` |

- **Ids and timestamps.** Job ids are 32 lowercase hex characters. Org and user ids are the
  platform's own ids. Timestamps are ISO 8601 UTC.
- **Aliases.** Several request and response fields have aliases for existing clients:

  | Field | Alias |
  |---|---|
  | `simulation` | `spec` |
  | `kind` | `operation` |
  | `job_id` | `id` |
  | `DELETE /v1/jobs/{id}` | `POST /v1/jobs/{id}/cancel` |
  | `line` (log events) | `message` |

## Endpoints

### `GET /v1/health` (public)
Returns:

```json
{"status": "ok", "service": "Lightsmith" | "Lightsmith Cloud", "mode": "local" | "hosted",
 "version": "…", "library": {"name": "lightsmith", "version": "…"}}
```

In hosted mode it also returns `queues`: `{queue: depth}`.

### `GET /v1/schema` (public)
Returns the JSON Schema of every problem kind: the envelope `{"problem": <union discriminated on kind>}`
(`problem_json_schema()`). `?kind=scattering` returns only the scattering `Simulation` schema.

### `GET /v1/backends` (public)
Returns `[{name, method, problem_kinds, license, gpu, available, unavailable_reason, citation, options_schema}]`.

`options_schema` is the JSON Schema of the backend's options, keyed as
`simulation.options[<method or backend>]`. It is `null` until the library exposes `options_model`
on backends.

### `POST /v1/supports`
Body: `{"simulation": {...}}`. For every backend of the problem's kind, returns
`{name, method, available, unavailable_reason, supported, reason}`.

### `GET /v1/doctor`
Local mode returns the library's `doctor()`. Hosted mode returns
`{api_host: doctor(), gpu_tiers: [{queue, memory_gib, credit_multiplier}]}`.

### `POST /v1/estimate`
```json
{"simulation": {...}, "method": "auto", "device": "auto"}
```
Returns the library's `estimate()` output plus `estimated_runtime_s`. It never solves.
- **Always present:** `backend`, `method`, `framework`, `device`.
- **Backend-specific:** `memory_bytes` (optional), and for DDA `dipoles`, `grid`, `solves`,
  `precision`, `fits`.
- **Hosted mode:** the estimate runs on CPU, so `memory_bytes` is an upper bound. The response also
  adds:

  ```json
  {"credits_estimate": 12, "seconds_estimate": 9.6, "estimated_runtime_s": 9.6,
   "runtime_basis": "backend|calibrated|rough", "queue": "gpu-24g", "fits_plan": true, "plan": "free"}
  ```

### `POST /v1/discretization`
```json
{"simulation": {...}, "method": "auto"}
```
What the backend will discretise for the problem, before any solve: the library's `discretization()` (see the
library's `docs/discretization.md`), run in the same sandboxed worker as `estimate`, with the same validation and limits. It never
solves and costs no credits. The response is the library's envelope:

```json
{"backend": "lightsmith-dda", "method": "dda", "problem_kind": "scattering", "layout_kind": "dipole_lattice",
 "summary": {"dipoles": 2176, "spacing (nm)": 4.98}, "payload": {"points_nm": [[...]], "spacing_nm": 4.98},
 "estimate": {...}, "warnings": [], "error": null}
```

`layout_kind` is `yee_grid`, `unit_cell`, `device_grid`, `dipole_lattice`, `surface_mesh`, `multipoles`,
`harmonics`, `cross_section_grid`, `cells`, `plane_waves` or `none`. `error` carries the library's refusal of the
problem while the geometry is still returned. A backend that cannot run the problem gives a 422; a server whose
library has no `discretization()` gives `501 NotImplemented`.

### `POST /v1/jobs` → `202`
```json
{"simulation": {...},             // alias "spec"; any problem kind
 "kind": "solve",                 // alias "operation": solve | compare | estimate | optimize (needs an optimization problem) | sweep
 "method": "auto",                // solve/estimate: method or backend name
 "methods": ["mie", "dda"],       // compare: default = every applicable backend for the problem kind
 "reference": null,               // compare: default = Mie if run, else the first method
 "device": "auto",                // auto | cpu | gpu | cuda:N | torch-cpu
 "label": "optional text",
 "sweep": {"path": ["particles", 0, "shape", "radius_nm"], "values": [30, 40, 50]}}  // sweep jobs only
```
Returns `{"job_id", "id", "status": "queued", "credits_estimate": null|number, "queue": null|"gpu-24g",
"quota_warning": null|string}`. `quota_warning` (hosted) is set when the job takes the org past 80% of
its included credits, or into billed overage.

The spec, method names and the backend's ability to run the problem are all checked **before** the
job is queued, so a mistake gives an immediate 422. In hosted mode the same call also does
admission: it estimates memory, picks the GPU tier, and checks the plan's concurrency, quota and
memory limits.

### `GET /v1/jobs/{id}`
Optional query parameters: `include_result=1` (default) and `include_spec=0`.

```json
{"job_id": "…", "id": "…", "kind": "solve", "operation": "solve", "problem_kind": "scattering",
 "status": "queued|running|succeeded|failed|cancelled", "progress": 0.0,
 "stage": "started|method_started|method_done|done|…", "message": "…",
 "method": "auto", "methods": null, "reference": null, "device": "auto", "label": null,
 "created_at": "…", "started_at": "…", "finished_at": "…",
 "error": null | {"error": "Timeout", "message": "…"},
 "warnings": ["…"], "device_used": {"framework": "torch", "device": "cuda:0", "gpu_name": "…"},
 "estimate": {…} | null,
 "usage": {"runtime_s": 1.9, "gpu_seconds": 1.9, "credits": 1.9},
 "result": {…} | null, "result_kind": "cross_sections", "result_url": "/v1/jobs/…/result", "result_bytes": 1234,
 "simulation": null}
```

- **`result_kind`** says what `result` holds:

  | `result_kind` | `result` |
  |---|---|
  | `cross_sections` | `Result`: scattering cross sections, fields, provenance |
  | `spectrum` | `SpectrumResult`: R/T/A, from layer_stack and periodic problems |
  | `modes` | `ModeResult` |
  | `sparameters` | `SParameterResult` |
  | `bands` | `BandResult` |
  | `optimization` | `OptimizationResult`: final design, history, validated S-parameters |
  | `compare` | `{reference, results: {method: <result>}, relative_difference}` |
  | `estimate` | the `estimate()` dict |

  The result is inlined up to 25 MB (`LIGHTSMITH_INLINE_RESULT_MAX_BYTES`). Above that, `result`
  is `null`; use `result_url`.
- **`usage`.** In local mode `credits` is `null`.
- **`error.error`.** It can take one of two kinds of value:
  - The library's exception type: `ValueError`, `MaterialRangeError`, …
  - A service error:

    | Error | Meaning |
    |---|---|
    | `Timeout` | runtime cap hit |
    | `MemoryLimitExceeded` | host memory cap hit |
    | `Cancelled` | the job was cancelled |
    | `WorkerCrashed` | the solver process died without reporting an error |
    | `AdmissionRejected` | the job does not fit the GPU |
    | `ServerShutdown` | the server stopped during the run |
    | `WorkerLost` | the worker running the job stopped |
    | `StorageError` | the result could not be stored |
    | `QueueUnavailable` | the job could not be enqueued |

### `GET /v1/jobs/{id}/result`
Returns the result JSON as stored. Returns `409 NoResult` until the job succeeds.

### `DELETE /v1/jobs/{id}` → `202` (alias `POST /v1/jobs/{id}/cancel`)
Cancels the job and returns the job view.
- A queued job becomes `cancelled` immediately.
- A running job's solver process is killed within about 1 s.
- Calling it on a finished job returns that job unchanged.

### `GET /v1/jobs?mine=1&status=&limit=50&before=`
Returns `{"jobs": [JobView without result], "next_before": timestamp|null}`, newest first.
`mine=1` restricts the list to the caller's own jobs; without it, the list covers the whole org.

### `WS /v1/jobs/{id}/events?after=0`
Streams the job's event log, replaying it from sequence number `after`, then closes after the job
ends:

```json
{"type": "status",   "seq": 1, "ts": "…", "status": "queued"}
{"type": "status",   "seq": 2, "ts": "…", "status": "running"}
{"type": "progress", "seq": 3, "ts": "…", "stage": "started", "progress": 0.02}
{"type": "progress", "seq": 4, "ts": "…", "stage": "method_started", "method": "mie", "progress": 0.1}
{"type": "progress", "seq": 5, "ts": "…", "stage": "method_done", "method": "mie", "progress": 0.525}
{"type": "log",      "seq": 6, "ts": "…", "stream": "stderr", "line": "…", "message": "…"}
{"type": "status",   "seq": 9, "ts": "…", "status": "succeeded"}       // or failed, with "error": {…}
{"type": "result", "result_kind": "cross_sections", "result": {…}}    // succeeded (result null + result_url if large)
{"type": "error", "error": "Timeout", "message": "…"}                 // failed or cancelled, instead of "result"
{"type": "end", "status": "succeeded"}
```

**Progress detail.**
- Optimization problems report every iteration, throttled to 2/s; the first and last iterations are
  always sent:

  ```json
  {"type": "progress", "stage": "iteration", "method": "inverse", "iteration": 7, "max_iter": 50,
   "objective": 0.83, "binarization": 0.61, "beta": 8.0, "evaluations": 8, "elapsed_s": 41.2, "progress": 0.236}
  ```

- Other jobs are coarse today: they report start, then progress per method. Once the library takes
  a `progress=` callback, the same events carry finer stages without any change to this API, for
  example `dda_solve` with `wavelength_nm`.
- To reconnect, pass the last `seq` you received as `after`.

**Hosted auth on the WebSocket.** In order of preference:
1. An `Authorization` header.
2. Subprotocols `["lightsmith.bearer", "<token>"]`; the server echoes `lightsmith.bearer`.
3. `?token=<jwt>` or `?api_key=<key>`. Query strings can end up in proxy logs, so use this only over
   `wss` and with short-lived JWTs.

Close codes: `4401` unauthenticated, `4403` forbidden, `4404` unknown job, `1000` done.

### Optimize jobs (inverse design)
`kind: "optimize"` runs an `optimization` problem with method `inverse` (`auto` picks it). A plain
`solve` of such a problem behaves the same way. It is long-running: progress arrives per iteration,
and the plan's runtime cap applies. In hosted mode the credit estimate is rough: it is based on
`max_iter × sources × 2` solves.

### Sweep jobs
`kind: "sweep"` solves the problem once per value of one number: `sweep.path` is the JSON path of that number in
the problem (a geometry field, a wavelength, the medium index…) or in a backend's options
(`["options", "dda", "dipoles_per_diameter"]`; missing objects are created), `sweep.values` 1 to 50 numbers. Every
swept problem is validated and checked against the limits before the job is queued, so a bad value gives an
immediate 422 that names it. The values run in order in one worker, with progress per value (`stage:
"sweep_value"`). In hosted mode each value is estimated and priced, and the job's credit estimate is their sum.

The result (`result_kind: "sweep"`) is `{"kind": "sweep", "path", "values", "method", "results": [<one result per
value, or null>], "errors": {"<index>": {"error", "message"}}}`: a value whose solve fails is recorded and the sweep
goes on. The job view repeats the request as `sweep: {path, values}`. Solves are independent: a sweep saves
submissions and queue slots, not solver set-up, which changes with the swept field.

## Lightsmith Cloud only

### Identity and auth
Users and orgs live in the platform (apps/web). This service stores only their ids, on jobs, API
keys and usage, plus one `orgs` row per org id holding plan, quota and billing state. It never
stores names, emails or other profile data.

**Platform JWT.**
- Signed HS256 with `PLATFORM_JWT_SECRET`.
- Required claims: `sub` and `aud`, where `aud` must equal `LIGHTSMITH_JWT_AUDIENCE` (default
  `compute`). `exp`, `nbf` and `iat` are verified when present. `iss` is checked if
  `LIGHTSMITH_JWT_ISSUER` is set.
- `role: "admin"` grants the admin endpoints.
- `org_id` is the acting org. Without it, the acting org is the personal org `user:<sub>`.
- `orgs` is an optional list of other org ids the user may select with an `X-Org-Id` header.

**API keys.**
- Format: `lsk_<12 hex>_<secret>`, sent as `Authorization: Bearer …` or `X-API-Key: …`. The server
  stores only an HMAC of the key.
- Create with `POST /v1/keys` (body `{"name"}`). This requires a platform JWT, and the response
  shows the key once.
- List with `GET /v1/keys`; revoke with `DELETE /v1/keys/{id}`.
- A key acts for one org and never carries the admin role.

**Admin token.** `Authorization: Bearer $LIGHTSMITH_ADMIN_TOKEN` gives admin access, for
operators.

### Account and billing

| Endpoint | Auth | Returns or does |
|---|---|---|
| `GET /v1/me` | any | `{kind, user_id, role, api_key_id, org: {id, plan, subscription_status}, plan: {…limits}, usage: Usage}` |
| `GET /v1/usage` | any | `Usage` (below) |
| `GET /v1/plans` | public | `{credit_unit, currency: "usd", plans: [Plan]}` |
| `GET /v1/billing` | any | `Billing` (below): everything a billing page shows, in one call |
| `POST /v1/billing/checkout` | JWT | `{url}` of a Stripe Checkout page. Body `{plan: "pro"|"team", success_url, cancel_url}`. `409 AlreadySubscribed` if the org already has a subscription. |
| `POST /v1/billing/portal` | JWT | `{url}` of the Stripe customer portal (invoices, payment method, cancel at period end). Body `{return_url}`. |
| `POST /v1/billing/plan` | JWT | Switches an active subscription between `pro` and `team`, prorated. Body `{plan}`. Returns `{status: "pending"|"unchanged", plan, previous_plan?}`; the plan changes when Stripe's webhook arrives. `409 NoActiveSubscription` without one. |
| `PUT /v1/billing/limits` | JWT | Sets the org's own monthly spending cap. Body `{monthly_credit_limit: int|null}`. Returns `Usage`. |
| `POST /v1/billing/webhook` | Stripe signature | Stripe webhook; test-mode events only. |

"JWT" means a platform login token. API keys get `403` on those endpoints, so a leaked key cannot
change billing. Redirect URLs must start with `LIGHTSMITH_PUBLIC_WEB_URL`.

```ts
Plan = {name: "free"|"pro"|"team", monthly_credits, overage: boolean, hard_limit_credits,
        monthly_price_usd, overage_usd_per_credit,
        purchasable: boolean,                  // a Stripe price is configured for it
        max_concurrent_jobs, max_runtime_s, max_memory_bytes, host_memory_bytes, rate_limit_per_minute, max_spec_bytes}
Usage = {period_start, period_end,             // calendar month, UTC
         plan,                                 // the plan whose limits apply now (the effective plan)
         credits_used, credits_reserved,       // reserved: estimates of queued and running jobs
         credits_included, credits_remaining,  // remaining = included - used, never below 0
         overage_allowed, overage_credits, overage_usd_per_credit,
         soft_limit,                           // 80% of included (LIGHTSMITH_QUOTA_SOFT_LIMIT_FRACTION)
         hard_limit,                           // most credits this month, overage included
         credit_limit: int|null,               // the org's own cap, if set
         state: "ok"|"soft_limit"|"overage"|"blocked"}
Billing = {billing_enabled, test_mode: true,
           org: {id, plan, effective_plan,
                 subscription_status: null|"active"|"trialing"|"past_due"|"unpaid"|"canceled"|"incomplete"|
                                      "incomplete_expired"|"paused",
                 current_period_end: timestamp|null, cancel_at_period_end, has_customer},
           payment_issue: boolean,             // past_due, unpaid or incomplete
           actions: {checkout: ("pro"|"team")[], change_plan: ("pro"|"team")[], portal: boolean},
           usage: Usage, plans: Plan[]}
```

#### Quotas and billing rules
- **Credits.** 1 credit = 1 GPU-second on the base tier (`gpu-24g`). The 80 GB tier costs 3 credits
  per second and CPU-only solves cost 0.25. Every finished job is metered from its solver's wall
  time. Failures caused by the service are not charged.
- **Period.** Quotas are per calendar month (UTC) on every plan. Stripe invoices on the
  subscription's own monthly cycle, and overage is billed on the invoice whose period contains it.
- **Included credits and overage.** Free has 1,800 credits and no overage. Pro and team may go past
  their included credits up to their hard limit. Only the credits beyond the included ones are sent
  to Stripe, as whole credits (the floor of the month's running total), at the plan's overage price.
- **Admission.** A new job is refused with `402 QuotaExceeded` when this month's usage, plus the
  estimates of the org's queued and running jobs, plus this job's estimate, would pass the hard limit.
  The error carries `plan`, `hard_limit`, `credits_used` and `credits_reserved`. The quota never stops
  a running job; how far a job can overshoot is bounded by the plan's runtime cap.
- **Hard limit.** The plan's `hard_limit_credits` (an operator can replace it per org), lowered by the
  org's own `credit_limit` if set.
- **Failed payment.** When Stripe reports a failed invoice (`past_due`, `unpaid`), the org keeps its
  plan name but gets the free plan's limits until an invoice is paid. A deleted subscription returns
  the org to `free`.
- **Webhook events handled:** `checkout.session.completed`; `customer.subscription.created`,
  `.updated`, `.deleted` (and `.paused`, `.resumed`); `invoice.paid`, `invoice.payment_succeeded` and
  `invoice.payment_failed`. Each event id is applied once (`outcome: "duplicate"` on redelivery).
  Stripe does not guarantee delivery order, so an event older than the last one applied to the org is
  skipped (`outcome: "stale"`). A handler error returns 500, so Stripe retries.

#### For the platform (Community): what to call and what to show
Call these server-side with the user's platform JWT (`aud: "compute"`, with `org_id` set to the org
being billed). Never send API keys from the browser.

| Page | Call | Show |
|---|---|---|
| Pricing (public) | `GET /v1/plans` | One card per plan: `monthly_price_usd` per month; `monthly_credits / 3600` GPU-hours included on the base tier; `overage_usd_per_credit × 3600` per extra GPU-hour; `max_runtime_s`, `max_concurrent_jobs`, `max_memory_bytes`. Show the subscribe button only when `purchasable`. Label prices "test mode" while Stripe is in test mode. |
| Subscribe | `POST /v1/billing/checkout` with `{plan, success_url: <web>/settings/billing?checkout=success, cancel_url: <web>/pricing}` | Redirect the browser to `url`. On return, poll `GET /v1/billing` until `org.plan` is the new plan (the webhook usually lands within seconds). |
| Billing settings | `GET /v1/billing` | Plan and status. The renewal date `current_period_end`, or "ends on …" when `cancel_at_period_end`. A usage bar of `credits_used` against `credits_included`, with `credits_reserved` as a pending segment and the overage beyond it (`overage_credits × overage_usd_per_credit` as "overage so far"). `hard_limit` and `credit_limit`. Buttons from `actions`: Subscribe (`checkout`), Switch to … (`change_plan`, then `POST /v1/billing/plan`), Manage billing (`portal`, then `POST /v1/billing/portal` and redirect). |
| Alerts | `GET /v1/billing` | When `payment_issue`: "Payment failed: GPU jobs are limited to the free plan until the invoice is paid", with Manage billing. By `usage.state`: `soft_limit` "80% of the included credits used"; `overage` "overage is billed per credit"; `blocked` "monthly limit reached: new jobs are refused until `period_end`". |
| Spending cap | `PUT /v1/billing/limits` with `{monthly_credit_limit}` | A number input (empty = the plan's hard limit), for org admins only (the platform decides who). |
| Job submit | `POST /v1/jobs` | Show `quota_warning` when present. On `402 QuotaExceeded`, show the message with a link to billing settings. |

### Admin (role `admin`)

| Endpoint | Returns or does |
|---|---|
| `GET /v1/admin/usage?since=&until=&org_id=` | per org: `jobs`, `runtime_s`, `gpu_seconds`, `credits`, `overage_credits`, `billable_units`, `credits_unreported_to_stripe`, `units_unreported_to_stripe`; plus `jobs_by_status` and `totals` |
| `GET /v1/admin/usage/daily?days=30` | usage per day, with `billable_units` |
| `GET /v1/admin/orgs` | the orgs, with billing state and `usage` |
| `GET /v1/admin/orgs/{id}` | one org: billing state, `usage`, its latest `usage_records` (with Stripe meter status and error) and the `stripe_events` applied to it (with `outcome`) |
| `PUT /v1/admin/orgs/{id}/plan` | sets an org's plan; body `{plan}` |
| `PUT /v1/admin/orgs/{id}/limits` | replaces the plan's hard limit for one org; body `{credit_cap_override: int|null}` |
| `POST /v1/admin/billing/sync` | resends Stripe meter events that failed (also `lightsmith-server billing-sync`, for cron) |

## Limits

These are defaults set with environment variables (`LIGHTSMITH_MAX_*`). Hosted plans add their own
limits on top.

**Every problem kind:**

| Limit | Default |
|---|---|
| Request body | 2 MB |
| JSON values per spec | 200,000 |
| Wavelengths | 5,000 |
| `dipoles_per_diameter` | 512 |
| Methods per compare job | 8 |
| `estimate()` run time | 60 s |
| `estimate()` memory | 4 GiB |

**Scattering only:**

| Limit | Default |
|---|---|
| Particles | 1,000 |
| Tabulated points per material | 20,000 |
| Shells per particle | 50 |
| Near-field samples | 2e7 |
| Far-field samples | 2e6 |
| DDA lattice lower bound | 5e7 cells |

Local mode has no runtime or memory cap on jobs unless `LIGHTSMITH_JOB_TIMEOUT_S` or
`LIGHTSMITH_JOB_MEMORY_BYTES` is set.

**Hosted plans** (`GET /v1/plans`):

| Plan | Price / month (test mode) | Included credits / month | Overage | Hard limit / month | Concurrent jobs | Runtime cap | Memory cap | Requests / min |
|---|---|---|---|---|---|---|---|---|
| free | $0 | 1,800 | none | 1,800 | 1 | 5 min | 16 GiB | 60 |
| pro | $29 | 36,000 | $0.0005 / credit | 360,000 | 4 | 1 h | 72 GiB | 300 |
| team | $129 | 180,000 | $0.0004 / credit | 1,800,000 | 16 | 4 h | 72 GiB | 1,200 |

## Stability

This is v1. Fields may be **added** to responses and events at any time, so clients must ignore
fields they don't know. Removing or renaming a field, or changing its meaning, requires `/v2`.

**Changes since the first draft** (2026-11-01):
- `/v1/schema` now returns the multi-kind envelope.
- API keys use the `lsk_` prefix.
- The WebSocket subprotocol is `lightsmith.bearer`.
- Environment variables use `LIGHTSMITH_` instead of `COMPUTE_`.
- `/v1/me` no longer returns an org name.
- Billing (branch `cloud-billing`): `GET /v1/billing`, `POST /v1/billing/plan`,
  `PUT /v1/billing/limits`, `GET /v1/admin/orgs/{id}` and `PUT /v1/admin/orgs/{id}/limits` are new;
  `Usage` and `Plan` gained fields; jobs return `quota_warning`. Checkout, portal and plan changes need
  a platform JWT (API keys get 403). Only overage credits are sent to Stripe.
