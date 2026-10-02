"""Многопоточная докачка больших файлов Range-запросами (сервер ФНС рвёт длинные соединения)."""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

CHUNK = 8 * 1024 * 1024


def download(url: str, path: Path, workers: int = 12) -> None:
    client = httpx.Client(trust_env=False, timeout=httpx.Timeout(30, read=60), follow_redirects=True)
    size = int(client.head(url).headers["content-length"])
    n_chunks = (size + CHUNK - 1) // CHUNK
    done_path = path.with_suffix(path.suffix + ".done")
    done = set(int(x) for x in done_path.read_text().split()) if done_path.exists() else set()

    if path.exists() and path.stat().st_size < size and not done:
        done = set(range(path.stat().st_size // CHUNK))
    if not path.exists() or path.stat().st_size != size:
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.truncate(size)

    lock = threading.Lock()
    todo = [i for i in range(n_chunks) if i not in done]
    print(f"size {size / 1e9:.2f} GB, {n_chunks} chunks, {len(todo)} to go")
    t0 = time.time()
    got = [0]

    def fetch(i: int) -> None:
        start, end = i * CHUNK, min((i + 1) * CHUNK, size) - 1
        for attempt in range(30):
            try:
                with httpx.Client(trust_env=False, timeout=httpx.Timeout(30, read=60)) as c:
                    buf = bytearray()
                    pos = start
                    while pos <= end:
                        with c.stream("GET", url, headers={"Range": f"bytes={pos}-{end}"}) as r:
                            if r.status_code != 206:
                                raise RuntimeError(f"status {r.status_code}")
                            try:
                                for part in r.iter_bytes(256 * 1024):
                                    buf += part
                                    pos += len(part)
                            except Exception:
                                pass
                    with lock:
                        with open(path, "r+b") as f:
                            f.seek(start)
                            f.write(buf)
                        done.add(i)
                        with open(done_path, "a") as df:
                            df.write(f"{i}\n")
                        got[0] += len(buf)
                        if len(done) % 10 == 0:
                            sp = got[0] / (time.time() - t0) / 1e6
                            print(f"  {len(done)}/{n_chunks} chunks, {sp:.2f} MB/s", flush=True)
                return
            except Exception as e:
                time.sleep(min(2 * (attempt + 1), 20))
        print(f"  chunk {i} FAILED")

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fetch, todo))
    missing = n_chunks - len(done)
    print(f"done in {time.time() - t0:.0f}s, missing chunks: {missing}")


if __name__ == "__main__":
    download(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 12)
