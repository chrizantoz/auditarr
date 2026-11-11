FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir pyyaml
WORKDIR /app
COPY audit.py ui.html /app/
COPY tests /app/tests
ENV PYTHONUNBUFFERED=1
EXPOSE 3091
CMD ["python", "/app/audit.py", "--daemon"]
