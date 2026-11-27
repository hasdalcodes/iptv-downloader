#!/usr/bin/env python3
import os
import json
import time
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional Arabic shaping and BiDi support for X11/Tkinter
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_ARABIC_SUPPORT = True
except ImportError:
    HAS_ARABIC_SUPPORT = False

CONFIG_FILE = os.path.expanduser("~/.iptv_tk_config.json")
HEADERS = {
    "User-Agent": "IPTVSmartersPro",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def fix_text(text):
    """Reshapes Arabic letters and applies Right-to-Left BiDi ordering for Tkinter on X11."""
    if not text:
        return ""
    text_str = str(text)
    if not HAS_ARABIC_SUPPORT:
        return text_str
    if any("\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" for ch in text_str):
        try:
            reshaped = arabic_reshaper.reshape(text_str)
            return get_display(reshaped)
        except Exception:
            return text_str
    return text_str


def sanitize_filename(name):
    """Sanitizes strings for safe filesystem directory and file naming while preserving Arabic/English."""
    cleaned = "".join(c if c.isalnum() or c in " ._-[]" else "_" for c in str(name)).strip()
    return cleaned or "Untitled"


class IPTVManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Xtream IPTV Browser & Download Manager")
        self.root.geometry("1320x800")

        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        retry_strategy = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.ui_font = ("DejaVu Sans", 10)
        self.root.option_add("*Font", self.ui_font)

        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=self.ui_font)
        style.configure("Treeview", font=self.ui_font, rowheight=26)
        style.configure("Treeview.Heading", font=("DejaVu Sans", 10, "bold"))

        # Data & state
        self.categories = []
        self.current_series = []
        self.current_episodes = []
        self.selected_series_title = ""
        self.series_lookup = {}
        self.episode_lookup = {}
        self.catalog_cache = {}
        self.downloads = {}
        self.dl_counter = 0
        self.req_token = 0
        self.queue_lock = threading.Lock()

        self._build_ui()
        self._load_config()

        if not HAS_ARABIC_SUPPORT:
            self.status_var.set("Tip: Install arabic-reshaper & python-bidi for connected RTL Arabic text.")

    def _build_ui(self):
        # 1. Top Bar: Credentials, Destination Directory & Concurrency Limiter
        top_frame = ttk.LabelFrame(self.root, text="Account, Destination & Concurrency", padding=8)
        top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_frame, text="Server (http://host:port):").grid(row=0, column=0, sticky="w", padx=4)
        self.host_var = tk.StringVar()
        ttk.Entry(top_frame, textvariable=self.host_var, width=28).grid(row=0, column=1, padx=4)

        ttk.Label(top_frame, text="Username:").grid(row=0, column=2, sticky="w", padx=4)
        self.user_var = tk.StringVar()
        ttk.Entry(top_frame, textvariable=self.user_var, width=16).grid(row=0, column=3, padx=4)

        ttk.Label(top_frame, text="Password:").grid(row=0, column=4, sticky="w", padx=4)
        self.pass_var = tk.StringVar()
        ttk.Entry(top_frame, textvariable=self.pass_var, show="*", width=16).grid(row=0, column=5, padx=4)

        self.login_btn = ttk.Button(top_frame, text="Connect / Login", command=self.login)
        self.login_btn.grid(row=0, column=6, padx=8)

        ttk.Label(top_frame, text="Save To:").grid(row=1, column=0, sticky="w", padx=4, pady=(6, 0))
        self.dl_dir_var = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        ttk.Entry(top_frame, textvariable=self.dl_dir_var, width=42).grid(
            row=1, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0)
        )
        ttk.Button(top_frame, text="Browse Folder...", command=self.choose_directory).grid(
            row=1, column=4, padx=4, pady=(6, 0), sticky="w"
        )

        # Max Concurrent Downloads Limiter
        limit_frame = ttk.Frame(top_frame)
        limit_frame.grid(row=1, column=5, padx=4, pady=(6, 0), sticky="w")
        ttk.Label(limit_frame, text="Max Concurrent DLs:").pack(side="left", padx=(0, 4))
        self.max_dl_var = tk.IntVar(value=2)
        self.max_dl_spin = ttk.Spinbox(
            limit_frame,
            from_=1,
            to=10,
            width=4,
            textvariable=self.max_dl_var,
            command=self._on_limit_changed,
        )
        self.max_dl_spin.pack(side="left")
        self.max_dl_spin.bind("<Return>", lambda *_: self._on_limit_changed())
        self.max_dl_spin.bind("<FocusOut>", lambda *_: self._on_limit_changed())

        self.status_var = tk.StringVar(value="Not connected.")
        ttk.Label(top_frame, textvariable=self.status_var, foreground="gray", wraplength=360).grid(
            row=1, column=6, columnspan=2, sticky="e", padx=4, pady=(6, 0)
        )

        # 2. Content Mode & Instant Filter Bar
        filter_frame = ttk.Frame(self.root, padding=(10, 2))
        filter_frame.pack(fill="x")

        ttk.Label(filter_frame, text="Content Type:").pack(side="left", padx=(0, 5))
        self.mode_var = tk.StringVar(value="vod")
        for label, val in [("Movies (VOD)", "vod"), ("TV Series", "series"), ("Live TV", "live")]:
            ttk.Radiobutton(
                filter_frame, text=label, variable=self.mode_var, value=val, command=self.load_categories
            ).pack(side="left", padx=6)

        ttk.Separator(filter_frame, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Label(filter_frame, text="Filter / Search:").pack(side="left", padx=4)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_search_filter())
        ttk.Entry(filter_frame, textvariable=self.search_var, width=32).pack(side="left", padx=4)
        ttk.Button(filter_frame, text="Clear", width=6, command=lambda: self.search_var.set("")).pack(
            side="left", padx=2
        )

        # 3. Triple Split View: Categories (Left) -> Series/Movies (Middle) -> Episodes (Right)
        main_pane = ttk.PanedWindow(self.root, orient="horizontal")
        main_pane.pack(fill="both", expand=True, padx=10, pady=5)

        # Pane 1: Categories
        cat_frame = ttk.LabelFrame(main_pane, text="Categories", padding=5)
        main_pane.add(cat_frame, weight=1)

        self.cat_list = tk.Listbox(cat_frame, exportselection=False, font=self.ui_font)
        cat_scroll = ttk.Scrollbar(cat_frame, orient="vertical", command=self.cat_list.yview)
        self.cat_list.configure(yscrollcommand=cat_scroll.set)
        self.cat_list.pack(side="left", fill="both", expand=True)
        cat_scroll.pack(side="right", fill="y")
        self.cat_list.bind("<<ListboxSelect>>", self.on_category_selected)

        # Pane 2: Series / Movies List
        series_frame = ttk.LabelFrame(main_pane, text="Series / Movies List (Click Series to load Episodes)", padding=5)
        main_pane.add(series_frame, weight=2)

        series_container = ttk.Frame(series_frame)
        series_container.pack(side="top", fill="both", expand=True)

        s_cols = ("id", "title")
        self.series_tree = ttk.Treeview(series_container, columns=s_cols, show="headings", selectmode="extended")
        self.series_tree.heading("id", text="ID")
        self.series_tree.heading("title", text="Title")
        self.series_tree.column("id", width=70, anchor="center", stretch=False)
        self.series_tree.column("title", width=300, anchor="w")

        series_scroll = ttk.Scrollbar(series_container, orient="vertical", command=self.series_tree.yview)
        self.series_tree.configure(yscrollcommand=series_scroll.set)
        self.series_tree.pack(side="left", fill="both", expand=True)
        series_scroll.pack(side="right", fill="y")
        self.series_tree.bind("<<TreeviewSelect>>", self.on_series_selected)

        # Pane 3: Episodes List
        ep_frame = ttk.LabelFrame(main_pane, text="Episodes List", padding=5)
        main_pane.add(ep_frame, weight=3)

        ep_container = ttk.Frame(ep_frame)
        ep_container.pack(side="top", fill="both", expand=True)

        e_cols = ("id", "title", "ext")
        self.ep_tree = ttk.Treeview(ep_container, columns=e_cols, show="headings", selectmode="extended")
        self.ep_tree.heading("id", text="ID")
        self.ep_tree.heading("title", text="Episode Title")
        self.ep_tree.heading("ext", text="Format")
        self.ep_tree.column("id", width=75, anchor="center", stretch=False)
        self.ep_tree.column("title", width=340, anchor="w")
        self.ep_tree.column("ext", width=70, anchor="center", stretch=False)

        ep_scroll = ttk.Scrollbar(ep_container, orient="vertical", command=self.ep_tree.yview)
        self.ep_tree.configure(yscrollcommand=ep_scroll.set)
        self.ep_tree.pack(side="left", fill="both", expand=True)
        ep_scroll.pack(side="right", fill="y")

        ep_action_bar = ttk.Frame(ep_frame, padding=(0, 6, 0, 0))
        ep_action_bar.pack(side="bottom", fill="x")

        ttk.Button(ep_action_bar, text="Download Selected", command=self.download_selected).pack(side="left", padx=2)
        ttk.Button(ep_action_bar, text="Download ALL Episodes", command=self.download_all_episodes).pack(side="left", padx=2)
        ttk.Button(ep_action_bar, text="Stream in mpv", command=self.stream_selected).pack(side="right", padx=2)

        # 4. Bottom Panel: Active & Completed Downloads Monitor
        dl_frame = ttk.LabelFrame(self.root, text="Download Queue & Progress Monitor", padding=5)
        dl_frame.pack(fill="x", padx=10, pady=(0, 10))

        dl_table_container = ttk.Frame(dl_frame)
        dl_table_container.pack(side="left", fill="both", expand=True)

        dl_cols = ("file", "progress", "size", "speed", "status")
        self.dl_tree = ttk.Treeview(dl_table_container, columns=dl_cols, show="headings", height=6, selectmode="extended")
        self.dl_tree.heading("file", text="Filename (Disk)")
        self.dl_tree.heading("progress", text="Progress")
        self.dl_tree.heading("size", text="Downloaded / Total")
        self.dl_tree.heading("speed", text="Speed")
        self.dl_tree.heading("status", text="Status")

        self.dl_tree.column("file", width=440, anchor="w")
        self.dl_tree.column("progress", width=95, anchor="center", stretch=False)
        self.dl_tree.column("size", width=175, anchor="center", stretch=False)
        self.dl_tree.column("speed", width=105, anchor="center", stretch=False)
        self.dl_tree.column("status", width=155, anchor="center", stretch=False)

        dl_scroll = ttk.Scrollbar(dl_table_container, orient="vertical", command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)
        self.dl_tree.pack(side="left", fill="both", expand=True)
        dl_scroll.pack(side="right", fill="y")

        # Queue management buttons (Cancel, Retry, Remove from List, Delete from List & Disk)
        dl_btn_frame = ttk.Frame(dl_frame)
        dl_btn_frame.pack(side="right", padx=6, fill="y")

        ttk.Button(dl_btn_frame, text="Cancel Selected", width=22, command=self.cancel_download).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Retry Selected", width=22, command=self.retry_download).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Remove from List", width=22, command=self.remove_from_list).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Delete from List & Disk", width=22, command=self.delete_from_disk).pack(pady=2)

    # --- Configuration & Helpers ---
    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.host_var.set(cfg.get("host", ""))
                self.user_var.set(cfg.get("user", ""))
                self.pass_var.set(cfg.get("password", ""))
                self.dl_dir_var.set(cfg.get("dl_dir", os.path.expanduser("~/Downloads")))
                self.max_dl_var.set(int(cfg.get("max_concurrent_dl", 2)))
            except Exception:
                pass

    def _save_config(self):
        try:
            max_dl = max(1, int(self.max_dl_var.get()))
        except Exception:
            max_dl = 2
        cfg = {
            "host": self.host_var.get().strip().rstrip("/"),
            "user": self.user_var.get().strip(),
            "password": self.pass_var.get().strip(),
            "dl_dir": self.dl_dir_var.get().strip(),
            "max_concurrent_dl": max_dl,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
        except Exception:
            pass

    def _on_limit_changed(self):
        self._save_config()
        self._schedule_queue()

    def choose_directory(self):
        folder = filedialog.askdirectory(initialdir=self.dl_dir_var.get())
        if folder:
            self.dl_dir_var.set(folder)
            self._save_config()

    def api_params(self, action=None, **extra):
        params = {
            "username": self.user_var.get().strip(),
            "password": self.pass_var.get().strip(),
        }
        if action:
            params["action"] = action
        for k, v in extra.items():
            if v is not None:
                params[k] = v
        return params

    def base_endpoint(self):
        host = self.host_var.get().strip().rstrip("/")
        return f"{host}/player_api.php"

    def api_get_json(self, action=None, timeout=(15, 65), **extra):
        r = self.session.get(
            self.base_endpoint(),
            params=self.api_params(action, **extra),
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    # --- API Calls ---
    def login(self):
        host = self.host_var.get().strip()
        if not host.startswith("http"):
            messagebox.showwarning("Invalid Server", "Server must start with http:// or https://")
            return
        self._save_config()
        self.catalog_cache.clear()
        self.status_var.set("Connecting...")

        def worker():
            try:
                data = self.api_get_json(timeout=(12, 30))
                auth = data.get("user_info", {}).get("auth", 0)
                if int(auth) == 1:
                    status = data["user_info"].get("status", "Active")
                    self.root.after(0, lambda: self.status_var.set(f"Connected ({status})"))
                    self.root.after(0, self.load_categories)
                else:
                    self.root.after(0, lambda: messagebox.showerror("Login Failed", "Invalid username or password."))
                    self.root.after(0, lambda: self.status_var.set("Authentication failed."))
            except Exception as e:
                self.root.after(0, lambda err=str(e): messagebox.showerror("Connection Error", err))
                self.root.after(0, lambda: self.status_var.set("Connection error."))

        threading.Thread(target=worker, daemon=True).start()

    def load_categories(self):
        mode = self.mode_var.get()
        action_map = {
            "vod": "get_vod_categories",
            "series": "get_series_categories",
            "live": "get_live_categories",
        }
        self.status_var.set(f"Loading {mode} categories...")
        self.cat_list.delete(0, tk.END)
        self.series_tree.delete(*self.series_tree.get_children())
        self.ep_tree.delete(*self.ep_tree.get_children())

        def worker():
            try:
                res = self.api_get_json(action_map[mode], timeout=(15, 45))
                cats = res if isinstance(res, list) else []
                self.categories = cats
                self.root.after(0, self._populate_categories)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.status_var.set(f"Failed loading categories: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_categories(self):
        self.cat_list.delete(0, tk.END)
        for c in self.categories:
            raw_name = c.get("category_name", "Unknown")
            self.cat_list.insert(tk.END, fix_text(raw_name))
        self.status_var.set(f"Loaded {len(self.categories)} categories.")

    def on_category_selected(self, event=None):
        sel = self.cat_list.curselection()
        if not sel or sel[0] >= len(self.categories):
            return
        cat_obj = self.categories[sel[0]]
        cat_id = str(cat_obj.get("category_id", ""))
        cat_name = fix_text(cat_obj.get("category_name", ""))
        mode = self.mode_var.get()
        action_map = {
            "vod": "get_vod_streams",
            "series": "get_series",
            "live": "get_live_streams",
        }

        self.req_token += 1
        current_token = self.req_token
        self.status_var.set(f"Fetching '{cat_name}' (up to 60s on large servers)...")

        def worker():
            try:
                if mode in self.catalog_cache:
                    data = [
                        x for x in self.catalog_cache[mode]
                        if str(x.get("category_id", "")) == cat_id
                    ]
                else:
                    try:
                        res = self.api_get_json(
                            action_map[mode], timeout=(15, 75), category_id=cat_id
                        )
                        data = res if isinstance(res, list) else []
                    except Exception:
                        self.root.after(
                            0,
                            lambda: self.status_var.set(
                                "Category query timed out; fetching full catalog fallback..."
                            ),
                        )
                        full_res = self.api_get_json(action_map[mode], timeout=(20, 120))
                        full_list = full_res if isinstance(full_res, list) else []
                        self.catalog_cache[mode] = full_list
                        data = [
                            x for x in full_list
                            if str(x.get("category_id", "")) == cat_id
                        ]

                if current_token != self.req_token:
                    return

                parsed = []
                for item in data:
                    raw_title = (item.get("name") or item.get("title") or "Untitled").strip()
                    if mode == "series":
                        parsed.append({
                            "id": str(item.get("series_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": "SERIES",
                            "kind": "series_folder",
                        })
                    elif mode == "vod":
                        parsed.append({
                            "id": str(item.get("stream_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": item.get("container_extension", "mp4"),
                            "kind": "movie",
                        })
                    else:
                        parsed.append({
                            "id": str(item.get("stream_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": "ts",
                            "kind": "live",
                        })

                self.current_series = parsed
                self.root.after(0, self.apply_search_filter)
                self.root.after(0, lambda: self.status_var.set(f"Showing {len(parsed)} items."))
            except Exception as e:
                if current_token == self.req_token:
                    err_msg = str(e)
                    self.root.after(0, lambda: self.status_var.set("Error loading category items."))
                    self.root.after(
                        0,
                        lambda: messagebox.showerror(
                            "Category Load Error",
                            f"Failed to load items from server:\n\n{err_msg}",
                        ),
                    )

        threading.Thread(target=worker, daemon=True).start()

    def on_series_selected(self, event=None):
        sel = self.series_tree.selection()
        if not sel:
            return
        row_id = sel[0]
        item = self.series_lookup.get(row_id)
        if not item or item["ext"] != "SERIES":
            return

        item_id = item["id"]
        raw_series_title = item["raw_title"]
        self.selected_series_title = raw_series_title
        self.status_var.set(f"Loading episodes for {fix_text(raw_series_title)}...")
        self.ep_tree.delete(*self.ep_tree.get_children())
        self.episode_lookup.clear()

        def worker():
            try:
                res = self.api_get_json("get_series_info", timeout=(15, 60), series_id=item_id)
                episodes_by_season = res.get("episodes", {}) if isinstance(res, dict) else {}
                parsed = []
                seasons = (
                    episodes_by_season.items()
                    if isinstance(episodes_by_season, dict)
                    else enumerate(episodes_by_season, 1)
                )
                for season_num, ep_list in seasons:
                    if isinstance(ep_list, dict):
                        ep_iterable = ep_list.values()
                    elif isinstance(ep_list, list):
                        ep_iterable = ep_list
                    else:
                        continue

                    for ep in ep_iterable:
                        if not isinstance(ep, dict):
                            continue
                        try:
                            s_num = int(season_num)
                        except ValueError:
                            s_num = 1
                        try:
                            e_num = int(ep.get("episode_num", 0))
                        except ValueError:
                            e_num = 0
                        ep_raw = (ep.get("title") or ep.get("name") or f"Episode {e_num}").strip()

                        # UI list stays clean without prepending series title
                        clean_ep_title = f"[S{s_num:02d}E{e_num:02d}] {ep_raw}"
                        parsed.append({
                            "id": str(ep.get("id")),
                            "raw_title": clean_ep_title,
                            "display_title": fix_text(clean_ep_title),
                            "series_title": raw_series_title,
                            "ext": ep.get("container_extension", "mp4"),
                            "kind": "series_episode",
                        })

                def update_ui():
                    self.current_episodes = parsed
                    self.ep_tree.delete(*self.ep_tree.get_children())
                    self.episode_lookup.clear()
                    for idx, ep_item in enumerate(parsed):
                        e_id = f"ep_{idx}"
                        self.episode_lookup[e_id] = ep_item
                        self.ep_tree.insert(
                            "",
                            tk.END,
                            iid=e_id,
                            values=(ep_item["id"], ep_item["display_title"], ep_item["ext"]),
                            tags=(ep_item["kind"],),
                        )
                    self.status_var.set(f"Loaded {len(parsed)} episodes.")

                self.root.after(0, update_ui)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.status_var.set(f"Error loading series: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def apply_search_filter(self):
        raw_query = self.search_var.get().strip().lower()
        shaped_query = fix_text(raw_query).lower()

        self.series_tree.delete(*self.series_tree.get_children())
        self.series_lookup.clear()
        if self.mode_var.get() != "series":
            self.ep_tree.delete(*self.ep_tree.get_children())
            self.episode_lookup.clear()

        for idx, item in enumerate(self.current_series):
            raw_t = item["raw_title"].lower()
            disp_t = item["display_title"].lower()
            if not raw_query or (raw_query in raw_t) or (shaped_query in disp_t):
                row_id = f"item_{idx}"
                self.series_lookup[row_id] = item
                self.series_tree.insert(
                    "",
                    tk.END,
                    iid=row_id,
                    values=(item["id"], item["display_title"]),
                )

    def build_stream_url(self, stream_id, ext, kind):
        host = self.host_var.get().strip().rstrip("/")
        user = self.user_var.get().strip()
        pw = self.pass_var.get().strip()
        if kind == "movie":
            return f"{host}/movie/{user}/{pw}/{stream_id}.{ext}"
        elif kind == "series_episode":
            return f"{host}/series/{user}/{pw}/{stream_id}.{ext}"
        else:
            return f"{host}/live/{user}/{pw}/{stream_id}.{ext}"

    # --- Streaming & Downloading with Concurrency Limiter ---
    def stream_selected(self):
        mode = self.mode_var.get()
        if mode == "series":
            sel = self.ep_tree.selection()
            if not sel:
                messagebox.showinfo("Selection Required", "Please select an episode from the episodes list to stream.")
                return
            item = self.episode_lookup.get(sel[0])
        else:
            sel = self.series_tree.selection()
            if not sel:
                messagebox.showinfo("Selection Required", "Please select a movie or live channel to stream.")
                return
            item = self.series_lookup.get(sel[0])

        if not item:
            return
        url = self.build_stream_url(item["id"], item["ext"], item["kind"])
        subprocess.Popen(["mpv", f"--title={item['raw_title']}", url])

    def download_selected(self):
        mode = self.mode_var.get()
        if mode == "series":
            sel = self.ep_tree.selection()
            if not sel:
                messagebox.showinfo("Selection Required", "Please select one or more episodes to download.")
                return
            items_to_download = [self.episode_lookup.get(r_id) for r_id in sel if r_id in self.episode_lookup]
        else:
            sel = self.series_tree.selection()
            if not sel:
                messagebox.showinfo("Selection Required", "Please select an item to download.")
                return
            items_to_download = [self.series_lookup.get(r_id) for r_id in sel if r_id in self.series_lookup]

        self._enqueue_downloads(items_to_download)

    def download_all_episodes(self):
        if not self.current_episodes:
            messagebox.showinfo("No Episodes", "No episodes loaded in the list.")
            return
        self._enqueue_downloads(self.current_episodes)

    def _enqueue_downloads(self, items):
        base_dir = self.dl_dir_var.get().strip()
        os.makedirs(base_dir, exist_ok=True)
        self._save_config()

        for item in items:
            if not item:
                continue

            stream_id = item["id"]
            raw_title = item["raw_title"]
            ext = item["ext"]
            kind = item["kind"]

            if kind == "series_episode":
                series_name = item.get("series_title") or self.selected_series_title or "Series"
                safe_series_folder = sanitize_filename(series_name)
                target_dir = os.path.join(base_dir, safe_series_folder)
                os.makedirs(target_dir, exist_ok=True)

                # Ensure filename on disk includes the series name
                if series_name.lower() not in raw_title.lower():
                    disk_base_title = f"{series_name} - {raw_title}"
                else:
                    disk_base_title = raw_title
            else:
                target_dir = base_dir
                disk_base_title = raw_title

            safe_file_stem = sanitize_filename(disk_base_title)
            filename = f"{safe_file_stem}.{ext}"
            display_filename = fix_text(filename)
            filepath = os.path.join(target_dir, filename)
            url = self.build_stream_url(stream_id, ext, kind)

            self.dl_counter += 1
            dl_id = f"dl_{self.dl_counter}"
            self.downloads[dl_id] = {
                "url": url,
                "filepath": filepath,
                "filename": filename,
                "display_filename": display_filename,
                "state": "queued",  # queued, downloading, completed, error, cancelled
                "cancel": False,
                "attempt_id": 0,
            }
            self.dl_tree.insert(
                "", 0, iid=dl_id, values=(display_filename, "0%", "0 MB / ?", "0.0 MB/s", "Queued")
            )

        self._schedule_queue()

    def _schedule_queue(self):
        """Starts queued downloads up to the user-configured Max Concurrent DLs limit."""
        with self.queue_lock:
            try:
                max_concurrent = max(1, int(self.max_dl_var.get()))
            except Exception:
                max_concurrent = 2

            active_count = sum(1 for d in self.downloads.values() if d["state"] == "downloading")
            if active_count >= max_concurrent:
                return

            # Start queued items in FIFO order (oldest queued first)
            for dl_id in reversed(self.dl_tree.get_children()):
                if active_count >= max_concurrent:
                    break
                info = self.downloads.get(dl_id)
                if info and info["state"] == "queued":
                    info["state"] = "downloading"
                    info["cancel"] = False
                    info["attempt_id"] += 1
                    attempt_id = info["attempt_id"]
                    self._update_dl_row(dl_id, info["display_filename"], "0%", "0 MB / ?", "0.0 MB/s", "Starting...")
                    active_count += 1
                    threading.Thread(
                        target=self._download_worker,
                        args=(dl_id, attempt_id),
                        daemon=True,
                    ).start()

    def _download_worker(self, dl_id, attempt_id):
        info = self.downloads.get(dl_id)
        if not info:
            self.root.after(0, self._schedule_queue)
            return

        url = info["url"]
        filepath = info["filepath"]
        display_filename = info["display_filename"]

        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with self.session.get(url, stream=True, timeout=(15, 60)) as r:
                r.raise_for_status()
                total_bytes = int(r.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()
                last_ui_update = 0

                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        curr = self.downloads.get(dl_id)
                        if not curr or curr["cancel"] or curr["attempt_id"] != attempt_id:
                            return
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if now - last_ui_update >= 0.25:
                                elapsed = max(now - start_time, 0.001)
                                speed_mb = (downloaded / (1024 * 1024)) / elapsed
                                dl_mb = downloaded / (1024 * 1024)
                                if total_bytes > 0:
                                    pct = f"{(downloaded / total_bytes) * 100:.1f}%"
                                    tot_mb = total_bytes / (1024 * 1024)
                                    size_str = f"{dl_mb:.1f} / {tot_mb:.1f} MB"
                                else:
                                    pct = "Live/Stream"
                                    size_str = f"{dl_mb:.1f} MB"

                                self.root.after(
                                    0,
                                    lambda p=pct, s=size_str, sp=f"{speed_mb:.2f} MB/s": self._update_dl_row(
                                        dl_id, display_filename, p, s, sp, "Downloading"
                                    ),
                                )
                                last_ui_update = now

                curr = self.downloads.get(dl_id)
                if curr and curr["attempt_id"] == attempt_id and not curr["cancel"]:
                    curr["state"] = "completed"
                    dl_mb = downloaded / (1024 * 1024)
                    self.root.after(
                        0,
                        lambda: self._update_dl_row(
                            dl_id, display_filename, "100%", f"{dl_mb:.1f} MB", "-", "Completed"
                        ),
                    )
        except Exception as e:
            curr = self.downloads.get(dl_id)
            if curr and curr["attempt_id"] == attempt_id and not curr["cancel"]:
                curr["state"] = "error"
                err_short = str(e)[:28]
                self.root.after(
                    0, lambda: self.dl_tree.set(dl_id, "status", f"Error: {err_short}")
                )
        finally:
            self.root.after(0, self._schedule_queue)

    def _update_dl_row(self, dl_id, display_filename, pct, size_str, speed_str, status):
        if self.dl_tree.exists(dl_id):
            self.dl_tree.item(dl_id, values=(display_filename, pct, size_str, speed_str, status))

    def cancel_download(self):
        for dl_id in self.dl_tree.selection():
            info = self.downloads.get(dl_id)
            if info and info["state"] in ("queued", "downloading"):
                info["cancel"] = True
                info["state"] = "cancelled"
                if self.dl_tree.exists(dl_id):
                    self.dl_tree.set(dl_id, "status", "Cancelled")
                    self.dl_tree.set(dl_id, "speed", "-")
        self._schedule_queue()

    def retry_download(self):
        sel = self.dl_tree.selection()
        if not sel:
            messagebox.showinfo("Retry Download", "Select one or more downloads from the queue to retry.")
            return
        for dl_id in sel:
            info = self.downloads.get(dl_id)
            if not info:
                continue
            # Cancel current worker if running and re-queue
            info["cancel"] = True
            info["state"] = "queued"
            self._update_dl_row(dl_id, info["display_filename"], "0%", "0 MB / ?", "0.0 MB/s", "Queued")
        self._schedule_queue()

    def remove_from_list(self):
        sel = self.dl_tree.selection()
        if not sel:
            return
        for dl_id in sel:
            info = self.downloads.get(dl_id)
            if info:
                info["cancel"] = True
                info["state"] = "cancelled"
                del self.downloads[dl_id]
            if self.dl_tree.exists(dl_id):
                self.dl_tree.delete(dl_id)
        self._schedule_queue()

    def delete_from_disk(self):
        sel = self.dl_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno(
            "Confirm Delete from Disk",
            f"Delete {len(sel)} selected item(s) from the list AND permanently remove their file(s) from disk?",
        ):
            return

        for dl_id in sel:
            info = self.downloads.get(dl_id)
            filepath = info["filepath"] if info else None
            if info:
                info["cancel"] = True
                info["state"] = "cancelled"
                del self.downloads[dl_id]
            if self.dl_tree.exists(dl_id):
                self.dl_tree.delete(dl_id)
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    # Remove parent series directory if it is now empty
                    parent_dir = os.path.dirname(filepath)
                    base_dir = os.path.abspath(self.dl_dir_var.get().strip())
                    if os.path.abspath(parent_dir) != base_dir and not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                except Exception as e:
                    messagebox.showwarning("Disk Delete Warning", f"Could not delete {filepath}:\n{e}")

        self._schedule_queue()


if __name__ == "__main__":
    root = tk.Tk()
    app = IPTVManagerApp(root)
    root.mainloop()
