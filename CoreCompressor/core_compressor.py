#!/usr/bin/env python3
"""
Core Compressor - pack files and folders into one smaller archive and
unpack them later bit-for-bit identical (no lost pixels, quality or detail).

How it works
  * Everything is stored in a standard .tar.xz archive (LZMA compression).
    LZMA is a *lossless* algorithm: decompression restores the exact bytes.
  * Every file's SHA-256 hash is saved inside the archive. After compressing,
    the archive is re-read and checked against the originals, and the same
    check runs again on extract, so you know nothing changed.
  * The archive is a normal .tar.xz, so 7-Zip, WinRAR, tar etc. can open it too.

Usage
  python core_compressor.py                      # opens the window (GUI)
  python core_compressor.py compress OUT.tar.xz FILE_OR_FOLDER [...]
  python core_compressor.py extract ARCHIVE.tar.xz [DEST_FOLDER]
  python core_compressor.py list ARCHIVE.tar.xz

Only the Python standard library is used (Python 3.8+). If the optional
package "tkinterdnd2" is installed, you can drag files onto the window.
"""

import argparse
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor

MANIFEST_NAME = ".lossless_manifest.json"
EXTENSION = ".tar.xz"
CHUNK = 1024 * 1024
LEVELS = {
    "best": 9 | lzma.PRESET_EXTREME,  # smallest archive, slowest
    "normal": 6,
    "fast": 1,
}
CPU_COUNT = os.cpu_count() or 1
BLOCK_SIZE = 16 * 1024 * 1024  # each block is compressed on its own CPU core
BEST_MAX_THREADS = 8  # "best" needs ~200 MB of memory per core
# Extraction filter (Python 3.11.4+). Paths are also checked by _check_safe.
EXTRACT_KW = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}


class Cancelled(Exception):
    pass


def _noop(*_args):
    pass


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


class _Reader:
    """File wrapper that counts bytes, optionally hashes them, and can cancel."""

    def __init__(self, f, on_read=_noop, cancel=None, hasher=None):
        self.f, self.on_read, self.cancel, self.hasher = f, on_read, cancel, hasher
        self.pos = 0

    def read(self, n=-1):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        data = self.f.read(n)
        if self.hasher is not None:
            self.hasher.update(data)
        self.pos += len(data)
        self.on_read(len(data))
        return data

    def seek(self, *args):
        self.pos = self.f.seek(*args)
        return self.pos

    def tell(self):
        return self.f.tell()

    def seekable(self):
        return True

    def readable(self):
        return True


# ----------------------------------------------------------------------------- multi-core xz

