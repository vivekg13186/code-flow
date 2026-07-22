"""Workflow execution engine: conditions, loops, retries, next-step chaining."""
from __future__ import annotations

import concurrent.futures
import inspect
import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .workflow import Workflow

MAX_STEPS = 1000  # safety net against accidental infinite chains
CANCEL_POLL_SECONDS = 0.25  # how often a waiting step checks for cancellation


class StepTimeoutError(TimeoutError):
    """Raised when a step attempt exceeds its timeout= budget."""


class RunCancelled(BaseException):
    """Raised internally when a run is cancelled. Inherits BaseException so
    that step-level `except Exception` handling (retries, continue_on_error)
    cannot swallow a cancellation."""

_SAFE_BUILTINS = {
    "len": len, "min": min, "max": max, "sum": sum, "abs": abs,
    "round": round, "range": range, "sorted": sorted, "any": any,
    "all": all, "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "set": set, "enumerate": enumerate,
    "zip": zip, "True": True, "False": False, "None": None,
}


def _eval_expr(expr: str, ctx: Dict[str, Any]) -> Any:
    """Evaluate a small Python expression against the workflow context."""
    return eval(expr, {"__builtins__": _SAFE_BUILTINS}, dict(ctx))


def _ctx_key(step_name: str) -> str:
    """Derive a valid identifier from a step name for ctx keys:
    'Fetch Data' -> 'Fetch_Data' (so conditions can use Fetch_Data_result)."""
    return re.sub(r"\W+", "_", step_name).strip("_")


