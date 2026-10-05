#!/usr/bin/env python3
"""
Lossless Compressor - pack files and folders into one smaller archive and
unpack them later bit-for-bit identical (no lost pixels, quality or detail).

How it works
  * Everything is stored in a standard .tar.xz archive (LZMA, max preset).
    LZMA is a *lossless* algorithm: decompression restores the exact bytes.
  * Every file's SHA-256 hash is saved inside the archive. After compressing,
    the archive is re-read and checked against the originals, and the same
    check runs again on extract, so you know nothing changed.
  * The archive is a normal .tar.xz, so 7-Zip, WinRAR, tar etc. can open it too.

Usage
  python lossless_compressor.py                      # opens the window (GUI)
  python lossless_compressor.py compress OUT.tar.xz FILE_OR_FOLDER [...]
  python lossless_compressor.py extract ARCHIVE.tar.xz [DEST_FOLDER]
  python lossless_compressor.py list ARCHIVE.tar.xz

Only the Python standard library is used (Python 3.8+).
"""

import argparse
import hashlib
import io
import json
import lzma
import os
import sys
import tarfile
import time

MANIFEST_NAME = ".lossless_manifest.json"
EXTENSION = ".tar.xz"
CHUNK = 1024 * 1024
XZ_PRESET = 9 | lzma.PRESET_EXTREME


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def sha256_stream(fileobj):
    h = hashlib.sha256()
    for block in iter(lambda: fileobj.read(CHUNK), b""):
        h.update(block)
    return h.hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return sha256_stream(f)


def collect(inputs):
    """Return a list of (source_path, name_in_archive) for every file/folder."""
    entries = []
    seen = set()
    for raw in inputs:
        src = os.path.abspath(raw)
        if not os.path.exists(src):
            raise FileNotFoundError(f"Not found: {raw}")
        base = os.path.basename(src.rstrip(os.sep)) or "root"
        name = base
        i = 2
        while name in seen:  # two inputs with the same name
            name = f"{base}_{i}"
            i += 1
        seen.add(name)
        entries.append((src, name))
        if os.path.isdir(src) and not os.path.islink(src):
            for root, dirs, files in os.walk(src):
                dirs.sort()
                rel_root = os.path.relpath(root, src)
                for d in dirs:
                    p = os.path.join(root, d)
                    entries.append((p, _arc(name, rel_root, d)))
                for f in sorted(files):
                    p = os.path.join(root, f)
                    entries.append((p, _arc(name, rel_root, f)))
    return entries


def _arc(top, rel_root, leaf):
    parts = [top] + ([] if rel_root == "." else rel_root.split(os.sep)) + [leaf]
    return "/".join(parts)


def compress(output, inputs, log=print):
    if not output.endswith(EXTENSION):
        output += EXTENSION
    out_abs = os.path.abspath(output)
    entries = [e for e in collect(inputs) if os.path.abspath(e[0]) != out_abs]

    manifest = {"format": 1, "created": time.time(), "files": {}}
    original_size = 0
    start = time.time()

    with tarfile.open(output, "w:xz", preset=XZ_PRESET) as tar:
        for src, arcname in entries:
            info = tar.gettarinfo(src, arcname)
            if info is None:  # sockets, devices, etc.
                log(f"  skipped (unsupported type): {src}")
                continue
            if info.isreg():
                digest = sha256_file(src)
                manifest["files"][arcname] = {"sha256": digest, "size": info.size}
                original_size += info.size
                with open(src, "rb") as f:
                    tar.addfile(info, f)
                log(f"  added {arcname} ({human(info.size)})")
            else:
                tar.addfile(info)
        data = json.dumps(manifest, indent=1).encode("utf-8")
        minfo = tarfile.TarInfo(MANIFEST_NAME)
        minfo.size = len(data)
        minfo.mtime = int(time.time())
        tar.addfile(minfo, io.BytesIO(data))

    log("Verifying archive against the original files...")
    problems = verify(output)
    if problems:
        raise RuntimeError("Verification FAILED:\n  " + "\n  ".join(problems))

    packed = os.path.getsize(output)
    saved = (1 - packed / original_size) * 100 if original_size else 0.0
    log(
        f"Done in {time.time() - start:.1f}s: {len(manifest['files'])} files, "
        f"{human(original_size)} -> {human(packed)} ({saved:.1f}% smaller). "
        f"Verified lossless."
    )
    return output


def read_manifest(tar):
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError:
        return None
    return json.loads(tar.extractfile(member).read().decode("utf-8"))


def verify(archive):
    """Check every file stored in the archive against its saved SHA-256."""
    problems = []
    with tarfile.open(archive, "r:xz") as tar:
        manifest = read_manifest(tar)
        if manifest is None:
            return ["archive has no manifest (not made by this tool?)"]
        expected = dict(manifest["files"])
        for member in tar:
            if member.isreg() and member.name in expected:
                digest = sha256_stream(tar.extractfile(member))
                if digest != expected.pop(member.name)["sha256"]:
                    problems.append(f"content differs: {member.name}")
        problems += [f"missing from archive: {n}" for n in expected]
    return problems


def _safe_members(tar, dest):
    """Refuse entries that would write outside the destination folder."""
    dest = os.path.realpath(dest)
    for m in tar.getmembers():
        if m.name == MANIFEST_NAME:
            continue
        target = os.path.realpath(os.path.join(dest, m.name))
        if os.path.commonpath([dest, target]) != dest:
            raise RuntimeError(f"Unsafe path in archive, refusing: {m.name}")
        if m.issym() or m.islnk():
            link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
            if os.path.commonpath([dest, link]) != dest:
                raise RuntimeError(f"Unsafe link in archive, refusing: {m.name}")
        if m.isdev():
            continue
        yield m


