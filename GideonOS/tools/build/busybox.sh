#!/usr/bin/env bash
# Stage: build a static BusyBox providing the minimal base userspace.
source "$(dirname "$0")/../lib/common.sh"

src="$SRC_DIR/busybox-$BUSYBOX_VERSION"
out="$OUT_DIR/busybox"
key="$(hash_inputs config/busybox tools/build/busybox.sh)-$BUSYBOX_VERSION-$BUSYBOX_SHA256"

if stamp_ok busybox "$key" && [[ -x "$out" ]]; then ok "busybox up to date"; exit 0; fi

extract "$(fetch "$BUSYBOX_URL" "$BUSYBOX_SHA256")" "$src"
mkdir -p "$OUT_DIR"

log "Configuring BusyBox $BUSYBOX_VERSION"
make -C "$src" defconfig >/dev/null
while IFS= read -r line; do
    [[ "$line" =~ ^(CONFIG_[A-Z0-9_]+)=(.*)$ ]] || continue
    name="${BASH_REMATCH[1]}" val="${BASH_REMATCH[2]}"
    sed -i -e "/^$name=/d" -e "/^# $name is not set/d" "$src/.config"
    if [[ "$val" == n ]]; then echo "# $name is not set" >> "$src/.config"
    else echo "$name=$val" >> "$src/.config"; fi
done < "$GIDEON_ROOT/config/busybox/gideon.config"
yes "" | make -C "$src" oldconfig >/dev/null 2>&1 || true
grep -qx 'CONFIG_STATIC=y' "$src/.config" || die "BusyBox CONFIG_STATIC not applied"

log "Building BusyBox"
make -C "$src" -j"$JOBS" busybox > "$BUILD_DIR/busybox-build.log" 2>&1 \
    || { tail -40 "$BUILD_DIR/busybox-build.log"; die "BusyBox build failed (log: build/busybox-build.log)"; }
file "$src/busybox" 2>/dev/null | grep -q 'statically linked' || warn "busybox does not look statically linked"

cp "$src/busybox" "$out"
"$out" --list-full > "$OUT_DIR/busybox.links"
stamp_set busybox "$key"
ok "busybox: $("$out" | head -1 | cut -d' ' -f1-2) ($(wc -l < "$OUT_DIR/busybox.links") applets)"
