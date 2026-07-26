# code flow — REST API

Base URL: `http://127.0.0.1:8000` (or wherever the server runs).
All requests and responses are JSON (`Content-Type: application/json`).
There is no authentication — the API is intended for personal, local use.

Interactive Swagger docs are always available at **`/api/docs`**.

Errors follow FastAPI's shape:

```json
{ "detail": "Unknown workflow: Foo" }
```

| Code | Meaning |
|------|---------|
| 404  | Unknown flow / run / schedule / environment |
| 409  | Operation conflicts with run state (e.g. deleting a RUNNING run) |
| 422  | Invalid body (bad schedule spec, malformed JSON, typed-input validation) — input errors come back as `{"detail": {"message": "input validation failed", "errors": {"amount": "must be >= 0.01"}}}` |
| 500  | Flow failed to start / internal error |

Run statuses: `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED` (user), `INTERRUPTED`
(server stopped mid-run). Step statuses additionally include `SKIPPED`
(condition false) and `PENDING`.

---

## Workflows

### `GET /api/flows`

List every workflow discovered in `workflows/` (recursive; re-scanned on
each call, so edits appear without a restart).

```json
{
  "flows": [
    {
      "name": "OrderFlow",
      "description": "Order processing demo — condition + retry + branching",
      "dashboard": false,
      "tags": ["billing", "demo"],
      "file": "Flow.py",
      "inputs": { "amount": 120, "customer": "ACME Corp" },
      "start": "begin",
      "steps": [
        {
          "name": "Charge",
          "next": "Notify",
          "condition": "not env.get('dry_run')",
          "loop": null,
          "retry": 4,
          "retry_delay": 0.5,
          "retry_on": ["ConnectionError", "TimeoutError"],
          "continue_on_error": false,
          "timeout": null,
          "is_start": false,
          "doc": "…"
        }
      ]
    }
  ],
  "errors": [ { "file": "Broken.py", "error": "SyntaxError: …" } ]
}
```

### `POST /api/run/{flow_name}`

Start a run in the background. Returns immediately with the run id.

Body (all fields optional):

```json
{ "inputs": { "amount": 250 }, "env": "prod" }
```

A bare inputs object (`{ "amount": 250 }`, no `inputs`/`env` keys) is also
accepted for backward compatibility. `inputs` override the flow's declared
defaults key-by-key; `env` names a JSON file in `environments/`.

```bash
curl -X POST localhost:8000/api/run/OrderFlow \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"amount": 250}, "env": "prod"}'
```

Response `200`:

```json
{ "run_id": "9bf75928e9fc", "workflow": "OrderFlow", "environment": "prod" }
```

Poll `GET /api/runs/{run_id}` for progress. Runs execute on a thread pool
(8 workers) — multiple flows, or the same flow multiple times, run
concurrently.

---

## Runs & history

### `GET /api/runs`

All runs, newest first — live in-memory runs merged over the persisted
history (retention keeps the newest `CODEFLOW_HISTORY_LIMIT`, default 500).

```json
{
  "runs": [
    {
      "run_id": "9bf75928e9fc",
      "workflow": "OrderFlow",
      "status": "SUCCESS",
      "started_at": "2026-07-22T10:44:53.123+02:00",
      "ended_at": "2026-07-22T10:44:54.001+02:00",
      "duration_ms": 878.0,
      "error": null,
      "steps": 5,
      "parent_run_id": null,
      "environment": "prod",
      "tags": ["billing", "demo"]
    }
  ]
}
```

`parent_run_id` is set on sub-workflow runs started via
`self.call_workflow(...)`.

### `GET /api/runs/{run_id}`

Full run record: inputs, outputs, masked `env_values`, `context` snapshot,
`widgets`, and per-step detail (status, attempts, iterations, logs, result,
error, traceback).

### `POST /api/runs/{run_id}/cancel`

Cooperative cancel — takes effect at the next step boundary, loop
iteration, retry wait, or timeout poll. Cancelling a run also cancels its
sub-workflows. `404` if unknown/finished, `409` if not RUNNING.

```json
{ "run_id": "9bf75928e9fc", "cancelling": true }
```

### `POST /api/runs/{run_id}/restart`

Start a **new** run of the same flow with the original run's inputs and
environment. Env values are re-resolved fresh at restart time. `404` if the
flow or environment no longer exists; `422` if the flow's `inputs_schema`
changed and the old inputs no longer validate.

Response: `{ "run_id": "…", "workflow": "…", "environment": "…", "restarted_from": "…" }`

### `DELETE /api/runs/{run_id}`

Delete one finished run (report + record + index entry). `409` if the run
is still RUNNING — cancel it first.

### `POST /api/runs/bulk-delete`

```json
{ "run_ids": ["a1…", "b2…"] }
```

Response: `{ "deleted": [...], "skipped": [...] }` — RUNNING runs are
skipped, never deleted.

### `DELETE /api/runs`

Delete **all finished** runs. RUNNING runs are kept.
Response: `{ "deleted": 42 }`.

### `GET /reports/{run_id}`

