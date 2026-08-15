"""The one HandBrakeCLI driver. Do not add a second.

Carries the same three hard-won behaviours as the sibling ffmpeg runner in
Media_Transcoder, for the same reasons:

* stderr goes to an unbounded temp file rather than a pipe, so a chatty
  process cannot block on a full pipe buffer and stall the stdout stream it
  shares a write path with;
* a daemon watcher thread kills the process the moment the cancel event
  fires, so a stalled encode emitting no further progress is still
  cancellable;
* a cancel arriving *after* a clean exit is a no-op, because the output is
  complete and a killed process never exits 0 — treating it as a
  cancellation would discard a finished encode.
"""

import functools
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Callable

from app import config

logger = logging.getLogger(__name__)

_CANCEL_GRACE_SECONDS = 0.2
"""Grace window after cancel fires before the process is killed.

Narrows the clean-exit race rather than eliminating it: on heavily loaded
hardware a process could still be killed inside its own teardown if that
teardown outlasts this window. A practical mitigation, not a guarantee.
"""

_MAX_BUFFER_BYTES = 1 << 20
"""Cap on the accumulated stdout buffer in run_encode, in bytes (1 MiB).

Guards against a stray unmatched ``{`` in HandBrake's stdout noise: with no
matching close, ``parse_progress_objects`` never sees depth return to zero,
so no object ever completes again and the buffer would otherwise grow for
the rest of a multi-hour encode — silently losing all further progress
reports and leaking memory in a long-lived service. When the cap is hit the
buffer is dropped and accumulation restarts from the next line.
"""


class HandBrakeCancelled(RuntimeError):
    """Raised when the encode was cancelled before it finished."""


class HandBrakeError(RuntimeError):
    """Raised when HandBrakeCLI exited non-zero."""


def build_encode_command(
    src: str, dst: str, preset_file: str, preset_name: str
) -> list[str]:
    """Argv for a preset-driven encode.

    Pure and argv-only: every value is a separate list element, so a preset
    name is never re-parsed as a flag no matter what it contains. This is
    the whole reason the service can accept a preset *name* from the network
    while accepting no HandBrake arguments at all.
    """
    return [
        *_handbrake_cmd(),
        "--json",
        "--preset-import-file",
        preset_file,
        "--preset",
        preset_name,
        "-i",
        src,
        "-o",
        dst,
    ]


def _handbrake_cmd() -> list[str]:
    return [config.HANDBRAKE_BIN]


