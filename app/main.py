"""FastAPI application for the HandBrake encoder service."""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as _HTTPException

from app import config, encoders
from app.gpu import detect_gpu_name
from app.handbrake_runner import handbrake_info
from app.job_manager import Job, QueueFull, manager
from app.limits import BodySizeLimitMiddleware
from app.models import EncodeRequest
from app.ops import run_encode_job
from app.paths import PathNotAllowed, SourceNotFound, validate_source_path
from app.presets import (
    PresetError,
    find_preset,
    preset_encoder,
    preset_extension,
    preset_video_preset,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SWEEP_INTERVAL = 300


def _sweep_loop(stop: threading.Event) -> None:
    while not stop.wait(timeout=_SWEEP_INTERVAL):
        try:
            removed = manager.sweep()
            if removed:
                logger.info("Swept %d expired job(s)", removed)
        except Exception:
            logger.exception("Job sweep failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.ALLOWED_ROOTS:
        logger.warning(
            "ENCODER_ALLOWED_ROOTS is empty - every request will be rejected with "
            "403 path_not_allowed. /health reports 'degraded' until it is set."
        )
    manager.start()
    threading.Thread(target=encoders.available_encoders, daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=_sweep_loop, args=(stop,), daemon=True).start()
    try:
        yield
    finally:
        # Starlette throws into this generator at the yield on an abnormal
        # shutdown too, so this must be a finally, not code after a bare
        # yield: manager.shutdown()'s cancel sweep is load-bearing (it kills
        # any HandBrake child still running), and skipping it on an error
        # path would leave that child writing an uncollected partial.
        stop.set()
        manager.shutdown()


app = FastAPI(title="HandBrake Video Encoder", version="0.1.0", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.MAX_BODY_BYTES)


@app.exception_handler(_HTTPException)
async def _flatten_detail(_request: Request, exc: _HTTPException):
    """Return machine-readable error bodies at the top level.

    The renamer branches on ``code``, so it must not be buried under
    ``{"detail": {...}}``. Registered on Starlette's HTTPException, not
    FastAPI's subclass: Starlette's handler lookup walks
    ``type(exc).__mro__``, so this still catches the FastAPI subclass, but
    also catches framework-raised errors (unknown route, wrong method) that
    are plain ``starlette.exceptions.HTTPException`` instances and would
    otherwise bypass this handler entirely and escape as
    ``{"detail": "Not Found"}`` with no ``code``.
    """
    headers = getattr(exc, "headers", None)
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code, content=exc.detail, headers=headers
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": "error", "reason": str(exc.detail)},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def _flatten_validation_error(_request: Request, exc: RequestValidationError):
    """Same top-level ``code`` contract for body/query validation failures.

    FastAPI raises this separately from HTTPException for malformed request
    bodies (e.g. a missing required field), so it needs its own handler to
    avoid leaking the default ``{"detail": [...]}`` shape.
    """
    return JSONResponse(
        status_code=422,
        content={"code": "invalid_request", "reason": str(exc)},
    )


@app.get("/health")
def health() -> dict:
    """Report whether this service can actually encode, not merely respond.

    The renamer validates uploaded presets against ``encoders`` here, and
    shows this in an always-visible pill. Truthfulness matters: a service
    that reports healthy but cannot encode turns every dispatched job into
    an async failure with no prompt.
    """
    info = handbrake_info()
    available = encoders.available_encoders()
    encoder_presets = {
        encoder: encoders.encoder_presets(encoder) for encoder in available
    }
    reasons: list[str] = []
    if not info["available"]:
        reasons.append("HandBrakeCLI is not available or not runnable on PATH")
    if not available:
        reasons.append("HandBrakeCLI reported no usable encoders")
    if not config.ALLOWED_ROOTS:
        reasons.append("ENCODER_ALLOWED_ROOTS is empty; every request will be rejected")
    try:
        gpu_name = detect_gpu_name()
    except Exception:
        logger.exception("GPU detection failed; health remains available")
        gpu_name = None
    return {
        "status": "degraded" if reasons else "ok",
        "reasons": reasons,
        "handbrake_available": info["available"],
        "handbrake_version": info["version"],
        "encoders": available,
        "encoder_presets": encoder_presets,
        "gpu_name": gpu_name,
        "allowed_roots": config.ALLOWED_ROOTS,
        "workers": config.WORKERS,
    }


@app.post("/jobs", status_code=202)
def post_job(req: EncodeRequest) -> dict:
    """Queue an encode.

    Every rejection that can be decided up front is decided here, so a
    client mistake is an immediate 4xx rather than a job that must be polled
    to discover why it failed.
    """
    try:
        validate_source_path(req.source_path)
    except PathNotAllowed as exc:
        raise HTTPException(
            status_code=403, detail={"code": "path_not_allowed", "reason": str(exc)}
        ) from exc
    except SourceNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "source_not_found_on_encoder", "reason": str(exc)},
        ) from exc

    try:
        preset = find_preset(req.preset_json, req.preset_name)
        encoder = preset_encoder(preset)
        # Decidable now, same as encoder/path: an unsupported container must
        # not become an async job failure the caller has to poll to discover.
        preset_extension(preset)
    except PresetError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "preset_not_found", "reason": str(exc)}
        ) from exc

    if not encoders.is_available(encoder):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "encoder_unavailable",
                "reason": (
                    f"Preset {req.preset_name!r} needs encoder {encoder!r}, which this "
                    f"build does not provide. Available: {encoders.available_encoders()}"
                ),
                "encoder": encoder,
            },
        )

    # Speed presets are encoder-specific vocabularies: x264 takes
    # ultrafast..placebo, QSV takes speed/balanced/quality, NVENC takes
    # fastest..slowest. A preset that pairs one encoder's speed preset with
    # another's encoder is internally inconsistent and fails on ANY machine,
    # so it is a 400 rather than the 409 used for "this machine cannot".
    #
    # 400 rather than an async failure because HandBrake's own diagnosis of it
    # ("hb_qsv_param_default_preset: invalid preset 'veryfast'") only surfaces
    # once the worker runs, which the caller can reach only by polling.
    #
    # An empty list means "could not determine" -- an encoder with no presets
    # at all (theora, mpeg2) is indistinguishable from a failed probe -- and
    # must not block the encode. HandBrake still rejects a genuinely bad value
    # downstream, exactly as it did before this check existed.
    video_preset = preset_video_preset(preset)
    valid_presets = encoders.encoder_presets(encoder)
    if video_preset and valid_presets and video_preset not in valid_presets:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_video_preset",
                "reason": (
                    f"Preset {req.preset_name!r} asks for speed preset "
                    f"{video_preset!r}, which encoder {encoder!r} does not accept. "
                    f"Valid: {valid_presets}"
                ),
                "encoder": encoder,
                "video_preset": video_preset,
                "valid_presets": valid_presets,
            },
        )

    try:
        job = manager.submit(lambda j: run_encode_job(j, req))
    except QueueFull as exc:
        # Transient and retryable, unlike the other 503 below: the service is
        # healthy, just saturated. Retry-After gives the renamer something to
        # honour instead of hot-looping the endpoint that is already behind.
        raise HTTPException(
            status_code=503,
            detail={"code": "queue_full", "reason": str(exc)},
            headers={"Retry-After": "60"},
        ) from exc
    if manager.get(job.id) is None:
        # submit() marks a job FAILED without storing it when the manager is
        # not running (during/after shutdown) - that is the correct signal
        # for "never accepted". Checking job.status == FAILED instead would
        # be racy: post_job runs concurrently with the worker, which can
        # claim and fail a genuinely-stored job (source vanished, missing
        # HandBrakeCLI, ...) before this line reads job.status. Reporting
        # that as 503 would hide a real, pollable job_id from the caller,
        # and since no id was returned it could never be DELETEd either -
        # it would linger until the TTL sweep. "not stored" has no such race.
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "reason": job.error},
        )
    return {"job_id": job.id}


def _require_job(job_id: str) -> Job:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})
    return job


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return _require_job(job_id).to_dict()


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    if not manager.delete(job_id):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})
