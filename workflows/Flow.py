"""Sample workflow 1: order processing — shows condition, retry and branching."""
import random

from engine import Workflow, start, step


class OrderFlow(Workflow):
    """Validates an order, applies a discount when the amount is big enough,
    then charges the customer (with retries against a flaky payment API)."""

    description = "Order processing demo — condition + retry + branching"
    tags = ["billing", "demo"]
    inputs = {"amount": 120, "customer": "ACME Corp"}
    inputs_schema = {
        "amount": {"type": "number", "min": 0.01, "required": True,
                   "help": "order total — discount applies above 100"},
        "customer": {"type": "string", "required": True},
        "priority": {"type": "select", "options": ["low", "normal", "high"],
                     "default": "normal"},
    }

    @start(next="Validate")
    def begin(self, ctx):
        self.log(f"Processing order for {ctx['customer']}, amount={ctx['amount']}")
        if self.env:
            self.log(f"environment: api_url={self.env.get('api_url')} dry_run={self.env.get('dry_run')}")
        return {"validated": False}

    @step(name="Validate", next="ApplyDiscount")
    def validate(self, ctx):
        if ctx["amount"] <= 0:
            raise ValueError("Order amount must be positive")
        self.log("Order is valid")
        return {"validated": True}

    @step(name="ApplyDiscount", next="Charge", condition="amount > 100")
    def apply_discount(self, ctx):
        """Only runs when amount > 100 — otherwise SKIPPED."""
        discounted = round(ctx["amount"] * 0.9, 2)
        self.log(f"Big order! 10% discount: {ctx['amount']} -> {discounted}")
        return {"amount": discounted}

    @step(name="Charge", next="Notify", condition="not env.get('dry_run')",
          retry=4, retry_delay=0.5, retry_on=(ConnectionError, TimeoutError))
    def charge(self, ctx):
        """Skipped when the selected environment has dry_run=true.
        Simulates a flaky payment API that fails ~50% of the time.
        Only network-ish errors are retried (retry_on) — a real bug
        (e.g. KeyError) fails immediately instead of hammering the API."""
        if random.random() < 0.5:
            raise ConnectionError("payment gateway timeout")
        self.log(f"Charged {ctx['amount']} to {ctx['customer']} via {self.env.get('api_url', 'default API')}")
        return {"charged": True}

    @step(name="Notify")
    def notify(self, ctx):
        self.log(f"Receipt sent to {ctx['customer']}")
        self.outputs({"charged_amount": ctx["amount"], "customer": ctx["customer"]})
