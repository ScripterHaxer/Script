#!/usr/bin/env bash
# Stage: build a hybrid BIOS + UEFI bootable ISO with GRUB.
source "$(dirname "$0")/../lib/common.sh"

iso_root="$BUILD_DIR/iso-root"
iso="$BUILD_DIR/GideonOS.iso"
for f in vmlinuz initramfs.img rootfs.squashfs rootfs.squashfs.sha256; do
    [[ -f "$OUT_DIR/$f" ]] || die "missing build/out/$f; run earlier stages first"
done

log "Staging ISO tree"
rm -rf "$iso_root"
mkdir -p "$iso_root/boot/grub" "$iso_root/gideon"
cp "$OUT_DIR/vmlinuz" "$OUT_DIR/initramfs.img" "$iso_root/boot/"
cp "$OUT_DIR/rootfs.squashfs" "$OUT_DIR/rootfs.squashfs.sha256" "$iso_root/gideon/"
cp "$GIDEON_ROOT/boot/grub/grub.cfg" "$iso_root/boot/grub/grub.cfg"
printf '%s %s\n' "GideonOS" "$GIDEON_VERSION" > "$iso_root/GIDEONOS"
find "$iso_root" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

log "Creating ISO with grub-mkrescue (BIOS + UEFI)"
grub-mkrescue -o "$iso.tmp" "$iso_root" -- -volid GIDEONOS \
    > "$BUILD_DIR/iso-build.log" 2>&1 \
    || { cat "$BUILD_DIR/iso-build.log"; die "grub-mkrescue failed"; }
mv "$iso.tmp" "$iso"
(cd "$BUILD_DIR" && sha256sum GideonOS.iso > GideonOS.iso.sha256)
ok "ISO: build/GideonOS.iso ($(du -h "$iso" | cut -f1))"
