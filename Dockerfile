FROM python:3.15-slim
# upgrade first so Debian security fixes (e.g. zlib) land; pip is removed after install because the app never needs it and its vendored urllib3/msgpack trip the image scan
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
LABEL org.opencontainers.image.source="https://github.com/chrizantoz/auditarr" org.opencontainers.image.description="Nightly quality control for a Sonarr/Radarr/Bazarr/Plex library" org.opencontainers.image.licenses="GPL-3.0"
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && pip uninstall -y pip && rm -rf /usr/local/lib/python3*/site-packages/pip* /root/.cache
COPY audit.py ui.html /app/
COPY tests /app/tests
ARG VERSION=dev
ARG REVISION=
ENV PYTHONUNBUFFERED=1 APP_VERSION=$VERSION APP_REVISION=$REVISION
EXPOSE 3091
CMD ["python", "/app/audit.py", "--daemon"]