def _vli(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _stored_xz(data):
    """A standard .xz stream that holds data without compressing it (LZMA2 "stored"
    chunks). Used for data that is already compressed, such as video, PNG and most
    RAW photos: trying to compress it again takes ages and saves almost nothing."""
    crc = zlib.crc32
    flags = b"\x00\x01"  # check type CRC32
    out = [b"\xfd7zXZ\x00", flags, struct.pack("<I", crc(flags))]
    # Block header: size, flags, LZMA2 filter id, 1 property byte (128 KiB dictionary).
    header = bytes([2, 0x00, 0x21, 0x01, 10]) + b"\x00" * 3
    header += struct.pack("<I", crc(header))
    body = bytearray()
    for i in range(0, len(data), 65536):
        piece = data[i:i + 65536]
        body += bytes([1 if i == 0 else 2]) + struct.pack(">H", len(piece) - 1) + piece
    body += b"\x00"
    unpadded = len(header) + len(body) + 4
    out += [header, bytes(body), b"\x00" * (-len(body) % 4), struct.pack("<I", crc(data))]
    index = b"\x00" + _vli(1) + _vli(unpadded) + _vli(len(data))
    index += b"\x00" * (-len(index) % 4)
    index += struct.pack("<I", crc(index))
    backward = struct.pack("<I", len(index) // 4 - 1) + flags
    out += [index, struct.pack("<I", crc(backward)), backward, b"YZ"]
    return b"".join(out)


def _looks_compressed(data, samples=3, size=128 * 1024):
    """Quick test on a few small samples: does this block barely compress?"""
    if len(data) < 4 * size:
        return False
    step = (len(data) - size) // (samples - 1)
    sample = b"".join(data[i * step:i * step + size] for i in range(samples))
    return len(lzma.compress(sample, preset=0)) > 0.97 * len(sample)


def _compress_block(data, filters):
    if _looks_compressed(data):
        return _stored_xz(data), True
    return lzma.compress(data, format=lzma.FORMAT_XZ, filters=filters), False


class _ParallelXZ:
    """Write-only file object: cuts the stream into blocks, compresses them on several
    CPU cores at once, and writes the results in order. Each block becomes its own
    .xz stream; streams placed one after another are still one valid .xz file."""

    def __init__(self, raw, level, threads):
        self.raw = raw
        self.filters = [{"id": lzma.FILTER_LZMA2, "preset": LEVELS[level]}]
        if level == "best":  # its 64 MB dictionary is bigger than a block; cap it to save memory
            self.filters[0]["dict_size"] = BLOCK_SIZE
        if level == "best":
            threads = min(threads, BEST_MAX_THREADS)
        self.threads = max(1, threads)
        self.pool = ThreadPoolExecutor(self.threads)
        self.pending = deque()
        self.buf = bytearray()
        self.pos = 0
        self.stored_bytes = 0

    def write(self, data):
        self.buf += data
        self.pos += len(data)
        while len(self.buf) >= BLOCK_SIZE:
            self._submit(bytes(self.buf[:BLOCK_SIZE]))
            del self.buf[:BLOCK_SIZE]
        return len(data)

    def tell(self):
        return self.pos

    def _submit(self, block):
        self.pending.append((len(block), self.pool.submit(_compress_block, block, self.filters)))
        while len(self.pending) > self.threads * 2:
            self._write_oldest()

    def _write_oldest(self):
        size, future = self.pending.popleft()
        data, stored = future.result()
        if stored:
            self.stored_bytes += size
        self.raw.write(data)

    def finish(self):
        if self.buf:
            self._submit(bytes(self.buf))
            self.buf.clear()
        while self.pending:
            self._write_oldest()
        self.pool.shutdown()

    def abort(self):
        for _, future in self.pending:
            future.cancel()
        self.pending.clear()
        self.pool.shutdown()


def sha256_stream(fileobj, cancel=None, on_read=_noop):
    h = hashlib.sha256()
    reader = _Reader(fileobj, on_read, cancel, h)
    while reader.read(CHUNK):
        pass
    return h.hexdigest()


def sha256_file(path, cancel=None, on_read=_noop):
    with open(path, "rb") as f:
        return sha256_stream(f, cancel, on_read)


def collect(inputs):
    """Return a list of (source_path, name_in_archive) for every file/folder."""
    entries = []
    seen = set()
    for raw in inputs:
        src = os.path.abspath(raw)
        if not os.path.lexists(src):
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
                    entries.append((os.path.join(root, d), _arc(name, rel_root, d)))
                for f in sorted(files):
                    entries.append((os.path.join(root, f), _arc(name, rel_root, f)))
    return entries


def _arc(top, rel_root, leaf):
    parts = [top] + ([] if rel_root == "." else rel_root.split(os.sep)) + [leaf]
    return "/".join(parts)


def _is_regular(path):
    return os.path.isfile(path) and not os.path.islink(path)


def _add_entries(tar, entries, manifest, log, on_read, cancel):
    for src, arcname in entries:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        info = tar.gettarinfo(src, arcname)
        if info is None:  # sockets, devices, etc.
            log(f"  skipped (unsupported type): {src}")
            continue
        if info.isreg():
            h = hashlib.sha256()
            with open(src, "rb") as f:
                tar.addfile(info, _Reader(f, on_read, cancel, h))
            manifest["files"][arcname] = {"sha256": h.hexdigest(), "size": info.size}
            log(f"  added {arcname} ({human(info.size)})")
        else:
            tar.addfile(info)
    data = json.dumps(manifest, indent=1).encode("utf-8")
    minfo = tarfile.TarInfo(MANIFEST_NAME)
    minfo.size = len(data)
    minfo.mtime = int(time.time())
    tar.addfile(minfo, io.BytesIO(data))


def compress(output, inputs, level="best", log=print, progress=_noop, cancel=None,
             threads=CPU_COUNT):
    """Create an archive. progress(fraction, text) is called as work proceeds.
    threads = how many CPU cores compress at the same time."""
    if not output.endswith(EXTENSION):
        output += EXTENSION
    out_abs = os.path.abspath(output)
    part = output + ".part"
    skip = {out_abs, os.path.abspath(part)}
    entries = [e for e in collect(inputs) if os.path.abspath(e[0]) not in skip]
    total = sum(os.path.getsize(src) for src, _ in entries if _is_regular(src)) or 1

    manifest = {"format": 1, "created": time.time(), "files": {}}
    start = time.time()
    done = 0

    def on_read(n):
        nonlocal done
        done += n
        progress(0.85 * done / total, f"Compressing... {human(done)} of {human(total)}")

    try:
        with open(part, "wb") as raw:
            xz = _ParallelXZ(raw, level, threads)
            log(f"Using {xz.threads} CPU core(s).")
            try:
                tar = tarfile.open(fileobj=xz, mode="w")
                _add_entries(tar, entries, manifest, log, on_read, cancel)
                tar.close()
                xz.finish()
            except BaseException:
                xz.abort()
                raise
        original_size = sum(meta["size"] for meta in manifest["files"].values())
        if xz.stored_bytes:
            log(f"{human(xz.stored_bytes)} was already compressed (video, PNG, RAW...) "
                "and was stored as-is to save time.")

        log("Verifying archive against the original files...")
        problems = verify(part, progress=progress, cancel=cancel, base=0.85, span=0.15)
        if problems:
            raise RuntimeError("Verification FAILED:\n  " + "\n  ".join(problems))
        os.replace(part, output)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise

    packed = os.path.getsize(output)
    stats = {
        "output": output,
        "files": len(manifest["files"]),
        "original": original_size,
        "packed": packed,
        "saved": (1 - packed / original_size) * 100 if original_size else 0.0,
        "seconds": time.time() - start,
    }
    progress(1.0, "Done")
    log(
        f"Done in {stats['seconds']:.1f}s: {stats['files']} files, "
        f"{human(original_size)} -> {human(packed)} ({stats['saved']:.1f}% smaller). "
        f"Verified lossless."
    )
    return stats


def _open_archive(f, archive, on_progress, cancel):
    size = os.path.getsize(archive) or 1
    raw = _Reader(f, lambda n: on_progress(raw.pos / size), cancel)
    return tarfile.open(fileobj=raw, mode="r:xz")


def _load_manifest(tar, member):
    return json.loads(tar.extractfile(member).read().decode("utf-8"))


def verify(archive, progress=_noop, cancel=None, base=0.0, span=1.0):
    """Check every file stored in the archive against its saved SHA-256."""
    hashes = {}
    manifest = None
    report = lambda frac: progress(base + span * frac, "Verifying...")
    with open(archive, "rb") as f, _open_archive(f, archive, report, cancel) as tar:
        for member in tar:
            if member.name == MANIFEST_NAME:
                manifest = _load_manifest(tar, member)
            elif member.isreg():
                hashes[member.name] = sha256_stream(tar.extractfile(member), cancel)
    if manifest is None:
        return ["archive has no manifest (not made by this tool?)"]
    problems = []
    for name, meta in manifest["files"].items():
        if name not in hashes:
            problems.append(f"missing from archive: {name}")
        elif hashes[name] != meta["sha256"]:
            problems.append(f"content differs: {name}")
    return problems


def _check_safe(m, dest):
    """Refuse entries that would write outside the destination folder."""
    target = os.path.realpath(os.path.join(dest, m.name))
    if os.path.commonpath([dest, target]) != dest:
        raise RuntimeError(f"Unsafe path in archive, refusing: {m.name}")
    if m.issym() or m.islnk():
        base = os.path.dirname(target) if m.issym() else dest
        link = os.path.realpath(os.path.join(base, m.linkname))
        if os.path.commonpath([dest, link]) != dest:
            raise RuntimeError(f"Unsafe link in archive, refusing: {m.name}")


def default_extract_dir(archive):
    name = os.path.basename(archive)
    if name.endswith(EXTENSION):
        name = name[: -len(EXTENSION)]
    return os.path.join(os.path.dirname(os.path.abspath(archive)), name)


def extract(archive, dest=None, log=print, progress=_noop, cancel=None):
    """Unpack an archive and verify every restored file."""
    dest = dest or default_extract_dir(archive)
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    start = time.time()
    manifest = None
    dirs = []

    report = lambda frac: progress(0.8 * frac, "Extracting...")
    with open(archive, "rb") as f, _open_archive(f, archive, report, cancel) as tar:
        for m in tar:
            if m.name == MANIFEST_NAME:
                manifest = _load_manifest(tar, m)
                continue
            _check_safe(m, dest_real)
            if m.isdev():
                continue
            if m.isdir():
                os.makedirs(os.path.join(dest, m.name), exist_ok=True)
                dirs.append(m)
                continue
            log(f"  extracting {m.name}")
            tar.extract(m, dest, **EXTRACT_KW)
        # Folders last, so their dates are not changed by the files written into them.
        tar.extractall(dest, members=dirs, **EXTRACT_KW)

    if manifest is None:
        log("No manifest found; extracted without verification.")
        progress(1.0, "Done")
        return {"dest": dest, "files": None, "original": None, "seconds": time.time() - start}

    log("Verifying extracted files...")
    total = sum(meta["size"] for meta in manifest["files"].values()) or 1
    done = 0

    def on_read(n):
        nonlocal done
        done += n
        progress(0.8 + 0.2 * done / total, "Verifying...")

    bad = []
    for arcname, meta in manifest["files"].items():
        path = os.path.join(dest, *arcname.split("/"))
        if not os.path.isfile(path) or sha256_file(path, cancel, on_read) != meta["sha256"]:
            bad.append(arcname)
    if bad:
        raise RuntimeError("Extracted files do not match:\n  " + "\n  ".join(bad))
    stats = {
        "dest": dest,
        "files": len(manifest["files"]),
        "original": total if manifest["files"] else 0,
        "seconds": time.time() - start,
    }
    progress(1.0, "Done")
    log(
        f"Done in {stats['seconds']:.1f}s: {stats['files']} files restored to {dest}. "
        f"Every file is byte-for-byte identical to the original."
    )
    return stats


# ----------------------------------------------------------------------------- downloads

GOOGLE_EXPORTS = {  # Google Docs editors files are exported to Office formats
    "document": "docx",
    "spreadsheets": "xlsx",
    "presentation": "pptx",
}


def resolve_url(url):
    """Turn a share link (Google Drive, Docs, Dropbox) into a direct download link."""
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    query = urllib.parse.parse_qs(parsed.query)

    if host == "docs.google.com":
        m = re.match(r"/(document|spreadsheets|presentation)/d/([\w-]+)", parsed.path)
        if m:
            kind, file_id = m.groups()
            return f"https://docs.google.com/{kind}/d/{file_id}/export?format={GOOGLE_EXPORTS[kind]}"
    if host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        if "/folders/" in parsed.path:
            raise ValueError(
                "Google Drive folder links can't be downloaded directly. Open the folder, "
                "select everything, choose Download (Drive makes a ZIP), or share the "
                "individual files."
            )
        m = re.search(r"/file/d/([\w-]+)", parsed.path)
        file_id = m.group(1) if m else (query.get("id") or [None])[0]
        if file_id:
            return (
                "https://drive.usercontent.google.com/download?"
                f"id={file_id}&export=download&confirm=t"
            )
    if host.endswith("dropbox.com"):
        query["dl"] = ["1"]
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
        )
    return url


def _filename_from_response(resp, url):
    name = None
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
        if m:
            name = m.group(1).strip()
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(resp.url).path))
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name or "download"


