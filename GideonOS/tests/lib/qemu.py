"""Minimal QEMU serial-console driver for GideonOS integration tests.

Boots an ISO headless, exposes the serial console as a stream, and lets a test
wait for patterns and run shell commands. Standard library only.
"""
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\r")
OVMF_PATHS = ["/usr/share/ovmf/OVMF.fd", "/usr/share/OVMF/OVMF_CODE.fd",
              "/usr/share/edk2/x64/OVMF.fd", "/usr/share/edk2-ovmf/x64/OVMF.fd"]


class TestFailure(Exception):
    pass


class Image:
    """A screenshot (binary PPM, P6)."""

    def __init__(self, width, height, data):
        self.width, self.height, self.data = width, height, data

    @classmethod
    def from_ppm(cls, path):
        with open(path, "rb") as f:
            raw = f.read()
        fields, pos = [], 0
        while len(fields) < 4:                 # magic, width, height, maxval
            while raw[pos:pos + 1].isspace():
                pos += 1
            if raw[pos:pos + 1] == b"#":
                pos = raw.index(b"\n", pos) + 1
                continue
            end = pos
            while not raw[end:end + 1].isspace():
                end += 1
            fields.append(raw[pos:end])
            pos = end
        if fields[0] != b"P6":
            raise TestFailure(f"unexpected screendump format {fields[0]!r}")
        w, h = int(fields[1]), int(fields[2])
        return cls(w, h, raw[pos + 1:pos + 1 + w * h * 3])

    def pixel(self, x, y):
        i = (y * self.width + x) * 3
        return tuple(self.data[i:i + 3])

    def matches(self, x, y, rgb_hex, tolerance=6):
        want = (int(rgb_hex[0:2], 16), int(rgb_hex[2:4], 16), int(rgb_hex[4:6], 16))
        return all(abs(a - b) <= tolerance for a, b in zip(self.pixel(x, y), want))


