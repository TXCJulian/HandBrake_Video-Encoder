"""The encode operation: validate, build argv, run, clean up on failure."""

import json
import logging
import os
import secrets
import tempfile

from app import config
from app.handbrake_runner import (
    HandBrakeError,
    build_encode_command,
    run_encode,
)
from app.job_manager import Job
from app.models import EncodeRequest
from app.paths import (
    PathNotAllowed,
    derive_output_path,
    derive_staging_path,
    validate_source_path,
)
from app.presets import PresetError, find_preset, preset_encoder, preset_extension

logger = logging.getLogger(__name__)

_CLOSED = -1


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

    # Encode to a name the caller cannot predict, then publish by rename.
    #
    # dst is derivable by anyone holding the job id, which the caller receives
    # in the 202. A symlink pre-created there is followed by HandBrakeCLI's -o,
    # writing the encode through it to an arbitrary target -- outside the
    # allowed roots included. Verified against the real binary: a decoy file
    # went from 26 bytes to 5018 bytes of video. The realpath(dst) == src check
    # above only covers the case where that target is the source itself.
    #
    # The staging token is fresh randomness that never leaves this process, so
    # the path actually handed to HandBrake cannot be pre-empted. os.replace()
    # then swaps the finished file into place; rename replaces a symlink at the
    # destination instead of writing through it.
    staging = derive_staging_path(src, job.id, extension, secrets.token_hex(8))

    # O_EXCL|O_NOFOLLOW so the claim fails rather than follows if anything at
    # all already sits there -- belt and braces, in case the staging name ever
    # becomes guessable.
    #
    # The descriptor is deliberately held open for the whole encode rather than
    # closed here. It is the anchor for the tamper check below: an inode with
    # an open descriptor cannot be freed, so if the path is unlinked and
    # replaced, the new file is guaranteed a different inode. Comparing a
    # stat() taken before and after instead would miss the swap outright --
    # Linux readily reuses a just-freed inode number for the next file created
    # in the same directory, which is exactly what a replacement is.
    try:
        fd = os.open(
            staging,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
    except OSError as exc:
        raise PathNotAllowed(
            f"Could not claim the staging output path: {exc.strerror}"
        ) from exc

    job.encoder_used = encoder
    # The path the finished file will occupy. It does not exist until the
    # encode completes and is published -- callers must key off the job's
    # terminal status, not the presence of this file.
    job.output_path = dst
    job.message = "Encoding"

    # The preset document is written to a temp file because HandBrakeCLI can
    # only import a preset from a path. delete=False + explicit removal
    # rather than a context manager: on Windows a still-open NamedTemporaryFile
    # cannot be reopened by another process, and the test suite runs there.
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    )
    try:  # noqa: PLR1702 - the claimed fd must outlive the encode; see os.close below
        # close() lives in its own finally, separate from the outer one that
        # unlinks: if json.dump raises (non-serialisable document, ENOSPC),
        # the handle must still be closed here, or os.unlink below raises
        # PermissionError on Windows (an OSError, silently swallowed by the
        # existing handler) and the temp file leaks.
        try:
            json.dump(req.preset_json, handle)
        finally:
            handle.close()
        cmd = build_encode_command(src, staging, handle.name, req.preset_name)
        try:
            run_encode(
                cmd,
                on_progress=lambda pct: setattr(job, "progress", pct),
                cancel_event=job.cancel_event,
            )
            # samestat against the still-open descriptor, not against a stat
            # captured earlier: holding the descriptor keeps the original inode
            # alive, so a file that replaced ours cannot have inherited its
            # inode number.
            if not os.path.samestat(os.fstat(fd), os.stat(staging)):
                # The claimed file was unlinked and replaced while HandBrake
                # ran. Whatever is there now is not what this job produced, so
                # it must not be published under the job's output path.
                raise PathNotAllowed(
                    "Staging output was replaced during the encode; refusing to publish"
                )
            # Released only now: the check above needed it, and Windows -- where
            # the test suite runs, though the service itself does not -- refuses
            # to rename a file that still has an open descriptor.
            os.close(fd)
            fd = _CLOSED
            os.replace(staging, dst)
        except BaseException:
            # Covers failure and cancellation alike: a partial output must
            # never be left behind for the caller to mistake for a finished
            # encode. The source is untouched either way.
            #
            # The claim is dropped first: Windows refuses to unlink a file that
            # still has an open descriptor, which would turn a clean failure
            # into a leaked staging file.
            if fd != _CLOSED:
                os.close(fd)
                fd = _CLOSED
            _remove_partial(staging)
            raise
    finally:
        # Already released on the success path, just before the rename.
        if fd != _CLOSED:
            os.close(fd)
        try:
            os.unlink(handle.name)
        except OSError:
            logger.warning("Could not remove temp preset file %s", handle.name)


def _remove_partial(path: str) -> None:
    # lexists, not isfile: isfile() follows symlinks, so a link to a
    # directory or a dangling link would be silently left behind. os.remove
    # does not follow, so a link is unlinked rather than its target touched.
    try:
        if os.path.lexists(path):
            os.remove(path)
    except OSError:
        logger.warning("Could not remove partial output %s", path)
