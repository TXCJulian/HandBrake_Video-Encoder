"""Environment-driven configuration for the HandBrake encoder service."""

import os
import posixpath


def parse_roots(raw: str) -> list[str]:
    """Split a comma-separated root list into normalised absolute paths.

    Trailing separators are stripped so prefix comparison in
    ``paths.validate_source_path`` cannot be defeated by a stray slash.
    posixpath (not os.path) because these are in-container paths and the
    service only runs on Linux — os.path would normalise the same config
    string differently depending on the developer's host OS.
    """
    roots: list[str] = []
    for part in raw.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        roots.append(posixpath.normpath(cleaned))
    return roots


ALLOWED_ROOTS: list[str] = parse_roots(os.getenv("ENCODER_ALLOWED_ROOTS", ""))
HANDBRAKE_BIN: str = os.getenv("HANDBRAKE_BIN", "HandBrakeCLI")
WORKERS: int = int(os.getenv("ENCODER_WORKERS", "1"))
JOB_TTL_SECONDS: int = int(os.getenv("ENCODER_JOB_TTL", "3600"))
PORT: int = int(os.getenv("PORT", "3335"))
