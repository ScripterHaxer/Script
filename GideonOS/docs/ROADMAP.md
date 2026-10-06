# GideonOS Roadmap

Every milestone ends with something bootable and testable in QEMU, an automated test, a
git commit describing what actually works, and an updated status here.

Status legend: **DONE** (built + tested) · **IN PROGRESS** · **PLANNED**

| # | Milestone | Status |
|---|---|---|
| M1 | Bootable minimal OS | **DONE** |
| M2 | System foundation | PLANNED (next) |
| M3 | Graphical foundation | PLANNED |
| M4 | Gideon Compositor | PLANNED |
| M5 | Gideon Desktop Shell | PLANNED |
| M6 | System applications | PLANNED |
| M7 | Application packaging (`gpk`) | PLANNED |
| M8 | Software compatibility | PLANNED |
| M9 | Networking | PLANNED |
| M10 | Updates | PLANNED |
| M11 | Security | PLANNED |
| M12 | Installer | PLANNED |
| M13 | First-boot experience | PLANNED |

---

## M1 — Bootable minimal OS — DONE

**Delivered**
* `./build.sh` builds Linux 6.18.55 LTS and BusyBox 1.37.0 from verified sources, assembles an
  FHS root filesystem, packs the initramfs rootless and reproducibly, and creates the hybrid
  BIOS + UEFI `build/GideonOS.iso` with GRUB.
* `./run.sh` boots it in QEMU (window, or serial with `--serial`; `--uefi` for OVMF).
* BusyBox init + `rc.boot`: kernel filesystems, mdev hotplug, hostname, loopback, a service hook
  directory, and root shells on tty1 and ttyS0.
* `tools/check-deps.sh` detects missing host tools and prints the install command for
  apt, dnf or pacman.
* `tests/boot_test.py`: automated BIOS + UEFI boot, 12 system checks, clean power-off.

**Known limitations**
* Root filesystem is the initramfs (RAM only, changes are lost at reboot).
* Passwordless root autologin. A development image only.
* No kernel modules or firmware, so only virtual/common hardware drivers built in by defconfig.
* No networking configuration (the interface is detected but not brought up).
* This dev environment has no KVM: QEMU runs in software emulation (≈16 s from power-on to shell).

---

## M2 — System foundation — PLANNED (next)

Goal: a real multi-user base system with modular services, still on BusyBox init (see D3).

1. **Root image**: squashfs root image on the ISO. A small initramfs finds it, mounts it under a
   tmpfs overlay and `switch_root`s. This prepares the A/B layout (D5).
2. **Service framework**: `gideon-service` (start/stop/status, dependencies via simple
   `requires=` headers, logs per service under `/var/log/gideon/`).
3. **Logging**: syslogd + klogd service, log rotation.
4. **Networking**: bring up wired interfaces with DHCP (udhcpc) and write DNS to
   `/etc/resolv.conf`. Test: guest reaches the QEMU user-net gateway and resolves DNS.
5. **Time**: RTC → system clock at boot, NTP service (ntpd), timezone in `/etc/gideon/time.toml`.
6. **Users**: `getty` + `login` with real passwords; a `gideon` user (uid 1000, `wheel`);
   `gideon-user add|del|passwd` wrapper; root login disabled on the console by default.
7. **Storage**: detect block devices and auto-mount labelled removable media under `/media`.
8. **Power**: ACPI power button → clean shutdown (`acpid` applet).
9. **Session placeholder**: `gideon-session` launched for the logged-in user (still a shell).
10. **Configuration**: `/usr/share/gideon/defaults` → `/etc/gideon` layering (D7), first
    consumer is hostname/time/network.

Tests: boot test extended to log in as `gideon`, check each service's status, DHCP lease +
DNS lookup, power-button shutdown (QEMU `system_powerdown`), user creation/deletion.

## M3 — Graphical foundation — PLANNED

1. Decision checkpoint: switch init to systemd (D3) and record the result in ARCHITECTURE.md.
2. Buildroot `BR2_EXTERNAL` tree (`system/buildroot/`) producing glibc, Mesa (virgl/llvmpipe),
   libdrm, Wayland, wayland-protocols, libinput, libxkbcommon, seatd/logind, fonts.
