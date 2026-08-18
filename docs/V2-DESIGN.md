# code flow v2 — design document

Status: **draft / not started**. Written after building v1 (this repo) so the
next version starts from evidence rather than guesses. Everything here is a
recommendation with reasoning; open decisions are flagged at the end.

**Target**: an internal tool for **DevOps automation** (deploys, cert renewal,
backup verification, runbooks, infra checks) and **office automation**
(spreadsheet/report generation, SharePoint/Jira/Confluence glue, form-driven
tasks for non-developers). Used by a *trusted internal team* — not a
multi-tenant SaaS (see Non-goals).

---

## 1. What v1 got right — carry these over

These are the parts that made v1 pleasant. Do not redesign them for novelty.

- **Flows are plain Python.** No DSL, no YAML, no visual builder. Reviewable
  in git, testable with pytest, generatable by an LLM.
- **Every run produces a self-contained HTML report**: step-by-step status,
  timings, text logs, structured JSON/table blocks, embedded images, inputs,
  context snapshot, outputs. This is the product's soul — "run it and see
  what happened later" is the thing most lightweight tools lack.
- **Dashboards are flows** (`dashboard = True` + `self.widget(...)`). One
  primitive, two products. Keep the widget set: metric, stat, status,
  progress, table (with conditional formatting), chart, list, alert, text,
  json, html, link, section.
- **Typed inputs** (`inputs_schema`) that generate a form in the UI and
  validate at every entry point (UI, API, webhook, schedule).
- **Environments** as named JSON files + `${VAR}` references resolved from the
  process environment, with automatic masking of secret-looking keys in
  reports and UI.
- **Decorator ergonomics** for reliability: `retry`, `retry_delay`,
  `retry_backoff`, `retry_on`, `timeout`, `continue_on_error`.
- **Zero-infra start**: one command, no database server, no message broker.
  v2 must preserve "clone, run, works" even though the internals grow.
- **`llms.txt`** — a compact machine-readable reference so AI can write
  correct flows. Keep it in sync; a stale one is worse than none.

## 2. What v1 got wrong — change these at the foundation

Three things cannot be retrofitted cheaply. They are the reason to rebuild.

### 2.1 String-graph control flow → plain Python + journaled replay

v1 encodes the flow as a state machine of named steps: `next="Step2"`,
`condition="a > 10"`, `loop="i in items"`, `return {"__next__": "Other"}`.
This reimplements Python's own control flow as decorator arguments. Costs
paid in v1: string `eval` for conditions/loops, unknown-step runtime errors,
cycle detection, a flow diagram that cannot show runtime branches, and
awkward bolt-ons (`resumable=True`) to make loops resumable.

**v2: the flow body is ordinary Python. Steps are journaled function calls.**

```python
from codeflow import flow, step, lock, approve, ctx

@step(retry=3, retry_on=ConnectionError, timeout=30)
def push(artifact: str, host: str) -> dict:
    ...

@flow(name="Deploy service", tags=["devops"])
def deploy(service: str, version: str, dry_run: bool = False):
    art = build(service, version)                 # journaled
    with lock(f"deploy:{service}"):               # cross-run mutex
        approve(f"Deploy {service} {version} to prod?")   # suspends the run
        for host in hosts(service):               # a real for loop
            push(art, host)                       # journaled per call
        if not dry_run:
            smoke_test(service)
    return {"deployed": version}
```

**How replay works.** Every `@step` call gets a deterministic key. On call,
the engine looks the key up in the run's journal:

- entry exists with status `SUCCESS` → return the recorded result immediately,
  do not execute
- otherwise → execute, record result or error, continue

A resume is therefore just *running the flow function again* with the journal
preloaded: completed work returns instantly, execution effectively continues
at the first incomplete step. This gives, for free, what v1 needed special
cases for: resume-from-failed-step, resume-at-failed-loop-item, and
suspension for approvals or long waits.

**Step keys.** `key = f"{step_name}:{sha1(canonical_json(args, kwargs))}:{n}"`
where `n` disambiguates identical repeated calls. Args-based keys survive
reordering of a loop's iterable better than positional indices (v1's
fingerprint hack existed for exactly this reason).

