#!/usr/bin/env python3
"""GideonOS system test (milestones 1-2).

Boots build/GideonOS.iso in QEMU (BIOS and/or UEFI) with a USB stick attached,
then drives the serial console through the whole base system: boot, login
policy, sessions, services and supervision, logging, networking, time,
configuration, user management, removable storage (coldplug + hotplug) and
power-button shutdown.

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
ALL_SERVICES = "devices syslog klog clock acpid storage network dhcp@eth0 ntp"


def wait_for(cond_cmd, seconds):
    """Shell snippet: poll cond_cmd once a second for up to `seconds`."""
    return f"i=0; until {cond_cmd}; do i=$((i+1)); [ $i -ge {seconds} ] && break; sleep 1; done; {cond_cmd}"


class Suite:
    def __init__(self, vm):
        self.vm = vm
        self.failures = 0

    def check(self, desc, cmd, pred=lambda o: True, timeout=60):
        try:
            status, out = self.vm.run(cmd, timeout)
        except TestFailure as e:
            self.fail(desc, str(e))
            raise
        if status == 0 and pred(out):
            print(f"  ok   {desc}")
            return out
        self.fail(desc, f"exit={status} output={out!r}")
        return None

    def ok(self, desc):
        print(f"  ok   {desc}")

    def fail(self, desc, why):
        self.failures += 1
        print(f"  FAIL {desc}: {why}")


def boot_and_check(iso, uefi, timeout, log_dir):
    name = "uefi" if uefi else "bios"
    log_path = os.path.join(log_dir, f"boot-{name}.log")
    print(f"--- system test [{name}] (serial log: {os.path.relpath(log_path, ROOT)})")
    assets = os.path.join(ROOT, "build", "test-assets")
    os.makedirs(assets, exist_ok=True)
    stick1 = fat_image(os.path.join(assets, f"usb1-{name}.img"), "GDNUSB", {"hello.txt": "hello from usb1\n"})
    stick2 = fat_image(os.path.join(assets, f"usb2-{name}.img"), "HOTSTICK", {"hot.txt": "hotplugged\n"})

    vm = QemuVM(iso, log_path, uefi=uefi, usb=[stick1])
    t = Suite(vm)
    try:
        t0 = time.monotonic()

        # --- login policy -------------------------------------------------
        vm.expect(r"login: $", timeout)
        t.ok(f"login prompt after {time.monotonic() - t0:.1f}s")
        vm.send("root\n")
        out, m = vm.expect(r"Password: $|Login incorrect", 30)
        if out.endswith("Password: "):   # some logins ask before refusing
            vm.send(LIVE_PW + "\n")
            vm.expect(r"Login incorrect", 30)
        t.ok("root login on a terminal is refused (empty /etc/securetty)")

        vm.login("gideon", LIVE_PW, 60)
        t.ok("live user logs in")

        # --- base system (as the unprivileged user) ------------------------
        t.check("boot scripts completed", "cat /run/gideon/boot-status", lambda o: o.startswith("GIDEONOS_BOOT_OK"))
        t.check("GideonOS kernel running", "uname -r", lambda o: o.endswith("-gideon"))
        t.check("os-release identifies GideonOS 0.2", ". /etc/os-release; echo $ID $VERSION_ID", lambda o: o.startswith("gideonos 0.2"))
        t.check("root is an overlay on the read-only squashfs image",
                "awk '$2==\"/\"{print $3} $2==\"/run/rootfs/lower\"{print $3}' /proc/mounts | tr '\\n' ' '",
                lambda o: o.split() == ["squashfs", "overlay"] or o.split() == ["overlay", "squashfs"])
        t.check("lower image is read-only", "touch /run/rootfs/lower/x 2>&1; echo done", lambda o: "Read-only" in o)
        t.check("FHS directories present",
                "for d in /bin /sbin /etc /home /usr /var /tmp /dev /proc /sys /run /opt /boot /root /media; do [ -d $d ] || echo MISSING $d; done; echo done",
                lambda o: o == "done")
        t.check("user identity and desktop groups", "id",
                lambda o: o.startswith("uid=1000(gideon)") and all(g in o for g in ("wheel", "video", "input", "audio", "users")))
        t.check("private XDG_RUNTIME_DIR (0700, owned by user)",
                "echo $XDG_RUNTIME_DIR; stat -c '%U %a' $XDG_RUNTIME_DIR", lambda o: o.split() == ["/run/user/1000", "gideon", "700"])
        t.check("session is recorded", "gideon-session --list", lambda o: "gideon" in o and "ttyS0" in o)
        t.check("unprivileged user cannot read /etc/shadow", "cat /etc/shadow 2>&1; true", lambda o: "denied" in o)
        t.check("unprivileged user cannot start services", "gideon-service start ntp 2>&1; true", lambda o: "must be run as root" in o)
        t.check("hostname from configuration", "hostname; gideon-config get system.hostname", lambda o: o.split() == ["gideon", "gideon"])
        t.check("time zone from configuration", "echo $TZ", lambda o: o == "UTC0")
        t.check("clock is sane (RTC read at boot)", "date -u +%Y", lambda o: int(o) >= 2026)
        t.check("input/DRM device groups from mdev rules",
                "stat -c '%G' /dev/input/event0 /dev/dri/card0 | tr '\\n' ' '", lambda o: o.split() == ["input", "video"])

        # --- administration ------------------------------------------------
        vm.su(LIVE_PW)
        t.check("su to root with the root password", "id -u", lambda o: o == "0")
        t.check("all enabled services active", f"gideon-service status {ALL_SERVICES}")
        t.check("service list marks enabled services", "gideon-service list", lambda o: "* syslog" in o and "dhcp@eth0" in o)

        old = t.check("syslogd is supervised", "pidof syslogd", lambda o: o.isdigit())
        if old:
            t.check("crashed daemon is restarted by its supervisor",
                    "kill -9 $(pidof syslogd); " + wait_for(f"[ -n \"$(pidof syslogd)\" ] && [ \"$(pidof syslogd)\" != {old} ]", 10) +
                    " && gideon-service status syslog", lambda o: "active" in o)
        t.check("service stop/start", "gideon-service stop ntp && ! pidof ntpd >/dev/null && gideon-service start ntp && pidof ntpd >/dev/null && echo cycled",
                lambda o: o.endswith("cycled"))

        t.check("syslog receives messages",
                "logger -t gideontest probe-$$; " + wait_for("grep -q \"gideontest: probe-$$\" /var/log/messages", 5) + " && echo found",
                lambda o: o.endswith("found"))
        t.check("kernel messages reach syslog (klogd)", "grep -c 'kern\\.' /var/log/messages", lambda o: int(o) > 0)
        t.check("login was logged", "grep -c 'session: login: gideon' /var/log/messages", lambda o: int(o) > 0)

        # --- networking ----------------------------------------------------
        t.check("DHCP lease on eth0", wait_for("ip -4 addr show eth0 | grep -q 'inet 10.0.2.15/24'", 30) + " && echo leased",
                lambda o: o.endswith("leased"), 60)
        t.check("default route via the DHCP router", "ip route show default", lambda o: o.startswith("default via 10.0.2.2"))
        t.check("DNS server from DHCP", "cat /etc/resolv.conf", lambda o: "nameserver 10.0.2.3" in o)
        t.check("IPv6 SLAAC address", wait_for("ip -6 addr show eth0 | grep -q 'scope site\\|scope global'", 20) + " && echo v6",
                lambda o: o.endswith("v6"), 40)
        t.check("DNS server answers queries", "nslookup kernel.org 2>&1; true",
                lambda o: "timed out" not in o and "unreachable" not in o and ("Address" in o.split("kernel.org", 1)[-1] or "NXDOMAIN" in o), 40)

        # --- configuration -------------------------------------------------
        t.check("config: default layer", "gideon-config get logging.rotate", lambda o: o == "4")
        t.check("config: machine override wins and is reversible",
                "gideon-config set logging.rotate 7 && gideon-config get logging.rotate && "
                "gideon-config list logging | grep rotate && gideon-config reset logging.rotate && gideon-config get logging.rotate",
                lambda o: o.split("\n")[0] == "7" and "(machine)" in o and o.split("\n")[-1] == "4")
        t.check("config: invalid keys are rejected", "gideon-config set 'bad;key.x' 1 2>&1; echo rc=$?", lambda o: o.endswith("rc=2"))

        # --- users -----------------------------------------------------------
        t.check("create user", "gideon-user add alice --fullname 'Alice Test' && printf 'Wonder-123\\n' | gideon-user passwd alice --stdin && id alice",
                lambda o: "uid=1001(alice)" in o and "users" in o and "wheel" not in o)
        t.check("new user's home is private", "stat -c '%U %a' /home/alice", lambda o: o == "alice 700")
        t.check("password stored as SHA-512 crypt", "grep '^alice:' /etc/shadow | cut -d: -f2 | cut -c1-3", lambda o: o == "$6$")
        t.check("user list", "gideon-user list", lambda o: "alice" in o and "gideon" in o)
        t.check("system accounts are protected", "gideon-user del root 2>&1; echo rc=$?", lambda o: "refusing" in o)

        # --- storage ---------------------------------------------------------
        t.check("USB stick present at boot is auto-mounted",
                wait_for("[ -f /media/GDNUSB/hello.txt ]", 15) + " && cat /media/GDNUSB/hello.txt", lambda o: o.endswith("hello from usb1"), 30)
        t.check("removable media is mounted nosuid,nodev", "grep ' /media/GDNUSB ' /proc/mounts", lambda o: "nosuid" in o and "nodev" in o)

        vm.monitor(f"drive_add 0 if=none,id=hot,format=raw,file={stick2}")
        vm.monitor("device_add usb-storage,bus=xhci.0,drive=hot,id=hotstick,removable=on")
        t.check("hot-plugged USB stick is auto-mounted",
                wait_for("[ -f /media/HOTSTICK/hot.txt ]", 20) + " && cat /media/HOTSTICK/hot.txt", lambda o: o.endswith("hotplugged"), 40)
        vm.monitor("device_del hotstick")
        t.check("unplugged USB stick is unmounted",
                wait_for("! grep -q ' /media/HOTSTICK ' /proc/mounts", 20) + " && [ ! -d /media/HOTSTICK ] && echo gone",
                lambda o: o.endswith("gone"), 40)
        t.check("storage events are logged", "grep -c 'storage: \\(un\\)\\?mounted' /var/log/messages", lambda o: int(o) >= 3)

        # --- a second user logs in on the console -----------------------------
        vm.exit_shell()   # leave root
        vm.expect(r"[#$] $", 10)
        vm.exit_shell()   # log out gideon
        vm.login("alice", "Wonder-123", 60)
        t.check("newly created user can log in", "id -un; echo $XDG_RUNTIME_DIR; pwd", lambda o: o.split() == ["alice", "/run/user/1001", "/home/alice"])
        t.check("user can write to removable media (group users)", "echo hi > /media/GDNUSB/alice.txt && cat /media/GDNUSB/alice.txt", lambda o: o == "hi")
        vm.exit_shell()
        vm.login("gideon", LIVE_PW, 60)
        vm.su(LIVE_PW)
        t.check("delete user removes account and home",
                wait_for("! pgrep -u 1001 >/dev/null", 10) + " && gideon-user del alice && ! id alice 2>/dev/null && [ ! -e /home/alice ] && echo deleted",
                lambda o: o.endswith("deleted"))

        # --- power button ------------------------------------------------------
        vm.monitor("system_powerdown")
        vm.expect(r"reboot: Power down", 60)
        vm.wait_exit(30)
        t.ok("ACPI power button shuts the system down cleanly")
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
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("GIDEON_BOOT_TIMEOUT", 300)),
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
