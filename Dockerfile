# ---- build stage -----------------------------------------------------------
FROM ubuntu:24.04 AS build
ARG DEBIAN_FRONTEND=noninteractive
ARG HANDBRAKE_VERSION=1.9.2

RUN apt-get update && apt-get install -y --no-install-recommends \
      autoconf automake build-essential cmake git libass-dev libbz2-dev \
      libfontconfig-dev libfreetype-dev libfribidi-dev libharfbuzz-dev \
      libjansson-dev liblzma-dev libmp3lame-dev libnuma-dev libogg-dev \
      libopus-dev libsamplerate0-dev libspeex-dev libtheora-dev libtool \
      libtool-bin libturbojpeg0-dev libvorbis-dev libx264-dev libxml2-dev \
      libvpx-dev m4 make meson nasm ninja-build patch pkg-config python3 \
      tar zlib1g-dev ca-certificates \
      libva-dev libdrm-dev \
    && rm -rf /var/lib/apt/lists/*

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
FROM python:3.11-slim-bookworm
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      libass9 libjansson4 libmp3lame0 libnuma1 libogg0 libopus0 \
      libsamplerate0 libspeex1 libtheora0 libturbojpeg0 libvorbis0a \
      libvorbisenc2 libx264-164 libxml2 libvpx7 libfribidi0 libharfbuzz0b \
      libfontconfig1 libfreetype6 util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/HandBrakeCLI /usr/local/bin/HandBrakeCLI

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY entrypoint.sh /entrypoint.sh

RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -M -s /bin/false appuser && \
    chmod +x /entrypoint.sh

ENV PORT=3335
EXPOSE 3335

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"3335\")}/health').read()"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-3335}"]
