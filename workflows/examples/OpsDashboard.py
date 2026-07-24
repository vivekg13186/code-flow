"""Sample dashboard: set ``dashboard = True`` and build widgets in steps.

Open it from the Dashboards tab. Refreshing the dashboard runs the whole
flow (transiently — no history entry) and renders whatever widgets the
steps produced with self.widget(...).
"""
import random

from engine import Workflow, start, step


class OpsDashboard(Workflow):
    dashboard = True
    description = "Demo operations dashboard — metrics, charts, table"
    tags = ["demo"]
    inputs = {"region": "EU", "top_customers": 5}
    inputs_schema = {
        "region": {"type": "select", "options": ["EU", "US", "APAC"], "default": "EU"},
        "top_customers": {"type": "integer", "min": 1, "max": 10, "default": 5,
                          "help": "rows in the recent-orders table"},
    }

    @start(next="Collect")
    def begin(self, ctx):
        self.log(f"collecting metrics for {ctx['region']}")

    @step(name="Collect", next="Build")
    def collect(self, ctx):
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
            "trend": [{"label": f"{h}:00", "value": rnd.randint(20, 90)} for h in range(8, 18)],
            "recent": [
                {"order": f"#{1000 + i}", "customer": c, "amount": rnd.randint(20, 400),
                 "status": rnd.choice(["paid", "pending", "shipped"])}
                for i, c in enumerate(["ACME Corp", "Globex", "Initech", "Umbrella",
                                       "Stark", "Wayne", "Wonka", "Tyrell", "Hooli",
                                       "Aperture"][: ctx["top_customers"]])
            ],
        }

    @step(name="Build")
    def build(self, ctx):
        self.widget("section", title=f"Overview — {ctx['region']}")
        self.widget("metric", title="Orders today", value=ctx["orders"])
        self.widget("stat", title="Revenue", value=f"€{ctx['revenue']:,.0f}",
                    delta=f"{ctx['delta']:+d}%")
        self.widget("status", title="Payment API",
                    value="online" if not self.env.get("dry_run") else "dry-run",
                    status="ok" if not self.env.get("dry_run") else "warn")
        self.widget("progress", title="Daily quota", value=ctx["quota_used"], max=100)

        self.widget("section", title="Sales")
        self.widget("chart", title="By category", chart="bar",
                    data=ctx["by_category"], size="wide")
        self.widget("chart", title="Orders per hour", chart="line",
                    data=ctx["trend"], size="wide")
        self.widget("chart", title="Category share", chart="pie", data=ctx["by_category"])
        if ctx["quota_used"] > 85:
            self.widget("alert", text="Daily quota nearly exhausted", level="warn")

        self.widget("section", title="Recent orders")
        self.widget("table", rows=ctx["recent"], title="Recent orders", size="full",
                    format=[
                        # map: value -> style (ok | warn | err | info | muted)
                        {"col": "status", "map": {"paid": "ok", "pending": "warn",
                                                  "shipped": "info"}},
                        # comparison rules: first match wins
                        {"col": "amount", "gt": 300, "style": "err"},
                        {"col": "amount", "lt": 50, "style": "muted"},
                    ])
        self.outputs({"orders": ctx["orders"], "revenue": ctx["revenue"]})
