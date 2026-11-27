import io
import os
import time

import pytest

from iptv_downloader import posters


class FakeResponse:
    def __init__(self, content, status=200):
        self.content = content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return FakeResponse(self.content)


class FakeRoot:
    def after(self, delay, fn, *args):
        fn(*args)


def make_loader(tmp_path, content):
    return posters.PosterLoader(FakeRoot(), FakeSession(content), str(tmp_path / "cache"), size=(40, 60))


def test_fetch_uses_disk_cache(tmp_path):
    loader = make_loader(tmp_path, b"imagebytes")
    assert loader._fetch("https://x/p.jpg") == b"imagebytes"
    assert loader._fetch("https://x/p.jpg") == b"imagebytes"
    assert loader.session.calls == 1
    loader.shutdown()


def test_decode_jpeg_to_small_png(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "red").save(buf, format="JPEG")
    loader = make_loader(tmp_path, buf.getvalue())
    png = loader._decode(buf.getvalue())
    assert png.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(png)).size == (40, 60)
    loader.shutdown()


def test_empty_url_reports_no_poster(tmp_path):
    loader = make_loader(tmp_path, b"")
    got = []
    loader.request("", lambda photo, msg: got.append((photo, msg)))
    assert got == [(None, "No poster")]
    loader.shutdown()


def test_prune_cache_removes_oldest(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    now = time.time()
    for i in range(5):
        p = d / f"f{i}"
        p.write_bytes(b"x" * 100)
        os.utime(p, (now - 100 + i, now - 100 + i))
    posters.prune_cache(str(d), max_bytes=250)
    assert sorted(os.listdir(d)) == ["f3", "f4"]
