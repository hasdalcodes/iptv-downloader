"""Background poster loading with a disk cache, for the Tk preview panel.

Network and decoding happen in a thread pool. The Tk ``PhotoImage`` is built on
the Tk thread, as Tk requires. Pillow is optional: it decodes JPEG/WebP, resizes
smoothly and re-encodes to PNG, which Tk 8.6 reads natively (so Pillow's separate
ImageTk/tkinter binding is not needed). Without Pillow, only PNG/GIF posters can
be shown, using Tk's own decoder and integer subsampling.
"""

import base64
import hashlib
import io
import math
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_PIL = False

MAX_BYTES = 8 * 1024 * 1024  # ignore anything larger than a sane poster


def _cache_name(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def prune_cache(directory, max_bytes=200 * 1024 * 1024):
    """Deletes the least recently used cached posters until the cache fits in ``max_bytes``."""
    try:
        files = []
        for name in os.listdir(directory):
            p = os.path.join(directory, name)
            st = os.stat(p)
            files.append((st.st_atime, st.st_size, p))
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


class PosterLoader:
    """``request(url, callback)`` calls ``callback(photo_or_None, message)`` on the Tk thread.

    Only the most recent request's callback fires; older ones are dropped, so
    scrolling quickly through a list never shows a stale poster.
    """

    def __init__(self, root, session, cache_dir, size=(240, 360), workers=4, memory_items=64):
        self.root = root
        self.session = session
        self.cache_dir = cache_dir
        self.size = size
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="poster")
        self.memory = OrderedDict()  # url -> PhotoImage (also keeps Tk from GC-ing it)
        self.memory_items = memory_items
        self.token = 0
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)
        self.pool.submit(prune_cache, cache_dir)

    def request(self, url, callback):
        self.token += 1
        token = self.token
        if not url:
            callback(None, "No poster")
            return
        photo = self.memory.get(url)
        if photo is not None:
            self.memory.move_to_end(url)
            callback(photo, "")
            return
        self.pool.submit(self._work, url, token, callback)

    def cancel(self):
        """Forget any pending request (e.g. the list was cleared)."""
        self.token += 1

    def shutdown(self):
        self.token += 1
        self.pool.shutdown(wait=False, cancel_futures=True)

    # --- worker thread ---
    def _work(self, url, token, callback):
        if token != self.token:
            return
        try:
            data = self._fetch(url)
            png_or_raw = self._decode(data) if HAS_PIL else data
            self.root.after(0, self._finish, url, token, callback, png_or_raw, None)
        except Exception as e:  # network, HTTP, bad image: all just mean "no poster"
            self.root.after(0, self._finish, url, token, callback, None, e)

    def _fetch(self, url):
        path = os.path.join(self.cache_dir, _cache_name(url))
        try:
            with open(path, "rb") as f:
                data = f.read()
            os.utime(path)  # LRU for prune_cache
            return data
        except OSError:
            pass
        r = self.session.get(url, timeout=(8, 20))
        r.raise_for_status()
        data = r.content
        if not data or len(data) > MAX_BYTES:
            raise ValueError("empty or oversized image")
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data

    def _decode(self, data):
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.thumbnail(self.size, Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    # --- Tk thread ---
    def _finish(self, url, token, callback, data, error):
        photo = None
        if data is not None:
            try:
                photo = self._tk_photo(data)
            except Exception:
                photo = None  # not a format Tk can read
        if photo is not None:
            self.memory[url] = photo
            while len(self.memory) > self.memory_items:
                self.memory.popitem(last=False)
        if token != self.token:
            return
        if photo is not None:
            callback(photo, "")
        elif not HAS_PIL:
            callback(None, "Poster format not supported.\nInstall Pillow to show JPEG posters.")
        else:
            callback(None, "Poster unavailable")

    def _tk_photo(self, data):
        import tkinter as tk

        photo = tk.PhotoImage(master=self.root, data=base64.b64encode(data).decode("ascii"))
        factor = max(1, math.ceil(max(photo.width() / self.size[0], photo.height() / self.size[1])))
        return photo.subsample(factor) if factor > 1 else photo
