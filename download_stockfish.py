# Build-time helper: fetch the official Stockfish 19 Linux build.
# Runs inside the Dockerfile — internet is available during image build.
# The SF19 universal binary embeds its NNUE net, so no net file is needed.
import io
import tarfile
import urllib.request

URL = (
    "https://github.com/official-stockfish/Stockfish/releases/"
    "download/sf_19/stockfish-linux-x86-64-universal.tar.gz"
)

raw = urllib.request.urlopen(URL, timeout=120).read()

best = None
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
    for m in tf.getmembers():
        if not m.isfile():
            continue
        name = m.name.rsplit("/", 1)[-1]
        if name.startswith("README") or name.endswith(".md") or name.endswith(".txt"):
            continue
        if best is None or m.size > best.size:
            best = m

if best is None:
    raise SystemExit("download_stockfish: no engine binary found in tarball")

with open("engines/stockfish", "wb") as f:
    f.write(tf.extractfile(best).read())

print("download_stockfish: engines/stockfish ready (%.1f MB)" % (best.size / 1048576))