**Determinism contract.** The flow body must produce the same sequence of
step calls on replay. Rules to document loudly:

- No `datetime.now()`, `random`, `uuid4()`, or environment-dependent
  branching **in the flow body** — do that inside steps, or use the provided
  journaled helpers `ctx.now()`, `ctx.uuid()`, `ctx.random()` (recorded on
  first execution, replayed thereafter).
- Real work belongs inside `@step`. The body is orchestration only.
- If a replayed call's key does not match the journal's expected sequence,
  raise `NondeterminismError` and fail loudly rather than silently doing the
  wrong thing.

**What is lost:** the static pre-run flow diagram. Replace it with a *trace*
view built from the journal — what actually ran, in order, with timings.
More useful anyway; v1's diagram could not show runtime branches.

### 2.2 Threads in the web process → worker processes, subprocess per run

v1 runs flows on a `ThreadPoolExecutor` inside the FastAPI process.
Consequences: cancellation is cooperative only, timeouts cannot kill an
abandoned call, CPU-bound `parallel=` is GIL-bound, a queued run is lost on
restart, a deploy kills in-flight runs, and multiple uvicorn workers are
impossible because live state is in memory.

**v2 process model:**

- **web** — FastAPI. Serves UI/API, reads/writes the database, enqueues runs.
  Never executes flow code. Can be scaled/restarted freely.
- **worker (supervisor)** — claims queued runs from the database, spawns a
  **child process per run**, forwards its event stream to the database,
  enforces deadlines, handles kill/cancel. One or more per host.
- **child** — the flow's own interpreter: imports the deployed flow version,
  executes it, streams events (log lines, step transitions, artifacts) over a
  pipe. Isolated: a crash or memory explosion takes down only that run.

Gains: real cancellation (SIGTERM → grace → SIGKILL), enforceable timeouts,
crash isolation, restart safety (claimed-but-dead runs are re-queued or
marked INTERRUPTED by a reaper), horizontal scale later, and per-flow
dependency isolation if you give a flow version its own virtualenv.

Cost: ~50–200 ms interpreter start per run. Irrelevant for these workloads.

### 2.3 JSON files → SQLite (Postgres-compatible)

Measured in v1 (median, this repo, warm cache):

| operation | cost |
|---|---|
| `discover_workflows()` — ran on **every** API request | 2.6 ms |
| 10 trivial steps, no persistence | 1.0 ms |
| **same 10 steps, saving after each step** | **21 ms** |
| `save_run` with one 300 KB embedded image | 3.4 ms |
| `save_run` with ~300 runs in `index.json` (98 KB) | 4.2 ms |

95% of the engine's overhead was persistence: every step transition
re-rendered the entire HTML report (re-encoding embedded images), rewrote the
run JSON, the resume sidecar, *and* the whole index — all behind one global
lock, so concurrent runs serialised.

**v2 storage: SQLite (WAL) by default, Postgres as a drop-in for scale.**
Still zero-infra, but transactional, indexed, and searchable. Logs become
append-only event rows (which is what makes live streaming trivial). Reports
are rendered **on demand** from data, never stored as HTML.

Suggested schema (abbreviated):

```
flow_versions(id, name, description, tags, params_schema, git_sha, dirty,
              source_path, registered_at, registered_by)
runs(id, flow_version_id, status, trigger, triggered_by, environment,
     inputs_json, outputs_json, started_at, ended_at, duration_ms, error,
     parent_run_id, resumed_from_id, attempt)
steps(id, run_id, key, name, seq, status, attempts, started_at, ended_at,
      duration_ms, result_json, error, traceback)        -- the journal
events(id, run_id, step_id, ts, kind, payload_json)       -- log/json/table/image
artifacts(id, run_id, name, kind(in|out), mime, bytes, sha256, path)
approvals(id, run_id, step_key, prompt, status, decided_by, decided_at, comment)
locks(name, run_id, acquired_at, expires_at)
schedules(id, flow_name, cron, timezone, inputs_json, environment, enabled,
          last_run_id, last_error, next_run_at)
notifications(channels, subscriptions)                    -- see §4.4
users(id, subject, email, name, role), api_tokens(id, user_id, hash, scopes)
audit_log(id, ts, actor, action, target, detail_json)
```

