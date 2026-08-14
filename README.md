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
confirmed that one HandBrakeCLI binary carries NVENC, QSV, and VCE support
simultaneously — there is no per-vendor ML-stack incompatibility to work
around here, so there is no need for per-vendor images. Verified against the
built image: `nvenc_{h264,h265,h265_10bit,av1,av1_10bit}`,
`qsv_{h264,h265,h265_10bit,av1,av1_10bit}` and
`vce_{h264,h265,h265_10bit,av1}` are all present in the one binary.

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
docker compose -f docker-compose.yml -f docker-compose.intel.yml up -d     # Intel QSV
docker compose -f docker-compose.yml -f docker-compose.amd.yml up -d       # AMD (VAAPI)
```

Or pull the published image instead of building:

```bash
docker pull ghcr.io/txcjulian/handbrake-video-encoder:latest
```

The Intel and AMD overrides pass `/dev/dri` through; the entrypoint joins the
container user to the host's render group automatically. The image ships the
vendor userspace drivers those encoders need (`iHD` and the oneVPL GPU runtime
for Intel, mesa's `radeonsi` for AMD) — passing the device alone is not enough
without them. NVENC needs no driver package: the NVIDIA container runtime
injects `libnvidia-encode.so.1`, which is why a plain `docker run` logs
`Cannot load libnvidia-encode.so.1`.

**AMD VCE does not work on this image**, deliberately. HandBrake's `vce_*`
encoders need AMD's proprietary AMF runtime (`libamfrt64.so`) from
`amdgpu-pro`, which is not in the Ubuntu archive. They are compiled into the
binary but will not initialise, and HandBrake logs `vcn: not available on this
system`. The AMD override gives you VAAPI, not VCE.

`HandBrakeCLI --help` only reports encoders available at *runtime*, not what
was compiled in — so `/health`'s `encoders` list is a truthful reflection of
what this specific machine can actually do. A GPU-less host legitimately
reports software encoders only (`x264`, `x265`, `svt_av1`, ...); that is
expected, not a misconfiguration.

To debug a GPU host that reports no hardware encoders, `vainfo` is installed:

```bash
docker compose exec handbrake-encoder vainfo
```

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

No test requires a GPU, HandBrake, or any media files — they all drive a
scriptable fake HandBrakeCLI.

That speed has a cost worth knowing about: the progress parser,
`--preset-import-file`, and the derived output path are checked against a fake
written by the same author as the code under test, so a shared misreading of
HandBrake's real behaviour would pass the whole suite. `scripts/e2e_encode.py`
closes that gap by running one real encode through the HTTP API, against real
HandBrakeCLI and a preset exported from HandBrake itself. CI runs it on every
push; to run it locally against a container:

```bash
mkdir -p /tmp/e2e && chmod 777 /tmp/e2e
ffmpeg -y -f lavfi -i testsrc=duration=2:size=128x128:rate=10 \
  -pix_fmt yuv420p /tmp/e2e/sample.mp4
docker run --rm -v /tmp/e2e:/work --entrypoint HandBrakeCLI \
  handbrake-video-encoder:latest -Z "Very Fast 1080p30" \
  --preset-export "E2E x264" --preset-export-file /work/preset.json
docker run -d --name hb-e2e -p 3335:3335 \
  -e ENCODER_ALLOWED_ROOTS=/media1 -v /tmp/e2e:/media1 \
  handbrake-video-encoder:latest
python scripts/e2e_encode.py --media-dir /tmp/e2e --preset-file /tmp/e2e/preset.json
docker rm -f hb-e2e
```

It covers the software x264 path only. Hardware encoders need real GPUs and
stay a deployment check.
