FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    FC_HOST=0.0.0.0 FC_PORT=8000 FC_DATA_DIR=/data

# ffmpeg with libass for captions; Noto fonts cover most of the world's scripts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-cjk fontconfig \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY freeclipper ./freeclipper
COPY run.py .

RUN useradd --create-home --uid 1000 clipper && mkdir -p /data && chown clipper /data
USER clipper
# Videos, clips, models and the database live here: mount a persistent volume on /data.
VOLUME ["/data"]
EXPOSE 8000

# Hosting platforms that set $PORT are honoured automatically (see freeclipper/config.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', os.environ.get('FC_PORT', '8000')), timeout=4)" || exit 1

CMD ["python", "run.py"]