3. Kernel modules + firmware installed into the root image.
4. Bring up an **existing** Wayland compositor (cage or weston kiosk) running a test client,
   only to validate the stack: DRM/KMS, input, resolution changes, multiple QEMU heads.
5. Tests: compositor starts on KMS in QEMU, a client renders, screenshot via QEMU monitor
   (`screendump`) compared against expected colours.

## M4 — Gideon Compositor — PLANNED

`components/compositor/` (Rust + Smithay). Steps in order, each committed when tested:
1. Start on winit (nested) and KMS backends, show a solid Gideon background.
2. xdg-shell: launch one client and render it.
3. Keyboard + pointer input routing and focus.
4. Interactive move. 5. Resize. 6. Multiple windows and stacking.
7. Maximize, minimize, fullscreen; server-side decorations (xdg-decoration) in Gideon style.
8. Workspaces. 9. Multiple outputs, output configuration, fractional scaling.
10. Clipboard and drag-and-drop (data-device), screenshots (image-copy-capture),
    animations, vsync/presentation-time, keybinding config.
11. JSON IPC socket and a headless backend for automated tests.

Replaces the M3 interim compositor.

## M5 — Gideon Desktop Shell — PLANNED

`components/desktop/` + `components/ui/` (gideon-ui). Start with a toolkit prototype (D6).
Order: panel with clock + task list → launcher/start menu with app search → notifications →
quick settings (volume, network, brightness, battery) → system tray (StatusNotifierItem) →
lock screen → overview. Shell processes are restarted by the compositor if they crash.

## M6 — System applications — PLANNED

`components/apps/`. Order of necessity: **Terminal** (vte-free Rust terminal on
gideon-ui, or an embedded proven terminal core) → **Settings** (display, input, network, users,
about) → **Files** (browse, copy/paste/move/rename/delete, trash per freedesktop spec, search,
previews, drives via UDisks2) → Text Editor → System Monitor → Screenshot → Calculator →
About → Software Center (needs M7).

## M7 — Application packaging — PLANNED

`components/gpk/` (Rust). Format and rules as in ARCHITECTURE D8.
`gpk build|install|remove|list|update|verify|run`. Repository index signed with ed25519.
Tests: install/remove leaves no stray files, a tampered package is rejected, an interrupted install
leaves the previous version intact, permissions are enforced (network denied when not requested).

## M8 — Software compatibility — PLANNED

Flatpak + Flathub remote, AppImage (FUSE), plain ELF; desktop-entry integration in the launcher.
Investigate Wine/Proton packaging as a runtime (not reimplemented).

## M9 — Networking — PLANNED

NetworkManager (+ iwd) replaces the M2 DHCP service. Ethernet, Wi-Fi, IPv4/IPv6, DNS.
Network indicator and Settings → Network UI over NetworkManager's D-Bus API.
Tests in QEMU: wired DHCP, IPv6 SLAAC, connect/disconnect. Wi-Fi via `mac80211_hwsim`.

## M10 — Updates — PLANNED

Design review first (D5). `gideon-update`: fetch signed image → write inactive slot → verify →
switch boot default → boot counting → automatic rollback. App updates through gpk/Flatpak.
Tests: power-cut simulation at each step, rollback after a failed boot.

## M11 — Security — PLANNED

Non-root desktop user, polkit rules, service capabilities, bubblewrap+seccomp sandbox for gpk,
signed images and packages, AppArmor evaluation, Secure Boot via shim. Each claim needs a test
that demonstrates it.

## M12 — Installer — PLANNED

Graphical installer on gideon-ui: language, keyboard, timezone, disk selection, partitioning,
user, password, hostname, install, bootloader, first-boot config. **Never erases a disk without
an explicit confirmation that names the disk and model.** Installer logic is unit-tested
against loop-device disk images.

## M13 — First-boot experience — PLANNED

Plymouth-style boot splash (or our own via KMS), greeter, welcome app, desktop. Goal: no
terminal is ever visible during normal boot.
