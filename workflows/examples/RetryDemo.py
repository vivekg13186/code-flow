"""Demonstrates retry_on and continue_on_error."""
import random

from engine import Workflow, flow, step


class RetryDemoFlow(Workflow):
    """FetchData retries only ConnectionError; SendMetrics is best-effort —
    it always fails, but continue_on_error lets the flow finish anyway."""

    description = "retry_on + continue_on_error demo"
    tags = ["demo"]
    inputs = {"fail_rate": 0.6}

    @flow
    def main(self, ctx):
        rows = self.fetch(ctx["fail_rate"])

        # continue_on_error steps return None instead of raising
        metrics = self.send_metrics()
        if metrics is None:
            self.log("metrics failed — continuing, it is best-effort")

        return {"rows": rows, "metrics_ok": metrics is not None}

    @step(retry=5, retry_delay=0.3, retry_on=ConnectionError, timeout=20)
    def fetch(self, fail_rate):
        """Flaky network call — ConnectionError is retried up to 5x.
        Any other exception type fails the step immediately."""
        if random.random() < fail_rate:
            raise ConnectionError("upstream unreachable")
        self.log("data fetched")
        return 42

    @step(continue_on_error=True)
    def send_metrics(self):
        """Best-effort: always fails, but the flow continues."""
        raise RuntimeError("metrics endpoint is down")
