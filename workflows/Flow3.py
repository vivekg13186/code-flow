"""Sample workflow 3: calls another workflow as a sub-workflow."""
from engine import Workflow, flow, step


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

    @flow
    def main(self, ctx):
        self.log(f"Billing {len(ctx['orders'])} customers")
        billed = []
        for order in ctx["orders"]:
            billed.append(self.bill(order))
        total = round(sum(billed), 2)
        self.log(f"Billed total {total} across {len(billed)} customers")
        return {"total_billed": total, "customers": len(billed)}

    @step(retry=1, retry_delay=1, timeout=300)
    def bill(self, order):
        """One journaled step per customer — a resume skips the ones that
        already went through."""
        out = self.call_workflow("OrderFlow", inputs=order)
        return out["charged_amount"]
