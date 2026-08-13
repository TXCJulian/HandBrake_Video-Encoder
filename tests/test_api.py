import pytest
from fastapi.testclient import TestClient

from app import encoders, main, ops
from app.main import app

PRESET_DOC = {
    "PresetList": [
        {"PresetName": "P1", "VideoEncoder": "x264", "FileFormat": "av_mkv"}
    ]
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    media = tmp_path / "media1"
    media.mkdir()
    (media / "movie.mkv").write_text("x")
    monkeypatch.setattr(main.config, "ALLOWED_ROOTS", [str(media)])
    monkeypatch.setattr(ops.config, "ALLOWED_ROOTS", [str(media)])
    monkeypatch.setattr(encoders, "available_encoders", lambda: ["x264"])
    monkeypatch.setattr(ops, "run_encode", lambda *a, **k: None)
    main.manager.start()
    try:
        yield TestClient(app), media
    finally:
        main.manager.shutdown()


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
