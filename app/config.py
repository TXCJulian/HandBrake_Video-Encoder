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

MAX_QUEUE: int = int(os.getenv("ENCODER_MAX_QUEUE", "100"))
"""How many jobs may be waiting for a worker before submissions are refused.

Bounds the backlog, not the store: running jobs are already limited by
``WORKERS``, and finished ones are cleared by the TTL sweep. Without it a
caller looping faster than a single worker drains — a bug, not necessarily an
attack — queues jobs indefinitely, each pinning a Job in memory until its TTL
expires. Refusing with 503 immediately is far easier to diagnose than a service
that accepts everything and falls further behind.
"""

MAX_BODY_BYTES: int = int(os.getenv("ENCODER_MAX_BODY_BYTES", "1000000"))
"""Largest accepted request body.

``preset_json`` is an unbounded dict otherwise, parsed into memory before any
route check runs. Real HandBrake preset documents are a few KB — a full export
of every built-in preset is well under 100KB — so 1MB is generous.
"""
