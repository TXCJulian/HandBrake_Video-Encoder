import threading
import time

from app.handbrake_runner import HandBrakeCancelled
from app.job_manager import Job, JobManager, JobStatus


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
    manager = JobManager(workers=1, ttl_seconds=3600)
    ran = threading.Event()
    job = manager.submit(lambda j: ran.set())
    manager.delete(job.id)
    manager.start()
    try:
        time.sleep(0.3)
        assert not ran.is_set()
        assert job.status is JobStatus.CANCELLED
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
