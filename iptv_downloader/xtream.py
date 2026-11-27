"""Xtream Codes API client helpers and poster/info extraction (no Tk)."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": "IPTVSmartersPro",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def make_session(pool_size=10):
    """A requests session with the IPTV headers and retries on connection setup and 5xx/429."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry_strategy = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class Credentials:
    """A snapshot of server/user/password, taken on the Tk thread and safe to pass to workers."""

    __slots__ = ("host", "user", "password")

    def __init__(self, host, user, password):
        self.host = (host or "").strip().rstrip("/")
        self.user = (user or "").strip()
        self.password = (password or "").strip()

    @property
    def endpoint(self):
        return f"{self.host}/player_api.php"


def api_get_json(session, creds, action=None, timeout=(15, 65), **extra):
    params = {"username": creds.user, "password": creds.password}
    if action:
        params["action"] = action
    for k, v in extra.items():
        if v is not None:
            params[k] = v
    r = session.get(creds.endpoint, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def build_stream_url(creds, stream_id, ext, kind):
    if kind == "movie":
        path = "movie"
    elif kind == "series_episode":
        path = "series"
    else:
        path = "live"
    return f"{creds.host}/{path}/{creds.user}/{creds.password}/{stream_id}.{ext}"


# --- Poster / info extraction -------------------------------------------------
#
# Every function returns a "preview" dict with the keys below. Missing values are
# empty strings so the UI never has to check for None.

PREVIEW_KEYS = ("title", "poster", "year", "rating", "genre", "plot", "duration")


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        if value == 0:
            return ""
        return f"{value:g}" if isinstance(value, float) else str(value)
    s = str(value).strip()
    return "" if s.lower() in ("", "null", "none", "n/a", "0") else s


def _first(*values):
    for v in values:
        c = _clean(v)
        if c:
            return c
    return ""


def _url(*values):
    for v in values:
        if isinstance(v, list):  # e.g. backdrop_path: ["https://..."]
            v = v[0] if v else ""
        c = _clean(v)
        if c.startswith(("http://", "https://")):
            return c
    return ""


def _year(*values):
    for v in values:
        c = _clean(v)
        if len(c) >= 4 and c[:4].isdigit():
            return c[:4]
    return ""


def _info_dict(obj):
    """Servers return ``info`` as a dict, or as an empty list when there is no data."""
    info = obj.get("info") if isinstance(obj, dict) else None
    return info if isinstance(info, dict) else {}


def _preview(**kw):
    p = {k: "" for k in PREVIEW_KEYS}
    p.update({k: v for k, v in kw.items() if v})
    return p


def merge_preview(base, extra):
    """Fields from ``extra`` override ``base`` only when they are non-empty."""
    out = dict(base)
    for k, v in extra.items():
        if v:
            out[k] = v
    return out


def movie_preview(item):
    """From a ``get_vod_streams`` entry."""
    return _preview(
        title=_first(item.get("name"), item.get("title")),
        poster=_url(item.get("stream_icon"), item.get("cover")),
        year=_year(item.get("year"), item.get("releaseDate"), item.get("release_date")),
        rating=_first(item.get("rating"), item.get("rating_5based")),
        genre=_first(item.get("genre")),
        plot=_first(item.get("plot")),
    )


def movie_info_preview(vod_info, base=None):
    """From a ``get_vod_info`` response, layered over the list entry's preview."""
    info = _info_dict(vod_info)
    movie_data = vod_info.get("movie_data", {}) if isinstance(vod_info, dict) else {}
    if not isinstance(movie_data, dict):
        movie_data = {}
    extra = _preview(
        title=_first(info.get("name"), movie_data.get("name")),
        poster=_url(info.get("movie_image"), info.get("cover_big"), info.get("cover")),
        year=_year(info.get("releasedate"), info.get("release_date"), info.get("year")),
        rating=_first(info.get("rating")),
        genre=_first(info.get("genre")),
        plot=_first(info.get("plot"), info.get("description")),
        duration=_first(info.get("duration")),
    )
    return merge_preview(base or _preview(), extra)


def series_preview(item):
    """From a ``get_series`` entry."""
    return _preview(
        title=_first(item.get("name"), item.get("title")),
        poster=_url(item.get("cover"), item.get("stream_icon"), item.get("backdrop_path")),
        year=_year(item.get("year"), item.get("releaseDate"), item.get("release_date")),
        rating=_first(item.get("rating"), item.get("rating_5based")),
        genre=_first(item.get("genre")),
        plot=_first(item.get("plot")),
    )


def series_info_preview(series_info, base=None):
    """From a ``get_series_info`` response (its ``info`` block), layered over ``base``."""
    info = _info_dict(series_info)
    extra = _preview(
        title=_first(info.get("name")),
        poster=_url(info.get("cover"), info.get("backdrop_path")),
        year=_year(info.get("releaseDate"), info.get("release_date"), info.get("year")),
        rating=_first(info.get("rating")),
        genre=_first(info.get("genre")),
        plot=_first(info.get("plot")),
    )
    return merge_preview(base or _preview(), extra)


def episode_preview(ep, title, series=None):
    """From one episode of ``get_series_info``. Falls back to the series' poster and genre."""
    series = series or _preview()
    info = _info_dict(ep)
    return _preview(
        title=title,
        poster=_url(info.get("movie_image"), info.get("cover_big")) or series.get("poster", ""),
        year=_year(info.get("releasedate"), info.get("release_date"), info.get("air_date")),
        rating=_first(info.get("rating")),
        genre=series.get("genre", ""),
        plot=_first(info.get("plot"), info.get("overview")),
        duration=_first(info.get("duration")),
    )


def live_preview(item):
    """From a ``get_live_streams`` entry: just the channel logo."""
    return _preview(
        title=_first(item.get("name"), item.get("title")),
        poster=_url(item.get("stream_icon")),
    )
