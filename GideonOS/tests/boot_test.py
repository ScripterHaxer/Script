#!/usr/bin/env python3
"""GideonOS system test (milestones 1-4).

Boots build/GideonOS.iso in QEMU (BIOS and/or UEFI) with a two-head virtio GPU,
a USB tablet and a USB stick, then drives the serial console and QEMU monitor
through the whole system: boot, login policy, systemd services, journal,
networking, configuration, users, removable storage, kernel modules, the
graphical session (KMS, rendering, input routing, multiple outputs, mode and
scale changes) and power-button shutdown.

    tests/boot_test.py [--bios] [--uefi] [--iso PATH] [--timeout SECONDS]
"""
import argparse
import json
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


MSG = "XDG_RUNTIME_DIR=/run/user/1000 gideon-compositor msg"
BAR_FOCUSED = "1f242c"


def ipc(vm, *args):
    """Run a compositor IPC command in the guest; returns the parsed JSON result."""
    status, out = vm.run(f"{MSG} {' '.join(str(a) for a in args)}", 60)
    if status != 0:
        raise TestFailure(f"ipc {args} failed: {out}")
    return json.loads(out) if out.strip() else None


def wait_until(fn, seconds, what):
    deadline = time.monotonic() + seconds
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TestFailure(f"timed out waiting for {what}")
        time.sleep(1)


def start_probe(vm, args, log):
    vm.run(f"({WL}; gideon-gfx-probe {args} > {log} 2>&1 &)")


SCREEN = {"w": 1280, "h": 800}   # head 0 size, updated when the test changes mode


def point(vm, x, y):
    vm.pointer_to(x, y, SCREEN["w"], SCREEN["h"])
    time.sleep(0.3)


def click(vm, x, y):
    point(vm, x, y)
    vm.pointer_button(True)
    time.sleep(0.2)
    vm.pointer_button(False)
    time.sleep(0.3)


