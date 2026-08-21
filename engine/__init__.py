"""code flow: a tiny workflow engine where flows are plain Python.

A workflow is a class with one @flow body; the steps it calls are journaled,
so a resumed run replays the body and completed steps return instantly.

    from engine import Workflow, flow, step, parallel_map

    class MyFlow(Workflow):
        description = "Shown in the UI"
        inputs = {"service": "payments"}

        @flow
        def main(self, ctx):
            art = self.build(ctx["service"])
            for host in self.hosts(ctx["service"]):
                self.push(art, host)
            return {"deployed": True}

        @step(retry=3, retry_delay=2, retry_on=ConnectionError, timeout=30)
        def push(self, artifact, host):
            ...
"""
from .decorators import flow, step
from .workflow import Workflow
from .runner import (WorkflowRunner, RunRecord, StepRecord, StepTimeoutError,
                     NondeterminismError, parallel_map)
from .registry import discover_workflows
from .steps import HttpError, HttpClientError, HttpServerError, ShellError

__all__ = [
    "flow",
    "step",
    "parallel_map",
    "NondeterminismError",
    "Workflow",
    "WorkflowRunner",
    "RunRecord",
    "StepRecord",
    "StepTimeoutError",
    "discover_workflows",
    # standard step library errors (self.http / self.fs / self.sh / self.db)
    "HttpError",
    "HttpClientError",
    "HttpServerError",
    "ShellError",
]