Retention: per-table policies (runs older than N days, artifacts by size),
enforced by a maintenance job — not by the write path (v1 pruned inside
`save_run`, which coupled hot writes to cleanup).

---

## 3. Programming model reference (target API)

```python
from codeflow import flow, step, ctx, lock, approve, sleep_for, artifact

@step(retry=3, retry_delay=2, retry_backoff=2,
      retry_on=(ConnectionError, TimeoutError), timeout=30,
      cache=None)                    # cache="1h" → reuse across runs
def fetch(url: str) -> dict: ...

@flow(name="...", tags=[...], dashboard=False, webhook=False,
      concurrency="1 per service",   # optional declarative lock
      notify_on=["failure"])
def my_flow(service: str, count: int = 5) -> dict:
    ...
```

Inside a flow or step:

| API | purpose |
|---|---|
| `ctx.log(msg)` / `log_json` / `log_table` / `log_image` | report content (keep v1 semantics) |
| `ctx.env["api_url"]` | selected environment values (secrets resolved, masked in UI) |
| `ctx.secret("name")` | explicit secret fetch, never journaled, never logged |
| `ctx.now()` / `ctx.uuid()` / `ctx.random()` | journaled non-determinism |
| `ctx.input_file(name)` / `artifact(data, name=)` | run artifacts in / out |
| `approve(prompt, timeout=..., approvers=[...])` | human gate; suspends run |
| `sleep_for("2h")` / `sleep_until(dt)` | durable timer; suspends run |
| `lock(name, timeout=...)` | cross-run mutex (context manager) |
| `parallel(fn, items, workers=8)` | fan-out; each call journaled separately |
| `call_flow("Other", **kwargs)` | child run, own record, linked in UI |

**Parameters, not `inputs` dicts.** Flow parameters are ordinary typed Python
function arguments; the UI form and validation are derived from type hints
plus an optional `Annotated[...]` metadata (label, help, choices, min/max).
This removes v1's parallel `inputs` / `inputs_schema` duplication.

**Suspension.** `approve()` and `sleep_*()` raise an internal `Suspend`
signal: the child exits cleanly, the run is marked `SUSPENDED`, the worker is
freed, and the run is re-queued when the approval is decided or the timer
fires. Replay makes this cheap — a run can wait for days at zero cost.

---

## 4. Capabilities v1 lacks that this audience requires

### 4.1 Approvals / human-in-the-loop
Prod deploys and outbound office documents need a human "yes" mid-run.
Needs: an approvals inbox in the UI, notification on suspend, optional
approver allow-list and timeout (auto-reject or auto-approve), decision +
comment recorded in the audit log and shown in the report.

### 4.2 Artifacts (files in and out)
Office automation is mostly "here is a spreadsheet, give me a report".
Needs: file upload as a flow parameter (`Annotated[File, ...]`), download
links on the run page, content-addressed storage, size limits, retention,
and virus-scan hook if the org requires it. Replaces v1's base64-in-report
hack for anything larger than a thumbnail (keep inline images for charts).

### 4.3 Live log streaming
Watching a deploy is the primary UX for DevOps runs. Events table +
Server-Sent Events endpoint (`/api/runs/{id}/stream`) + auto-scrolling log
pane. v1's 2-second polling of a whole re-rendered table does not scale.

### 4.4 Notifications
First-class, not an afterthought — nobody watches a history tab.
Channels (Slack / Teams / email / generic webhook) configured centrally;
subscriptions per flow or schedule on events: `failure`, `success`,
`suspended (approval needed)`, `sla_breach`, `schedule_error`. Include a
deep link to the run and the failing step's error.

### 4.5 Concurrency control
`lock("deploy:payments")` and declarative `concurrency=` on a flow. Needed so
two deploys, or two runs processing the same record, cannot overlap. Locks
must expire so a crashed run does not wedge the system forever.

### 4.6 Scheduling
Cron expressions with per-schedule timezone (v1 had interval/daily in server
local time only), plus: overlap policy (skip / queue / allow), catch-up
policy after downtime (`fire once` vs `skip`), and a visible next-run time.
Leader election if more than one worker host runs the scheduler.