def graphics_checks(t, vm):
    t.check("gideon-compositor running for the live user on tty1",
            wait_for("pidof gideon-compositor >/dev/null && [ -S /run/user/1000/gideon-compositor.sock ]", 120)
            + " && stat -c %U /proc/$(pidof gideon-compositor)", lambda o: o.endswith("gideon"), 150)
    t.check("compositor reports the DRM/KMS backend", f"{MSG} version", lambda o: '"udev"' in o)
    t.check("graphical session has the system locale",
            "tr '\\0' '\\n' < /proc/$(pidof gideon-compositor)/environ | grep '^LANG='", lambda o: o == "LANG=en_US.UTF-8")
    t.check("compositor log: KMS output brought up", "grep -c 'output connected' /run/user/1000/gideon/compositor.log",
            lambda o: int(o) >= 1)

    outputs = ipc(vm, "outputs")
    name = outputs[0]["name"]
    t.expect_true("outputs named after their connectors", name.startswith("Virtual-"), f"{outputs}")
    # Runtime mode change to a desktop-sized mode the (virtual) monitor offers;
    # under QEMU's GTK UI the preferred mode follows the small window size.
    offered = [m.split("@")[0] for m in outputs[0]["modes"]]
    mode = next((m for m in ("1280x800", "1280x720", "1280x1024", "1024x768") if m in offered), None)
    t.expect_true("display offers a desktop-sized mode", mode is not None, f"{offered}")
    W, H = map(int, (mode or "1024x768").split("x"))
    SCREEN.update(w=W, h=H)
    t.check(f"runtime mode change to {mode}", f"{MSG} set-mode {name} {mode}", lambda o: True)
    img = wait_until(lambda: (lambda i: i if (i.width, i.height) == (W, H) else None)(vm.screendump(0)), 30, f"{mode} scanout")
    t.expect_true(f"display scans out {mode} after the change", (img.width, img.height) == (W, H))
    t.expect_true("desktop background drawn in GideonOS colour", img.matches(20, 20, BG), f"{img.pixel(20, 20)}")

    # --- first client (CPU / wl_shm) ------------------------------------------
    start_probe(vm, "", "/tmp/probe1.log")
    w1 = wait_until(lambda: next(iter(ipc(vm, "windows")), None), 30, "first window")
    t.check("client connects, sees keyboard+pointer and draws",
            wait_for(f"grep -q 'drawn color={TEAL}' /tmp/probe1.log", 20) + " && grep seat /tmp/probe1.log | tail -1",
            lambda o: "keyboard=1" in o and "pointer=1" in o, 40)
    time.sleep(1)
    img = vm.screendump(0)
    cx, cy = w1["x"] + w1["width"] // 2, w1["y"] + w1["height"] // 2
    t.expect_true("client frame scanned out (pixel-exact)", img.matches(cx, cy, TEAL), f"{img.pixel(cx, cy)} at {cx},{cy}")
    t.expect_true("server-side title bar above the client", img.matches(w1["x"] + 10, w1["y"] - 18, BAR_FOCUSED),
                  f"{img.pixel(w1['x'] + 10, w1['y'] - 18)}")
    t.expect_true("meridian accent line marks the focused window", img.matches(w1["x"] + 10, w1["y"] - 36, TEAL),
                  f"{img.pixel(w1['x'] + 10, w1['y'] - 36)}")

    # --- input routing ----------------------------------------------------------
    vm.monitor("sendkey a")
    t.check("keyboard input routed to the focused client", wait_for("grep -q 'key 30' /tmp/probe1.log", 15) + " && echo got",
            lambda o: o.endswith("got"), 30)
    click(vm, cx, cy)
    t.check("pointer button routed to the client", wait_for("grep -q 'button 272' /tmp/probe1.log", 15) + " && echo got",
            lambda o: o.endswith("got"), 30)
    img = wait_screen(vm, RED)
    t.expect_true("client re-rendered after input", img.matches(cx, cy, RED), f"{img.pixel(cx, cy)}")

    # --- second client through Mesa EGL/GLES ---------------------------------------
    start_probe(vm, "--egl", "/tmp/probe2.log")
    ws = wait_until(lambda: (lambda l: l if len(l) == 2 else None)(ipc(vm, "windows")), 60, "second window")
    w2 = next(w for w in ws if w["id"] != w1["id"])
    t.check("EGL/GLES2 client through Mesa", wait_for(f"grep -q 'drawn color={TEAL}' /tmp/probe2.log", 60) + " && grep renderer= /tmp/probe2.log",
            lambda o: "renderer=egl" in o, 90)
    t.expect_true("new window takes focus and cascades", w2["focused"] and (w2["x"], w2["y"]) != (w1["x"], w1["y"]), f"{ws}")
    time.sleep(1)
    img = vm.screendump(0)
    t.expect_true("GLES frame scanned out", img.matches(w2["x"] + w2["width"] // 2, w2["y"] + w2["height"] // 2, TEAL),
                  f"{img.pixel(w2['x'] + w2['width'] // 2, w2['y'] + w2['height'] // 2)}")

    # --- stacking, focus, moving with the pointer -------------------------------
    click(vm, w1["x"] + 4, w1["y"] + 4)
    t.expect_true("click raises and focuses the lower window",
                  wait_until(lambda: next(w for w in ipc(vm, "windows") if w["id"] == w1["id"])["focused"], 10, "focus"))
    bx, by = w1["x"] + 40, w1["y"] - 18
    point(vm, bx, by)
    vm.pointer_button(True)
    point(vm, bx + 60, by + 30)
    point(vm, bx + 120, by + 70)
    vm.pointer_button(False)
    time.sleep(0.3)
    moved = next(w for w in ipc(vm, "windows") if w["id"] == w1["id"])
    t.expect_true("title-bar drag moves the window", (moved["x"] - w1["x"], moved["y"] - w1["y"]) == (120, 70),
                  f"moved by {moved['x'] - w1['x']},{moved['y'] - w1['y']}")

    # --- keyboard shortcuts (docs/DESIGN.md §9) ----------------------------------
    def focused():
        return next(w for w in ipc(vm, "windows") if w["focused"])

    vm.monitor("sendkey meta_l-up")
    mx = wait_until(lambda: (lambda w: w if w["maximized"] and w["width"] == W else None)(focused()), 15, "maximize")
    t.expect_true("Super+Up maximizes below the title bar", (mx["x"], mx["y"], mx["height"]) == (0, 36, H - 36), f"{mx}")
    vm.monitor("sendkey meta_l-f")
    fs = wait_until(lambda: (lambda w: w if w["fullscreen"] and w["height"] == H else None)(focused()), 15, "fullscreen")
    img = vm.screendump(0)
    t.expect_true("Super+F fullscreen covers the display (no title bar)",
                  (fs["x"], fs["y"]) == (0, 0) and any(img.matches(5, 5, c) for c in (TEAL, AMBER, RED)), f"{fs} {img.pixel(5, 5)}")
    vm.monitor("sendkey meta_l-f")
    vm.monitor("sendkey meta_l-down")
    wait_until(lambda: (lambda w: not w["maximized"] and not w["fullscreen"])(focused()), 15, "restore")
    vm.monitor("sendkey meta_l-2")
    wait_until(lambda: ipc(vm, "workspaces")["active"] == 2, 10, "workspace 2")
    img = vm.screendump(0)
    t.expect_true("Super+2 switches to an empty workspace", img.matches(W // 2, H // 2, BG), f"{img.pixel(W // 2, H // 2)}")
    vm.monitor("sendkey meta_l-1")
    wait_until(lambda: ipc(vm, "workspaces")["active"] == 1, 10, "workspace 1")
    vm.monitor("sendkey meta_l-ret")
    t.expect_true("Super+Enter launches the terminal (foot)",
                  wait_until(lambda: any(w["app_id"] == "foot" for w in ipc(vm, "windows")), 60, "foot window"))
    vm.monitor("sendkey print")
    t.check("Print saves a PNG screenshot",
            wait_for("ls /home/gideon/Pictures/Screenshot-*.png >/dev/null 2>&1", 20) + " && head -c 8 \"$(ls /home/gideon/Pictures/Screenshot-*.png | head -1)\" | od -c | head -1",
            lambda o: "P   N   G" in o, 40)

    # --- scaling, more modes ------------------------------------------------------
    ipc(vm, "set-scale", name, 2)
    start_probe(vm, "", "/tmp/probe3.log")
    t.check("output scale 2 advertised to clients", wait_for("grep -q 'scale=2' /tmp/probe3.log", 30) + " && grep 'output name' /tmp/probe3.log | head -1",
            lambda o: "scale=2" in o, 60)
    ipc(vm, "set-scale", name, 1)
    other = "800x600" if mode == "1024x768" else "1024x768"
    ow, oh = map(int, other.split("x"))
    ipc(vm, "set-mode", name, other)
    img = wait_until(lambda: (lambda i: i if (i.width, i.height) == (ow, oh) else None)(vm.screendump(0)), 30, other)
    t.expect_true(f"runtime mode change to {other}", (img.width, img.height) == (ow, oh))
    ipc(vm, "set-mode", name, mode)

    # --- multiple displays ---------------------------------------------------------
    if not vm.multihead:
        print("  SKIP multiple displays: Xvfb not installed (QEMU shows one head without a UI)")
    else:
        outs = ipc(vm, "outputs")
        t.expect_true("two displays driven, laid out side by side",
                      len(outs) == 2 and outs[1]["x"] == outs[0]["x"] + outs[0]["width"], f"{outs}")
        img1 = vm.screendump(1)
        t.expect_true("second display shows the desktop", img1.matches(20, 20, BG), f"{img1.pixel(20, 20)}")

    # --- VT switching (logind session pause/resume) -------------------------------
    vm.monitor("sendkey ctrl-alt-f2")
    t.check("Ctrl+Alt+F2 switches VT: compositor releases the display",
            wait_for("grep -q 'session paused' /run/user/1000/gideon/compositor.log", 20) + " && echo paused", lambda o: o.endswith("paused"), 40)
    vm.monitor("sendkey ctrl-alt-f1")
    t.check("Ctrl+Alt+F1 returns: compositor resumes",
            wait_for("grep -q 'session resumed' /run/user/1000/gideon/compositor.log", 20) + " && echo resumed", lambda o: o.endswith("resumed"), 40)
    vm.run("pkill -x gideon-gfx-probe; pkill -x foot")
    img = wait_screen(vm, BG, seconds=30)
    t.expect_true("desktop redrawn after returning", img.matches(img.width // 2, img.height // 2, BG), f"{img.pixel(img.width // 2, img.height // 2)}")


def mode_change_checks(t, vm):
    """As root: the configured startup mode applies when the session restarts."""
    old = vm.run("pidof gideon-compositor")[1]
    t.check("set display.mode 1024x768", "gideon-config set display.mode 1024x768 && echo set", lambda o: o.endswith("set"))
    vm.run("kill $(pidof gideon-compositor)")
    if t.check("graphical session restarts with the new settings",
               wait_for(f"[ -n \"$(pidof gideon-compositor)\" ] && [ \"$(pidof gideon-compositor)\" != '{old}' ]", 60)
               + " && echo restarted", lambda o: o.endswith("restarted"), 90) is None:
        # Diagnostics for a slow or stuck restart.
        print(vm.run(f"pidof gideon-compositor; cat /proc/{old}/wchan /proc/{old}/stack 2>&1; ls -l /proc/{old}/task 2>&1 | tail -n +2;"
                     " for t in /proc/" + old + "/task/*; do echo $t $(cat $t/comm $t/wchan); done;"
                     " tail -15 /run/user/1000/gideon/compositor.log | cut -c1-200;"
                     " journalctl -b --since -100s --no-pager | grep -v 'timesyncd' | tail -30 | cut -c1-200", 60)[1])
    img = wait_until(lambda: (lambda i: i if (i.width, i.height) == (1024, 768) else None)(vm.screendump(0)), 60, "1024x768 at startup")
    t.expect_true("display starts at the configured 1024x768", (img.width, img.height) == (1024, 768))
    vm.run("gideon-config reset display.mode")


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
        t.check("os-release identifies GideonOS 0.4", ". /etc/os-release; echo $ID $VERSION_ID", lambda o: o.startswith("gideonos 0.4"))
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
