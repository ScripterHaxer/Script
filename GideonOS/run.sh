#!/usr/bin/env bash
# Boot build/GideonOS.iso in QEMU.
#
#   ./run.sh               graphical window if a display is available, else serial console
#   ./run.sh --serial      force serial console in this terminal (Ctrl-a x quits QEMU)
#   ./run.sh --uefi        boot with UEFI firmware (OVMF) instead of BIOS
#   ./run.sh --mem 4G      guest memory (default 2G)
#   ./run.sh --outputs 2   number of virtual displays (default 1)
#   ./run.sh -- <args>     pass extra arguments to QEMU
source "$(dirname "$0")/tools/lib/common.sh"

iso="$BUILD_DIR/GideonOS.iso"; mem=2G; uefi=0; mode=auto; outputs=1; extra=()
while (( $# )); do
    case "$1" in
        --serial|--headless) mode=serial ;;
        --gui)   mode=gui ;;
        --uefi)  uefi=1 ;;
        --mem)   mem="$2"; shift ;;
        --outputs) outputs="$2"; shift ;;
        --iso)   iso="$2"; shift ;;
        --) shift; extra=("$@"); break ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option '$1' (try ./run.sh --help)" ;;
    esac
    shift
done

command -v qemu-system-x86_64 >/dev/null || "$GIDEON_ROOT/tools/check-deps.sh" run
[[ -f "$iso" ]] || die "$iso not found — run ./build.sh first"

args=(-m "$mem" -smp 2 -cdrom "$iso" -boot d
      -nic user,model=virtio-net-pci
      -device virtio-rng-pci
      -vga none -device "virtio-vga,max_outputs=$outputs"
      -device qemu-xhci -device usb-tablet)
if [[ -w /dev/kvm ]]; then args+=(-enable-kvm -cpu host)
else warn "KVM unavailable: using software emulation (slower boot)"; args+=(-cpu max); fi

if (( uefi )); then
    fw=""
    for f in /usr/share/ovmf/OVMF.fd /usr/share/OVMF/OVMF_CODE.fd /usr/share/edk2/x64/OVMF.fd /usr/share/edk2-ovmf/x64/OVMF.fd; do
        [[ -f "$f" ]] && { fw="$f"; break; }
    done
    [[ -n "$fw" ]] || die "UEFI firmware (OVMF) not found; install package 'ovmf'"
    args+=(-bios "$fw")
fi

if [[ "$mode" == auto ]]; then
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then mode=gui; else mode=serial; fi
fi
if [[ "$mode" == serial ]]; then
    log "Serial console mode — press Ctrl-a x to quit QEMU"
    args+=(-nographic)
else
    args+=(-serial mon:stdio)
fi

exec qemu-system-x86_64 "${args[@]}" "${extra[@]}"
