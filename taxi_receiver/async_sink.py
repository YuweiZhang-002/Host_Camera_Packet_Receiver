"""Bounded asynchronous dispatcher for expensive Layer-5 output callbacks."""
from __future__ import annotations

from dataclasses import dataclass
import queue
import sys
import threading
import time
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
_STOP = object()


@dataclass
class AsyncCallbackStatistics:
    submitted: int = 0
    processed: int = 0
    failures: int = 0
    queue_peak: int = 0
    # How long the producer sat blocked in submit() because the bounded queue
    # was full.  queue_peak == capacity only proves the queue filled once; this
    # is the number that says whether the sink is the throughput ceiling.
    submit_blocked_seconds: float = 0.0
    submit_blocked_count: int = 0
    consecutive_failures: int = 0
    disabled: bool = False
    dropped_after_disable: int = 0


class AsyncCallbackDispatcher(Generic[T]):
    """Run one callback on a dedicated worker with bounded buffering.

    ``submit`` blocks only when the bounded output queue is genuinely full.
    It never silently drops a completed frame.  This keeps slow atomic disk
    publication out of the packet parsing/reassembly worker while retaining an
    explicit memory limit and an observable queue high-water mark.
    """

    def __init__(
        self,
        callback: Callable[[T], None],
        *,
        queue_depth: int = 256,
        name: str = "taxi-frame-output",
        error_sink: Callable[[str], None] | None = None,
        max_consecutive_failures: int = 50,
        error_report_limit: int = 20,
    ) -> None:
        if queue_depth <= 0:
            raise ValueError("queue_depth must be positive")
        if max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be positive")
        self.callback = callback
        self.queue_depth = queue_depth
        self.name = name
        self.max_consecutive_failures = max_consecutive_failures
        self.error_report_limit = error_report_limit
        self._reported_errors = 0
        self.error_sink = error_sink or (
            lambda message: print(message, file=sys.stderr)
        )
        self.stats = AsyncCallbackStatistics()
        self._queue: queue.Queue[T | object] = queue.Queue(
            maxsize=queue_depth
        )
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._worker.start()

    def submit(self, item: T) -> None:
        if self._closed:
            raise RuntimeError("async callback dispatcher is closed")
        if self.stats.disabled:
            self.stats.dropped_after_disable += 1
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            started = time.monotonic()
            self._queue.put(item)
            self.stats.submit_blocked_seconds += time.monotonic() - started
            self.stats.submit_blocked_count += 1
        self.stats.submitted += 1
        self.stats.queue_peak = max(
            self.stats.queue_peak,
            self._queue.qsize(),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._queue.join()
        self._queue.put(_STOP)
        self._worker.join(timeout=30.0)
        if self._worker.is_alive():
            raise RuntimeError("async callback worker did not stop")
        self._closed = True

    def report_lines(self) -> tuple[str, ...]:
        return (
            "FRAME OUTPUT QUEUE",
            f"  queue capacity      : {self.queue_depth}",
            f"  queue peak          : {self.stats.queue_peak}",
            f"  frames submitted    : {self.stats.submitted}",
            f"  frames processed    : {self.stats.processed}",
            f"  callback failures   : {self.stats.failures}",
            f"  sink disabled       : {int(self.stats.disabled)}",
            f"  dropped after disable: {self.stats.dropped_after_disable}",
            f"  submit blocked      : "
            f"{self.stats.submit_blocked_seconds:.3f} s "
            f"over {self.stats.submit_blocked_count} waits",
        )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                try:
                    self.callback(item)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001 - isolate sink failure
                    self.stats.failures += 1
                    self.stats.consecutive_failures += 1
                    # 2767 identical tracebacks on stderr is not evidence, it is
                    # a second bottleneck: console writes are synchronous.
                    if self._reported_errors < self.error_report_limit:
                        self._reported_errors += 1
                        self.error_sink(f"[FRAME OUTPUT ERROR] {exc}")
                    if (
                        not self.stats.disabled
                        and self.stats.consecutive_failures
                        >= self.max_consecutive_failures
                    ):
                        self.stats.disabled = True
                        self.error_sink(
                            f"[FRAME OUTPUT DISABLED] {self.name}: "
                            f"{self.stats.consecutive_failures} consecutive "
                            "failures; no further frames will be submitted. "
                            "Fix the sink and rerun."
                        )
                else:
                    self.stats.processed += 1
                    self.stats.consecutive_failures = 0
            finally:
                self._queue.task_done()
