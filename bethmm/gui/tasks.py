"""Running slow work (extracting archives, deploying) off the UI thread.

Jobs run one at a time -- they share the game folder and the manifest lock --
and their results come back to the UI thread through a queued signal."""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


class TaskRunner(QObject):
    #: True while a job runs (the UI disables changes meanwhile).
    busyChanged = Signal(bool)
    _finished = Signal(int, bool, object)  # job id, ok, result or exception

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._jobs: dict[int, tuple[Callable[[Any], None], Callable[[BaseException], None]]] = {}
        self._next = 0
        self._finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    def run(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
            on_error: Callable[[BaseException], None]) -> None:
        """Run *fn* in the worker thread, then *on_done(result)* or
        *on_error(exception)* in the UI thread."""
        self._next += 1
        job_id = self._next
        self._jobs[job_id] = (on_done, on_error)
        if len(self._jobs) == 1:
            self.busyChanged.emit(True)
        self._pool.start(_Job(job_id, fn, self._finished))

    def wait(self) -> None:
        """Block until every job has finished (results are still delivered
        through the event loop)."""
        self._pool.waitForDone()

    @Slot(int, bool, object)
    def _deliver(self, job_id: int, ok: bool, result: object) -> None:
        on_done, on_error = self._jobs.pop(job_id)
        try:
            if ok:
                on_done(result)
            else:
                on_error(result)  # type: ignore[arg-type]
        finally:
            if not self._jobs:
                self.busyChanged.emit(False)


class _Job(QRunnable):
    def __init__(self, job_id: int, fn: Callable[[], Any], finished: Signal) -> None:
        super().__init__()
        self._id, self._fn, self._finished = job_id, fn, finished

    def run(self) -> None:
        try:
            result, ok = self._fn(), True
        except BaseException as exc:  # handed to the UI thread, which reports it
            log.debug("job %d failed", self._id, exc_info=True)
            result, ok = exc, False
        self._finished.emit(self._id, ok, result)
