# syntax=docker/dockerfile:1
# CPU-only, linux/amd64 release image. Version pins are updated deliberately.
FROM mwader/static-ffmpeg:8.0.1@sha256:9a4bf2b7915ef1c4d725a176159438146b68485cc613bf6662e2786c661cd16d AS ffmpeg
FROM python:3.14.4-slim-trixie AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt ./
# The workstation freeze includes CUDA. Keep all other exact pins, installing
# torch from its CPU-only index so the resolver cannot introduce CUDA wheels.
RUN python -c "from pathlib import Path; p=Path('requirements.txt'); excluded=('cuda-', 'nvidia-', 'torch==', 'torchvision==', 'triton=='); Path('cpu.txt').write_text('\\n'.join(x for x in p.read_text().splitlines() if not x.lower().startswith(excluded))+'\\n')" \
    && python -m venv /venv \
    && /venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchvision==0.29.1+cpu \
    && /venv/bin/pip install -r cpu.txt \
    && /venv/bin/pip check
# Versioned upstream weights, not the workstation's untracked model file.
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt', 'yolov8n.pt')" \
    && echo 'f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36  yolov8n.pt' | sha256sum -c -

FROM python:3.14.4-slim-trixie
ARG VERSION=development
LABEL org.opencontainers.image.source="https://github.com/zerolink-m/zerohome" \
      org.opencontainers.image.title="ZeroHome" \
      org.opencontainers.image.version="${VERSION}"
ENV PATH="/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/data/models YOLO_CONFIG_DIR=/data/models/ultralytics \
    CACHE_DIR=/data/cache MOTION_FILES_DIR=/data/motion_files \
    HOST=0.0.0.0 PORT=8000 VERSION=${VERSION}
# OpenCV's wheel loads GL even though no display server is used.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 zerohome && useradd --uid 10001 --gid 10001 --no-create-home zerohome \
    && mkdir -p /app /data/cache /data/motion_files /data/models \
    && chown -R 10001:10001 /app /data \
    && printf '%s\n' "$VERSION" > /usr/local/share/zerohome-version
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=dependencies /venv /venv
RUN HOME=/tmp/build-home YOLO_CONFIG_DIR=/tmp/build-yolo python -c "import torch, torchvision, cv2, aiortc, ultralytics, easyocr; assert torch.version.cuda is None" \
    && rm -rf /tmp/build-home /tmp/build-yolo \
    && ffmpeg -version && ffprobe -version
WORKDIR /app
COPY --chown=10001:10001 app/ ./app/
COPY --chown=10001:10001 alembic/ ./alembic/
COPY --chown=10001:10001 main.py alembic.ini frontend_versions.json ./
COPY --from=dependencies --chown=10001:10001 /build/yolov8n.pt ./yolov8n.pt
COPY --chmod=0555 deploy/container-entrypoint.sh /usr/local/bin/zerohome-entrypoint
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zerohome-entrypoint"]
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=3).read()"
# Exactly one process: in-process camera ownership/background jobs are not distributed.
CMD ["python", "main.py"]
