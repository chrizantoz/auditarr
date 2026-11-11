#!/bin/bash
# Release checklist as a script (R2). Usage:  scripts/release.sh v0.9.1 [--prerelease] [--no-baselines] [--dry-run]
#  1. checks: on main, clean, in sync, CI green on HEAD, tag free
#  2. notes: the top CHANGELOG section becomes the release notes; its heading is renamed to the version + today's date
#  3. baselines: the last green CI run's screenshots become the visual-diff baselines (uibot/baseline/)
#  4. commit "release vX.Y.Z", push, annotated tag, push tag  -> CI builds ghcr.io/<repo>:X.Y.Z (+ X.Y, latest) from that commit
#  5. GitHub release with the notes (--prerelease while in beta)
#  6. waits for the tag's CI run, then boots the tagged image once (docker here, or over ssh with RELEASE_VERIFY_SSH=user@host) and checks it reports the version
# Identity: uses your git config; or set RELEASE_GIT_NAME / RELEASE_GIT_EMAIL for the release commit.
set -euo pipefail
V="${1:?usage: $0 vX.Y.Z [--prerelease] [--no-baselines] [--dry-run]}"; shift || true
PRE=""; BASE=1; DRY=0
for a in "$@"; do case "$a" in --prerelease) PRE="--prerelease";; --no-baselines) BASE=0;; --dry-run) DRY=1;; *) echo "unknown flag $a"; exit 2;; esac; done
[[ "$V" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like v0.9.1"; exit 2; }
cd "$(dirname "$0")/.."
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner); IMG="ghcr.io/${REPO,,}:${V#v}"
GITID=(); [ -n "${RELEASE_GIT_NAME:-}" ] && GITID=(-c "user.name=$RELEASE_GIT_NAME" -c "user.email=${RELEASE_GIT_EMAIL:?}")
say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }; die(){ echo "STOP: $*" >&2; exit 1; }

say "1/6 checks"
[ "$(git branch --show-current)" = main ] || die "not on main"
git fetch -q origin main --tags
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree has uncommitted changes"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main is not in sync with origin/main (pull or push first)"
git rev-parse -q --verify "refs/tags/$V" >/dev/null && die "tag $V already exists"
RUN=$(gh run list -w ci.yml --commit "$(git rev-parse HEAD)" --json databaseId,status,conclusion -q '.[0]')
[ -n "$RUN" ] || die "no CI run found for HEAD"
if [ "$(jq -r .status <<<"$RUN")" != completed ]; then echo "CI still running on HEAD, waiting..."; gh run watch "$(jq -r .databaseId <<<"$RUN")" --exit-status >/dev/null || die "CI failed on HEAD"
elif [ "$(jq -r .conclusion <<<"$RUN")" != success ]; then die "CI is not green on HEAD ($(jq -r .conclusion <<<"$RUN"))"; fi
RUNID=$(jq -r .databaseId <<<"$RUN"); echo "ok: main clean, in sync, CI run $RUNID green, tag free, image $IMG"

say "2/6 release notes from CHANGELOG"
NOTES=$(mktemp); python3 - "$V" "$NOTES" <<'PY'
import re, sys, datetime
v, out = sys.argv[1], sys.argv[2]; s = open("CHANGELOG.md", encoding="utf-8").read()
m = list(re.finditer(r"^## .*$", s, re.M)); assert len(m) >= 1, "no sections in CHANGELOG.md"
start, end = m[0].start(), (m[1].start() if len(m) > 1 else len(s)); head_end = s.index("\n", start)
heading = f"## {v} — {datetime.date.today().isoformat()}"
body = s[head_end + 1:end].strip("\n")
assert body.strip(), "the top CHANGELOG section is empty - write the notes first"
open(out, "w", encoding="utf-8").write(body + "\n")
if not s[start:head_end].startswith(f"## {v}"): s = s[:start] + heading + s[head_end:]; open("CHANGELOG.md", "w", encoding="utf-8").write(s); print("heading set:", heading)
else: print("heading already", s[start:head_end])
print(f"notes: {len(body.splitlines())} lines")
PY

say "3/6 visual baselines from CI run $RUNID"
if [ $BASE = 1 ]; then
  T=$(mktemp -d); if gh run download "$RUNID" -n ui-screenshots -D "$T" >/dev/null 2>&1 && ls "$T"/ui-phone.png "$T"/ui-desktop.png >/dev/null 2>&1; then cp "$T"/ui-phone.png "$T"/ui-desktop.png uibot/baseline/; echo "baselines refreshed (phone, desktop)"; else echo "no screenshots artifact on that run - baselines unchanged"; fi; rm -rf "$T"
else echo "skipped (--no-baselines)"; fi

say "4/6 commit, push, tag"
git add CHANGELOG.md uibot/baseline/
if [ $DRY = 1 ]; then echo "dry run: would commit, tag $V and push"; git --no-pager diff --cached --stat; git checkout -q -- CHANGELOG.md uibot/baseline/; exit 0; fi
if ! git diff --cached --quiet; then git "${GITID[@]}" commit -q -m "release $V"; fi
git push -q origin main
git "${GITID[@]}" tag -a "$V" -m "$V"; git push -q origin "$V"; echo "tagged $V at $(git rev-parse --short HEAD)"

say "5/6 GitHub release"
gh release create "$V" --title "$V" --notes-file "$NOTES" $PRE >/dev/null && echo "release published: https://github.com/$REPO/releases/tag/$V"

say "6/6 wait for the tag build, then boot the image once"
for i in $(seq 1 30); do TR=$(gh run list -w ci.yml --branch "$V" --json databaseId -q '.[0].databaseId' 2>/dev/null); [ -n "$TR" ] && break; sleep 10; done
[ -n "$TR" ] || die "no CI run appeared for tag $V"
gh run watch "$TR" --exit-status >/dev/null || die "CI failed on the tag"
CHECK="import audit, sys; print('boots', audit.APP_VERSION); sys.exit(0 if audit.APP_VERSION.startswith('$V') else 1)"
if docker info >/dev/null 2>&1; then docker pull -q "$IMG" >/dev/null; docker run --rm -e CONFIG_DIR=/tmp/c "$IMG" python -c "$CHECK"
elif [ -n "${RELEASE_VERIFY_SSH:-}" ]; then ssh "$RELEASE_VERIFY_SSH" "docker pull -q $IMG >/dev/null && docker run --rm -e CONFIG_DIR=/tmp/c $IMG python -c \"$CHECK\""
else echo "no docker here and RELEASE_VERIFY_SSH unset - skipped the boot check"; fi
say "done: $V"
echo "next: promote with your deploy script (auditarr-deploy.sh update), and the About tab will show $V as current."
