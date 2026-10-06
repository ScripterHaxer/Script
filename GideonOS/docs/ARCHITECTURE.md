# GideonOS Architecture

GideonOS is a desktop operating system built on the Linux kernel. Linux, its drivers, and
proven low-level userspace components handle the hard problems. The parts a user actually
experiences are GideonOS's own: the compositor, desktop shell, system applications, package
format, update system, installer, configuration system and branding.

This document records the architecture, the decisions behind it, and what is **implemented
today** versus **planned**. If the two ever disagree, this file is wrong and must be fixed.

---

## 1. Layer model

```
┌──────────────────────────────────────────────────────────────────────┐
│ Gideon system applications   Files · Settings · Terminal · Monitor … │  planned (M6)
├──────────────────────────────────────────────────────────────────────┤
│ Gideon Desktop Shell         panel · launcher · notifications · lock │  planned (M5)
│   (separate processes; Wayland layer-shell + gideon-shell protocol)  │
├──────────────────────────────────────────────────────────────────────┤
│ Gideon Compositor            Rust + Smithay; window mgmt, input,     │  planned (M4)
│                              outputs, decorations, IPC               │
├──────────────────────────────────────────────────────────────────────┤
│ Wayland · DRM/KMS · GBM/EGL (Mesa) · libinput · libxkbcommon · libseat│  planned (M3)
├──────────────────────────────────────────────────────────────────────┤
│ Platform services   session/seat · udev · D-Bus · network · audio ·  │  M2 basic, then M3+
│                     time · logging · storage · power                 │
├──────────────────────────────────────────────────────────────────────┤
│ System init         BusyBox init + /usr/lib/gideon/rc.boot           │  IMPLEMENTED (M1)
├──────────────────────────────────────────────────────────────────────┤
│ Minimal userspace   static BusyBox 1.37.0                            │  IMPLEMENTED (M1)
├──────────────────────────────────────────────────────────────────────┤
│ Linux 6.18 LTS      upstream source, x86_64_defconfig + fragment     │  IMPLEMENTED (M1)
├──────────────────────────────────────────────────────────────────────┤
│ Bootloader          GRUB 2, hybrid BIOS + UEFI ISO                   │  IMPLEMENTED (M1)
└──────────────────────────────────────────────────────────────────────┘
```

### Target boot flow (final)

```
Firmware (UEFI/BIOS) → GRUB (Gideon theme, A/B slot selection)
 → Linux kernel + initramfs (mount root slot, verify, switch_root)
 → init (PID 1) → udev coldplug → platform services
 → gideon-greeter (login) → session: gideon-compositor
 → gideon-shell processes → desktop ready
```

### Current boot flow (milestone 1, verified in QEMU)

```
GRUB (build/GideonOS.iso, El Torito BIOS + EFI) → /boot/vmlinuz + /boot/initramfs.img
 → kernel unpacks initramfs as the root filesystem → /init (BusyBox init)
 → /usr/lib/gideon/rc.boot: mount proc/sys/dev/devpts/run/tmp, hostname,
   mdev coldplug + hotplug, loopback, start /etc/gideon/services.d/*
 → root shell on tty1 and ttyS0
```

---

## 2. Development environment (inspected 2026-10-06)

| Item | Finding |
|---|---|
| Host OS | Ubuntu 24.04.5 LTS, x86_64, 4 cores, 15 GiB RAM |
| Repository | `scripterhaxer/script` holds unrelated Lua scripts at its root. GideonOS lives entirely under `GideonOS/` and does not touch them. |
| C/C++ | gcc 13.3, clang, make, cmake, ninja (meson missing) |
| Rust | rustc / cargo 1.97 (rustup) |
| QEMU | not installed initially; `qemu-system-x86` 8.2.2 installed via apt |
| KVM | **unavailable** (`/dev/kvm` missing). QEMU uses TCG software emulation, so boots take tens of seconds, not seconds |
| Bootloader tools | grub-mkrescue, xorriso, mtools, grub-pc-bin, grub-efi-amd64-bin, OVMF installed via apt |
| Kernel build deps | bc, bison, perl present; flex, libelf-dev, libssl-dev, cpio installed via apt |
| Cross-compilers | none needed. Host and target are both x86_64 |
| Wayland dev libraries | **not installed** (only pixman, udev .pc files). Needed from M3; will be built for the target, not taken from the host |
| Network | kernel.org and busybox.net reachable through the HTTPS proxy |

