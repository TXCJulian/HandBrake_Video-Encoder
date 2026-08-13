# Build Spike Findings

HandBrake 1.9.2, Ubuntu 24.04, amd64. Raw captures in `docs/spike/`.

## Single-image strategy: **CONFIRMED**

One HandBrakeCLI binary carries NVENC, QSV and VCE simultaneously. Per-vendor
images (the Whisper_Lyric-Transcriber pattern) are **not** needed.

`./configure --disable-gtk --enable-nvenc --enable-qsv --enable-vce` succeeds,
and HandBrake's bundled ffmpeg is configured with:

```text
--enable-nvenc --enable-ffnvcodec --enable-amf --enable-libvpl --enable-vaapi
```

Encoder symbols present in the built binary:

```text
nvenc_av1  nvenc_av1_10bit  nvenc_h264  nvenc_h265  nvenc_h265_10bit
qsv_av1    qsv_av1_10bit    qsv_h264    qsv_h265    qsv_h265_10bit
vce_av1    vce_h264         vce_h265    vce_h265_10bit
```

### Required build dependency, easily missed

`libva-dev` (and `libdrm-dev`) must be installed. `--enable-vce` and
`--enable-qsv` make HandBrake request VAAPI from its bundled ffmpeg; without
the headers, ffmpeg's configure aborts the whole build:

```text
ERROR: vaapi requested but not found
gmake: *** [contrib/ffmpeg/.stamp.ffmpeg.configure] Error 1
```

This is the failure mode of the first spike attempt.

### Benign build-log noise

These lines look like failures and are not. `nv-codec-headers` and `AMF` are
header-only packages with no `clean` target; HandBrake ignores the failed
`make clean` and installs the headers immediately afterwards.

```text
gmake: [../contrib/amf/module.rules:2: contrib/amf/.stamp.amf.build] Error 2 (ignored)
gmake: [../contrib/nvenc/module.rules:2: contrib/nvenc/.stamp.nvenc.build] Error 2 (ignored)
```

## `--help` lists RUNTIME availability, not compile-time support

This is the single most consequential finding for `app/encoders.py`.

On a container with no GPU passthrough, HandBrake reports:

```text
Cannot load libnvidia-encode.so.1
[..] vcn: not available on this system
[..] qsv: not available on this system
```

and the `-e, --encoder` block then lists **software encoders only** —
`svt_av1`, `ffv1`, `x264`, `x265`, `x265_10bit`, `mpeg4`, `mpeg2`, `VP8`,
`VP9`, `theora` — even though the binary demonstrably contains `nvenc_h264`.

Consequences:

1. **`encoders.py`'s runtime probe is correct by design.** It reports what can
   actually encode on this machine, which is exactly what preset validation
   needs. A compile-time list would accept presets that can never run.
2. **Never verify a build with `--help`.** Use `strings` on the binary
   (see Task 9). A build check and a deployment check are different things.
3. `/health` on a GPU-less host will legitimately report only software
   encoders. That is truthful, not a bug.

### Encoder-block format (settles two deferred review findings)

```text
   -e, --encoder <string>  Select video encoder:
                               svt_av1
                               x264
                               x265_10bit
       --encoder-preset <string>
```

One bare token per line at 31 spaces of indentation; the block ends at the
next flag line. Both `encoders.py` regexes are correct against this:

- `_INDENTED_VALUE = ^\s{4,}(\S+)\s*$` — matches; no trailing annotation text
  appears on encoder lines.
- `_ANY_FLAG = ^\s*-` — `       --encoder-preset` is whitespace-then-hyphen,
  so the block terminates correctly. No hyphen-led wrapped lines occur inside
  the block.

## `--json` progress format

Objects are **pretty-printed across multiple lines**, prefixed `Version: ` or
`Progress: `. Verbatim (`docs/spike/json-output.txt`):

```text
Progress: {
    "State": "WORKING",
    "Working": {
        "ETASeconds": 0,
        "Pass": 1,
        "PassCount": 1,
        "Progress": 0.98000001907348633,
        "Rate": 0.0,
        "RateAvg": 0.0,
        "SequenceID": 1
    }
}
Progress: {
    "State": "WORKDONE",
    "WorkDone": {
        "Error": 0,
        "SequenceID": 1
    }
}
```

- Progress is a **fraction** at `Working.Progress` — multiply by 100.
- ETA is `Working.ETASeconds`.
- States seen in this short encode: `WORKING`, `WORKDONE`. `SCANNING` and
  `MUXING` did not appear but should be tolerated.
- `WORKDONE` uses the key `WorkDone`, **not** `Working` — so a parser keying on
  `Working` correctly ignores it, and 100% must come from job completion rather
  than from the stream. Observed final `Progress` was 0.98, never 1.0.
- The leading `Version:` object has no `Working` key and is correctly ignored.
- Exit code on success: 0.

### Parser bug this format revealed

A line-buffering parser that clears its buffer on any brace-bearing line is
**broken** against this output: the line `    }` closing the nested `Working`
object contains a brace while the outer object is still open. Clearing there
discards the accumulated text and the object never parses — progress silently
never fires. The buffer must be cleared only once a complete object has been
parsed. Pinned by `test_parses_real_handbrake_output_verbatim`.

## Reproduction

See Task 1 of the plan. Build takes roughly 15-30 minutes on 8 cores.
