import threading
import time
from pathlib import Path

import pytest

from app import handbrake_runner
from app.handbrake_runner import (
    HandBrakeCancelled,
    HandBrakeError,
    build_encode_command,
    handbrake_info,
    parse_progress_objects,
    run_encode,
)

_SPIKE_JSON = Path(__file__).resolve().parents[1] / "docs" / "spike" / "json-output.txt"
_SPIKE_VERSION = Path(__file__).resolve().parents[1] / "docs" / "spike" / "version.txt"


# ---- argv building (pure) -------------------------------------------------

def test_build_encode_command_uses_preset_import_and_name():
    cmd = build_encode_command(
        "/media1/in.mkv", "/media1/.hbenc-abc.mkv", "/tmp/p.json", "My Preset"
    )
    assert "--preset-import-file" in cmd
    assert cmd[cmd.index("--preset-import-file") + 1] == "/tmp/p.json"
    assert cmd[cmd.index("--preset") + 1] == "My Preset"
    assert cmd[cmd.index("-i") + 1] == "/media1/in.mkv"
    assert cmd[cmd.index("-o") + 1] == "/media1/.hbenc-abc.mkv"
    assert "--json" in cmd


def test_build_encode_command_accepts_no_free_form_arguments():
    """A preset name containing a flag must stay one argv element."""
    cmd = build_encode_command("/a/in.mkv", "/a/out.mkv", "/tmp/p.json", "--evil x")
    assert cmd[cmd.index("--preset") + 1] == "--evil x"
    assert cmd.count("--evil x") == 1


# ---- progress parsing (pure) ---------------------------------------------

def test_parses_a_single_line_progress_object():
    chunk = 'Progress: {"State": "WORKING", "Working": {"Progress": 0.25}}\n'
    assert parse_progress_objects(chunk) == [
        {"State": "WORKING", "Working": {"Progress": 0.25}}
    ]


def test_parses_a_pretty_printed_multi_line_object():
    chunk = (
        "Progress: {\n"
        '  "State": "WORKING",\n'
        '  "Working": {\n'
        '    "Progress": 0.5,\n'
        '    "ETASeconds": 120\n'
        "  }\n"
        "}\n"
    )
    assert parse_progress_objects(chunk) == [
        {"State": "WORKING", "Working": {"Progress": 0.5, "ETASeconds": 120}}
    ]


def test_parses_several_objects_from_one_chunk():
    chunk = (
        'Progress: {"State": "WORKING", "Working": {"Progress": 0.1}}\n'
        'Progress: {"State": "MUXING"}\n'
    )
    assert [o["State"] for o in parse_progress_objects(chunk)] == ["WORKING", "MUXING"]


def test_ignores_noise_between_objects():
    chunk = 'random banner text\nProgress: {"State": "MUXING"}\nmore noise\n'
    assert parse_progress_objects(chunk) == [{"State": "MUXING"}]


def test_ignores_malformed_json_rather_than_raising():
    assert parse_progress_objects("Progress: {not json}\n") == []


def test_braces_inside_strings_do_not_break_accumulation():
    chunk = 'Progress: {"State": "WORKING", "Note": "a } brace"}\n'
    assert parse_progress_objects(chunk) == [
        {"State": "WORKING", "Note": "a } brace"}
    ]


# ---- running --------------------------------------------------------------

def test_parses_real_handbrake_output_verbatim(fake_handbrake):
    """Regression: the real --json stream is pretty-printed, and the line
    closing the nested "Working" object contains a brace while the outer
    object is still open. An implementation that clears its buffer on any
    brace-bearing line never completes the object and reports no progress
    at all. This reads docs/spike/json-output.txt directly (rather than
    embedding a hand-trimmed copy) so the test stays honestly "verbatim",
    including the leading Version record's own nested "Version": {...}
    object — a second brace-nesting level outside any Progress record,
    which is exactly the kind of construct this regression must survive.
    """
    output = _SPIKE_JSON.read_text(encoding="utf-8")
    # Built as a single repr()'d literal (rather than a nested triple-quoted
    # string) so every line of the *generated script* stays uniformly
    # indented. A raw multi-line literal here would put unindented JSON
    # lines (they start at column 0, same as the real HandBrake output) in
    # the same block as the indented `import sys`/`sys.exit(0)` lines,
    # which defeats textwrap.dedent's common-prefix stripping in the
    # fixture and raises IndentationError when the script is written out.
    exe = fake_handbrake(
        f'''
        import sys
        sys.stdout.write({output!r})
        sys.stdout.flush()
        sys.exit(0)
        '''
    )
    seen: list[float] = []
    run_encode(exe, on_progress=seen.append, cancel_event=threading.Event())
    # Exactly one progress report: the Version object has no "Working" key,
    # and WORKDONE carries "WorkDone" (different key), so neither reports.
    assert seen == [pytest.approx(98.000001907348633)]