def parse_progress_objects(chunk: str) -> list[dict]:
    """Every complete JSON object in *chunk*, ignoring surrounding noise.

    HandBrake pretty-prints its ``--json`` state objects across multiple
    lines, so a line-by-line ``json.loads`` cannot work. Braces are counted
    to find object boundaries, with an in-string flag so a ``}`` inside a
    string value does not close the object early. Malformed objects are
    skipped rather than raised on: progress reporting must never be able to
    fail an otherwise healthy encode.
    """
    objects: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(chunk):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(chunk[start : index + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(parsed, dict):
                        objects.append(parsed)
                start = -1
    return objects


def _watch_cancel(proc: "subprocess.Popen", cancel_event: threading.Event) -> None:
    """Kill *proc* once *cancel_event* fires, unless it finishes on its own.

    A cancel can race a process already exiting cleanly. Rather than killing
    on the spot, this grants a brief grace window — Popen.wait() is safe to
    call concurrently with the main thread's wait, CPython serialises reaping
    internally — and only kills if the process is still alive afterwards.
    """
    while proc.poll() is None:
        if cancel_event.wait(timeout=0.25):
            try:
                proc.wait(timeout=_CANCEL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                if proc.poll() is None:
                    logger.info("Cancel requested; killing HandBrake pid=%s", proc.pid)
                    proc.kill()
            return


def run_encode(
    cmd: list[str],
    *,
    on_progress: Callable[[float], None],
    cancel_event: threading.Event,
    timeout: float = 14400,
) -> None:
    """Run *cmd*, reporting 0-100 progress and honouring cancellation.

    HandBrake reports its own fractional progress, so unlike the ffmpeg
    runner no source duration is needed. The default timeout is four hours:
    a 4K CPU x265 encode of a feature film legitimately takes hours.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(
            cmd,
            # Nothing writes to HandBrake's stdin; leaving it inherited risks
            # an interactive prompt (e.g. an overwrite confirmation) blocking
            # the child forever until the timeout kills it. DEVNULL makes any
            # such prompt fail fast instead.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errf,
            text=True,
            bufsize=1,
            # Stdout is decoded leniently, matching the stderr temp file: a
            # media path or encoder banner can carry non-UTF-8 bytes, and the
            # stream nobody parses byte-exactly must not be stricter than the
            # one that's explicitly best-effort.
            errors="replace",
        )
        watcher = threading.Thread(
            target=_watch_cancel, args=(proc, cancel_event), daemon=True
        )
        watcher.start()

        buffer = ""
        returncode: int | None = None
        completed = False
        try:
            for line in proc.stdout or []:
                buffer += line
                if "}" not in line:
                    continue
                objects = parse_progress_objects(buffer)
                # Clear ONLY once at least one complete object was parsed.
                # HandBrake pretty-prints, so the line closing a nested object
                # (`    }` ending "Working") contains a brace while the outer
                # object is still open. Clearing there would discard the
                # accumulated text and the object would never parse at all —
                # progress would silently never fire. Confirmed against real
                # --json output in docs/spike/json-output.txt.
                if not objects:
                    if len(buffer) > _MAX_BUFFER_BYTES:
                        logger.warning(
                            "HandBrake stdout buffer exceeded %d bytes with no "
                            "complete object (likely an unmatched '{'); "
                            "dropping buffer to recover",
                            len(buffer),
                        )
                        buffer = ""
                    continue
                for obj in objects:
                    working = obj.get("Working")
                    if not isinstance(working, dict):
                        continue
                    fraction = working.get("Progress")
                    if isinstance(fraction, (int, float)):
                        # A caller-supplied callback must never be able to
                        # fail an otherwise healthy encode — the same
                        # guarantee parse_progress_objects makes for
                        # malformed JSON extends to what consumes its result.
                        try:
                            on_progress(min(float(fraction) * 100.0, 100.0))
                        except Exception:
                            logger.warning(
                                "on_progress callback raised; ignoring",
                                exc_info=True,
                            )
                buffer = ""
            completed = True
        finally:
            if not completed and proc.poll() is None:
                # The stdout loop aborted (e.g. on_progress or the line
                # iterator itself raised) with the process still running and
                # nobody left to drain stdout. Taking the polite `proc.wait
                # (timeout=timeout)` branch below would let the child block
                # on a full pipe buffer for up to `timeout`, and a
                # subsequent TimeoutExpired would then replace the real
                # exception with a bogus timeout error from inside this same
                # `finally`. Kill immediately and reap with a short bounded
                # wait instead, so the original exception propagates.
                proc.kill()
                proc.wait(timeout=10)
            elif proc.poll() is None:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "HandBrake pid=%s exceeded timeout=%ss; killing",
                        proc.pid,
                        timeout,
                    )
                    proc.kill()
                    proc.wait(timeout=10)
                    raise HandBrakeError(f"HandBrake timed out after {timeout}s")
            returncode = proc.wait()
            watcher.join(timeout=1.0)

        if returncode == 0:
            return

        if cancel_event.is_set():
            raise HandBrakeCancelled("Cancelled")

        errf.seek(0)
        stderr = errf.read().strip()
        raise HandBrakeError(stderr or f"HandBrake exited with code {returncode}")


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


@functools.lru_cache(maxsize=1)
def handbrake_info() -> dict:
    """Describe the HandBrakeCLI binary: whether it runs, and its version.

    Cached for the process lifetime — the binary cannot change under a
    running container. Never raises: being on PATH is not the same as being
    runnable (a wrong-architecture binary resolves fine then fails to exec),
    so an unrunnable binary is reported as ``available: False``.
    """
    path = shutil.which(config.HANDBRAKE_BIN)
    if path is None:
        return {"available": False, "version": "", "path": ""}
    try:
        result = subprocess.run(
            [config.HANDBRAKE_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("Could not run HandBrakeCLI --version", exc_info=True)
        return {"available": False, "version": "", "path": path}
    if result.returncode != 0:
        return {"available": False, "version": "", "path": path}

    banner = result.stdout or result.stderr or ""
    match = _VERSION_RE.search(banner)
    return {
        "available": True,
        "version": match.group(1) if match else "",
        "path": path,
    }
