"""Demonstrates retry_on and continue_on_error."""
import random

from engine import Workflow, start, step


class RetryDemoFlow(Workflow):
    """FetchData retries only ConnectionError; SendMetrics is best-effort —
    it always fails, but continue_on_error lets the flow finish anyway."""

    description = "retry_on + continue_on_error demo"
    inputs = {"fail_rate": 0.6}

    @start(next="FetchData")
    def begin(self, ctx):
        self.log("starting")

    @step(name="FetchData", next="SendMetrics",
          retry=5, retry_delay=0.3, retry_on=ConnectionError)
    def fetch(self, ctx):
        """Flaky network call — ConnectionError is retried up to 5x.
        Any other exception type would fail the step immediately."""
        if random.random() < ctx["fail_rate"]:
            raise ConnectionError("upstream unreachable")
        self.log("data fetched")
        return {"rows": 42}

    @step(name="SendMetrics", next="Done", continue_on_error=True)
    def metrics(self, ctx):
        """Best-effort: always fails, but the flow continues. The error is
        stored in ctx['SendMetrics_error'] for later steps to inspect."""
        raise RuntimeError("metrics endpoint is down")

    @step(name="Done")
    def done(self, ctx):
        if "SendMetrics_error" in ctx:
            self.log(f"finished despite: {ctx['SendMetrics_error']}")
        self.outputs({"rows": ctx["rows"], "metrics_ok": "SendMetrics_error" not in ctx})
