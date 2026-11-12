FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FC_HOST=0.0.0.0 FC_PORT=8000 FC_DATA_DIR=/data

# ffmpeg with libass for captions; Noto fonts cover most of the world's scripts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-cjk fontconfig \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY freeclipper ./freeclipper
COPY run.py .

RUN useradd --create-home --uid 1000 clipper && mkdir -p /data && chown clipper /data
USER clipper
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "run.py"]
