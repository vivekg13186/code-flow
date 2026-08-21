<p align="center"><img src="code_flow.png" alt="code flow" width="420"></p>

<p align="center">
A small workflow engine for Python. Flows are plain functions; every run
leaves a report you can read afterwards.
</p>

[![code flow demo](docs/code-flow-demo.gif)](https://vimeo.com/1212565346)

*5× speed preview — click for the [full demo](https://vimeo.com/1212565346).*

---

## What this is

You have a handful of scripts that call APIs, move files, reconcile records.
They work, but when one fails at 3am you have no idea which step broke or what
the values were. code-flow gives those scripts a UI, a history and a resume
button without asking you to learn a DSL or run a broker.

A workflow is a Python class with one `@flow` method. The body is ordinary
Python — `if`, `for`, `try`, function calls. The `@step` methods it calls are
journaled, which is what makes retries, resume and the per-step report
possible. Flows are files, so git handles versioning, review and rollback.

**What it is not:** a distributed scheduler. It is one process with a thread
pool, a folder of JSON, and no authentication. It is built for one developer
or a small team running internal automation on a machine they control. If you
need multi-tenant queues and horizontal scale, use Temporal or Airflow.

**Contents** — [1. Install](#1-install) · [2. Write a flow](#2-write-a-flow) ·
[3. Run and operate](#3-run-and-operate) · [4. Deploy](#4-deploy) ·
[5. Reference](#5-reference)

---

# 1. Install

Python 3.10 or newer. No database, no broker, no external services.

### macOS / Linux

```bash
bash scripts/install.sh    # creates a venv, installs deps, asks where your flows live
bash scripts/start.sh      # starts the server and opens the browser
```

### Windows

Double-click `scripts\install.bat`, then `scripts\start.bat`. You need
[Python 3.10+](https://www.python.org/downloads/) installed with **"Add
Python to PATH"** ticked — the installer will tell you if it can't find it.

### Without the scripts

```bash
pip install -r requirements.txt
python app.py
```

Either way you end up at **http://127.0.0.1:8000**. If the sample flows are
present you should see them listed; click **▶ Run** on `HelloFlow` and then
open the report from the History tab. That round trip is the whole product in
about ten seconds.

### Configuration

The installer writes your answers to `.codeflow.env`. Edit that file (or
re-run the installer) to change them:

```ini
CODEFLOW_WORKFLOWS_DIR=./workflows
CODEFLOW_PORT=8000
```

Every setting is a plain environment variable, so it works identically in a
shell, in `.codeflow.env`, or in Docker. See
[`.codeflow.env.sample`](.codeflow.env.sample) for the annotated list and
[§5](#configuration-reference) for the full table.

> **There is no login.** Bind to `127.0.0.1` (the default) and reach it
> remotely over Tailscale or SSH rather than exposing the port.

---

# 2. Write a flow

## Your first flow

Create `workflows/Greet.py`:

```python
from engine import Workflow, flow, step

class GreetFlow(Workflow):
    description = "Says hello"          # shown in the UI
    inputs = {"name": "world"}          # default inputs

    @flow
    def main(self, ctx):
        greeting = self.build_greeting(ctx["name"])
        self.log(greeting)
        return {"greeting": greeting}   # a returned dict becomes the run outputs

    @step()
    def build_greeting(self, name):
        return f"Hello, {name}!"
```

Save it. Don't restart anything — the folder is re-scanned on every request,
so the flow appears in the UI immediately. Hit **▶ Run**.

Three things are worth noticing. `main` is normal Python, not a graph
declaration. `build_greeting` receives a real argument and returns a real
value — nothing is merged into a shared context dict behind your back. And
because the call went through `@step`, the report shows its arguments, its
return value and how long it took.

## Where flows live

Any `.py` file under `workflows/` is imported, at any depth. Every subclass of
`Workflow` that has a `@flow` method is registered under its class name.

```
workflows/
├── Deploy.py              -> DeployFlow
├── billing/
│   └── Invoices.py        -> InvoiceFlow, tagged "billing"
├── _lib/
│   └── helpers.py         -> shared code, not scanned
└── _drafts/               -> ignored entirely
```

Folders on a flow's path become **tags** automatically, which is what drives
the tag bar and the history filters — you don't declare them. Files and
folders starting with `_` are skipped, so `_drafts/` is a safe scratch area
and `_lib/` is where shared helpers go. The workflows root is on `sys.path`,
so import them as `from _lib.helpers import parse_invoice`.

> Module top-level code runs on **every** scan. Keep imports cheap and don't
> open connections at import time — the linter flags this as CF013.

## Steps

A step is a method with `@step()`. It takes arguments, returns a value, and
raises on failure. That's the entire contract.

```python
@step(retry=3, retry_delay=1, retry_backoff=2,
      retry_on=ConnectionError, timeout=30)
def fetch(self, url):
    self.log(f"GET {url}")
    return requests.get(url, timeout=10).json()
```

| Option | What it does |
|---|---|
| `name` | Label in the report. Defaults to the method name. |
| `retry` | Retries after a failure. Total attempts = `retry + 1`. |
| `retry_delay` | Seconds to wait between attempts. |
| `retry_backoff` | Multiplier per further attempt. `retry_delay=2, retry_backoff=3` waits 2s, 6s, 18s. Default `1` means a fixed delay. |
| `retry_on` | Exception class or tuple that counts as retryable. Anything else fails immediately — use it so a `ValueError` doesn't burn three attempts. |
| `continue_on_error` | After the last attempt, mark the step FAILED and return `None` to the caller instead of raising. |
| `timeout` | Seconds per attempt, then `StepTimeoutError`. |

All of this applies **per call**. A step called inside a loop gets its own
attempts and its own journal entry for each iteration, which is why a failed
loop can resume at the item that failed.

> A timed-out or cancelled step is *abandoned*, not killed — Python can't kill
> a running thread. The call keeps going in the background until it returns.
> Make steps with external side effects idempotent.

## Control flow

There is no `next=`, `condition=` or `loop=`. You write Python:

```python
@flow
def main(self, ctx):
    artifact = self.build(ctx["service"])
    hosts = self.discover_hosts(ctx["service"])

    if ctx["canary"]:                                   # branch
        self.push(artifact, hosts[0])
        self.verify(hosts[0])

    parallel_map(lambda h: self.push(artifact, h),      # fan out, 3 at a time
                 hosts[1:], workers=3)

    try:                                                # compensate on failure
        self.smoke_test(ctx["service"])
    except Exception:
        self.rollback(ctx["service"])
        raise

    return {"deployed": True}
```

`parallel_map` (imported from `engine`) runs calls on threads and journals
each one separately. Threads mean it's right for I/O — HTTP, disk, database —
and useless for CPU-bound work.

To pause, use `self.sleep(30)` rather than `time.sleep`. It's cancellable, and
it's journaled so a resumed run doesn't sleep again.

## Inputs

For anything beyond a couple of defaults, declare inputs as a **dataclass**.
You get a real form in the run dialog and validation on every start path — UI,
API, webhook, scheduler, sub-workflow:

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

@dataclass
class OrderInputs:
    customer: str                                     # no default -> required
    amount: float = field(default=120.0,
                          metadata={"min": 0.01, "help": "order total"})
    priority: Literal["low", "normal", "high"] = "normal"
    notify: bool = False
    extra: Optional[dict] = None

class OrderFlow(Workflow):
    inputs = OrderInputs          # the class itself, not an instance
```

The annotation picks the widget:

| Annotation | Field |
|---|---|
| `str` | text |
| `int` | number, integer |
| `float` | number |
| `bool` | checkbox |
| `Literal[...]` or an `Enum` subclass | dropdown |
| `list`, `dict`, anything else | JSON box |
| `Optional[X]` | `X`, not required |

Anything an annotation can't express goes in `field(metadata={...})`: `min`,
`max`, `help`, `label`, `options`, `required`, `type`.
`Annotated[int, {"min": 1}]` works too.

Values are coerced on the way in (`"42"` → `42`, `"true"` → `True`). Bad input
gets a `422` with per-field errors from the API; runs started any other way
fail immediately with a readable message instead of crashing halfway through.
Undeclared keys pass through untouched, the body still receives a plain dict,
and the dialog keeps an "edit as JSON" escape hatch.

An `Enum` field carries its `.value` into `ctx`, not the member — journal
entries have to stay JSON-serializable.

Typing is optional. `inputs = {"amount": 120}` still works; it just skips
validation and renders a generic form.

## Environments and secrets

Each file in `environments/` is an environment, named after the file, picked
per run from a dropdown. Inside a flow it's `self.env` (and `ctx["env"]`).

```json
// environments/prod.json
{
  "api_url": "https://api.example.com",
  "api_token": "${DEPLOY_TOKEN}",
  "dry_run": false
}
```

`${VAR}` reads from the OS environment at load time, so real secrets live in
your shell or systemd unit, not in the repo. Resolved values and keys that
look secret (`*token*`, `*secret*`, `*password*`, `*api_key*`, `*credential*`,
`*private*`) are masked in the UI and reports.

Masking does **not** apply to `self.log` lines. If you log a token it appears
in the report in clear text. The linter warns about this (CF012).

A `dry_run` flag pairs well with this:

```python
if not self.env.get("dry_run"):
    self.charge(customer, amount)
```

## Logging and reports

Every run writes an HTML report to `history/`, served at `/reports/<run_id>`.

```python
self.log("plain line")
self.log_json({"rows": 42, "decision": "publish"}, title="Decision input")
self.log_table(rows, title="Discovered files")        # renders a real table
self.log_image(fig, title="Sales chart")              # matplotlib, path, bytes or data URI
self.outputs({"invoice_id": 91})                      # merge into run outputs
```

Images embed as base64 so a report stays a single self-contained file you can
email (3 MB per image, 20 per step). Tables cap at 200 rows.

## The standard step library

`self.http`, `self.fs`, `self.sh` and `self.db` are ready-made steps for the
things nearly every flow does. They are ordinary journaled steps — they appear
in the report with their arguments and results, they obey retries and
timeouts, and a resume skips the ones that already finished.

```python
@flow
def main(self, ctx):
    orders = self.http.get(f"{ctx['api']}/orders")["json"]
    self.fs.write_json("/data/orders.json", orders)
    self.db.executemany("ops.db",
        "INSERT INTO orders (id, total) VALUES (?, ?)",
        [(o["id"], o["total"]) for o in orders])
    self.sh.run("./reconcile.sh --today")
```

**`self.http`** — `get` `post` `put` `patch` `delete` `download`, plus
`request(method, url)` which does **not** retry, for calls that must happen at
most once. Each returns a dict:

```python
{"status": 200, "ok": True, "json": <parsed or None>, "text": "...",
 "headers": {...}, "elapsed_ms": 41.2, "url": ..., "method": ...}
```

Defaults are 2 retries with 1s/2s backoff and a 30s request timeout. Retries
fire on network errors and on 5xx/429; a 4xx raises `HttpClientError`
immediately rather than burning attempts on a request that is simply wrong.
`check=False` returns the response instead of raising, which is how you branch
on a 404.

**`self.fs`** — `read_text` `read_json` `read_csv` `read_yaml` `glob` `stat`,
`write_text` `write_json` `write_csv`, `copy` `move` `remove` `ensure_dir`
`archive` `unpack`. Writes create parent folders; `copy`/`move` refuse to
clobber unless you pass `overwrite=True`; `stat` returns
`{"exists": False, ...}` for a missing path instead of raising, so it's the
way to branch on existence. `read_json(path, dirty=True)` falls back to
`dirtyjson` for files with trailing commas or comments.

**`self.sh`** — `run(cmd, ...)` returning
`{cmd, returncode, ok, stdout, stderr, duration_ms}`, and `which(program)` as
a preflight check. A non-zero exit raises `ShellError` unless `check=False`.
A **string** command goes through the shell so pipes and globs work; a
**list** does not — use the list form whenever any part comes from run inputs,
because a string built from user input is a shell injection. The `timeout=`
argument kills the process, unlike the step-level timeout which only abandons
it.

**`self.db`** — SQLite from the standard library: `query` `query_one`
`execute` `executemany` `script` `table_exists`. Good for the local state an
internal flow needs — a ledger of what's been processed, a dedupe table, a
small reporting store. Each call opens its own connection, so it is safe under
`parallel_map`. Parameters are always bound; never build SQL with f-strings.

```python
rows = self.db.query("ops.db",
                     "SELECT id, total FROM orders WHERE day = ?", [ctx["day"]])
```

Two constraints the library respects, and yours should too. **Return values
are JSON-serializable** — the journal stores and restores them verbatim, so
nothing hands back a file handle or a connection. And **no argument is a
callable**: a function's `repr()` contains its memory address, which changes
every process, so a step keyed on one would never match its journal entry and
resume would silently re-run it.

Large payloads are truncated before they reach the journal — 200k chars for an
HTTP body or text file, 100k for command output, 50k rows for `read_csv` and
`db.query` — and the step says so when it happens. If you're moving more than
that, do the work inside one of your own steps and return a summary instead of
pulling it all through the flow body.

`workflows/examples/StdLib.py` runs all of this against a temp folder.

## Calling another flow

```python
@step(timeout=300)
def bill(self, order):
    out = self.call_workflow("OrderFlow", inputs=order)
    return out["charged_amount"]
```

The child gets its own run record and report, linked from the parent in the
history with a `↳`. It returns the child's outputs and raises if the child
fails. It inherits the parent's environment unless you pass `env="prod"`, and
cycles are detected.

A `retry=` on the calling step re-runs the **entire** child flow, and
multiplies with the child's own retries. Usually you want the retry inside the
child.

## The one rule: keep the flow body pure

On resume, the flow body **re-executes from the top**. Completed steps return
their recorded result instantly, so execution effectively continues where it
stopped. That only works if the body is deterministic.

```python
@flow
def main(self, ctx):
    stamp = datetime.now()          # WRONG — different value on resume
    open("out.txt", "w").write(x)   # WRONG — repeats the side effect
    data = self.load()              # right — journaled, returns the recorded value
```

So: real work and side effects belong inside `@step` methods. No
`datetime.now()`, `random`, `uuid` or I/O in the body. Use `self.sleep()`
instead of `time.sleep()`. Keep step return values JSON-serializable so they
restore exactly.

`workflows/examples/ResumeDemo.py` is a runnable demonstration — run it, fix
the "outage" it describes, then hit resume and watch the earlier steps come
back marked "carried over".

## Lint before you commit

```bash
bash scripts/lint.sh                  # or: python -m engine.lint workflows
python -m engine.lint workflows --strict --json
```

It catches duplicate step names, missing or duplicate `@flow`, leftover
graph-era syntax, `retry=` without `timeout=`, swallowed exceptions, secrets
in log lines, import-time side effects, and non-determinism or side effects in
a flow body. Exit code 1 on errors; `--strict` also fails on warnings.

Worth wiring into pre-commit or CI. It is especially worth it for flows
generated by an LLM, which reliably get the purity rule wrong.

---

# 3. Run and operate

## The web UI

Four tabs. **Workflows** lists everything discovered, with search, tag
filters, a **Flow** button that shows the flow's source, and **▶ Run**.
**History** is the run table, auto-refreshing every 2 seconds. **Dashboards**
and **Schedules** are covered below. There's a light/dark toggle top-right
that remembers your choice and carries into the reports you open.

Multiple flows — or the same flow several times — run concurrently on a thread
pool.

## History, resume and cancel

Run statuses are `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED` and
`INTERRUPTED`.

Every step transition is written to disk as it happens, so a crashed server
still leaves an honest partial record. Runs that were RUNNING when the process
died are marked **INTERRUPTED** at the next startup rather than lying about
their state.

**Resume** (⏭) is available on FAILED, CANCELLED and INTERRUPTED runs. It
replays the body; steps that already completed return instantly and show as
"carried over" in the new report. Loop iterations are journaled individually,
so a loop that died on item 7 resumes at item 7.

**Restart** (↺) is the other option: a brand-new run from the top with the
same inputs and environment.

**Cancel** (✕) is cooperative. It takes effect at the next step boundary, loop
iteration, retry wait, or timeout poll. A step already executing without a
`timeout=` finishes first. Cancelling a parent cancels its sub-workflows.

Only the newest `CODEFLOW_HISTORY_LIMIT` runs are kept (default 500). Older
finished runs and their reports are pruned automatically; RUNNING entries
never are.

## Dashboards

Set `dashboard = True` and build widgets in your steps:

```python
class OpsDashboard(Workflow):
    dashboard = True

    @step()
    def build(self, data):
        self.widget("section", title="Overview")            # full-width divider
        self.widget("metric", title="Orders", value=128)
        self.widget("stat", title="Revenue", value="€12.4k", delta="+8%")
        self.widget("status", title="API", value="online", status="ok")
        self.widget("progress", title="Quota", value=64, max=100)
        self.widget("chart", title="Sales", chart="bar", size="wide",
                    data={"Books": 850, "Games": 1200})
        self.widget("table", title="Orders", rows=rows, size="full")
```

Also available: `list`, `alert`, `text`, `json`, `html`, `link`. `size` is
`""`, `"wide"` (2 columns) or `"full"` (the row). Charts are dependency-free
inline SVG; tables sort on click and export to CSV.

Opening a dashboard runs the flow and renders what it produced. The toolbar
has Refresh, auto-refresh (10s–5m) and an environment picker, and an input bar
generated from the flow's inputs — change a value, press Enter, the flow
re-runs with it.

Dashboard renders are **transient**: they don't create history entries, since
auto-refresh would flood the table. Use ▶ Run for a snapshot with a report.
Deep-link with `/#dashboards/<FlowName>`.

Table widgets take conditional formatting rules:

```python
self.widget("table", rows=rows, format=[
    {"col": "status", "map": {"paid": "ok", "failed": "err"}},
    {"col": "amount", "gt": 300, "style": "err"},
])
```

Operators are `eq/ne/gt/gte/lt/lte/contains`; styles are `ok`, `warn`, `err`,
`info`, `muted`; the first matching rule wins.

## Schedules

The Schedules tab runs flows on a timer: every N minutes, or daily at HH:MM
with optional weekdays. Times are the **server's** local time. Each schedule
carries its own inputs and environment, can be toggled off, run immediately
with ▶, and links to its last run's report.

Schedules persist in `history/schedules.json` — inside the history volume, so
Docker rebuilds keep them.

Two behaviours worth knowing. If the server was down when a schedule was due,
it fires **once** at startup, not once per missed period. And if the previous
run of a schedule is still RUNNING when the next fire is due, that fire is
skipped and retried on the next tick, so a slow flow can't stack up concurrent
runs. Manual ▶ run-now is not guarded.

## Webhooks

Opt in per flow, then POST to it:

```python
class DeployFlow(Workflow):
    webhook = True
    # webhook_token = "s3cret"      # optional per-flow secret
```

```bash
curl -X POST localhost:8000/api/hooks/DeployFlow \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: s3cret" \
  -d '{"version": "1.4.2"}'
# -> {"run_id": "…", "workflow": "DeployFlow"}
```

The JSON body becomes the run's inputs; `?env=prod` selects an environment.
Auth is the flow's `webhook_token` if set, otherwise the
`CODEFLOW_WEBHOOK_TOKEN` variable if set, otherwise open — which is fine on
localhost and not fine anywhere else. Flows without `webhook = True` return
404. Triggered runs land in the history like any other.

## REST API

Interactive Swagger at **`/api/docs`**; full reference in
[`docs/API.md`](docs/API.md).

```
GET    /api/flows                      POST   /api/run/<flow>
GET    /api/runs                       GET    /api/runs/<id>
POST   /api/runs/<id>/cancel           POST   /api/runs/<id>/restart
POST   /api/runs/<id>/resume           DELETE /api/runs/<id>
POST   /api/runs/bulk-delete           GET    /api/environments
POST   /api/dashboards/<flow>/render   POST   /api/hooks/<flow>
GET/POST/PATCH/DELETE /api/schedules   GET    /reports/<id>
```

Typed-input failures return `422` with
`{"detail": {"message": ..., "errors": {field: msg}}}`.

---

# 4. Deploy

## Docker

The image holds only the engine and web app. Your `workflows/`,
`environments/` and `history/` folders stay on the host and are mounted in, so
you edit flows in your normal editor and the UI picks them up live — no
rebuild, no restart.

```bash
docker compose up --build -d
# http://localhost:8000
```

```yaml
volumes:
  - ./workflows:/data/workflows        # your flows, live-editable
  - ./environments:/data/environments  # env JSON
  - ./history:/data/history            # reports survive rebuilds
```

Point them anywhere — `- ~/my-flows:/data/workflows` is fine. Keep importing
`from engine import ...` inside your flow files; the engine is installed in
the image and resolves wherever the flows are mounted.

Without compose:

```bash
docker build -t codeflow .
docker run -d -p 8000:8000 \
  -v "$PWD/workflows:/data/workflows" \
  -v "$PWD/environments:/data/environments" \
  -v "$PWD/history:/data/history" \
  --name codeflow codeflow
```

If your flows need extra packages, add them to `requirements.txt` and rebuild.
The mounted flow files themselves never need one.

## Remote access

There is no authentication and no plan to add any — it would be the wrong
shape for a tool this size. Two options that actually work:

- **Tailscale.** Install it on the host, keep `CODEFLOW_HOST=0.0.0.0`, and
  reach the box on its tailnet address. Nothing is exposed to the internet.
- **SSH tunnel.** `ssh -L 8000:localhost:8000 you@host` and browse locally.

A spare mini PC on your desk running Docker plus Tailscale is a perfectly good
home for this.

---

# 5. Reference

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `CODEFLOW_WORKFLOWS_DIR` | `./workflows` | Where flows are discovered |
| `CODEFLOW_ENVIRONMENTS_DIR` | `./environments` | Environment JSON files |
| `CODEFLOW_HISTORY_DIR` | `./history` | Run records, reports, schedules |
| `CODEFLOW_HISTORY_LIMIT` | `500` | Runs kept before pruning |
| `CODEFLOW_HOST` | `127.0.0.1` | Bind address |
| `CODEFLOW_PORT` | `8000` | Port |
| `CODEFLOW_WEBHOOK_TOKEN` | — | Global webhook secret |
| `CODEFLOW_SCHEDULES_FILE` | `history/schedules.json` | Schedule store |

`requests`, `PyYAML` and `dirtyjson` are preinstalled for use in flows.

## Project layout

```
code-flow/
├── app.py                 # FastAPI server + REST API
├── ui.html                # single-page web UI
├── engine/
│   ├── decorators.py      # @flow / @step
│   ├── workflow.py        # Workflow base class
│   ├── runner.py          # execution: journal, replay, retries, timeouts
│   ├── registry.py        # discovery of workflows/
│   ├── inputs.py          # dataclass -> form schema + validation
│   ├── steps.py           # standard step library (http/fs/sh/db)
│   ├── lint.py            # codeflow lint
│   └── reports.py         # HTML reports + history store
├── workflows/             # your flows (subfolders become tags)
│   ├── Flow.py
│   └── examples/
├── environments/          # dev.json, prod.json, …
└── history/               # run records, reports, schedules.json
```

## Gotchas

- Two steps sharing a `name=` — the second silently wins in the report (CF001).
- Step results must be JSON-serializable to restore exactly on resume.
- A timed-out step call is abandoned, not killed. Make it duplicate-safe.
- `parallel_map` uses threads: right for I/O, wrong for CPU.
- Module top-level code runs on every folder scan.
- A run is capped at 10,000 step calls as a runaway-loop guard.

## Examples

| File | Shows |
|---|---|
| `workflows/Flow.py` | branching, retries, dry-run guard |
| `workflows/Flow2.py` | loops, structured logging |
| `workflows/Flow3.py` | sub-workflows via `call_workflow` |
| `examples/HelloFlow.py` | shared `_lib` imports, `log_image` |
| `examples/OpsDashboard.py` | widgets and dashboard inputs |
| `examples/ParallelFetch.py` | `parallel_map`, backoff, webhook |
| `examples/RetryDemo.py` | `retry_on`, `continue_on_error` |
| `examples/ResumeDemo.py` | resume, including inside a loop |
| `examples/LoopPatterns.py` | tolerant loops that don't stop on one bad item |
| `examples/StdLib.py` | the standard step library: `fs`, `sh`, `db` |

## Further reading

- [`docs/API.md`](docs/API.md) — REST reference
- [`docs/BEST_PRACTICES.md`](docs/BEST_PRACTICES.md) — writing flows well
- [`llms.txt`](llms.txt) — compact complete engine reference. Paste it into an
  LLM prompt and ask for the flow you want, then run the linter on what comes
  back.
