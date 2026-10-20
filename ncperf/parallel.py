"""Run independent searches and note fetches at the same time.

Callers merge results in the original order, so a take does not depend on
which reply arrived first. Each worker keeps the caller's context, so Stop
still raises Cancelled.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from .cancel import Cancelled


def run_all(jobs, workers: int = 8):
    """Run zero-arg callables and return their results in input order.

    Cancelled is raised on the caller. Any other exception from a job is
    raised too, once the jobs already running have finished.
    """
    jobs = list(jobs)
    if not jobs:
        return []
    if len(jobs) == 1:
        return [jobs[0]()]
    # Copy on this thread, once per job. A Context cannot be entered by two
    # threads, and a worker's own context would not see Stop.
    contexts = [contextvars.copy_context() for _ in jobs]

    def run(ctx, job):
        return ctx.run(job)

    width = max(1, min(int(workers), len(jobs)))
    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="nc-par") as pool:
        futs = [pool.submit(run, ctx, job) for ctx, job in zip(contexts, jobs)]
        out = []
        try:
            for fut in futs:
                out.append(fut.result())
        except Cancelled:
            raise
        return out


def submit(fn, *args):
    """Start fn(*args) on one worker that still sees Cancelled.

    Returns the pool and the future. The caller shuts the pool down.
    """
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nc-bg")
    return pool, pool.submit(ctx.run, fn, *args)
