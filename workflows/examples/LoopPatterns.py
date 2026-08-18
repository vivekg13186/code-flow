"""Three patterns for loops that tolerate failure.

Resume already replays completed iterations from the journal, so a failed
loop continues at the failed item. These patterns handle the other half:
what to do when an *item* is bad rather than the run.

Run it once — everything processes. Run it AGAIN — patterns 2 and 3 skip
the work that is already done. Reset with:

    rm -rf $TMPDIR/codeflow-loop-demo      (macOS/Linux)
"""
import tempfile
from pathlib import Path

from engine import Workflow, flow, parallel_map, step

OUT = Path(tempfile.gettempdir()) / "codeflow-loop-demo"


class LoopPatternsFlow(Workflow):
    description = "Tolerant loops: markers · idempotent items · filter-then-loop"
    tags = ["demo"]
    inputs = {"items": ["a", "b", "c", "d", "e"], "fail_on": "c"}

    @flow
    def main(self, ctx):
        self.prepare()

        # -- Pattern 1: MARKERS — an iteration never raises ----------------
        # Catch inside the step and return an ok/error marker. The loop
        # always completes and YOU decide later what a partial failure
        # means. Works with parallel_map too.
        marked = parallel_map(
            lambda i: self.try_item(i, ctx["fail_on"]), ctx["items"], workers=2)
        failed = [r["item"] for r in marked if not r["ok"]]

        # -- Pattern 2: IDEMPOTENT ITEMS — skip work already done ----------
        # Each step checks for its own output first, so a re-run (or a
        # resume that re-executes an item) is a no-op.
        skipped = []
        for item in ctx["items"]:
            if self.write_once(item)["skipped"]:
                skipped.append(item)

        # -- Pattern 3: FILTER THEN LOOP — recompute remaining work --------
        # Ask reality what is left; the loop only ever sees unfinished work.
        remaining = self.remaining(ctx["items"])
        for item in remaining:
            self.finish(item)

        self.log_table(marked, title="Pattern 1 — marker results")
        if failed:
            self.log(f"partial failure is a decision: {failed} failed — "
                     "alert, retry later, or raise here if it is fatal")
        return {"marker_failures": failed,
                "idempotent_skipped": skipped,
                "processed_this_run": remaining}

    @step()
    def prepare(self):
        OUT.mkdir(exist_ok=True)
        self.log(f"scratch folder: {OUT}")

    @step()
    def try_item(self, item, fail_on):
        try:
            if item == fail_on:
                raise ConnectionError(f"simulated API failure for {item!r}")
            return {"item": item, "ok": True, "value": item.upper()}
        except Exception as exc:  # noqa: BLE001 - converted to a marker
            return {"item": item, "ok": False, "error": str(exc)}

    @step()
    def write_once(self, item):
        marker = OUT / f"{item}.txt"
        if marker.exists():
            self.log(f"{item}: output exists — skipping")
            return {"item": item, "skipped": True}
        marker.write_text(item.upper())
        return {"item": item, "skipped": False}

    @step()
    def remaining(self, items):
        left = [i for i in items if not (OUT / f"{i}.done").exists()]
        self.log(f"{len(left)} of {len(items)} item(s) still to process")
        return left

    @step()
    def finish(self, item):
        (OUT / f"{item}.done").write_text("ok")
        return item
