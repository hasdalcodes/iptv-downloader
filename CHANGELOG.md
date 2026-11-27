# Changelog

## 7.0.0 — 2026-11-27

First release as the **iptv-downloader** project. It continues `iptv_manager-v6.py`.

### Added
- **Resumable downloads.**
  - Files download to `<name>.part` and continue with HTTP Range requests.
  - New **Resume Selected** button.
  - **Pause / Cancel** keeps the partial data.
  - **Restart from Scratch** discards it.
- **Persistent download queue** (`~/.config/iptv-downloader/queue.json`). Unfinished downloads come back paused after the app restarts.
- Optional **auto-resume on error** (3 attempts with back-off).
- **Poster and info preview panel** for movies, series, episodes and live channels (poster, year, rating, genre, duration, plot), with background loading and a disk cache.
- README, MIT license, `requirements.txt`, `pyproject.toml` with an `iptv-downloader` command, and tests.

### Fixed
- A download whose connection ended early was marked **Completed**. It is now an error that can be resumed.
- Retrying a download while its old worker was still running could start two writers on the same file.
- The same item could be queued twice and downloaded into the same file at once.
- Episode lists from a previously clicked series could replace the current one when clicking quickly.
- Downloads now request uncompressed transfer, so byte counts and resume offsets refer to the real file.
- A clear error message is shown when mpv is not installed.

### Changed
- The code is split into a package (`iptv_downloader/`). Run it with `python3 iptv_downloader.py`.
- Settings moved to `~/.config/iptv-downloader/config.json`, written atomically with `0600` permissions. Settings from `~/.iptv_tk_config.json` are migrated automatically.
- Server credentials are read on the UI thread and passed to background workers, instead of being read from Tk variables inside threads.
