#!/usr/bin/env bash
# Buildroot post-fakeroot hook (runs as fake root just before the image is made).
# Rewrites live-session password hashes reproducibly and fixes ownership.
# Environment: GIDEON_CRYPTPW (a busybox binary with the cryptpw applet),
# GIDEON_LIVE_CONF (config/live.conf).
set -euo pipefail
T="$1"
: "${GIDEON_CRYPTPW:?} ${GIDEON_LIVE_CONF:?}"
source "$GIDEON_LIVE_CONF"

hash_pw() { # user password -> SHA-512 crypt with a salt derived from the inputs
    local salt; salt="$(printf 'gideonos-live:%s:%s' "$1" "$2" | sha256sum | cut -c1-16)"
    "$GIDEON_CRYPTPW" cryptpw -m sha512 -S "$salt" "$2"
}
set_hash() { # user hash
    awk -F: -v OFS=: -v u="$1" -v h="$2" '$1 == u { $2 = h; $3 = 20000 } { print }' \
        "$T/etc/shadow" > "$T/etc/shadow.new"
    mv "$T/etc/shadow.new" "$T/etc/shadow"
}
set_hash root "$(hash_pw root "$LIVE_ROOT_PASSWORD")"
set_hash "$LIVE_USER" "$(hash_pw "$LIVE_USER" "$LIVE_USER_PASSWORD")"
# Every other account stays locked; normalise the last-change field so the
# image does not depend on the build date.
awk -F: -v OFS=: '{ $3 = 20000; print }' "$T/etc/shadow" > "$T/etc/shadow.new"
mv "$T/etc/shadow.new" "$T/etc/shadow"
chown 0:0 "$T/etc/shadow"; chmod 0600 "$T/etc/shadow"

chown -R 1000:1000 "$T/home/$LIVE_USER"; chmod 0700 "$T/home/$LIVE_USER"
chmod 0700 "$T/root"
