# GideonOS

GideonOS is a desktop operating system built on the Linux kernel, with its own userspace,
compositor, desktop shell, system applications, package format, update system and installer.
Linux and proven low-level components do the hard work (drivers, filesystems, networking,
Wayland); the experience on top is GideonOS's own.

**Current state: Milestone 2 — system foundation (GideonOS 0.2.0).** The ISO boots in QEMU
(BIOS and UEFI) from a read-only root image to a text login, with supervised services
(logging, device hotplug, DHCP/DNS/IPv6, time, power button, USB automount), user accounts,
sessions and a layered configuration system. There is no graphical desktop yet; see
[docs/ROADMAP.md](docs/ROADMAP.md) for what comes next and in what order.

## Quick start

```sh
cd GideonOS
tools/check-deps.sh           # lists anything missing + the exact install command
tools/check-deps.sh --install # optional: install it for you (apt / dnf / pacman)

./build.sh                    # -> build/GideonOS.iso   (first build ≈ 10–15 min: compiles Linux)
./run.sh                      # boot it in QEMU (window if available, else this terminal)
./run.sh --serial             # force serial console here; Ctrl-a x quits QEMU
./run.sh --uefi               # boot via UEFI firmware (OVMF)
./build.sh test               # automated BIOS + UEFI system test (49 checks)
```

Log in as **`gideon` / `gideon`** (live image). Root cannot log in on a terminal. Use `su`
(root password `gideon`). Useful commands inside the guest:

```sh
gideon-service list                     # services and their state
gideon-config list                      # effective configuration and where it comes from
su -c 'gideon-config set system.hostname mybox'
su -c 'gideon-user add alice --admin' && su -c 'gideon-user passwd alice'
gideon-session --list                   # who is logged in
```

Shut down with `su -c poweroff` or the (virtual) power button.

## What's in the image

| Component | Version / source |
|---|---|
| Kernel | Linux 6.18.55 LTS, `x86_64_defconfig` + `config/kernel/gideon.config` |
| Userspace | BusyBox 1.37.0, static |
| Init | BusyBox init + `rc.boot` + `gideon-service` (runsv supervision) |
| Root filesystem | read-only squashfs image + RAM overlay, mounted by our initramfs |
| Bootloader | GRUB 2, hybrid BIOS/UEFI ISO |

All sources are SHA-256 verified (`config/versions.env`). The build is designed to need no
root, and the root image and initramfs are bit-for-bit reproducible.

## Layout

```
build.sh  run.sh     entry points
config/              version pins, kernel + BusyBox config fragments
boot/grub/           boot menu
system/rootfs/       files installed into the root filesystem (services, tools, defaults)
system/initramfs/    early-boot init that mounts the root image
tools/               build stages, dependency checker
tests/               QEMU-driven integration tests
docs/                ARCHITECTURE.md · ROADMAP.md · DESIGN.md
build/               generated output (git-ignored)
```

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): layers, key decisions and their alternatives,
  build system, testing.
* [docs/ROADMAP.md](docs/ROADMAP.md): milestones M1–M13 with status and acceptance tests.
* [docs/DESIGN.md](docs/DESIGN.md): the "Meridian" design system (colour, type, spacing,
  decorations, motion, shortcuts).

## Development image warning

The live image's passwords are public (`config/live.conf`), all services run as root, and
nothing is sandboxed yet. It is for development and VMs only. See the security notes in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (D10) for exactly what is implemented.
