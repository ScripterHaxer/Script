# GideonOS

GideonOS is a desktop operating system built on the Linux kernel, with its own userspace,
compositor, desktop shell, system applications, package format, update system and installer.
Linux and proven low-level components do the hard work (drivers, filesystems, networking,
Wayland); the experience on top is GideonOS's own.

**Current state: Milestone 1 — a bootable minimal system.** It builds a GideonOS ISO from
pinned upstream sources and boots in QEMU (BIOS and UEFI) to a shell. There is no graphical
desktop yet; see [docs/ROADMAP.md](docs/ROADMAP.md) for what comes next and in what order.

## Quick start

```sh
cd GideonOS
tools/check-deps.sh           # lists anything missing + the exact install command
tools/check-deps.sh --install # optional: install it for you (apt / dnf / pacman)

./build.sh                    # -> build/GideonOS.iso   (first build ≈ 10–15 min: compiles Linux)
./run.sh                      # boot it in QEMU (window if available, else this terminal)
./run.sh --serial             # force serial console here; Ctrl-a x quits QEMU
./run.sh --uefi               # boot via UEFI firmware (OVMF)
./build.sh test               # automated BIOS + UEFI boot test
```

Inside the guest you get a root shell. `poweroff` shuts it down.

## What's in the image

| Component | Version / source |
|---|---|
| Kernel | Linux 6.18.55 LTS, `x86_64_defconfig` + `config/kernel/gideon.config` |
| Userspace | BusyBox 1.37.0, static |
| Init | BusyBox init + `/usr/lib/gideon/rc.boot` (services in `/etc/gideon/services.d`) |
| Bootloader | GRUB 2, hybrid BIOS/UEFI ISO |

All sources are SHA-256 verified (`config/versions.env`). The build needs no root, and the
initramfs is bit-for-bit reproducible.

## Layout

```
build.sh  run.sh     entry points
config/              version pins, kernel + BusyBox config fragments
boot/grub/           boot menu
system/rootfs/       files installed into the root filesystem
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

The M1 image logs in as **root without a password**. It is for development in a VM only.
