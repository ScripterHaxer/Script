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
│ Gideon Compositor            Rust + Smithay; window mgmt, input,     │  IMPLEMENTED (M4)
│                              outputs, decorations, IPC               │  (replaced Weston)
├──────────────────────────────────────────────────────────────────────┤
│ Wayland · DRM/KMS · GBM/EGL (Mesa) · libinput · libxkbcommon · libseat│  IMPLEMENTED (M3)
├──────────────────────────────────────────────────────────────────────┤
│ Platform services   udevd · journald · logind · networkd/resolved ·  │  IMPLEMENTED (M3, systemd)
│                     timesyncd · D-Bus · automount · config-apply     │  audio/NetworkManager: later
├──────────────────────────────────────────────────────────────────────┤
│ System init         systemd 258 (PID 1)                              │  IMPLEMENTED (M3)
├──────────────────────────────────────────────────────────────────────┤
│ Userspace           glibc · BusyBox · util-linux · shadow · PAM,     │  IMPLEMENTED (M3)
│                     built by Buildroot 2026.02 LTS (our BR2_EXTERNAL) │
├──────────────────────────────────────────────────────────────────────┤
│ Linux 6.18 LTS      upstream source, x86_64_defconfig + fragment,    │  IMPLEMENTED (M1, modules M3)
│                     modules + GPU firmware in the root image          │
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

### Current boot flow (milestone 4, verified in QEMU under BIOS and UEFI)

```
GRUB (build/GideonOS.iso, El Torito BIOS + EFI) → /boot/vmlinuz + /boot/initramfs.img
 → initramfs /init (system/initramfs/init, static BusyBox):
     find the ISO9660 medium whose /gideon/rootfs.squashfs.sha256 matches the
     checksum built into this initramfs (optional full hash: gideon.verify=1)
     → mount squashfs read-only at /run/rootfs/lower
     → tmpfs /run/rootfs/rw → overlayfs at /newroot → switch_root
 → systemd (PID 1): gideon-session-generator (tty1 autologin from session.autologin)
     → gideon-config-apply (hostname, time zone, networkd + timesyncd config)
     → udevd (devices, modules, firmware; removable media → gideon-automount@)
     → journald, logind, networkd, resolved, timesyncd
 → getty@tty1 (autologin, live image) → login (PAM: pam_systemd → logind session)
 → gideon-session → graphical-session → gideon-compositor --backend udev
     (DRM/KMS + pixman, libinput, libseat→logind; IPC socket in /run/user/1000)
 → background on every connected output; Super+Enter opens the terminal (foot)
 Serial console: serial-getty@ttyS0 → login → gideon-session → text shell
```

Boot options (kernel command line): `gideon.verify=1` (hash the root image before use),
`gideon.break=premount|preinit` (rescue shell in the initramfs), `gideon.timeout=N` (seconds to
wait for the boot medium). Standard systemd options (`systemd.unit=`, `systemd.debug_shell`) work too.

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
| Wayland dev libraries | not installed initially. The target's are built by Buildroot. Host copies (libwayland-dev, wayland-protocols, EGL/GLES headers, weston) were installed only to compile-check and smoke-test `components/gfx-probe` |
| Buildroot host tools | rsync, unzip, file, wget, patch, cpio, kmod (depmod) installed via apt |
| Multi-display tests | Xvfb + QEMU's GTK UI module (`qemu-system-gui`): QEMU enables extra virtio-gpu heads only when a UI reports them |
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
* VM-critical drivers (virtio, bochs/virtio-gpu, AHCI, NVMe, USB storage, HID) are built in.
  Real-hardware GPU drivers (i915, amdgpu, nouveau) are **modules** (M3), installed into
  `/usr/lib/modules` of the root image and loaded by udev after the root image, and its
  `/usr/lib/firmware` (Buildroot `linux-firmware`: i915, amdgpu), is available.
  The fragment also carries systemd's kernel requirements (cgroups, autofs, BPF, …).
* No kernel fork. If patches ever become necessary they go in `kernel/patches/` with a stated
  reason.

