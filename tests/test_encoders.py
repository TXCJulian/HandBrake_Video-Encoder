import pytest

from app import encoders

HELP = """\
   -Z, --preset <string>   Select preset by name
   -e, --encoder <string>  Select video encoder:
                               x264
                               x264_10bit
                               x265
                               x265_10bit
                               nvenc_h264
                               nvenc_h265
                               qsv_h264
                               vce_h264
   -q, --quality <float>   Set video quality
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    encoders.reset_cache()
    yield
    encoders.reset_cache()


def test_parses_the_indented_encoder_block():
    assert encoders.parse_encoder_list(HELP) == [
        "x264", "x264_10bit", "x265", "x265_10bit",
        "nvenc_h264", "nvenc_h265", "qsv_h264", "vce_h264",
    ]


def test_stops_at_the_next_flag():
    assert "-q" not in encoders.parse_encoder_list(HELP)
    assert "--quality" not in encoders.parse_encoder_list(HELP)


def test_returns_empty_when_the_block_is_absent():
    assert encoders.parse_encoder_list("no encoder section here") == []


def test_available_encoders_reads_from_handbrake(monkeypatch):
    monkeypatch.setattr(
        encoders.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": HELP, "stderr": ""})(),
    )
    assert "nvenc_h264" in encoders.available_encoders()


def test_available_encoders_is_empty_when_handbrake_is_missing(monkeypatch):
    def _raise(*_a, **_k):
        raise FileNotFoundError("HandBrakeCLI")

    monkeypatch.setattr(encoders.subprocess, "run", _raise)
    assert encoders.available_encoders() == []


def test_available_encoders_is_cached(monkeypatch):
    calls = {"n": 0}

    def _run(*_a, **_k):
        calls["n"] += 1
        return type("R", (), {"returncode": 0, "stdout": HELP, "stderr": ""})()

    monkeypatch.setattr(encoders.subprocess, "run", _run)
    encoders.available_encoders()
    encoders.available_encoders()
    assert calls["n"] == 1


def test_is_available_reflects_the_probed_list(monkeypatch):
    monkeypatch.setattr(
        encoders.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": HELP, "stderr": ""})(),
    )
    assert encoders.is_available("qsv_h264") is True
    assert encoders.is_available("av1_nvenc") is False
