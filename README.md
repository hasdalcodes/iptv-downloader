# IPTV Downloader

A desktop app for browsing an **Xtream Codes** IPTV account (movies, TV series and live channels), with poster previews, streaming in [mpv](https://mpv.io/), and a download queue that **resumes interrupted downloads** instead of starting over.

It is a single-window Tkinter app with no browser or server needed. Arabic titles are shown correctly when the optional shaping libraries are installed.

<!-- Screenshot: add docs/screenshot.png and uncomment
![IPTV Downloader](docs/screenshot.png)
-->

## Features

- **Browse** movies (VOD), TV series and live TV by category, with an instant filter/search box.
- **Poster and info preview** for the selected movie, series, episode or channel: poster, year, rating, genre, duration and plot.
  - Movies show their poster immediately and load full details (larger poster, plot, release date) in the background.
  - Series show their cover and plot. Each episode shows its own still and plot, and falls back to the series cover.
  - Posters are cached on disk, so browsing the same titles again is instant.
- **Download** single items, a multi-selection, or *all episodes* of a series. Episodes are saved to a folder named after the series.
- **Resumable downloads:**
  - Data is written to `<name>.part` and renamed only when every byte has arrived.
  - **Resume Selected** continues a failed, paused or cancelled download from where it stopped, using HTTP Range requests.
  - Downloads cut short by a dropped connection are reported as errors you can resume, never as "Completed".
  - The queue is saved, so unfinished downloads survive closing the app or a crash and come back **paused**.
  - Optional **auto-resume on error** (3 attempts with 5 s / 10 s / 20 s back-off).
- **Concurrency limit** (1–10 simultaneous downloads) with a first-in, first-out queue.
- **Stream** the selected item in mpv.

## Requirements

- Python **3.9+** with **Tkinter** (Tk 8.6)
- [`requests`](https://pypi.org/project/requests/)
- Recommended: [`Pillow`](https://pypi.org/project/Pillow/) for posters. Most posters are JPEG, which Tk can't decode by itself.
- Optional: `arabic-reshaper` + `python-bidi` for connected right-to-left Arabic text
- Optional: `mpv` on your `PATH` for streaming

Tkinter usually comes as a separate system package:

| OS | Install Tk |
|---|---|
| Debian / Ubuntu | `sudo apt install python3-tk` |
| Fedora | `sudo dnf install python3-tkinter` |
| Arch | `sudo pacman -S tk` |
| FreeBSD | `sudo pkg install py311-tkinter` (match your Python version, e.g. `py312-tkinter`) |
| macOS | Use the python.org installer, or `brew install python-tk` |
| Windows | Included with the python.org installer |

## Install & run

```sh
git clone https://github.com/hasdalcodes/iptv-downloader.git
cd iptv-downloader
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 iptv_downloader.py
```

You can also install it as a command:

```sh
pip install ".[all]"
iptv-downloader
```

`python3 -m iptv_downloader` works as well.

## Usage

1. Enter your **server** (`http://host:port`), **username** and **password**, then click **Connect / Login**.
2. Choose **Movies (VOD)**, **TV Series** or **Live TV** and pick a category.
3. Click a title to see its poster and details in the **Preview** panel.
   - For a **series**, clicking it also loads its episodes on the right. Click an episode to preview it, and click the series again to get its preview back.
4. Choose where to save with **Save To**, then:
   - **Download Selected**: the selected movies/channels, or the selected episodes in series mode.
   - **Download ALL Episodes**: every episode of the loaded series.
   - **Stream in mpv**: plays the selection.

### The download queue

| Button | What it does |
|---|---|
| **Pause / Cancel Selected** | Stops the download and **keeps** the partial data. |
| **Resume Selected** | Continues paused, cancelled or failed downloads from the partial data. |
| **Restart from Scratch** | Discards the partial (or finished) file and downloads again from the start. |
| **Remove from List** | Forgets the entry. Files on disk, including any `.part`, are left alone. |
| **Delete from List & Disk** | Forgets the entry and deletes the file and its `.part`. |
| **Auto-resume on error** | Retries a failed download automatically up to 3 times. |

**How resuming works.** The app asks the server for the remaining bytes (`Range: bytes=N-`) and appends them to the `.part` file.
- If the server doesn't support resuming, it sends the whole file again. The app notices, starts that download over cleanly, and says so in the status column.
- **Live TV** recordings have no fixed length. They are written straight to the `.ts` file, and resuming appends new footage to the same file.

When you close the app, running downloads are saved as interrupted. Next time they appear as **Paused — Resume available**. Nothing starts on its own; select them and click **Resume Selected**.

Adding something that is already in the list is ignored, so the same file is never downloaded twice at once. If a movie or episode file already exists on disk, it is listed as *Already on disk*. Use **Restart from Scratch** to download it again.

## Files and privacy

| What | Where |
|---|---|
| Settings (server, username, **password**, folder, limits) | `~/.config/iptv-downloader/config.json` |
| Download queue | `~/.config/iptv-downloader/queue.json` |
| Poster cache (pruned to about 200 MB) | `~/.cache/iptv-downloader/posters/` |

`$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME` are respected.

- The **password is stored in plain text**. The settings and queue files are created readable only by you (`0600`). Don't put them in a shared or synced folder.
- The queue stores stream IDs, not stream URLs, so your password isn't copied into a second file. A changed password still works when resuming.
- Version 6 kept its settings in `~/.iptv_tk_config.json`. They are copied over automatically on first start. You can delete the old file afterwards.

## Troubleshooting

- **`ModuleNotFoundError: No module named '_tkinter'`**: install the Tk package for your OS (see the table above).
- **Posters say "Install Pillow"**: run `pip install Pillow`. Without it only PNG/GIF posters can be shown.
- **Arabic text appears disconnected or reversed**: `pip install arabic-reshaper python-bidi`.
- **"mpv not found"**: install mpv and make sure it is on your `PATH`.
- **A category takes a long time**: some servers are slow to filter by category. After a timeout the app downloads the full catalog once and filters it locally; later categories are then instant.
- **A download keeps restarting from 0%**: the server doesn't support HTTP Range requests, so resuming isn't possible with that provider.

## Development

```sh
pip install -r requirements-dev.txt
pytest
python -m pyflakes iptv_downloader tests
```

The code is split so that everything except the GUI can be tested without a display:

| Module | Responsibility |
|---|---|
| `iptv_downloader/xtream.py` | HTTP session, Xtream API calls, stream URLs, poster/info extraction |
| `iptv_downloader/downloader.py` | Resumable download engine, queue persistence |
| `iptv_downloader/posters.py` | Background poster fetching, disk cache |
| `iptv_downloader/storage.py` | Config/cache paths, atomic private JSON files |
| `iptv_downloader/text.py` | Arabic shaping, safe filenames |
| `iptv_downloader/app.py` | Tkinter GUI |

The tests run the download engine against a local HTTP server that drops connections, ignores Range requests and returns `416`.

## Legal

This is a client for the Xtream Codes API. It doesn't provide any content, channels or accounts. Use it only with services you're subscribed to and authorised to download from, and respect your provider's terms and local copyright law.

## License

[MIT](LICENSE) © 2026 hasdalcodes
