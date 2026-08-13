"""Three RESUME-FRIENDLY LOOP PATTERNS, runnable side by side.

Run it once — everything processes. Run it AGAIN — patterns 2 and 3 skip
the work that's already done (that's exactly what makes a failed/resumed
loop safe). Delete the scratch folder to reset the demo:

    rm -rf $TMPDIR/codeflow-loop-demo      (macOS/Linux)

Why these patterns matter: a loop step that fails is re-run FROM THE FIRST
ITEM on retry/resume. These three shapes make that harmless.
"""
import tempfile
from pathlib import Path

from engine import Workflow, start, step

OUT = Path(tempfile.gettempdir()) / "codeflow-loop-demo"


class LoopPatternsFlow(Workflow):
    description = "Resume-friendly loops: markers · idempotent items · filter-then-loop"
    tags = ["demo"]
    inputs = {"items": ["a", "b", "c", "d", "e"], "fail_on": "c"}

    @start(next="Markers")
    def begin(self, ctx):
        OUT.mkdir(exist_ok=True)
        self.log(f"scratch folder: {OUT}")

    # ---- Pattern 1: MARKERS — an iteration never raises -------------------
    # Catch inside the body and return an ok/error marker. The loop always
    # completes, every item lands in Markers_results, and YOU decide later
    # what a partial failure means. Works with parallel= too.
    @step(name="Markers", next="Idempotent", loop="i in items", parallel=2)
    def markers(self, ctx):
        item = ctx["i"]
        try:
            if item == ctx["fail_on"]:
                raise ConnectionError(f"simulated API failure for {item!r}")
            return {"item": item, "ok": True, "value": item.upper()}
        except Exception as e:  # noqa: BLE001 - converted to a marker
            return {"item": item, "ok": False, "error": str(e)}

    # ---- Pattern 2: IDEMPOTENT ITEMS — skip work that's already done ------
    # Each iteration checks for its own output first. A re-run (retry or
    # resume) flies through finished items, so re-processing is a no-op.
    @step(name="Idempotent", next="Filter", loop="i in items")
    def idempotent(self, ctx):
        marker = OUT / f"{ctx['i']}.txt"
        if marker.exists():
            self.log(f"{ctx['i']}: output exists — skipping")
            return {"item": ctx["i"], "skipped": True}
        marker.write_text(ctx["i"].upper())
        return {"item": ctx["i"], "skipped": False}

    # ---- Pattern 3: FILTER THEN LOOP — recompute remaining work -----------
    # A step before the loop asks reality what's left; the loop only ever
    # sees unfinished items. On resume the filter re-runs and shrinks.
    @step(name="Filter", next="Process")
    def filter_remaining(self, ctx):
        remaining = [i for i in ctx["items"] if not (OUT / f"{i}.done").exists()]
        self.log(f"{len(remaining)} of {len(ctx['items'])} item(s) still to process")
        return {"remaining": remaining}

    @step(name="Process", next="Summary", loop="i in remaining")
    def process(self, ctx):
        (OUT / f"{ctx['i']}.done").write_text("ok")
        return {"item": ctx["i"]}

    # ---- Aggregate from <Step>_results, never from running accumulators ---
    @step(name="Summary")
    def summary(self, ctx):
        self.log_table(ctx["Markers_results"], title="Pattern 1 — marker results")
        failed = [r["item"] for r in ctx["Markers_results"] if not r["ok"]]
        self.outputs({
            "marker_failures": failed,
            "idempotent_skipped": [r["item"] for r in ctx["Idempotent_results"] if r["skipped"]],
            "processed_this_run": [r["item"] for r in ctx["Process_results"]],
        })
        if failed:
            self.log(f"partial failure is now a decision: {failed} failed — "
                     "alert, retry later, or raise here if it's fatal")
