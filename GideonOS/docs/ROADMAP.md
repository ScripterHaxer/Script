# GideonOS Roadmap

Every milestone ends with something bootable and testable in QEMU, an automated test, a
git commit describing what actually works, and an updated status here.

Status legend: **DONE** (built + tested) · **IN PROGRESS** · **PLANNED**

| # | Milestone | Status |
|---|---|---|
| M1 | Bootable minimal OS | **DONE** |
| M2 | System foundation | **DONE** |
| M3 | Graphical foundation | **DONE** |
| M4 | Gideon Compositor | PLANNED (next) |
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
* BusyBox init + `rc.boot`: kernel filesystems, mdev coldplug, hostname, loopback, a service hook
  directory, and root shells on tty1 and ttyS0.
  *Correction (found in M2):* M1 also wrote `/proc/sys/kernel/hotplug`, but the kernel has no
  uevent helper (`CONFIG_UEVENT_HELPER` unset), so M1 had **no** hotplug handling, only
  devtmpfs nodes. M2 adds real hotplug through `mdev -d` (netlink).
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

## M2 — System foundation — DONE

**Delivered** (GideonOS 0.2.0)
1. **Root image**: reproducible read-only squashfs (`/gideon/rootfs.squashfs`) under a tmpfs
   overlay. The initramfs (`system/initramfs/init`) finds the medium by matching the root image
   checksum and `switch_root`s. `gideon.verify=1` hashes the image first (boot menu entry, tested
   with a tampered image). Rescue shell on failure or with `gideon.break=`.
2. **Service manager** `gideon-service` (start/stop/restart/status/list/enable/disable): runsv
   supervision with automatic restart, `requires=` dependencies with cycle detection, templated
   instances (`dhcp@eth0`), per-service logs in `/var/log/gideon/`, boot/shutdown ordering.
3. **Logging**: `syslog` (rotation via `logging.max_size_kb` / `logging.rotate`) + `klog`.
4. **Networking**: `network` brings up wired interfaces (auto-detected or configured). `dhcp@IF`
   (udhcpc) applies the lease, default route and `/run/gideon/resolv.conf`. IPv6 SLAAC by the
   kernel. Verified: lease, route, DNS resolution, IPv6 address.
5. **Time**: `clock` (RTC → system clock, written back at shutdown), `ntp` (servers from
   `time.ntp_servers`), POSIX time zone from `time.timezone`.
6. **Users**: getty + login with SHA-512 passwords. Root is refused on all terminals and reached
   via `su`. Live user `gideon` (uid 1000, wheel). `gideon-user add|del|passwd|list`.
7. **Storage**: removable/USB media auto-mounted at `/media/<label>` (`nosuid,nodev`, FAT/exFAT
   writable by group `users`) on coldplug and hotplug, and unmounted on removal.
8. **Power**: ACPI power button → clean shutdown (`acpid`).
9. **Sessions**: login's pre-setuid hook (as root) creates `/run/user/UID` (0700) and records
   the session. `gideon-session` is the users' login shell (exports `XDG_RUNTIME_DIR`).
   `gideon-session --list` shows active sessions.
10. **Configuration**: `gideon-config` with vendor defaults → `/etc/gideon` → `~/.config/gideon`.
11. **Tests**: `./build.sh test` runs 49 checks under BIOS and UEFI, including USB hotplug via
    the QEMU monitor and power-button shutdown.

**Deviations from the plan**
* "Service framework with `requires=` headers" became shell-fragment definitions on runit
  (`runsv`/`svlogd`), because supervision and restart come for free and are well tested.
* `ntpd` is a service toggled with `gideon-service enable|disable ntp` rather than a config flag.
* Time zones are POSIX TZ strings. The zoneinfo database comes with the M3 libc/tzdata.

**Upstream issues found and handled**
* BusyBox 1.37.0 `syslogd` silently drops every `/dev/log` message unless `-L` is passed: the
  implicit local-logging flag is set in `option_mask32`, but `syslogd_init()` returns the getopt
  result without it. Worked around with `-L` (documented in the service file).
* BusyBox's default password hash is DES. Set to SHA-512 in `config/busybox/gideon.config`.
* BusyBox `mountpoint` misreports overlayfs directories as mount points. `rc.boot` uses
  `/proc/mounts`.

**Known limitations**
* The live system keeps changes in RAM only (persistence arrives with the installer, M12).
* All services run as root. No capability dropping until M11.
* BusyBox is setuid root (needed for su/passwd/login). Reviewed in M11.
* The session record directory and `/run/user` are managed without logind. Replaced in M3.
* NTP cannot be verified in this sandbox (outbound NTP is not reachable). The daemon runs and
  resolves servers, but synchronisation itself is untested.
