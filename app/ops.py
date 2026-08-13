"""The encode operation: validate, build argv, run, clean up on failure."""

import json
import logging
import os
import tempfile

from app import config
from app.handbrake_runner import (
    HandBrakeError,
    build_encode_command,
    run_encode,
)
from app.job_manager import Job
from app.models import EncodeRequest
from app.paths import PathNotAllowed, derive_output_path, validate_source_path
from app.presets import PresetError, find_preset, preset_encoder, preset_extension

logger = logging.getLogger(__name__)


def run_encode_job(job: Job, req: EncodeRequest) -> None:
    """Encode one file. Raises on failure; the job manager records the outcome.

    The source is validated a second time here even though the route already
    did it. Defence in depth: the op must be safe when invoked outside a
    route, and the gap between the route's check and the worker actually
    running is not zero.
    """
    src = validate_source_path(req.source_path)
    preset = find_preset(req.preset_json, req.preset_name)
    encoder = preset_encoder(preset)
    extension = preset_extension(preset)
    dst = derive_output_path(src, job.id, extension)

    if os.path.realpath(dst) == src:
        # Unreachable through the API today (job.id is a fresh uuid4), but
        # run_encode_job is explicitly callable outside a route with a
        # caller-chosen id. Were this to collide, HandBrakeCLI's -o would
        # truncate the source in place, and a subsequent failure would then
        # have _remove_partial delete it outright. Caught where the claim on
        # dst is made, before anything touches disk.
        raise PathNotAllowed("Derived output collides with the source")

    job.encoder_used = encoder
    job.output_path = dst
    job.message = "Encoding"

    # The preset document is written to a temp file because HandBrakeCLI can
    # only import a preset from a path. delete=False + explicit removal
    # rather than a context manager: on Windows a still-open NamedTemporaryFile
    # cannot be reopened by another process, and the test suite runs there.
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    )
    try:
        # close() lives in its own finally, separate from the outer one that
        # unlinks: if json.dump raises (non-serialisable document, ENOSPC),
        # the handle must still be closed here, or os.unlink below raises
        # PermissionError on Windows (an OSError, silently swallowed by the
        # existing handler) and the temp file leaks.
        try:
            json.dump(req.preset_json, handle)
        finally:
            handle.close()
        cmd = build_encode_command(src, dst, handle.name, req.preset_name)
        try:
            run_encode(
                cmd,
                on_progress=lambda pct: setattr(job, "progress", pct),
                cancel_event=job.cancel_event,
            )
        except BaseException:
            # Covers failure and cancellation alike: a partial output must
            # never be left behind for the caller to mistake for a finished
            # encode. The source is untouched either way.
            _remove_partial(dst)
            raise
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            logger.warning("Could not remove temp preset file %s", handle.name)


def _remove_partial(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.warning("Could not remove partial output %s", path)