### D2. Base userspace: BusyBox (M1–M2), Buildroot as the third-party build engine (M3, done)
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
* **Implemented (M3):** Buildroot **2026.02.3 LTS** (pinned + SHA-256) with the prebuilt Bootlin
  x86-64 glibc toolchain (gcc 14). `system/buildroot/` holds:
  * `configs/gideonos_x86_64_defconfig`: the package selection. The build fails if Kconfig
    drops any line.
  * `package/`: GideonOS packages (`gideon-gfx-probe` builds from `components/gfx-probe`).
  * `board/gideonos/`: users table, a BusyBox fragment, `post-build.sh` (identity, kernel
    modules, unit enablement, masks) and `post-fakeroot.sh` (reproducible password hashes,
    ownership).

  The Buildroot overlay is `system/rootfs/`. Our kernel, initramfs and ISO stages stay outside
  Buildroot. Ownership and setuid bits are applied under fakeroot, so no root is needed.

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
* **M3, decided and implemented: systemd 258** as PID 1, with udevd, logind, journald,
  networkd + resolved, timesyncd, hostnamed/timedated/localed and D-Bus. The graphical
  desktop needs seat and session management (libseat → logind). PipeWire, NetworkManager,
  polkit, UDisks2, UPower and Flatpak all integrate with it. Avoiding it would mean re-solving
  these with less-tested replacements (eudev + seatd + elogind), which goes against
  *"do not sacrifice stability just to avoid existing Linux components."*
  M2's pieces were retired or ported:

  | M2 | M3 |
  |---|---|
  | `gideon-service` + runsv | systemd units (`systemctl`), Restart= policies |
  | mdev -d | systemd-udevd (+ hwdb) |
  | syslogd + klogd | journald (volatile on the live image) |
  | udhcpc + `dhcp@` | systemd-networkd, configured by `gideon-config-apply` |
  | ntpd | systemd-timesyncd |
  | acpid | logind `HandlePowerKey=poweroff` |
  | login pre-setuid hook | `pam_systemd` / logind (`/run/user/UID`, seat, device ACLs) |
  | `storage` service | udev rule → `gideon-automount@DEV.service` (BindsTo the device) |

  GideonOS-specific units: `gideon-config-apply.service`, `gideon-automount@.service`,
  `gideon-session-generator` (a systemd generator for tty1 autologin).
  Masked: `systemd-networkd-persistent-storage.service`. When networkd crashes, its restart
  job and this unit's restart wait on each other (found by the crash-restart test). The live
  system keeps no persistent network state.

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
* **Implemented (M4): `gideon-compositor`** (`components/compositor`, Smithay 0.7, Rust 1.88
  in Buildroot). It replaced the M3 interim Weston, which is no longer in the image.
  * Backends: **udev** (production): libseat session (logind), udev hotplug, libinput,
    DRM/KMS with one CRTC per connector, rendered by **pixman into DRM dumb buffers**, page flips
    paced by vblank. **winit** (cargo feature, development): nested GLES window on a host.
  * Protocols: wl_compositor, wl_subcompositor, xdg-shell, xdg-decoration (server side is
    forced), wl_shm, wl_seat, wl_output + xdg-output, wl_data_device (clipboard, drag and
    drop), primary selection, fractional-scale, viewporter.
  * Window management (`wm.rs`): placement, focus/raise, server-side decorations (title bar,
    close/maximize/minimize buttons, meridian accent line), move/resize (title-bar drag,
    client-requested, Super+drag), maximize, minimize, fullscreen, left/right tiling,
    9 workspaces, window cycling. Shortcuts as in DESIGN §9.
  * Outputs: laid out left to right; mode and scale from `display.mode`/`display.scale` at
    start, and changeable live over IPC. VT switching via logind.
  * Rendering: damage-tracked, 150 ms fade-in for new windows, software cursor, screenshots
    (Print key → `~/Pictures/*.png`, or IPC `screenshot`).
  * `graphical-session` starts the compositor. If it fails within 15 s with a real error (not
    a signal), the user gets a text shell and the log. SIGTERM exits cleanly with status 0.
* **Mesa 26.0** drivers in the image: softpipe (CPU), virgl (VMs), nouveau, svga. **iris,
  crocus, radeonsi and llvmpipe need LLVM**, because Mesa 26 compiles their OpenCL-C kernels with
  it. LLVM is deferred for build cost (hours on this host), so modern Intel and AMD GPUs
  currently have KMS and display, but no hardware GL. Buildroot 2026.02 does not mark crocus
  as needing LLVM; Mesa's configure rejects it, and we drop it.
