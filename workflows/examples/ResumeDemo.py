"""Demonstrates RESUME — both step-level and inside a loop.

Try it:
  1. Run this flow — "Expensive" and the first two loop items succeed, then
     "Fragile" FAILS because a marker file is missing.
  2. "Fix the outage" by creating the marker file it names:
         macOS/Linux:  touch /tmp/codeflow-resume-demo
         Windows:      type nul > %TEMP%\\codeflow-resume-demo
  3. In History, click ⏭ resume on the failed run.

The resumed run replays the flow body, but every step that already
completed returns instantly from the journal — watch the report: the
expensive step and the finished loop items are marked "carried over", and
its timestamp is unchanged. Only the failed item onwards actually runs.
"""
import os
import tempfile
import time

from engine import Workflow, flow, step

MARKER = os.path.join(tempfile.gettempdir(), "codeflow-resume-demo")


class ResumeDemoFlow(Workflow):
    description = "Fails until a marker file exists — then ⏭ resume"
    tags = ["demo"]
    inputs = {"items": ["alpha", "beta", "gamma", "delta"]}

    @flow
    def main(self, ctx):
        # Side effects belong in steps: the body re-executes on resume, so a
        # reset written here would undo the fix you just made.
        self.reset_demo()

        data = self.expensive(len(ctx["items"]))

        done = []
        for item in ctx["items"]:
            done.append(self.handle(item))       # journaled per item

        self.fragile()
        self.log(f"data computed at {data['computed_at']} — "
                 "unchanged if this run was resumed")
        return {"handled": done, "computed_at": data["computed_at"]}

    @step()
    def reset_demo(self):
        """Makes a fresh run fail again; journaled, so resume skips it."""
        if os.path.exists(MARKER):
            os.remove(MARKER)

    @step(timeout=30)
    def expensive(self, n):
        """Pretend this is slow/costly — resume must not repeat it."""
        self.log("doing 'expensive' work…")
        time.sleep(1)
        return {"values": [i * 10 for i in range(n)],
                "computed_at": time.strftime("%H:%M:%S")}

    @step()
    def handle(self, item):
        self.log(f"handled {item}")
        return item.upper()

    @step(retry=1, retry_delay=0.5, timeout=20)
    def fragile(self):
        """Fails until the marker file exists (simulates an outage)."""
        if not os.path.exists(MARKER):
            self.log(f"to fix the 'outage', create this file:  {MARKER}")
            raise ConnectionError("upstream unavailable — see step log for the fix")
        self.log("upstream is back!")
        return True
