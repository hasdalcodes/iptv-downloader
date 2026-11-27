"""Resumable HTTP download engine and download-queue persistence (no Tk).

Files (movies, episodes) download to ``<dest>.part``. After a failure or a
pause, the next attempt asks the server for the remaining bytes with an HTTP
``Range`` request and appends them. The ``.part`` file is renamed to its final
name only once every byte announced by the server has arrived.

Live channels have no fixed length, so they record straight into the
destination file. Resuming a live recording appends new footage to it, which
is fine for MPEG-TS.
"""

import os
import re
import time

import requests

from . import storage

CHUNK_SIZE = 256 * 1024
PART_SUFFIX = ".part"

# Result statuses
COMPLETED = "completed"  # whole file on disk under its final name
STOPPED = "stopped"  # should_stop() asked us to stop; partial data kept
INCOMPLETE = "incomplete"  # connection ended before the announced size; resumable
ERROR = "error"  # HTTP/network/disk error; partial data kept, resumable

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
_UNSATISFIED_RANGE = re.compile(r"bytes\s+\*/(\d+)", re.I)


def part_path(dest_path):
    return dest_path + PART_SUFFIX


def working_path(dest_path, live=False):
    """The file that holds the data while a download is unfinished."""
    return dest_path if live else part_path(dest_path)


def partial_size(dest_path, live=False):
    try:
        return os.path.getsize(working_path(dest_path, live))
    except OSError:
        return 0


def parse_content_range(value):
    """``"bytes 100-199/1000"`` -> ``(100, 199, 1000)``; total is None for ``*``."""
    m = _CONTENT_RANGE.match(value or "")
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


class Progress:
    """Passed to ``on_progress``. ``done``/``total`` cover the whole file, including resumed bytes."""

    __slots__ = ("done", "total", "session_bytes", "elapsed", "resumed_from", "restarted")

    def __init__(self, done, total, session_bytes, elapsed, resumed_from, restarted):
        self.done = done
        self.total = total
        self.session_bytes = session_bytes
        self.elapsed = elapsed
        self.resumed_from = resumed_from
        self.restarted = restarted

    @property
    def speed(self):
        """Bytes per second during this attempt only."""
        return self.session_bytes / max(self.elapsed, 0.001)


class DownloadResult:
    __slots__ = ("status", "done", "total", "message", "resumed_from", "restarted")

    def __init__(self, status, done, total, message="", resumed_from=0, restarted=False):
        self.status = status
        self.done = done
        self.total = total
        self.message = message
        self.resumed_from = resumed_from
        self.restarted = restarted

    def __repr__(self):
        return f"DownloadResult({self.status!r}, done={self.done}, total={self.total}, msg={self.message!r})"


class _Restart(Exception):
    """The partial data can't be continued; start again from byte 0."""


def download(
    session,
    url,
    dest_path,
    on_progress=None,
    should_stop=None,
    live=False,
    timeout=(15, 60),
    chunk_size=CHUNK_SIZE,
    progress_interval=0.25,
):
    """Downloads ``url`` to ``dest_path``, resuming from any partial data. Never raises
    for network, HTTP or disk errors; they come back as a ``DownloadResult``."""
    should_stop = should_stop or (lambda: False)
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        try:
            return _attempt(session, url, dest_path, on_progress, should_stop, live,
                            timeout, chunk_size, progress_interval, restarted=False)
        except _Restart:
            try:
                os.remove(working_path(dest_path, live))
            except FileNotFoundError:
                pass
            return _attempt(session, url, dest_path, on_progress, should_stop, live,
                            timeout, chunk_size, progress_interval, restarted=True)
    except _Restart as e:
        return DownloadResult(ERROR, partial_size(dest_path, live), None, f"Restart failed: {e}")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return DownloadResult(ERROR, partial_size(dest_path, live), None, f"HTTP {code}")
    except requests.RequestException as e:
        return DownloadResult(ERROR, partial_size(dest_path, live), None, _short_error(e))
    except OSError as e:
        return DownloadResult(ERROR, partial_size(dest_path, live), None, f"Disk: {e.strerror or e}")