class QemuVM:
    def __init__(self, iso, log_path, uefi=False, mem="2G", extra=(), usb=(), outputs=1, display="auto"):
        qemu = shutil.which("qemu-system-x86_64")
        if not qemu:
            raise TestFailure("qemu-system-x86_64 not installed (see tools/check-deps.sh)")
        self.tmp = tempfile.mkdtemp(prefix="gideon-qemu-")
        self.xvfb = None
        env = dict(os.environ)
        # QEMU enables virtio-gpu heads beyond the first only when a UI reports
        # a window for them; headless (-display none) shows one. For multi-head
        # tests run the GTK UI on a private virtual X server.
        self.multihead = False
        if display == "auto":
            display = "none"
            if outputs > 1 and shutil.which("Xvfb"):
                r, w = os.pipe()
                self.xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                             pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.close(w)
                with os.fdopen(r) as f:
                    env["DISPLAY"] = ":" + f.readline().strip()
                display, self.multihead = "gtk,show-tabs=on,zoom-to-fit=on", True
        self.mon_path = os.path.join(self.tmp, "monitor.sock")
        args = [qemu, "-m", mem, "-smp", "2", "-cdrom", iso, "-boot", "d",
                "-display", display, "-serial", "stdio", "-no-reboot",
                "-monitor", f"unix:{self.mon_path},server=on,wait=off",
                "-nic", "user,model=virtio-net-pci", "-device", "virtio-rng-pci",
                "-device", "qemu-xhci,id=xhci",
                # Display: virtio GPU (KMS) with `outputs` heads; absolute pointer.
                "-vga", "none", "-device", f"virtio-vga,max_outputs={outputs},id=gpu",
                "-device", "usb-tablet,bus=xhci.0"]
        for i, img in enumerate(usb):  # USB sticks present at boot
            args += ["-drive", f"if=none,id=usb{i},format=raw,file={img}",
                     "-device", f"usb-storage,bus=xhci.0,drive=usb{i},id=stick{i},removable=on"]
        if os.access("/dev/kvm", os.W_OK):
            args += ["-enable-kvm", "-cpu", "host"]
        else:
            args += ["-cpu", "max"]
        if uefi:
            fw = next((p for p in OVMF_PATHS if os.path.exists(p)), None)
            if not fw:
                raise TestFailure("UEFI requested but OVMF firmware not found")
            args += ["-bios", fw]
        args += list(extra)
        self.log = open(log_path, "wb")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
        self.raw = b""          # raw output not yet consumed by expect()
        self.lock = threading.Condition()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk:
                break
            self.log.write(chunk)
            self.log.flush()
            with self.lock:
                self.raw += chunk
                self.lock.notify_all()
        with self.lock:
            self.lock.notify_all()

    def _clean(self):
        # Strip ANSI on the whole pending buffer so sequences split across reads are handled.
        return ANSI.sub(b"", self.raw)

    def expect(self, pattern, timeout):
        """Wait for regex `pattern`; consume output up to the match and return it."""
        rx = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                buf = self._clean()
                m = rx.search(buf)
                if m:
                    # Consume through the match; keep anything after it (re-cleaned later).
                    self.raw = buf[m.end():]
                    return buf[:m.end()].decode(errors="replace"), m
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.proc.poll() is not None:
                    tail = buf[-600:].decode(errors="replace")
                    why = "QEMU exited" if self.proc.poll() is not None else f"timeout after {timeout}s"
                    raise TestFailure(f"{why} waiting for {pattern!r}; last output:\n{tail}")
                self.lock.wait(min(remaining, 1.0))

    def send(self, text):
        self.proc.stdin.write(text.encode())
        self.proc.stdin.flush()

    _seq = 0

    def run(self, command, timeout=60):
        """Run a shell command on the serial console; return (exit_status, output).

        Output is bracketed by begin/end markers, so prompts and command echo never leak
        into it. Markers are assembled at runtime so the echoed command line cannot match.
        """
        QemuVM._seq += 1
        b, e = f"__GDN_B{QemuVM._seq}__", f"__GDN_E{QemuVM._seq}__"
        self.send(f"echo '{b[:5]}''{b[5:]}'; {command}; echo \"{e[:5]}\"\"{e[5:]}:$?\"\n")
        self.expect(re.escape(b) + r"\n", timeout)
        out, m = self.expect(re.escape(e) + r":(\d+)", timeout)
        return int(m.group(1)), out[:m.start()].strip()

    def monitor(self, command, timeout=10):
        """Run a QEMU human-monitor command (e.g. system_powerdown); return its output."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as m:
            m.settimeout(timeout)
            m.connect(self.mon_path)
            def until_prompt():
                data = b""
                while not data.endswith(b"(qemu) "):
                    chunk = m.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                return data
            until_prompt()
            m.sendall(command.encode() + b"\n")
            out = ANSI.sub(b"", until_prompt()).decode(errors="replace")
        lines = out.splitlines()[1:-1]  # drop echoed command and trailing prompt
        return "\n".join(lines).strip()

    def screendump(self, head=0):
        """Capture display head `head`; returns Image(width, height, pixel(x, y) -> (r, g, b))."""
        path = os.path.join(self.tmp, f"screen{head}.ppm")
        if os.path.exists(path):
            os.remove(path)
        out = self.monitor(f"screendump {path} gpu {head}")
        for _ in range(50):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.1)
        else:
            raise TestFailure(f"screendump of head {head} failed: {out}")
        return Image.from_ppm(path)

    def login(self, user, password, timeout=300):
        """Log in on the serial console; returns once a shell prompt appears."""
        self.expect(r"login: $", timeout)
        self.send(user + "\n")
        self.expect(r"Password: $", 30)
        self.send(password + "\n")
        self.expect(r"[#$] $", 30)
        self.quiet_shell()

    def quiet_shell(self):
        """Keep transcripts to command output: no echo, pager or colours."""
        self.run("stty -echo; export SYSTEMD_PAGER=cat PAGER=cat SYSTEMD_COLORS=0")

    def su(self, password):
        """Become root in the current shell session (exit() to leave)."""
        self.send("su\n")
        self.expect(r"Password: $", 30)
        self.send(password + "\n")
        self.expect(r"[#$] $", 30)
        self.quiet_shell()

    def exit_shell(self):
        self.send("exit\n")

    def wait_exit(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise TestFailure(f"QEMU did not exit within {timeout}s")

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        if self.xvfb:
            self.xvfb.kill()
            self.xvfb.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)
