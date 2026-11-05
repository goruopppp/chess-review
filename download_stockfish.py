# Build-time helper: fetch the official Stockfish 19 Linux build.
# Runs inside the Dockerfile — internet is available during image build.
# The SF19 universal binary embeds its NNUE net, so no net file is needed.
# If downloading fails, the Dockerfile falls back to Debian's stockfish package.
import io
import sys
import tarfile
import time
import urllib.request
import urllib.error

URL = (
    "https://github.com/official-stockfish/Stockfish/releases/"
    "download/sf_19/stockfish-linux-x86-64-universal.tar.gz"
)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "chess-review-deploy/1.0"})
    for attempt in range(1, 4):
        try:
            t0 = time.time()
            raw = urllib.request.urlopen(req, timeout=180).read()
            print(f"  fetched {len(raw) / 1048576:.1f} MB in {time.time() - t0:.0f}s", flush=True)
            if len(raw) < 1000:
                print("  truncated response, retrying", flush=True)
                continue
            return raw
        except Exception as e:
            print(f"  attempt {attempt} failed: {type(e).__name__}: {e}", flush=True)
            time.sleep(2 * attempt)
    return None


print(f"downloading {URL}", flush=True)
raw = fetch(URL)
if not raw:
    print("download_stockfish: download FAILED - Dockerfile falls back to the distro package", flush=True)
    sys.exit(1)

best = None
try:
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
            print("download_stockfish: no engine binary found in tarball", flush=True)
            sys.exit(1)
        with open("engines/stockfish", "wb") as f:
            f.write(tf.extractfile(best).read())
except Exception as e:
    print(f"download_stockfish: tarball parse failed: {type(e).__name__}: {e}", flush=True)
    print("first bytes:", raw[:60], flush=True)
    sys.exit(1)

print(f"download_stockfish: engines/stockfish ready ({best.size / 1048576:.1f} MB)", flush=True)
