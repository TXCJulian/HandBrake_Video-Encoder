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

_cache: list[str] | None = None
_generation: int = 0
_lock = threading.Lock()


def reset_cache() -> None:
    """Drop the cached probe result. For tests."""
    global _cache, _generation
    with _lock:
        _cache = None
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