def extract(archive, dest=None, log=print):
    if dest is None:
        name = os.path.basename(archive)
        if name.endswith(EXTENSION):
            name = name[: -len(EXTENSION)]
        dest = os.path.join(os.path.dirname(os.path.abspath(archive)), name)
    os.makedirs(dest, exist_ok=True)
    start = time.time()

    with tarfile.open(archive, "r:xz") as tar:
        manifest = read_manifest(tar)
        members = list(_safe_members(tar, dest))
        for m in members:
            log(f"  extracting {m.name}")
        kwargs = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(dest, members=members, **kwargs)

    if manifest is None:
        log("No manifest found; extracted without verification.")
        return dest

    log("Verifying extracted files...")
    bad = []
    for arcname, meta in manifest["files"].items():
        path = os.path.join(dest, *arcname.split("/"))
        if not os.path.isfile(path) or sha256_file(path) != meta["sha256"]:
            bad.append(arcname)
    if bad:
        raise RuntimeError("Extracted files do not match:\n  " + "\n  ".join(bad))
    log(
        f"Done in {time.time() - start:.1f}s: {len(manifest['files'])} files restored "
        f"to {dest}. Every file is byte-for-byte identical to the original."
    )
    return dest


def list_archive(archive, log=print):
    with tarfile.open(archive, "r:xz") as tar:
        total = 0
        for m in tar:
            if m.name == MANIFEST_NAME:
                continue
            kind = "dir " if m.isdir() else "file"
            log(f"  {kind} {human(m.size) if m.isreg() else '':>10}  {m.name}")
            total += m.size if m.isreg() else 0
    packed = os.path.getsize(archive)
    log(f"Original size {human(total)}, compressed {human(packed)}")


# ----------------------------------------------------------------------------- GUI

def run_gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("Lossless Compressor")
    root.geometry("720x520")

    items = []

    top = tk.Frame(root, padx=10, pady=10)
    top.pack(fill="both", expand=True)

    tk.Label(top, text="Files and folders to compress:", anchor="w").pack(fill="x")
    listbox = tk.Listbox(top, height=8, selectmode="extended")
    listbox.pack(fill="both", expand=True, pady=(2, 6))

    def add_files():
        for p in filedialog.askopenfilenames(title="Add files"):
            items.append(p)
            listbox.insert("end", p)

    def add_folder():
        p = filedialog.askdirectory(title="Add folder")
        if p:
            items.append(p)
            listbox.insert("end", p + os.sep)

    def remove_selected():
        for i in reversed(listbox.curselection()):
            listbox.delete(i)
            del items[i]

    buttons = tk.Frame(top)
    buttons.pack(fill="x")
    tk.Button(buttons, text="Add files...", command=add_files).pack(side="left")
    tk.Button(buttons, text="Add folder...", command=add_folder).pack(side="left", padx=4)
    tk.Button(buttons, text="Remove selected", command=remove_selected).pack(side="left")

    log_box = scrolledtext.ScrolledText(top, height=12, state="disabled")
    log_box.pack(fill="both", expand=True, pady=(8, 6))

    def log(msg):
        def write():
            log_box.configure(state="normal")
            log_box.insert("end", msg + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")
        root.after(0, write)

    action_buttons = []

    def run_job(job):
        for b in action_buttons:
            b.configure(state="disabled")

        def worker():
            try:
                job()
            except Exception as e:  # show any error in a dialog
                log(f"ERROR: {e}")
                root.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                root.after(0, lambda: [b.configure(state="normal") for b in action_buttons])

        threading.Thread(target=worker, daemon=True).start()

    def do_compress():
        if not items:
            messagebox.showinfo("Nothing to do", "Add some files or folders first.")
            return
        out = filedialog.asksaveasfilename(
            title="Save compressed archive as",
            defaultextension=EXTENSION,
            filetypes=[("Compressed archive", "*" + EXTENSION)],
        )
        if out:
            run_job(lambda: compress(out, list(items), log=log))

    def do_extract():
        archive = filedialog.askopenfilename(
            title="Choose archive to extract",
            filetypes=[("Compressed archive", "*" + EXTENSION), ("All files", "*")],
        )
        if not archive:
            return
        dest = filedialog.askdirectory(title="Extract into which folder?")
        if dest:
            run_job(lambda: extract(archive, dest, log=log))

    bottom = tk.Frame(top)
    bottom.pack(fill="x")
    b1 = tk.Button(bottom, text="Compress", width=16, command=do_compress)
    b2 = tk.Button(bottom, text="Extract archive...", width=16, command=do_extract)
    b1.pack(side="left")
    b2.pack(side="right")
    action_buttons.extend([b1, b2])

    root.mainloop()


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        try:
            run_gui()
            return 0
        except ImportError:
            print("tkinter is not available; use the command line (see --help).")
            return 1

    parser = argparse.ArgumentParser(description="Lossless file/folder compressor.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compress", help="pack files/folders into an archive")
    c.add_argument("output", help="archive to create (.tar.xz is added if missing)")
    c.add_argument("inputs", nargs="+", help="files and folders to include")
    x = sub.add_parser("extract", help="unpack an archive")
    x.add_argument("archive")
    x.add_argument("dest", nargs="?", help="destination folder (default: next to archive)")
    l = sub.add_parser("list", help="show what is inside an archive")
    l.add_argument("archive")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "compress":
            compress(args.output, args.inputs)
        elif args.cmd == "extract":
            extract(args.archive, args.dest)
        else:
            list_archive(args.archive)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
