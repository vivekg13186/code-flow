"""Sample dashboard: set ``dashboard = True`` and build widgets in steps.

Open it from the Dashboards tab. Refreshing the dashboard runs the whole
flow (transiently — no history entry) and renders whatever widgets the
steps produced with self.widget(...).
"""
import random
from dataclasses import dataclass, field
from typing import Literal

from engine import Workflow, flow, step


@dataclass
class DashboardInputs:
    region: Literal["EU", "US", "APAC"] = "EU"
    top_customers: int = field(default=5,
                               metadata={"min": 1, "max": 10,
                                         "help": "rows in the recent-orders table"})


class OpsDashboard(Workflow):
    dashboard = True
    description = "Demo operations dashboard — metrics, charts, table"
    tags = ["demo"]
    inputs = DashboardInputs

    @flow
    def main(self, ctx):
        self.log(f"collecting metrics for {ctx['region']}")
        data = self.collect(ctx["top_customers"])
        self.build(ctx["region"], data)
        return {"orders": data["orders"], "revenue": data["revenue"]}

    @step(timeout=30)
    def collect(self, top_customers):
        """Normally you'd call APIs / DBs here (requests is preinstalled)."""
        rnd = random.Random()
        return {
            "orders": rnd.randint(90, 160),
            "revenue": round(rnd.uniform(9000, 16000), 2),
            "delta": rnd.randint(-15, 25),
            "quota_used": rnd.randint(40, 95),
            "by_category": {
                "Electronics": rnd.randint(800, 1500),
                "Books": rnd.randint(300, 900),
                "Games": rnd.randint(500, 1300),
                "Home": rnd.randint(200, 700),
            },
            "trend": [{"label": f"{h}:00", "value": rnd.randint(20, 90)}
                      for h in range(8, 18)],
            "recent": [
                {"order": f"#{1000 + i}", "customer": c, "amount": rnd.randint(20, 400),
                 "status": rnd.choice(["paid", "pending", "shipped"])}
                for i, c in enumerate(["ACME Corp", "Globex", "Initech", "Umbrella",
                                       "Stark", "Wayne", "Wonka", "Tyrell", "Hooli",
                                       "Aperture"][:top_customers])
            ],
        }

    @step()
    def build(self, region, data):
        self.widget("section", title=f"Overview — {region}")
        self.widget("metric", title="Orders today", value=data["orders"])
        self.widget("stat", title="Revenue", value=f"€{data['revenue']:,.0f}",
                    delta=f"{data['delta']:+d}%")
        self.widget("status", title="Payment API",
                    value="online" if not self.env.get("dry_run") else "dry-run",
                    status="ok" if not self.env.get("dry_run") else "warn")
        self.widget("progress", title="Daily quota", value=data["quota_used"], max=100)

        self.widget("section", title="Sales")
        self.widget("chart", title="By category", chart="bar",
                    data=data["by_category"], size="wide")
        self.widget("chart", title="Orders per hour", chart="line",
                    data=data["trend"], size="wide")
        self.widget("chart", title="Category share", chart="pie",
                    data=data["by_category"])
        if data["quota_used"] > 85:
            self.widget("alert", text="Daily quota nearly exhausted", level="warn")

        self.widget("section", title="Recent orders")
        self.widget("table", rows=data["recent"], title="Recent orders", size="full",
                    format=[
                        # map: value -> style (ok | warn | err | info | muted)
                        {"col": "status", "map": {"paid": "ok", "pending": "warn",
                                                  "shipped": "info"}},
                        # comparison rules: first match wins
                        {"col": "amount", "gt": 300, "style": "err"},
                        {"col": "amount", "lt": 50, "style": "muted"},
                    ])