* Renderer policy: the compositor composes on the **CPU (pixman)**, so it works on every GPU
  and in VMs. GPU composition (GBM + GLES) is a later optimisation and is not implemented
  yet. Clients can still use EGL/GLES (tested through Mesa on Wayland).

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
* **Implemented for the live ISO (M2):** the OS is a read-only squashfs image
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
  `$XDG_RUNTIME_DIR/gideon-compositor.sock`. **Implemented (M4):** one JSON request per
  connection (`{"cmd":…,"args":[…]}`), with the CLI client `gideon-compositor msg CMD ARGS`.
  Commands: version, outputs, windows, workspaces, pointer, workspace, focus, close,
  maximize, minimize, restore, fullscreen, tile-left/right, floating, move, resize,
  to-workspace, action, spawn, set-mode, set-scale, screenshot, reload-config, quit.
  The layer-shell and foreign-toplevel protocols are M5 work.
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
Current consumers (M3): hostname, time zone (zoneinfo names), network (DHCP, interfaces,
send hostname → systemd-networkd), NTP servers (→ timesyncd), storage automount, display
mode/scale/background, keyboard layout and terminal (read by gideon-compositor), and session graphical
mode and autologin. `gideon-config apply` (root) re-applies system settings live and restarts
only the affected services. Display settings apply when the graphical session starts.
The compositor re-reads its settings on IPC `reload-config`. Other running components get
live changes through IPC or D-Bus later.

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
M3 uses systemd-networkd + resolved for wired networking (DHCPv4, IPv6 SLAAC, DNS stub).
M9 adds NetworkManager (Wi-Fi via iwd or wpa_supplicant), whose D-Bus API drives our network UI;
PipeWire + WirePlumber for audio; UDisks2 for removable drives in Files; UPower + logind for
battery, suspend and power buttons. These are existing components. GideonOS writes the UI and the policy.

### D10. Security model
Linux mechanisms only: users/groups (desktop user is non-root, in `wheel`), file permissions,
polkit for privileged actions, capabilities for services, namespaces + seccomp (bubblewrap) for
apps, signed OS images and packages, and Secure Boot (shim) later. AppArmor is evaluated in M11.
**Current state (M3), what is actually implemented and tested:**
* Accounts: `root` cannot log in on any terminal. `/etc/securetty` is empty and
  `pam_securetty noconsole` is set. Without `noconsole`, pam_securetty implicitly allows kernel
  consoles such as `ttyS0`; an early M3 test caught exactly that. Root is reached with `su`
  (util-linux, PAM). The live user `gideon` (uid 1000, `wheel`) logs in automatically on tty1
  (live image only, `session.autologin`) and with a password elsewhere.
  Hashes are SHA-512 crypt. Live-image passwords are public by design (`config/live.conf`) and
  must never reach an installed system.
* PAM: one `system-auth` stack. Account tools (useradd/usermod/chpasswd …) are root-only, and
  anything without a policy hits `other` = deny. logind sessions come via `pam_systemd`.
* Files: `/etc/shadow` 0600, homes 0700, `/run/user/UID` 0700 (logind). Removable media
  is mounted `nosuid,nodev`. Device access goes through groups (`video`, `render`, `input`) plus
  logind's seat ACLs.
* setuid binaries: `su`, `passwd` and shadow's account tools (`BR2_PACKAGE_SHADOW_ACCOUNT_TOOLS_SETUID`).
  BusyBox is no longer setuid.