### 4.7 Identity, RBAC, audit
- Auth: trust a reverse-proxy header (`X-Forwarded-User`, configurable) as
  the primary mode — every company already terminates SSO at the proxy —
  with direct OIDC as an option and hashed API tokens for machines.
- Roles: `viewer` (see runs), `runner` (start flows), `editor` (schedules,
  environments), `admin` (users, secrets, deletion). Optional per-flow
  restrictions for dangerous flows.
- Audit log for every mutation: run started/cancelled/deleted, approval
  decided, schedule/env/secret changed, with actor and timestamp.

### 4.8 Deployment of flows (not editing in the app)
Flows live in a git repo. A `codeflow deploy` CLI (or CI job / webhook)
registers a **flow version**: parses the modules, records name, parameters,
tags, git SHA and dirty flag, and stores the source. Runs reference an
immutable version, so "which code produced this run" is always answerable
and rollback is a redeploy. The app never writes flow files — this is both
the change-control story and the security boundary.

Optional: build a venv (or container image) per flow version, which finally
makes per-flow `requirements.txt` possible.

---

## 5. Queues, triggers and delivery semantics

Three distinct things get called "queue". Keep them separate in the design.

| | what it is | where it lives |
|---|---|---|
| **Run queue** | durable list of runs waiting for a worker | core (§2.2) — already the execution model |
| **Named queues / worker pools** | routing, priority and concurrency limits for runs | §5.1 |
| **External brokers** | RabbitMQ / Kafka / SQS / MQ messages *starting* runs | §5.2 |

### 5.1 Named run queues and worker pools

One long deploy must not block a two-second office task, some flows must run
on a specific host (network segment, VPN, licensed software, a Windows box
for Excel automation), and an incident runbook should jump ahead of a nightly
report. All three are the same feature:

```python
@flow(name="Deploy service", queue="devops", priority=10)
@flow(name="Monthly report", queue="office", priority=1)
```

```bash
codeflow worker --queues devops,default --concurrency 4
codeflow worker --queues office          --concurrency 8   # on the Windows host
```

Schema: `runs.queue`, `runs.priority`, `runs.available_at` (delayed start and
run-level backoff), plus a `queues(name, max_concurrent, paused)` table.
Claiming is one transaction:

```sql
-- Postgres: ... FOR UPDATE SKIP LOCKED;  SQLite: BEGIN IMMEDIATE
SELECT id FROM runs
 WHERE status = 'queued' AND queue IN (:worker_queues)
   AND available_at <= :now
   AND (SELECT count(*) FROM runs r2
         WHERE r2.queue = runs.queue AND r2.status = 'running')
       < (SELECT max_concurrent FROM queues WHERE name = runs.queue)
 ORDER BY priority DESC, created_at ASC
 LIMIT 1;
```

Pausing a queue (drain before deploy/maintenance) and per-queue concurrency
both fall out of this for free.

### 5.2 External brokers as triggers

The STP shape: a message arrives, one run processes it. Implemented as a
**listener process** — a third process type alongside web and worker,
leader-elected if several hosts run it. It never executes flow code; it
converts messages into durable run rows.

```python
@flow(name="Process payment",
      trigger=broker("amqp://…/payments",
                     dedup_key="message_id",     # idempotency
                     ack="after_enqueue",        # see below
                     max_attempts=5,
                     on_exhausted="park"),       # or "dlq"
      queue="stp", concurrency="1 per account_id")
def process_payment(message: dict) -> dict: ...
```

Targets worth supporting, in order of likely need: AMQP (RabbitMQ), Azure
Service Bus, IBM MQ (common in Dutch banking/insurance back offices), SQS,
Kafka, Redis Streams.

**The semantics that actually matter** — get these wrong and you lose or
double-process transactions:

- **Ack policy.** Default `after_enqueue`: the listener writes the run row in
  a transaction, then acks. Durability hands off cleanly from broker to our
  database, and a slow flow never blocks redelivery timeouts. Use
  `after_complete` only when the broker must remain the source of truth, and
  accept that visibility timeouts now bound your run duration.
- **Deduplication.** Store `dedup_key` (message id, or a business key) with a
  unique index; a redelivered message finds the existing run and acks without
  starting a second one. Non-negotiable for at-least-once brokers.
