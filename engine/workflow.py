"""Workflow base class."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class Workflow:
    """Base class for all workflows.

    Subclass it and declare steps with @start / @step. Example::

        class Flow1(Workflow):
            description = "Says hello"

            @start(next="Step1")
            def begin(self, ctx):
                return {"a": 42}

            @step(name="Step1")
            def step1(self, ctx):
                self.log(f"a is {ctx['a']}")

    Steps receive the shared context dict ``ctx`` (inputs + everything merged
    from previous steps' returned dicts). A step that returns a dict has that
    dict merged into the context. ``self.log(...)`` records a line into the
    execution report. ``self.set_output(...)`` / ``self.outputs(...)`` record
    workflow outputs shown in the report.
    """

    #: optional human description shown in the UI
    description: str = ""
    #: tags used to group/filter workflows and their runs in the UI
    tags: List[str] = []
    #: default inputs (may be overridden per-run)
    inputs: Dict[str, Any] = {}

    def __init__(self, name: Optional[str] = None, inputs: Optional[Dict[str, Any]] = None):
        self.name = name or getattr(self.__class__, "name_override", None) or self.__class__.__name__
        merged = dict(getattr(self.__class__, "inputs", {}) or {})
        merged.update(inputs or {})
        self.inputs = merged
        self.ctx: Dict[str, Any] = dict(merged)
        self._outputs: Dict[str, Any] = {}
        self._logger = None  # injected by the runner
        self._runner = None  # injected by the runner (used by call_workflow)
        self.env: Dict[str, Any] = {}  # selected environment (set by the runner)

    # ------------------------------------------------------------------ api
    def outputs(self, outputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Set (merge) and/or get the workflow outputs."""
        if outputs:
            self._outputs.update(outputs)
        return self._outputs

    def set_output(self, key: str, value: Any) -> None:
        self._outputs[key] = value

    def call_workflow(self, workflow_name: str, inputs: Optional[Dict[str, Any]] = None,
                      env: Optional[str] = None) -> Dict[str, Any]:
        """Run another workflow from inside a step and return its outputs.

        The sub-workflow gets its own run record and HTML report (it shows up
        in the history as a child run). If it fails, this raises — so the
        calling step fails too (and its retry= applies to the whole sub-run).

        The child inherits the parent's environment; pass ``env="prod"`` to
        run it against a different environment from ``environments/``.

            @step(name="Charge")
            def charge(self, ctx):
                out = self.call_workflow("PaymentFlow", inputs={"amount": ctx["amount"]})
                return {"receipt": out["receipt_id"]}
        """
        from .registry import discover_workflows, get_workflows_dir
        from .runner import WorkflowRunner

        registry, _ = discover_workflows(get_workflows_dir())
        cls = registry.get(workflow_name)
        if cls is None:
            raise ValueError(
                f"Unknown workflow {workflow_name!r} (available: {sorted(registry)})"
            )

        parent_runner = self._runner
        chain = list(getattr(parent_runner, "call_chain", []) or [self.name])
        if workflow_name in chain:
            raise RuntimeError(
                f"Workflow call cycle detected: {' -> '.join(chain + [workflow_name])}"
            )

        # environment: inherit the parent's, unless env= names another one
        child_env = dict(getattr(parent_runner, "env", {}) or {})
        child_env_name = getattr(parent_runner, "env_name", None)
        if env is not None:
            from .environments import load_environments
            envs, _ = load_environments()
            if env not in envs:
                raise ValueError(f"Unknown environment {env!r} (available: {sorted(envs)})")
            child_env, child_env_name = envs[env], env

        self.log(f"→ calling sub-workflow {workflow_name} with inputs={inputs or {}}"
                 + (f" env={child_env_name}" if child_env_name else ""))
        runner = WorkflowRunner(
            cls,
            inputs=inputs,
            on_update=parent_runner.on_update if parent_runner else None,
            parent_run_id=parent_runner.run_id if parent_runner else None,
            call_chain=chain + [workflow_name],
            env=child_env,
            env_name=child_env_name,
            cancel_event=getattr(parent_runner, "cancel_event", None),  # cancel cascades
        )
        record = runner.run()
        self.log(
            f"← sub-workflow {workflow_name} finished: {record.status} "
            f"(run {record.run_id}, {record.duration_ms} ms)"
        )
        if record.status != "SUCCESS":
            raise RuntimeError(
                f"Sub-workflow {workflow_name} failed (run {record.run_id}): {record.error}"
            )
        return dict(record.outputs)

    def log(self, message: Any) -> None:
        """Log a message into the current step's execution report."""
        if self._logger is not None:
            self._logger(str(message))
        else:  # pragma: no cover - headless use
            print(f"[{self.name}] {message}")

    # ------------------------------------------------------------- helpers
    @classmethod
    def collect_steps(cls) -> Dict[str, Dict[str, Any]]:
        """Return {step_name: meta} for all decorated methods of this class."""
        steps: Dict[str, Dict[str, Any]] = {}
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            meta = getattr(attr, "_step_meta", None)
            if meta:
                steps[meta["name"]] = dict(meta)
        return steps

    @classmethod
    def start_step(cls) -> Optional[str]:
        for name, meta in cls.collect_steps().items():
            if meta.get("is_start"):
                return name
        return None

    @classmethod
    def is_workflow(cls) -> bool:
        return cls is not Workflow and cls.start_step() is not None
