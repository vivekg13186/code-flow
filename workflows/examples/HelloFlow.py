"""Lives in workflows/examples/ — shows that subfolders are scanned too.

The folder name ("examples") is added to this flow's tags automatically,
so subfolders act as groups in the UI.
"""
from engine import Workflow, flow, step

from _lib.helpers import shout  # shared code from workflows/_lib/


class HelloFlow(Workflow):
    description = "Minimal flow living in a subfolder (auto-tagged 'examples')"
    inputs = {"who": "world"}

    @flow
    def main(self, ctx):
        self.log("hello flow starting")
        message = self.greet(ctx["who"])
        return {"message": message}

    @step()
    def greet(self, who):
        message = shout(f"Hello, {who}")
        self.log(message)
        # log_image: attach an image to this step — it shows up inline in
        # the run's HTML report (also accepts file paths, bytes, data URIs
        # and matplotlib figures)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="64">'
               f'<rect width="260" height="64" rx="10" fill="#eef2ff"/>'
               f'<text x="130" y="40" font-size="20" font-family="sans-serif" '
               f'text-anchor="middle" fill="#4338ca">{message}</text></svg>')
        self.log_image(svg.encode(), title="greeting badge", format="svg")
        return message
