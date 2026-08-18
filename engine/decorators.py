"""Decorators: @flow marks the workflow body, @step marks a journaled call.

A workflow is a class with exactly one @flow method whose body is ordinary
Python. Steps are methods called from that body; each call is journaled, so
a resumed run replays the body and completed steps return their recorded
result instead of executing again.
"""
from __future__ import annotations

import functools
from typing import Callable, Optional, Tuple, Type, Union

ExcTypes = Optional[Union[Type[BaseException], Tuple[Type[BaseException], ...]]]


def _normalize_retry_on(retry_on: ExcTypes) -> Optional[Tuple[Type[BaseException], ...]]:
    if retry_on is None:
        return None
    if isinstance(retry_on, type):
        return (retry_on,)
    return tuple(retry_on)


def flow(func: Callable) -> Callable:
    """Mark the workflow body — the program that orchestrates the steps.

        class DeployFlow(Workflow):
            @flow
            def main(self, ctx):
                art = self.build(ctx["service"])       # journaled step call
                for host in self.hosts(ctx["service"]):
                    self.push(art, host)
                if ctx["smoke"]:
                    self.smoke_test()
                return {"deployed": True}              # dict -> outputs

    ``ctx`` is the validated inputs plus ``ctx["env"]``. The body may use
    any Python control flow: if/for/while/try/except/finally, helpers,
    comprehensions.

    DETERMINISM: on ⏭ resume the body re-executes from the top (completed
    steps return instantly from the journal), so keep side effects — and
    datetime.now(), random, uuid — inside @step methods. ``codeflow lint``
    flags violations.
    """
    func._is_flow_entry = True
    return func


def step(
    name: Optional[str] = None,
    retry: int = 0,
    retry_delay: float = 0,
    retry_backoff: float = 1,
    retry_on: ExcTypes = None,
    continue_on_error: bool = False,
    timeout: Optional[float] = None,
):
    """Mark a method as a step: a journaled unit of work.

    Steps take ordinary arguments and return ordinary values::

        @step(retry=3, retry_delay=2, retry_backoff=2,
              retry_on=(ConnectionError, TimeoutError), timeout=30)
        def push(self, artifact: str, host: str) -> str:
            ...
            return host

    Parameters
    ----------
    name:        label shown in the report (defaults to the method name).
    retry:       retries after a failure (total attempts = retry + 1).
    retry_delay: seconds to wait between attempts.
    retry_backoff:
                 multiplier applied to the delay each further attempt —
                 retry_delay=2, retry_backoff=3 waits 2s, 6s, 18s.
    retry_on:    exception class or tuple that is retryable; anything else
                 fails the step immediately. Default: everything retries.
    continue_on_error:
                 if the step still fails after all attempts, mark it FAILED
                 in the report and return ``None`` to the caller instead of
                 raising.
    timeout:     max seconds per attempt; raises StepTimeoutError (a
                 TimeoutError subclass). The timed-out call is abandoned,
                 not killed — make such steps safe to duplicate.

    Retries, timeouts and the journal apply per CALL, so a step invoked in a
    loop gets independent attempts and its own journal entry per iteration.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            runner = getattr(args[0], "_program_runner", None) if args else None
            if runner is not None:
                return runner.call_step(args[0], func, wrapper._step_meta,
                                        args[1:], kwargs)
            # called outside a run (unit test, helper reuse): plain call
            return func(*args, **kwargs)

        wrapper._step_meta = {
            "name": name or func.__name__,
            "func_name": func.__name__,
            "retry": int(retry or 0),
            "retry_delay": float(retry_delay or 0),
            "retry_backoff": max(1.0, float(retry_backoff or 1)),
            "retry_on": _normalize_retry_on(retry_on),
            "continue_on_error": bool(continue_on_error),
            "timeout": float(timeout) if timeout else None,
            "doc": (func.__doc__ or "").strip(),
        }
        return wrapper

    return decorator
