#!/usr/bin/env bash
# Stage: build the Linux kernel image (bzImage) from pinned upstream source.
source "$(dirname "$0")/../lib/common.sh"

src="$SRC_DIR/linux-$LINUX_VERSION"
obj="$BUILD_DIR/kernel-obj"
out="$OUT_DIR/vmlinuz"
key="$(hash_inputs config/kernel tools/build/kernel.sh)-$LINUX_VERSION-$LINUX_SHA256"

if stamp_ok kernel "$key" && [[ -f "$out" ]]; then ok "kernel up to date"; exit 0; fi

extract "$(fetch "$LINUX_URL" "$LINUX_SHA256")" "$src"
mkdir -p "$obj" "$OUT_DIR"

log "Configuring Linux $LINUX_VERSION (x86_64_defconfig + GideonOS fragment)"
make -C "$src" O="$obj" ARCH=x86_64 x86_64_defconfig >/dev/null
(cd "$obj" && "$src/scripts/kconfig/merge_config.sh" -m .config "$GIDEON_ROOT/config/kernel/gideon.config" >/dev/null)
make -C "$src" O="$obj" ARCH=x86_64 olddefconfig >/dev/null

# Fail loudly if kconfig silently dropped an option we depend on.
bad=0
while IFS= read -r line; do
    [[ "$line" =~ ^CONFIG_([A-Z0-9_]+)=(.*)$ ]] || continue
    name="CONFIG_${BASH_REMATCH[1]}" want="${BASH_REMATCH[2]}"
    if [[ "$want" == n ]]; then
        grep -q "^$name=" "$obj/.config" && { warn "$name should be unset"; bad=1; }
    else
        grep -qx "$name=$want" "$obj/.config" || { warn "$name=$want not applied (got: $(grep "^$name=" "$obj/.config" || echo unset))"; bad=1; }
    fi
done < "$GIDEON_ROOT/config/kernel/gideon.config"
(( bad == 0 )) || die "kernel config fragment not fully applied (missing dependencies in fragment?)"

log "Building kernel with $JOBS jobs (this takes a while)"
make -C "$src" O="$obj" ARCH=x86_64 -j"$JOBS" bzImage > "$BUILD_DIR/kernel-build.log" 2>&1 \
    || { tail -40 "$BUILD_DIR/kernel-build.log"; die "kernel build failed (full log: build/kernel-build.log)"; }

cp "$obj/arch/x86/boot/bzImage" "$out"
cp "$obj/.config" "$OUT_DIR/kernel.config"
stamp_set kernel "$key"
ok "kernel: $(make -s -C "$src" O="$obj" kernelrelease) -> build/out/vmlinuz"
