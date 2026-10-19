# One image for Cloudflare Containers, Docker Compose, and Modal.
# Build context is the repository root (the kit and this service together).
#   docker build -f service/Dockerfile -t nc-perform .
#
# Checked against the Debian trixie (deb13) package index, without building:
#   python3 is 3.13. pedalboard==0.9.26 has a cp313 manylinux wheel.
#   ffmpeg depends on libavfilter10 (librubberband, libsoxr) and libavcodec61 (libmp3lame).
#   rubberband-cli is 3.3, which the kit uses for R3 time-stretch.
#   dragonfly-reverb pulls dragonfly-reverb-vst3, which installs
#   /usr/lib/vst3/DragonflyHallReverb.vst3. That is the path the kit searches.
#   Do not symlink that directory onto itself.
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
      libsndfile1 \
      passwd \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && mkdir -p /usr/share/doc/nc-perform /data

# Fail here if the package layout changes, before a render falls back quietly.
RUN if [ ! -d /usr/lib/vst3/DragonflyHallReverb.vst3 ]; then \
      echo "DragonflyHallReverb.vst3 is not in /usr/lib/vst3. The kit will not find the reverb hack."; \
      find /usr -type d -name 'DragonflyHallReverb.vst3' -print || true; \
      exit 1; \
    fi

ENV PATH=/opt/venv/bin:$PATH
WORKDIR /opt/nc-performer

COPY pyproject.toml README.md LICENSE ./
COPY ncperf ./ncperf
COPY nc ./nc
COPY service/pyproject.toml service/README.md service/NOTICE service/LICENSE ./service/
COPY service/nc_perform ./service/nc_perform
COPY service/web ./service/web

# pedalboard and Dragonfly are separate GPL-3.0 tools. See service/NOTICE.
# The kit and this service stay MIT.
RUN pip install --no-cache-dir . \
 && pip install --no-cache-dir "pedalboard==0.9.26" \
 && pip install --no-cache-dir ./service \
 && python3 -c "import nc_perform, pathlib, shutil; dest = pathlib.Path(nc_perform.__file__).resolve().parent / 'web'; dest.mkdir(exist_ok=True); shutil.copy('/opt/nc-performer/service/web/index.html', dest / 'index.html')" \
 && cp /opt/nc-performer/service/NOTICE /usr/share/doc/nc-perform/NOTICE \
 && python3 -c "import ncperf, nc_perform, pedalboard, numpy, scipy, soundfile, librosa, pyloudnorm, cryptography; from nc_perform.api import web_index; open(web_index(), encoding='utf-8').read()" \
 && ffmpeg -hide_banner -filters | grep -q rubberband \
 && ffmpeg -hide_banner -h filter=aresample 2>&1 | grep -q soxr \
 && ffmpeg -hide_banner -encoders | grep -q libmp3lame \
 && rubberband --version \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin nc \
 && chown -R nc:nc /data

USER nc
EXPOSE 8080
CMD ["python3", "-m", "nc_perform"]
