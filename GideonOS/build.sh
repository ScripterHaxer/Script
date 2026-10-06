#!/usr/bin/env bash
# GideonOS build entry point.
#
#   ./build.sh            build everything -> build/GideonOS.iso
#   ./build.sh <stage>    run one stage: deps | kernel | busybox | rootfs | initramfs | iso
#   ./build.sh test       boot the ISO in QEMU (BIOS + UEFI) and run system checks
#   ./build.sh clean      remove build outputs (keeps downloaded sources)
#   ./build.sh distclean  remove the whole build/ directory
#
# Stages are incremental: kernel and BusyBox rebuild only when their config,
# pinned version or build script changes.
source "$(dirname "$0")/tools/lib/common.sh"
stages="$GIDEON_ROOT/tools/build"

run_stage() { log "[$1]"; "$stages/$1.sh"; }

case "${1:-all}" in
    all)
        "$GIDEON_ROOT/tools/check-deps.sh" build
        start=$SECONDS
        run_stage kernel; run_stage busybox; run_stage rootfs; run_stage initramfs; run_stage iso
        ok "GideonOS $GIDEON_VERSION built in $((SECONDS - start))s -> build/GideonOS.iso"
        ;;
    deps)                       "$GIDEON_ROOT/tools/check-deps.sh" all ;;
    kernel|busybox|rootfs|initramfs|iso)  run_stage "$1" ;;
    test)  shift; exec "$GIDEON_ROOT/tests/boot_test.py" "$@" ;;
    clean)
        rm -rf "$BUILD_DIR"/{out,rootfs,iso-root,stamps,kernel-obj,initramfs.list,*.log,GideonOS.iso*,test-logs,test-assets}
        rm -rf "$SRC_DIR"; ok "cleaned (downloads kept in build/downloads)" ;;
    distclean)  rm -rf "$BUILD_DIR"; ok "removed build/" ;;
    -h|--help|help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown target '$1' (try ./build.sh help)" ;;
esac
