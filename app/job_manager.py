"""Thread-safe job store with a bounded worker pool and TTL eviction.

One worker by default: sequential encodes prevent GPU thrashing, and NVENC
enforces a limit on concurrent encode sessions.
"""

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app import config
from app.handbrake_runner import HandBrakeCancelled

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


@dataclass
class Job:
    """Concurrency invariant: single-writer — only the worker thread owning
    this job mutates its fields, including ``status``; the terminal writes in
    ``_worker`` are made *without* ``JobManager._lock``. Their visibility to
    other threads rests on CPython's GIL (each attribute store is atomic and
    globally ordered) rather than on any explicit lock — a free-threaded
    (no-GIL) build would need real locking here, since a plain attribute
    store then carries no cross-thread ordering guarantee. Lock-free reads of
    all fields (by ``GET /jobs/{id}``) are safe *because* of the
    single-writer invariant; do not add a second writer without revisiting
    it. ``_claim`` and ``submit`` are the two exceptions that write ``status``
    under ``JobManager._lock`` — both do so before the job has (or, for
    submit's synthetic FAILED case, ever will have) a worker thread touching
    it, so there is no second concurrent writer at that point either.
    """

    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    output_path: str | None = None
    encoder_used: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        """Client-facing view.

        ``output_path`` is where the finished file was written. The caller
        needs it to perform the swap, and it is derived here rather than
        supplied by the caller.
        """
        return {
            "job_id": self.id,
            "status": self.status.value,
            "progress": round(self.progress, 1),
            "message": self.message,
            "error": self.error,
            "output_path": self.output_path,
            "encoder_used": self.encoder_used,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    def __init__(self, workers: int | None = None, ttl_seconds: int | None = None):
        self._workers = workers if workers is not None else config.WORKERS
        self._ttl = ttl_seconds if ttl_seconds is not None else config.JOB_TTL_SECONDS
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[Job, Callable[[Job], None]] | None]" = (
            queue.Queue()
        )
        self._threads: list[threading.Thread] = []
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(max(1, self._workers)):
            thread = threading.Thread(
                target=self._worker, name=f"encoder-worker-{i}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def shutdown(self) -> None:
        """Stop the workers, killing any HandBrake still running.

        Cancelling every non-terminal job first is load-bearing: the
        sentinels queue up *behind* pending work, the workers are daemons,
        and join(timeout) gives up long before a real encode finishes.
        Without setting the cancel events, a running encode's child process
        survives the process that spawned it and keeps writing a partial
        file nobody will collect.

        ``_running`` is cleared under ``_lock`` *before* the pending snapshot
        is taken, in the same critical section, so it cannot interleave with
        ``submit()``: either ``submit()`` sees ``_running`` still True and
        enqueues the job (in which case it is present in ``self._jobs`` and
        this snapshot will catch it), or it sees ``_running`` False and
        rejects the job itself without ever reaching the queue. There is no
        window where a job is enqueued but excluded from `pending`.
        """
        with self._lock:
            self._running = False
            pending = [j for j in self._jobs.values() if j.status not in _TERMINAL]
        for job in pending:
            job.cancel_event.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def submit(self, fn: Callable[[Job], None]) -> Job:
        """Queue *fn* to run against a fresh job, unless the manager is not
        running (never started, or already shut down).

        In the not-running case the job is marked FAILED immediately rather
        than silently enqueued: with no worker consuming the queue it would
        stay QUEUED forever, invisible to ``sweep()`` (which only evicts
        terminal jobs) and reported as perpetually queued by ``GET
        /jobs/{id}``. Returning a FAILED job keeps the caller's contract
        simple — no exception to catch — while making the condition visible.
        """
        job = Job(id=uuid.uuid4().hex)
        with self._lock:
            if not self._running:
                job.finished_at = time.time()
                job.error = "Service is shutting down; job not accepted"
                job.status = JobStatus.FAILED
                return job
            self._jobs[job.id] = job
        self._queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        """Remove a job, cancelling it first if it is still running.

        No directory cleanup is needed: this service writes a single output
        file beside the source, and the job function removes its own partial
        on cancellation or failure.
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is None:
                return False
            job.cancel_event.set()
        return True

    def sweep(self) -> int:
        """Evict finished jobs older than the TTL. Returns the number removed.

        The ``finished_at is None`` check is defensive: ``_worker`` writes
        ``finished_at`` *before* the terminal status, so any thread observing
        a terminal status already sees a populated ``finished_at``. If that
        ordering is ever changed, this guard prevents a crash — but the real
        fix would belong in ``_worker``, not here.
        """
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for job_id, job in self._jobs.items():
                if job.status not in _TERMINAL or job.finished_at is None:
                    continue
                if now - job.finished_at >= self._ttl:
                    expired.append(job_id)
            for job_id in expired:
                self._jobs.pop(job_id, None)
        return len(expired)

    def _claim(self, job: Job) -> bool:
        """Atomically decide whether this worker may run *job*.

        Serialised under the same lock ``delete()`` uses, so a job deleted
        while queued is never started.
        """
        with self._lock:
            if job.cancel_event.is_set():
                return False
            job.status = JobStatus.RUNNING
            return True

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fn = item
            if not self._claim(job):
                # finished_at before status, so any thread observing a
                # terminal status also observes a populated finished_at.
                job.finished_at = time.time()
                job.status = JobStatus.CANCELLED
                continue
            try:
                fn(job)
            except HandBrakeCancelled:
                job.finished_at = time.time()
                job.status = JobStatus.CANCELLED
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                logger.exception("Job %s failed", job.id)
                job.finished_at = time.time()
                job.error = str(exc)
                job.status = JobStatus.FAILED
            except BaseException as exc:
                # SystemExit/KeyboardInterrupt/etc. would otherwise propagate
                # out of _worker and silently kill this thread — with the
                # mandated single-worker default that stops the whole
                # service without a trace. Mark the job FAILED so it is at
                # least observable, then re-raise to preserve normal
                # interpreter-level semantics for these exceptions.
                logger.exception("Job %s failed with BaseException", job.id)
                job.finished_at = time.time()
                job.error = str(exc)
                job.status = JobStatus.FAILED
                raise
            else:
                job.finished_at = time.time()
                job.progress = 100.0
                job.status = JobStatus.COMPLETED


manager = JobManager()
