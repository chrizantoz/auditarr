#!/bin/bash
# Auditarr deploy / staging / rollback for a Docker host (written for Unraid). Install at /boot/config/custom/auditarr-deploy.sh with an auditarr-deploy.env beside it.
#   auditarr-deploy.sh run <image>        recreate the production container from <image>
#   auditarr-deploy.sh stage <image>      run the image against a COPY of the database with every write gate off, wait for its run + self-test, compare with production
#   auditarr-deploy.sh update             pull :latest; if it is new, stage it; promote only if staging passes (nightly, cron)
#   auditarr-deploy.sh rollback           recreate production from the previously running image
set -u
# Site values live in auditarr-deploy.env next to this script (not in the repository); these are the generic defaults.
IMG_REPO="ghcr.io/chrizantoz/auditarr"; L=/mnt/user/appdata/auditarr; STATE=/boot/config/custom/auditarr-deploy.state
NTFY=""; NTFY_TOKEN_FILE=""; HOST_IP=192.168.1.10; NETWORK=bridge; CONTAINER=media-audit; PAPERCLIP_ARGS=""; EXTRA_MOUNTS=""; EXTRA_ENV=""; EXTRA_ENV_ARR=(); SECRET_MOUNTS=""; EXTRA_NETWORKS=""   # EXTRA_ENV_ARR=(-e "NAME=value with spaces") for values that contain spaces
[ -f "$(dirname "$0")/auditarr-deploy.env" ] && . "$(dirname "$0")/auditarr-deploy.env"
log(){ echo "$(date '+%F %T') $*"; }
notify(){ curl -s -o /dev/null -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: package" ${NTFY_TOKEN_FILE:+-H "Authorization: Bearer $(cat $NTFY_TOKEN_FILE)"} -d "$2" "$NTFY" || true; }
common_args(){ cat <<EOF
--network $NETWORK --memory=1g -e TZ=${TZ:-UTC} -e HOST_IP=$HOST_IP -e PROBE_WORKERS=4
-v ${MEDIA:-/mnt/user/data}:/data
$EXTRA_MOUNTS
EOF
}
wait_idle(){ # never recreate production while a UI action (remux, re-grab) or an audit run is in flight; gives up after 30 min
  local i=0 busy
  while [ $i -lt 180 ]; do busy=$(curl -s --max-time 5 http://127.0.0.1:3091/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("busy", 0))' 2>/dev/null || echo 0); [ "${busy:-0}" = "0" ] && return 0; [ $i -eq 0 ] && log "production busy ($busy in flight) - waiting before the swap"; sleep 10; i=$((i+1)); done
  log "still busy after 30 min - swapping anyway"
}
run_prod(){ # $1 = image
  local img="$1"; wait_idle; docker rm -f media-audit >/dev/null 2>&1
  docker run -d --name media-audit --restart unless-stopped -p 3091:3091 $(common_args | tr '\n' ' ') \
    -e RUN_AT=05:00 -e RUN_ON_START=0 -e NTFY_URL="$NTFY" -e NTFY_TOKEN_FILE=/run/secrets/ntfy \
    -e ARR_RESCAN=on -e DEDUPE=on -e DEDUPE_MAX=50 -e REMEDIATE=${REMEDIATE:-dryrun} -e REMEDIATE_CONFIRM=I_UNDERSTAND_FILES_WILL_BE_DELETED $PAPERCLIP_ARGS -e PLEX_REFRESH=on -e PLEX_CHECK=on \
    -e HOST_CONTAINERNAME=media-audit $EXTRA_ENV "${EXTRA_ENV_ARR[@]}" -l net.unraid.docker.managed=dockerman -l com.centurylinklabs.watchtower.enable=false \
    -v $L/data:/config ${NTFY_TOKEN_FILE:+-v $NTFY_TOKEN_FILE:/run/secrets/ntfy:ro} $SECRET_MOUNTS "$img" >/dev/null || return 1
  for n in $EXTRA_NETWORKS; do docker network connect $n media-audit 2>/dev/null; done; sleep 6
  curl -sf http://127.0.0.1:3091/health >/dev/null && log "production running $img" || { log "production did not come up on $img"; return 1; }
}
stage(){ # $1 = image, $2 = extra docker args (fault drill), $3 = "drill" to judge a drill instead of a release ; returns 0 when the staged run looks sane
  local img="$1" extra="${2:-}" mode="${3:-}" S="$L-staging"; docker rm -f media-audit-staging >/dev/null 2>&1; rm -rf "$S"; mkdir -p "$S/data"
  cp -a "$L/data/state.db" "$S/data/"; cp -a "$L/data/ignore.txt" "$S/data/" 2>/dev/null
  docker run -d --name media-audit-staging -p 3093:3091 $(common_args | tr '\n' ' ') \
    -e RUN_AT=23:59 -e RUN_ON_START=1 -e ARR_RESCAN=off -e DEDUPE=off -e REMEDIATE=off -e PAPERCLIP=off -e PLEX_REFRESH=off -e PLEX_CHECK=on \
    -e SUBTAG=off -e AUTO_FIXHEADER=off -e AUTO_VERIFY=off -e AUTO_ACCEPTLANG=off -e AUTO_WHISPER=off -e TRACK_PREFS=off -e JOBS=off -e READ_ONLY=1 -e TRASH_DIR=/data/media/.media-audit-trash-staging -e HOST_CONTAINERNAME=media-audit-staging \
    -l com.centurylinklabs.watchtower.enable=false -v $S/data:/config $extra "$img" >/dev/null || { log "staging container failed to start"; return 1; }
  local i=0; while [ $i -lt 180 ]; do sleep 10; i=$((i+1)); [ -f "$S/data/reports/selftest.json" ] && break; done
  local prod stag verdict
  curl -s http://127.0.0.1:3091/api/state > "$S/prod.json"; curl -s http://127.0.0.1:3093/api/state > "$S/stag.json"; curl -s http://127.0.0.1:3093/health > "$S/health.json"
  prod=$(python3 -c 'import json,sys; j=json.load(open(sys.argv[1])); print(len(j["findings"]), j["health"]["status"])' "$S/prod.json" 2>/dev/null)
  stag=$(python3 -c 'import json,sys; j=json.load(open(sys.argv[1])); print(len(j["findings"]), j["progress"]["phase"])' "$S/stag.json" 2>/dev/null)
  verdict=$(python3 - "$S/data/reports/selftest.json" "$S/prod.json" "$S/stag.json" "$S/health.json" "$mode" <<'EOF'
import json, sys, collections
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
st, P, Sg, H, mode = load(sys.argv[1]), load(sys.argv[2]), load(sys.argv[3]), load(sys.argv[4]), sys.argv[5]
pf, sf = P.get("findings", []), Sg.get("findings", []); pn, sn = len(pf), len(sf); reasons = []
if mode == "drill":   # dependencies were pointed at dead ports: the run must still finish, say what was unreachable, and write nothing
    if (Sg.get("progress") or {}).get("phase") != "done": reasons.append(f"run did not finish: phase={(Sg.get('progress') or {}).get('phase')}")
    if H.get("status") not in ("ok", "running"): reasons.append(f"health {H.get('status')}")
    import os; expect = set((os.environ.get("DRILL_SET") or "bazarr plex whisparr").split())
    missing = expect - set(H.get("degraded") or []); reasons += [f"{m} outage not reported as degraded" for m in sorted(missing)]
    extra_down = set(H.get("degraded") or []) - expect; reasons += [f"{m} reported down but was not part of the drill" for m in sorted(extra_down)]
    if H.get("write_attempts"): reasons.append(f"tried to write {H['write_attempts']}x")
    if not sf: reasons.append("no findings at all - the other arrs were not audited")
    print("PASS" if not reasons else "FAIL: " + "; ".join(reasons)); sys.exit(0)
if st.get("failed") or not st.get("total"): reasons.append(f"self-test {st.get('passed')}/{st.get('total')} failed={st.get('failed')}")
if (Sg.get("progress") or {}).get("phase") != "done": reasons.append(f"staged run phase={(Sg.get('progress') or {}).get('phase')}")
if pn and abs(sn - pn) > max(50, 0.2 * pn): reasons.append(f"findings {pn} -> {sn} (>20% swing)")
pc, sc = collections.Counter(f["check"] for f in pf if f["severity"] != "low"), collections.Counter(f["check"] for f in sf if f["severity"] != "low")
notes = []
for c in sorted(set(pc) | set(sc)):   # a check that suddenly doubles (medium+) is a regression even when the total looks fine; drops are usually the night's fixes landing, so they are noted, not failed
    a, b = pc.get(c, 0), sc.get(c, 0)
    if max(a, b) >= 20 and b > 2 * a: reasons.append(f"{c} {a} -> {b} (new noise)")
    elif max(a, b) >= 20 and b < 0.5 * a: notes.append(f"{c} {a} -> {b}")
if notes: print("note: drops " + "; ".join(notes), file=sys.stderr)
if H.get("write_attempts"): reasons.append(f"staging tried to write {H['write_attempts']}x: {H.get('write_log')}")
if not H.get("read_only"): reasons.append("staging is not in READ_ONLY mode")
print("PASS" if not reasons else "FAIL: " + "; ".join(reasons))
EOF
)
  docker rm -f media-audit-staging >/dev/null 2>&1; rm -rf "$S" /mnt/user/media/media/.media-audit-trash-staging
  log "${mode:-staging} $img -> $verdict (prod: $prod | staged: $stag)"; echo "$verdict" | grep -q '^PASS'
}
current_image(){ docker inspect media-audit --format '{{.Config.Image}}@{{.Image}}' 2>/dev/null; }
case "${1:-}" in
  run)      run_prod "${2:-$IMG_REPO:latest}" ;;
  stage)    stage "${2:-$IMG_REPO:latest}" ;;
  rollback) prev=$(grep '^previous=' $STATE 2>/dev/null | cut -d= -f2); [ -n "$prev" ] && run_prod "$prev" && notify "Auditarr rolled back" "$prev" || { log "no previous image recorded"; exit 1; } ;;
  update)
    docker pull -q $IMG_REPO:latest >/dev/null 2>&1 || { log "pull failed"; exit 1; }
    new=$(docker inspect $IMG_REPO:latest --format '{{.Id}}'); cur=$(docker inspect media-audit --format '{{.Image}}' 2>/dev/null)
    [ "$new" = "$cur" ] && { log "no new image"; exit 0; }
    tag="$IMG_REPO@$(docker inspect $IMG_REPO:latest --format '{{index .RepoDigests 0}}' | cut -d@ -f2)"
    if stage "$IMG_REPO:latest"; then
      echo "previous=$(docker inspect media-audit --format '{{index .RepoDigests 0}}' 2>/dev/null || echo $cur)" > $STATE
      run_prod "$IMG_REPO:latest" && notify "Auditarr updated" "new image passed staging and is live: ${tag:0:60}" || notify "Auditarr update FAILED to start" "$tag" high
    else notify "Auditarr update held back" "new image failed staging - production unchanged. See /var/log/auditarr-deploy.log" high; fi ;;
  drill)   # fault drill: the running production image, staged read-only with services pointed at dead ports; rotates weekly (all three / bazarr / plex / whisparr)
    img=$(docker inspect media-audit --format '{{.Config.Image}}' 2>/dev/null || echo $IMG_REPO:latest)
    case "${2:-$(( 10#$(date +%U) % 4 ))}" in 1) DRILL_SET="bazarr";; 2) DRILL_SET="plex";; 3) DRILL_SET="whisparr";; *) DRILL_SET="bazarr plex whisparr";; esac
    extra=""; for svc in $DRILL_SET; do extra="$extra -e $(echo $svc | tr a-z A-Z)_URL=http://127.0.0.1:9"; done; export DRILL_SET
    if stage "$img" "$extra" drill; then notify "Auditarr fault drill passed" "with $DRILL_SET unreachable the audit still finished, reported it as degraded and wrote nothing"
    else notify "Auditarr fault drill FAILED" "see /var/log/auditarr-deploy.log" high; exit 1; fi ;;
  *) echo "usage: $0 run|stage|update|rollback|drill [image]"; exit 2 ;;
esac