def test_reports_progress_scaled_to_percent(fake_handbrake):
    exe = fake_handbrake(
        """
        import sys, time
        for p in (0.2, 0.5, 1.0):
            print('Progress: {"State": "WORKING", "Working": {"Progress": %s}}' % p, flush=True)
            time.sleep(0.01)
        sys.exit(0)
        """
    )
    seen: list[float] = []
    run_encode(exe, on_progress=seen.append, cancel_event=threading.Event())
    assert seen[0] == pytest.approx(20.0)
    assert seen[-1] == pytest.approx(100.0)


def test_progress_never_exceeds_one_hundred(fake_handbrake):
    exe = fake_handbrake(
        """
        import sys
        print('Progress: {"State": "WORKING", "Working": {"Progress": 1.4}}', flush=True)
        sys.exit(0)
        """
    )
    seen: list[float] = []
    run_encode(exe, on_progress=seen.append, cancel_event=threading.Event())
    assert seen == [100.0]


def test_nonzero_exit_raises_with_stderr_detail(fake_handbrake):
    exe = fake_handbrake(
        """
        import sys
        sys.stderr.write("Invalid preset name\\n")
        sys.exit(3)
        """
    )
    with pytest.raises(HandBrakeError, match="Invalid preset name"):
        run_encode(exe, on_progress=lambda _p: None, cancel_event=threading.Event())


def test_nonzero_exit_without_stderr_reports_the_code(fake_handbrake):
    exe = fake_handbrake("import sys; sys.exit(7)")
    with pytest.raises(HandBrakeError, match="7"):
        run_encode(exe, on_progress=lambda _p: None, cancel_event=threading.Event())


def test_cancel_kills_a_stalled_process(fake_handbrake):
    exe = fake_handbrake("import time; time.sleep(60)")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(HandBrakeCancelled):
        run_encode(exe, on_progress=lambda _p: None, cancel_event=cancel)


def test_cancel_after_clean_exit_is_not_a_cancellation(fake_handbrake):
    """A cancel racing a successful finish must not discard a complete result."""
    exe = fake_handbrake(
        """
        import sys
        print('Progress: {"State": "WORKDONE", "Working": {"Progress": 1.0}}', flush=True)
        sys.exit(0)
        """
    )
    cancel = threading.Event()
    run_encode(exe, on_progress=lambda _p: cancel.set(), cancel_event=cancel)


def test_large_stderr_does_not_deadlock(fake_handbrake):
    """Stderr goes to a temp file, so HandBrake cannot block on a full pipe."""
    exe = fake_handbrake(
        """
        import sys
        sys.stderr.write("noise\\n" * 200_000)
        sys.exit(0)
        """
    )
    run_encode(exe, on_progress=lambda _p: None, cancel_event=threading.Event())


# ---- stdout buffer cap (recovery from an unmatched brace) -----------------

def test_stray_unmatched_brace_recovers_after_buffer_cap(fake_handbrake, monkeypatch):
    """A single unmatched '{' pins parse_progress_objects' depth above zero
    forever, so no later object can ever complete against that buffer. The
    cap must drop the poisoned buffer once it grows past the limit so a
    subsequent, well-formed object still gets parsed and reported.
    """
    monkeypatch.setattr(handbrake_runner, "_MAX_BUFFER_BYTES", 64)
    exe = fake_handbrake(
        """
        import sys
        sys.stdout.write("noise { unmatched\\n")
        sys.stdout.write("padding padding padding padding padding padding}\\n" * 5)
        print('Progress: {"State": "WORKING", "Working": {"Progress": 0.5}}', flush=True)
        sys.exit(0)
        """
    )
    seen: list[float] = []
    run_encode(exe, on_progress=seen.append, cancel_event=threading.Event())
    assert seen == [pytest.approx(50.0)]


def test_buffer_cap_triggers_and_logs_a_warning(fake_handbrake, monkeypatch, caplog):
    """The buffer must not grow unbounded for the rest of a multi-hour
    encode just because one stray '{' appeared in stdout noise: the cap has
    to trigger (and be observable, via a warning) well before that.
    """
    monkeypatch.setattr(handbrake_runner, "_MAX_BUFFER_BYTES", 64)
    exe = fake_handbrake(
        """
        import sys
        sys.stdout.write("noise { unmatched\\n")
        sys.stdout.write("padding padding padding padding padding padding}\\n" * 5)
        sys.exit(0)
        """
    )
    with caplog.at_level("WARNING", logger="app.handbrake_runner"):
        run_encode(exe, on_progress=lambda _p: None, cancel_event=threading.Event())
    assert any(
        "buffer exceeded" in record.getMessage() for record in caplog.records
    )


# ---- handbrake_info ---------------------------------------------------------

def test_handbrake_info_reports_unavailable_when_binary_is_absent(monkeypatch):
    handbrake_info.cache_clear()
    monkeypatch.setattr(handbrake_runner.shutil, "which", lambda _name: None)
    info = handbrake_info()
    assert info == {"available": False, "version": "", "path": ""}


