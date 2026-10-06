#!/usr/bin/env python3
"""gideon-compositor window-management test on the development host.

Runs the compositor nested (winit backend) on a private Xvfb server, starts
gfx-probe clients, drives real keyboard/mouse input with xdotool and checks the
result through the compositor IPC and pixel-exact screenshots. Fast (seconds),
so it is the inner loop for compositor work; the QEMU system test covers the
DRM/KMS backend on the real image.

    tests/compositor_test.py [--keep]

Needs: Xvfb, xdotool, wl-clipboard, a host build of the compositor
(cargo build --release --features winit) and of components/gfx-probe.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "lib"))
from qemu import Image  # noqa: E402  (PPM reader)

COMP = os.path.join(ROOT, "components/compositor/target/release/gideon-compositor")
PROBE = os.path.join(ROOT, "components/gfx-probe/gideon-gfx-probe")
BG, TEAL, AMBER, RED, ACCENT, BAR = "0f1216", "3db8c6", "e8b04b", "f06a6a", "3db8c6", "1f242c"
TITLE_H = 36


class Fail(Exception):
    pass


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="gideon-comp-test-")
        self.runtime = os.path.join(self.tmp, "xdg")
        os.makedirs(self.runtime, mode=0o700)
        # Private config root: the terminal shortcut starts a probe.
        cfg = os.path.join(self.tmp, "config", "etc", "gideon")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "session.conf"), "w") as f:
            f.write(f'terminal = "{PROBE}"\n')
        r, w = os.pipe()
        self.xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                     pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(w)
        with os.fdopen(r) as f:
            self.display = ":" + f.readline().strip()
        self.env = dict(os.environ, DISPLAY=self.display, XDG_RUNTIME_DIR=self.runtime, HOME=self.tmp,
                        GIDEON_CONFIG_ROOT=os.path.join(self.tmp, "config"), RUST_LOG="info")
        self.env.pop("WAYLAND_DISPLAY", None)
        self.log = open(os.path.join(self.tmp, "compositor.log"), "w")
        self.comp = subprocess.Popen([COMP, "--backend", "winit"], env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: os.path.exists(os.path.join(self.runtime, "gideon-compositor.sock")), 20, "IPC socket")
        sock = [n for n in os.listdir(self.runtime) if n.startswith("wayland-") and not n.endswith(".lock")]
        self.wl_env = dict(self.env, WAYLAND_DISPLAY=sock[0])
        self.wl_env.pop("DISPLAY")
        self.probes = []
        self.xwin = self.wait(lambda: subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."], env=self.env,
                                                     capture_output=True, text=True).stdout.split(), 20, "compositor X window")[0]
        # No X window manager: give the compositor window keyboard focus directly.
        subprocess.run(["xdotool", "windowfocus", "--sync", self.xwin], env=self.env, check=True, stderr=subprocess.DEVNULL)

    def msg(self, *args):
        out = subprocess.run([COMP, "msg", *map(str, args)], env=self.env, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise Fail(f"msg {args}: {out.stderr.strip()}")
        return json.loads(out.stdout) if out.stdout.strip() else None

    def probe(self, *args):
        log = os.path.join(self.tmp, f"probe{len(self.probes)}.log")
        p = subprocess.Popen([PROBE, *args], env=self.wl_env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
        self.probes.append((p, log))
        return log

    def xdo(self, *args):
        subprocess.run(["xdotool", *map(str, args)], env=self.env, check=True, stderr=subprocess.DEVNULL)
        time.sleep(0.25)

    def windows(self):
        return self.msg("windows")

    def window(self, wid):
        return next((w for w in self.windows() if w["id"] == wid), None)

    def screen(self):
        path = os.path.join(self.tmp, "screen.ppm")
        self.msg("screenshot", path)
        return Image.from_ppm(path)

    @staticmethod
    def wait(cond, seconds, what):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        raise Fail(f"timed out waiting for {what}")

    def close(self, keep):
        for p, _ in self.probes:
            p.kill()
        self.comp.terminate()
        try:
            self.comp.wait(5)
        except subprocess.TimeoutExpired:
            self.comp.kill()
        self.xvfb.kill()
        self.log.close()
        if keep:
            print(f"kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


def main():
    keep = "--keep" in sys.argv
    for tool in ("Xvfb", "xdotool", "wl-copy"):
        if not shutil.which(tool):
            sys.exit(f"{tool} missing (apt install xvfb xdotool wl-clipboard)")
    for path in (COMP, PROBE):
        if not os.path.exists(path):
            sys.exit(f"{path} missing; build it first")
    e = Env()
    failures = 0

    def check(desc, fn):
        nonlocal failures
        try:
            r = fn()
            if r is False:
                raise Fail("condition false")
            print(f"  ok   {desc}")
        except (Fail, AssertionError, KeyError, IndexError, subprocess.CalledProcessError) as ex:
            failures += 1
            print(f"  FAIL {desc}: {ex}")

    def wait_windows(n):
        return e.wait(lambda: len(e.windows()) == n and e.windows(), 15, f"{n} windows")

    try:
        out = e.msg("outputs")[0]
        W, H = out["width"], out["height"]
        check("compositor starts and reports its output", lambda: out["name"] and W > 0)
        check("background in GideonOS colour", lambda: e.screen().matches(5, 5, BG))

        log1 = e.probe()
        w1 = wait_windows(1)[0]
        time.sleep(0.4)  # let the open animation (150 ms fade) finish
        check("first client is mapped, centred and focused",
              lambda: w1["focused"] and abs(w1["x"] + w1["width"] // 2 - W // 2) <= 1)
        img = e.screen()
        check("client pixels on screen", lambda: img.matches(w1["x"] + 10, w1["y"] + 10, TEAL))
        check("server-side title bar above the window",
              lambda: img.matches(w1["x"] + 10, w1["y"] - TITLE_H // 2, BAR))
        check("meridian accent line on the focused window", lambda: img.matches(w1["x"] + 10, w1["y"] - TITLE_H, ACCENT))

        log2 = e.probe()
        ws = wait_windows(2)
        w2 = next(w for w in ws if w["id"] != w1["id"])
        check("second window cascades and takes focus", lambda: w2["focused"] and w2["x"] != w1["x"])

        # Keyboard routing goes to the focused window only.
        e.xdo("key", "a")
        check("keys reach the focused client",
              lambda: e.wait(lambda: "key 30" in open(log2).read(), 5, "key") and "key 30" not in open(log1).read())

        # Click the visible part of window 1 to raise and focus it.
        e.xdo("mousemove", w1["x"] + 5, w1["y"] + 5, "click", 1)
        check("click raises and focuses the lower window", lambda: e.window(w1["id"])["focused"])
        check("the click reached that client", lambda: e.wait(lambda: "button 272" in open(log1).read(), 5, "button"))

        # Drag the title bar.
        bx, by = w1["x"] + 60, w1["y"] - TITLE_H // 2
        e.xdo("mousemove", bx, by, "mousedown", 1)
        e.xdo("mousemove", bx + 100, by + 50)
        e.xdo("mousemove", bx + 150, by + 80, "mouseup", 1)
        moved = e.window(w1["id"])
        check("title-bar drag moves the window", lambda: (moved["x"] - w1["x"], moved["y"] - w1["y"]) == (150, 80))

        # Super+drag anywhere in the window.
        x0, y0 = moved["x"], moved["y"]
        e.xdo("mousemove", x0 + 200, y0 + 200, "keydown", "super", "mousedown", 1)
        e.xdo("mousemove", x0 + 150, y0 + 170, "mouseup", 1, "keyup", "super")
        m2 = e.window(w1["id"])
        check("Super+drag moves the window", lambda: (m2["x"] - x0, m2["y"] - y0) == (-50, -30))

        # Super+right-drag resizes from the bottom-right.
        e.xdo("mousemove", m2["x"] + 100, m2["y"] + 100, "keydown", "super", "mousedown", 3)
        e.xdo("mousemove", m2["x"] + 180, m2["y"] + 140, "mouseup", 3, "keyup", "super")
        check("Super+right-drag resizes the window",
              lambda: e.wait(lambda: e.window(w1["id"])["width"] == m2["width"] + 80, 5, "resize")
              and e.window(w1["id"])["height"] == m2["height"] + 40)

        # Maximize button, then double-click the bar to restore.
        cur = e.window(w1["id"])
        max_btn = (cur["x"] + cur["width"] - 2 * 28 + 10, cur["y"] - TITLE_H // 2)
        e.xdo("mousemove", *max_btn, "click", 1)
        mx = e.wait(lambda: (lambda w: w["maximized"] and w["width"] == W and w)(e.window(w1["id"])), 5, "maximize")
        check("maximize button fills the output below the title bar",
              lambda: (mx["x"], mx["y"], mx["width"], mx["height"]) == (0, TITLE_H, W, H - TITLE_H))
        e.xdo("mousemove", 300, TITLE_H // 2, "click", "--repeat", 2, "--delay", 80, 1)
        check("double-click on the bar restores the previous geometry",
              lambda: e.wait(lambda: (lambda w: not w["maximized"] and w["width"] == cur["width"] and (w["x"], w["y"]) == (cur["x"], cur["y"]))(e.window(w1["id"])), 5, "restore"))

        # Keyboard shortcuts (docs/DESIGN.md §9) on the focused window.
        def key(*k):
            e.xdo("key", *k)

        key("super+f")
        check("Super+F: fullscreen covers the whole output",
              lambda: e.wait(lambda: (lambda w: w["fullscreen"] and (w["x"], w["y"], w["width"], w["height"]) == (0, 0, W, H))(e.window(w1["id"])), 5, "fullscreen"))
        # The probe recolours on any key (the Super press reaches it too), so accept
        # any of its colours: the point is that no title bar is drawn at the top.
        check("fullscreen window hides the title bar",
              lambda: (lambda img: any(img.matches(5, 5, c) for c in (TEAL, AMBER, RED)))(e.screen()))
        key("super+f")
        key("super+Left")
        check("Super+Left tiles to the left half",
              lambda: e.wait(lambda: (lambda w: w["tiled"] == "left" and (w["x"], w["width"]) == (0, W // 2))(e.window(w1["id"])), 5, "tile"))
        key("super+Right")
        check("Super+Right tiles to the right half",
              lambda: e.wait(lambda: (lambda w: w["tiled"] == "right" and w["x"] == W // 2)(e.window(w1["id"])), 5, "tile right"))
        key("super+Down")  # restore floating
        key("super+Up")
        check("Super+Up maximizes", lambda: e.wait(lambda: e.window(w1["id"])["maximized"], 5, "maximize key"))
        key("super+Down")
        key("super+Down")
        check("Super+Down restores, then minimizes",
              lambda: e.wait(lambda: e.window(w1["id"])["minimized"], 5, "minimize") and e.window(w2["id"])["focused"])
        e.msg("restore", w1["id"])
        check("minimized window can be restored", lambda: (lambda w: not w["minimized"] and w["focused"])(e.window(w1["id"])))

        key("alt+Tab")
        check("Alt+Tab switches to the other window", lambda: e.window(w2["id"])["focused"])

        # Workspaces.
        key("super+2")
        check("Super+2 switches workspace (desktop empty)",
              lambda: e.msg("workspaces")["active"] == 2 and e.screen().matches(W // 2, H // 2, BG))
        key("super+1")
        e.msg("focus", w2["id"])
        key("super+shift+3")
        check("Super+Shift+3 moves the focused window to workspace 3",
              lambda: e.window(w2["id"])["workspace"] == 3 and e.window(w1["id"])["focused"])
        key("super+3")
        check("its workspace shows it again", lambda: e.msg("workspaces")["active"] == 3 and e.window(w2["id"])["focused"])
        key("super+ctrl+Left")
        check("Super+Ctrl+Left goes to the previous workspace", lambda: e.msg("workspaces")["active"] == 2)
        key("super+1")

        # Clipboard between clients (wl-copy / wl-paste talk to the data device).
        subprocess.run(["wl-copy", "gideon-clipboard-ok"], env=e.wl_env, check=True, timeout=10)
        pasted = subprocess.run(["wl-paste", "--no-newline"], env=e.wl_env, capture_output=True, text=True, timeout=10).stdout
        check("clipboard: copy and paste between clients", lambda: pasted == "gideon-clipboard-ok")

        # Application launching.
        before = len(e.windows())
        key("super+Return")
        check("Super+Enter launches the terminal (configured command)", lambda: e.wait(lambda: len(e.windows()) == before + 1, 10, "terminal"))

        # Close button on the focused window.
        tw = next(w for w in e.windows() if w["focused"])
        e.xdo("mousemove", tw["x"] + tw["width"] - 28 + 10, tw["y"] - TITLE_H // 2, "click", 1)
        check("close button closes the window", lambda: e.wait(lambda: all(w["id"] != tw["id"] for w in e.windows()), 5, "close"))

        # Display scaling: new clients see the output scale.
        e.msg("set-scale", out["name"], 2)
        log3 = e.probe()
        check("output scale 2 is advertised to clients",
              lambda: e.wait(lambda: "scale=2" in open(log3).read(), 10, "scale"))
        check("logical output size halves at scale 2", lambda: e.msg("outputs")[0]["width"] == W // 2)
        e.msg("set-scale", out["name"], 1)

        # Print key saves a screenshot.
        key("Print")
        pics = os.path.join(e.tmp, "Pictures")
        check("Print saves a PNG screenshot", lambda: e.wait(lambda: os.path.isdir(pics) and any(n.endswith(".png") for n in os.listdir(pics)), 5, "png"))
    except Fail as ex:
        failures += 1
        print(f"  FAIL aborted: {ex}")
    finally:
        e.close(keep or failures > 0)
    print("PASS" if failures == 0 else f"FAILED ({failures})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