- **Ordering.** Global ordering is not offered. Per-key ordering via
  `concurrency="1 per <field>"`, which takes the §4.5 lock — messages for the
  same account serialise, everything else runs in parallel.
- **Backpressure.** Listener prefetch is bounded by the target queue's free
  capacity, so a burst does not create fifty thousand queued runs.
- **Poison messages.** After `max_attempts`, `park` moves the run to a
  *needs-attention* state (same inbox as approvals, §4.1) with the payload
  attached and a notification fired — or `dlq` publishes to a dead-letter
  destination. Never silently drop, never retry forever.

### 5.3 Prefer state-driven triggers where a broker is not mandatory

A **poll trigger** — "ask the source of truth what is unprocessed, process
it, mark it done" — is more robust than message consumption and needs no
broker at all: a crash, a missed schedule, or a lost run self-heals on the
next poll, and idempotency is structural rather than bolted on.

```python
@flow(trigger=poll("SELECT id FROM orders WHERE status='NEW'", every="1m",
                   claim="status='PROCESSING'"))
def process_order(id: str) -> dict: ...
```

Same mechanism covers office automation: folder watch (new file appears),
mailbox poll (IMAP), API poll. Recommend this as the default trigger style
and reach for §5.2 only when a broker is already the integration contract.

### 5.4 Explicit non-features

- No item-level work queue *inside* a flow. Fan-out with `parallel()`, or use
  a run per item and let the run queue do the queuing.
- code flow is not a message broker: no persistence guarantees for messages
  it did not create, no topic management, no replay of broker history.

---

## 6. Standard step library ("batteries")

### 6.1 What earns a place in the box

Ship a step only if it satisfies **at least two** of:

1. **used by most flows** — not one team's convention,
2. **easy to get subtly wrong** — encoding, retry semantics, redaction,
   pagination, atomic claims,
3. **benefits from engine integration** — live log streaming, artifacts,
   report blocks, secret redaction, durable sleep, parking.

Criterion 3 is the real test. `http_get()` that only calls `requests.get` is
maintenance debt; one that redacts auth headers from the report, honours
`Retry-After`, and logs the response as a JSON block is a feature. Anything
that is a thin wrapper over a documented SDK belongs in an optional pack
(§6.3) or in a team's own `_lib/`.

### 6.2 Core library (base install)

**Process control** — highest value, and unique to a workflow engine.

| step | purpose | engine integration |
|---|---|---|
| `idempotent(key)` | skip if this business key was already processed | shares the dedup table with broker triggers (§5.2); makes at-least-once safe |
| `require(cond, msg)` | business assertion (reject, not a bug) | **parks** the run in the needs-attention inbox (§4.1) instead of retrying |
| `claim_rows(table, where, claim_as)` | atomically claim unprocessed records | the state-driven trigger pattern (§5.3) in one call |
| `poll_until(fn, every, timeout)` | wait for external state to change | uses durable sleep — releases the worker instead of blocking it |
| `chunk(items, size)` · `dry_run_guard(env)` | batching; env-gated no-op | — |

**Shell and remote (DevOps).**

| step | purpose | engine integration |
|---|---|---|
| `run_command(cmd, timeout=, cwd=, env=)` | subprocess with exit-code check | streams stdout/stderr into the **live** run log (§4.3); scrubs secrets |
| `ssh_run(host, cmd)` · `scp_put/get` | remote exec and copy | keys from the secret store; same streaming |
| `git_clone/pull/tag` | repo operations | records the resulting SHA on the run |

**HTTP and APIs.**

| step | purpose | engine integration |
|---|---|---|
| `http_request(...)` | session reuse, default timeout, retry on 5xx/429 honouring `Retry-After` | redacts auth headers; auto-logs the (truncated) response as JSON |
| `paginate(url, ...)` | follow cursors / `next` links to exhaustion | — |
| `download(url)` | fetch to a run artifact | artifact store (§4.2) |

**Data and office automation** — these pay for themselves via artifacts.

