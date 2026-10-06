"""Test assets built on the host without root (dosfstools + mtools)."""
import os
import subprocess


def fat_image(path, label, files, size_kib=16384):
    """Create a FAT-formatted disk image at `path` containing {name: text}."""
    if os.path.exists(path):
        os.remove(path)
    subprocess.run(["mkfs.vfat", "-C", "-n", label, path, str(size_kib)],
                   check=True, stdout=subprocess.DEVNULL)
    for name, text in files.items():
        src = path + "." + name
        with open(src, "w") as f:
            f.write(text)
        subprocess.run(["mcopy", "-i", path, src, "::" + name], check=True)
        os.remove(src)
    return path
