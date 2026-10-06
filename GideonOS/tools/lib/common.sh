#!/usr/bin/env bash
# Shared helpers for the GideonOS build scripts. Source, don't execute.

set -euo pipefail

GIDEON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${GIDEON_BUILD_DIR:-$GIDEON_ROOT/build}"
DL_DIR="$BUILD_DIR/downloads"
SRC_DIR="$BUILD_DIR/src"
OUT_DIR="$BUILD_DIR/out"
STAMP_DIR="$BUILD_DIR/stamps"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 2)}"

# shellcheck source=../../config/versions.env
source "$GIDEON_ROOT/config/versions.env"

# Reproducibility: pin timestamps and identity so outputs depend only on inputs.
# Default epoch is the last commit touching the tree; falls back to a constant.
if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
    SOURCE_DATE_EPOCH="$(git -C "$GIDEON_ROOT" log -1 --format=%ct -- . 2>/dev/null || true)"
    SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"
fi
export SOURCE_DATE_EPOCH
export KBUILD_BUILD_TIMESTAMP="@${SOURCE_DATE_EPOCH}"
export KBUILD_BUILD_USER="gideon"
export KBUILD_BUILD_HOST="gideonos-build"
export LC_ALL=C TZ=UTC

if [[ -t 2 ]]; then
    _c_info=$'\e[1;36m' _c_ok=$'\e[1;32m' _c_warn=$'\e[1;33m' _c_err=$'\e[1;31m' _c_off=$'\e[0m'
else
    _c_info="" _c_ok="" _c_warn="" _c_err="" _c_off=""
fi
log()  { printf '%s==>%s %s\n' "$_c_info" "$_c_off" "$*" >&2; }
ok()   { printf '%s ok%s %s\n' "$_c_ok" "$_c_off" "$*" >&2; }
warn() { printf '%swarn%s %s\n' "$_c_warn" "$_c_off" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$_c_err" "$_c_off" "$*" >&2; exit 1; }

# fetch URL SHA256 -> prints local path. Verifies checksum every time.
fetch() {
    local url="$1" sum="$2" file
    file="$DL_DIR/$(basename "$url")"
    mkdir -p "$DL_DIR"
    if [[ ! -f "$file" ]]; then
        log "Downloading $(basename "$url")"
        curl -fL --retry 4 --retry-delay 2 -o "$file.part" "$url" || die "download failed: $url"
        mv "$file.part" "$file"
    fi
    if ! echo "$sum  $file" | sha256sum -c --status; then
        rm -f "$file"
        die "checksum mismatch for $(basename "$url") (file removed; re-run to retry)"
    fi
    printf '%s\n' "$file"
}

# extract ARCHIVE DEST_DIR: unpack once into SRC_DIR (top-level dir becomes DEST_DIR).
extract() {
    local archive="$1" dest="$2"
    [[ -d "$dest" ]] && return 0
    log "Extracting $(basename "$archive")"
    mkdir -p "$dest.tmp"
    tar -xf "$archive" -C "$dest.tmp" --strip-components=1
    mv "$dest.tmp" "$dest"
}

# Stamp helpers: a stage reruns when its input hash changes.
stamp_ok()  { [[ -f "$STAMP_DIR/$1" && "$(cat "$STAMP_DIR/$1")" == "$2" ]]; }
stamp_set() { mkdir -p "$STAMP_DIR"; printf '%s\n' "$2" > "$STAMP_DIR/$1"; }
hash_inputs() { # hash files/dirs given as args (content + relative paths)
    (cd "$GIDEON_ROOT" && find "$@" -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) | sha256sum | cut -d' ' -f1
}
