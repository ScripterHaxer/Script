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
│ Platform services   logging · devices · network · time · storage ·   │  IMPLEMENTED (M2, basic)
│                     power button · users · sessions · config         │  D-Bus/audio/seat: M3+
├──────────────────────────────────────────────────────────────────────┤
│ System init         BusyBox init + rc.boot + gideon-service (runsv)  │  IMPLEMENTED (M1–M2)
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

### Current boot flow (milestone 2, verified in QEMU under BIOS and UEFI)

```
GRUB (build/GideonOS.iso, El Torito BIOS + EFI) → /boot/vmlinuz + /boot/initramfs.img
 → initramfs /init (system/initramfs/init):
     find the ISO9660 medium whose /gideon/rootfs.squashfs.sha256 matches the
     checksum built into this initramfs (optional full hash: gideon.verify=1)
     → mount squashfs read-only at /run/rootfs/lower
     → tmpfs /run/rootfs/rw → overlayfs at /newroot → switch_root
 → /sbin/init (BusyBox init) → /usr/lib/gideon/rc.boot:
     kernel filesystems, hostname from config, mdev coldplug,
     gideon-service boot  (devices, syslog, klog, clock, acpid, storage, network → dhcp@eth0, ntp)
 → getty → login (root refused on terminals) → session-setup (as root: /run/user/UID)
 → gideon-session (user's login shell; M3 turns this into the graphical session)
```

