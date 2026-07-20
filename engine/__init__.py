"""code-flow: a tiny annotation-based workflow engine.

Define workflows as classes with @start / @step decorated methods:

    from engine import Workflow, start, step

    class MyFlow(Workflow):
        @start(next="Step1")
        def begin(self, ctx):
            return {"a": 42}

        @step(name="Step1", next="Step2", condition="a > 10", retry=3, retry_delay=2)
        def step1(self, ctx):
            ...

        @step(name="Step2", loop="i in items")
        def step2(self, ctx):
            print(ctx["i"])
"""
from .decorators import start, step
from .workflow import Workflow
from .runner import WorkflowRunner, RunRecord, StepRecord, StepTimeoutError
from .registry import discover_workflows

__all__ = [
    "start",
    "step",
    "Workflow",
    "WorkflowRunner",
    "RunRecord",
    "StepRecord",
    "StepTimeoutError",
    "discover_workflows",
]
