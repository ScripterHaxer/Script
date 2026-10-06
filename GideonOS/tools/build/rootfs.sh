#!/usr/bin/env bash
# Stage: build the root image userspace with Buildroot (system/buildroot is our
# BR2_EXTERNAL tree) and export build/out/rootfs.squashfs.
#
# Buildroot handles ownership, setuid bits and device permissions under
# fakeroot, so this stage needs no root privileges. The first build takes
# about an hour (systemd, Mesa, Wayland, Weston ...); later builds reuse
# finished packages and only redo the image (minutes).
#
#   GIDEON_BR_TARGETS="weston-rebuild"  extra make targets to run first
source "$(dirname "$0")/../lib/common.sh"

br_src="$SRC_DIR/buildroot-$BUILDROOT_VERSION"
br_out="$BUILD_DIR/buildroot"
defconfig="$GIDEON_ROOT/system/buildroot/configs/gideonos_x86_64_defconfig"
out="$OUT_DIR/rootfs.squashfs"
[[ -x "$OUT_DIR/busybox" ]] || die "busybox not built; run the busybox stage first (used for password hashing)"
[[ -d "$OUT_DIR/modules/lib/modules" ]] || die "kernel modules missing; run the kernel stage first"

extract "$(fetch "$BUILDROOT_URL" "$BUILDROOT_SHA256")" "$br_src"

export BR2_DL_DIR="$DL_DIR/buildroot"
export GIDEON_VERSION GIDEON_CODENAME
export GIDEON_BUILD_ID="$(git -C "$GIDEON_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
export GIDEON_MODULES_DIR="$OUT_DIR/modules"
export GIDEON_CRYPTPW="$OUT_DIR/busybox"
export GIDEON_LIVE_CONF="$GIDEON_ROOT/config/live.conf"
# Buildroot refuses to run with these set.
unset PERL_MM_OPT LD_LIBRARY_PATH CROSS_COMPILE ARCH

log "Configuring Buildroot $BUILDROOT_VERSION (GideonOS defconfig)"
mkdir -p "$br_out" "$BR2_DL_DIR"
make -C "$br_src" O="$br_out" BR2_EXTERNAL="$GIDEON_ROOT/system/buildroot" \
    BR2_DEFCONFIG="$defconfig" defconfig > "$BUILD_DIR/buildroot-config.log" 2>&1 \
    || { cat "$BUILD_DIR/buildroot-config.log"; die "Buildroot defconfig failed"; }

# Fail loudly if Kconfig dropped an option (unmet dependency or typo).
bad=0
while IFS= read -r line; do
    if [[ "$line" =~ ^(BR2_[A-Za-z0-9_]+)=(.*)$ ]]; then
        grep -qxF "$line" "$br_out/.config" || { warn "not applied: $line (got: $(grep "^${BASH_REMATCH[1]}=" "$br_out/.config" || echo unset))"; bad=1; }
    elif [[ "$line" =~ ^#\ (BR2_[A-Za-z0-9_]+)\ is\ not\ set$ ]]; then
        grep -q "^${BASH_REMATCH[1]}=" "$br_out/.config" && { warn "should be unset: ${BASH_REMATCH[1]}"; bad=1; }
    fi
done < "$defconfig"
(( bad == 0 )) || die "Buildroot defconfig not fully applied"

# Our own packages build from the working tree: always re-sync and rebuild
# them (`-rebuild` re-runs the rsync of local sources; cargo stays incremental).
targets=(gideon-gfx-probe-dirclean gideon-compositor-rebuild ${GIDEON_BR_TARGETS:-})
log "Building userspace with Buildroot (log: build/buildroot.log)"
start=$SECONDS
make -C "$br_out" "${targets[@]}" > "$BUILD_DIR/buildroot.log" 2>&1 \
  && make -C "$br_out" >> "$BUILD_DIR/buildroot.log" 2>&1 \
  || { tail -40 "$BUILD_DIR/buildroot.log"; die "Buildroot build failed (full log: build/buildroot.log)"; }

cp "$br_out/images/rootfs.squashfs" "$out"
sha256sum "$out" | cut -d' ' -f1 > "$out.sha256"
ok "root image: $(du -h "$out" | cut -f1) in $((SECONDS - start))s -> build/out/rootfs.squashfs"
