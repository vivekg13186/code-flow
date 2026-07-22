"""Demonstrates parallel loops, exponential backoff, and webhook triggering.

Trigger externally (flow opts in with webhook = True):

    curl -X POST localhost:8000/api/hooks/ParallelFetchFlow \
         -H "Content-Type: application/json" -d '{"count": 6}'
"""
import random
import time

from engine import Workflow, start, step


class ParallelFetchFlow(Workflow):
    description = "Fetch N sources concurrently (parallel=4) with backoff retries"
    tags = ["demo"]
    inputs = {"count": 8}
    inputs_schema = {
        "count": {"type": "integer", "min": 1, "max": 50, "required": True,
                  "help": "number of sources to fetch"},
    }
    webhook = True          # allow POST /api/hooks/ParallelFetchFlow
    # webhook_token = "s3cret"  # optionally require ?token= / X-Webhook-Token

    @start(next="MakeList")
    def begin(self, ctx):
        self.log(f"fetching {ctx['count']} sources")

    @step(name="MakeList", next="Fetch")
    def make_list(self, ctx):
        return {"sources": [f"https://example.com/api/{i}" for i in range(ctx["count"])]}

    @step(name="Fetch", next="Aggregate", loop="url in sources",
          parallel=4,                       # 4 iterations at a time
          retry=3, retry_delay=0.2, retry_backoff=2,   # waits 0.2s, 0.4s, 0.8s
          retry_on=ConnectionError, timeout=15)
    def fetch(self, ctx):
        """Each iteration sees its own context snapshot with ctx['url'] set.
        NOTE: with parallel= don't use running accumulators — return the
        per-item result and aggregate from Fetch_results afterwards."""
        time.sleep(0.3)  # simulate network latency
        if random.random() < 0.25:
            raise ConnectionError(f"timeout fetching {ctx['url']}")
        return {"url": ctx["url"], "bytes": random.randint(500, 5000)}

    @step(name="Aggregate")
    def aggregate(self, ctx):
        results = ctx["Fetch_results"]      # ordered like the input list
        total = sum(r["bytes"] for r in results)
        self.log(f"fetched {len(results)} sources, {total} bytes")
        self.outputs({"sources": len(results), "total_bytes": total})
