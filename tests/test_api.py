import os
import time

import pytest
from fastapi.testclient import TestClient

from app import encoders, main, ops
from app.main import app

PRESET_DOC = {
    "PresetList": [
        {"PresetName": "P1", "VideoEncoder": "x264", "FileFormat": "av_mkv"}
    ]
}

_TERMINAL = ("completed", "failed", "cancelled")


def _poll_until_terminal(c, job_id, attempts=50, interval=0.02):
    body = None
    for _ in range(attempts):
        body = c.get(f"/jobs/{job_id}").json()
        if body["status"] in _TERMINAL:
            break
        time.sleep(interval)
    return body


@pytest.fixture
def client(tmp_path, monkeypatch):
    media = tmp_path / "media1"
    media.mkdir()
    (media / "movie.mkv").write_text("x")
    monkeypatch.setattr(main.config, "ALLOWED_ROOTS", [str(media)])
    monkeypatch.setattr(ops.config, "ALLOWED_ROOTS", [str(media)])
    monkeypatch.setattr(encoders, "available_encoders", lambda: ["x264"])
    monkeypatch.setattr(ops, "run_encode", lambda *a, **k: None)
    # Uses the real lifespan (manager.start()/shutdown(), the encoder
    # warm-up thread, the sweep loop) rather than hand-rolling
    # manager.start()/shutdown() around a bare TestClient(app): the latter
    # never runs the lifespan at all, since TestClient only triggers it as
    # a context manager.
    with TestClient(app) as c:
        yield c, media


def test_health_reports_ok_with_roots_and_encoders(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(
        main, "handbrake_info", lambda: {"available": True, "version": "1.9.2", "path": "/x"}
    )
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["encoders"] == ["x264"]
    assert body["handbrake_version"] == "1.9.2"


def test_health_is_degraded_without_handbrake(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(
        main, "handbrake_info", lambda: {"available": False, "version": "", "path": ""}
    )
    body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert any("HandBrake" in r for r in body["reasons"])


def test_post_jobs_returns_202_with_a_job_id(client):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    assert r.status_code == 202
    assert "job_id" in r.json()


def test_post_jobs_rejects_a_path_outside_the_roots(client, tmp_path):
    c, _ = client
    outside = tmp_path / "elsewhere.mkv"
    outside.write_text("x")
    r = c.post("/jobs", json={
        "source_path": str(outside), "preset_json": PRESET_DOC, "preset_name": "P1",
    })
    assert r.status_code == 403
    assert r.json()["code"] == "path_not_allowed"


def test_post_jobs_reports_a_missing_source_distinctly(client, tmp_path, monkeypatch):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "absent.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    assert r.status_code == 404
    assert r.json()["code"] == "source_not_found_on_encoder"


def test_post_jobs_rejects_an_unknown_preset_name(client):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "Nope",
    })
    assert r.status_code == 400
    assert r.json()["code"] == "preset_not_found"


def test_post_jobs_rejects_an_unavailable_encoder(client, monkeypatch):
    c, media = client
    monkeypatch.setattr(encoders, "available_encoders", lambda: ["x265"])
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    assert r.status_code == 409
    assert r.json()["code"] == "encoder_unavailable"
    assert "x264" in r.json()["reason"]


def test_get_unknown_job_is_404(client):
    c, _ = client
    assert c.get("/jobs/nope").status_code == 404
    assert c.get("/jobs/nope").json()["code"] == "job_not_found"


def test_delete_unknown_job_is_404(client):
    c, _ = client
    assert c.delete("/jobs/nope").status_code == 404


def test_post_jobs_returns_503_when_the_manager_is_not_running(client):
    """manager.submit() marks a job FAILED without storing it when the
    manager is not running (during/after shutdown). The route must surface
    this synchronously as a 503 rather than returning a job_id that
    GET /jobs/{id} would immediately 404 on.
    """
    c, media = client
    main.manager.shutdown()
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    assert r.status_code == 503
    assert r.json()["code"] == "service_unavailable"


def test_post_jobs_rejects_an_unsupported_container(client):
    """FileFormat outside {av_mkv, av_mp4, av_webm} is decidable at the
    route via preset_extension, same as encoder/path. It must not become an
    async job failure the caller has to poll to discover.
    """
    c, media = client
    preset = {
        "PresetList": [
            {"PresetName": "P1", "VideoEncoder": "x264", "FileFormat": "av_avi"}
        ]
    }
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": preset,
        "preset_name": "P1",
    })
    assert r.status_code == 400
    assert r.json()["code"] == "preset_not_found"


def test_post_jobs_returns_202_for_a_job_that_fails_immediately(client, monkeypatch):
    """A worker claiming and failing a genuinely-stored job before the route
    reads job.status must still surface as a normal 202 with a pollable id,
    not the 503 reserved for jobs the manager never accepted.
    """
    c, media = client

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "run_encode", boom)
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    body = _poll_until_terminal(c, job_id)
    assert body["status"] == "failed"
    assert body["job_id"] == job_id


def test_unknown_route_returns_a_top_level_code(client):
    """Framework-raised 404s (plain starlette.exceptions.HTTPException, not
    routed through any handler in main.py) must still carry a top-level
    ``code`` - the calling service branches on it unconditionally.
    """
    c, _ = client
    r = c.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert "code" in r.json()


def test_post_jobs_with_a_malformed_body_returns_422_invalid_request(client):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        # preset_name omitted
    })
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_request"


def test_get_returns_the_job_with_a_derived_output_path(client):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    job_id = r.json()["job_id"]

    body = _poll_until_terminal(c, job_id)
    assert body["job_id"] == job_id
    assert body["status"] == "completed"
    output_path = body["output_path"]
    assert output_path is not None
    # Derived, not caller-supplied: a hidden sibling of the source, never
    # e.g. the source path itself or anything else the request carried.
    assert os.path.basename(output_path).startswith(".hbenc-")
    assert os.path.dirname(output_path) == str(media)


def test_delete_removes_an_existing_job(client):
    c, media = client
    r = c.post("/jobs", json={
        "source_path": str(media / "movie.mkv"),
        "preset_json": PRESET_DOC,
        "preset_name": "P1",
    })
    job_id = r.json()["job_id"]

    assert c.delete(f"/jobs/{job_id}").status_code == 204
    assert c.get(f"/jobs/{job_id}").status_code == 404