def test_handbrake_info_reports_unavailable_on_nonzero_exit(monkeypatch):
    handbrake_info.cache_clear()
    monkeypatch.setattr(handbrake_runner.shutil, "which", lambda _name: "/usr/bin/HandBrakeCLI")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(handbrake_runner.subprocess, "run", lambda *a, **kw: _Result())
    info = handbrake_info()
    assert info["available"] is False
    assert info["path"] == "/usr/bin/HandBrakeCLI"


def test_handbrake_info_reports_unavailable_when_run_raises_oserror(monkeypatch):
    handbrake_info.cache_clear()
    monkeypatch.setattr(handbrake_runner.shutil, "which", lambda _name: "/usr/bin/HandBrakeCLI")

    def _raise(*_a, **_kw):
        raise OSError("wrong architecture")

    monkeypatch.setattr(handbrake_runner.subprocess, "run", _raise)
    info = handbrake_info()
    assert info == {"available": False, "version": "", "path": "/usr/bin/HandBrakeCLI"}


def test_handbrake_info_parses_the_version_from_a_realistic_banner(monkeypatch):
    """Uses the real banner shape captured in docs/spike/version.txt, whose
    last line is "HandBrake 1.9.2" — HandBrakeCLI writes its startup log
    (including this line) to stderr, not stdout.
    """
    handbrake_info.cache_clear()
    banner = _SPIKE_VERSION.read_text(encoding="utf-8")
    monkeypatch.setattr(handbrake_runner.shutil, "which", lambda _name: "/usr/bin/HandBrakeCLI")

    class _Result:
        returncode = 0
        stdout = ""
        stderr = banner

    monkeypatch.setattr(handbrake_runner.subprocess, "run", lambda *a, **kw: _Result())
    info = handbrake_info()
    assert info == {
        "available": True,
        "version": "1.9.2",
        "path": "/usr/bin/HandBrakeCLI",
    }


# ---- C-1 regression coverage: exception escaping the stdout loop ----------

def test_exception_in_loop_kills_process_and_preserves_original_error(
    fake_handbrake, monkeypatch
):
    """Pins C-1(a): if anything inside the stdout loop raises (here forced
    via a broken parse_progress_objects, since the guarded on_progress from
    C-1(c) can no longer be the trigger), the process still running must be
    killed immediately rather than politely waited on — and the ORIGINAL
    exception must propagate unmasked. Before the fix, the still-running
    process took the long `proc.wait(timeout=timeout)` branch in `finally`,
    blocked on its full stdout pipe, and a subsequent TimeoutExpired there
    raised a bogus HandBrakeError that replaced the real exception.
    """
    monkeypatch.setattr(
        handbrake_runner,
        "parse_progress_objects",
        lambda _chunk: (_ for _ in ()).throw(RuntimeError("parser exploded")),
    )
    exe = fake_handbrake(
        """
        import sys, time
        print('Progress: {"State": "WORKING", "Working": {"Progress": 0.1}}', flush=True)
        time.sleep(60)
        """
    )
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="parser exploded"):
        run_encode(
            exe,
            on_progress=lambda _p: None,
            cancel_event=threading.Event(),
            timeout=5,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"expected a fast kill, took {elapsed}s (close to the 5s timeout)"


def test_invalid_utf8_on_stdout_does_not_abort_the_encode(fake_handbrake):
    """Pins C-1(b): stdout is decoded with errors="replace", matching the
    stderr temp file's leniency, so undecodable bytes on stdout (a media
    path or encoder banner can carry them) do not raise out of the read
    loop and abort an otherwise healthy encode.
    """
    exe = fake_handbrake(
        r"""
        import sys
        sys.stdout.buffer.write(b"\xff\xfe invalid \xc3\x28\n")
        sys.stdout.buffer.flush()
        print('Progress: {"State": "WORKING", "Working": {"Progress": 0.3}}', flush=True)
        sys.exit(0)
        """
    )
    seen: list[float] = []
    run_encode(exe, on_progress=seen.append, cancel_event=threading.Event())
    assert seen == [pytest.approx(30.0)]


def test_raising_on_progress_does_not_fail_a_healthy_encode(fake_handbrake, caplog):
    """Pins C-1(c): a raising on_progress callback must not fail an
    otherwise healthy encode — the same guarantee parse_progress_objects'
    docstring already makes for malformed JSON extends to what consumes its
    parsed result.
    """
    exe = fake_handbrake(
        """
        import sys
        print('Progress: {"State": "WORKING", "Working": {"Progress": 0.4}}', flush=True)
        print('Progress: {"State": "WORKING", "Working": {"Progress": 0.8}}', flush=True)
        sys.exit(0)
        """
    )

    def _raise(_p):
        raise RuntimeError("progress consumer exploded")

    with caplog.at_level("WARNING", logger="app.handbrake_runner"):
        run_encode(exe, on_progress=_raise, cancel_event=threading.Event())
    assert any(
        "on_progress" in record.getMessage() for record in caplog.records
    )
