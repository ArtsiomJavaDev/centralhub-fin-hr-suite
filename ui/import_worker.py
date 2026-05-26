"""Background worker that runs a single import on a QThread.

UI thread owns the progress dialog and the Cancel button. When the user
cancels, the main thread flips the CancelToken; the worker sees it on the
next row boundary and raises ImportCancelled, which causes the surrounding
SQL transaction to roll back (see db.service).

Signals emitted back to the UI thread:
- progress(done: int, total: int)
- finished(stats: object)   — stats is the per-profile ImportStats dataclass
- failed(message: str)      — generic error message
- cancelled()               — user cancelled, nothing was written
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from database import CancelToken, ImportCancelled


class ImportWorker(QObject):
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, job: Callable[[Callable[[int, int], None], CancelToken], Any]) -> None:
        """Create a worker bound to a callable.

        The job receives two callables:
          - progress_cb(done, total)
          - cancel_token
        and must return the stats object produced by the corresponding
        db_service.execute_* method. Any exception inside the job is turned
        into a `failed` signal (ImportCancelled becomes `cancelled`).
        """
        super().__init__()
        self._job = job
        self._cancel_token = CancelToken()

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel_token

    def request_cancel(self) -> None:
        self._cancel_token.cancel()

    def _report_progress(self, done: int, total: int) -> None:
        # Signals are queued to the UI thread automatically.
        self.progress.emit(int(done), int(total))

    def run(self) -> None:
        try:
            stats = self._job(self._report_progress, self._cancel_token)
        except ImportCancelled:
            self.cancelled.emit()
        except Exception as exc:
            tb = traceback.format_exc()
            self.failed.emit(f"{exc}\n\n{tb}")
        else:
            self.finished.emit(stats)


def run_in_thread(
    parent: QObject,
    job: Callable[[Callable[[int, int], None], CancelToken], Any],
) -> tuple[QThread, ImportWorker]:
    """Spin up a QThread + ImportWorker, wire thread lifecycle, start both.

    Caller must connect progress/finished/failed/cancelled signals BEFORE
    returning control to the event loop. The thread auto-quits after the
    worker finishes and is scheduled for deletion.
    """
    thread = QThread(parent)
    worker = ImportWorker(job)
    worker.moveToThread(thread)

    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    return thread, worker
