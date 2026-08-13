"""Demonstrates resumable=True — a loop that RESUMES AT THE FAILED ITEM.

Try it:
  1. Run the flow — items 1–3 process, item 4 fails (marker file missing).
  2. Fix the "outage":   touch /tmp/codeflow-resumable-fixed
     (Windows: type nul > %TEMP%\\codeflow-resumable-fixed)
  3. Click ⏭ resume on the failed run in History.

The resumed run's Crunch step logs "3/5 item(s) already done — skipping"
and processes ONLY items 4 and 5. The work log in the outputs proves each
item was processed exactly once across both runs.

Requirements for resumable loops: sequential only (no parallel=), a
deterministic iterable, and JSON-serializable iteration results.
"""
import os
import tempfile
from pathlib import Path

from engine import Workflow, start, step

FIXED = Path(tempfile.gettempdir()) / "codeflow-resumable-fixed"
WORKLOG = Path(tempfile.gettempdir()) / "codeflow-resumable-worklog"


class ResumableLoopFlow(Workflow):
    description = "resumable=True loop — resume continues at the failed item"
    tags = ["demo"]
    inputs = {"items": [1, 2, 3, 4, 5]}

    @start(next="Crunch")
    def begin(self, ctx):
        # fresh run: reset the demo so it fails at item 4 again
        if FIXED.exists():
            FIXED.unlink()
        WORKLOG.write_text("")
        self.log("fresh run — item 4 will fail until you create "
                 f"{FIXED} and press ⏭ resume")

    @step(name="Crunch", next="Done", loop="i in items", resumable=True)
    def crunch(self, ctx):
        item = ctx["i"]
        if item == 4 and not FIXED.exists():
            raise ConnectionError(f"item {item} hit the simulated outage — "
                                  f"create {FIXED} then resume")
        # side effect AFTER the failure gate: the work log records each
        # item that was actually processed
        with WORKLOG.open("a") as f:
            f.write(f"{item}\n")
        self.log(f"processed item {item}")
        return {"item": item, "squared": item * item}

    @step(name="Done")
    def done(self, ctx):
        processed = WORKLOG.read_text().split()
        self.outputs({
            "processed_order": processed,
            "each_item_once": len(processed) == len(set(processed)),
            "squares": [r["squared"] for r in ctx["Crunch_results"]],
        })