The run's standalone HTML report (also served live while the run is in
progress). Not JSON — this is the human-readable page linked from the UI.

### `POST /api/hooks/{flow_name}`

Webhook trigger for external systems. The flow must set `webhook = True`
(others return 404). Body = inputs directly, or `{"inputs": {...}, "env": "prod"}`;
`?env=` query param also works.

Auth: if the flow sets `webhook_token` (or the server sets
`CODEFLOW_WEBHOOK_TOKEN`), pass it via `X-Webhook-Token` header or
`?token=`; wrong/missing token → `401`.

```bash
curl -X POST "localhost:8000/api/hooks/ParallelFetchFlow?env=dev" \
  -H "Content-Type: application/json" -d '{"count": 6}'
```

Response `200`: `{ "run_id": "…", "workflow": "ParallelFetchFlow", "environment": "dev" }`
— the run is asynchronous and appears in the history like any other.

---

## Environments

### `GET /api/environments`

Environments from `environments/*.json`. `${VAR}` references are resolved
from the server's OS environment; secret-looking keys and resolved values
are **masked** in this response (runs receive the real values).

```json
{
  "environments": [
    { "name": "prod",
      "values": { "api_url": "https://api.example.com", "api_token": "••••••" } }
  ],
  "errors": [
    { "file": "prod.json", "error": "warning: OS env var(s) not set: CODEFLOW_PROD_TOKEN" }
  ]
}
```

---

## Dashboards

### `POST /api/dashboards/{flow_name}/render`

Run a dashboard flow **synchronously** and return its widgets. Renders are
transient: no history entry, no report (auto-refresh would flood the
history). Use `POST /api/run/{flow}` when you want a persisted snapshot.

Body: `{ "inputs": {...}, "env": "dev" }` (both optional).

```json
{
  "run_id": "3539851477aa",
  "status": "SUCCESS",
  "error": null,
  "duration_ms": 0.7,
  "widgets": [
    { "type": "metric", "title": "Orders today", "value": 128 },
    { "type": "chart", "title": "By category", "chart": "bar",
      "data": { "Books": 850, "Games": 1200 }, "size": "wide" }
  ],
  "steps": [ { "name": "Collect", "status": "SUCCESS", "error": null } ]
}
```

Widget types: `metric`, `stat`, `status`, `progress`, `table`, `chart`
(`bar`/`line`/`area`/`pie`), `list`, `alert`, `text`, `json`, `html`,
`link`, `section`. Size: `""` | `"wide"` | `"full"`.

---

## Schedules

Times are the **server's local time**. Schedules persist in
`history/schedules.json`. An overlap guard skips a fire while the previous
run of that schedule is still RUNNING (retried next tick, ~15 s).

### `GET /api/schedules`

```json
{
  "schedules": [
    {
      "id": "722d799a84",
      "flow": "HelloFlow",
      "type": "interval",
      "every_minutes": 60,
      "inputs": { "who": "scheduler" },
      "env": null,
      "enabled": true,
      "created_at": "2026-07-22T14:17:47",
      "last_run_at": "2026-07-22T15:17:47",
      "last_run_id": "7cb77fee9a59",
      "last_error": null,
      "when": "every 60 min",
      "next_run_at": "2026-07-22T16:17:47"
    }
  ]
}
```

### `POST /api/schedules`

Interval:

```json
{ "flow": "HelloFlow", "type": "interval", "every_minutes": 30,
  "inputs": { "who": "cron" }, "env": "dev" }
```

Daily (days: `0`=Mon … `6`=Sun; omit/empty = every day):

```json
{ "flow": "OrderFlow", "type": "daily", "time": "07:30", "days": [0,1,2,3,4] }
```

`422` on invalid spec (`every_minutes < 1`, bad `time`, unknown `type`).

### `PATCH /api/schedules/{id}`

Partial update — send only what changes, e.g. `{ "enabled": false }` or
`{ "every_minutes": 15 }`.

### `DELETE /api/schedules/{id}`

Remove the schedule. Response: `{ "deleted": "722d799a84" }`.

### `POST /api/schedules/{id}/run`

Fire immediately (bypasses the overlap guard — explicit intent).
Response: `{ "run_id": "…" }`; the schedule's `last_run_*` fields update.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODEFLOW_WORKFLOWS_DIR` | `./workflows` | Flow files (Docker: `/data/workflows`) |
| `CODEFLOW_ENVIRONMENTS_DIR` | `./environments` | Env JSON files |
| `CODEFLOW_HISTORY_DIR` | `./history` | Reports, records, schedules |
| `CODEFLOW_SCHEDULES_FILE` | `<history>/schedules.json` | Schedule store |
| `CODEFLOW_HISTORY_LIMIT` | `500` | Max runs kept (oldest finished pruned) |
| `CODEFLOW_WEBHOOK_TOKEN` | *(unset)* | Global token required by `/api/hooks/*` (per-flow `webhook_token` overrides) |
| `CODEFLOW_HOST` / `CODEFLOW_PORT` | `127.0.0.1` / `8000` | Bind address |
