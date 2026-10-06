"""Minimal QEMU serial-console driver for GideonOS integration tests.

Boots an ISO headless, exposes the serial console as a stream, and lets a test
wait for patterns and run shell commands. Standard library only.
"""
import os
import re
import shutil
import subprocess
import threading
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\r")
OVMF_PATHS = ["/usr/share/ovmf/OVMF.fd", "/usr/share/OVMF/OVMF_CODE.fd",
              "/usr/share/edk2/x64/OVMF.fd", "/usr/share/edk2-ovmf/x64/OVMF.fd"]


class TestFailure(Exception):
    pass


class QemuVM:
    def __init__(self, iso, log_path, uefi=False, mem="1G", extra=()):
        qemu = shutil.which("qemu-system-x86_64")
        if not qemu:
            raise TestFailure("qemu-system-x86_64 not installed (see tools/check-deps.sh)")
        args = [qemu, "-m", mem, "-smp", "2", "-cdrom", iso, "-boot", "d",
                "-nographic", "-no-reboot",
                "-nic", "user,model=virtio-net-pci", "-device", "virtio-rng-pci"]
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
                                     stderr=subprocess.STDOUT)
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