| step | purpose | engine integration |
|---|---|---|
| `read_csv` · `write_csv` | encoding/delimiter sniffing, BOM handling | input file parameter in, downloadable artifact out |
| `read_excel` · `write_excel` | sheet selection, header detection, autofit | same |
| `validate_rows(rows, schema)` | split clean vs rejected, with reasons | rejected set → `log_table` + parked item (the STP reject path) |
| `diff_rows(a, b, key=)` | added / changed / removed | `log_table` with conditional formatting |
| `dedupe` · `template_render` (Jinja) · `to_pdf` · `archive/extract` · `checksum` | everyday plumbing | artifacts |

**Database.**

| step | purpose | engine integration |
|---|---|---|
| `sql_query(dsn, sql, params)` | parameterised read | logs row count and a `log_table` preview automatically |
| `bulk_upsert(rows, table, key)` | idempotent write | pairs with `idempotent()`; what most integrations actually need |

**Notify and monitor.**

| step | purpose | engine integration |
|---|---|---|
| `send_email(..., attachments=[artifact])` | SMTP, HTML + text | uses centrally configured channels (§4.4), not per-flow credentials |
| `notify(channel, text)` | Slack / Teams / webhook | same |
| `http_health(url)` · `tcp_check(host, port)` · `cert_expiry(host)` · `disk_usage(path)` | the four checks behind most monitor flows | map directly onto status/progress widgets |

### 6.3 Optional packs

`pip install codeflow[office,cloud,atlassian,k8s]`. SDK-shaped integrations
(S3/Blob, Jira, Confluence, SharePoint, kubectl/helm, Teams Graph) live here
so the base install stays small. **Graduation rule:** an integration moves
from a pack into core only when three unrelated flows need it. Better to have
two integrations that work than twelve that half-work.

### 6.4 Contract for library steps

Every shipped step must be:

- **importable without side effects** (no connections at import time),
- **safe to retry**, with its idempotency documented — or explicitly marked
  non-idempotent so `retry=` is refused,
- **secret-clean**: never emit credentials into logs, reports or artifacts,
- **typed**, so parameter forms and editor completion work,
- **tested**, with a fake/mock mode for offline CI,
- **listed in `llms.txt`** with a one-line signature, so generated flows use
  the library instead of reinventing it.

---

## 7. UI surface

- **Flows** — list with tags/search, parameter form (typed), Run, Trace view.
- **Run detail** — live streaming log, journal tree (step, attempts, timing,
  result/error), artifacts, images, approve/reject buttons, cancel, restart,
  resume, link to parent/child runs and to the flow version.
- **History** — server-side filtering (flow, status, actor, env, tag, date)
  and full-text search over logs; this must not be client-side as in v1.
- **Approvals** — inbox of runs waiting on the current user.
- **Schedules**, **Dashboards**, **Admin** (users, roles, environments,
  secrets, channels, audit log, retention).
- **Queues** — depth, running count, per-queue concurrency, pause/drain, and
  worker health (which hosts serve which queues, last heartbeat).
- **Triggers** — listener status per broker/poll source, lag or backlog,
  last message, parked/dead-lettered items with a re-drive button.

Build it with a real frontend toolchain (Svelte or Preact + Vite) served as
static files from the same process. v1's single-file vanilla HTML reached
~1500 lines and would not survive approvals + streaming + RBAC screens.

---

## 8. Non-goals

- Multi-tenant isolation between users who do not trust each other. Flow code
  runs with the worker's privileges; authoring rights = server ownership.
  If that changes, it is a different product (see §8).
- HA / distributed scheduling at v1. Single host, multiple workers.
- Visual flow builder. The differentiator is that flows are code.
- Being a data-pipeline framework (no dataframe passing, lineage, backfills).
- Replacing Temporal for long-running, mission-critical sagas.

---

## 9. Build order

Each milestone lands the slice of the step library (§6) that its
infrastructure makes possible — the library grows with the engine rather
than as a separate project.

1. **M1 — engine + CLI, no UI.** Journal, replay, step keys, determinism
   guard, SQLite schema, subprocess execution, retries/timeouts/cancel,
   artifacts, named run queues (§5.1), `codeflow run` / `deploy` / `worker`
   / `ls` commands. Ship with pytest coverage of retry/cancel/resume
   semantics from day one — v1 never had a test suite and every change was
   verified ad hoc.
   *Steps: process control (`idempotent`, `require`, `chunk`,
   `dry_run_guard`) + `run_command` — enough to be useful headless.*
