# HandBrake Video Encoder

Standalone HandBrakeCLI encoding service for
[Jellyfin_Media-Renamer](https://github.com/TXCJulian/Media-Helper). Runs on a
machine with a real GPU so the renamer backend does not have to encode video
itself.

## Why this is a separate service

An NVENC-capable HandBrakeCLI has to be built from source — distro packages
ship without it, because NVIDIA's codec SDK is proprietary. That is a large
image and a slow build, and most deployments of the renamer (episode renaming,
music tagging) have no use for it. Keeping it separate means those deployments
never build or pull it, and the encoding runs on whichever machine actually has
the hardware.

Unlike the sibling
[Whisper_Lyric-Transcriber](https://github.com/TXCJulian/Whisper_Lyric-Transcriber)
service, this is a **single image for every GPU vendor**. A build spike
(`docs/build-spike-findings.md`) confirmed that one HandBrakeCLI binary
carries NVENC, QSV, and VCE support simultaneously — there is no per-vendor
ML-stack incompatibility to work around here, so there is no need for
per-vendor images.

## The one deployment rule

**Both containers must mount the media share at identical in-container paths.**

The renamer sends absolute paths and this service uses them verbatim. If the
renamer sees a file at `/media1/Movies/x.mkv`, this service must too — even
though the host side may be NFS on one machine and SMB on the other.

A mismatch produces `404 source_not_found_on_encoder`.

## Running

```bash
docker compose up -d                                                       # CPU
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d    # NVIDIA
```

For Intel QSV or AMD VCE, add `devices: [/dev/dri:/dev/dri]` to the service.
The entrypoint joins the container user to the host's render group
automatically.

`HandBrakeCLI --help` only reports encoders available at *runtime*, not what
was compiled in — so `/health`'s `encoders` list is a truthful reflection of
what this specific machine can actually do. A GPU-less host legitimately
reports software encoders only (`x264`, `x265`, `svt_av1`, ...); that is
expected, not a misconfiguration. See `docs/build-spike-findings.md` for the
full spike writeup.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENCODER_ALLOWED_ROOTS` | *(empty)* | Comma-separated roots. **Required** — with none set, every request is rejected with `403 path_not_allowed` and `/health` reports `degraded`. |
| `ENCODER_WORKERS` | `1` | Concurrent encodes. Keep at 1 unless you know your NVENC session limit. |
| `ENCODER_JOB_TTL` | `3600` | Completed-job eviction, in seconds. |
| `HANDBRAKE_BIN` | `HandBrakeCLI` | Path to the binary. |
| `PORT` | `3335` | HTTP port inside the container. |
| `PUID` / `PGID` | `1000` | User the process runs as (applied by `entrypoint.sh`, not read by the app itself). Match your media share's owner. |
| `UMASK` | `022` | Permissions for written output (applied by `entrypoint.sh`) — must stay readable by Jellyfin. |

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Status, HandBrake version, probed encoders, allowed roots. |
| `POST` | `/jobs` | Queue an encode. `202` with `{"job_id": "..."}`. |
| `GET` | `/jobs/{id}` | Status, progress, `output_path`, encoder used, error. |
| `DELETE` | `/jobs/{id}` | Cancel if running, then delete. |

`POST /jobs` takes `{source_path, preset_json, preset_name}`. **No output path
and no HandBrake arguments are accepted.** The output path is derived as
`<dirname(source)>/.hbenc-<job_id>.<ext>`, where the extension comes from the
preset's own `FileFormat`. The caller reads it back from `GET /jobs/{id}` and
performs the swap itself — this service never modifies or deletes the source.

### Error codes

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `preset_not_found` | No such preset in the supplied document, or its container format is unsupported. |
| 403 | `path_not_allowed` | Path resolves outside `ENCODER_ALLOWED_ROOTS`. |
| 404 | `source_not_found_on_encoder` | Mount paths differ between the machines. |
| 404 | `job_not_found` | Unknown or already-deleted job. |
| 409 | `encoder_unavailable` | The preset's encoder is not available on this machine right now. |
| 503 | `service_unavailable` | The job queue is not running (service starting up or shutting down); the job was never accepted or stored, so there is no `job_id` to poll or delete. |

## Development

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -v
uvicorn app.main:app --host 0.0.0.0 --port 3335 --reload
```

No test requires a GPU, HandBrake, or any media files.