* Unprivileged users cannot manage services (no polkit rules: systemd denies). Tested.
* Not yet: polkit rules for desktop actions, service sandboxing directives, app sandboxing,
  signed images, Secure Boot, firewall, AppArmor. None of these should be assumed.

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
├── system/buildroot/      BR2_EXTERNAL tree: defconfig, GideonOS packages, image hooks
├── components/compositor/ gideon-compositor (Rust + Smithay; Cargo.lock pinned, vendored at build)
├── components/gfx-probe/  Wayland test client (wl_shm + EGL/GLES, input reporting)
├── tools/
│   ├── lib/common.sh      shared build helpers (fetch + verify, stamps, reproducibility env)
│   ├── build/*.sh         build stages: kernel, busybox, rootfs, initramfs, iso
│   └── check-deps.sh      host dependency detection
├── tests/                 automated tests (QEMU serial-console harness in tests/lib/)
├── docs/                  ARCHITECTURE, ROADMAP, DESIGN
└── build/                 all generated output (git-ignored)
```

Planned and created only when work on them begins (so there are no placeholder directories):
`components/desktop`, `components/apps`, `components/ui` (gideon-ui),
`components/gpk`, `components/update`, `components/installer`, `kernel/patches`.

The suggested top-level `kernel/` and `userspace/` directories became `config/kernel` and
`system/`. We do not keep a kernel source tree or fork, only configuration. User-facing
components are grouped under `components/`.

## 5. Build system

`./build.sh` runs the stages in order. Each stage is a standalone script in `tools/build/`:

| Stage | Input | Output | Rebuilds when |
|---|---|---|---|
| kernel | Linux tarball (SHA-256 verified), `config/kernel/*` | `build/out/vmlinuz`, `build/out/modules/` | fragment, version or script changes |
| busybox | BusyBox tarball (verified), `config/busybox/*` | `build/out/busybox` (static: initramfs + build-time `cryptpw`) | same |
| rootfs | Buildroot (verified) + `system/buildroot`, `system/rootfs/`, kernel modules, `config/live.conf` | `build/out/rootfs.squashfs` (+ `.sha256`) | packages once (≈1 h first build), then image only (≈1 min) |
| initramfs | `system/initramfs/init`, BusyBox, root image checksum | `build/out/initramfs.img` | always (seconds) |
| iso | kernel, initramfs, root image, `boot/grub` | `build/GideonOS.iso` | always (seconds) |

Reproducibility measures: pinned and verified sources; `SOURCE_DATE_EPOCH` (from the last git
commit) drives `KBUILD_BUILD_TIMESTAMP`, cpio mtimes and ISO file times; fixed build
user/host; the cpio list is sorted and every file is owned by root regardless of the builder;
`gzip -n`; Buildroot's `BR2_REPRODUCIBLE`; reproducible password salts. Status: the initramfs is
bit-for-bit reproducible (tested). Re-running the root image step on an existing Buildroot tree
is bit-for-bit identical (tested twice in M3). **However**, the image from a from-scratch build
differed from the one produced by the next incremental re-run. The cause is not investigated
yet; it is an open item for M10 (updates need reproducible images).

The whole build is designed to run **without root privileges**. Initramfs device nodes and
ownership are declared in the cpio list (`gen_init_cpio`). Root-image ownership, setuid bits
and the user table are applied by Buildroot under fakeroot (plus our `post-fakeroot.sh`).
Nothing is created as root on the host. In this dev environment the build happens to run
as root, so a fully unprivileged build has not been exercised since Buildroot was introduced.

## 6. Testing

`tests/boot_test.py` (`./build.sh test`) boots the ISO in QEMU under **BIOS and UEFI (OVMF)**
with a two-head virtio GPU, a USB tablet and a FAT USB stick. It drives the serial console
the QEMU monitor and QMP through 87 checks per firmware:
* login policy, logind sessions and privilege boundaries
* systemd state (no failed units), overlay/squashfs root, modules and firmware
* graphics:
  * gideon-compositor on DRM/KMS (IPC `version`), session locale, libinput keyboard and pointer
  * background pixels, client frames via wl_shm and **EGL/GLES (Mesa)**, decorations
  * **keyboard and pointer routing** proven by the client re-rendering; absolute pointer
    clicks through QMP `input-send-event`
  * window management: title-bar drag, maximize/tile/fullscreen/workspace shortcuts,
    foot via Super+Enter, Print screenshot PNG
  * **two displays**; **mode + scale change** over IPC and through `gideon-config`;
    VT switch to tty2 and back
* services: active, crash restart, stop/start; journal (user + kernel + logins)
* DHCP, route, resolved DNS, IPv6 SLAAC
* `gideon-config apply` (hostname, time zone)
* user create/login/delete
* USB coldplug, hotplug and unplug mounts
* power-button shutdown via logind

Screenshots come from the QEMU monitor (`screendump`, PPM) and are checked pixel by pixel
against `docs/DESIGN.md` colour tokens. QEMU enables virtio-gpu heads beyond the first only
when a UI reports them, so multi-display runs start QEMU's GTK UI on a private Xvfb
(zoom-to-fit, so the guest's chosen mode is not overridden by the window size). Without Xvfb
those checks are reported as SKIP, never as passed. Assets are created rootless, and serial
logs go to `build/test-logs/`.

`tests/compositor_test.py` runs the compositor nested (winit backend) on a private Xvfb with
xdotool and wl-clipboard: 33 checks covering protocols, decorations, focus, move/resize,
maximize/minimize/fullscreen, tiling, workspaces, shortcuts, clipboard, screenshots and IPC
errors. It needs `cargo build --release --features winit` and `make -C components/gfx-probe`
first, and runs in about a minute. `cargo test` covers config parsing.