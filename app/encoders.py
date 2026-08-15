"""Which video encoders this HandBrakeCLI build actually supports.

This service is the sole owner of encoder availability: the calling renamer
cannot see this machine's GPU, so it validates uploaded presets against
whatever ``/health`` reports from here.
"""

import logging
import re
import subprocess
import threading

from app import config

logger = logging.getLogger(__name__)

_ENCODER_FLAG = re.compile(r"^\s*-e,\s*--encoder\b")
_INDENTED_VALUE = re.compile(r"^\s{4,}(\S+)\s*$")
_ANY_FLAG = re.compile(r"^\s*-")

_PRESET_HEADER = re.compile(
    r"^Available --encoder-preset values for '(?P<encoder>[^']*)' encoder:"
)

_cache: list[str] | None = None
_preset_cache: dict[str, list[str]] = {}
_generation: int = 0
_lock = threading.Lock()


def reset_cache() -> None:
    """Drop the cached probe results. For tests."""
    global _cache, _generation
    with _lock:
        _cache = None
        _preset_cache.clear()
        _generation += 1


def parse_encoder_list(output: str) -> list[str]:
    """Encoder names from ``HandBrakeCLI --help``.

    The list is an indented block following the ``-e, --encoder`` flag and
    ends at the next flag line. Parsed positionally rather than by matching
    known names, so a build carrying an encoder this code has never heard of
    is still reported truthfully.
    """
    names: list[str] = []
    collecting = False
    for line in output.splitlines():
        if not collecting:
            if _ENCODER_FLAG.match(line):
                collecting = True
            continue
        if _ANY_FLAG.match(line):
            break
        match = _INDENTED_VALUE.match(line)
        if match:
            names.append(match.group(1))
    return names


def available_encoders() -> list[str]:
    """Encoders this build supports, probed once and cached.

    Never raises: a missing or unrunnable HandBrakeCLI yields an empty list,
    which ``/health`` reports as degraded rather than crashing the service.
    """
    global _cache, _generation
    with _lock:
        if _cache is not None:
            return list(_cache)
        gen = _generation
    try:
        result = subprocess.run(
            [config.HANDBRAKE_BIN, "--help"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (result.stdout or "") + (result.stderr or "")
        names = parse_encoder_list(output) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not probe HandBrakeCLI encoders", exc_info=True)
        names = []
    with _lock:
        if _generation == gen:
            _cache = names
        return list(names)


def is_available(encoder: str) -> bool:
    """Whether *encoder* is supported by this build."""
    return encoder in available_encoders()


def parse_preset_list(output: str, encoder: str) -> list[str]:
    """Speed presets from ``HandBrakeCLI --encoder-preset-list <encoder>``.

    Same shape as :func:`parse_encoder_list`: a header line followed by an
    indented block, ending at the first non-indented line ("HandBrake has
    exited.").

    Returns an empty list rather than raising for every "no answer" case,
    because callers treat empty as "unknown, allow it":

    * the header names a *different* encoder than the one asked about -- an
      unknown encoder makes HandBrake print ``'(null)'`` and still exit 0, so
      the header is the only signal that the answer is not about us;
    * the encoder has no presets at all (theora, mpeg2), where the block holds
      the prose ``Option not supported by encoder``. That line is multi-word,
      so the single-token pattern skips it and does not mistake ``Option`` for
      a preset name.
    """
    names: list[str] = []
    collecting = False
    for line in output.splitlines():
        if not collecting:
            match = _PRESET_HEADER.match(line)
            if match:
                if match.group("encoder") != encoder:
                    return []
                collecting = True
            continue
        if not line.strip():
            continue
        if not line.startswith(" "):
            break
        value = _INDENTED_VALUE.match(line)
        if value:
            names.append(value.group(1))
    return names


def encoder_presets(encoder: str) -> list[str]:
    """Speed presets *encoder* accepts, probed once per encoder and cached.

    Probed per encoder because the vocabularies genuinely differ: x264 uses
    ``ultrafast..placebo``, QSV uses ``speed/balanced/quality``, NVENC uses
    ``fastest..slowest``. Assuming one global set would reject valid presets.

    Never raises. An empty list means "could not determine", which callers
    must treat as permission to proceed -- refusing an encode because a
    diagnostic probe failed would turn a cosmetic fault into an outage.
    """
    with _lock:
        cached = _preset_cache.get(encoder)
        if cached is not None:
            return list(cached)
        gen = _generation
    try:
        result = subprocess.run(
            [config.HANDBRAKE_BIN, "--encoder-preset-list", encoder],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        # The list is written to stderr, not stdout -- confirmed against the
        # real binary, whose stdout is empty for this flag. Both are joined so
        # the parser keeps working if that ever changes.
        output = (result.stdout or "") + (result.stderr or "")
        names = parse_preset_list(output, encoder) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        logger.warning(
            "Could not probe HandBrakeCLI presets for %s", encoder, exc_info=True
        )
        names = []
    with _lock:
        if _generation == gen:
            _preset_cache[encoder] = names
        return list(names)
