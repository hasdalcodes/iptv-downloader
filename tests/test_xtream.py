from iptv_downloader import xtream

VOD_ITEM = {
    "name": "Some Movie",
    "stream_id": 11,
    "stream_icon": "https://img.example/poster.jpg",
    "rating": "7.4",
    "container_extension": "mkv",
}
VOD_INFO = {
    "info": {
        "movie_image": "https://img.example/big.jpg",
        "plot": "A plot.",
        "releasedate": "2019-05-01",
        "genre": "Drama",
        "duration": "01:52:00",
        "rating": "",
    },
    "movie_data": {"stream_id": 11, "name": "Some Movie"},
}
SERIES_ITEM = {"name": "A Show", "series_id": 5, "cover": "http://img.example/show.png", "genre": "Comedy",
               "plot": "", "releaseDate": "2021-01-01", "rating": "8"}
SERIES_INFO = {
    "info": {"name": "A Show", "cover": "http://img.example/show-hd.png", "plot": "Show plot."},
    "episodes": {
        "1": [
            {"id": "100", "episode_num": 1, "title": "Pilot",
             "info": {"movie_image": "https://img.example/e1.jpg", "plot": "Ep plot", "duration": "00:42:00"}},
            {"id": "101", "episode_num": 2, "title": "Two", "info": []},
        ]
    },
}


def test_movie_preview_then_info():
    base = xtream.movie_preview(VOD_ITEM)
    assert base["poster"] == "https://img.example/poster.jpg"
    assert base["rating"] == "7.4"
    full = xtream.movie_info_preview(VOD_INFO, base)
    assert full["poster"] == "https://img.example/big.jpg"
    assert full["year"] == "2019"
    assert full["plot"] == "A plot."
    assert full["rating"] == "7.4"  # empty info rating doesn't erase the list rating


def test_movie_info_with_empty_info_list():
    base = xtream.movie_preview(VOD_ITEM)
    assert xtream.movie_info_preview({"info": [], "movie_data": []}, base) == base


def test_series_and_episode_preview():
    s = xtream.series_info_preview(SERIES_INFO, xtream.series_preview(SERIES_ITEM))
    assert s["poster"] == "http://img.example/show-hd.png"
    assert s["plot"] == "Show plot."
    assert s["year"] == "2021"
    assert s["genre"] == "Comedy"

    eps = SERIES_INFO["episodes"]["1"]
    e1 = xtream.episode_preview(eps[0], "[S01E01] Pilot", s)
    assert e1["poster"] == "https://img.example/e1.jpg"
    assert e1["duration"] == "00:42:00"
    e2 = xtream.episode_preview(eps[1], "[S01E02] Two", s)
    assert e2["poster"] == s["poster"]  # falls back to the series cover
    assert e2["genre"] == "Comedy"


def test_bad_values_are_ignored():
    p = xtream.movie_preview({"name": "X", "stream_icon": "null", "rating": 0, "year": "None"})
    assert p["poster"] == "" and p["rating"] == "" and p["year"] == ""
    assert xtream.series_preview({"name": "Y", "backdrop_path": ["https://b.example/1.jpg"]})["poster"] == \
        "https://b.example/1.jpg"


def test_build_stream_url():
    c = xtream.Credentials("http://h:8080/", " u ", "p")
    assert xtream.build_stream_url(c, 1, "mp4", "movie") == "http://h:8080/movie/u/p/1.mp4"
    assert xtream.build_stream_url(c, 2, "mkv", "series_episode") == "http://h:8080/series/u/p/2.mkv"
    assert xtream.build_stream_url(c, 3, "ts", "live") == "http://h:8080/live/u/p/3.ts"
    assert c.endpoint == "http://h:8080/player_api.php"
