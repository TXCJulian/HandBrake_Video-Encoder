from pathlib import Path

import threading

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


def test_reset_cache_invalidates_inflight_probe(monkeypatch):
    """Verify that reset_cache during an inflight probe discards the result."""
    calls = {"n": 0}
    event = threading.Event()

    def _blocking_run(*_a, **_k):
        calls["n"] += 1
        event.wait(timeout=5)  # Block until event is set
        return type("R", (), {"returncode": 0, "stdout": HELP, "stderr": ""})()

    monkeypatch.setattr(encoders.subprocess, "run", _blocking_run)

    # Start a probe in a background thread
    result_holder = {}

    def _probe_in_thread():
        result_holder["result"] = encoders.available_encoders()

    thread = threading.Thread(target=_probe_in_thread)
    thread.start()

    # Give the thread time to start its probe
    import time

    time.sleep(0.1)

    # While the probe is in-flight, reset the cache
    encoders.reset_cache()

    # Release the blocking probe
    event.set()
    thread.join(timeout=5)

    # The inflight probe should have run but NOT populated the cache
    assert calls["n"] == 1

    # The next call should perform a NEW probe (cache was not populated)
    event.clear()
    thread2 = threading.Thread(target=_probe_in_thread)
    thread2.start()
    time.sleep(0.1)
    event.set()
    thread2.join(timeout=5)

    # Should have called subprocess twice now
    assert calls["n"] == 2


# ---- encoder speed presets (--encoder-preset-list) -------------------------
#
# Parsed from output captured verbatim from the real binary rather than a
# hand-typed sample: the whole point of this check is to know what HandBrake
# actually accepts, so an approximation written by the same author as the
# parser would defeat it.

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / f"encoder-preset-list-{name}.txt").read_text(encoding="utf-8")


def test_parses_the_qsv_preset_block():
    assert encoders.parse_preset_list(_fixture("qsv_h265"), "qsv_h265") == [
        "speed", "balanced", "quality",
    ]


def test_parses_the_x264_preset_block():
    presets = encoders.parse_preset_list(_fixture("x264"), "x264")
    assert presets[0] == "ultrafast"
    assert presets[-1] == "placebo"
    assert "veryfast" in presets
    # The banner and the trailing "HandBrake has exited." must not leak in.
    assert "HandBrake" not in presets
    assert all(" " not in p for p in presets)


def test_encoders_have_different_preset_vocabularies():
    """The reason this is probed per encoder rather than assumed global."""
    qsv = encoders.parse_preset_list(_fixture("qsv_h265"), "qsv_h265")
    x264 = encoders.parse_preset_list(_fixture("x264"), "x264")
    assert "veryfast" in x264 and "veryfast" not in qsv
    assert "balanced" in qsv and "balanced" not in x264


def test_an_encoder_without_presets_yields_empty():
    """theora reports 'Option not supported by encoder', not a preset list.

    Must come back empty so the caller fails open, rather than being read as
    a single valid preset literally named "Option".
    """
    assert encoders.parse_preset_list(_fixture("theora"), "theora") == []


def test_an_unknown_encoder_yields_empty():
    """HandBrake exits 0 with a '(null)' header, so absence is the only signal."""
    assert encoders.parse_preset_list(_fixture("unknown"), "nonsense_enc") == []


def test_a_block_for_a_different_encoder_is_not_attributed():
    """Guards against reading one encoder's presets as another's."""
    assert encoders.parse_preset_list(_fixture("qsv_h265"), "x264") == []


def test_encoder_presets_probes_and_caches(monkeypatch):
    calls = {"n": 0}

    def _run(*_a, **_k):
        calls["n"] += 1
        return type("R", (), {
            "returncode": 0, "stdout": "", "stderr": _fixture("qsv_h265"),
        })()

    monkeypatch.setattr(encoders.subprocess, "run", _run)
    assert encoders.encoder_presets("qsv_h265") == ["speed", "balanced", "quality"]
    assert encoders.encoder_presets("qsv_h265") == ["speed", "balanced", "quality"]
    assert calls["n"] == 1


def test_encoder_presets_reads_stderr():
    """The list goes to stderr, not stdout -- verified against the real binary."""
    assert "Available --encoder-preset values" in _fixture("qsv_h265")


def test_encoder_presets_caches_per_encoder(monkeypatch):
    seen: list[str] = []

    def _run(cmd, *_a, **_k):
        encoder = cmd[-1]
        seen.append(encoder)
        return type("R", (), {
            "returncode": 0, "stdout": "", "stderr": _fixture(encoder),
        })()

    monkeypatch.setattr(encoders.subprocess, "run", _run)
    assert "balanced" in encoders.encoder_presets("qsv_h265")
    assert "veryfast" in encoders.encoder_presets("x264")
    assert seen == ["qsv_h265", "x264"]


def test_encoder_presets_is_empty_when_handbrake_is_missing(monkeypatch):
    def _raise(*_a, **_k):
        raise FileNotFoundError("HandBrakeCLI")

    monkeypatch.setattr(encoders.subprocess, "run", _raise)
    assert encoders.encoder_presets("qsv_h265") == []


def test_reset_cache_clears_preset_cache(monkeypatch):
    calls = {"n": 0}

    def _run(*_a, **_k):
        calls["n"] += 1
        return type("R", (), {
            "returncode": 0, "stdout": "", "stderr": _fixture("qsv_h265"),
        })()

    monkeypatch.setattr(encoders.subprocess, "run", _run)
    encoders.encoder_presets("qsv_h265")
    encoders.reset_cache()
    encoders.encoder_presets("qsv_h265")
    assert calls["n"] == 2
