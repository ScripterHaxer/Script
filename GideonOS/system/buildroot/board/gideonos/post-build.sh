#!/usr/bin/env bash
# Buildroot post-build hook: GideonOS identity, kernel modules, unit enablement.
# Runs after packages and the rootfs overlay are installed into $1 (TARGET_DIR).
# Environment (exported by tools/build/rootfs.sh): GIDEON_VERSION,
# GIDEON_CODENAME, GIDEON_BUILD_ID, GIDEON_MODULES_DIR.
set -euo pipefail
T="$1"
: "${GIDEON_VERSION:?} ${GIDEON_MODULES_DIR:?}"

# Identity: replace Buildroot's os-release with ours.
sed -e "s/@GIDEON_VERSION@/$GIDEON_VERSION/g" \
    -e "s/@GIDEON_CODENAME@/$GIDEON_CODENAME/g" \
    -e "s/@GIDEON_CODENAME_LC@/${GIDEON_CODENAME,,}/g" \
    -e "s/@BUILD_ID@/$GIDEON_BUILD_ID/g" \
    "$T/etc/os-release.in" > "$T/usr/lib/os-release"
rm -f "$T/etc/os-release.in" "$T/etc/os-release"
ln -s ../usr/lib/os-release "$T/etc/os-release"
printf '\nGideonOS %s (%s) \\l\n\n' "$GIDEON_VERSION" "$GIDEON_CODENAME" > "$T/etc/issue"
echo gideon > "$T/etc/hostname"

# Kernel modules built by tools/build/kernel.sh (merged /usr: /lib -> usr/lib).
rm -rf "$T/usr/lib/modules"
mkdir -p "$T/usr/lib/modules"
cp -a "$GIDEON_MODULES_DIR/lib/modules/." "$T/usr/lib/modules/"

# DNS through systemd-resolved's stub.
ln -sf ../run/systemd/resolve/stub-resolv.conf "$T/etc/resolv.conf"

# Enable units (symlinks, as `systemctl enable` would create them).
enable() { # unit target
    mkdir -p "$T/etc/systemd/system/$2.wants"
    ln -sf "/usr/lib/systemd/system/$1" "$T/etc/systemd/system/$2.wants/$1"
}
enable gideon-config-apply.service sysinit.target
enable systemd-networkd.service multi-user.target
enable systemd-resolved.service multi-user.target
enable systemd-timesyncd.service sysinit.target
enable getty@tty1.service getty.target

# The live system boots to the console session (graphical session starts from it).
ln -sf /usr/lib/systemd/system/multi-user.target "$T/etc/systemd/system/default.target"

# Remove Buildroot's sample serial-getty enablement; systemd's getty
# generator starts one on every active kernel console.
rm -f "$T"/etc/systemd/system/getty.target.wants/serial-getty@*.service

# Root may not log in on any terminal (pam_securetty): keep securetty empty
# even if a package or Buildroot's getty setup adds entries.
: > "$T/etc/securetty"

# systemd-networkd-persistent-storage.service talks to networkd over varlink;
# when networkd crashes, its restart job waits for this unit's restart, which
# waits for networkd: a deadlock (found by the crash-restart test). The live
# system keeps no persistent network state, so mask it.
ln -sf /dev/null "$T/etc/systemd/system/systemd-networkd-persistent-storage.service"
