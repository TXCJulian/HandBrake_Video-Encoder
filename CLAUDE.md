# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

Standalone HandBrakeCLI encoding service for Jellyfin_Media-Renamer. FastAPI
plus a thread-based job queue. Clients POST a source path and a HandBrake
preset document, poll for progress, and read back the output path.

## Commands

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -v
uvicorn app.main:app --host 0.0.0.0 --port 3335 --reload
docker compose up -d --build
```

## Architecture

- `app/paths.py` — **security boundary.** Every network-supplied path goes
  through `validate_source_path` before reaching subprocess. Output paths are
  derived, never caller-supplied (`derive_output_path`, prefix `OUTPUT_PREFIX
  = ".hbenc-"`).
- `app/presets.py` — pure HandBrake preset-document parsing. No I/O.
- `app/encoders.py` — encoder probing. This service is the sole owner of
  encoder availability; the renamer cannot see this machine's GPU.
- `app/handbrake_runner.py` — the one HandBrakeCLI driver. Do not add a second.
- `app/ops.py` — the encode operation: validate, build argv, run, clean up.
- `app/job_manager.py` — job store, worker pool, TTL eviction.
- `app/main.py` — FastAPI routes: `GET /health`, `POST /jobs`, `GET
  /jobs/{id}`, `DELETE /jobs/{id}`. Six error codes total, listed in
  `README.md`; note in particular `503 service_unavailable`, returned by
  `POST /jobs` when the job manager is not running and the job was therefore
  never stored (no `job_id` is returned in that case, so there is nothing to
  poll or delete).

## Conventions

- Port 3335. 3334 belongs to Whisper_Lyric-Transcriber.
- No endpoint accepts HandBrake or ffmpeg arguments. `EncodeRequest`
  (`app/models.py`) is exactly `{source_path, preset_json, preset_name}` —
  no output path field exists anywhere in the request model.
- Command-building functions are pure and tested by asserting on argv.
- The source file is never modified or deleted. Only `.hbenc-*` siblings are
  written, and a partial one is removed on failure or cancellation.
- The `.hbenc-` prefix is a contract with the renamer, which sweeps orphaned
  partials by it. Do not change it on one side only.
- Tests must never require a GPU, HandBrake, or media files.
- Single image for all GPU vendors (NVENC + QSV + VCE together), unlike the
  sibling Whisper_Lyric-Transcriber, which needs one image per vendor because
  those ML stacks are mutually incompatible. Here, one HandBrakeCLI binary
  carries all three vendors' support — confirmed by the build spike in
  `docs/build-spike-findings.md`.
- `HandBrakeCLI --help` reports only encoders available at runtime, not what
  was compiled in. Never use `--help` output to verify a build; a build check
  and a deployment check are different things (use `strings` on the binary to
  check compile-time support instead).
- **Build and runtime Docker stages must stay on the same distro release**
  (`Dockerfile` currently pins both to `ubuntu:24.04`). The compiled
  HandBrakeCLI binary requires `GLIBC_2.38` and `libvpx.so.9`; a Debian
  bookworm-based runtime image only has GLIBC 2.36 and `libvpx.so.7`, and the
  binary will not exec there. If a future maintainer "modernises" the runtime
  base to a slim/different-release image without also rebuilding on that
  release, the container is broken until someone notices this note.
