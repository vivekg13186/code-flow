"""code-flow web UI — FastAPI server.

Run:  python app.py   (or: uvicorn app:app --reload)
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import WorkflowRunner, discover_workflows
from engine.environments import load_environments, mask_env, set_environments_dir
from engine.inputs import validate_for_class
from engine.registry import set_workflows_dir, workflow_summary
from engine.reports import HistoryStore
from engine.scheduler import Scheduler

BASE_DIR = Path(__file__).parent

# .codeflow.env (written by scripts/install.*) — loaded as defaults so plain
# `python app.py` also respects the configured paths; real env vars win
_cfg = BASE_DIR / ".codeflow.env"
if _cfg.exists():
    for _line in _cfg.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# folder locations are overridable so they can live outside the app
# (e.g. Docker volume mounts) — see docker-compose.yml
WORKFLOWS_DIR = Path(os.environ.get("CODEFLOW_WORKFLOWS_DIR", BASE_DIR / "workflows"))
HISTORY_DIR = Path(os.environ.get("CODEFLOW_HISTORY_DIR", BASE_DIR / "history"))
ENVIRONMENTS_DIR = Path(os.environ.get("CODEFLOW_ENVIRONMENTS_DIR", BASE_DIR / "environments"))

set_workflows_dir(WORKFLOWS_DIR)  # lets steps resolve self.call_workflow(...)
set_environments_dir(ENVIRONMENTS_DIR)

HISTORY_LIMIT = int(os.environ.get("CODEFLOW_HISTORY_LIMIT", "500"))

app = FastAPI(title="code flow", docs_url="/api/docs")
if (BASE_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    icon = BASE_DIR / "static" / "favicon.png"
    if icon.exists():
        return FileResponse(icon, media_type="image/png")
    raise HTTPException(404)
store = HistoryStore(HISTORY_DIR, limit=HISTORY_LIMIT)
executor = ThreadPoolExecutor(max_workers=8)  # multiple flows run concurrently

# in-memory live state of runs (persisted to disk on every update)
_runs_lock = threading.Lock()
_live_runs: Dict[str, dict] = {}
_cancel_events: Dict[str, threading.Event] = {}

# crash visibility: runs persisted as RUNNING did not survive the previous
# process — mark them INTERRUPTED so they show up honestly in the history
_interrupted = store.mark_interrupted()
if _interrupted:
    print(f"[code-flow] marked {_interrupted} interrupted run(s) from previous session")


def _scheduled_launch(flow_name: str, inputs: Optional[Dict[str, Any]],
                      env_name: Optional[str]) -> str:
    """Launcher used by the scheduler — same path as the Run button."""
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None:
        raise ValueError(f"unknown workflow: {flow_name}")
    env = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise ValueError(f"unknown environment: {env_name}")
        env = envs[env_name]
    run_id = _launch(cls, inputs, env=env, env_name=env_name)
    if not run_id:
        raise RuntimeError("workflow failed to start")
    return run_id


SCHEDULES_FILE = Path(os.environ.get("CODEFLOW_SCHEDULES_FILE",
                                     HISTORY_DIR / "schedules.json"))
def _run_is_active(run_id: str) -> bool:
    with _runs_lock:
        rec = _live_runs.get(run_id)
    return bool(rec and rec.get("status") == "RUNNING")


scheduler = Scheduler(SCHEDULES_FILE, _scheduled_launch, is_running=_run_is_active)


# ----------------------------------------------------------------- helpers
def _get_registry():
    registry, errors = discover_workflows(WORKFLOWS_DIR)
    return registry, errors


def _launch(workflow_cls, inputs: Optional[Dict[str, Any]],
            env: Optional[Dict[str, Any]] = None,
            env_name: Optional[str] = None,
            resume: Optional[Dict[str, Any]] = None) -> str:
    holder: Dict[str, str] = {}
    started = threading.Event()
    cancel_event = threading.Event()

    def on_update(record):
        with _runs_lock:
            _live_runs[record.run_id] = record.to_dict()
            # child runs share the parent's cancel event (cancel cascades)
            _cancel_events.setdefault(record.run_id, cancel_event)
        holder.setdefault("run_id", record.run_id)  # first record = the parent run
        started.set()
        # incremental persistence: every step transition hits disk, so a
        # crashed server leaves an honest partial record behind
        store.save_run(record)
        if record.status in ("SUCCESS", "FAILED", "CANCELLED"):
            with _runs_lock:
                _cancel_events.pop(record.run_id, None)

    def job():
        try:
            WorkflowRunner(workflow_cls, inputs=inputs, on_update=on_update,
                           env=env, env_name=env_name,
                           cancel_event=cancel_event, resume=resume).run()
        finally:
            started.set()

    executor.submit(job)
    started.wait(timeout=10)
    return holder.get("run_id", "")


# --------------------------------------------------------------------- api
@app.get("/api/flows")
def list_flows():
    registry, errors = _get_registry()
    return {
        "flows": [workflow_summary(cls) for cls in registry.values()],
        "errors": [{"file": e["file"], "error": e["error"].splitlines()[-1]} for e in errors],
    }


@app.get("/api/environments")
def list_environments():
    envs, errors = load_environments(ENVIRONMENTS_DIR)
    return {
        # secrets are masked in the UI; runs receive the real values
        "environments": [{"name": n, "values": mask_env(v)} for n, v in envs.items()],
        "errors": errors,
    }


@app.post("/api/run/{flow_name}")
def run_flow(flow_name: str, body: Optional[Dict[str, Any]] = None):
    """Body: {"inputs": {...}, "env": "dev"} — or a bare inputs dict (legacy)."""
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None:
        raise HTTPException(404, f"Unknown workflow: {flow_name}")

    body = body or {}
    if "inputs" in body or "env" in body:
        inputs, env_name = body.get("inputs") or {}, body.get("env") or None
    else:
        inputs, env_name = body, None

    inputs, errors = validate_for_class(cls, inputs)
    if errors:
        raise HTTPException(422, {"message": "input validation failed", "errors": errors})

    env = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise HTTPException(404, f"Unknown environment: {env_name}")
        env = envs[env_name]

    run_id = _launch(cls, inputs, env=env, env_name=env_name)
    if not run_id:
        raise HTTPException(500, "Workflow failed to start")
    return {"run_id": run_id, "workflow": flow_name, "environment": env_name}


@app.get("/api/runs")
def list_runs():
    """Live (in-memory) runs merged over the persisted history."""
    with _runs_lock:
        live = {rid: dict(r) for rid, r in _live_runs.items()}
    merged: Dict[str, dict] = {}
    for entry in store.history():
        merged[entry["run_id"]] = entry
    for rid, rec in live.items():
        merged[rid] = {
            "run_id": rid,
            "workflow": rec["workflow"],
            "status": rec["status"],
            "started_at": rec["started_at"],
            "ended_at": rec.get("ended_at"),
            "duration_ms": rec.get("duration_ms"),
            "error": rec.get("error"),
            "steps": len(rec.get("steps", [])),
            "parent_run_id": rec.get("parent_run_id"),
            "environment": rec.get("environment"),
            "tags": rec.get("tags", []),
        }
    runs = sorted(merged.values(), key=lambda r: r.get("started_at") or "", reverse=True)
    return {"runs": runs}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    """Cooperative cancel: takes effect at the next step boundary, loop
    iteration, retry wait, or timeout poll. Cancelling a sub-run cancels the
    whole tree (they share one cancel flag)."""
    with _runs_lock:
        event = _cancel_events.get(run_id)
        rec = _live_runs.get(run_id)
    if event is None or rec is None:
        raise HTTPException(404, "Run not found or already finished")
    if rec.get("status") != "RUNNING":
        raise HTTPException(409, f"Run is {rec.get('status')}, not RUNNING")
    event.set()
    return {"run_id": run_id, "cancelling": True}


@app.post("/api/runs/{run_id}/restart")
def restart_run(run_id: str):
    """Start a NEW run of the same flow with the original run's inputs and
    environment (env values are re-resolved fresh, so rotated secrets or
    edited env files apply)."""
    with _runs_lock:
        rec = dict(_live_runs.get(run_id) or {})
    if not rec:
        json_path = HISTORY_DIR / f"{run_id}.json"
        if not json_path.exists():
            raise HTTPException(404, "Run not found")
        import json as _json
        rec = _json.loads(json_path.read_text(encoding="utf-8"))

    flow_name = rec.get("workflow")
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None:
        raise HTTPException(404, f"Workflow no longer exists: {flow_name}")

    inputs = rec.get("inputs") or {}
    inputs, errors = validate_for_class(cls, inputs)
    if errors:
        raise HTTPException(422, {"message": "input validation failed", "errors": errors})

    env_name = rec.get("environment")
    env = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise HTTPException(404, f"Environment no longer exists: {env_name}")
        env = envs[env_name]

    new_id = _launch(cls, inputs, env=env, env_name=env_name)
    if not new_id:
        raise HTTPException(500, "Workflow failed to start")
    return {"run_id": new_id, "workflow": flow_name, "environment": env_name,
            "restarted_from": run_id}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str):
    """Resume a FAILED / CANCELLED / INTERRUPTED run: start a new run at the
    step where it stopped, with the context restored to the state at that
    moment. Env values are re-resolved fresh. Steps before the resume point
    are not re-executed."""
    import json as _json

    resume_path = HISTORY_DIR / f"{run_id}.resume.json"
    if not resume_path.exists():
        raise HTTPException(404, "No resume state for this run")
    state = _json.loads(resume_path.read_text(encoding="utf-8"))

    flow_name = state.get("workflow")
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None:
        raise HTTPException(404, f"Workflow no longer exists: {flow_name}")

    # replay: the journal decides where execution effectively continues
    if not state.get("journal"):
        raise HTTPException(409, "Nothing completed to resume from — use restart")

    env_name = state.get("environment")
    env = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise HTTPException(404, f"Environment no longer exists: {env_name}")
        env = envs[env_name]

    new_id = _launch(cls, state.get("inputs") or {}, env=env, env_name=env_name,
                     resume={"from_run": run_id,
                             "journal": state.get("journal") or {}})
    if not new_id:
        raise HTTPException(500, "Workflow failed to start")
    return {"run_id": new_id, "workflow": flow_name, "environment": env_name,
            "resumed_from": run_id,
            "completed_steps_skipped": len(state.get("journal") or {})}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    with _runs_lock:
        rec = _live_runs.get(run_id)
        if rec and rec.get("status") == "RUNNING":
            raise HTTPException(409, "Run is still RUNNING — cancel it first")
        _live_runs.pop(run_id, None)
    if not store.delete_run(run_id):
        raise HTTPException(404, "Run not found")
    return {"deleted": run_id}


@app.post("/api/runs/bulk-delete")
def bulk_delete_runs(body: Dict[str, Any]):
    """Delete a set of finished runs: {"run_ids": [...]}. RUNNING runs are skipped."""
    ids = body.get("run_ids") or []
    deleted, skipped = [], []
    for rid in ids:
        with _runs_lock:
            rec = _live_runs.get(rid)
            if rec and rec.get("status") == "RUNNING":
                skipped.append(rid)
                continue
            _live_runs.pop(rid, None)
        (deleted if store.delete_run(rid) else skipped).append(rid)
    return {"deleted": deleted, "skipped": skipped}


@app.delete("/api/runs")
def clear_history():
    """Delete all finished runs (RUNNING ones are kept)."""
    deleted = store.clear(keep_statuses=("RUNNING",))
    with _runs_lock:
        for rid in [r for r, rec in _live_runs.items() if rec.get("status") != "RUNNING"]:
            _live_runs.pop(rid, None)
    return {"deleted": deleted}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    with _runs_lock:
        rec = _live_runs.get(run_id)
    if rec:
        return rec
    json_path = HISTORY_DIR / f"{run_id}.json"
    if json_path.exists():
        return JSONResponse(content=__import__("json").loads(json_path.read_text()))
    raise HTTPException(404, "Run not found")


# ------------------------------------------------------------------- hooks
@app.post("/api/hooks/{flow_name}")
def webhook_trigger(flow_name: str, body: Optional[Dict[str, Any]] = None,
                    env: Optional[str] = None, token: Optional[str] = None,
                    x_webhook_token: Optional[str] = Header(None)):
    """Start a flow from an external system. The flow must opt in with
    ``webhook = True``. The JSON body becomes the run's inputs (or use
    {"inputs": {...}, "env": "prod"}). Token: per-flow ``webhook_token``
    attr, else the CODEFLOW_WEBHOOK_TOKEN env var, else open. Pass it as
    the X-Webhook-Token header or ?token= query param."""
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None or not getattr(cls, "webhook", False):
        raise HTTPException(404, "No such webhook")

    expected = getattr(cls, "webhook_token", None) or os.environ.get("CODEFLOW_WEBHOOK_TOKEN")
    if expected and (x_webhook_token or token) != expected:
        raise HTTPException(401, "Invalid webhook token")

    body = body or {}
    if "inputs" in body or "env" in body:
        inputs, env_name = body.get("inputs") or {}, body.get("env") or env
    else:
        inputs, env_name = body, env

    inputs, errors = validate_for_class(cls, inputs)
    if errors:
        raise HTTPException(422, {"message": "input validation failed", "errors": errors})

    env_values = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise HTTPException(404, f"Unknown environment: {env_name}")
        env_values = envs[env_name]

    run_id = _launch(cls, inputs, env=env_values, env_name=env_name)
    if not run_id:
        raise HTTPException(500, "Workflow failed to start")
    return {"run_id": run_id, "workflow": flow_name, "environment": env_name}


# --------------------------------------------------------------- dashboards
@app.post("/api/dashboards/{flow_name}/render")
def render_dashboard(flow_name: str, body: Optional[Dict[str, Any]] = None):
    """Run a dashboard flow synchronously and return its widgets.

    Dashboard refreshes are transient by design: they do NOT create history
    entries or reports (auto-refresh every 10s would flood the history).
    Use the normal Run button when you want a persisted snapshot."""
    registry, _ = _get_registry()
    cls = registry.get(flow_name)
    if cls is None:
        raise HTTPException(404, f"Unknown workflow: {flow_name}")

    body = body or {}
    inputs, env_name = body.get("inputs") or {}, body.get("env") or None

    inputs, errors = validate_for_class(cls, inputs)
    if errors:
        raise HTTPException(422, {"message": "input validation failed", "errors": errors})

    env = None
    if env_name:
        envs, _ = load_environments(ENVIRONMENTS_DIR)
        if env_name not in envs:
            raise HTTPException(404, f"Unknown environment: {env_name}")
        env = envs[env_name]

    rec = WorkflowRunner(cls, inputs=inputs, env=env, env_name=env_name).run()
    return {
        "run_id": rec.run_id,
        "status": rec.status,
        "error": rec.error,
        "duration_ms": rec.duration_ms,
        "widgets": rec.widgets,
        "steps": [{"name": s.name, "status": s.status, "error": s.error} for s in rec.steps],
    }


# ---------------------------------------------------------------- schedules
@app.get("/api/schedules")
def list_schedules():
    return {"schedules": scheduler.list()}


@app.post("/api/schedules")
def create_schedule(body: Dict[str, Any]):
    try:
        return scheduler.add(body or {})
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.patch("/api/schedules/{sid}")
def update_schedule(sid: str, body: Dict[str, Any]):
    try:
        s = scheduler.update(sid, body or {})
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if s is None:
        raise HTTPException(404, "Schedule not found")
    return s


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: str):
    if not scheduler.delete(sid):
        raise HTTPException(404, "Schedule not found")
    return {"deleted": sid}


@app.post("/api/schedules/{sid}/run")
def run_schedule_now(sid: str):
    try:
        return scheduler.fire(sid)
    except KeyError:
        raise HTTPException(404, "Schedule not found")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


@app.get("/reports/{run_id}")
def report(run_id: str):
    # live run not yet finished -> render current state on the fly
    with _runs_lock:
        rec = _live_runs.get(run_id)
    path = store.report_path(run_id)
    if path.exists():
        return FileResponse(path, media_type="text/html")
    if rec:
        from engine.reports import render_report
        return HTMLResponse(render_report(rec))
    raise HTTPException(404, "Report not found")


# ---------------------------------------------------------------------- ui
@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "ui.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("CODEFLOW_HOST", "127.0.0.1"),  # 0.0.0.0 in Docker
        port=int(os.environ.get("CODEFLOW_PORT", "8000")),
    )
