#!/bin/bash
# Auditarr playground with plain docker (no compose needed; works on Unraid). Run from the repository root or any copy that holds sandbox/ and tests/.
#   sandbox/run.sh up        build the fake stack, start both containers (port $SANDBOX_PORT, default 3095)
#   sandbox/run.sh reset     stop, wipe the fake files and the database, start again (cron this hourly for a public copy)
#   sandbox/run.sh down      stop and remove everything
# SANDBOX_PUBLIC=1 puts the pair on an internal network with no host port: attach your reverse proxy to network 'auditarr-sandbox' and point it at auditarr-sandbox:3091.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd); IMG="${AUDITARR_IMAGE:-ghcr.io/chrizantoz/auditarr:latest}"; NAME="${SANDBOX_NAME:-auditarr-sandbox}"; NET="$NAME"; PORT="${SANDBOX_PORT:-3095}"; PUBLIC="${SANDBOX_PUBLIC:-0}"
# SANDBOX_NAME lets two copies live side by side (a private one and a public one): containers $NAME and $NAME-stack, network and volumes named after it
down(){ docker rm -f "$NAME" "$NAME-stack" >/dev/null 2>&1 || true; docker volume rm -f "$NAME-media" "$NAME-config" >/dev/null 2>&1 || true; }
up(){
  docker network inspect $NET >/dev/null 2>&1 || docker network create $([ "$PUBLIC" = "1" ] && echo --internal) $NET >/dev/null
  docker build -q -t auditarr-sandbox-stack -f "$HERE/sandbox/Dockerfile.stack" "$HERE" >/dev/null
  docker volume create "$NAME-media" >/dev/null; docker volume create "$NAME-config" >/dev/null
  docker run -d --name "$NAME-stack" --network $NET -v "$NAME-media:/data" --restart unless-stopped auditarr-sandbox-stack >/dev/null
  sleep 3
  docker run -d --name "$NAME" --network $NET $([ "$PUBLIC" = "1" ] || echo -p "$PORT:3091") --restart unless-stopped --memory=512m --pids-limit 256 \
    -e SANDBOX=1 -e TZ="${TZ:-UTC}" -e SONARR_URL=http://$NAME-stack:8765/sonarr -e SONARR_APIKEY=test -e RADARR_URL=http://$NAME-stack:8765/radarr -e RADARR_APIKEY=test \
    -e BAZARR_URL=http://$NAME-stack:8765/bazarr -e BAZARR_APIKEY=test -e PLEX_URL=http://$NAME-stack:8765/plex -e PLEX_TOKEN=test -e PLEX_PATH_MAP=/data=/data \
    -e SUBGEN_URLS=http://$NAME-stack:8765/whisper -e HOST_IP= -e RUN_ON_START=1 -e RUN_AT=23:59 -e ARR_RESCAN=on -e DEDUPE=on -e REMEDIATE=on -e REMEDIATE_CONFIRM=I_UNDERSTAND_FILES_WILL_BE_DELETED \
    -e TRACK_PREFS=on -e PLEX_CHECK=on -e PLEX_REFRESH=on -e AUTO_WHISPER=on -e AUTO_WHISPER_DAYS=0 -e WATCH_PRIORITY=on -e PAPERCLIP=off -e NTFY_URL= -e UPDATE_CHECK=0 -e TRASH_DIR=/data/.trash -e MISSING_MIN_DAYS=3 -e PROBE_WORKERS=2 \
    -v "$NAME-media:/data" -v "$NAME-config:/config" -l com.centurylinklabs.watchtower.enable=false "$IMG" >/dev/null
  echo "playground $NAME up: $([ "$PUBLIC" = "1" ] && echo "internal network $NET, no host port - attach the reverse proxy to it and point it at $NAME:3091" || echo "http://localhost:$PORT")"
}
case "${1:-}" in up) up ;; reset) down; up ;; down) down ;; *) echo "usage: $0 up|reset|down"; exit 2 ;; esac
