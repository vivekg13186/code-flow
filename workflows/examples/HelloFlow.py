"""Lives in workflows/examples/ — shows that subfolders are scanned too.

The folder name ("examples") is added to this flow's tags automatically,
so subfolders act as groups in the UI.
"""
from engine import Workflow, start, step


class HelloFlow(Workflow):
    description = "Minimal flow living in a subfolder (auto-tagged 'examples')"
    inputs = {"who": "world"}

    @start(next="Greet")
    def begin(self, ctx):
        self.log("hello flow starting")

    @step(name="Greet")
    def greet(self, ctx):
        message = f"Hello, {ctx['who']}!"
        self.log(message)
        self.outputs({"message": message})
