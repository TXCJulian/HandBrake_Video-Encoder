"""Path validation for network-supplied source paths.

The service receives absolute paths from a remote backend. Every one is
attacker-controlled from this process's point of view, so each is resolved
through symlinks and checked against an explicit allowlist before it can
reach ``subprocess``. Output paths are never accepted from callers; they are
derived from the source directory, the job id, and the preset's container.
"""

import os
import re

from app import config

OUTPUT_PREFIX = ".hbenc-"
"""Fixed prefix for in-progress output files.

The calling renamer sweeps orphaned partials by this prefix after a crash,
so it is a contract between the two services, not an implementation detail.
"""

_SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class PathNotAllowed(ValueError):
    """Raised when a requested path resolves outside the configured roots."""


class SourceNotFound(FileNotFoundError):
    """Raised when an allowed path does not point at an existing file."""


def _is_within(candidate: str, root: str) -> bool:
    """True when *candidate* is *root* itself or lives beneath it.

    ``os.path.commonpath`` rather than ``str.startswith`` because the latter
    accepts ``/media1x`` for the root ``/media1`` — a real escape.
    """
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        # Different drives on Windows, or one path relative and one absolute.
        return False


def validate_source_path(path: str, roots: list[str] | None = None) -> str:
    """Resolve *path* and confirm it is a readable file inside an allowed root.

    Returns the fully resolved path, which is what callers must hand to
    HandBrakeCLI — passing the unresolved original would reintroduce the
    symlink escape.
    """
    allowed = config.ALLOWED_ROOTS if roots is None else roots
    if not allowed:
        raise PathNotAllowed(
            "No allowed roots are configured; set ENCODER_ALLOWED_ROOTS."
        )

    try:
        real = os.path.realpath(path)
    except ValueError as exc:
        # An embedded NUL byte makes realpath raise ValueError. That is a
        # rejected path, not a server fault: without this it escapes as an
        # uncaught 500 instead of the 403 every other disallowed path gets.
        raise PathNotAllowed(f"Invalid source path: {path!r}") from exc

    for root in allowed:
        if _is_within(real, os.path.realpath(root)):
            break
    else:
        raise PathNotAllowed(f"Path is outside the allowed roots {allowed}: {path}")

    if not os.path.isfile(real):
        raise SourceNotFound(
            f"Source not found on encoder: {path}. Both machines must mount the "
            f"media share at identical in-container paths."
        )
    return real


def derive_output_path(source: str, job_id: str, extension: str) -> str:
    """Path for the in-progress encode: a hidden sibling of *source*.

    Never caller-supplied. ``job_id`` and ``extension`` are both checked
    against a strict token pattern rather than merely basename'd, because
    ``os.path.basename`` leaves a bare ``".."`` untouched — which would place
    the output in the parent directory.
    """
    for label, token in (("job id", job_id), ("extension", extension)):
        if not _SAFE_TOKEN.match(token):
            raise PathNotAllowed(f"Invalid {label}: {token!r}")
    return os.path.join(os.path.dirname(source), f"{OUTPUT_PREFIX}{job_id}.{extension}")
