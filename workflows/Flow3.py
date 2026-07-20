"""Sample workflow 3: calls another workflow (OrderFlow) as a sub-workflow."""
from engine import Workflow, start, step


class DailyBillingFlow(Workflow):
    """Loops over customers and runs the whole OrderFlow for each one via
    ``self.call_workflow(...)``. Each sub-run gets its own report, linked
    in the history (↳)."""

    description = "Parent flow — runs OrderFlow per customer via call_workflow"
    tags = ["billing", "batch"]
    inputs = {
        "orders": [
            {"customer": "ACME Corp", "amount": 250},
            {"customer": "Globex", "amount": 80},
        ]
    }

    @start(next="BillCustomers")
    def begin(self, ctx):
        self.log(f"Billing {len(ctx['orders'])} customers")
        return {"billed": []}

    @step(name="BillCustomers", next="Summary", loop="order in orders", retry=1, retry_delay=1)
    def bill(self, ctx):
        out = self.call_workflow("OrderFlow", inputs=ctx["order"])  # ← sub-workflow
        return {"billed": ctx["billed"] + [out["charged_amount"]]}

    @step(name="Summary")
    def summary(self, ctx):
        total = round(sum(ctx["billed"]), 2)
        self.log(f"Billed total {total} across {len(ctx['billed'])} customers")
        self.outputs({"total_billed": total, "customers": len(ctx["billed"])})