`tools/check-deps.sh` reproduces these checks on any host and prints the exact package names
for apt, dnf or pacman.

---

## 3. Key decisions

Each decision lists the alternatives and why they were rejected.

### D1. Kernel: upstream LTS, from source, configuration as a fragment
* Linux **6.18.55 LTS**, pinned by URL + SHA-256 in `config/versions.env`.
* Configuration: upstream `x86_64_defconfig` plus `config/kernel/gideon.config`. The build
  **fails** if Kconfig silently drops any fragment option, so the config cannot drift unnoticed.
* M1 builds everything in (no modules). Real hardware (M3+) needs modules and
  `linux-firmware`; the stage will then install modules into the root image.
* No kernel fork. If patches ever become necessary they go in `kernel/patches/` with a stated
  reason.

### D2. Base userspace: BusyBox now, Buildroot as the third-party build engine from M3
* **M1–M2**: a static BusyBox gives a complete, auditable minimal userspace (≈1 MB) with no libc
  runtime dependency.
* **M3+** needs a real libc and a large graphics stack (Mesa, Wayland, libinput, libxkbcommon,
  libdrm, fonts, D-Bus, PipeWire …). Writing and maintaining hundreds of cross-build recipes
  ourselves would add little value, so we adopt **Buildroot** as the build engine for
  *third-party* userspace, via a `BR2_EXTERNAL` tree (`system/buildroot/`) that we own.
  GideonOS components are Buildroot packages in that tree, built with cargo.
* Rejected: *Alpine/Debian/Ubuntu rootfs* (the result would be a derivative distro with its own
  package manager fighting our image-based update model); *hand-written recipes for
  everything* (maintenance burden); *Yocto* (much heavier than we need).
* Buildroot builds images, not a package-managed system. That matches D5: the OS is an
  immutable image and applications are installed separately.

### D3. Init and services
* **M1–M2**: BusyBox `init` + `/usr/lib/gideon/rc.boot`. Services are executable scripts in
  `/etc/gideon/services.d/`, started in lexical order with `start` and stopped in reverse with
  `stop`. This is deliberately simple and enough for logging, DHCP, time and mdev.
* **M3 recommendation: systemd** as PID 1, with udevd, logind, journald and timesyncd. The
  graphical desktop needs seat and session management (libseat → logind), and PipeWire,
  NetworkManager, polkit, UDisks2, UPower and Flatpak all integrate with it. Avoiding it would mean
  re-solving these with less-tested replacements (eudev + seatd + elogind), which goes against
  the rule *"do not sacrifice stability just to avoid existing Linux components."*
  GideonOS services become systemd units, and the M2 rc scripts are retired at that point.
  The switch happens at the start of M3 and is recorded here when it is made.

### D4. Graphics and compositor
* DRM/KMS for modesetting, GBM + EGL/GLES (Mesa) for rendering, libinput for input,
  libxkbcommon for keymaps, libseat for device access. No X11 server. XWayland support
  for legacy X11 apps comes later and stays optional.
