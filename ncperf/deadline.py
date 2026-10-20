"""Wall-clock budgets for a search.

A socket timeout starts over whenever bytes arrive, so a slow reply can sit
for minutes. A budget here does not. Search code checks it, or reads the
response through read_response. A parent budget can only make a nested one
shorter. Rendering a file has no budget, so its own timeout still applies.
"""
from __future__ import annotations

import contextlib
import threading
import time
import urllib.request

_local = threading.local()


class SearchTimeout(TimeoutError):
    """The search budget ran out. Callers skip that search and continue."""


class Budget:
    def __init__(self, deadline_at: float):
        self.deadline = deadline_at

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise SearchTimeout("search timed out")


def _stack() -> list[Budget]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def current() -> Budget | None:
    stack = getattr(_local, "stack", None)
    if not stack:
        return None
    return stack[-1]


def check() -> None:
    budget = current()
    if budget is not None:
        budget.check()


@contextlib.contextmanager
def limit(seconds: float):
    """Bound the following work. Nested limits keep the earlier deadline."""
    parent = current()
    deadline_at = time.monotonic() + max(0.0, float(seconds))
    if parent is not None:
        deadline_at = min(deadline_at, parent.deadline)
    budget = Budget(deadline_at)
    stack = _stack()
    stack.append(budget)
    try:
        yield budget
    finally:
        if stack and stack[-1] is budget:
            stack.pop()
        else:
            try:
                stack.remove(budget)
            except ValueError:
                pass


def socket_timeout(timeout: float) -> float:
    """The timeout to hand urllib. A live budget can only shorten it."""
    budget = current()
    if budget is None:
        return float(timeout)
    budget.check()
    return min(float(timeout), max(0.0, budget.remaining()))


def _arm_socket(resp, seconds: float) -> None:
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None) if fp is not None else None
    sock = getattr(raw, "_sock", None) or getattr(resp, "_sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(max(0.05, seconds))
    except Exception:
        pass


def read_response(resp) -> bytes:
    """Read a body. With a budget, stop at the deadline even if bytes trickle in."""
    budget = current()
    if budget is None:
        return resp.read()
    chunks = []
    while True:
        budget.check()
        _arm_socket(resp, budget.remaining())
        block = resp.read(65536)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def read_url(url: str, timeout: float, headers: dict) -> bytes:
    """GET url, bounded by timeout seconds of wall clock (or a tighter parent)."""
    with limit(timeout):
        wait = socket_timeout(timeout)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=wait) as resp:
            return read_response(resp)
