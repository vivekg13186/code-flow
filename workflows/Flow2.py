"""Sample workflow 2: batch ETL — loops, branching, structured logs."""
import time
from dataclasses import dataclass, field

from engine import Workflow, flow, step


@dataclass
class EtlInputs:
    batch_size: int = field(default=5, metadata={"min": 1, "max": 100})


class BatchEtlFlow(Workflow):
    """Fetches a list of files, processes each one, then either publishes or
    archives the batch depending on how many rows came out."""

    description = "Batch ETL demo — for loop + branch + structured logging"
    tags = ["etl", "demo"]
    inputs = EtlInputs

    @flow
    def main(self, ctx):
        self.log(f"Starting ETL batch of {ctx['batch_size']} files")
        files = self.extract(ctx["batch_size"])

        rows = 0
        for f in files:                          # a real for loop
            rows += self.transform(f)            # each call journaled

        self.log_json({"rows": rows, "threshold": 50,
                       "decision": "publish" if rows >= 50 else "archive"},
                      title="Decision input")
        if rows >= 50:                           # a real branch
            self.publish(rows)
            published = True
        else:
            self.archive(rows)
            published = False

        return {"published": published, "total_rows": rows}

    @step()
    def extract(self, batch_size):
        files = [f"data_{i:03d}.csv" for i in range(1, batch_size + 1)]
        self.log(f"Found {len(files)} files")
        self.log_table([{"file": f, "size_kb": 40 + 3 * i}
                        for i, f in enumerate(files)], title="Discovered files")
        return files

    @step(retry=2, retry_delay=0.2, timeout=30)
    def transform(self, filename):
        time.sleep(0.15)                         # simulate work
        rows = 10 + len(filename)
        self.log(f"Processed {filename} -> {rows} rows")
        return rows

    @step()
    def publish(self, rows):
        self.log(f"Published {rows} rows to the warehouse")

    @step()
    def archive(self, rows):
        self.log(f"Only {rows} rows — batch archived for review")
