#!/usr/bin/env bash
# Detect host build/run dependencies for GideonOS and explain what is missing.
#   tools/check-deps.sh [build|run|all] [--install]
# --install runs the distro package manager for you (apt/dnf/pacman only).
source "$(dirname "$0")/lib/common.sh"
set +e

scope="${1:-all}"; install=0
[[ "${2:-}" == "--install" || "${1:-}" == "--install" ]] && install=1
[[ "$scope" == "--install" ]] && scope=all

# name | kind(cmd/hdr/file) | probe | apt pkg | dnf pkg | pacman pkg | why
BUILD_DEPS=(
  "gcc|cmd|gcc|build-essential|gcc|base-devel|C compiler (kernel, BusyBox)"
  "make|cmd|make|make|make|make|build driver"
  "bc|cmd|bc|bc|bc|bc|kernel build"
  "bison|cmd|bison|bison|bison|bison|kernel kconfig"
  "flex|cmd|flex|flex|flex|flex|kernel kconfig"
  "perl|cmd|perl|perl|perl|perl|kernel build"
  "curl|cmd|curl|curl|curl|curl|source downloads"
  "xz|cmd|xz|xz-utils|xz|xz|kernel tarball"
  "bzip2|cmd|bzip2|bzip2|bzip2|bzip2|BusyBox tarball"
  "cpio|cmd|cpio|cpio|cpio|cpio|initramfs archive"
  "gzip|cmd|gzip|gzip|gzip|gzip|initramfs compression"
  "xorriso|cmd|xorriso|xorriso|xorriso|libisoburn|ISO image creation"
  "mformat|cmd|mformat|mtools|mtools|mtools|UEFI boot image in ISO"
  "grub-mkrescue|cmd|grub-mkrescue|grub-common|grub2-tools-extra|grub|bootloader ISO builder"
  "grub-bios|file|/usr/lib/grub/i386-pc/modinfo.sh|grub-pc-bin|grub2-pc-modules|grub|BIOS boot support"
  "grub-efi|file|/usr/lib/grub/x86_64-efi/modinfo.sh|grub-efi-amd64-bin|grub2-efi-x64-modules|grub|UEFI boot support"
  "libelf|hdr|gelf.h|libelf-dev|elfutils-libelf-devel|libelf|kernel objtool"
  "openssl|hdr|openssl/opensslv.h|libssl-dev|openssl-devel|openssl|kernel certificate tools"
)
RUN_DEPS=(
  "qemu|cmd|qemu-system-x86_64|qemu-system-x86|qemu-system-x86|qemu-system-x86|run.sh / boot tests"
  "python3|cmd|python3|python3|python3|python|automated boot tests"
  "ovmf|file|/usr/share/ovmf/OVMF.fd|ovmf|edk2-ovmf|edk2-ovmf|UEFI boot in QEMU (optional)"
)

probe() {
    case "$1" in
        cmd)  command -v "$2" >/dev/null ;;
        file) [[ -e "$2" ]] ;;
        hdr)  echo "#include <$2>" | ${CC:-cc} -E -x c - >/dev/null 2>&1 ;;
    esac
}

pm=""
for c in apt-get dnf pacman; do command -v $c >/dev/null && { pm=$c; break; }; done
col=4; case "$pm" in dnf) col=5;; pacman) col=6;; esac

missing_pkgs=(); missing=0; optional_missing=0
check_list() {
    local entry name kind probe_arg why pkg
    for entry in "$@"; do
        IFS='|' read -r name kind probe_arg _ _ _ why <<<"$entry"
        pkg="$(cut -d'|' -f$col <<<"$entry")"
        if probe "$kind" "$probe_arg"; then
            ok "$name"
        elif [[ "$name" == ovmf ]]; then
            warn "$name missing (optional: $why) -> package: $pkg"; optional_missing=1
        else
            printf '%smissing%s %-14s needed for: %s -> package: %s\n' "$_c_err" "$_c_off" "$name" "$why" "$pkg" >&2
            missing_pkgs+=("$pkg"); missing=1
        fi
    done
}

log "Host: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}") / $(uname -m)"
[[ "$(uname -m)" == x86_64 ]] || warn "Host is not x86_64; cross-compilation is not supported yet."
[[ "$scope" == build || "$scope" == all ]] && { log "Build dependencies"; check_list "${BUILD_DEPS[@]}"; }
[[ "$scope" == run   || "$scope" == all ]] && { log "Run/test dependencies"; check_list "${RUN_DEPS[@]}"; }
if [[ "$scope" == run || "$scope" == all ]]; then
    if [[ -w /dev/kvm ]]; then ok "KVM acceleration available"
    else warn "/dev/kvm not usable: QEMU will use software emulation (works, but slower)."; fi
fi

if (( missing )); then
    mapfile -t missing_pkgs < <(printf '%s\n' "${missing_pkgs[@]}" | sort -u)
    case "$pm" in
        apt-get) cmd="apt-get install -y ${missing_pkgs[*]}" ;;
        dnf)     cmd="dnf install -y ${missing_pkgs[*]}" ;;
        pacman)  cmd="pacman -S --needed ${missing_pkgs[*]}" ;;
        *)       cmd="" ;;
    esac
    if [[ -z "$cmd" ]]; then
        die "Missing dependencies (unknown package manager). Install the equivalents of: ${missing_pkgs[*]}"
    fi
    if (( install )); then
        [[ $EUID -eq 0 ]] || cmd="sudo $cmd"
        log "Running: $cmd"; eval "$cmd" || die "package installation failed"
        exec "$0" "$scope"
    fi
    die "Missing dependencies. Install them with:  $cmd   (or run: tools/check-deps.sh $scope --install)"
fi
ok "All required dependencies present."
