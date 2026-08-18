"""Sample workflow 1: order processing — conditions, retries, branching."""
import random
from dataclasses import dataclass, field
from typing import Literal

from engine import Workflow, flow, step


@dataclass
class OrderInputs:
    """Typed run inputs — rendered as the run form, validated on every start."""
    customer: str = "ACME Corp"
    amount: float = field(default=120.0,
                          metadata={"min": 0.01,
                                    "help": "order total — discount applies above 100"})
    priority: Literal["low", "normal", "high"] = "normal"


class OrderFlow(Workflow):
    """Validates an order, applies a discount when the amount is big enough,
    then charges the customer (with retries against a flaky payment API)."""

    description = "Order processing demo — if/else + retry + dry-run guard"
    tags = ["billing", "demo"]
    inputs = OrderInputs

    @flow
    def main(self, ctx):
        self.log(f"Processing order for {ctx['customer']}, amount={ctx['amount']}")
        self.validate(ctx["amount"])

        amount = ctx["amount"]
        if amount > 100:                       # plain Python, no condition=
            amount = self.apply_discount(amount)

        if self.env.get("dry_run"):
            self.log("dry run — skipping the charge")
        else:
            self.charge(ctx["customer"], amount)
            self.notify(ctx["customer"])

        return {"charged_amount": amount, "customer": ctx["customer"]}

    @step()
    def validate(self, amount):
        if amount <= 0:
            raise ValueError("Order amount must be positive")
        self.log("Order is valid")
        return True

    @step()
    def apply_discount(self, amount):
        discounted = round(amount * 0.9, 2)
        self.log(f"Big order! 10% discount: {amount} -> {discounted}")
        return discounted

    @step(retry=4, retry_delay=0.5, retry_on=(ConnectionError, TimeoutError),
          timeout=30)
    def charge(self, customer, amount):
        """Simulates a flaky payment API that fails ~50% of the time.
        Only network-ish errors retry — a real bug fails immediately."""
        if random.random() < 0.5:
            raise ConnectionError("payment gateway timeout")
        self.log(f"Charged {amount} to {customer} via "
                 f"{self.env.get('api_url', 'default API')}")
        return {"charged": True}

    @step()
    def notify(self, customer):
        self.log(f"Receipt sent to {customer}")
