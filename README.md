# lightsmith-cloud

The Lightsmith job API, around the `lightsmith` scientific library. The same code runs in two modes:

- **Local** (`lightsmith-server`): one user, no auth, `127.0.0.1:8200`. This is the backend for the
  Lightsmith studio on someone's own machine. It uses SQLite, migrated automatically, and a results
  folder in the user data directory, with an in-process worker pool and no external services.
- **Lightsmith Cloud** (`LIGHTSMITH_MODE=hosted`): multi-tenant.
  - Identity comes from platform JWTs (ids only) and from Lightsmith API keys.
  - Storage is Postgres with Alembic migrations, plus S3-compatible object storage.
  - Jobs go to Redis/RQ queues per GPU tier, with one GPU worker per GPU.
  - Quotas, per-job metering in GPU-seconds and credits, Stripe billing in **test mode only**, and
    admin usage endpoints.

**Contract.** The API is described in [API.md](API.md) and [openapi.json](openapi.json). Deployment
and GPU hosting costs are in [deploy/README.md](deploy/README.md).

Every Docker build uses this repository as the context.

## Local mode

```bash
uv sync --extra gpu          # add --extra hosted for the Cloud dependencies
uv run lightsmith-server     # http://127.0.0.1:8200/docs   (omniscatter-server is an alias for now)
```

Submit a job:

```bash
curl -s localhost:8200/v1/jobs -H 'content-type: application/json' -d '{"simulation": {"particles": [{"shape": {"kind": "sphere", "radius_nm": 40}, "material": {"kind": "library", "name": "Au"}}], "medium_index": 1.33, "wavelengths": {"start_nm": 450, "stop_nm": 700, "num": 51}}}'
```

Then poll it:

```bash
curl -s localhost:8200/v1/jobs/<job_id>
```

**Options.**
- `--workers N` runs N solver processes at once (default 1, one GPU job at a time).
- `--data-dir` sets where the database and results go.
- `LIGHTSMITH_JOB_TIMEOUT_S` and `LIGHTSMITH_JOB_MEMORY_BYTES` cap jobs.

Every setting is in `src/lightsmith_cloud/config.py`, prefixed `LIGHTSMITH_`.

## Lightsmith Cloud with Docker

See [deploy/README.md](deploy/README.md) for the full procedure. In short:

```bash
cp .env.example .env                    # set LIBRARY_SPEC, POSTGRES_PASSWORD and the secrets
docker compose build
docker compose up -d                    # postgres, redis, migrate (one-shot), api, GPU worker
```

**Images.** Both are multi-stage builds, run as a non-root user, and have healthchecks:
- `docker/api.Dockerfile`: a slim CPU image, also used for `migrate` and the CPU-only worker.
- `docker/worker.Dockerfile`: a CUDA worker on the PyTorch CUDA 12.8 base.

**Dependencies.** Python dependencies are pinned in `requirements/hosted.txt`, exported from
`uv.lock`. The library comes from the `LIBRARY_SPEC` build arg: a release (`lightsmith==0.2.0`), a
wheel in `./wheels` or a git URL. `POST /v1/discretization` needs a library newer than 0.2.0 and
answers `501` otherwise.

**Without Docker:**

```bash
LIGHTSMITH_MODE=hosted DATABASE_URL=postgresql+psycopg://... LIGHTSMITH_API_KEY_PEPPER=... lightsmith-server migrate
```

```bash
LIGHTSMITH_MODE=hosted DATABASE_URL=... REDIS_URL=... LIGHTSMITH_API_KEY_PEPPER=... lightsmith-server --host 0.0.0.0
```

```bash
LIGHTSMITH_MODE=hosted DATABASE_URL=... REDIS_URL=... LIGHTSMITH_API_KEY_PEPPER=... lightsmith-worker   # one per GPU
```

## How a job runs

1. **Validation.** The API parses the spec with the library's pydantic models. Any problem kind is
   accepted, and unknown fields are rejected. It then applies size limits and checks that a backend
   can run the problem. Specs are data: nothing in them is executed, and no field names a file or
   URL.
2. **Hosted admission.** `estimate()` runs in a sandboxed child process. Its memory figure, which is
   optional per backend, picks the GPU tier and is checked against the plan. The API also checks
   quota and concurrency.
3. **Queueing.** The job row is written to the database, which is the source of truth for job
   state, and queued: on a local thread, or on the RQ queue of its tier.
4. **Execution.** The worker runs the library in a fresh `python -I` subprocess. The subprocess gets
   a scrubbed environment, a private temporary directory, a runtime cap, a host-memory watchdog and
   a CUDA memory fraction. Crashes, timeouts and cancels kill only that subprocess.