def _parse_loop(loop: str):
    """Parse 'i in items' -> ('i', 'items')."""
    m = re.match(r"^\s*([A-Za-z_]\w*)\s+in\s+(.+?)\s*$", loop)
    if not m:
        raise ValueError(f"Invalid loop expression: {loop!r} (expected 'var in iterable')")
    return m.group(1), m.group(2)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class StepRecord:
    name: str
    func_name: str
    status: str = "PENDING"          # RUNNING / SUCCESS / FAILED / SKIPPED
    condition: Optional[str] = None
    condition_result: Optional[bool] = None
    loop: Optional[str] = None
    iterations: int = 0
    attempts: int = 0
    max_attempts: int = 1
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    continued: bool = False   # failed but flow continued (continue_on_error)
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RunRecord:
    run_id: str
    workflow: str
    status: str = "RUNNING"          # RUNNING / SUCCESS / FAILED
    parent_run_id: Optional[str] = None   # set when run as a sub-workflow
    environment: Optional[str] = None     # selected environment name
    env_values: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)  # inherited from the workflow class
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)  # ctx snapshot (masked)
    started_at: str = ""
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    steps: List[StepRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
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
                 cancel_event: Optional[threading.Event] = None):
        self.workflow_cls = workflow_cls
        self.inputs = inputs or {}
        self.on_update = on_update or (lambda rec: None)
        self.run_id = uuid.uuid4().hex[:12]
        self.parent_run_id = parent_run_id
        self.call_chain: List[str] = list(call_chain or [])
        self.env: Dict[str, Any] = dict(env or {})
        self.env_name = env_name
        # cooperative cancellation flag; sub-workflows share the parent's
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()

    # ------------------------------------------------------------------ run
    def run(self) -> RunRecord:
        from .environments import mask_env

        wf: Workflow = self.workflow_cls(inputs=self.inputs)
        wf._runner = self
        # steps get the REAL values; "__secrets__" bookkeeping is stripped
        wf.env = {k: v for k, v in self.env.items() if k != "__secrets__"}
        wf.ctx["env"] = wf.env  # steps + conditions can read env['key']
        if not self.call_chain:
            self.call_chain = [wf.name]
        record = RunRecord(
            run_id=self.run_id,
            workflow=wf.name,
            parent_run_id=self.parent_run_id,
            environment=self.env_name,
            env_values=mask_env(self.env),  # persisted/displayed form is masked
            tags=sorted(getattr(self.workflow_cls, "tags", []) or []),
            inputs=dict(wf.inputs),
            started_at=_now(),
        )
        t0 = time.monotonic()
        steps_meta = self.workflow_cls.collect_steps()
        current = self.workflow_cls.start_step()
        if current is None:
            record.status = "FAILED"
            record.error = "Workflow has no @start step"
            record.ended_at = _now()
            record.duration_ms = (time.monotonic() - t0) * 1000
            self.on_update(record)
            return record

        self.on_update(record)
        visited = 0
        try:
            while current is not None:
                self._check_cancelled()
                visited += 1
                if visited > MAX_STEPS:
                    raise RuntimeError(f"Aborted after {MAX_STEPS} steps (possible cycle)")
                meta = steps_meta.get(current)
                if meta is None:
                    raise RuntimeError(f"Unknown step referenced by next=: {current!r}")
                step_rec = self._make_step_record(meta)
                record.steps.append(step_rec)
                self.on_update(record)
                override_next = self._execute_step(wf, meta, step_rec)
                record.context = self._snapshot_ctx(wf)  # live view of ctx
                self.on_update(record)
                current = override_next if override_next is not None else meta.get("next")
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
            record.outputs = dict(wf.outputs())
            record.context = self._snapshot_ctx(wf)
            record.ended_at = _now()
            record.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            self.on_update(record)
        return record

    # ---------------------------------------------------------------- steps
    def _snapshot_ctx(self, wf: Workflow) -> Dict[str, Any]:
        """Display-safe snapshot of the context for reports: the env dict is
        left out (it has its own report section), secret-looking keys are
        masked, and oversized values are truncated to a repr."""
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

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled()

    @staticmethod
    def _make_step_record(meta: Dict[str, Any]) -> StepRecord:
        return StepRecord(
            name=meta["name"],
            func_name=meta["func_name"],
            condition=meta.get("condition"),
            loop=meta.get("loop"),
            max_attempts=meta.get("retry", 0) + 1,
        )

    def _execute_step(self, wf: Workflow, meta: Dict[str, Any],
                      rec: StepRecord) -> Optional[str]:
        """Run one step (with condition / loop / retry). Returns an optional
        runtime override for the next step name."""
        rec.started_at = _now()
        t0 = time.monotonic()
        wf._logger = lambda msg: rec.logs.append(f"{_now()}  {msg}")

        try:
            # -- condition gate ------------------------------------------
            if meta.get("condition"):
                ok = bool(_eval_expr(meta["condition"], wf.ctx))
                rec.condition_result = ok
                if not ok:
                    rec.status = "SKIPPED"
                    rec.logs.append(f"{_now()}  condition '{meta['condition']}' is false — step skipped")
                    return None

            rec.status = "RUNNING"
            func = getattr(wf, meta["func_name"])

            # -- loop or single execution --------------------------------
            override_next: Optional[str] = None
            if meta.get("loop"):
                var, iterable_expr = _parse_loop(meta["loop"])
                iterable = _eval_expr(iterable_expr, wf.ctx)
                results = []
                for item in iterable:
                    self._check_cancelled()
                    wf.ctx[var] = item
                    rec.iterations += 1
                    res = self._call_with_retry(wf, func, meta, rec)
                    results.append(res)
                    self._merge_result(wf, res)
                rec.result = _short_repr(results)
                wf.ctx[f"{_ctx_key(meta['name'])}_results"] = results
            else:
                res = self._call_with_retry(wf, func, meta, rec)
                if isinstance(res, dict) and "__next__" in res:
                    override_next = res.pop("__next__")
                self._merge_result(wf, res)
                if res is not None:
                    rec.result = _short_repr(res)
                    wf.ctx[f"{_ctx_key(meta['name'])}_result"] = res

            rec.status = "SUCCESS"
            return override_next
        except Exception as exc:
            rec.status = "FAILED"
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.traceback = traceback.format_exc()
            if meta.get("continue_on_error"):
                rec.continued = True
                wf.ctx[f"{_ctx_key(meta['name'])}_error"] = rec.error
                rec.logs.append(
                    f"{_now()}  continue_on_error=True — flow continues with next step"
                )
                return None  # proceed to meta["next"]
            raise
        finally:
            rec.ended_at = _now()
            rec.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            wf._logger = None

    def _call_with_retry(self, wf: Workflow, func, meta: Dict[str, Any],
                         rec: StepRecord) -> Any:
        retries = meta.get("retry", 0)
        delay = meta.get("retry_delay", 0)
        retry_on = meta.get("retry_on")  # None = everything is retryable
        timeout = meta.get("timeout")
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            self._check_cancelled()
            rec.attempts += 1
            try:
                return self._call(wf, func, timeout)
            except Exception as exc:  # noqa: BLE001 - reported in record
                last_exc = exc
                rec.logs.append(
                    f"{_now()}  attempt {attempt}/{retries + 1} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                if retry_on is not None and not isinstance(exc, retry_on):
                    rec.logs.append(
                        f"{_now()}  {type(exc).__name__} is not in retry_on="
                        f"({', '.join(c.__name__ for c in retry_on)}) — failing immediately"
                    )
                    raise
                if attempt <= retries and delay:
                    # sleep in small slices so cancellation stays responsive
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        self._check_cancelled()
                        time.sleep(min(CANCEL_POLL_SECONDS, deadline - time.monotonic()))
        raise last_exc  # type: ignore[misc]

    def _call(self, wf: Workflow, func, timeout: Optional[float] = None) -> Any:
        """Call a step method (with or without the ctx argument), enforcing
        timeout= and staying responsive to cancellation.

        The function runs in a helper thread; this thread polls the future.
        A timed-out or cancelled call is ABANDONED (Python threads cannot be
        killed) — it may keep running in the background, but the flow moves
        on. Steps with timeouts should therefore be safe to duplicate."""
        sig = inspect.signature(func)
        call = (lambda: func(wf.ctx)) if len(sig.parameters) >= 1 else func

        if timeout is None and not self.cancel_event.is_set():
            # fast path — still cancellable at step boundaries
            return call()

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
        return future.result()  # re-raises the step's own exception, if any

    @staticmethod
    def _merge_result(wf: Workflow, res: Any) -> None:
        if isinstance(res, dict):
            wf.ctx.update(res)


def _short_repr(value: Any, limit: int = 500) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
