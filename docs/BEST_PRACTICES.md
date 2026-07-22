# Writing workflows — best practices

Practical guidance for writing flows that are easy to read, debug from
their reports, and safe to retry and schedule. Everything here maps to how
the engine actually behaves.

## Shape of a good flow

**Keep steps small and single-purpose.** One step = one unit of work that
can fail and be retried on its own: fetch, validate, transform, write,
notify. If a step does three things and the third fails, a retry redoes
all three. Small steps also make reports readable — each gets its own
status, timing, logs and result.

**Name steps like actions.** `Validate`, `Charge`, `SendReceipt` — the
report and flow diagram read as a sentence. Avoid spaces if you plan to
use derived keys in expressions: a step named `"Fetch Data"` is referenced
in `next=` with the space, but its context keys are sanitized to
`Fetch_Data_result` / `Fetch_Data_results` / `Fetch_Data_error`.

**Describe the flow.** Set `description` (shown in the UI) and give
non-obvious steps a docstring — it appears in the flow's metadata.

```python
class SyncInvoices(Workflow):
    description = "Pull unpaid invoices from the API and post them to accounting"
    tags = ["billing"]
    inputs = {"days_back": 7}          # sensible defaults, overridable per-run
```

## Context discipline

The context (`ctx`) is the flow's shared state. Rules of thumb:

- **Return dicts, don't mutate.** `return {"total": ctx["total"] + n}` is
  visible in the report as the step's result; `ctx["total"] += n` works
  but hides the change.
- **Seed accumulators before loops.** `return {"files": [...], "rows": 0}`
  in the step *before* a loop, then fold: `return {"rows": ctx["rows"] + n}`.
- **Keep it JSON-ish and reasonably small.** Everything in ctx lands in the
  report's Context section (values over ~2 KB are truncated, non-JSON
  values become reprs). Don't carry a 50 MB DataFrame through ctx — write
  it to a file and pass the path.
- **Don't shadow the built-ins.** `env` is the environment dict; avoid
  returning a key named `env` from a step.

## Conditions and loops

`condition=` and `loop=` are Python expressions evaluated against the
context — bare names are ctx keys:

```python
@step(name="Publish", condition="rows > 0 and not env.get('dry_run')")
@step(name="Process", loop="f in files")
@step(name="Retry failed", loop="item in [x for x in results if x['error']]")
```

- A falsy condition **skips** the step (status SKIPPED) and continues with
  `next` — it is not a branch. For real branching return
  `{"__next__": "OtherStep"}` from the previous step.
- The loop iterable is evaluated **once** at loop start; merging new items
  into ctx mid-loop doesn't extend the iteration.
- Loop results are auto-collected in `ctx["<Step>_results"]`; remember the
  last iteration's returned keys also sit merged in ctx.

## Failure design (the important one)

Decide per step what failure should mean, and encode it:

```python
@step(name="Charge", retry=4, retry_delay=2,
      retry_on=(ConnectionError, TimeoutError), timeout=30)
```

- **Retry only what's retryable.** Use `retry_on=` so transient network
  errors retry but a `KeyError` (a bug) fails immediately instead of
  hammering an API 10 times.
- **Always set `timeout=` on network steps.** A hung HTTP call without a
  timeout blocks a worker forever. Note the timed-out attempt is
  *abandoned*, not killed — see idempotency below.
- **`raise` to fail, `return` to succeed.** Use `check=True` on
  subprocesses; raise `ValueError` on bad data. A step that swallows
  exceptions and returns normally looks green in the report.
- **`continue_on_error=True` for best-effort steps** (metrics, cache warm,
  notifications). The step shows FAILED, the run continues, and the error
  is available as `ctx["<Step>_error"]` for a later step or condition.
- **Make side-effecting steps idempotent.** Retries re-run the whole step
  body; timeouts can leave an abandoned attempt finishing in the
  background; schedules re-fire. Charging money or sending email should
  use a dedup key (order id, message id) or check-before-write, so a
  duplicate execution is harmless.