* **Compositor: Rust on [Smithay](https://github.com/Smithay/smithay).** Smithay is a maintained
  compositor *library* (used by niri and COSMIC), comparable to wlroots in C. It provides the
  protocol plumbing and DRM/libinput backends while we own all policy: window management,
  layout, decorations, animations and shortcuts. Rust suits a long-running process
  that every app depends on.
  Rejected: *wlroots/C* (equally viable, but memory-safety argues for Rust); *from scratch on
  libwayland-server* (years of work for no user-visible gain); *forking an existing compositor*
  (would not be ours).
* Development backend: Smithay's winit/nested backend lets the compositor run inside a window
  on a dev host. KMS backend in QEMU via `virtio-gpu`/`bochs` (already enabled in the kernel).

### D5. Filesystem and update model: image-based A/B system
```
Installed disk (GPT):
  ESP        FAT32   512 MiB   bootloader, kernels per slot
  ROOT_A     erofs/squashfs, read-only   /usr (+ minimal /)
  ROOT_B     erofs/squashfs, read-only   inactive slot for updates
  DATA       ext4    rest      /etc overlay, /var, /home, /opt/gpk apps
```
* The OS is a signed, read-only image. `gideon-update` writes the **inactive** slot, verifies it,
  then switches the bootloader default. Boot counting falls back to the previous slot if the
  new one fails to reach "desktop ready". An interrupted update leaves the system on its old
  slot, never half-updated.
* Kernel and desktop updates are just new images. Applications (gpk, Flatpak) live on DATA and
  update independently.
* The live ISO (M1) uses the initramfs as the whole root filesystem. M2 moves to a squashfs root
  image mounted by the initramfs, which is the first step toward this layout.

### D6. Desktop shell: separate processes, standard protocols first
* The compositor and the shell are **separate processes**, so a shell crash does not kill the
  session. The compositor restarts it.
* Shell components: `gideon-panel` (taskbar, tray, clock, indicators, quick settings),
  `gideon-launcher` (start menu + search), `gideon-notifyd` (`org.freedesktop.Notifications`),
  `gideon-background`, `gideon-lock`, `gideon-overview`.
* Communication: standard Wayland protocols first (`wlr-layer-shell`,
  `ext-foreign-toplevel-list`/`wlr-foreign-toplevel-management`, `ext-workspace`,
  `ext-session-lock`, `xdg-activation`, `ext-image-copy-capture`). Where nothing standard
  fits, a private `gideon-shell-v1` Wayland protocol is used. Non-display control (settings
  reload, scripting, tests) goes through a JSON-over-Unix-socket IPC at
  `$XDG_RUNTIME_DIR/gideon-compositor.sock`.
* **UI toolkit:** recommendation is Rust + [iced](https://iced.rs) wrapped in our own `gideon-ui`
  crate, which holds the design tokens from `docs/DESIGN.md` and shared widgets, so the
  shell and every system app look the same. A one-week prototype at M5 start (panel + launcher)
  confirms this or falls back to Slint/GTK4. Third-party GTK/Qt apps run natively on Wayland
  and get a matching theme later.

### D7. Configuration system
Three layers, highest precedence last:
`/usr/share/gideon/defaults/` (immutable vendor defaults, part of the image) →
`/etc/gideon/` (machine-wide) → `~/.config/gideon/` (per user).
Files are TOML, one file per domain (`display.toml`, `input.toml` …). Tools: `gideonctl config
get|set|reset`. Running components get live changes through the compositor IPC or D-Bus.

### D8. Applications: `.gpk` + full Linux compatibility
* A `.gpk` is a **declarative** archive: `manifest.toml` (id, version, arch, permissions,
  runtime requirement, exports), a payload tree, and an ed25519 signature over a hash of
  both. **No install scripts are ever executed.** Installing means verify → extract to
  `/var/lib/gpk/apps/<id>/<version>/` → atomically switch `current` → export desktop entries
  and icons via an exports directory on `XDG_DATA_DIRS`. Removal deletes exactly what the manifest
  declared.
* Dependencies are on versioned *runtimes* (the GideonOS platform ABI), not individual
  libraries. This avoids dependency hell, as Flatpak does.
* Permissions (network, home, devices, …) are enforced at launch by `gpk run` through
  **bubblewrap** (namespaces + seccomp). This is not a security claim until M11 tests prove it.
* Compatibility: plain ELF binaries run as on any Linux. **Flatpak** (with Flathub) and
  **AppImage** are supported as-is. Wine/Proton is investigated later and never reimplemented.

### D9. Networking, audio, storage, power (M3+)
NetworkManager (Wi-Fi via iwd or wpa_supplicant) with its D-Bus API driving our network UI;
PipeWire + WirePlumber for audio; UDisks2 for removable drives in Files; UPower + logind for
battery, suspend and power buttons. These are existing components. GideonOS writes the UI and the policy.

### D10. Security model
Linux mechanisms only: users/groups (desktop user is non-root, in `wheel`), file permissions,
polkit for privileged actions, capabilities for services, namespaces + seccomp (bubblewrap) for
apps, signed OS images and packages, and Secure Boot (shim) later. AppArmor is evaluated in M11.
**Current state (M1):** the live image has a passwordless root shell. That is a development
image and is not secure.

---

## 4. Repository layout

```
GideonOS/
├── build.sh, run.sh       entry points
├── config/                pinned versions (versions.env), kernel + BusyBox config fragments
├── boot/grub/             bootloader configuration
├── system/rootfs/         files installed verbatim into the root filesystem
│                          (/etc, /usr/lib/gideon …)
├── tools/
│   ├── lib/common.sh      shared build helpers (fetch + verify, stamps, reproducibility env)
│   ├── build/*.sh         build stages: kernel, busybox, rootfs, iso
│   └── check-deps.sh      host dependency detection
├── tests/                 automated tests (QEMU serial-console harness in tests/lib/)
├── docs/                  ARCHITECTURE, ROADMAP, DESIGN
└── build/                 all generated output (git-ignored)
```

Planned and created only when work on them begins (so there are no placeholder directories):
`components/compositor`, `components/desktop`, `components/apps`, `components/ui` (gideon-ui),
`components/gpk`, `components/update`, `components/installer`, `system/buildroot`, `kernel/patches`.

The suggested top-level `kernel/` and `userspace/` directories became `config/kernel` and
`system/`. We do not keep a kernel source tree or fork, only configuration. User-facing
components are grouped under `components/`.

## 5. Build system

`./build.sh` runs the stages in order. Each stage is a standalone script in `tools/build/`:

| Stage | Input | Output | Rebuilds when |
|---|---|---|---|
| kernel | Linux tarball (SHA-256 verified), `config/kernel/*` | `build/out/vmlinuz` | fragment, version or script changes |
| busybox | BusyBox tarball (verified), `config/busybox/*` | `build/out/busybox` | same |
| rootfs | `system/rootfs/`, BusyBox | `build/out/initramfs.img` | always (seconds) |
| iso | kernel, initramfs, `boot/grub` | `build/GideonOS.iso` | always (seconds) |

Reproducibility measures: pinned and verified sources; `SOURCE_DATE_EPOCH` (from the last git
commit) drives `KBUILD_BUILD_TIMESTAMP`, cpio mtimes and ISO file times; fixed build
user/host; the cpio list is sorted and every file is owned by root regardless of the builder;
`gzip -n`. The initramfs is bit-for-bit reproducible across rebuilds (tested); a full
from-scratch ISO comparison has not been done yet.

The whole build runs **without root privileges**. Device nodes and ownership are declared in
the cpio list (`gen_init_cpio`), not created on the host.

## 6. Testing

`tests/boot_test.py` boots the ISO in QEMU under **BIOS and UEFI (OVMF)**, drives the serial
console, checks PID 1, boot completion, kernel identity, os-release, mounts, FHS layout,
writable /tmp, device nodes, virtio-net detection and loopback, then requires a clean ACPI
power-off. Serial logs go to `build/test-logs/`. Later milestones add tests to the same
harness (services, users, gpk, compositor startup via IPC, and so on).
