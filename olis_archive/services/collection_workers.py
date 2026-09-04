"""Single-run dispatcher whose queue contains only durable run IDs."""

from __future__ import annotations

import logging
import queue
import threading
import time

from .collection import CollectionService


LOGGER = logging.getLogger(__name__)


class CollectionWorkerManager:
    """Dispatch one durable run at a time.

    Each run remains subject to its configured OData, HTML, and download limits;
    dependency-ordered work may use fewer workers.  Keeping the outer dispatcher
    singular prevents separately queued runs from multiplying those limits or
    racing one another for the same document state.
    """

    def __init__(self, service: CollectionService) -> None:
        self.service = service
        self.worker_count = 1
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._pending: set[int] = set()
        self._active: set[int] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stopping = False

    def start(self, *, enqueue_existing: bool = True) -> None:
        with self._lock:
            if self._started:
                return
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._threads:
                raise RuntimeError("collection workers have not finished stopping")
            self._stopping = False
            self._started = True
            for number in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"legiview-collector-{number + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
        if enqueue_existing:
            with self.service.database.connection() as connection:
                ids = [
                    int(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM collection_runs WHERE status='queued' ORDER BY id"
                    ).fetchall()
                ]
            for run_id in ids:
                self.enqueue(run_id)

    def enqueue(self, run_id: int) -> bool:
        with self._lock:
            if self._stopping:
                return False
            if run_id in self._pending:
                return False
            self._pending.add(run_id)
        self._queue.put(run_id)
        return True

    def stop(self, *, wait: bool = True, timeout: float | None = 30.0) -> bool:
        """Stop accepting work and quiesce workers without running queued jobs.

        Waiting run IDs remain durable ``queued`` rows and will be discovered by
        the next process. Active runs are normalized to ``interrupted`` so their
        download callbacks stop and the user can explicitly resume them.

        Returns ``True`` only when every worker thread has exited. A caller must
        not release the process instance lock after a ``False`` result.
        """

        with self._lock:
            threads = [thread for thread in self._threads if thread.is_alive()]
            self._started = False
            self._stopping = True
            active = set(self._active)

        if active:
            # This process owns the mutation lock, so every running row belongs
            # to this worker set. Queued, paused, and canceled rows are untouched.
            self.service.storage.normalize_interrupted_work()

        # Do not begin queued work merely because the UI or process is stopping.
        # Its durable row remains queued for startup discovery next time.
        while True:
            try:
                queued_run_id = self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                if queued_run_id is not None:
                    with self._lock:
                        if queued_run_id not in self._active:
                            self._pending.discard(queued_run_id)
                self._queue.task_done()

        for _ in threads:
            self._queue.put(None)
        if wait:
            deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
            for thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(timeout=remaining)

        alive = [thread for thread in threads if thread.is_alive()]
        with self._lock:
            self._threads = alive
        if not alive:
            # Remove surplus sentinels left by repeated stop calls so this
            # manager can be restarted deliberately in tests or future wiring.
            while True:
                try:
                    queued_run_id = self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    if queued_run_id is not None:
                        with self._lock:
                            self._pending.discard(queued_run_id)
                    self._queue.task_done()
        return not alive

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "workers": len(self._threads),
                "queued": len(self._pending - self._active),
                "active": len(self._active),
                "alive": sum(thread.is_alive() for thread in self._threads),
            }

    def _worker(self) -> None:
        while True:
            run_id = self._queue.get()
            if run_id is None:
                self._queue.task_done()
                return
            try:
                with self._lock:
                    should_run = not self._stopping
                    if should_run:
                        # Claim while holding the same gate used by stop().  Once
                        # stop observes an active ID, its durable row is already
                        # running and startup normalization can interrupt it;
                        # otherwise stop wins and this queued run is never begun.
                        should_run = self.service.runs.claim_run(run_id)
                    if should_run:
                        self._active.add(run_id)
                    else:
                        self._pending.discard(run_id)
                if should_run:
                    self.service.execute_run(run_id, _already_claimed=True)
            except BaseException:
                # execute_run normally persists exceptions itself; this guard keeps
                # a daemon alive even if the guardrail itself encounters a bug.
                LOGGER.exception("Unexpected collection worker failure for run %s", run_id)
            finally:
                with self._lock:
                    self._active.discard(run_id)
                    self._pending.discard(run_id)
                self._queue.task_done()


__all__ = ["CollectionWorkerManager"]
