#!/bin/bash
# Install ffmpeg/ffprobe and mkvtoolnix on a CI runner WITHOUT Ubuntu's package mirrors (they stalled for an hour on 2026-11-11).
# ffmpeg: static build from GitHub releases (BtbN). mkvtoolnix: copied out of the Debian image our container is built on, libs bundled.
# Usage: sudo bash scripts/ci-media-tools.sh [prefix=/usr/local]
set -euo pipefail
PREFIX="${1:-/usr/local}"; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
FFURL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
echo "-- ffmpeg (static, from GitHub)"
curl -fsSL --retry 3 "$FFURL" -o "$TMP/ff.tar.xz"; tar -xJf "$TMP/ff.tar.xz" -C "$TMP"
install -m755 "$TMP"/ffmpeg-*/bin/ffmpeg "$TMP"/ffmpeg-*/bin/ffprobe "$PREFIX/bin/"
echo "-- mkvtoolnix (from Debian trixie, same as the production image)"
DEST="$PREFIX/lib/mkvtoolnix-bundle"; mkdir -p "$DEST/bin" "$DEST/lib"
docker run --rm -v "$DEST":/out debian:trixie-slim bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends mkvtoolnix >/dev/null
  for b in mkvmerge mkvpropedit mkvinfo mkvextract; do
    cp /usr/bin/$b /out/bin/
    ldd /usr/bin/$b | awk "/=> \//{print \$3}" | grep -vE "/(libc|libm|libpthread|libdl|librt|libresolv|ld-linux)[.-]" | xargs -r cp -n -t /out/lib/ 2>/dev/null || true
  done
  cp -r /usr/share/mkvtoolnix /out/share 2>/dev/null || true
  chmod -R a+rX /out'
for b in mkvmerge mkvpropedit mkvinfo mkvextract; do
  printf '#!/bin/sh\nexport LD_LIBRARY_PATH=%s/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexec %s/bin/%s "$@"\n' "$DEST" "$DEST" "$b" > "$PREFIX/bin/$b"; chmod 755 "$PREFIX/bin/$b"
done
echo "-- installed:"; "$PREFIX/bin/ffmpeg" -version | head -1; "$PREFIX/bin/ffprobe" -version | head -1; "$PREFIX/bin/mkvmerge" --version; "$PREFIX/bin/mkvpropedit" --version
