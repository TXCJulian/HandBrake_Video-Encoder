"""FastAPI application for the HandBrake encoder service."""

import logging

from fastapi import FastAPI

from app import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HandBrake Video Encoder", version="0.1.0")


@app.get("/health")
def health() -> dict:
    reasons: list[str] = []
    if not config.ALLOWED_ROOTS:
        reasons.append("ENCODER_ALLOWED_ROOTS is empty; every request will be rejected")
    return {
        "status": "degraded" if reasons else "ok",
        "reasons": reasons,
        "allowed_roots": config.ALLOWED_ROOTS,
        "workers": config.WORKERS,
    }