Boot options (kernel command line): `gideon.verify=1` (hash the root image before use),
`gideon.break=premount|preinit` (rescue shell in the initramfs), `gideon.timeout=N` (seconds to
wait for the boot medium), `gideon.debug=1` (trace rc.boot and service startup).

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
* **M1–M2**: BusyBox `init` + `/usr/lib/gideon/rc.boot` + **`gideon-service`** (M2), a small
  service manager on top of BusyBox's runit applets:
  * Definitions are shell fragments in `/usr/lib/gideon/services/NAME`. An admin copy in
    `/etc/gideon/services/` overrides them. Each sets `description`, `requires`, and either
    `type=daemon` + `exec` (foreground command, optional `prepare()` to compute it from
    configuration) or `type=oneshot` + `start()`/`stop()`.
  * Daemons run under `runsv`, so a crashed daemon is restarted (tested). Output goes through
    `svlogd` to `/var/log/gideon/NAME/current`. Oneshot output goes to `/var/log/gideon/NAME.log`.
  * Templates: `dhcp@` is instantiated as `dhcp@eth0`.
  * Dependencies start first, and cycles are detected. State changes are serialised by a lock
    that is never inherited by daemons, and nested starts are re-entrant.
  * `/etc/gideon/services.enabled` lists boot services. Shutdown stops them in reverse start order.
  * Services in M2: `devices` (mdev -d, netlink hotplug), `syslog`, `klog`, `clock` (RTC),
    `acpid` (power button), `storage` (removable media automount), `network`
    (link up, IPv6 SLAAC by the kernel, starts `dhcp@IF`), `dhcp@` (udhcpc), `ntp`.
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
* **Implemented for the live ISO (M2):** the OS is a read-only, reproducible squashfs image
  (`/gideon/rootfs.squashfs`, zstd) and the initramfs puts a RAM overlay on top. Changes
  last until reboot. The initramfs accepts only the image whose SHA-256 it was built with.
  A full hash check before mounting is opt-in (`gideon.verify=1`, also a boot-menu entry,
  tested against a tampered image). Always-on integrity for installed systems will be
  dm-verity (M10/M11). Hashing the whole image on every boot does not scale.

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
One file per domain (`system.conf`, `time.conf`, `network.conf`, `logging.conf`,
`storage.conf` …) in a flat TOML subset (`name = "string" | true | false | integer`), so the
Rust components can parse the same files with a standard TOML parser later.
**Implemented (M2):** `gideon-config get|set|reset|list [--user]`. Writes are atomic
(temp file + rename), keys are validated, and `list` shows which layer each value comes from.
Current consumers are hostname, time zone, RTC mode, NTP servers, DHCP, logging rotation and
storage automount. Running components get live changes through compositor IPC or D-Bus (M4+).

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
**Current state (M2), what is actually implemented and tested:**
* Accounts: `root` cannot log in on any terminal (empty `/etc/securetty`) and is reached with
  `su`. The live user `gideon` (uid 1000, `wheel`) has a password. Password hashes are
  SHA-512 crypt (BusyBox's default was DES and was changed). Live-image passwords are
  public by design (`config/live.conf`) and must never reach an installed system.
* `/etc/shadow` is 0600, homes are 0700, and the per-user runtime dir is created by root
  (via `login`'s pre-setuid hook) as 0700, so it can't be squatted. Removable media is
  mounted `nosuid,nodev`.
* BusyBox is installed **setuid root** so `su`, `passwd` and `login` work. BusyBox drops
  privileges for every applet not marked as needing them. This is a known trade-off of a
  single multi-call binary and is reviewed in M11 (split setuid helpers or shadow-utils).
* Not yet: polkit, capabilities for services (all run as root), sandboxing, signed images,
  Secure Boot, firewall. None of these should be assumed.

---

## 4. Repository layout

```
GideonOS/
├── build.sh, run.sh       entry points
├── config/                pinned versions (versions.env), kernel + BusyBox config fragments
├── boot/grub/             bootloader configuration
├── system/rootfs/         files installed verbatim into the root filesystem
│                          (/etc, /usr/lib/gideon/{rc.*,services/}, /usr/bin/gideon-*, …)
├── system/initramfs/      the initramfs /init (finds medium, mounts image, switch_root)
├── tools/
│   ├── lib/common.sh      shared build helpers (fetch + verify, stamps, reproducibility env)
│   ├── build/*.sh         build stages: kernel, busybox, rootfs, initramfs, iso
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
| rootfs | `system/rootfs/`, BusyBox, `config/live.conf` | `build/out/rootfs.squashfs` (+ `.sha256`) | always (seconds) |
| initramfs | `system/initramfs/init`, BusyBox, root image checksum | `build/out/initramfs.img` | always (seconds) |
| iso | kernel, initramfs, root image, `boot/grub` | `build/GideonOS.iso` | always (seconds) |

Reproducibility measures: pinned and verified sources; `SOURCE_DATE_EPOCH` (from the last git
commit) drives `KBUILD_BUILD_TIMESTAMP`, cpio mtimes and ISO file times; fixed build
user/host; the cpio list is sorted and every file is owned by root regardless of the builder;
`gzip -n`; mksquashfs with forced root ownership. The root image and the initramfs are
bit-for-bit reproducible across rebuilds (tested). A full from-scratch ISO comparison has not
been done yet.

The whole build is designed to run **without root privileges**. Device nodes and ownership are
declared in the cpio list (`gen_init_cpio`) and in mksquashfs actions/pseudo-definitions
(root ownership, setuid BusyBox, user-owned home), not created on the host. (In this dev
environment the build happens to run as root. Ownership forcing was tested with a
non-root-owned source tree.)

## 6. Testing

`tests/boot_test.py` (`./build.sh test`) boots the ISO in QEMU under **BIOS and UEFI (OVMF)**
with a FAT-formatted USB stick attached and drives the serial console and QEMU monitor
through 49 checks:
* login policy, sessions and privilege boundaries
* overlay/squashfs root
* all services active, crash restart, stop/start
* syslog + klogd
* DHCP, default route, DNS, IPv6 SLAAC
* config layering
* user create/login/delete
* USB coldplug, hotplug and unplug mounts
* ACPI power-button shutdown

Assets (USB images) are created rootless with dosfstools/mtools. Serial logs go to
`build/test-logs/`. The harness (`tests/lib/qemu.py`) is reused by later milestones.
