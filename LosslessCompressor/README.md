# Lossless Compressor

Packs files and folders into one smaller archive. When you extract it, every file comes back **byte-for-byte identical** to the original, so images keep every pixel and documents keep every detail.

## How to use

You need Python 3.8 or newer. Nothing else has to be installed.

**With the window (easiest):** double-click `lossless_compressor.py`, or run:

```
python lossless_compressor.py
```

Click **Add files...** or **Add folder...**, then **Compress**, and choose where to save the archive.
To get your files back, click **Extract archive...**.

**From the command line:**

```
python lossless_compressor.py compress MyStuff.tar.xz photo.png "My Folder" notes.txt
python lossless_compressor.py list     MyStuff.tar.xz
python lossless_compressor.py extract  MyStuff.tar.xz  RestoredFolder
```

## Why nothing is lost

- It compresses with LZMA (`.tar.xz` at the strongest setting). LZMA is a lossless method, so it never throws data away.
- It saves a SHA-256 fingerprint of every file inside the archive. After compressing, it re-reads the archive and checks each file against its fingerprint. It does the same check again after extracting. If even one byte is different, you get an error.
- Folder structure, empty folders, file dates and permissions are kept.
- The archive is a standard `.tar.xz`, so 7-Zip, WinRAR and `tar` can also open it.

## How much smaller will it get?

That depends on what kind of file it is:

| File type | Typical savings |
|---|---|
| Text, scripts, code, logs, CSV, BMP/TIFF/WAV and other raw data | 50–90% |
| Documents (DOCX, PDF), PNG | 0–20% |
| JPG, MP4, MP3, ZIP, RAR, 7z | about 0% |

Formats in the last row are already compressed. No lossless tool can shrink them much further. To make those smaller you would need lossy re-encoding, and that is exactly what reduces quality.
