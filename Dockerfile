# ---- build stage -----------------------------------------------------------
FROM ubuntu:24.04 AS build
ARG DEBIAN_FRONTEND=noninteractive
ARG HANDBRAKE_VERSION=1.9.2

RUN apt-get update && apt-get install -y --no-install-recommends \
      autoconf automake build-essential cmake git libass-dev libbz2-dev \
      libfontconfig-dev libfreetype-dev libfribidi-dev libharfbuzz-dev \
      libjansson-dev liblzma-dev libmp3lame-dev libnuma-dev libogg-dev \
      libopus-dev libsamplerate0-dev libspeex-dev libtheora-dev libtool \
      libtool-bin libturbojpeg0-dev libvorbis-dev libx11-dev libx264-dev \
      libxml2-dev libvpx-dev m4 make meson nasm ninja-build patch pkg-config \
      python3 tar zlib1g-dev ca-certificates \
      libva-dev libdrm-dev \
      curl clang llvm libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# libdovi (Dolby Vision RPU parsing/injection) is a Rust crate built through
# cargo-c. HandBrake's ./configure treats it as optional: with no Rust
# toolchain present it just skips it and reports a successful configure, so
# the build "works" but silently drops DoVi metadata from every x265 encode.
# libssl-dev above is cargo-c's own build-time dep (TLS for crates.io);
# clang/llvm are HandBrake's own NVENC/NVDEC requirement, not cargo-c's,
# though clang incidentally also covers cargo-c's bindgen use.
ENV RUSTUP_HOME=/opt/rust CARGO_HOME=/opt/rust PATH="/opt/rust/bin:$PATH"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain stable \
    && cargo install cargo-c --locked

RUN git clone --depth 1 --branch ${HANDBRAKE_VERSION} \
      https://github.com/HandBrake/HandBrake.git /src
WORKDIR /src

# NVENC/QSV/VCE are amd64-only. On arm64 the build falls back to the CPU
# encoders (x264/x265), which is the only thing that hardware can do anyway.
RUN set -eux; \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      GPU_FLAGS="--enable-nvenc --enable-qsv --enable-vce"; \
    else \
      GPU_FLAGS=""; \
    fi; \
    ./configure --launch-jobs="$(nproc)" --launch --disable-gtk ${GPU_FLAGS}

# ---- runtime stage ---------------------------------------------------------
# Ubuntu 24.04, the SAME release as the build stage. This is load-bearing, not
# stylistic. Verified with objdump/ldd against the Task 1 spike binary:
#   * it requires GLIBC_2.38, and Debian bookworm ships 2.36 — the binary
#     cannot exec there at all;
#   * it needs libvpx.so.9, and bookworm provides only libvpx.so.7;
#   * it links libva.so.2 / libva-drm.so.2 / libdrm.so.2, which must be present
#     at RUNTIME too, not merely as -dev packages in the build stage.
# Build and runtime bases must stay on the same distro release.
FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      libass9 libjansson4 libmp3lame0 libnuma1 libogg0 libopus0 \
      libsamplerate0 libspeex1 libtheora0 libturbojpeg libvorbis0a \
      libvorbisenc2 libx264-164 libxml2 libvpx9 libfribidi0 libharfbuzz0b \
      libfontconfig1 libfreetype6 \
      libva2 libva-drm2 libdrm2 \
      util-linux python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Vendor userspace drivers. libva2/libva-drm2 above are only the *API*; without
# a driver behind them /usr/lib/x86_64-linux-gnu/dri is empty and every hardware
# encoder fails at init even with /dev/dri passed through and a working GPU.
#
# The Intel media stack comes from Intel's PPA, NOT Ubuntu's archive. Noble
# ships intel-media-va-driver-non-free 24.1.0 and libmfx-gen1.2 23.2.3, both of
# which predate Battlemage (Arc B580, launched after they were cut), so QSV
# simply would not come up on that card. The PPA carries 26.2.2. This is the
# same source the sibling Whisper_Lyric-Transcriber service had to adopt for
# Battlemage (e09d81e), there for the compute stack rather than the media one.
#   https://dgpu-docs.intel.com/installation-guides/installing-packages-from-the-intel-ppa.html
#
#   intel-media-va-driver-non-free  iHD VA driver (QSV). The non-free build
#                                   carries codecs the -free one omits.
#   libmfx-gen1.2                   Intel oneVPL GPU Runtime — the actual QSV
#                                   implementation for Gen12+ / Arc / Battlemage.
#   libvpl2                         oneVPL dispatcher; it only *finds* a runtime,
#                                   so it is not sufficient on its own.
#   libmfx1                         Legacy Media SDK runtime for Gen8-11. Stays
#                                   on the archive version; the PPA does not
#                                   carry it and those parts are long stable.
#   mesa-va-drivers                 radeonsi VAAPI, for AMD decode/VAAPI encode.
#   vainfo                          Diagnostics. Tiny, and the first thing worth
#                                   running when a GPU host reports no encoders.
#
# NVENC needs no package here: libnvidia-encode.so.1 is injected at runtime by
# the NVIDIA container runtime (`--gpus`), which is why a plain `docker run`
# logs "Cannot load libnvidia-encode.so.1" and is expected.
#
# AMD VCE is NOT made to work by this. HandBrake's vce_* encoders require AMD's
# proprietary AMF runtime (libamfrt64.so) from amdgpu-pro, which is not in the
# Ubuntu archive. Those encoders are compiled in but will not initialise on a
# stock image; mesa-va-drivers gives AMD hosts VAAPI, not VCE. See README.
RUN apt-get update && apt-get install -y --no-install-recommends \
      software-properties-common \
    && add-apt-repository -y ppa:kobuk-team/intel-graphics \
    && apt-get update && apt-get install -y --no-install-recommends \
      intel-media-va-driver-non-free libmfx-gen1.2 libvpl2 \
      libmfx1 mesa-va-drivers vainfo \
    && apt-get purge -y software-properties-common \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/HandBrakeCLI /usr/local/bin/HandBrakeCLI

# PEP 668: Ubuntu's system Python is externally managed and refuses a bare
# `pip install`, so the dependencies live in a venv that is first on PATH.
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
RUN python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY entrypoint.sh /entrypoint.sh

# Ubuntu 24.04 (unlike 22.04 and Debian) ships a stock `ubuntu` account holding
# UID/GID 1000, which must go before appuser can take those ids.
#
# The build-time symptom is just "groupadd: GID '1000' already exists". The
# runtime consequence is worse and is the real reason this matters: entrypoint.sh
# force-remaps appuser onto PUID/PGID via `usermod -o` (allow-non-unique), so
# both accounts end up on UID 1000; `setpriv --init-groups` then resolves that
# shared UID back to "ubuntu" and loads ITS groups instead of appuser's,
# silently dropping the render-group membership the entrypoint just granted for
# /dev/dri. The service would start, look healthy, and report no GPU encoders.
# Diagnosed in the sibling Whisper_Lyric-Transcriber service (commit 35ccb0b),
# where the same base image hid the same collision behind a torch.xpu device
# count of zero.
#
# So UID 1000 is not cosmetic: entrypoint.sh defaults PUID/PGID to it, outputs
# land in a shared media library whose ownership must match the renamer's, and
# it is the near-universal first-user id on the hosts this runs on.
RUN userdel -r ubuntu 2>/dev/null || true; \
    groupdel ubuntu 2>/dev/null || true; \
    groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -M -s /bin/false appuser && \
    chmod +x /entrypoint.sh

ENV PORT=3335
EXPOSE 3335

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"3335\")}/health').read()"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-3335}"]