def _unique_path(folder, name):
    path = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    if name.endswith(EXTENSION):
        stem, ext = name[: -len(EXTENSION)], EXTENSION
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({i}){ext}")
        i += 1
    return path


SPLIT_MIN = 32 * 1024 * 1024  # files at least this big are fetched over several connections
USER_AGENT = {"User-Agent": "Mozilla/5.0 CoreCompressor"}
_name_lock = threading.Lock()


class _AnyEvent:
    """Looks like a threading.Event that is set when any of the given events is set."""

    def __init__(self, *events):
        self.events = [e for e in events if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.events)


def _fetch_range(url, path, start, end, add, cancel, attempts=4):
    """Download bytes start..end (inclusive) into the same place in path, retrying."""
    pos = start
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={**USER_AGENT, "Range": f"bytes={pos}-{end}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status != 206:
                    raise RuntimeError("the server does not support split downloads")
                with open(path, "r+b") as f:
                    f.seek(pos)
                    while pos <= end:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        block = r.read(min(CHUNK, end - pos + 1))
                        if not block:
                            raise ConnectionError("connection closed early")
                        f.write(block)
                        pos += len(block)
                        add(len(block))
            return
        except (Cancelled, RuntimeError):
            raise
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def download(url, folder, log=print, progress=_noop, cancel=None, connections=1):
    """Download one link into folder. progress(fraction or None, text). Returns the path.
    Big files are fetched over `connections` connections at once when the server allows."""
    direct = resolve_url(url)
    req = urllib.request.Request(direct, headers=USER_AGENT)
    progress(None, f"Connecting to {urllib.parse.urlparse(direct).netloc}...")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (401, 403, 404) and "google" in direct:
            hint = ' Make sure the file is shared as "Anyone with the link".'
        raise RuntimeError(f"Download failed ({e.code} {e.reason}) for {url}.{hint}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not connect for {url}: {e.reason}") from None

    lock = threading.Lock()
    done = 0
    with resp:
        ctype = resp.headers.get("Content-Type", "")
        if "google" in direct and ctype.startswith("text/html"):
            raise RuntimeError(
                f"Google returned a web page instead of the file for {url}. The file is "
                'probably private: share it as "Anyone with the link" and try again.'
            )
        name = _filename_from_response(resp, direct)
        total = int(resp.headers.get("Content-Length") or 0)
        split = (connections > 1 and total >= SPLIT_MIN
                 and resp.headers.get("Accept-Ranges", "").lower() == "bytes")
        with _name_lock:  # several downloads may pick a name at the same moment
            path = _unique_path(folder, name)
            open(path, "wb").close()
        log(f"  downloading {name}" + (f" ({human(total)})" if total else "")
            + (f" over {connections} connections" if split else ""))

        def add(n):
            nonlocal done
            with lock:
                done += n
                current = done
            text = f"Downloading {name}... {human(current)}"
            if total:
                progress(min(current / total, 1.0), text + f" of {human(total)}")
            else:
                progress(None, text)

        try:
            if not split:
                with open(path, "wb") as out:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        add(len(block))
        except BaseException:
            os.remove(path)
            raise

    if split:
        try:
            with open(path, "r+b") as f:
                f.truncate(total)
            step = -(-total // connections)
            ranges = [(a, min(a + step, total) - 1) for a in range(0, total, step)]
            stop = threading.Event()
            either = _AnyEvent(cancel, stop)
            with ThreadPoolExecutor(len(ranges)) as pool:
                futures = [pool.submit(_fetch_range, direct, path, a, b, add, either)
                           for a, b in ranges]
                try:
                    for fut in futures:
                        fut.result()
                except BaseException:
                    stop.set()
                    raise
        except BaseException:
            os.remove(path)
            raise

    if total and done < total:
        os.remove(path)
        raise RuntimeError(f"Download of {name} was cut off ({human(done)} of {human(total)}).")
    log(f"  downloaded {name} ({human(done)})")
    return path


def download_and_compress(urls, output, level="best", keep_originals=False, log=print,
                          progress=_noop, cancel=None, threads=CPU_COUNT,
                          parallel=1, connections=1):
    """Download every link (`parallel` at a time), then pack them all into one verified
    archive using `threads` CPU cores."""
    urls = [u.strip() for u in urls if u.strip()]
    if not urls:
        raise ValueError("No links given.")
    if not output.endswith(EXTENSION):
        output += EXTENSION
    folder = os.path.dirname(os.path.abspath(output))
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".downloading-", dir=folder)
    fractions = [0.0] * len(urls)
    stop = threading.Event()
    either = _AnyEvent(cancel, stop)

    def fetch(i, url):
        log(f"Link {i + 1} of {len(urls)}: {url}")

        def step(frac, text):
            if frac is not None:
                fractions[i] = frac
            busy = sum(1 for f in fractions if 0 < f < 1)
            if busy > 1:
                text = f"Downloading {busy} files at once..."
            progress(0.5 * sum(fractions) / len(urls), text)

        return download(url, work, log=log, progress=step, cancel=either,
                        connections=connections)

    try:
        with ThreadPoolExecutor(max(1, min(parallel, len(urls)))) as pool:
            futures = [pool.submit(fetch, i, u) for i, u in enumerate(urls)]
            try:
                files = [f.result() for f in futures]
            except BaseException:
                stop.set()
                raise

        log(f"Compressing {len(files)} downloaded file(s)...")
        stats = compress(output, files, level=level, log=log, cancel=cancel, threads=threads,
                         progress=lambda f, t: progress(0.5 + 0.5 * f, t))
        if keep_originals:
            for f in files:
                shutil.move(f, _unique_path(folder, os.path.basename(f)))
            log(f"Kept the original downloads in {folder}")
        return stats
    finally:
        shutil.rmtree(work, ignore_errors=True)


def contents(archive):
    """Return [(name, is_dir, size)] for everything in the archive."""
    with tarfile.open(archive, "r:xz") as tar:
        return [
            (m.name, m.isdir(), m.size if m.isreg() else 0)
            for m in tar
            if m.name != MANIFEST_NAME
        ]


def list_archive(archive, log=print):
    total = 0
    for name, is_dir, size in contents(archive):
        log(f"  {'dir ' if is_dir else 'file'} {'' if is_dir else human(size):>10}  {name}")
        total += size
    log(f"Original size {human(total)}, compressed {human(os.path.getsize(archive))}")


# ----------------------------------------------------------------------------- GUI

def open_in_file_manager(path):
    import subprocess

    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def folder_size(path):
    if not os.path.isdir(path) or os.path.islink(path):
        return os.path.getsize(path), 1
    size = count = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if _is_regular(p):
                size += os.path.getsize(p)
                count += 1
    return size, count


def run_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    import tkinter.font as tkfont

    if sys.platform == "win32":  # sharp text on high-DPI screens
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    try:  # optional drag and drop support
        from tkinterdnd2 import DND_FILES, TkinterDnD

        root = TkinterDnD.Tk()
    except Exception:
        DND_FILES = None
        root = tk.Tk()

    root.title("Core Compressor")
    root.geometry("860x640")
    root.minsize(680, 520)

    style = ttk.Style(root)
    if sys.platform == "win32":
        style.theme_use("vista")
    elif sys.platform != "darwin":
        style.theme_use("clam")

    base_font = tkfont.nametofont("TkDefaultFont")
    family = base_font.actual("family")
    size = max(base_font.actual("size"), 10)
    GREEN, RED, GREY, ACCENT = "#1a7f37", "#cf222e", "#57606a", "#0969da"
    style.configure("Title.TLabel", font=(family, size + 9, "bold"))
    style.configure("Sub.TLabel", font=(family, size), foreground=GREY)
    style.configure("Hint.TLabel", font=(family, size - 1), foreground=GREY)
    style.configure("Big.TLabel", font=(family, size + 4, "bold"))
    style.configure("Ok.TLabel", font=(family, size + 4, "bold"), foreground=GREEN)
    style.configure("Err.TLabel", font=(family, size + 1, "bold"), foreground=RED)
    style.configure("TNotebook.Tab", padding=(18, 6), font=(family, size + 1))
    style.configure("Treeview", rowheight=int(size * 2.4))
    style.configure("Accent.TButton", font=(family, size + 1, "bold"), padding=(18, 8))
    if style.theme_use() == "clam":
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#d0d7de")
        style.map(
            "Accent.TButton",
            background=[("disabled", "#8c959f"), ("active", "#0550ae"), ("!active", ACCENT)],
            foreground=[("!disabled", "white")],
        )

    ui_queue = queue.Queue()
    state = {"busy": None, "progress": None, "cancel": None}

    def call_ui(fn):
        ui_queue.put(fn)

    def log(msg):
        call_ui(lambda: _append_log(msg))

    def _append_log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    # ---- header
    header = ttk.Frame(root, padding=(18, 14, 18, 6))
    header.pack(fill="x")
    ttk.Label(header, text="Core Compressor", style="Title.TLabel").pack(anchor="w")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=14, pady=(6, 14))

    def make_tree(parent, columns, widths):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c for c, _ in columns], show="headings")
        for (key, title), width in zip(columns, widths):
            anchor = "e" if key == "size" else "w"
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=width, anchor=anchor, stretch=key in ("name", "path"))
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return frame, tree

    def path_row(parent, label, browse):
        row = ttk.Frame(parent)
        ttk.Label(row, text=label, width=12).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse...", command=browse).pack(side="left")
        return row, var

    def result_area(parent):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        main = ttk.Label(frame, text="", style="Big.TLabel")
        main.grid(row=0, column=0, sticky="w")
        detail = ttk.Label(frame, text="", style="Hint.TLabel")
        detail.grid(row=1, column=0, sticky="w")
        button = ttk.Button(frame, text="Open folder")
        button.grid(row=0, column=1, rowspan=2, sticky="e")
        button.grid_remove()
        return frame, main, detail, button

    # ---- Compress tab
    ctab = ttk.Frame(notebook, padding=14)
    notebook.add(ctab, text="Compress")

    items = {}  # tree item id -> path
    sizes = {}  # tree item id -> (bytes, file count)

    tools = ttk.Frame(ctab)
    tools.pack(fill="x")
    ttk.Label(tools, text="Files and folders to compress", style="Big.TLabel").pack(side="left")
    summary = ttk.Label(tools, text="", style="Hint.TLabel")
    summary.pack(side="right")

    tree_frame, ctree = make_tree(
        ctab,
        [("name", "Name"), ("type", "Type"), ("size", "Size"), ("path", "Location")],
        [220, 80, 100, 300],
    )
    empty_hint = ttk.Label(
        ctree,
        text=("Drag files here, or use the buttons below" if DND_FILES
              else "Click \"Add files\" or \"Add folder\" to get started"),
        style="Sub.TLabel",
    )

    def refresh_summary():
        if items:
            empty_hint.place_forget()
        else:
            empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        total = sum(s for s, _ in sizes.values())
        files = sum(c for _, c in sizes.values())
        pending = len(items) - len(sizes)
        text = f"{len(items)} item(s), {files} file(s), {human(total)}" if items else ""
        summary.configure(text=text + (" (measuring...)" if pending else ""))

    def add_paths(paths):
        known = set(items.values())
        for p in paths:
            p = os.path.abspath(p)
            if p in known or not os.path.lexists(p):
                continue
            known.add(p)
            is_dir = os.path.isdir(p)
            iid = ctree.insert(
                "", "end",
                values=(os.path.basename(p.rstrip(os.sep)) or p,
                        "Folder" if is_dir else "File", "...", os.path.dirname(p)),
            )
            items[iid] = p

            def measure(iid=iid, p=p):
                try:
                    result = folder_size(p)
                except OSError:
                    result = (0, 0)

                def done():
                    if iid in items:
                        sizes[iid] = result
                        ctree.set(iid, "size", human(result[0]))
                        refresh_summary()

                call_ui(done)

            threading.Thread(target=measure, daemon=True).start()
        if items and not out_var.get():
            first = next(iter(items.values()))
            name = os.path.basename(first.rstrip(os.sep)) if len(items) == 1 else "Compressed"
            name = os.path.splitext(name)[0] if os.path.isfile(first) else name
            out_var.set(os.path.join(os.path.dirname(first), name + EXTENSION))
        refresh_summary()

    def add_files():
        add_paths(filedialog.askopenfilenames(title="Add files"))

    def add_folder():
        p = filedialog.askdirectory(title="Add folder")
        if p:
            add_paths([p])

    def remove_selected():
        for iid in ctree.selection():
            ctree.delete(iid)
            items.pop(iid, None)
            sizes.pop(iid, None)
        refresh_summary()

    def clear_all():
        for iid in list(items):
            ctree.delete(iid)
        items.clear()
        sizes.clear()
        out_var.set("")
        refresh_summary()

    ctree.bind("<Delete>", lambda e: remove_selected())
    ctree.bind("<BackSpace>", lambda e: remove_selected())

    if DND_FILES:
        ctree.drop_target_register(DND_FILES)
        ctree.dnd_bind("<<Drop>>", lambda e: add_paths(root.tk.splitlist(e.data)))

    buttons = ttk.Frame(ctab)
    ttk.Button(buttons, text="+ Add files", command=add_files).pack(side="left")
    ttk.Button(buttons, text="+ Add folder", command=add_folder).pack(side="left", padx=6)
    ttk.Button(buttons, text="Remove selected", command=remove_selected).pack(side="left")
    ttk.Button(buttons, text="Clear all", command=clear_all).pack(side="left", padx=6)

    level_names = {
        "Best: smallest file (slower)": "best",
        "Normal": "normal",
        "Fast: bigger file (quicker)": "fast",
    }
    level_var = tk.StringVar(value=next(iter(level_names)))
    core_names = {f"All {CPU_COUNT} (fastest)": CPU_COUNT}
    core_names.setdefault(f"Half ({max(1, CPU_COUNT // 2)})", max(1, CPU_COUNT // 2))
    core_names.setdefault("1 (uses least memory)", 1)
    cores_var = tk.StringVar(value=next(iter(core_names)))  # shared by both tabs

    def cores_picker(parent):
        ttk.Label(parent, text="CPU cores:").pack(side="left", padx=(16, 6))
        ttk.Combobox(parent, textvariable=cores_var, values=list(core_names),
                     state="readonly", width=22).pack(side="left")

    level_row = ttk.Frame(ctab)
    ttk.Label(level_row, text="Compression:", width=12).pack(side="left")
    ttk.Combobox(
        level_row, textvariable=level_var, values=list(level_names), state="readonly", width=30
    ).pack(side="left")
    cores_picker(level_row)

    def browse_output():
        p = filedialog.asksaveasfilename(
            title="Save compressed archive as",
            defaultextension=EXTENSION,
            initialfile=os.path.basename(out_var.get()) or "Compressed" + EXTENSION,
            filetypes=[("Compressed archive", "*" + EXTENSION)],
        )
        if p:
            out_var.set(p)

    out_row, out_var = path_row(ctab, "Save as:", browse_output)

    c_action = ttk.Frame(ctab)
    c_button = ttk.Button(c_action, text="Compress", style="Accent.TButton")
    c_button.pack(side="right")
    c_prog = ttk.Progressbar(c_action, maximum=1000)
    c_prog.pack(side="left", fill="x", expand=True, padx=(0, 12))
    c_status = ttk.Label(ctab, text="", style="Hint.TLabel")
    c_result, c_main, c_detail, c_open = result_area(ctab)
    # Bottom-up, so the controls always stay visible and the list gets the rest.
    c_result.pack(side="bottom", fill="x", pady=(6, 0))
    c_status.pack(side="bottom", fill="x", pady=(4, 0))
    c_action.pack(side="bottom", fill="x", pady=(12, 0))
    out_row.pack(side="bottom", fill="x", pady=(8, 0))
    level_row.pack(side="bottom", fill="x", pady=(10, 0))
    buttons.pack(side="bottom", fill="x")
    tree_frame.pack(fill="both", expand=True, pady=(8, 6))

    # ---- Extract tab
    xtab = ttk.Frame(notebook, padding=14)
    notebook.add(xtab, text="Extract")

    def browse_archive():
        p = filedialog.askopenfilename(
            title="Choose an archive to extract",
            filetypes=[("Compressed archive", "*" + EXTENSION), ("All files", "*")],
        )
        if p:
            load_archive(p)

    arc_row, arc_var = path_row(xtab, "Archive:", browse_archive)
    arc_row.pack(fill="x")

    def browse_dest():
        p = filedialog.askdirectory(title="Extract into which folder?")
        if p:
            dest_var.set(p)

    dest_row, dest_var = path_row(xtab, "Extract to:", browse_dest)
    dest_row.pack(fill="x", pady=(8, 0))

    xinfo = ttk.Label(xtab, text="Choose an archive to see what is inside.", style="Hint.TLabel")
    xinfo.pack(fill="x", pady=(12, 0))
    xtree_frame, xtree = make_tree(xtab, [("name", "Name"), ("size", "Size")], [500, 120])

    def load_archive(path):
        arc_var.set(path)
        dest_var.set(default_extract_dir(path))
        xtree.delete(*xtree.get_children())
        xinfo.configure(text="Reading archive...")

        def work():
            try:
                rows = contents(path)
            except Exception as e:
                call_ui(lambda e=e: xinfo.configure(text=f"Cannot read this archive: {e}"))
                return

            def show():
                if arc_var.get() != path:
                    return
                for name, is_dir, nbytes in rows:
                    xtree.insert("", "end", values=(name + ("/" if is_dir else ""),
                                                    "" if is_dir else human(nbytes)))
                total = sum(r[2] for r in rows)
                files = sum(1 for r in rows if not r[1])
                packed = os.path.getsize(path)
                saved = (1 - packed / total) * 100 if total else 0
                xinfo.configure(
                    text=f"{files} file(s). Original size {human(total)}, "
                    f"compressed {human(packed)} ({saved:.0f}% smaller)."
                )

            call_ui(show)

        threading.Thread(target=work, daemon=True).start()

    if DND_FILES:
        xtree.drop_target_register(DND_FILES)
        xtree.dnd_bind("<<Drop>>", lambda e: load_archive(root.tk.splitlist(e.data)[0]))

    x_action = ttk.Frame(xtab)
    x_button = ttk.Button(x_action, text="Extract", style="Accent.TButton")
    x_button.pack(side="right")
    x_prog = ttk.Progressbar(x_action, maximum=1000)
    x_prog.pack(side="left", fill="x", expand=True, padx=(0, 12))
    x_status = ttk.Label(xtab, text="", style="Hint.TLabel")
    x_result, x_main, x_detail, x_open = result_area(xtab)
    x_result.pack(side="bottom", fill="x", pady=(6, 0))
    x_status.pack(side="bottom", fill="x", pady=(4, 0))
    x_action.pack(side="bottom", fill="x", pady=(12, 0))
    xtree_frame.pack(fill="both", expand=True, pady=(6, 0))

    # ---- Download tab
    dtab = ttk.Frame(notebook, padding=14)
    notebook.add(dtab, text="Download")
    ttk.Label(dtab, text="Download from links and save them compressed",
              style="Big.TLabel").pack(anchor="w")
    links_frame = ttk.Frame(dtab)
    links = tk.Text(links_frame, height=6, wrap="none", relief="solid", borderwidth=1,
                    font=(family, size), undo=True)
    links_bar = ttk.Scrollbar(links_frame, orient="vertical", command=links.yview)
    links.configure(yscrollcommand=links_bar.set)
    links.pack(side="left", fill="both", expand=True)
    links_bar.pack(side="right", fill="y")

    def paste_links():
        try:
            text = root.clipboard_get()
        except tk.TclError:
            return
        if links.get("1.0", "end-1c").strip():
            links.insert("end", "\n")
        links.insert("end", text.strip())

    link_buttons = ttk.Frame(dtab)
    ttk.Button(link_buttons, text="Paste link", command=paste_links).pack(side="left")
    ttk.Button(link_buttons, text="Clear",
               command=lambda: links.delete("1.0", "end")).pack(side="left", padx=6)

    d_level_var = tk.StringVar(value=next(iter(level_names)))
    d_level_row = ttk.Frame(dtab)
    ttk.Label(d_level_row, text="Compression:", width=12).pack(side="left")
    ttk.Combobox(d_level_row, textvariable=d_level_var, values=list(level_names),
                 state="readonly", width=30).pack(side="left")
    keep_var = tk.BooleanVar(value=False)
    cores_picker(d_level_row)
    d_speed_row = ttk.Frame(dtab)
    fast_dl_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(d_speed_row, text="Fast download (3 files at once, 4 connections per file)",
                    variable=fast_dl_var).pack(side="left")
    ttk.Checkbutton(d_speed_row, text="Also keep the uncompressed files",
                    variable=keep_var).pack(side="left", padx=16)

    def browse_d_output():
        p = filedialog.asksaveasfilename(
            title="Save compressed downloads as",
            defaultextension=EXTENSION,
            initialfile=os.path.basename(d_out_var.get()),
            initialdir=os.path.dirname(d_out_var.get()),
            filetypes=[("Compressed archive", "*" + EXTENSION)],
        )
        if p:
            d_out_var.set(p)

    d_out_row, d_out_var = path_row(dtab, "Save as:", browse_d_output)
    downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(downloads_dir):
        downloads_dir = os.path.expanduser("~")
    d_out_var.set(os.path.join(downloads_dir, "Downloads" + EXTENSION))

    d_action = ttk.Frame(dtab)
    d_button = ttk.Button(d_action, text="Download & Compress", style="Accent.TButton")
    d_button.pack(side="right")
    d_prog = ttk.Progressbar(d_action, maximum=1000)
    d_prog.pack(side="left", fill="x", expand=True, padx=(0, 12))
    d_status = ttk.Label(dtab, text="", style="Hint.TLabel")
    d_result, d_main, d_detail, d_open = result_area(dtab)
    d_result.pack(side="bottom", fill="x", pady=(6, 0))
    d_status.pack(side="bottom", fill="x", pady=(4, 0))
    d_action.pack(side="bottom", fill="x", pady=(12, 0))
    d_out_row.pack(side="bottom", fill="x", pady=(8, 0))
    d_speed_row.pack(side="bottom", fill="x", pady=(8, 0))
    d_level_row.pack(side="bottom", fill="x", pady=(10, 0))
    link_buttons.pack(side="bottom", fill="x", pady=(6, 0))
    links_frame.pack(fill="both", expand=True, pady=(8, 0))

    # ---- Details tab
    ltab = ttk.Frame(notebook, padding=14)
    notebook.add(ltab, text="Details")
    log_box = scrolledtext.ScrolledText(ltab, state="disabled", relief="flat",
                                        font=(tkfont.nametofont("TkFixedFont").actual("family"), size - 1))
    log_box.pack(fill="both", expand=True)

    # ---- running jobs
    widgets = {
        "compress": (c_button, c_prog, c_status, c_main, c_detail, c_open, "Compress"),
        "extract": (x_button, x_prog, x_status, x_main, x_detail, x_open, "Extract"),
        "download": (d_button, d_prog, d_status, d_main, d_detail, d_open,
                     "Download & Compress"),
    }

    def start_job(kind, work, on_success):
        button, prog, status, main, detail, open_btn, _ = widgets[kind]
        cancel = threading.Event()
        state.update(busy=kind, cancel=cancel, progress=(0.0, "Starting..."))
        for k, w in widgets.items():
            w[0].configure(text="Cancel" if k == kind else w[6],
                           state="normal" if k == kind else "disabled")
        main.configure(text="", style="Big.TLabel")
        detail.configure(text="")
        open_btn.grid_remove()

        def worker():
            try:
                result = work(cancel)
                call_ui(lambda: on_success(result))
            except Cancelled:
                call_ui(lambda: main.configure(text="Cancelled.", style="Err.TLabel"))
            except Exception as e:
                msg = str(e)
                log(f"ERROR: {msg}")
                call_ui(lambda: (main.configure(text="Something went wrong.", style="Err.TLabel"),
                                 messagebox.showerror("Error", msg)))
            finally:
                call_ui(finish_job)

        threading.Thread(target=worker, daemon=True).start()

    def finish_job():
        kind = state["busy"]
        state.update(busy=None, cancel=None, progress=None)
        for k, w in widgets.items():
            w[0].configure(text=w[6], state="normal")
            if k == kind:
                w[2].configure(text="")

    def show_success(kind, headline, detail_text, folder):
        _, prog, _, main, detail, open_btn, _ = widgets[kind]
        prog.configure(value=1000)
        main.configure(text="✔ " + headline, style="Ok.TLabel")
        detail.configure(text=detail_text)
        open_btn.configure(command=lambda: open_in_file_manager(folder))
        open_btn.grid()

    def on_compress_click():
        if state["busy"] == "compress":
            state["cancel"].set()
            return
        if not items:
            messagebox.showinfo("Nothing to compress", "Add some files or folders first.")
            return
        out = out_var.get().strip()
        if not out:
            browse_output()
            out = out_var.get().strip()
            if not out:
                return
        if not out.endswith(EXTENSION):
            out += EXTENSION
        if os.path.exists(out) and not messagebox.askyesno(
            "Replace file?", f"{os.path.basename(out)} already exists. Replace it?"
        ):
            return
        paths = list(items.values())
        level, threads = level_names[level_var.get()], core_names[cores_var.get()]
        log(f"Compressing {len(paths)} item(s) into {out}")

        def work(cancel):
            return compress(out, paths, level=level, threads=threads, log=log, cancel=cancel,
                            progress=lambda f, t: state.__setitem__("progress", (f, t)))

        def success(s):
            show_success(
                "compress",
                f"{human(s['original'])}  →  {human(s['packed'])}   "
                f"({s['saved']:.1f}% smaller)",
                f"{s['files']} file(s) in {s['seconds']:.1f}s. "
                "Verified: every file is stored exactly.",
                os.path.dirname(os.path.abspath(s["output"])),
            )

        start_job("compress", work, success)

    def on_extract_click():
        if state["busy"] == "extract":
            state["cancel"].set()
            return
        archive = arc_var.get().strip()
        if not archive or not os.path.isfile(archive):
            messagebox.showinfo("No archive", "Choose an archive to extract first.")
            return
        dest = dest_var.get().strip() or default_extract_dir(archive)
        log(f"Extracting {archive} into {dest}")

        def work(cancel):
            return extract(archive, dest, log=log, cancel=cancel,
                           progress=lambda f, t: state.__setitem__("progress", (f, t)))

        def success(s):
            if s["files"] is None:
                headline, detail_text = "Extracted", "No checksums in this archive to verify."
            else:
                headline = f"{s['files']} file(s) restored ({human(s['original'])})"
                detail_text = "Verified: identical to the originals, nothing lost."
            show_success("extract", headline, detail_text, s["dest"])

        start_job("extract", work, success)

    c_button.configure(command=on_compress_click)
    x_button.configure(command=on_extract_click)

    def on_download_click():
        if state["busy"] == "download":
            state["cancel"].set()
            return
        urls = [u.strip() for u in links.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showinfo("No links", "Paste at least one link first.")
            return
        out = d_out_var.get().strip()
        if not out:
            browse_d_output()
            out = d_out_var.get().strip()
            if not out:
                return
        if not out.endswith(EXTENSION):
            out += EXTENSION
        if os.path.exists(out) and not messagebox.askyesno(
            "Replace file?", f"{os.path.basename(out)} already exists. Replace it?"
        ):
            return
        level, keep = level_names[d_level_var.get()], keep_var.get()
        threads = core_names[cores_var.get()]
        parallel, connections = (3, 4) if fast_dl_var.get() else (1, 1)
        log(f"Downloading {len(urls)} link(s) into {out}")

        def work(cancel):
            return download_and_compress(
                urls, out, level=level, keep_originals=keep, log=log, cancel=cancel,
                threads=threads, parallel=parallel, connections=connections,
                progress=lambda f, t: state.__setitem__("progress", (f, t)))

        def success(s):
            show_success(
                "download",
                f"{human(s['original'])}  \u2192  {human(s['packed'])}   "
                f"({s['saved']:.1f}% smaller)",
                f"Downloaded {s['files']} file(s) and saved them compressed and verified"
                + (", plus the uncompressed copies." if keep else "."),
                os.path.dirname(os.path.abspath(s["output"])),
            )

        start_job("download", work, success)

    d_button.configure(command=on_download_click)

    def poll():
        while True:
            try:
                ui_queue.get_nowait()()
            except queue.Empty:
                break
        busy, prog = state["busy"], state["progress"]
        if busy and prog:
            _, bar, status, *_ = widgets[busy]
            if prog[0] is None:  # size unknown: just show the text
                status.configure(text=prog[1])
            else:
                bar.configure(value=int(prog[0] * 1000))
                status.configure(text=f"{prog[1]}  {prog[0] * 100:.0f}%")
        root.after(80, poll)

    def on_close():
        if state["busy"] and not messagebox.askyesno(
            "Still working", "A job is still running. Stop it and quit?"
        ):
            return
        if state["cancel"]:
            state["cancel"].set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_summary()
    # Paths passed to the GUI (e.g. dropped onto the launcher icon).
    for arg in sys.argv[1:]:
        if arg.endswith(EXTENSION) and os.path.isfile(arg):
            notebook.select(xtab)
            load_archive(arg)
        else:
            add_paths([arg])
    poll()
    root.mainloop()


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("compress", "extract", "download", "list", "-h", "--help"):
        try:
            run_gui()
            return 0
        except ImportError:
            print("tkinter is not available; use the command line (see --help).")
            return 1

    parser = argparse.ArgumentParser(description="Core Compressor: lossless file/folder compressor.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compress", help="pack files/folders into an archive")
    c.add_argument("output", help="archive to create (.tar.xz is added if missing)")
    c.add_argument("inputs", nargs="+", help="files and folders to include")
    c.add_argument("--level", choices=list(LEVELS), default="best",
                   help="best = smallest (default), fast = quickest")
    c.add_argument("--threads", type=int, default=CPU_COUNT,
                   help=f"CPU cores to use (default: all {CPU_COUNT})")
    x = sub.add_parser("extract", help="unpack an archive")
    x.add_argument("archive")
    x.add_argument("dest", nargs="?", help="destination folder (default: next to archive)")
    d = sub.add_parser("download", help="download links (Google Drive, Dropbox, ...) "
                       "and save them compressed")
    d.add_argument("output", help="archive to create (.tar.xz is added if missing)")
    d.add_argument("urls", nargs="+", help="share links or direct download links")
    d.add_argument("--level", choices=list(LEVELS), default="best")
    d.add_argument("--keep", action="store_true", help="also keep the uncompressed downloads")
    d.add_argument("--threads", type=int, default=CPU_COUNT, help="CPU cores to use")
    d.add_argument("--parallel", type=int, default=3, help="files downloaded at once (3)")
    d.add_argument("--connections", type=int, default=4,
                   help="connections per big file (4); use 1 if a site complains")
    l = sub.add_parser("list", help="show what is inside an archive")
    l.add_argument("archive")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "compress":
            compress(args.output, args.inputs, level=args.level, threads=args.threads)
        elif args.cmd == "extract":
            extract(args.archive, args.dest)
        elif args.cmd == "download":
            download_and_compress(args.urls, args.output, level=args.level,
                                  keep_originals=args.keep, threads=args.threads,
                                  parallel=args.parallel, connections=args.connections)
        else:
            list_archive(args.archive)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