* No Wi-Fi (M9). No firmware or modules for real hardware yet (M3).

## M3 — Graphical foundation — DONE

**Delivered** (GideonOS 0.3.0)
1. **systemd 258** replaces BusyBox init and the M2 service framework (decision recorded in
   ARCHITECTURE D3, with a mapping of every M2 piece to its replacement).
2. **Buildroot 2026.02.3 LTS** builds the userspace through our `system/buildroot` external tree:
   glibc (Bootlin toolchain), systemd, PAM, util-linux, shadow, Mesa 26.0, libdrm, Wayland
   1.24, libinput, libxkbcommon, seatd/libseat (logind), fontconfig + DejaVu, Weston 14, tzdata,
   linux-firmware (i915, amdgpu), and our `gideon-gfx-probe`.
3. **Kernel modules** (i915, amdgpu, nouveau) and GPU firmware in the root image.
4. **Graphical session**: the live user is logged in on tty1 and `gideon-session` starts the
   interim compositor (Weston: DRM/KMS, libinput, libseat → logind) with GideonOS colours. A
   failed compositor falls back to a text shell with its log.
5. Display settings: `display.mode`, `display.scale`, `display.renderer`, `display.background`,
   `input.keyboard_layout` (applied at session start). The `session.*` keys control the
   graphical session and autologin.
6. Ported and improved system pieces: networkd/resolved (DHCP, IPv6, DNS stub), timesyncd,
   journald, logind power button, udev-driven USB automount, `gideon-config apply`, zoneinfo time
   zones, `gideon-user` on shadow's tools, PAM stack.
7. `components/gfx-probe`: Wayland test client (wl_shm and EGL/GLES2 paths, input reporting).
8. **Tests**: 72 checks per firmware (BIOS + UEFI). They cover pixel-verified rendering via
   CPU and EGL, keyboard/pointer routing, two displays, mode/scale changes, and all M2 checks
   ported to systemd. `./run.sh` shows the desktop in a window (`--outputs 2` for two monitors).

**Deviations from the plan**
* Interim compositor: Weston (desktop shell) rather than cage/kiosk, because it gives a usable
  terminal and launcher until M4. It is still only a stand-in.
* Resolution changes are tested through configuration + session restart. *Runtime* output
  reconfiguration (a protocol like wlr-output-management) belongs to gideon-compositor (M4).
* Mesa hardware drivers for Intel (iris/crocus) and AMD (radeonsi) are **not included**: they
  need LLVM (deferred; see ARCHITECTURE D4). Those GPUs get KMS display with CPU rendering.
* The GPU stack could only be validated in QEMU (virtio-gpu). No physical hardware has been
  tested.

**Upstream issues found and handled**
* `pam_securetty` silently allows kernel consoles (e.g. `console=ttyS0`) unless `noconsole` is
  given. Root could log in on the serial console until the test caught it.
* systemd-networkd + `systemd-networkd-persistent-storage.service`: after a networkd crash the
  two restart jobs wait on each other (networkd stays down). The unit is masked on the live system.
* Buildroot 2026.02 lets crocus be selected without LLVM; Mesa 26 then fails to configure.
* Buildroot's default BusyBox config lacks `stat`, `timeout`, `pgrep`/`pkill` (enabled via
  fragment). `getent` is not shipped with the external toolchain (scripts read /etc/passwd).
* QEMU (headless) enables only the first virtio-gpu head. Multi-display tests run QEMU's GTK UI
  on Xvfb with zoom-to-fit, otherwise the window size keeps overriding the guest's mode.
* Our own bugs found by the tests: the probe lacked `wl_pointer` v5 handlers (libwayland aborted
  it on the first pointer event), and its Makefile flags were overridden by Buildroot's
  command-line `CFLAGS` (EGL silently compiled out).

**Known limitations**
* No hardware GL for Intel/AMD yet (LLVM). Weston composites with pixman by default.
* First build takes about 1–1.5 h (Buildroot); the ISO is 106 MB (137 MB of firmware uncompressed).
* Live system: changes in RAM only. Autologin is on (live image only).
* No audio, no Wi-Fi, no polkit desktop authorization, no service sandboxing yet.
* Root image reproducibility is only partial: a from-scratch build and a later incremental
  rebuild produced different images (ARCHITECTURE §5). Investigate before M10.

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

Replaces the M3 interim compositor (Weston). The M3 test harness carries over: compositor startup,
pixel checks, input routing via the QEMU monitor, multi-head via Xvfb, and gfx-probe as the client.

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
