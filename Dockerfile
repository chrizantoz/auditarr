FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir pyyaml
LABEL org.opencontainers.image.source="https://github.com/chrizantoz/auditarr" org.opencontainers.image.description="Nightly quality control for a Sonarr/Radarr/Bazarr/Plex library" org.opencontainers.image.licenses="GPL-3.0"
WORKDIR /app
COPY audit.py ui.html /app/
COPY tests /app/tests
ENV PYTHONUNBUFFERED=1
EXPOSE 3091
CMD ["python", "/app/audit.py", "--daemon"]