def _short_error(exc):
    name = type(exc).__name__
    if isinstance(exc, requests.Timeout):
        return "Timed out"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "Connection dropped"
    if isinstance(exc, requests.ConnectionError):
        return "Connection error"
    return f"{name}: {exc}"[:80]


def _attempt(session, url, dest_path, on_progress, should_stop, live, timeout,
             chunk_size, progress_interval, restarted):
    work = working_path(dest_path, live)
    offset = partial_size(dest_path, live)

    # identity: byte offsets must refer to the file itself, not a gzip stream of it.
    headers = {"Accept-Encoding": "identity"}
    if offset and not live:
        headers["Range"] = f"bytes={offset}-"

    with session.get(url, stream=True, timeout=timeout, headers=headers) as r:
        if r.status_code == 416 and offset:
            m = _UNSATISFIED_RANGE.match(r.headers.get("Content-Range", ""))
            if m and int(m.group(1)) == offset:
                os.replace(work, dest_path)  # we already had every byte
                return DownloadResult(COMPLETED, offset, offset, "Already complete", offset, restarted)
            raise _Restart("server rejected the resume range")
        r.raise_for_status()

        length = _int_or_none(r.headers.get("Content-Length"))
        if live:
            mode, total, resumed_from = "ab", None, offset
        elif offset and r.status_code == 206:
            cr = parse_content_range(r.headers.get("Content-Range"))
            if not cr or cr[0] != offset:
                raise _Restart("server returned a different range")
            total = cr[2] if cr[2] is not None else (offset + length if length is not None else None)
            mode, resumed_from = "ab", offset
        else:
            # 200: a fresh download, or a server that ignored our Range header.
            restarted = restarted or bool(offset)
            mode, total, resumed_from, offset = "wb", length, 0, 0

        done = offset
        session_bytes = 0
        start = time.monotonic()
        last_report = 0.0

        def report(force=False):
            nonlocal last_report
            now = time.monotonic()
            if on_progress and (force or now - last_report >= progress_interval):
                last_report = now
                on_progress(Progress(done, total, session_bytes, now - start, resumed_from, restarted))

        report(force=True)
        with open(work, mode) as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if should_stop():
                    return DownloadResult(STOPPED, done, total, "Stopped", resumed_from, restarted)
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    session_bytes += len(chunk)
                    report()
        report(force=True)

    if should_stop():
        return DownloadResult(STOPPED, done, total, "Stopped", resumed_from, restarted)
    if total is not None and done < total:
        return DownloadResult(INCOMPLETE, done, total, "Connection ended early", resumed_from, restarted)
    if not live:
        os.replace(work, dest_path)
    return DownloadResult(COMPLETED, done, total if total is not None else done, "", resumed_from, restarted)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Queue persistence ---------------------------------------------------------
#
# Entries store what is needed to rebuild the request (kind, stream id, extension)
# rather than the stream URL, so the password isn't copied into a second file and a
# changed password still works when resuming.

QUEUE_FIELDS = ("kind", "stream_id", "ext", "filepath", "filename", "state", "done", "total")
QUEUE_VERSION = 1


def save_queue(path, entries):
    data = {
        "version": QUEUE_VERSION,
        "downloads": [{k: e.get(k) for k in QUEUE_FIELDS} for e in entries],
    }
    storage.write_json_atomic(path, data)


def load_queue(path):
    """Saved entries, with interrupted ones (queued/downloading) turned into paused ones."""
    data = storage.read_json(path, default={})
    entries = data.get("downloads", []) if isinstance(data, dict) else []
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("filepath") or not e.get("stream_id"):
            continue
        e = {k: e.get(k) for k in QUEUE_FIELDS}
        if e["state"] in ("queued", "downloading", None):
            e["state"] = "cancelled"
        if e["state"] != "completed":
            e["done"] = partial_size(e["filepath"], live=e.get("kind") == "live")
        out.append(e)
    return out