2. **M2 — web app.** Runs list/detail, live streaming, on-demand report
   rendering, flow list + parameter forms, cancel/restart/resume, queue view.
   *Steps: `http_request`, `paginate`, `download`, `sql_query`, and the
   CSV/Excel pair — artifacts and report blocks now exist to plug into.*
3. **M3 — team readiness.** Proxy auth, roles, audit log, notifications.
   *Steps: `send_email`, `notify` on the central channel config.*
4. **M4 — process features.** Approvals, locks, cron scheduler with
   timezones/overlap/catch-up policies, webhooks with tokens.
   *Steps: `poll_until` (durable sleep), `claim_rows`, `validate_rows`
   (parking path now exists).*
5. **M5 — triggers.** Listener process: poll triggers first (§5.3, no broker
   needed, covers most cases), then one broker integration end-to-end with
   dedup / ack / parking / re-drive before adding a second.
6. **M6 — dashboards.** Port the v1 widget system and renderer.
   *Steps: the monitor set (`http_health`, `tcp_check`, `cert_expiry`,
   `disk_usage`) — they exist to feed widgets.*
7. **M7 — operations.** Search, metrics (volume, failure rate, duration
   trends, queue depth), retention jobs, `/health`, graceful shutdown,
   backup guidance. Optional packs (§6.3) as demand appears.

## 10. Open decisions (choose at the time)

- **SQLite vs Postgres default.** SQLite keeps "clone and run"; Postgres is
  needed for multiple worker hosts. Recommend: SQLite default, one storage
  interface, Postgres switch by connection string.
- **Isolation depth.** Subprocess (recommended) vs container per run
  (stronger, needed only if flow authors are not fully trusted).
- **Per-flow dependencies.** Shared venv (simple) vs venv/image per flow
  version (isolated, slower deploys).
- **Reuse of v1 report renderer.** Porting it saves weeks; rewriting it as
  server-rendered components is cleaner. Recommend port, then refactor.
- **Which broker first** (§5.2). Pick the one already used by the process
  you are automating; do not build three half-integrations.
- **Listener process placement.** Same host as workers (simple) vs its own
  deployment (isolates broker credentials and connection churn).
- **Step library packaging** (§6.3). Single package with extras (simple to
  version) vs separately released integration packages (independent release
  cadence, more repos to maintain).
- **Package/product name and whether it is open-sourced internally.**

## 11. Appendix — hard-won pitfalls from v1

Encode these as tests in v2; each cost real debugging time.

- Two steps with the same name: the second silently overwrote the first.
- `condition=` is evaluated against the context **before** the step runs — a
  step cannot gate on data it produces itself.
- Thread-based timeouts cannot kill the call; the abandoned attempt keeps
  running in the background. Only process isolation fixes this.
- Secrets are masked in reports and the UI, but **not** in log lines — a
  `log()` of a full request URL leaks tokens. v2: scrub known secret values
  from all event payloads at write time.
- Reports embedding base64 images are self-contained but expensive to
  re-render; artifacts should be referenced, not inlined, above thumbnail
  size.
- A schedule whose run outlasts its interval will stack up without an
  overlap guard.
- Restoring context from JSON degrades non-serializable values to strings;
  the journal must record only serializable step results, and say so.
- Running accumulators (`ctx["total"] + n`) break with parallel iterations and
  double-count on resume; aggregate from collected results instead.
- Parent `retry=` around a child workflow re-runs the entire child, and
  multiplies with the child's own retries.
- Discovery that re-imports every flow module on every API request is a
  hidden constant cost; cache on mtime (or, in v2, deploy-time registration
  removes the problem entirely).
- The in-memory run queue loses queued work on restart. Business processes
  should be **state-driven** ("find everything unprocessed and process it")
  rather than depending on a single fire-and-forget trigger — see §5.3.
- A single global run pool means one slow flow starves everything else; named
  queues (§5.1) exist because "the nightly export blocked the incident
  runbook" is a question of when, not if.
