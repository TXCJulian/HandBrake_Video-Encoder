#!/usr/bin/env python3
"""End-to-end encode check against a running encoder service.

Why this exists
---------------
Every test in ``tests/`` drives ``fake_handbrake`` -- a scriptable stand-in
binary -- so the whole suite can run with no GPU, no HandBrake, and no media
files. That is the right trade for unit tests, but it leaves one hole: the
progress parser, ``--preset-import-file`` handling, and the derived
``.hbenc-`` output path have only ever been checked against a fake written by
the same author as the code under test. A shared misreading of HandBrake's
real behaviour would pass all 97 tests.

This script closes that hole by running one real encode, end to end, through
the HTTP API: real HandBrakeCLI, a real preset exported from HandBrake itself
(never a hand-written stub, which would reintroduce the same blind spot), and
a real file on disk. It is deliberately small and stdlib-only so CI needs no
extra dependencies.

It exercises the software x264 path only. Hardware encoders need real GPUs and
remain a deployment check -- see the README.

Usage:
    python scripts/e2e_encode.py --media-dir /tmp/e2e --preset-file /tmp/preset.json
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

OUTPUT_PREFIX = ".hbenc-"


class E2EFailure(AssertionError):
    """A check failed. Message is written for someone reading CI logs cold."""


def _get(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    """POST *payload*, returning (status, body).

    A 4xx/5xx is returned rather than raised: the service's error bodies carry
    a machine-readable ``code`` that is far more useful in a CI log than
    urllib's bare "HTTP Error 409".
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"code": "unparseable", "reason": raw}


def wait_for_health(base_url: str, timeout: float) -> dict:
    """Block until /health answers, then return it."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return _get(f"{base_url}/health")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(1)
    raise E2EFailure(f"Service never answered /health within {timeout}s: {last!r}")


def check_health(health: dict, encoder: str) -> None:
    if not health.get("handbrake_available"):
        raise E2EFailure(f"HandBrakeCLI not available in the container: {health}")
    available = health.get("encoders") or []
    if encoder not in available:
        raise E2EFailure(
            f"Preset needs encoder {encoder!r}, which /health does not list. "
            f"Available: {available}"
        )
    if health.get("status") != "ok":
        raise E2EFailure(f"/health is not ok: {health}")


def submit(base_url: str, source_path: str, preset: dict, preset_name: str) -> str:
    status, body = _post(
        f"{base_url}/jobs",
        {
            "source_path": source_path,
            "preset_json": preset,
            "preset_name": preset_name,
        },
    )
    if status != 202:
        raise E2EFailure(f"POST /jobs returned {status}, expected 202: {body}")
    job_id = body.get("job_id")
    if not job_id:
        raise E2EFailure(f"POST /jobs 202 carried no job_id: {body}")
    return job_id


def poll(base_url: str, job_id: str, timeout: float) -> tuple[dict, list[float]]:
    """Poll to a terminal state, collecting every distinct progress value."""
    deadline = time.monotonic() + timeout
    seen: list[float] = []
    while time.monotonic() < deadline:
        job = _get(f"{base_url}/jobs/{job_id}")
        progress = job.get("progress")
        if isinstance(progress, (int, float)) and (not seen or seen[-1] != progress):
            seen.append(float(progress))
        status = job.get("status")
        if status in {"completed", "failed", "cancelled"}:
            return job, seen
        time.sleep(0.5)
    raise E2EFailure(
        f"Job {job_id} did not reach a terminal state within {timeout}s; "
        f"progress seen: {seen}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:3335")
    parser.add_argument(
        "--media-dir",
        required=True,
        help="Host directory mounted into the container as --container-root.",
    )
    parser.add_argument("--container-root", default="/media1")
    parser.add_argument("--source-name", default="sample.mp4")
    parser.add_argument("--preset-file", required=True)
    parser.add_argument("--health-timeout", type=float, default=60.0)
    parser.add_argument("--encode-timeout", type=float, default=300.0)
    args = parser.parse_args()

    preset_doc = json.loads(open(args.preset_file, encoding="utf-8").read())
    leaf = preset_doc["PresetList"][0]
    preset_name = leaf["PresetName"]
    encoder = leaf["VideoEncoder"]

    source_host = os.path.join(args.media_dir, args.source_name)
    source_container = f"{args.container_root}/{args.source_name}"
    if not os.path.exists(source_host):
        raise E2EFailure(f"Source clip missing on the host: {source_host}")
    source_before = os.stat(source_host)

    print(f"[e2e] waiting for {args.base_url}/health")
    health = wait_for_health(args.base_url, args.health_timeout)
    check_health(health, encoder)
    print(f"[e2e] health ok; HandBrake {health.get('handbrake_version')}")

    print(f"[e2e] submitting {source_container} with preset {preset_name!r}")
    job_id = submit(args.base_url, source_container, preset_doc, preset_name)
    print(f"[e2e] job {job_id}")

    job, progress_seen = poll(args.base_url, job_id, args.encode_timeout)
    print(f"[e2e] terminal status={job.get('status')} progress={progress_seen}")

    if job.get("status") != "completed":
        raise E2EFailure(
            f"Encode did not complete: status={job.get('status')} "
            f"error={job.get('error')!r}"
        )

    # --- the assertions that the fake binary cannot make honestly ------------

    output_path = job.get("output_path")
    if not output_path:
        raise E2EFailure(f"Completed job reported no output_path: {job}")

    basename = os.path.basename(output_path)
    expected_prefix = f"{OUTPUT_PREFIX}{job_id}."
    if not basename.startswith(expected_prefix):
        raise E2EFailure(
            f"Output name {basename!r} does not match the {expected_prefix!r} "
            "contract the renamer sweeps orphans by"
        )

    output_host = os.path.join(args.media_dir, basename)
    if not os.path.exists(output_host):
        raise E2EFailure(
            f"Service reported {output_path} but nothing exists at {output_host}"
        )
    size = os.path.getsize(output_host)
    if size < 1024:
        raise E2EFailure(f"Output {output_host} is implausibly small: {size} bytes")

    # Progress parsing is the single most fragile part of this service and the
    # part the fake can least honestly stand in for, so assert it actually
    # moved rather than merely that the encode finished.
    if not progress_seen:
        raise E2EFailure("No progress was ever reported; the --json parser is dead")
    if max(progress_seen) < 50.0:
        raise E2EFailure(
            f"Progress never rose above {max(progress_seen)}%, suggesting the "
            f"parser reads some but not all of the stream: {progress_seen}"
        )

    # A stated invariant of the whole design: the source is never touched.
    source_after = os.stat(source_host)
    if source_after.st_size != source_before.st_size:
        raise E2EFailure(
            f"Source file size changed: {source_before.st_size} -> "
            f"{source_after.st_size}. The service must never modify the source."
        )
    if source_after.st_mtime != source_before.st_mtime:
        raise E2EFailure(
            "Source file mtime changed. The service must never modify the source."
        )

    print(
        f"[e2e] PASS: {basename} ({size} bytes), encoder="
        f"{job.get('encoder_used')}, source untouched"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except E2EFailure as exc:
        print(f"[e2e] FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
