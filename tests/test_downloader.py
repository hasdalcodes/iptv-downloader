import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from iptv_downloader import downloader
from iptv_downloader.downloader import COMPLETED, ERROR, INCOMPLETE, STOPPED

DATA = os.urandom(3 * 1024 * 1024 + 123)


class Behaviour:
    def __init__(self):
        self.support_range = True
        self.cut_first_after = None  # drop the connection after N body bytes, once
        self.status = None  # force a status code
        self.requests = []  # Range header (or None) of each request


class Handler(BaseHTTPRequestHandler):
    behaviour = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        b = self.behaviour
        rng = self.headers.get("Range")
        b.requests.append(rng)
        if b.status:
            self.send_response(b.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start = 0
        if rng and b.support_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(DATA):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(DATA)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if b.cut_first_after is not None:
            n = b.cut_first_after
            b.cut_first_after = None
            self.wfile.write(body[:n])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(2)
            return
        self.wfile.write(body)


@pytest.fixture
def server():
    behaviour = Behaviour()
    handler = type("H", (Handler,), {"behaviour": behaviour})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/movie/u/p/1.mp4", behaviour
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def session():
    s = requests.Session()
    yield s
    s.close()


def test_fresh_download(server, session, tmp_path):
    url, b = server
    dest = str(tmp_path / "movie.mp4")
    res = downloader.download(session, url, dest)
    assert res.status == COMPLETED
    assert open(dest, "rb").read() == DATA
    assert not os.path.exists(dest + ".part")
    assert b.requests == [None]


def test_dropped_connection_then_resume(server, session, tmp_path):
    url, b = server
    b.cut_first_after = 1024 * 1024
    dest = str(tmp_path / "movie.mp4")

    first = downloader.download(session, url, dest)
    assert first.status in (INCOMPLETE, ERROR)
    assert not os.path.exists(dest)
    part = os.path.getsize(dest + ".part")
    assert 0 < part <= 1024 * 1024

    progress = []
    second = downloader.download(session, url, dest, on_progress=progress.append)
    assert second.status == COMPLETED
    assert second.resumed_from == part
    assert open(dest, "rb").read() == DATA
    assert b.requests == [None, f"bytes={part}-"]
    assert progress[0].done == part and progress[0].total == len(DATA)


def test_server_ignores_range_restarts_cleanly(server, session, tmp_path):
    url, b = server
    b.support_range = False
    dest = str(tmp_path / "movie.mp4")
    with open(dest + ".part", "wb") as f:
        f.write(b"garbage that must not end up in the file")
    res = downloader.download(session, url, dest)
    assert res.status == COMPLETED
    assert res.restarted
    assert open(dest, "rb").read() == DATA


def test_416_on_already_complete_part(server, session, tmp_path):
    url, b = server
    dest = str(tmp_path / "movie.mp4")
    with open(dest + ".part", "wb") as f:
        f.write(DATA)
    res = downloader.download(session, url, dest)
    assert res.status == COMPLETED
    assert open(dest, "rb").read() == DATA


def test_416_with_oversized_part_restarts(server, session, tmp_path):
    url, b = server
    dest = str(tmp_path / "movie.mp4")
    with open(dest + ".part", "wb") as f:
        f.write(DATA + b"extra")
    res = downloader.download(session, url, dest)
    assert res.status == COMPLETED and res.restarted
    assert open(dest, "rb").read() == DATA


def test_stop_keeps_part_and_resume_finishes(server, session, tmp_path):
    url, b = server
    dest = str(tmp_path / "movie.mp4")
    calls = {"n": 0}

    def stop_after_two_chunks():
        calls["n"] += 1
        return calls["n"] > 2

    res = downloader.download(session, url, dest, should_stop=stop_after_two_chunks, chunk_size=64 * 1024)
    assert res.status == STOPPED
    assert os.path.getsize(dest + ".part") == 128 * 1024
    assert not os.path.exists(dest)

    res = downloader.download(session, url, dest)
    assert res.status == COMPLETED
    assert open(dest, "rb").read() == DATA


def test_http_error_is_reported(server, session, tmp_path):
    url, b = server
    b.status = 404
    res = downloader.download(session, url, str(tmp_path / "x.mp4"))
    assert res.status == ERROR and "404" in res.message


def test_live_appends_without_range(server, session, tmp_path):
    url, b = server
    dest = str(tmp_path / "live.ts")
    with open(dest, "wb") as f:
        f.write(b"earlier recording")
    res = downloader.download(session, url, dest, live=True)
    assert res.status == COMPLETED
    assert b.requests == [None]
    assert open(dest, "rb").read() == b"earlier recording" + DATA


def test_queue_round_trip(tmp_path):
    dest = str(tmp_path / "a.mp4")
    with open(dest + ".part", "wb") as f:
        f.write(b"x" * 10)
    path = str(tmp_path / "queue.json")
    entries = [
        {"kind": "movie", "stream_id": "1", "ext": "mp4", "filepath": dest, "filename": "a.mp4",
         "state": "downloading", "done": 3, "total": 100, "cancel": True},
        {"kind": "series_episode", "stream_id": "2", "ext": "mkv", "filepath": str(tmp_path / "b.mkv"),
         "filename": "b.mkv", "state": "completed", "done": 50, "total": 50},
    ]
    downloader.save_queue(path, entries)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    loaded = downloader.load_queue(path)
    assert loaded[0]["state"] == "cancelled"  # interrupted -> paused, never auto-started
    assert loaded[0]["done"] == 10  # read from the .part on disk
    assert "cancel" not in loaded[0]
    assert loaded[1]["state"] == "completed" and loaded[1]["done"] == 50


def test_load_queue_missing_or_corrupt(tmp_path):
    assert downloader.load_queue(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert downloader.load_queue(str(bad)) == []


def test_parse_content_range():
    assert downloader.parse_content_range("bytes 5-9/10") == (5, 9, 10)
    assert downloader.parse_content_range("bytes 5-9/*") == (5, 9, None)
    assert downloader.parse_content_range("garbage") is None
