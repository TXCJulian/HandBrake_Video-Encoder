import threading

import pytest

from app.handbrake_runner import (
    HandBrakeCancelled,
    HandBrakeError,
    build_encode_command,
    parse_progress_objects,
    run_encode,
)


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
    at all. Captured verbatim from HandBrake 1.9.2 (docs/spike/json-output.txt).
    """
    # Built as a single repr()'d literal (rather than a nested triple-quoted
    # string) so every line of the *generated script* stays uniformly
    # indented. A raw multi-line literal here would put unindented JSON
    # lines (they start at column 0, same as the real HandBrake output) in
    # the same block as the indented `import sys`/`sys.exit(0)` lines,
    # which defeats textwrap.dedent's common-prefix stripping in the
    # fixture and raises IndentationError when the script is written out.
    output = (
        'Version: {\n'
        '    "Arch": "x86_64",\n'
        '    "Name": "HandBrake",\n'
        '    "VersionString": "1.9.2"\n'
        '}\n'
        'Progress: {\n'
        '    "State": "WORKING",\n'
        '    "Working": {\n'
        '        "ETASeconds": 0,\n'
        '        "Pass": 1,\n'
        '        "Progress": 0.98000001907348633,\n'
        '        "SequenceID": 1\n'
        '    }\n'
        '}\n'
        'Progress: {\n'
        '    "State": "WORKDONE",\n'
        '    "WorkDone": {\n'
        '        "Error": 0,\n'
        '        "SequenceID": 1\n'
        '    }\n'
        '}\n'
    )
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
