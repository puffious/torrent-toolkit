"""Background jobs keyed by torrent hash, polled from the tick thread."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class Jobs:
    """At most one job per key; failed jobs can only be resubmitted after `retry_after`."""

    def __init__(
        self,
        name: str,
        retry_after: float,
        executor: ThreadPoolExecutor | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._futures: dict[str, Future] = {}
        self._retry_at: dict[str, float] = {}
        self.retry_after = retry_after
        self.clock = clock

    def running(self, key: str) -> bool:
        return key in self._futures

    def submit(self, key: str, fn: Callable[..., Any], *args: Any) -> bool:
        """Start a job unless one is running or a failure is still cooling down."""
        if key in self._futures or self.clock() < self._retry_at.get(key, 0):
            return False
        self._futures[key] = self._executor.submit(fn, *args)
        return True

    def collect(self, key: str) -> tuple[bool, Any, BaseException | None]:
        """`(finished, result, error)`; a finished job is forgotten."""
        future = self._futures.get(key)
        if future is None or not future.done():
            return False, None, None
        del self._futures[key]
        error = future.exception()
        if error is not None:
            self._retry_at[key] = self.clock() + self.retry_after
            return True, None, error
        self._retry_at.pop(key, None)
        return True, future.result(), None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
