"""Workflow base class."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class Workflow:
    """Base class for all workflows.

    Subclass it and give it exactly one ``@flow`` method — an ordinary Python
    body that calls ``@step`` methods::

        @dataclass
        class Inputs:
            amount: float = 10.0

        class Flow1(Workflow):
            description = "Says hello"
            inputs = Inputs

            @flow
            def main(self, ctx):
                total = self.charge(ctx["amount"])
                self.log(f"charged {total}")
                return {"total": total}

            @step(retry=2, timeout=30)
            def charge(self, amount):
                return amount * 1.2

    ``ctx`` is a plain dict of the validated run inputs plus ``ctx["env"]``.
    Steps take ordinary arguments and return ordinary values; each call is
    journaled, so a resumed run replays the body and completed steps return
    instantly. Keep the body pure — it re-executes on resume.
    ``self.log(...)`` records a line into the execution report;
    ``self.set_output(...)`` / ``self.outputs(...)`` record workflow outputs.
    """

    #: optional human description shown in the UI
    description: str = ""
    #: True marks this flow as a dashboard — it appears in the Dashboards tab
    #: and its steps build widgets via self.widget(...)
    dashboard: bool = False
    #: True allows this flow to be started via POST /api/hooks/<FlowName>
    #: (explicit opt-in). Optionally set webhook_token to require a secret;
    #: otherwise the global CODEFLOW_WEBHOOK_TOKEN env var applies if set.
    webhook: bool = False
    webhook_token: Optional[str] = None
    #: tags used to group/filter workflows and their runs in the UI
    tags: List[str] = []
    #: run inputs — either a @dataclass TYPE (typed: validated, coerced and
    #: rendered as a form) or a plain dict of untyped defaults.
    #: See engine/inputs.py. The flow body always receives a plain dict ctx.
    inputs: Any = {}

    def __init__(self, name: Optional[str] = None, inputs: Optional[Dict[str, Any]] = None):
        from .inputs import defaults_for

        self.name = name or getattr(self.__class__, "name_override", None) or self.__class__.__name__
        merged = defaults_for(self.__class__)
        merged.update(inputs or {})
        self.inputs = merged
        self.ctx: Dict[str, Any] = dict(merged)
        self._outputs: Dict[str, Any] = {}
        self._logger = None      # injected by the runner
        self._image_sink = None  # injected by the runner (log_image target)
        self._block_sink = None  # injected by the runner (log_json/log_table)
        self._runner = None  # injected by the runner (used by call_workflow)
        self._program_runner = None  # set while a program-mode flow body runs
        self.env: Dict[str, Any] = {}  # selected environment (set by the runner)
        self._widgets: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ api
    def outputs(self, outputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Set (merge) and/or get the workflow outputs."""
        if outputs:
            self._outputs.update(outputs)
        return self._outputs

    def set_output(self, key: str, value: Any) -> None:
        self._outputs[key] = value

    def widget(self, type: str | Dict[str, Any], **props: Any) -> None:
        """Add a dashboard widget. Either a full spec dict or type + props:

            self.widget("metric", title="Orders", value=128)
            self.widget("stat", title="Revenue", value="12.4k", delta="+8%")
            self.widget("status", title="API", value="online", status="ok")
            self.widget("progress", title="Quota", value=64, max=100)
            self.widget("chart", title="Sales", chart="bar",
                        data={"Books": 850, "Games": 1200}, size="wide")
            self.widget("table", title="Orders", rows=[{...}, ...], size="full",
                        format=[  # optional conditional cell formatting
                            {"col": "status", "map": {"paid": "ok", "failed": "err"}},
                            {"col": "amount", "gt": 300, "style": "err"},
                        ])
            self.widget("list", items=[...]);  self.widget("alert", text="…", level="warn")
            self.widget("text", text="…");     self.widget("json", value={...})
            self.widget("section", title="Overview")   # full-width divider

        size: "" (1 column) | "wide" (2 columns) | "full" (entire row).
        """
        spec = dict(type) if isinstance(type, dict) else {"type": type, **props}
        self._widgets.append(spec)

    def widgets(self) -> List[Dict[str, Any]]:
        return self._widgets

    def call_workflow(self, workflow_name: str, inputs: Optional[Dict[str, Any]] = None,
                      env: Optional[str] = None) -> Dict[str, Any]:
        """Run another workflow from inside a step and return its outputs.

        The sub-workflow gets its own run record and HTML report (it shows up
        in the history as a child run). If it fails, this raises — so the
        calling step fails too (and its retry= applies to the whole sub-run).

        The child inherits the parent's environment; pass ``env="prod"`` to
        run it against a different environment from ``environments/``.

            @step(timeout=300)
            def charge(self, amount):
                out = self.call_workflow("PaymentFlow", inputs={"amount": amount})
                return out["receipt_id"]
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

    def sleep(self, seconds: float) -> None:
        """Pause the flow, staying responsive to cancellation.

        Journaled like a step, so a resumed run does not sleep again.
        """
        runner = self._program_runner
        if runner is None:  # pragma: no cover - outside a run
            import time as _t
            _t.sleep(seconds)
            return
        meta = {"name": "sleep", "func_name": "sleep", "retry": 0,
                "retry_delay": 0, "retry_backoff": 1, "retry_on": None,
                "continue_on_error": False, "timeout": None}

        def _do_sleep(wf, secs):
            wf.log(f"sleeping {secs}s")
            runner.sleep(secs)
            return {"slept": secs}

        runner.call_step(self, _do_sleep, meta, (float(seconds),), {})

    def log(self, message: Any) -> None:
        """Log a message into the current step's execution report."""
        if self._logger is not None:
            self._logger(str(message))
        else:  # pragma: no cover - headless use
            print(f"[{self.name}] {message}")

    def log_json(self, data: Any, title: str = "") -> None:
        """Log a structured object — rendered as pretty-printed, syntax-safe
        JSON in the run's report instead of a flat string:

            self.log_json(response.json(), title="API response")
        """
        import json as _json
        try:
            text = _json.dumps(data, indent=2, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - truly unserializable
            text = repr(data)
        if len(text) > self.MAX_BLOCK_CHARS:
            text = text[: self.MAX_BLOCK_CHARS] + "\n… (truncated)"
        self._emit_block({"type": "json", "title": str(title), "text": text})

    def log_table(self, rows: Any, title: str = "") -> None:
        """Log tabular data — rendered as a real table in the run's report:

            self.log_table([{"file": f, "rows": n}, ...], title="Processed")

        Accepts a list of dicts (columns = union of keys) or a list of
        scalars (single 'value' column). Capped at 200 rows.
        """
        if not isinstance(rows, list):
            raise TypeError("log_table expects a list of dicts (or scalars)")
        norm = []
        for r in rows[: self.MAX_TABLE_ROWS]:
            norm.append(r if isinstance(r, dict) else {"value": r})
        truncated = len(rows) > self.MAX_TABLE_ROWS
        self._emit_block({"type": "table", "title": str(title),
                          "rows": norm, "truncated": truncated})

    def _emit_block(self, block: Dict[str, Any]) -> None:
        if self._block_sink is not None:
            self._block_sink(block)
        else:  # pragma: no cover - headless use without a runner
            self.log(f"[{block['type']}: {block.get('title') or 'untitled'}]")

    #: caps for log_image (reports are self-contained HTML — images embed as
    #: base64, so keep them reasonable)
    MAX_IMAGE_BYTES = 3_000_000
    MAX_IMAGES_PER_STEP = 20
    #: caps for log_json / log_table
    MAX_BLOCK_CHARS = 200_000
    MAX_TABLE_ROWS = 200
    MAX_BLOCKS_PER_STEP = 50

    def log_image(self, image: Any, title: str = "", format: str = "png") -> None:
        """Attach an image to the current step — it appears inline in the
        run's HTML report (history). Accepts:

        - a file path: ``self.log_image("out/chart.png", title="Sales")``
        - raw bytes:   ``self.log_image(svg_bytes, format="svg")``
        - a data URI string ("data:image/png;base64,...")
        - a matplotlib figure (anything with .savefig): saved as PNG

        Supported formats: png, jpg/jpeg, gif, svg, webp. Images embed into
        the report as base64 (cap: 3 MB each, 20 per step) so reports stay
        self-contained files.
        """
        import base64
        from pathlib import Path as _P

        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp"}

        if hasattr(image, "savefig"):  # matplotlib figure
            import io
            buf = io.BytesIO()
            image.savefig(buf, format="png", bbox_inches="tight", dpi=110)
            data, fmt = buf.getvalue(), "png"
        elif isinstance(image, (bytes, bytearray)):
            data, fmt = bytes(image), format.lower().lstrip(".")
        elif isinstance(image, (str, _P)) and str(image).startswith("data:image/"):
            self._emit_image({"title": str(title), "data": str(image)})
            return
        elif isinstance(image, (str, _P)):
            p = _P(image)
            if not p.is_file():
                raise FileNotFoundError(f"log_image: no such file: {image}")
            data, fmt = p.read_bytes(), (p.suffix.lstrip(".") or format).lower()
        else:
            raise TypeError(f"log_image: unsupported type {type(image).__name__}")

        if fmt not in mime_map:
            raise ValueError(f"log_image: unsupported format {fmt!r} "
                             f"(use one of {sorted(mime_map)})")
        if len(data) > self.MAX_IMAGE_BYTES:
            self.log(f"log_image: skipped {title or 'image'} — "
                     f"{len(data)} bytes exceeds {self.MAX_IMAGE_BYTES} cap")
            return
        uri = f"data:{mime_map[fmt]};base64,{base64.b64encode(data).decode()}"
        self._emit_image({"title": str(title), "data": uri})

    def _emit_image(self, spec: Dict[str, str]) -> None:
        if self._image_sink is not None:
            self._image_sink(spec)
        else:  # pragma: no cover - headless use without a runner
            self.log(f"[image: {spec.get('title') or 'untitled'}]")

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
    def flow_entry(cls):
        """The @flow-decorated method of a program-based workflow, or None."""
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if getattr(attr, "_is_flow_entry", False):
                return attr
        return None

    @classmethod
    def is_workflow(cls) -> bool:
        return cls is not Workflow and cls.flow_entry() is not None
