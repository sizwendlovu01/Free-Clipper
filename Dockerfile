# ClipForge AI production image (Railway, any Docker host). Debian-based Python 3.13.
# Pinned by digest for reproducible builds (python:3.13-slim, Debian 13 "trixie", Python 3.13.18).
# Note for Railway: this file must not declare persistent mounts itself (Railway rejects that);
# attach storage at /data in the Railway dashboard instead.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FC_HOST=0.0.0.0 \
    FC_PORT=8000 \
    STORAGE_ROOT=/data \
    YUNET_MODEL_PATH=/opt/clipforge/models/face_detection_yunet_2023mar.onnx

# ffmpeg (built with libass for captions), Noto fonts for most of the world's scripts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-cjk fontconfig ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer. constraints.txt pins every
# transitive package to the versions this image was tested with.
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

# Face-detection model (OpenCV YuNet, 233 KB), checksum-verified, so rendering never depends on
# downloading it at runtime.
RUN mkdir -p /opt/clipforge/models \
 && python -c "import hashlib, urllib.request; u = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'; d = urllib.request.urlopen(u, timeout=60).read(); assert hashlib.sha256(d).hexdigest() == '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4', 'checksum mismatch'; open('/opt/clipforge/models/face_detection_yunet_2023mar.onnx', 'wb').write(d)"

RUN useradd --create-home --uid 1000 --user-group clipper && mkdir -p /data && chown clipper:clipper /data

COPY freeclipper ./freeclipper
COPY run.py docker-entrypoint.sh ./
RUN chmod 755 docker-entrypoint.sh

# Videos, clips, models and the database are written to /data (attach persistent storage there).
EXPOSE 8000

# The app listens on $PORT when the platform sets it (Railway does), otherwise 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', os.environ.get('FC_PORT', '8000')), timeout=4)"]

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "run.py"]
