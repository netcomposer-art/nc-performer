# One image for Cloudflare Containers, Docker Compose, and Modal.
# Build context is the repository root (the kit and this service together).
#   docker build -f service/Dockerfile -t nc-perform .
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NC_STORAGE=file:///data \
    NC_DATA_DIR=/data \
    NC_BIND=0.0.0.0:8080 \
    NC_ROLE=container \
    NC_QUEUE_AUTOSTART=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      python3-pip \
      ca-certificates \
      ffmpeg \
      rubberband-cli \
      dragonfly-reverb \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && mkdir -p /usr/lib/vst3 /usr/share/doc/nc-perform /data

# The kit looks for DragonflyHallReverb.vst3 in the standard VST3 folders.
RUN set -eu; \
    found="$(find /usr -type d -name 'DragonflyHallReverb.vst3' 2>/dev/null | head -n 1 || true)"; \
    if [ -n "$found" ]; then ln -sfn "$found" /usr/lib/vst3/DragonflyHallReverb.vst3; fi

ENV PATH=/opt/venv/bin:$PATH
WORKDIR /opt/nc-performer

COPY pyproject.toml README.md LICENSE ./
COPY ncperf ./ncperf
COPY nc ./nc
COPY service/pyproject.toml service/NOTICE service/LICENSE ./service/
COPY service/nc_perform ./service/nc_perform
COPY service/web ./service/web

# pedalboard and Dragonfly are separate GPL-3.0 tools. See service/NOTICE.
# The kit and this service stay MIT.
RUN pip install --no-cache-dir . \
 && pip install --no-cache-dir "pedalboard==0.9.26" \
 && pip install --no-cache-dir ./service \
 && python3 -c "import nc_perform, pathlib, shutil; dest = pathlib.Path(nc_perform.__file__).resolve().parent / 'web'; dest.mkdir(exist_ok=True); shutil.copy('/opt/nc-performer/service/web/index.html', dest / 'index.html')" \
 && cp /opt/nc-performer/service/NOTICE /usr/share/doc/nc-perform/NOTICE \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin nc \
 && chown -R nc:nc /data

USER nc
EXPOSE 8080
CMD ["python3", "-m", "nc_perform"]
