"""Workflow execution engine.

A run executes the workflow's @flow body once. Every @step call made by the
body is journaled (key -> result); a resumed run replays the body with the
journal preloaded, so completed steps return instantly and execution
effectively continues where it stopped.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .workflow import Workflow

CANCEL_POLL_SECONDS = 0.25   # how often waiting code checks for cancellation
MAX_STEP_CALLS = 10_000      # runaway-loop guard


class StepTimeoutError(TimeoutError):
    """Raised when a step attempt exceeds its timeout= budget."""


class NondeterminismError(RuntimeError):
    """A replayed body took a different path than the run it resumed from."""


class RunCancelled(BaseException):
    """Raised internally when a run is cancelled. Inherits BaseException so
    step-level ``except Exception`` handling cannot swallow it."""


def parallel_map(step_method: Callable, items, workers: int = 4) -> List[Any]:
    """Run a @step method over items concurrently.

        results = parallel_map(self.fetch, urls, workers=8)

    Each call is journaled separately, so a resume re-executes only the
    calls that did not complete. Threads — good for I/O, not for CPU.
    Results come back in input order; the first failure propagates.
    """
    items = list(items)
    out: List[Any] = [None] * len(items)
    if not items:
        return out
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="codeflow-pmap") as pool:
        futs = {pool.submit(step_method, it): i for i, it in enumerate(items)}
        try:
            for f in concurrent.futures.as_completed(futs):
                out[futs[f]] = f.result()
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    return out


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _short_repr(value: Any, limit: int = 500) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class StepRecord:
    name: str
    func_name: str
    status: str = "PENDING"          # RUNNING / SUCCESS / FAILED / CANCELLED
    attempts: int = 0
    max_attempts: int = 1
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    args: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    continued: bool = False   # failed but continue_on_error returned None
    inherited: bool = False   # replayed from the journal, not re-executed
    logs: List[str] = field(default_factory=list)
    images: List[Dict[str, str]] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RunRecord:
    run_id: str
    workflow: str
    status: str = "RUNNING"          # RUNNING / SUCCESS / FAILED / CANCELLED / INTERRUPTED
    parent_run_id: Optional[str] = None    # set when run as a sub-workflow
    resumed_from: Optional[str] = None     # run this one resumed from
    environment: Optional[str] = None
    env_values: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)   # masked snapshot
    widgets: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)           # flow-body logs
    started_at: str = ""
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    steps: List[StepRecord] = field(default_factory=list)
    # resume state — written to the sidecar file, not into the run record
    raw_ctx: Dict[str, Any] = field(default_factory=dict)
    journal: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("raw_ctx", None)
        d.pop("journal", None)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d


class WorkflowRunner:
    """Executes one workflow instance and produces a RunRecord."""

    def __init__(self, workflow_cls, inputs: Optional[Dict[str, Any]] = None,
                 on_update: Optional[Callable[[RunRecord], None]] = None,
                 parent_run_id: Optional[str] = None,
                 call_chain: Optional[List[str]] = None,
                 env: Optional[Dict[str, Any]] = None,
                 env_name: Optional[str] = None,
                 cancel_event: Optional[threading.Event] = None,
                 resume: Optional[Dict[str, Any]] = None):
        self.workflow_cls = workflow_cls
        self.inputs = inputs or {}
        self.on_update = on_update or (lambda rec: None)
        self.run_id = uuid.uuid4().hex[:12]
        self.parent_run_id = parent_run_id
        self.call_chain: List[str] = list(call_chain or [])
        self.env: Dict[str, Any] = dict(env or {})
        self.env_name = env_name
        # cooperative cancellation; sub-workflows share the parent's flag
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        # resume: {"journal": {...}, "from_run": "<run_id>"}
        self.resume = resume
        self.journal: Dict[str, Any] = {}
        self._call_counts: Dict[str, int] = {}
        self._calls_made = 0
        self._record: Optional[RunRecord] = None
        self._lock = threading.Lock()
        self._depth = threading.local()   # "inside a step" per thread
        self._cur = threading.local()     # current StepRecord per thread

    # ------------------------------------------------------------------ run
    def run(self) -> RunRecord:
        from .environments import mask_env

        wf: Workflow = self.workflow_cls(inputs=self.inputs)
        wf._runner = self
        # typed inputs: validate + coerce (covers UI, API, webhook, scheduler)
        from .inputs import apply_schema, schema_for
        schema = schema_for(self.workflow_cls)
        input_errors: Dict[str, str] = {}
        if schema:
            cleaned, input_errors = apply_schema(schema, wf.inputs)
            if not input_errors:
                wf.inputs = cleaned
                wf.ctx.update(cleaned)
        wf.env = {k: v for k, v in self.env.items() if k != "__secrets__"}
        wf.ctx["env"] = wf.env
        if not self.call_chain:
            self.call_chain = [wf.name]

        record = RunRecord(
            run_id=self.run_id,
            journal=self.journal,          # live reference — sidecar sees updates
            workflow=wf.name,
            parent_run_id=self.parent_run_id,
            environment=self.env_name,
            env_values=mask_env(self.env),
            tags=sorted(getattr(self.workflow_cls, "tags", []) or []),
            inputs=dict(wf.inputs),
            started_at=_now(),
        )
        self._record = record
        t0 = time.monotonic()

        if input_errors:
            return self._fail_fast(record, t0, "input validation failed: " + "; ".join(
                f"{k}: {v}" for k, v in input_errors.items()))

        entry = self.workflow_cls.flow_entry()
        if entry is None:
            return self._fail_fast(record, t0, "Workflow has no @flow method")

        if self.resume:
            self.journal.update(self.resume.get("journal") or {})
            record.resumed_from = self.resume.get("from_run")

        wf._program_runner = self
        self._install_sinks(wf, record)
        self.on_update(record)
        try:
            result = entry(wf, wf.ctx)
            if isinstance(result, dict):
                wf.outputs(result)
            record.status = "SUCCESS"
        except RunCancelled:
            record.status = "CANCELLED"
            record.error = "cancelled by user"
            for s in record.steps:
                if s.status in ("RUNNING", "PENDING"):
                    s.status = "CANCELLED"
                    s.ended_at = _now()
        except Exception as exc:
            record.status = "FAILED"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            wf._program_runner = None
            record.outputs = dict(wf.outputs())
            record.context = self._snapshot_ctx(wf)
            record.raw_ctx = {k: v for k, v in wf.ctx.items() if k != "env"}
            record.widgets = list(wf.widgets())[:200]
            record.ended_at = _now()
            record.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            self.on_update(record)
        return record

    def _fail_fast(self, record: RunRecord, t0: float, error: str) -> RunRecord:
        record.status = "FAILED"
        record.error = error
        record.ended_at = _now()
        record.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        self.on_update(record)
        return record

    def _install_sinks(self, wf: Workflow, record: RunRecord) -> None:
        """Route log/image/block output to the step running on THIS thread
        (parallel_map runs several at once); body output goes to the run."""
        def make(kind):
            def emit(payload):
                rec = getattr(self._cur, "rec", None)
                if kind == "log":
                    (rec.logs if rec else record.logs).append(f"{_now()}  {payload}")
                elif rec is None:
                    record.logs.append(f"{_now()}  [{kind} outside a step, ignored]")
                elif kind == "image":
                    rec.images.append(payload)
                else:
                    rec.blocks.append(payload)
                    rec.logs.append(
                        f"{_now()}  [{payload['type']} #{len(rec.blocks)}"
                        + (f": {payload['title']}" if payload.get("title") else "") + "]")
            return emit
        wf._logger, wf._image_sink, wf._block_sink = (
            make("log"), make("image"), make("block"))

    # ---------------------------------------------------------------- steps
    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled()

    def _step_key(self, name: str, args: tuple, kwargs: dict) -> str:
        """Stable identity for one step call: name + arguments + occurrence.
        Argument-based keys keep the journal aligned on replay even if the
        surrounding loop's order shifts."""
        try:
            blob = json.dumps([args, sorted(kwargs.items())], sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            blob = repr((args, kwargs))
        base = f"{name}:{hashlib.sha1(blob.encode()).hexdigest()[:10]}"
        with self._lock:
            n = self._call_counts.get(base, 0) + 1
            self._call_counts[base] = n
        return f"{base}:{n}"

    def call_step(self, wf: Workflow, func, meta: Dict[str, Any],
                  args: tuple, kwargs: dict) -> Any:
        """Invoked by the @step wrapper for every step call in a flow body."""
        # a step calling another step runs it inline: one journal entry per
        # call made by the body, not per nested helper call
        if getattr(self._depth, "n", 0) > 0:
            return func(wf, *args, **kwargs)

        self._check_cancelled()
        with self._lock:
            self._calls_made += 1
            if self._calls_made > MAX_STEP_CALLS:
                raise RuntimeError(
                    f"Aborted after {MAX_STEP_CALLS} step calls (runaway loop?)")

        record = self._record
        name = meta["name"]
        key = self._step_key(name, args, kwargs)

        # ---- replay: this call already completed in the run we resumed from
        if key in self.journal:
            entry = self.journal[key]
            rec = StepRecord(name=name, func_name=meta["func_name"],
                             status="SUCCESS", inherited=True,
                             args=_short_repr(args, 200) if args else None,
                             result=_short_repr(entry.get("result")))
            with self._lock:
                record.steps.append(rec)
            return entry.get("result")

        rec = StepRecord(name=name, func_name=meta["func_name"],
                         max_attempts=meta.get("retry", 0) + 1,
                         args=_short_repr(args, 200) if args else None,
                         started_at=_now(), status="RUNNING")
        with self._lock:
            record.steps.append(rec)
        st0 = time.monotonic()
        prev_rec = getattr(self._cur, "rec", None)
        self._cur.rec = rec
        self._depth.n = getattr(self._depth, "n", 0) + 1
        try:
            result = self._call_with_retry(
                meta, rec, call=lambda: func(wf, *args, **kwargs))
            rec.status = "SUCCESS"
            if result is not None:
                rec.result = _short_repr(result)
            with self._lock:
                self.journal[key] = {"name": name, "result": result}
            return result
        except RunCancelled:
            rec.status = "CANCELLED"
            raise
        except Exception as exc:
            rec.status = "FAILED"
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.traceback = traceback.format_exc()
            if meta.get("continue_on_error"):
                rec.continued = True
                rec.logs.append(f"{_now()}  continue_on_error=True — returning None")
                return None
            raise
        finally:
            self._depth.n -= 1
            self._cur.rec = prev_rec
            rec.ended_at = _now()
            rec.duration_ms = round((time.monotonic() - st0) * 1000, 1)
            self.on_update(record)

    def _call_with_retry(self, meta: Dict[str, Any], rec: StepRecord,
                         call: Callable[[], Any]) -> Any:
        retries = meta.get("retry", 0)
        base_delay = meta.get("retry_delay", 0)
        backoff = meta.get("retry_backoff", 1) or 1
        retry_on = meta.get("retry_on")     # None = everything is retryable
        timeout = meta.get("timeout")
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            self._check_cancelled()
            with self._lock:
                rec.attempts += 1
            try:
                return self._call(call, timeout)
            except Exception as exc:  # noqa: BLE001 - reported in the record
                last_exc = exc
                rec.logs.append(
                    f"{_now()}  attempt {attempt}/{retries + 1} failed: "
                    f"{type(exc).__name__}: {exc}")
                if retry_on is not None and not isinstance(exc, retry_on):
                    rec.logs.append(
                        f"{_now()}  {type(exc).__name__} is not in retry_on="
                        f"({', '.join(c.__name__ for c in retry_on)}) — failing immediately")
                    raise
                if attempt <= retries and base_delay:
                    delay = base_delay * (backoff ** (attempt - 1))
                    if backoff > 1:
                        rec.logs.append(f"{_now()}  backing off {round(delay, 2)}s")
                    self.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _call(self, call: Callable[[], Any], timeout: Optional[float] = None) -> Any:
        """Run the step call, enforcing timeout= and staying responsive to
        cancellation. A timed-out or cancelled call is ABANDONED (Python
        threads cannot be killed) — it may keep running in the background,
        so steps with timeouts should be safe to duplicate."""
        if timeout is None and not self.cancel_event.is_set():
            return call()                      # fast path

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="codeflow-step")
        future = pool.submit(call)
        pool.shutdown(wait=False)
        deadline = time.monotonic() + timeout if timeout else None
        while not future.done():
            concurrent.futures.wait([future], timeout=CANCEL_POLL_SECONDS)
            if future.done():
                break
            if self.cancel_event.is_set():
                future.cancel()
                raise RunCancelled()
            if deadline is not None and time.monotonic() > deadline:
                future.cancel()
                raise StepTimeoutError(
                    f"step exceeded timeout of {timeout}s (attempt abandoned)")
        return future.result()

    def sleep(self, seconds: float) -> None:
        """Sleep in slices so cancellation stays responsive."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            self._check_cancelled()
            time.sleep(min(CANCEL_POLL_SECONDS, max(0.01, deadline - time.monotonic())))

    # -------------------------------------------------------------- context
    def _snapshot_ctx(self, wf: Workflow) -> Dict[str, Any]:
        """Display-safe snapshot for the report: env excluded (it has its own
        section), secret-looking keys masked, oversized values truncated."""
        from .environments import MASK, is_secret_key

        snap: Dict[str, Any] = {}
        for k, v in wf.ctx.items():
            if k == "env":
                continue
            if is_secret_key(k):
                snap[k] = MASK
                continue
            try:
                if len(json.dumps(v, default=str)) <= 2000:
                    snap[k] = v
                else:
                    snap[k] = _short_repr(v, 2000)
            except Exception:  # noqa: BLE001 - non-serializable value
                snap[k] = _short_repr(v)
        return snap
