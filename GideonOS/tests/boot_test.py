#!/usr/bin/env python3
"""GideonOS system test (milestones 1-3).

Boots build/GideonOS.iso in QEMU (BIOS and/or UEFI) with a two-head virtio GPU,
a USB tablet and a USB stick, then drives the serial console and QEMU monitor
through the whole system: boot, login policy, systemd services, journal,
networking, configuration, users, removable storage, kernel modules, the
graphical session (KMS, rendering, input routing, multiple outputs, mode and
scale changes) and power-button shutdown.

    tests/boot_test.py [--bios] [--uefi] [--iso PATH] [--timeout SECONDS]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
from assets import fat_image  # noqa: E402
from qemu import QemuVM, TestFailure  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_PW = "gideon"   # config/live.conf
CORE_UNITS = ("systemd-journald systemd-udevd systemd-logind systemd-networkd systemd-resolved "
              "systemd-timesyncd gideon-config-apply getty@tty1")
BG, TEAL, AMBER, RED = "0f1216", "3db8c6", "e8b04b", "f06a6a"   # docs/DESIGN.md tokens
PROBE_LOG = "/tmp/probe.log"
IFV = "IF=$(ls /sys/class/net | grep -vx lo | head -1); "
WL = "export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=$(cd /run/user/1000 && ls wayland-? | head -1)"


def wait_for(cond_cmd, seconds):
    """Shell snippet: poll cond_cmd once a second for up to `seconds`, then evaluate it."""
    return f"i=0; until {cond_cmd}; do i=$((i+1)); [ $i -ge {seconds} ] && break; sleep 1; done; {cond_cmd}"


class Suite:
    def __init__(self, vm):
        self.vm = vm
        self.failures = 0

    def check(self, desc, cmd, pred=lambda o: True, timeout=60):
        status, out = self.vm.run(cmd, timeout)
        if status == 0 and pred(out):
            print(f"  ok   {desc}")
            return out
        self.fail(desc, f"exit={status} output={out!r}")
        return None

    def expect_true(self, desc, cond, detail=""):
        if cond:
            print(f"  ok   {desc}")
        else:
            self.fail(desc, detail)
        return cond

    def ok(self, desc):
        print(f"  ok   {desc}")

    def fail(self, desc, why):
        self.failures += 1
        print(f"  FAIL {desc}: {why}")


def centre(img):
    return img.width // 2, img.height // 2


def wait_screen(vm, rgb, head=0, seconds=30):
    """Poll display head `head` until its centre pixel matches rgb; returns the last image."""
    deadline = time.monotonic() + seconds
    while True:
        img = vm.screendump(head)
        if img.matches(*centre(img), rgb) or time.monotonic() > deadline:
            return img
        time.sleep(1)


def start_probe(vm, args):
    vm.run(f"pkill -x gideon-gfx-probe; sleep 1; ({WL}; gideon-gfx-probe {args} > {PROBE_LOG} 2>&1 &)")


def graphics_checks(t, vm):
    t.check("graphical session (compositor) running for the live user on tty1",
            wait_for("pgrep -x weston >/dev/null", 90) + " && stat -c %U /proc/$(pgrep -x weston)", lambda o: o.endswith("gideon"), 120)
    t.check("Wayland socket in the user's runtime dir", "ls /run/user/1000/ | grep -x 'wayland-[0-9]'", lambda o: o.startswith("wayland-"))
    t.check("compositor drives the display through DRM/KMS",
            "grep -ciE 'drm backend|DRM: head|DRM: supports' /run/user/1000/gideon/compositor.log", lambda o: int(o) > 0)
    t.check("libinput sees keyboard and pointer",
            "grep -E 'device is a (keyboard|pointer)' /run/user/1000/gideon/compositor.log | sed 's/.*device is a //' | sort -u | tr '\\n' ' '",
            lambda o: o.split() == ["keyboard", "pointer"])

    img = wait_screen(vm, BG)
    t.expect_true("desktop background drawn in GideonOS colour (#0f1216)", img.matches(*centre(img), BG),
                  f"centre pixel {img.pixel(*centre(img))} size {img.width}x{img.height}")
    t.expect_true("panel drawn at the bottom of the screen", not img.matches(img.width // 2, img.height - 8, BG),
                  f"bottom pixel {img.pixel(img.width // 2, img.height - 8)}")

    # --- client rendering (CPU / wl_shm) -----------------------------------
    start_probe(vm, "--fullscreen")
    t.check("test client connects and draws (wl_shm)",
            wait_for(f"grep -q 'drawn color={TEAL}' {PROBE_LOG}", 30) + f" && cat {PROBE_LOG}", lambda o: "renderer=shm" in o, 60)
    img = wait_screen(vm, TEAL)
    t.expect_true("client frame reaches the screen (pixel-exact colour)", img.matches(*centre(img), TEAL),
                  f"centre pixel {img.pixel(*centre(img))}")
    outputs = vm.run(f"grep -o 'outputs=[0-9]*' {PROBE_LOG}")[1]

    # --- input routing -------------------------------------------------------
    vm.run(wait_for(f"grep -q 'keyboard focus' {PROBE_LOG}", 15))
    vm.monitor("sendkey a")
    t.check("keyboard input routed to the focused client",
            wait_for(f"grep -q 'key 30' {PROBE_LOG}", 15) + " && echo got", lambda o: o.endswith("got"), 30)
    img = wait_screen(vm, AMBER)
    t.expect_true("client re-rendered after the key press", img.matches(*centre(img), AMBER),
                  f"centre pixel {img.pixel(*centre(img))}")
    vm.monitor(f"mouse_move {img.width // 2} {img.height // 2}")
    vm.monitor("mouse_button 1")
    vm.monitor("mouse_button 0")
    t.check("pointer button routed to the client",
            wait_for(f"grep -q 'button 272' {PROBE_LOG}", 15) + " && echo got", lambda o: o.endswith("got"), 30)
    img = wait_screen(vm, RED)
    t.expect_true("client re-rendered after the click", img.matches(*centre(img), RED),
                  f"centre pixel {img.pixel(*centre(img))}")

    # --- GPU API path (Mesa EGL + GLES2) ----------------------------------
    start_probe(vm, "--fullscreen --egl")
    t.check("EGL/GLES2 context through Mesa",
            wait_for(f"grep -q 'drawn color={TEAL}' {PROBE_LOG}", 60) + f" && grep renderer= {PROBE_LOG}",
            lambda o: "renderer=egl" in o and "gl_renderer=" in o, 90)
    img = wait_screen(vm, TEAL, seconds=60)
    t.expect_true("GLES-rendered frame reaches the screen", img.matches(*centre(img), TEAL),
                  f"centre pixel {img.pixel(*centre(img))}")
    vm.run("pkill -x gideon-gfx-probe")

    # --- multiple displays -----------------------------------------------
    if not vm.multihead:
        print("  SKIP multiple displays: Xvfb not installed (QEMU shows one head without a UI)")
        return
    t.expect_true("two displays exposed to clients", outputs == "outputs=2", f"probe saw {outputs!r}")
    img1 = wait_screen(vm, BG, head=1, seconds=15)
    t.expect_true("second display shows the desktop", img1.matches(*centre(img1), BG),
                  f"head 1 centre pixel {img1.pixel(*centre(img1))} size {img1.width}x{img1.height}")


def mode_change_checks(t, vm):
    """As root: change display mode + scale via gideon-config, restart the session."""
    old = vm.run("pgrep -x weston")[1]
    t.check("set display mode 1024x768 and scale 2",
            "gideon-config set display.mode 1024x768 && gideon-config set display.scale 2 && echo set", lambda o: o.endswith("set"))
    vm.run("pkill -x weston")
    t.check("graphical session restarts with the new settings",
            wait_for(f"[ -n \"$(pgrep -x weston)\" ] && [ \"$(pgrep -x weston)\" != '{old}' ]", 60)
            + " && echo restarted", lambda o: o.endswith("restarted"), 90)
    vm.run(wait_for("ls /run/user/1000/wayland-? >/dev/null 2>&1", 30))
    deadline = time.monotonic() + 60
    while True:
        img = vm.screendump(0)
        if (img.width, img.height) == (1024, 768) or time.monotonic() > deadline:
            break
        time.sleep(2)
    t.expect_true("display runs at 1024x768", (img.width, img.height) == (1024, 768), f"screen is {img.width}x{img.height}")
    vm.run(f"runuser -u gideon -- sh -c '{WL}; gideon-gfx-probe > {PROBE_LOG} 2>&1 &'")
    t.check("clients see the new mode and output scale 2",
            wait_for(f"grep -q drawn {PROBE_LOG}", 30) + f" && grep 'output name' {PROBE_LOG} | head -1",
            lambda o: "mode=1024x768" in o and "scale=2" in o, 60)
    vm.run("pkill -x gideon-gfx-probe; gideon-config reset display.mode; gideon-config reset display.scale")


def boot_and_check(iso, uefi, timeout, log_dir):
    name = "uefi" if uefi else "bios"
    log_path = os.path.join(log_dir, f"boot-{name}.log")
    print(f"--- system test [{name}] (serial log: {os.path.relpath(log_path, ROOT)})")
    assets = os.path.join(ROOT, "build", "test-assets")
    os.makedirs(assets, exist_ok=True)
    stick1 = fat_image(os.path.join(assets, f"usb1-{name}.img"), "GDNUSB", {"hello.txt": "hello from usb1\n"})
    stick2 = fat_image(os.path.join(assets, f"usb2-{name}.img"), "HOTSTICK", {"hot.txt": "hotplugged\n"})

    vm = QemuVM(iso, log_path, uefi=uefi, usb=[stick1], outputs=2)
    t = Suite(vm)
    try:
        t0 = time.monotonic()

        # --- login policy -------------------------------------------------
        vm.expect(r"login: $", timeout)
        t.ok(f"serial login prompt after {time.monotonic() - t0:.1f}s")
        vm.send("root\n")
        out, _ = vm.expect(r"Password: $|Login incorrect", 30)
        if out.endswith("Password: "):   # util-linux login asks before refusing
            vm.send(LIVE_PW + "\n")
            vm.expect(r"Login incorrect", 30)
        t.ok("root login on a terminal is refused (pam_securetty)")
        vm.login("gideon", LIVE_PW, 60)
        t.ok("live user logs in")

        # --- base system (unprivileged) -----------------------------------
        t.check("system reached a running state with no failed units",
                "systemctl is-system-running --wait; systemctl --failed --no-legend --plain", lambda o: o.strip() == "running", 180)
        t.check("PID 1 is systemd", "cat /proc/1/comm", lambda o: o == "systemd")
        t.check("GideonOS kernel running", "uname -r", lambda o: o.endswith("-gideon"))
        t.check("os-release identifies GideonOS 0.3", ". /etc/os-release; echo $ID $VERSION_ID", lambda o: o.startswith("gideonos 0.3"))
        t.check("root is an overlay on the read-only squashfs image",
                "awk '$2==\"/\"{print $3} $2==\"/run/rootfs/lower\"{print $3}' /proc/mounts | sort | tr '\\n' ' '",
                lambda o: o.split() == ["overlay", "squashfs"])
        t.check("lower image is read-only", "touch /run/rootfs/lower/x 2>&1; true", lambda o: "Read-only" in o)
        t.check("FHS directories present (merged /usr)",
                "for d in /bin /sbin /lib /etc /home /usr /var /tmp /dev /proc /sys /run /opt /root /media; do [ -d $d ] || echo MISSING $d; done; echo done",
                lambda o: o == "done")
        t.check("user identity and desktop groups", "id",
                lambda o: o.startswith("uid=1000(gideon)") and all(g in o for g in ("wheel", "video", "input", "render", "audio", "users")))
        t.check("logind runtime dir (0700, owned by user)",
                "echo $XDG_RUNTIME_DIR; stat -c '%U %a' $XDG_RUNTIME_DIR", lambda o: o.split() == ["/run/user/1000", "gideon", "700"])
        t.check("sessions registered with logind (tty1 autologin + serial)", "gideon-session --list",
                lambda o: "tty1" in o and "ttyS0" in o and "gideon" in o)
        t.check("unprivileged user cannot read /etc/shadow", "cat /etc/shadow 2>&1; true", lambda o: "denied" in o)
        t.check("unprivileged user cannot manage services", "systemctl stop systemd-timesyncd 2>&1; echo rc=$?",
                lambda o: not o.endswith("rc=0"))
        t.check("hostname and time zone from configuration", "hostname; readlink /etc/localtime",
                lambda o: o.split()[0] == "gideon" and o.split()[1].endswith("zoneinfo/UTC"))
        t.check("clock is sane (RTC)", "date -u +%Y", lambda o: int(o) >= 2026)
        t.check("device permissions from udev (input, video groups)",
                "stat -c '%G' /dev/input/event0 /dev/dri/card0 | tr '\\n' ' '", lambda o: o.split() == ["input", "video"])
        t.check("kernel modules installed and indexed", "modinfo -F filename i915 amdgpu nouveau",
                lambda o: len(o.split()) == 3 and all(p.endswith(".ko") for p in o.split()))
        t.check("GPU firmware present", "ls /usr/lib/firmware/i915 /usr/lib/firmware/amdgpu | grep -c '\\.bin'", lambda o: int(o) > 10)

        # --- graphics (as the live user) -----------------------------------
        graphics_checks(t, vm)

        # --- administration ------------------------------------------------
        vm.su(LIVE_PW)
        t.check("su to root with the root password", "id -u", lambda o: o == "0")
        t.check("core services active", f"systemctl is-active {CORE_UNITS} | sort -u", lambda o: o == "active")
        old = t.check("networkd running", "systemctl show -p MainPID --value systemd-networkd", lambda o: o.isdigit() and o != "0")
        if old:
            t.check("crashed service is restarted by systemd",
                    f"kill -9 {old}; " + wait_for(f"[ \"$(systemctl show -p MainPID --value systemd-networkd)\" != {old} ] && "
                                                  "systemctl is-active -q systemd-networkd", 20) + " && echo restarted",
                    lambda o: o.endswith("restarted"), 40)
        t.check("service stop/start", "systemctl stop systemd-timesyncd && ! systemctl is-active -q systemd-timesyncd && "
                "systemctl start systemd-timesyncd && systemctl is-active systemd-timesyncd", lambda o: o == "active")
        t.check("journal receives messages",
                "logger -t gideontest probe-$$; " + wait_for("journalctl -q -t gideontest | grep -q probe-$$", 10) + " && echo found",
                lambda o: o.endswith("found"))
        t.check("kernel messages in the journal", "journalctl -q -k | grep -c 'Linux version'", lambda o: int(o) > 0)
        t.check("logins are logged", "journalctl -q | grep -c 'session opened for user gideon'", lambda o: int(o) > 0)

        # --- networking ----------------------------------------------------
        t.check("wired interface detected (predictable name)", "ls /sys/class/net | grep -vx lo", lambda o: o.startswith(("en", "eth")))
        t.check("DHCP lease (networkd)", IFV + wait_for("ip -4 addr show $IF | grep -q 'inet 10.0.2.15/24'", 30) + " && echo leased",
                lambda o: o.endswith("leased"), 60)
        t.check("default route via the DHCP router", "ip route show default", lambda o: o.startswith("default via 10.0.2.2"))
        t.check("DNS server from DHCP (resolved)", IFV + "resolvectl dns $IF", lambda o: "10.0.2.3" in o)
        t.check("resolv.conf points at the resolved stub", "grep nameserver /etc/resolv.conf", lambda o: "127.0.0.53" in o)
        t.check("IPv6 SLAAC address", IFV + wait_for("ip -6 addr show $IF | grep -q 'scope site\\|scope global'", 30) + " && echo v6",
                lambda o: o.endswith("v6"), 60)
        t.check("DNS resolution works", "nslookup kernel.org 2>&1; true",
                lambda o: "timed out" not in o and "unreachable" not in o and ("Address" in o.split("kernel.org", 1)[-1] or "NXDOMAIN" in o), 40)

        # --- configuration -------------------------------------------------
        t.check("config: apply changes the running system",
                "gideon-config set system.hostname testbox && gideon-config apply && hostname && "
                "gideon-config list system | grep hostname && gideon-config reset system.hostname && gideon-config apply && hostname",
                lambda o: o.split("\n")[0] == "testbox" and "(machine)" in o and o.split("\n")[-1] == "gideon", 60)
        t.check("config: time zone change", "gideon-config set time.timezone Europe/Berlin && gideon-config apply && date +%Z && "
                "gideon-config reset time.timezone && gideon-config apply && date +%Z",
                lambda o: o.split("\n")[0] in ("CET", "CEST") and o.split("\n")[-1] == "UTC", 60)
        t.check("config: invalid keys are rejected", "gideon-config set 'bad;key.x' 1 2>&1; echo rc=$?", lambda o: o.endswith("rc=2"))

        # --- display mode / scale -------------------------------------------
        mode_change_checks(t, vm)

        # --- users -----------------------------------------------------------
        t.check("create user", "gideon-user add alice --fullname 'Alice Test' && printf 'Wonder-123\\n' | gideon-user passwd alice --stdin && id alice",
                lambda o: "(alice)" in o and "users" in o and "wheel" not in o)
        t.check("new user's home is private", "stat -c '%U %a' /home/alice", lambda o: o == "alice 700")
        t.check("password stored as SHA-512 crypt", "grep '^alice:' /etc/shadow | cut -d: -f2 | cut -c1-3", lambda o: o == "$6$")
        t.check("user list", "gideon-user list", lambda o: "alice" in o and "gideon" in o)
        t.check("system accounts are protected", "gideon-user del root 2>&1; echo rc=$?", lambda o: "refusing" in o)

        # --- storage ---------------------------------------------------------
        t.check("USB stick present at boot is auto-mounted",
                wait_for("[ -f /media/GDNUSB/hello.txt ]", 20) + " && cat /media/GDNUSB/hello.txt", lambda o: o.endswith("hello from usb1"), 40)
        t.check("removable media is mounted nosuid,nodev", "grep ' /media/GDNUSB ' /proc/mounts", lambda o: "nosuid" in o and "nodev" in o)
        vm.monitor(f"drive_add 0 if=none,id=hot,format=raw,file={stick2}")
        vm.monitor("device_add usb-storage,bus=xhci.0,drive=hot,id=hotstick,removable=on")
        t.check("hot-plugged USB stick is auto-mounted",
                wait_for("[ -f /media/HOTSTICK/hot.txt ]", 30) + " && cat /media/HOTSTICK/hot.txt", lambda o: o.endswith("hotplugged"), 50)
        vm.monitor("device_del hotstick")
        t.check("unplugged USB stick is unmounted",
                wait_for("! grep -q ' /media/HOTSTICK ' /proc/mounts", 30) + " && [ ! -d /media/HOTSTICK ] && echo gone",
                lambda o: o.endswith("gone"), 50)
        t.check("storage events are logged", "journalctl -q -u 'gideon-automount@*' | grep -c 'mounted /dev'", lambda o: int(o) >= 3)

        # --- a second user logs in on the console ----------------------------
        vm.exit_shell()   # leave root
        vm.expect(r"[#$] $", 10)
        vm.exit_shell()   # log out gideon
        vm.login("alice", "Wonder-123", 60)
        t.check("newly created user can log in", "id -un; echo $XDG_RUNTIME_DIR; pwd",
                lambda o: o.split()[0] == "alice" and o.split()[1].startswith("/run/user/10") and o.split()[2] == "/home/alice")
        t.check("user can write to removable media (group users)",
                "echo hi > /media/GDNUSB/alice.txt && cat /media/GDNUSB/alice.txt", lambda o: o == "hi")
        vm.exit_shell()
        vm.login("gideon", LIVE_PW, 60)
        vm.su(LIVE_PW)
        t.check("delete user removes account and home",
                wait_for("[ -z \"$(loginctl show-user alice -p Sessions --value 2>/dev/null)\" ]", 20) +
                " && gideon-user del alice && ! id alice 2>/dev/null && [ ! -e /home/alice ] && echo deleted",
                lambda o: o.endswith("deleted"), 40)

        # --- power button ------------------------------------------------------
        vm.monitor("system_powerdown")
        vm.expect(r"reboot: Power down", 90)
        vm.wait_exit(30)
        t.ok("ACPI power button shuts the system down cleanly (logind)")
    except TestFailure as e:
        t.fail("test aborted", str(e))
    finally:
        vm.kill()
    return t.failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bios", action="store_true")
    ap.add_argument("--uefi", action="store_true")
    ap.add_argument("--iso", default=os.path.join(ROOT, "build", "GideonOS.iso"))
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("GIDEON_BOOT_TIMEOUT", 400)),
                    help="seconds to wait for the login prompt (software emulation is slow)")
    a = ap.parse_args()
    modes = [m for m, on in (("bios", a.bios), ("uefi", a.uefi)) if on] or ["bios", "uefi"]
    if not os.path.exists(a.iso):
        sys.exit(f"{a.iso} not found; run ./build.sh first")
    log_dir = os.path.join(ROOT, "build", "test-logs")
    os.makedirs(log_dir, exist_ok=True)
    failures = sum(boot_and_check(a.iso, m == "uefi", a.timeout, log_dir) for m in modes)
    print("PASS" if failures == 0 else f"FAILED ({failures} failure(s))")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
