# Video decoder tools

Used exclusively by **Similar files** for local video metadata and sampled-frame extraction.
Run `.venv\Scripts\python.exe tools\fetch_ffmpeg.py` from the repository root to fetch
the pinned FFmpeg 9.0.1 essentials Windows build from Gyan, a provider linked from
https://ffmpeg.org/download.html. The installer verifies SHA-256 before extracting
the two executables and upstream notices. No download occurs while the app runs.

Archive: https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.1-essentials_build.zip

SHA-256: `fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9`

These executables retain their upstream GPLv3 license; they are not covered by the
app's MIT source license. See `LICENSE.txt` and `UPSTREAM-README.txt`, which also
identifies the source revision and build configuration. The portable build embeds
both tools and those notices under `tools/ffmpeg`. Executables are ignored by Git.
