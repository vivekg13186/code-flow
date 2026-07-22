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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from engine import WorkflowRunner, discover_workflows
from engine.environments import load_environments, mask_env, set_environments_dir
from engine.registry import set_workflows_dir, workflow_summary
from engine.reports import HistoryStore

BASE_DIR = Path(__file__).parent
# folder locations are overridable so they can live outside the app
# (e.g. Docker volume mounts) — see docker-compose.yml
WORKFLOWS_DIR = Path(os.environ.get("CODEFLOW_WORKFLOWS_DIR", BASE_DIR / "workflows"))
HISTORY_DIR = Path(os.environ.get("CODEFLOW_HISTORY_DIR", BASE_DIR / "history"))
ENVIRONMENTS_DIR = Path(os.environ.get("CODEFLOW_ENVIRONMENTS_DIR", BASE_DIR / "environments"))

set_workflows_dir(WORKFLOWS_DIR)  # lets steps resolve self.call_workflow(...)
set_environments_dir(ENVIRONMENTS_DIR)

app = FastAPI(title="code-flow", docs_url="/api/docs")
store = HistoryStore(HISTORY_DIR)
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


# ----------------------------------------------------------------- helpers
def _get_registry():
    registry, errors = discover_workflows(WORKFLOWS_DIR)
    return registry, errors


def _launch(workflow_cls, inputs: Optional[Dict[str, Any]],
            env: Optional[Dict[str, Any]] = None,
            env_name: Optional[str] = None) -> str:
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
                           cancel_event=cancel_event).run()
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
