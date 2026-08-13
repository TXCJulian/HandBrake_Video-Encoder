import json
import os
import threading

import pytest

from app import ops
from app.job_manager import Job
from app.models import EncodeRequest

PRESET_DOC = {
    "PresetList": [
        {"PresetName": "P1", "VideoEncoder": "x264", "FileFormat": "av_mkv"}
    ]
}


@pytest.fixture
def source(tmp_path, monkeypatch):
    media = tmp_path / "media1"
    media.mkdir()
    movie = media / "movie.mkv"
    movie.write_text("source bytes")
    monkeypatch.setattr(ops.config, "ALLOWED_ROOTS", [str(media)])
    return movie


def test_writes_the_preset_to_a_temp_file_and_removes_it(source, monkeypatch):
    seen: dict = {}

    def fake_run(cmd, *, on_progress, cancel_event, timeout=0):
        preset_file = cmd[cmd.index("--preset-import-file") + 1]
        seen["path"] = preset_file
        seen["content"] = json.loads(open(preset_file).read())
        on_progress(50.0)

    monkeypatch.setattr(ops, "run_encode", fake_run)
    job = Job(id="abc123")
    ops.run_encode_job(job, EncodeRequest(
        source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
    ))
    assert seen["content"] == PRESET_DOC
    assert not os.path.exists(seen["path"]), "temp preset file must be cleaned up"
    assert job.progress == 50.0


def test_sets_output_path_and_encoder_used(source, monkeypatch):
    monkeypatch.setattr(ops, "run_encode", lambda *a, **k: None)
    job = Job(id="abc123")
    ops.run_encode_job(job, EncodeRequest(
        source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
    ))
    assert os.path.basename(job.output_path) == ".hbenc-abc123.mkv"
    assert job.encoder_used == "x264"


def test_removes_the_partial_output_when_the_encode_fails(source, monkeypatch):
    def fail(cmd, *, on_progress, cancel_event, timeout=0):
        dst = cmd[cmd.index("-o") + 1]
        open(dst, "w").write("partial")
        raise ops.HandBrakeError("boom")

    monkeypatch.setattr(ops, "run_encode", fail)
    job = Job(id="abc123")
    with pytest.raises(ops.HandBrakeError):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
        ))
    assert not os.path.exists(os.path.join(os.path.dirname(str(source)), ".hbenc-abc123.mkv"))


def test_never_touches_the_source_file(source, monkeypatch):
    monkeypatch.setattr(ops, "run_encode", lambda *a, **k: None)
    job = Job(id="abc123")
    ops.run_encode_job(job, EncodeRequest(
        source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
    ))
    assert source.read_text() == "source bytes"


def test_rejects_an_unknown_preset_name(source):
    job = Job(id="abc123")
    with pytest.raises(ops.PresetError):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(source), preset_json=PRESET_DOC, preset_name="Nope"
        ))
