"""Decorators that attach workflow metadata to plain methods.

The decorators do not change runtime behaviour of the function; they only
attach a ``_step_meta`` dict that the runner reads.
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


def _attach_meta(
    func: Callable,
    *,
    name: Optional[str],
    next: Optional[str],
    condition: Optional[str],
    loop: Optional[str],
    retry: int,
    retry_delay: float,
    retry_backoff: float,
    retry_on: ExcTypes,
    continue_on_error: bool,
    timeout: Optional[float],
    parallel: int,
    is_start: bool,
) -> Callable:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper._step_meta = {
        "name": name or func.__name__,
        "next": next,
        "condition": condition,
        "loop": loop,
        "is_wait": False,
        "retry": int(retry or 0),
        "retry_delay": float(retry_delay or 0),
        "retry_backoff": max(1.0, float(retry_backoff or 1)),
        "parallel": max(1, int(parallel or 1)),
        "retry_on": _normalize_retry_on(retry_on),
        "continue_on_error": bool(continue_on_error),
        "timeout": float(timeout) if timeout else None,
        "is_start": is_start,
        "func_name": func.__name__,
        "doc": (func.__doc__ or "").strip(),
    }
    return wrapper


def start(
    next: Optional[str] = None,
    name: Optional[str] = None,
    retry: int = 0,
    retry_delay: float = 0,
    retry_backoff: float = 1,
    retry_on: ExcTypes = None,
    continue_on_error: bool = False,
    timeout: Optional[float] = None,
):
    """Mark the entry step of a workflow.

    @start(next="Step1")
    def begin(self, ctx): ...
    """
    def decorator(func: Callable) -> Callable:
        return _attach_meta(
            func,
            name=name,
            next=next,
            condition=None,
            loop=None,
            retry=retry,
            retry_delay=retry_delay,
            retry_backoff=retry_backoff,
            retry_on=retry_on,
            continue_on_error=continue_on_error,
            timeout=timeout,
            parallel=1,
            is_start=True,
        )

    return decorator


def step(
    name: Optional[str] = None,
    next: Optional[str] = None,
    condition: Optional[str] = None,
    loop: Optional[str] = None,
    retry: int = 0,
    retry_delay: float = 0,
    retry_backoff: float = 1,
    retry_on: ExcTypes = None,
    continue_on_error: bool = False,
    timeout: Optional[float] = None,
    parallel: int = 1,
    resumable: bool = False,
):
    """Mark a workflow step.

    Parameters
    ----------
    name:        step name other steps refer to via ``next`` (defaults to the
                 function name).
    next:        name of the step to run after this one (None ends the flow).
                 A step may override this at runtime by returning
                 ``{"__next__": "OtherStep"}``.
    condition:   Python expression evaluated against the workflow context,
                 e.g. ``"a > 10"``. If it is falsy the step is SKIPPED and the
                 flow continues with ``next``.
    loop:        ``"i in items"`` — evaluates ``items`` in the context and runs
                 the step once per element with ``ctx["i"]`` bound.
    retry:       number of retries after a failure (total attempts = retry+1).
    retry_delay: seconds to sleep between attempts.
    retry_backoff:
                 multiplier applied to retry_delay for each further attempt
                 (exponential backoff). retry_delay=2, retry_backoff=3 waits
                 2s, 6s, 18s, ... Default 1 = fixed delay.
    parallel:    for loop= steps only — run up to N iterations concurrently
                 on threads. Results are still collected in input order in
                 ctx["<Step>_results"]. Each iteration sees a snapshot of the
                 context; do NOT use running accumulators
                 (ctx["total"] + n) with parallel — aggregate from
                 <Step>_results in the next step instead.
    retry_on:    exception class or tuple of classes that are retryable, e.g.
                 ``retry_on=(ConnectionError, TimeoutError)``. Any other
                 exception fails the step immediately without retrying.
                 Default None = every exception is retryable.
    continue_on_error:
                 if True and the step still fails after all attempts, the step
                 is marked FAILED but the flow continues with ``next`` instead
                 of aborting the run. The error message is stored in
                 ``ctx["<StepName>_error"]`` so later steps/conditions can
                 react to it.
    timeout:     max seconds per attempt. On expiry the attempt fails with
                 StepTimeoutError (a TimeoutError subclass, so it plays nice
                 with retry_on=TimeoutError). Note: the timed-out call cannot
                 be force-killed — it is abandoned in the background; write
                 steps so a stray duplicate finishing late is harmless.
    resumable:   for sequential loop= steps — checkpoint each completed
                 iteration, so a failed loop RESUMES AT THE FAILED ITEM
                 instead of re-running from item 1. Requires a deterministic
                 iterable (same expression → same items); not compatible
                 with parallel>1. Iteration results must be
                 JSON-serializable to restore exactly.
    """
    if resumable and parallel > 1:
        raise ValueError("resumable=True cannot be combined with parallel>1")

    def decorator(func: Callable) -> Callable:
        wrapped = _attach_meta(
            func,
            name=name,
            next=next,
            condition=condition,
            loop=loop,
            retry=retry,
            retry_delay=retry_delay,
            retry_backoff=retry_backoff,
            retry_on=retry_on,
            continue_on_error=continue_on_error,
            timeout=timeout,
            parallel=parallel,
            is_start=False,
        )
        wrapped._step_meta["resumable"] = bool(resumable)
        return wrapped

    return decorator


def wait(
    seconds: float | str,
    name: Optional[str] = None,
    next: Optional[str] = None,
    condition: Optional[str] = None,
):
    """Mark a wait step: run the function body once (optional work/logging,
    a returned dict merges into ctx as usual), then pause for ``seconds``
    before continuing to ``next``.

        @wait(seconds=30, name="Cooldown", next="Verify")
        def cooldown(self, ctx):
            self.log("letting the deploy settle")

    ``seconds`` may also be an expression evaluated against the context,
    e.g. ``seconds="delay * 2"`` or ``seconds="retry_after"``.

    The pause is cancellable from the UI, and a ``condition=`` that is
    falsy skips the step (including the pause) entirely.
    """
    def decorator(func: Callable) -> Callable:
        wrapped = _attach_meta(
            func,
            name=name,
            next=next,
            condition=condition,
            loop=None,
            retry=0,
            retry_delay=0,
            retry_backoff=1,
            retry_on=None,
            continue_on_error=False,
            timeout=None,
            parallel=1,
            is_start=False,
        )
        wrapped._step_meta.update({
            "is_wait": True,
            "wait_seconds": seconds,
        })
        return wrapped

    return decorator
