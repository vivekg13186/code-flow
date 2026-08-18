"""Parallel fan-out, exponential backoff, and webhook triggering.

Trigger externally (flow opts in with webhook = True):

    curl -X POST localhost:8000/api/hooks/ParallelFetchFlow \
         -H "Content-Type: application/json" -d '{"count": 6}'
"""
import random
import time
from dataclasses import dataclass, field

from engine import Workflow, flow, parallel_map, step


@dataclass
class FetchInputs:
    count: int = field(default=8, metadata={"min": 1, "max": 50,
                                            "help": "number of sources to fetch"})


class ParallelFetchFlow(Workflow):
    description = "Fetch N sources concurrently with backoff retries"
    tags = ["demo"]
    inputs = FetchInputs
    webhook = True          # allow POST /api/hooks/ParallelFetchFlow
    # webhook_token = "s3cret"  # optionally require ?token= / X-Webhook-Token

    @flow
    def main(self, ctx):
        self.log(f"fetching {ctx['count']} sources")
        urls = [f"https://example.com/api/{i}" for i in range(ctx["count"])]

        # 4 at a time; each call is journaled, so a resume only re-runs
        # the ones that did not finish
        results = parallel_map(self.fetch, urls, workers=4)

        total = sum(r["bytes"] for r in results)
        self.log_table(results, title="Fetched sources")
        self.log(f"fetched {len(results)} sources, {total} bytes")
        return {"sources": len(results), "total_bytes": total}

    @step(retry=3, retry_delay=0.2, retry_backoff=2,   # waits 0.2s, 0.4s, 0.8s
          retry_on=ConnectionError, timeout=15)
    def fetch(self, url):
        """Flaky network call — only ConnectionError is retried."""
        time.sleep(0.3)
        if random.random() < 0.25:
            raise ConnectionError(f"timeout fetching {url}")
        return {"url": url, "bytes": random.randint(500, 5000)}
