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


def test_rejects_a_job_id_that_makes_the_output_collide_with_the_source(source, monkeypatch):
    """run_encode_job is callable outside a route with a caller-chosen job
    id. A colliding derived output would let HandBrakeCLI's -o truncate the
    source in place, so this must raise before anything touches disk rather
    than silently overwriting/deleting the source file.
    """
    from app.paths import OUTPUT_PREFIX

    # The source's own name is engineered to equal what derive_output_path
    # would produce for job id "JOBID" and the P1 preset's "mkv" extension.
    colliding_source = source.parent / f"{OUTPUT_PREFIX}JOBID.mkv"
    colliding_source.write_text("source bytes")
    monkeypatch.setattr(ops.config, "ALLOWED_ROOTS", [str(source.parent)])

    job = Job(id="JOBID")
    with pytest.raises(ops.PathNotAllowed):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(colliding_source), preset_json=PRESET_DOC, preset_name="P1"
        ))
    assert colliding_source.read_text() == "source bytes"


# ---- output-path hijacking -------------------------------------------------
#
# The output path is derived rather than accepted precisely so a caller cannot
# aim writes wherever it likes. A symlink pre-created at the derived path hands
# that primitive straight back: HandBrake's -o follows it and writes through.
# Reproduced against the real binary before this guard existed -- a 26-byte
# decoy OUTSIDE the allowed roots was overwritten with 5018 bytes of video.
#
# Two layers guard it now: the encode goes to a staging name the caller cannot
# predict, and publishing uses rename(), which replaces a symlink at the
# destination instead of following it.


def _encode_writing(content="encoded"):
    def _run(cmd, *, on_progress, cancel_event, timeout=0):
        dst = cmd[cmd.index("-o") + 1]
        with open(dst, "w") as fh:
            fh.write(content)
    return _run


def test_encode_writes_to_an_unpredictable_staging_name(source, monkeypatch):
    """The path handed to HandBrake must not be the published one: knowing the
    job id must not be enough to pre-place a symlink there."""
    seen = {}

    def _run(cmd, *, on_progress, cancel_event, timeout=0):
        seen["dst"] = cmd[cmd.index("-o") + 1]
        open(seen["dst"], "w").write("encoded")

    monkeypatch.setattr(ops, "run_encode", _run)
    job = Job(id="abc123")
    ops.run_encode_job(job, EncodeRequest(
        source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
    ))
    staged = os.path.basename(seen["dst"])
    assert staged != ".hbenc-abc123.mkv"
    assert staged.startswith(".hbenc-abc123-")   # sweep-by-prefix still holds
    assert staged.endswith(".mkv")
    # ...and the finished file lands at the published path.
    assert os.path.basename(job.output_path) == ".hbenc-abc123.mkv"
    assert open(job.output_path).read() == "encoded"


def test_a_symlink_at_the_published_path_is_replaced_not_written_through(
    source, monkeypatch
):
    """The attack Codex described, end to end."""
    victim = source.parent / "victim.txt"
    victim.write_text("PRECIOUS")
    published = source.parent / ".hbenc-abc123.mkv"
    try:
        os.symlink(str(victim), str(published))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform/account")

    monkeypatch.setattr(ops, "run_encode", _encode_writing())
    job = Job(id="abc123")
    ops.run_encode_job(job, EncodeRequest(
        source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
    ))

    assert victim.read_text() == "PRECIOUS", "symlink target must be untouched"
    assert not os.path.islink(str(published)), "publish must replace the link"
    assert published.read_text() == "encoded"


def test_a_symlink_at_the_staging_path_is_refused(source, monkeypatch):
    """Belt and braces: even if the staging name leaks, claiming it with
    O_EXCL|O_NOFOLLOW refuses a pre-existing symlink rather than following it."""
    victim = source.parent / "victim.txt"
    victim.write_text("PRECIOUS")

    monkeypatch.setattr(ops.secrets, "token_hex", lambda _n: "fixedtoken")
    staged = source.parent / ".hbenc-abc123-fixedtoken.mkv"
    try:
        os.symlink(str(victim), str(staged))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform/account")

    monkeypatch.setattr(ops, "run_encode", _encode_writing())
    job = Job(id="abc123")
    with pytest.raises(ops.PathNotAllowed):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
        ))
    assert victim.read_text() == "PRECIOUS"


def test_a_swapped_staging_file_is_detected(source, monkeypatch):
    """Closes the residual race: if the claimed file is unlinked and replaced
    mid-encode, the result must not be published as if it were ours."""
    monkeypatch.setattr(ops.secrets, "token_hex", lambda _n: "fixedtoken")
    staged = source.parent / ".hbenc-abc123-fixedtoken.mkv"

    def _swap(cmd, *, on_progress, cancel_event, timeout=0):
        os.remove(str(staged))          # attacker unlinks our claimed file
        open(str(staged), "w").write("attacker's file")

    monkeypatch.setattr(ops, "run_encode", _swap)
    job = Job(id="abc123")
    with pytest.raises(ops.PathNotAllowed):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
        ))
    assert not os.path.exists(str(source.parent / ".hbenc-abc123.mkv"))


def test_a_failed_encode_leaves_no_staging_or_published_file(source, monkeypatch):
    def fail(cmd, *, on_progress, cancel_event, timeout=0):
        open(cmd[cmd.index("-o") + 1], "w").write("partial")
        raise ops.HandBrakeError("boom")

    monkeypatch.setattr(ops, "run_encode", fail)
    job = Job(id="abc123")
    with pytest.raises(ops.HandBrakeError):
        ops.run_encode_job(job, EncodeRequest(
            source_path=str(source), preset_json=PRESET_DOC, preset_name="P1"
        ))
    leftovers = [p for p in os.listdir(str(source.parent)) if p.startswith(".hbenc-")]
    assert leftovers == []
