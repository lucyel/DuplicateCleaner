"""Fetch the pinned Windows decoder bundle for local builds; never used at app runtime."""
import hashlib
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = "9.0.1"
URL = f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{VERSION}-essentials_build.zip"
SHA256 = "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9"
ROOT = Path(__file__).resolve().parent.parent


def main():
    archive = ROOT / "build" / f"ffmpeg-{VERSION}.zip"
    destination = ROOT / "tools" / "ffmpeg"
    archive.parent.mkdir(exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        partial = archive.with_suffix(".download")
        with urllib.request.urlopen(URL, timeout=60) as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target)
        partial.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError(f"FFmpeg checksum mismatch; remove {archive} and retry")
    prefix = f"ffmpeg-{VERSION}-essentials_build/"
    with zipfile.ZipFile(archive) as bundle:
        for member, name in (("bin/ffmpeg.exe", "ffmpeg.exe"), ("bin/ffprobe.exe", "ffprobe.exe"),
                             ("LICENSE", "LICENSE.txt"), ("README.txt", "UPSTREAM-README.txt")):
            with bundle.open(prefix + member) as source, (destination / name).open("wb") as target:
                shutil.copyfileobj(source, target)
    print(f"Verified FFmpeg {VERSION}: {destination}")


if __name__ == "__main__":
    main()
