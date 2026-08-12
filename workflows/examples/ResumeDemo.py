"""Demonstrates RESUME FROM FAILED STEP.

Try it:
  1. Run this flow — "Expensive" succeeds (pretend it took 10 minutes),
     then "Fragile" FAILS because a marker file is missing.
  2. "Fix the outage" by creating the marker file it asks for:
         macOS/Linux:  touch /tmp/codeflow-resume-demo   (path shown in the log)
         Windows:      type nul > %TEMP%\\codeflow-resume-demo
  3. In History, click ⏭ resume on the failed run.

The resumed run starts directly at "Fragile" with the context restored —
watch the report: "Expensive" is NOT re-executed (its results, including
the timestamp, are exactly the ones from the failed run).
"""
import os
import tempfile
import time
from engine import Workflow, start, step

MARKER = os.path.join(tempfile.gettempdir(), "codeflow-resume-demo")


class ResumeDemoFlow(Workflow):
    description = "Fails at 'Fragile' until a marker file exists — then ⏭ resume"
    tags = ["demo"]
    inputs = {"batch": 4}

    @start(next="Expensive")
    def begin(self, ctx):
        # clean the marker so every fresh run demonstrates the failure
        if os.path.exists(MARKER):
            os.remove(MARKER)
            self.log("removed old marker — this run will fail at Fragile again")

    @step(name="Expensive", next="Fragile")
    def expensive(self, ctx):
        """Pretend this step is slow/costly — resume must not repeat it."""
        self.log("doing 'expensive' work…")
        time.sleep(1)
        data = [i * 10 for i in range(ctx["batch"])]
        return {"data": data, "computed_at": time.strftime("%H:%M:%S")}

    @step(name="Fragile", next="Save")
    def fragile(self, ctx):
        """Fails until the marker file exists (simulates an outage)."""
        if not os.path.exists(MARKER):
            self.log(f"to fix the 'outage', create this file:  {MARKER}")
            raise ConnectionError("upstream unavailable — see step log for the fix")
        self.log("upstream is back!")
        return {"total": sum(ctx["data"])}

    @step(name="Save")
    def save(self, ctx):
        self.log(f"data was computed at {ctx['computed_at']} — "
                 "unchanged if this run was resumed")
        self.outputs({"total": ctx["total"], "computed_at": ctx["computed_at"]})
