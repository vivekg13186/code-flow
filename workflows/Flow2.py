"""Sample workflow 2: batch ETL — shows loops and dynamic next-step override."""
import time

from engine import Workflow, start, step


class BatchEtlFlow(Workflow):
    """Fetches a list of files, processes each one in a loop, then either
    publishes or archives the batch depending on how many rows came out."""

    description = "Batch ETL demo — loop over items + dynamic branching"
    tags = ["etl", "demo"]
    inputs = {"batch_size": 5}

    @start(next="Extract")
    def begin(self, ctx):
        self.log(f"Starting ETL batch of {ctx['batch_size']} files")

    @step(name="Extract", next="Transform")
    def extract(self, ctx):
        files = [f"data_{i:03d}.csv" for i in range(1, ctx["batch_size"] + 1)]
        self.log(f"Found {len(files)} files")
        # structured logging: rendered as a real table in the report
        self.log_table([{"file": f, "size_kb": 40 + 3 * i}
                        for i, f in enumerate(files)], title="Discovered files")
        return {"files": files, "rows": 0}

    @step(name="Transform", next="Decide", loop="f in files", retry=2, retry_delay=0.2)
    def transform(self, ctx):
        """Runs once per file thanks to loop="f in files"."""
        time.sleep(0.15)  # simulate work
        rows = 10 + len(ctx["f"])
        self.log(f"Processed {ctx['f']} -> {rows} rows")
        return {"rows": ctx["rows"] + rows}

    @step(name="Decide", next="Publish")
    def decide(self, ctx):
        """Return {"__next__": ...} to pick the next step at runtime."""
        # structured logging: rendered as pretty JSON in the report
        self.log_json({"rows": ctx["rows"], "threshold": 50,
                       "decision": "publish" if ctx["rows"] >= 50 else "archive"},
                      title="Decision input")
        if ctx["rows"] < 50:
            self.log(f"Only {ctx['rows']} rows — archiving instead of publishing")
            return {"__next__": "Archive"}
        self.log(f"{ctx['rows']} rows — publishing")

    @step(name="Publish")
    def publish(self, ctx):
        self.log("Published batch to warehouse")
        self.outputs({"published": True, "total_rows": ctx["rows"]})

    @step(name="Archive")
    def archive(self, ctx):
        self.log("Batch archived for review")
        self.outputs({"published": False, "total_rows": ctx["rows"]})
