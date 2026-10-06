#!/usr/bin/env bash
# Stage: assemble the root filesystem and pack it as a read-only squashfs image.
# Needs no root privileges: ownership and special modes are applied through
# mksquashfs pseudo-definitions, not on the host.
source "$(dirname "$0")/../lib/common.sh"
source "$GIDEON_ROOT/config/live.conf"

root="$BUILD_DIR/rootfs"
out="$OUT_DIR/rootfs.squashfs"
[[ -x "$OUT_DIR/busybox" ]] || die "busybox not built; run the busybox stage first"
command -v mksquashfs >/dev/null || die "mksquashfs missing (package squashfs-tools); see tools/check-deps.sh"

log "Assembling root filesystem"
rm -rf "$root"; mkdir -p "$root"

# Filesystem hierarchy (FHS). GideonOS-specific: /usr/lib/gideon (system
# scripts, services), /usr/share/gideon (vendor defaults), /etc/gideon (config).
for d in bin sbin boot dev etc home lib media mnt opt proc root run srv sys tmp \
         usr/bin usr/sbin usr/lib usr/share usr/local/bin usr/local/sbin \
         var/cache var/lib var/log var/tmp var/spool etc/profile.d; do
    mkdir -p "$root/$d"
done
ln -s ../run "$root/var/run"

cp -a "$GIDEON_ROOT/system/rootfs/." "$root/"

build_id="$(git -C "$GIDEON_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
sed -e "s/@GIDEON_VERSION@/$GIDEON_VERSION/g" \
    -e "s/@GIDEON_CODENAME@/$GIDEON_CODENAME/g" \
    -e "s/@GIDEON_CODENAME_LC@/${GIDEON_CODENAME,,}/g" \
    -e "s/@BUILD_ID@/$build_id/g" \
    "$root/etc/os-release.in" > "$root/etc/os-release"
rm "$root/etc/os-release.in"
printf '\nGideonOS %s (%s) \\l\n\n' "$GIDEON_VERSION" "$GIDEON_CODENAME" > "$root/etc/issue"
ln -s ../etc/os-release "$root/usr/lib/os-release"
ln -s /run/gideon/resolv.conf "$root/etc/resolv.conf"
echo gideon > "$root/etc/hostname"

# BusyBox and its applet links (relative, so the tree is relocatable).
install -m 0755 "$OUT_DIR/busybox" "$root/bin/busybox"
while IFS= read -r applet; do
    [[ "$applet" == bin/busybox || -e "$root/$applet" ]] && continue
    up="$(dirname "$applet" | sed -E 's#[^/]+#..#g')"
    ln -s "$up/bin/busybox" "$root/$applet"
done < "$OUT_DIR/busybox.links"

# Live-session accounts. Salts are derived from the inputs so the image is
# reproducible; these passwords are public anyway (config/live.conf).
hash_pw() { # user password
    local salt; salt="$(printf 'gideonos-live:%s:%s' "$1" "$2" | sha256sum | cut -c1-16)"
    "$OUT_DIR/busybox" cryptpw -m sha512 -S "$salt" "$2"
}
{
    echo "root:$(hash_pw root "$LIVE_ROOT_PASSWORD"):20000:0:99999:7:::"
    echo "$LIVE_USER:$(hash_pw "$LIVE_USER" "$LIVE_USER_PASSWORD"):20000:0:99999:7:::"
    echo "nobody:!:20000:0:99999:7:::"
} > "$root/etc/shadow"
mkdir -p "$root/home/$LIVE_USER"
cp -a "$root/etc/skel/." "$root/home/$LIVE_USER/"

chmod 0755 "$root"; chmod 0700 "$root/root" "$root/home/$LIVE_USER"
chmod 1777 "$root/tmp" "$root/var/tmp"; chmod 0600 "$root/etc/shadow"

log "Creating squashfs root image"
rm -f "$out"
mksquashfs "$root" "$out" -noappend -comp zstd -no-xattrs -quiet -no-progress \
    -action "uid(0)@true" -action "gid(0)@true" \
    -p "/bin/busybox m 4755 0 0" \
    -p "/home/$LIVE_USER m 700 1000 1000" \
    -p "/home/$LIVE_USER/.profile m 644 1000 1000" \
    > "$BUILD_DIR/squashfs.log" 2>&1 || { cat "$BUILD_DIR/squashfs.log"; die "mksquashfs failed"; }
sha256sum "$out" | cut -d' ' -f1 > "$out.sha256"
ok "root image: $(du -h "$out" | cut -f1) -> build/out/rootfs.squashfs"
