import threading
import time

import pytest

from app.handbrake_runner import HandBrakeCancelled
from app.job_manager import Job, JobManager, JobStatus, QueueFull


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_submit_runs_the_job_and_marks_it_completed():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        job = manager.submit(lambda j: None)
        assert _wait_for(lambda: job.status is JobStatus.COMPLETED)
        assert job.progress == 100.0
        assert job.finished_at is not None
    finally:
        manager.shutdown()


def test_a_raising_job_is_marked_failed_with_the_message():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        def boom(_job):
            raise RuntimeError("encoder exploded")

        job = manager.submit(boom)
        assert _wait_for(lambda: job.status is JobStatus.FAILED)
        assert job.error == "encoder exploded"
    finally:
        manager.shutdown()


def test_a_cancelled_job_is_marked_cancelled_not_failed():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        def cancelled(_job):
            raise HandBrakeCancelled("Cancelled")

        job = manager.submit(cancelled)
        assert _wait_for(lambda: job.status is JobStatus.CANCELLED)
        assert job.error is None
    finally:
        manager.shutdown()


def test_delete_sets_the_cancel_event_and_removes_the_job():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        started = threading.Event()

        def slow(job):
            started.set()
            job.cancel_event.wait(timeout=5)
            raise HandBrakeCancelled("Cancelled")

        job = manager.submit(slow)
        assert started.wait(timeout=5)
        assert manager.delete(job.id) is True
        assert job.cancel_event.is_set()
        assert manager.get(job.id) is None
    finally:
        manager.shutdown()


def test_delete_of_an_unknown_job_returns_false():
    manager = JobManager(workers=1, ttl_seconds=3600)
    assert manager.delete("nope") is False


def test_a_job_deleted_before_it_runs_never_executes():
    # submit() now requires the manager to be running (see the
    # submit()-after-shutdown fix below), so this can no longer submit
    # before start() to guarantee the job sits unclaimed in the queue.
    # Instead: keep the single worker busy with a blocker job, submit and
    # delete the real job while the worker can't possibly have reached it
    # yet, then release the blocker and let the worker dequeue the deleted
    # job.
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def blocker(_job):
            blocker_started.set()
            release_blocker.wait(timeout=5)

        blocker_job = manager.submit(blocker)
        assert blocker_started.wait(timeout=5)

        ran = threading.Event()
        job = manager.submit(lambda j: ran.set())
        assert manager.delete(job.id) is True

        release_blocker.set()
        assert _wait_for(lambda: blocker_job.status is JobStatus.COMPLETED)

        # Assert the positive first: this proves the worker actually
        # dequeued the deleted item and _claim rejected it, rather than
        # merely observing that a fixed sleep wasn't long enough for it to
        # run (which would pass vacuously on a loaded test runner).
        assert _wait_for(lambda: job.status is JobStatus.CANCELLED)
        assert not ran.is_set()
    finally:
        manager.shutdown()


def test_sweep_evicts_only_finished_jobs_past_the_ttl():
    manager = JobManager(workers=1, ttl_seconds=0)
    manager.start()
    try:
        job = manager.submit(lambda j: None)
        assert _wait_for(lambda: job.status is JobStatus.COMPLETED)
        assert manager.sweep() == 1
        assert manager.get(job.id) is None
    finally:
        manager.shutdown()


def test_sweep_keeps_jobs_inside_the_ttl():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        job = manager.submit(lambda j: None)
        assert _wait_for(lambda: job.status is JobStatus.COMPLETED)
        assert manager.sweep() == 0
        assert manager.get(job.id) is not None
    finally:
        manager.shutdown()


def test_shutdown_cancels_a_running_job_before_queueing_sentinels():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        started = threading.Event()

        def slow(job):
            started.set()
            job.cancel_event.wait(timeout=5)
            raise HandBrakeCancelled("Cancelled")

        job = manager.submit(slow)
        assert started.wait(timeout=5)
        manager.shutdown()
        assert job.cancel_event.is_set()
    finally:
        manager.shutdown()


def test_submit_after_shutdown_returns_a_failed_job_and_does_not_enqueue():
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    manager.shutdown()

    ran = threading.Event()
    job = manager.submit(lambda j: ran.set())

    assert job.status is JobStatus.FAILED
    assert job.error == "Service is shutting down; job not accepted"
    assert job.finished_at is not None
    # Nothing was ever started to consume the queue, so if the job had been
    # enqueued instead of rejected up front, this would still catch it.
    time.sleep(0.2)
    assert not ran.is_set()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_job_raising_base_exception_is_marked_failed():
    # BaseException (SystemExit here, standing in for
    # SystemExit/KeyboardInterrupt) is re-raised out of _worker after the
    # job is marked FAILED, per the fix's contract: interpreter-level
    # semantics are preserved, so this deliberately kills the worker thread.
    # We only assert on the job reaching FAILED, not on the thread's own
    # demise; the resulting PytestUnhandledThreadExceptionWarning is the
    # expected, intentional side effect of that re-raise and is silenced
    # here rather than treated as a test failure.
    manager = JobManager(workers=1, ttl_seconds=3600)
    manager.start()
    try:
        def boom(_job):
            raise SystemExit("bail out")

        job = manager.submit(boom)
        assert _wait_for(lambda: job.status is JobStatus.FAILED)
        assert job.error == "bail out"
    finally:
        manager.shutdown()


def test_to_dict_exposes_the_client_contract():
    job = Job(id="abc")
    job.output_path = "/media1/.hbenc-abc.mkv"
    job.encoder_used = "nvenc_h264"
    data = job.to_dict()
    assert data["job_id"] == "abc"
    assert data["status"] == "queued"
    assert data["output_path"] == "/media1/.hbenc-abc.mkv"
    assert data["encoder_used"] == "nvenc_h264"
    assert data["progress"] == 0.0


# ---- queue bound -----------------------------------------------------------
#
# Unbounded submission is a self-inflicted outage even without an attacker: a
# looping caller queues jobs faster than one worker drains them, and each one
# pins a Job in the store until the TTL sweep. Bounding the backlog turns that
# into an immediate, retryable refusal.


def test_submit_refuses_once_the_backlog_is_full():
    m = JobManager(workers=1, max_queue=2)
    m._running = True          # queue work without starting a worker to drain it
    m.submit(lambda _j: None)
    m.submit(lambda _j: None)
    with pytest.raises(QueueFull):
        m.submit(lambda _j: None)


def test_a_refused_job_is_not_stored():
    """A rejected submission must leave no trace to poll or sweep."""
    m = JobManager(workers=1, max_queue=1)
    m._running = True
    m.submit(lambda _j: None)
    before = len(m._jobs)
    with pytest.raises(QueueFull):
        m.submit(lambda _j: None)
    assert len(m._jobs) == before


def test_draining_the_queue_frees_capacity():
    """The bound is on work *waiting*, so a drained job must free its slot."""
    m = JobManager(workers=1, max_queue=1)
    m.start()
    try:
        done = threading.Event()
        m.submit(lambda _j: done.set())
        assert done.wait(timeout=5)
        for _ in range(50):
            try:
                m.submit(lambda _j: None)
                break
            except QueueFull:
                time.sleep(0.05)
        else:
            pytest.fail("capacity was never released after the job drained")
    finally:
        m.shutdown()


def test_shutdown_still_completes_with_a_full_backlog():
    """Regression: bounding the queue must not let shutdown's sentinels block
    behind pending work and hang the lifespan."""
    m = JobManager(workers=1, max_queue=2)
    m._running = True
    m.submit(lambda _j: None)
    m.submit(lambda _j: None)
    m.shutdown()   # must return, not block
