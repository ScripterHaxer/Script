#!/usr/bin/env python3
"""GideonOS milestone-1 boot test.

Boots build/GideonOS.iso in QEMU (BIOS and/or UEFI), waits for the shell on
the serial console, verifies the base system, then powers off cleanly.

    tests/boot_test.py [--bios] [--uefi] [--iso PATH] [--timeout SECONDS]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
from qemu import QemuVM, TestFailure  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (description, shell command, predicate on output)
CHECKS = [
    ("init is PID 1 (BusyBox init)", "cat /proc/1/comm", lambda o: o == "init"),
    ("boot scripts completed", "cat /run/gideon/boot-status", lambda o: o.startswith("GIDEONOS_BOOT_OK")),
    ("GideonOS kernel running", "uname -r", lambda o: o.endswith("-gideon")),
    ("os-release identifies GideonOS", ". /etc/os-release; echo $ID $VERSION_ID", lambda o: o.startswith("gideonos ")),
    ("hostname set", "hostname", lambda o: o == "gideon"),
    ("kernel filesystems mounted",
     "for m in /proc /sys /dev /dev/pts /run /tmp; do mountpoint -q $m || echo MISSING $m; done; echo done",
     lambda o: o == "done"),
    ("FHS directories present",
     "for d in /bin /sbin /etc /home /usr /var /tmp /dev /proc /sys /run /opt /boot /root; do [ -d $d ] || echo MISSING $d; done; echo done",
     lambda o: o == "done"),
    ("/tmp is writable", "echo gideon > /tmp/t && cat /tmp/t && rm /tmp/t", lambda o: o == "gideon"),
    ("device nodes via devtmpfs/mdev", "[ -c /dev/null ] && [ -c /dev/ttyS0 ] && [ -e /dev/vda -o -e /dev/sr0 ] && echo yes",
     lambda o: o == "yes"),
    ("virtio network interface detected", "ls /sys/class/net", lambda o: "eth0" in o.split()),
    ("loopback is up", "ip -o link show lo", lambda o: "UP" in o or "LOOPBACK,UP" in o),
    ("shell arithmetic / userspace works", "echo $((6*7))", lambda o: o == "42"),
]


def boot_and_check(iso, uefi, timeout, log_dir):
    name = "uefi" if uefi else "bios"
    log_path = os.path.join(log_dir, f"boot-{name}.log")
    print(f"--- boot test [{name}] (serial log: {os.path.relpath(log_path, ROOT)})")
    vm = QemuVM(iso, log_path, uefi=uefi)
    failures = 0
    try:
        t0 = time.monotonic()
        vm.expect(r"Milestone 1: minimal system shell", timeout)
        vm.expect(r"[#$] $", 30)
        print(f"  ok   reached shell in {time.monotonic() - t0:.1f}s")
        vm.run("stty -echo")  # keep the transcript to command output only
        for desc, cmd, pred in CHECKS:
            status, out = vm.run(cmd)
            if status == 0 and pred(out):
                print(f"  ok   {desc}")
            else:
                failures += 1
                print(f"  FAIL {desc}: exit={status} output={out!r}")
        vm.send("poweroff\n")
        vm.expect(r"reboot: Power down", 60)
        vm.wait_exit(30)
        print("  ok   clean poweroff")
    except TestFailure as e:
        failures += 1
        print(f"  FAIL {e}")
    finally:
        vm.kill()
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bios", action="store_true")
    ap.add_argument("--uefi", action="store_true")
    ap.add_argument("--iso", default=os.path.join(ROOT, "build", "GideonOS.iso"))
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("GIDEON_BOOT_TIMEOUT", 300)),
                    help="seconds to wait for the shell (software emulation is slow)")
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
