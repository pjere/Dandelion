"""Resumable download of the MaStR bulk export (raw landing zone).

The BNetzA export is ~3 GB and the connection to `download.marktstammdatenregister.de` drops mid-stream
(`ChunkedEncodingError`, occasional DNS failures). `open-mastr`'s plain `iter_content` loop restarts from
zero each time and had left a **truncated 1.72 GB file that still had a valid `PK` header** — so it looked
downloaded but failed as `BadZipFile`, because a zip's central directory sits at the *end*.

The server advertises `Accept-Ranges: bytes`, so we resume with HTTP Range instead of restarting, retry
around drops, and **verify the zip** before handing it to open-mastr's parser.
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

import requests

BASE = "https://download.marktstammdatenregister.de"


def export_url(date_str: str, version: str = "26.1") -> str:
    return f"{BASE}/Gesamtdatenexport_{date_str}_{version}.zip"


def remote_size(url: str, timeout: int = 30) -> int:
    r = requests.head(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return int(r.headers["Content-Length"])


def verify_zip(path: str | Path) -> bool:
    """A truncated file keeps a valid PK header — only reading the central directory proves completeness."""
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def fetch(url: str, dest: str | Path, attempts: int = 40, chunk: int = 8 << 20,
          progress_every: int = 25) -> Path:
    """Download `url` → `dest`, resuming from whatever is already on disk. Returns `dest`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = remote_size(url)
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if have > total:                                   # stale/mismatched partial → start clean
            dest.unlink()
            have = 0
        if have == total:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(30, 120)) as r:
                if have and r.status_code != 206:          # server ignored the range → cannot resume
                    r.raise_for_status()
                    dest.unlink(missing_ok=True)
                    have = 0
                r.raise_for_status()
                with dest.open("ab" if have else "wb") as f:
                    n = 0
                    for block in r.iter_content(chunk_size=chunk):
                        if not block:
                            continue
                        f.write(block)
                        have += len(block)
                        n += 1
                        if n % progress_every == 0:
                            print(f"  {have/1e9:5.2f}/{total/1e9:.2f} GB "
                                  f"({100*have/total:5.1f}%) {(time.time()-t0)/60:4.1f} min", flush=True)
        except (requests.RequestException, OSError) as e:
            got = dest.stat().st_size if dest.exists() else 0
            print(f"  attempt {attempt}: {type(e).__name__} at {got/1e9:.2f} GB — resuming", flush=True)
            time.sleep(min(5 * attempt, 60))
            continue

    got = dest.stat().st_size if dest.exists() else 0
    if got != total:
        raise RuntimeError(f"incomplete after {attempts} attempts: {got}/{total} bytes")
    if not verify_zip(dest):
        raise RuntimeError(f"downloaded {got} bytes but the zip fails verification: {dest}")
    print(f"OK {got/1e9:.2f} GB verified in {(time.time()-t0)/60:.1f} min -> {dest}")
    return dest