5. **Results and events.** Progress and log lines go to the job's event log, which the WebSocket
   replays. The result is stored gzip-compressed together with its `result_kind`. Usage is metered
   and, on Cloud, reported to Stripe.

**Library seams.**
- `src/lightsmith_cloud/lib.py` is the only module that imports the library, so moving or renaming
  it is a change to that file and the dependency name.
- For development the library is a path dependency on a sibling checkout (`../lightsmith`). Docker
  images take it from `LIBRARY_SPEC` instead.
- `proposals/library-progress.diff` adds an optional `progress` callback to `solve`/`compare`.
  The child already uses it when present.

## Stripe billing (test mode only)

Paid GPU compute uses Stripe Checkout, the customer portal, one Billing Meter for overage credits and
signed webhooks. The rules (credits, quotas, overage, failed payments) and the contract for the
platform's pricing and billing pages are in [API.md](API.md#account-and-billing). The service refuses
live keys (`sk_live_`) at start-up and live-mode webhook events, so nothing here can charge real money.

**Set-up, once per Stripe test account.** You need the test secret key from the Stripe dashboard
(test mode, Developers → API keys). Keep it in your shell or in `.env`, which is never committed. The
setup command reads it from `STRIPE_SECRET_KEY` and never prints or stores it.

1. See what will be created. This calls nothing:

   ```bash
   uv run lightsmith-server stripe-setup --dry-run
   ```

2. Create the meter, the pro and team products with their monthly prices, the metered overage prices
   and a customer-portal configuration. It prints the ids to add to `.env`:

   ```bash
   STRIPE_SECRET_KEY=sk_test_... uv run lightsmith-server stripe-setup
   ```

   With compose, run `docker compose run --rm api lightsmith-server stripe-setup` instead; it reads
   the key from `.env`. Running it again finds the same objects and creates nothing. Amounts come from
   `PLANS` in `src/lightsmith_cloud/billing.py`; Stripe prices cannot be edited, so to change one,
   archive it in the dashboard and run the command again.

3. Add the printed `LIGHTSMITH_STRIPE_*` lines to `.env`.

4. Webhooks. For local testing, the Stripe CLI forwards test events and prints a signing secret
   (`whsec_...`); put it in `STRIPE_WEBHOOK_SECRET`:

   ```bash
   stripe listen --forward-to localhost:8200/v1/billing/webhook --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed
   ```

   For a deployed API, add an endpoint `https://<api host>/v1/billing/webhook` with the same events in
   the dashboard (test mode), and use its signing secret.

5. Upgrade the database (revision `0002` adds the billing columns), then restart the API and workers:

   ```bash
   uv run lightsmith-server migrate
   ```

6. Try it from the platform's billing page, or call `POST /v1/billing/checkout` with a platform JWT
   and open the returned URL. Pay with the test card `4242 4242 4242 4242` (any future date, any CVC).
   To test a failed renewal, switch the card to `4000 0000 0000 0341` in the portal and advance the
   subscription with a Stripe test clock; the org then gets free-plan limits until an invoice is paid.

7. Schedule `lightsmith-server billing-sync` (every 15 minutes, say) to resend meter events that
   failed. Stripe accepts meter events up to 35 days old.

`GET /v1/admin/orgs/{id}` shows an org's subscription state, its usage records with their meter
status, and the Stripe events applied to it.

## Database migrations

Models live in `src/lightsmith_cloud/db.py` and migrations in `src/lightsmith_cloud/migrations/`.
After changing a model, generate a revision against a database that is at head, then review it:

```bash
uv run python -c "from sqlalchemy import create_engine; from lightsmith_cloud import migrate; migrate.autogenerate(create_engine('postgresql+psycopg://...'), 'describe change')"
```

## Tests

```bash
uv sync --extra hosted --extra gpu   # the Cloud tests import redis, rq and stripe; torch runs the optimize and GPU tests
uv run pytest                        # local end-to-end, security, Cloud (fakeredis + RQ), Stripe billing, migrations
uv run pytest -m gpu                 # only the GPU job test (skipped without CUDA)
```

Without `--extra gpu`, the optimize test and the GPU test are skipped; everything else runs.

To run the Cloud tests against Postgres (the database is wiped before each test):

```bash
LIGHTSMITH_TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost/db uv run pytest tests/test_hosted.py
```

The billing tests run the real `stripe` library against responses recorded from stripe-mock
(`tests/fixtures/stripe/`). To also check every request against Stripe's API spec, run stripe-mock:

```bash
docker run --rm -d -p 12111:12111 stripe/stripe-mock:latest
STRIPE_MOCK_URL=http://localhost:12111 uv run pytest tests/test_billing.py
```

## Licence

MIT ([LICENSE](LICENSE), the same terms as the library). This covers the Cloud code too: billing,
metering, auth and workers.
