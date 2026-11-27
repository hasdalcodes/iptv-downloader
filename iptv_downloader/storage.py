"""Config/cache locations and atomic, private JSON files."""

import json
import os
import tempfile

APP_NAME = "iptv-downloader"
LEGACY_CONFIG_FILE = os.path.expanduser("~/.iptv_tk_config.json")


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_NAME)


def config_file():
    return os.path.join(config_dir(), "config.json")


def queue_file():
    return os.path.join(config_dir(), "queue.json")


def read_json(path, default=None):
    """Returns the parsed JSON in ``path``, or ``default`` if it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data, mode=0o600):
    """Writes JSON via a temp file + rename so a crash never leaves a half-written file.

    The file is created with ``mode`` (owner-only by default) because the
    config holds the account password in plain text.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