## Sub-workflows

`self.call_workflow("Other", inputs={...})` runs the whole child flow and
returns its outputs. Use it to compose, not to hide:

- Good: a `DailyBilling` parent looping customers over an `OrderFlow`
  child — each child gets its own report, and a parent `retry=` re-runs
  the entire child.
- Watch the multiplication: parent `retry=2` × child step `retry=4` = up
  to 15 executions of that child step. Keep retries at ONE level.
- Children inherit the parent's environment; override with `env="prod"`
  only deliberately.

## Environments and secrets

- Environment = *where* the flow runs (URLs, flags); inputs = *what* it
  runs on. Don't put per-run values in env files.
- Support `dry_run` in flows with side effects:
  `condition="not env.get('dry_run')"` on the dangerous step gives you a
  safe way to test against prod config.
- Secrets: reference OS vars — `"api_token": "${MY_TOKEN}"` — never
  literal values. They're masked in the UI and reports automatically, but
  **`self.log()` output is not scrubbed** — never log a secret or echo a
  full request that contains one.

## Organization

- **Subfolders are groups**: `workflows/billing/…` auto-tags flows with
  `billing`. Use folders for domains, explicit `tags` for cross-cutting
  labels (`demo`, `critical`).
- **Shared code goes in `_lib/`** (or any `_folder/`): ignored by
  discovery but importable — `from _lib.helpers import money`. A helper
  file *outside* an underscore folder is executed on every scan; keep
  side effects out of module top-level.
- **Drafts in `workflows/_drafts/`** — invisible to the engine until moved.
- Extra pip packages: add to `requirements.txt` (rebuild the Docker image);
  `requests`, `PyYAML` and `dirtyjson` are already available.

## Scheduling

- The overlap guard prevents a schedule from stacking runs, but design for
  it anyway: a scheduled flow should finish comfortably within its
  interval, or run less often.
- Scheduled flows can't ask questions — validate inputs early and fail
  loudly (a clear `ValueError` beats a confusing downstream crash).
- Check `last_error` in the Schedules tab after changing a flow's name or
  inputs — a rename breaks the schedule at fire time, not at save time.

## Dashboards

- Keep dashboard flows **fast and read-only** — they run on every refresh
  and auto-refresh can be 10s. Move slow collection into a normal
  scheduled flow that writes a small JSON/SQLite snapshot; let the
  dashboard just read and render it.
- Build all widgets even when a source fails: wrap risky fetches with
  `continue_on_error=True` and show an `alert` widget instead of a broken
  dashboard.

## Testing a flow without the UI

Flows are plain classes — run them headless from a scratch script or test:

```python
from engine import WorkflowRunner
from engine.registry import discover_workflows

registry, errors = discover_workflows("workflows")
rec = WorkflowRunner(registry["OrderFlow"], inputs={"amount": 50}).run()
assert rec.status == "SUCCESS"
assert [s.status for s in rec.steps].count("SKIPPED") == 1
print(rec.outputs, rec.context)
```

`rec.steps` gives per-step status/attempts/logs — enough to assert retry
and skip behavior deterministically (seed `random`, use tiny
`retry_delay`).

## Common pitfalls

| Pitfall | What happens | Do instead |
|---|---|---|
| `next="Setp2"` typo | Run fails at that step: *Unknown step* | The flow diagram shows a ⚠ node — check it after wiring |
| Two steps with the same `name=` | Second silently wins | Unique names; default is the function name |
| Condition on data the step itself produces | Always evaluates against *pre-step* ctx | Put the condition on the *next* step |
| Heavy work at module top level | Runs on every folder re-scan | Do work inside steps; keep imports cheap |
| Mutating a list while `loop=`-ing over it | Skipped/duplicated iterations | Build a new list |
| Secrets in `self.log()` | Stored verbatim in reports | Log ids/counts, never credentials |
| Infinite `next` cycles | Run aborts at 1000 steps | Loop with `loop=`, not with `next` chains |
