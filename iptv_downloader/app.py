"""Tk GUI: browse an Xtream Codes server, preview posters, stream in mpv, download with resume."""

import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, downloader, storage, xtream
from .posters import HAS_PIL, PosterLoader
from .text import HAS_ARABIC_SUPPORT, fix_text, sanitize_filename

POSTER_SIZE = (240, 360)
AUTO_RESUME_MAX = 3
MB = 1024 * 1024


def fmt_size(n):
    if n is None:
        return "?"
    if n >= 1024 * MB:
        return f"{n / (1024 * MB):.2f} GB"
    return f"{n / MB:.1f} MB"


class IPTVManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"IPTV Downloader {__version__} — Xtream Browser & Download Manager")
        self.root.geometry("1480x860")

        self.session = xtream.make_session()

        self.ui_font = ("DejaVu Sans", 10)
        self.root.option_add("*Font", self.ui_font)

        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=self.ui_font)
        style.configure("Treeview", font=self.ui_font, rowheight=26)
        style.configure("Treeview.Heading", font=("DejaVu Sans", 10, "bold"))
        style.configure("PreviewTitle.TLabel", font=("DejaVu Sans", 11, "bold"))

        # Data & state
        self.categories = []
        self.current_series = []
        self.current_episodes = []
        self.selected_series_title = ""
        self.loaded_series_id = None
        self.series_lookup = {}
        self.episode_lookup = {}
        self.catalog_cache = {}
        self.downloads = {}
        self.dl_counter = 0
        self.req_token = 0
        self.series_token = 0
        self.preview_item = None  # the list item the preview panel is showing
        self._pending_disk_deletes = {}

        self.posters = PosterLoader(
            self.root, xtream.make_session(pool_size=4),
            os.path.join(storage.cache_dir(), "posters"), size=POSTER_SIZE,
        )

        self._build_ui()
        self._load_config()

        tips = []
        if not HAS_PIL:
            tips.append("install Pillow for JPEG posters")
        if not HAS_ARABIC_SUPPORT:
            tips.append("install arabic-reshaper & python-bidi for connected RTL Arabic text")
        if tips:
            self.status_var.set("Tip: " + "; ".join(tips) + ".")

        self._load_queue()  # its "restored N downloads" message wins over the tips
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- UI ----------------------------------------------------------------------
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
            limit_frame, from_=1, to=10, width=4, textvariable=self.max_dl_var, command=self._on_limit_changed,
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

        # 3. Split View: Categories -> Series/Movies -> Preview -> Episodes
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
        self.series_tree.column("title", width=280, anchor="w")

        series_scroll = ttk.Scrollbar(series_container, orient="vertical", command=self.series_tree.yview)
        self.series_tree.configure(yscrollcommand=series_scroll.set)
        self.series_tree.pack(side="left", fill="both", expand=True)
        series_scroll.pack(side="right", fill="y")
        self.series_tree.bind("<<TreeviewSelect>>", self.on_series_selected)
        # Clicking the already-selected series brings its preview back after viewing an episode.
        self.series_tree.bind("<ButtonRelease-1>", lambda e: self._preview_from_tree(self.series_tree, self.series_lookup))

        # Pane 3: Poster & info preview
        self._build_preview(main_pane)

        # Pane 4: Episodes List
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
        self.ep_tree.column("title", width=320, anchor="w")
        self.ep_tree.column("ext", width=70, anchor="center", stretch=False)

        ep_scroll = ttk.Scrollbar(ep_container, orient="vertical", command=self.ep_tree.yview)
        self.ep_tree.configure(yscrollcommand=ep_scroll.set)
        self.ep_tree.pack(side="left", fill="both", expand=True)
        ep_scroll.pack(side="right", fill="y")
        self.ep_tree.bind("<<TreeviewSelect>>", lambda e: self._preview_from_tree(self.ep_tree, self.episode_lookup))

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
        self.dl_tree = ttk.Treeview(dl_table_container, columns=dl_cols, show="headings", height=7, selectmode="extended")
        self.dl_tree.heading("file", text="Filename (Disk)")
        self.dl_tree.heading("progress", text="Progress")
        self.dl_tree.heading("size", text="Downloaded / Total")
        self.dl_tree.heading("speed", text="Speed")
        self.dl_tree.heading("status", text="Status")

        self.dl_tree.column("file", width=440, anchor="w")
        self.dl_tree.column("progress", width=95, anchor="center", stretch=False)
        self.dl_tree.column("size", width=190, anchor="center", stretch=False)
        self.dl_tree.column("speed", width=105, anchor="center", stretch=False)
        self.dl_tree.column("status", width=300, anchor="w", stretch=False)

        dl_scroll = ttk.Scrollbar(dl_table_container, orient="vertical", command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)
        self.dl_tree.pack(side="left", fill="both", expand=True)
        dl_scroll.pack(side="right", fill="y")

        # Queue management buttons
        dl_btn_frame = ttk.Frame(dl_frame)
        dl_btn_frame.pack(side="right", padx=6, fill="y")

        ttk.Button(dl_btn_frame, text="Pause / Cancel Selected", width=24, command=self.cancel_download).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Resume Selected", width=24, command=self.resume_download).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Restart from Scratch", width=24, command=self.restart_download).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Remove from List", width=24, command=self.remove_from_list).pack(pady=2)
        ttk.Button(dl_btn_frame, text="Delete from List & Disk", width=24, command=self.delete_from_disk).pack(pady=2)
        self.auto_resume_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dl_btn_frame, text=f"Auto-resume on error ({AUTO_RESUME_MAX}x)", variable=self.auto_resume_var,
            command=self._save_config,
        ).pack(pady=(6, 2), anchor="w")

    def _build_preview(self, main_pane):
        frame = ttk.LabelFrame(main_pane, text="Preview", padding=6)
        main_pane.add(frame, weight=0)

        self._blank_poster = tk.PhotoImage(width=POSTER_SIZE[0], height=POSTER_SIZE[1])
        self.poster_label = tk.Label(
            frame, image=self._blank_poster, compound="center", text="Select a title",
            bg="#2b2b2b", fg="#bbbbbb", wraplength=POSTER_SIZE[0] - 20, justify="center",
        )
        self.poster_label.pack(side="top", pady=(0, 6))

        self.preview_title_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.preview_title_var, style="PreviewTitle.TLabel",
                  wraplength=POSTER_SIZE[0] + 20, justify="left").pack(side="top", fill="x")
        self.preview_meta_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.preview_meta_var, foreground="gray",
                  wraplength=POSTER_SIZE[0] + 20, justify="left").pack(side="top", fill="x", pady=(2, 4))

        plot_box = ttk.Frame(frame)
        plot_box.pack(side="top", fill="both", expand=True)
        self.plot_text = tk.Text(plot_box, wrap="word", width=32, height=6, relief="flat",
                                 font=self.ui_font, state="disabled", takefocus=0)
        plot_scroll = ttk.Scrollbar(plot_box, orient="vertical", command=self.plot_text.yview)
        self.plot_text.configure(yscrollcommand=plot_scroll.set)
        self.plot_text.pack(side="left", fill="both", expand=True)
        plot_scroll.pack(side="right", fill="y")

    # --- Preview panel -------------------------------------------------------------
    def show_preview(self, p):
        p = p or {}
        self.preview_title_var.set(fix_text(p.get("title", "")))
        meta = [p.get("year", "")]
        if p.get("rating"):
            meta.append(f"★ {p['rating']}")
        meta += [p.get("genre", ""), p.get("duration", "")]
        self.preview_meta_var.set(fix_text("  ·  ".join(m for m in meta if m)))
        self.plot_text.configure(state="normal")
        self.plot_text.delete("1.0", tk.END)
        self.plot_text.insert("1.0", "\n".join(fix_text(line) for line in p.get("plot", "").splitlines()))
        self.plot_text.configure(state="disabled")

        if p.get("poster"):
            self.poster_label.configure(image=self._blank_poster, text="Loading poster…")
        self.posters.request(p.get("poster", ""), self._set_poster)

    def clear_preview(self):
        self.preview_item = None
        self.posters.cancel()
        self.preview_title_var.set("")
        self.preview_meta_var.set("")
        self.plot_text.configure(state="normal")
        self.plot_text.delete("1.0", tk.END)
        self.plot_text.configure(state="disabled")
        self.poster_label.configure(image=self._blank_poster, text="Select a title")

    def _set_poster(self, photo, message):
        if photo is not None:
            self.poster_label.configure(image=photo, text="")
        else:
            self.poster_label.configure(image=self._blank_poster, text=message)

    @staticmethod
    def _focused_item(tree, lookup):
        focus = tree.focus()
        if focus in lookup and focus in tree.selection():
            return lookup[focus]
        sel = tree.selection()
        return lookup.get(sel[0]) if sel else None

    def _preview_from_tree(self, tree, lookup):
        item = self._focused_item(tree, lookup)
        if not item or item is self.preview_item:
            return
        self.preview_item = item
        self.show_preview(item.get("preview"))
        if item["kind"] == "movie" and not item.get("info_loaded"):
            self._load_vod_info(item)

    def _load_vod_info(self, item):
        creds = self._creds()

        def worker():
            try:
                res = xtream.api_get_json(self.session, creds, "get_vod_info", timeout=(10, 30), vod_id=item["id"])
                prev = xtream.movie_info_preview(res if isinstance(res, dict) else {}, item.get("preview"))
            except Exception:
                return  # keep the list-entry preview

            def done():
                item["preview"] = prev
                item["info_loaded"] = True
                if self.preview_item is item:
                    self.show_preview(prev)

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # --- Configuration & Helpers ---------------------------------------------------
    def _load_config(self):
        cfg = storage.read_json(storage.config_file())
        migrated = False
        if cfg is None:
            cfg = storage.read_json(storage.LEGACY_CONFIG_FILE)
            migrated = cfg is not None
        if not isinstance(cfg, dict):
            return
        self.host_var.set(cfg.get("host", ""))
        self.user_var.set(cfg.get("user", ""))
        self.pass_var.set(cfg.get("password", ""))
        self.dl_dir_var.set(cfg.get("dl_dir", os.path.expanduser("~/Downloads")))
        try:
            self.max_dl_var.set(int(cfg.get("max_concurrent_dl", 2)))
        except (TypeError, ValueError):
            pass
        self.auto_resume_var.set(bool(cfg.get("auto_resume", False)))
        if migrated:
            self._save_config()

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
            "auto_resume": bool(self.auto_resume_var.get()),
        }
        try:
            storage.write_json_atomic(storage.config_file(), cfg)
        except OSError as e:
            self.status_var.set(f"Could not save settings: {e}")

    def _on_limit_changed(self):
        self._save_config()
        self._schedule_queue()

    def choose_directory(self):
        folder = filedialog.askdirectory(initialdir=self.dl_dir_var.get())
        if folder:
            self.dl_dir_var.set(folder)
            self._save_config()

    def _creds(self):
        """Read on the Tk thread; pass the result to worker threads."""
        return xtream.Credentials(self.host_var.get(), self.user_var.get(), self.pass_var.get())

    # --- API Calls ---------------------------------------------------------------
    def login(self):
        creds = self._creds()
        if not creds.host.startswith("http"):
            messagebox.showwarning("Invalid Server", "Server must start with http:// or https://")
            return
        self._save_config()
        self.catalog_cache.clear()
        self.status_var.set("Connecting...")

        def worker():
            try:
                data = xtream.api_get_json(self.session, creds, timeout=(12, 30))
                user_info = data.get("user_info", {}) if isinstance(data, dict) else {}
                if str(user_info.get("auth", 0)) == "1":
                    status = user_info.get("status", "Active")
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
        creds = self._creds()
        self.status_var.set(f"Loading {mode} categories...")
        self.cat_list.delete(0, tk.END)
        self.current_series = []
        self.current_episodes = []
        self.loaded_series_id = None
        self.series_tree.delete(*self.series_tree.get_children())
        self.ep_tree.delete(*self.ep_tree.get_children())
        self.series_lookup.clear()
        self.episode_lookup.clear()
        self.clear_preview()

        def worker():
            try:
                res = xtream.api_get_json(self.session, creds, action_map[mode], timeout=(15, 45))
                cats = res if isinstance(res, list) else []

                def apply():
                    if self.mode_var.get() == mode:
                        self.categories = cats
                        self._populate_categories()

                self.root.after(0, apply)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.status_var.set(f"Failed loading categories: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_categories(self):
        self.cat_list.delete(0, tk.END)
        for c in self.categories:
            self.cat_list.insert(tk.END, fix_text(c.get("category_name", "Unknown")))
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
        creds = self._creds()

        self.req_token += 1
        current_token = self.req_token
        self.status_var.set(f"Fetching '{cat_name}' (up to 60s on large servers)...")

        def worker():
            try:
                if mode in self.catalog_cache:
                    data = [x for x in self.catalog_cache[mode] if str(x.get("category_id", "")) == cat_id]
                else:
                    try:
                        res = xtream.api_get_json(self.session, creds, action_map[mode], timeout=(15, 75),
                                                  category_id=cat_id)
                        data = res if isinstance(res, list) else []
                    except Exception:
                        self.root.after(
                            0, lambda: self.status_var.set("Category query timed out; fetching full catalog fallback...")
                        )
                        full_res = xtream.api_get_json(self.session, creds, action_map[mode], timeout=(20, 120))
                        full_list = full_res if isinstance(full_res, list) else []
                        self.catalog_cache[mode] = full_list
                        data = [x for x in full_list if str(x.get("category_id", "")) == cat_id]

                if current_token != self.req_token:
                    return

                parsed = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    raw_title = str(item.get("name") or item.get("title") or "Untitled").strip()
                    if mode == "series":
                        parsed.append({
                            "id": str(item.get("series_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": "SERIES",
                            "kind": "series_folder",
                            "preview": xtream.series_preview(item),
                        })
                    elif mode == "vod":
                        parsed.append({
                            "id": str(item.get("stream_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": item.get("container_extension") or "mp4",
                            "kind": "movie",
                            "preview": xtream.movie_preview(item),
                        })
                    else:
                        parsed.append({
                            "id": str(item.get("stream_id")),
                            "raw_title": raw_title,
                            "display_title": fix_text(raw_title),
                            "ext": "ts",
                            "kind": "live",
                            "preview": xtream.live_preview(item),
                        })

                def apply():
                    if current_token != self.req_token:
                        return
                    self.current_series = parsed
                    self.apply_search_filter()
                    self.status_var.set(f"Showing {len(parsed)} items.")

                self.root.after(0, apply)
            except Exception as e:
                if current_token == self.req_token:
                    err_msg = str(e)
                    self.root.after(0, lambda: self.status_var.set("Error loading category items."))
                    self.root.after(
                        0,
                        lambda: messagebox.showerror("Category Load Error", f"Failed to load items from server:\n\n{err_msg}"),
                    )

        threading.Thread(target=worker, daemon=True).start()

    def on_series_selected(self, event=None):
        item = self._focused_item(self.series_tree, self.series_lookup)
        if not item:
            return
        self._preview_from_tree(self.series_tree, self.series_lookup)
        if item["kind"] != "series_folder" or item["id"] == self.loaded_series_id:
            return

        item_id = item["id"]
        raw_series_title = item["raw_title"]
        self.selected_series_title = raw_series_title
        self.loaded_series_id = item_id
        self.series_token += 1
        token = self.series_token
        creds = self._creds()
        self.status_var.set(f"Loading episodes for {fix_text(raw_series_title)}...")
        self.ep_tree.delete(*self.ep_tree.get_children())
        self.episode_lookup.clear()
        self.current_episodes = []

        def worker():
            try:
                res = xtream.api_get_json(self.session, creds, "get_series_info", timeout=(15, 60), series_id=item_id)
                res = res if isinstance(res, dict) else {}
                series_prev = xtream.series_info_preview(res, item.get("preview"))
                episodes_by_season = res.get("episodes", {})
                parsed = []
                seasons = (
                    episodes_by_season.items()
                    if isinstance(episodes_by_season, dict)
                    else enumerate(episodes_by_season or [], 1)
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
                            s_num = int(ep.get("season") or season_num)
                        except (TypeError, ValueError):
                            s_num = 1
                        try:
                            e_num = int(ep.get("episode_num", 0))
                        except (TypeError, ValueError):
                            e_num = 0
                        ep_raw = str(ep.get("title") or ep.get("name") or f"Episode {e_num}").strip()

                        # UI list stays clean without prepending series title
                        clean_ep_title = f"[S{s_num:02d}E{e_num:02d}] {ep_raw}"
                        parsed.append({
                            "id": str(ep.get("id")),
                            "raw_title": clean_ep_title,
                            "display_title": fix_text(clean_ep_title),
                            "series_title": raw_series_title,
                            "ext": ep.get("container_extension") or "mp4",
                            "kind": "series_episode",
                            "preview": xtream.episode_preview(ep, clean_ep_title, series_prev),
                        })

                def update_ui():
                    if token != self.series_token:
                        return
                    item["preview"] = series_prev
                    if self.preview_item is item:
                        self.show_preview(series_prev)
                    self.current_episodes = parsed
                    self.ep_tree.delete(*self.ep_tree.get_children())
                    self.episode_lookup.clear()
                    for idx, ep_item in enumerate(parsed):
                        e_id = f"ep_{idx}"
                        self.episode_lookup[e_id] = ep_item
                        self.ep_tree.insert(
                            "", tk.END, iid=e_id,
                            values=(ep_item["id"], ep_item["display_title"], ep_item["ext"]),
                            tags=(ep_item["kind"],),
                        )
                    self.status_var.set(f"Loaded {len(parsed)} episodes.")

                self.root.after(0, update_ui)
            except Exception as e:
                def failed(err=str(e)):
                    if token == self.series_token:
                        self.loaded_series_id = None
                        self.status_var.set(f"Error loading series: {err}")

                self.root.after(0, failed)

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
                self.series_tree.insert("", tk.END, iid=row_id, values=(item["id"], item["display_title"]))

    # --- Streaming ---------------------------------------------------------------
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
        url = xtream.build_stream_url(self._creds(), item["id"], item["ext"], item["kind"])
        try:
            subprocess.Popen(["mpv", f"--title={item['raw_title']}", url])
        except FileNotFoundError:
            messagebox.showerror("mpv not found", "Streaming needs mpv. Install it and make sure it is on your PATH.")

    # --- Downloads ---------------------------------------------------------------
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
            skipped = [i for i in items_to_download if i and i["kind"] == "series_folder"]
            if skipped:
                messagebox.showinfo("Series Selected",
                                    "To download a series, select it, then use the buttons under the Episodes list.")
            items_to_download = [i for i in items_to_download if i and i["kind"] != "series_folder"]

        self._enqueue_downloads(items_to_download)

    def download_all_episodes(self):
        if not self.current_episodes:
            messagebox.showinfo("No Episodes", "No episodes loaded in the list.")
            return
        self._enqueue_downloads(self.current_episodes)

    def _find_download_by_path(self, filepath):
        for dl_id, info in self.downloads.items():
            if info["filepath"] == filepath:
                return dl_id
        return None

    def _new_download(self, kind, stream_id, ext, filepath, filename, state="queued", done=0, total=None):
        self.dl_counter += 1
        dl_id = f"dl_{self.dl_counter}"
        self.downloads[dl_id] = {
            "kind": kind,
            "stream_id": stream_id,
            "ext": ext,
            "filepath": filepath,
            "filename": filename,
            "display_filename": fix_text(filename),
            "state": state,  # queued, downloading, completed, error, cancelled (= paused)
            "cancel": False,
            "attempt_id": 0,
            "worker_alive": False,
            "done": done or 0,
            "total": total,
            "message": "",
            "auto_attempts": 0,
        }
        self.dl_tree.insert("", 0, iid=dl_id, values=("", "", "", "", ""))
        self._refresh_dl_row(dl_id)
        return dl_id

    def _enqueue_downloads(self, items):
        base_dir = self.dl_dir_var.get().strip()
        if not base_dir:
            messagebox.showwarning("No Destination", "Choose a folder to save downloads to first.")
            return
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Destination Error", f"Cannot create {base_dir}:\n{e}")
            return
        self._save_config()

        already = 0
        for item in items:
            if not item:
                continue

            raw_title = item["raw_title"]
            ext = item["ext"]
            kind = item["kind"]

            if kind == "series_episode":
                series_name = item.get("series_title") or self.selected_series_title or "Series"
                target_dir = os.path.join(base_dir, sanitize_filename(series_name))
                # Ensure filename on disk includes the series name
                if series_name.lower() not in raw_title.lower():
                    disk_base_title = f"{series_name} - {raw_title}"
                else:
                    disk_base_title = raw_title
            else:
                target_dir = base_dir
                disk_base_title = raw_title

            filename = f"{sanitize_filename(disk_base_title)}.{ext}"
            filepath = os.path.join(target_dir, filename)

            existing = self._find_download_by_path(filepath)
            if existing:
                already += 1
                continue
            if kind != "live" and os.path.exists(filepath):
                dl_id = self._new_download(kind, item["id"], ext, filepath, filename, state="completed",
                                           done=os.path.getsize(filepath))
                self.downloads[dl_id]["message"] = "Already on disk"
                self._refresh_dl_row(dl_id)
                continue
            self._new_download(kind, item["id"], ext, filepath, filename)

        if already:
            self.status_var.set(f"{already} item(s) already in the download list (use Resume / Restart there).")
        self._save_queue()
        self._schedule_queue()

    def _schedule_queue(self):
        """Starts queued downloads up to the user-configured Max Concurrent DLs limit. Tk thread only."""
        try:
            max_concurrent = max(1, int(self.max_dl_var.get()))
        except Exception:
            max_concurrent = 2

        active_count = sum(1 for d in self.downloads.values() if d["worker_alive"])
        if active_count >= max_concurrent:
            return

        creds = None
        # Start queued items in FIFO order (oldest queued first)
        for dl_id in reversed(self.dl_tree.get_children()):
            if active_count >= max_concurrent:
                break
            info = self.downloads.get(dl_id)
            # A previous worker for this row must have exited before a new one may touch its file.
            if not info or info["state"] != "queued" or info["worker_alive"]:
                continue
            creds = creds or self._creds()
            if not creds.host.startswith("http"):
                self.status_var.set("Enter the server details to start queued downloads.")
                return
            info.update(state="downloading", cancel=False, worker_alive=True, message="")
            info["attempt_id"] += 1
            url = xtream.build_stream_url(creds, info["stream_id"], info["ext"], info["kind"])
            self._refresh_dl_row(dl_id, status="Starting...")
            active_count += 1
            threading.Thread(
                target=self._download_worker,
                args=(dl_id, info["attempt_id"], url, info["filepath"], info["kind"] == "live"),
                daemon=True,
            ).start()

    def _download_worker(self, dl_id, attempt_id, url, filepath, live):
        def should_stop():
            curr = self.downloads.get(dl_id)
            return curr is None or curr["cancel"] or curr["attempt_id"] != attempt_id

        def on_progress(p):
            self.root.after(0, self._on_progress, dl_id, attempt_id, p)

        try:
            result = downloader.download(self.session, url, filepath, on_progress, should_stop, live=live)
        except Exception as e:  # defensive: the engine reports expected errors itself
            result = downloader.DownloadResult(downloader.ERROR, downloader.partial_size(filepath, live), None, str(e)[:80])
        try:
            self.root.after(0, self._on_download_finished, dl_id, attempt_id, result)
        except RuntimeError:
            pass  # window already closed

    def _on_progress(self, dl_id, attempt_id, p):
        info = self.downloads.get(dl_id)
        if not info or info["attempt_id"] != attempt_id or info["state"] != "downloading":
            return
        info["done"], info["total"] = p.done, p.total
        if p.restarted:
            status = "Downloading (server can't resume; restarted)"
        elif p.resumed_from:
            status = f"Resumed from {fmt_size(p.resumed_from)}"
        else:
            status = "Downloading"
        self._refresh_dl_row(dl_id, speed=f"{p.speed / MB:.2f} MB/s", status=status)

    def _on_download_finished(self, dl_id, attempt_id, result):
        for path in self._pending_disk_deletes.pop(dl_id, ()):
            self._remove_file(path)
        info = self.downloads.get(dl_id)
        if info is None or info["attempt_id"] != attempt_id:
            self._schedule_queue()
            return

        info["worker_alive"] = False
        info["done"] = result.done
        if result.total is not None:
            info["total"] = result.total

        if info.pop("pending_restart", False):
            self._discard_data(info)
            info["state"] = "queued"
        elif info["state"] == "downloading":  # the worker ended on its own, not by user action
            if result.status == downloader.COMPLETED:
                info["state"] = "completed"
                info["message"] = ""
                info["auto_attempts"] = 0
            else:
                info["state"] = "error"
                info["message"] = result.message or result.status
                if self.auto_resume_var.get() and info["auto_attempts"] < AUTO_RESUME_MAX:
                    info["auto_attempts"] += 1
                    delay = 5 * 2 ** (info["auto_attempts"] - 1)
                    info["message"] += f" — auto-resume {info['auto_attempts']}/{AUTO_RESUME_MAX} in {delay}s"
                    self.root.after(delay * 1000, self._auto_resume, dl_id, attempt_id)
        self._refresh_dl_row(dl_id)
        self._save_queue()
        self._schedule_queue()

    def _auto_resume(self, dl_id, attempt_id):
        info = self.downloads.get(dl_id)
        if info and info["state"] == "error" and info["attempt_id"] == attempt_id:
            info["state"] = "queued"
            self._refresh_dl_row(dl_id)
            self._save_queue()
            self._schedule_queue()

    def _refresh_dl_row(self, dl_id, speed="-", status=None):
        info = self.downloads.get(dl_id)
        if not info or not self.dl_tree.exists(dl_id):
            return
        done, total, state = info["done"], info["total"], info["state"]
        if total:
            pct = f"{min(done / total, 1) * 100:.1f}%"
            size_str = f"{fmt_size(done)} / {fmt_size(total)}"
        elif info["kind"] == "live":
            pct, size_str = "Live", fmt_size(done)
        else:
            pct = "100%" if state == "completed" else ("0%" if not done else "?")
            size_str = f"{fmt_size(done)} / ?" if state != "completed" else fmt_size(done)
        if status is None:
            if state == "queued":
                status = "Queued" + (" (restarting)" if info.get("pending_restart") else "")
            elif state == "downloading":
                status = "Downloading"
            elif state == "completed":
                status = info["message"] or ("Recording saved" if info["kind"] == "live" else "Completed")
            elif state == "error":
                status = f"Error: {info['message']} — Resume available"
            elif state == "cancelled":
                status = "Paused — Resume available" if done else "Cancelled"
            else:
                status = state
        self.dl_tree.item(dl_id, values=(info["display_filename"], pct, size_str, speed, status))

    def _save_queue(self):
        entries = [self.downloads[i] for i in reversed(self.dl_tree.get_children()) if i in self.downloads]
        try:
            downloader.save_queue(storage.queue_file(), entries)
        except OSError as e:
            self.status_var.set(f"Could not save the download queue: {e}")

    def _load_queue(self):
        for e in downloader.load_queue(storage.queue_file()):
            self._new_download(e["kind"], e["stream_id"], e["ext"] or "mp4", e["filepath"],
                               e["filename"] or os.path.basename(e["filepath"]),
                               state=e["state"], done=e["done"], total=e["total"])
        if self.downloads:
            paused = sum(1 for d in self.downloads.values() if d["state"] in ("cancelled", "error"))
            if paused:
                self.status_var.set(f"Restored {paused} unfinished download(s). Select them and press Resume.")

    def _selected_downloads(self, title):
        sel = self.dl_tree.selection()
        if not sel:
            messagebox.showinfo(title, "Select one or more downloads in the queue first.")
        return [(dl_id, self.downloads[dl_id]) for dl_id in sel if dl_id in self.downloads]

    def cancel_download(self):
        """Pause: the partial file is kept, so Resume continues from where it stopped."""
        for dl_id, info in self._selected_downloads("Pause / Cancel"):
            if info["state"] in ("queued", "downloading"):
                info["cancel"] = True
                info["state"] = "cancelled"
                info.pop("pending_restart", None)
                self._refresh_dl_row(dl_id)
        self._save_queue()
        self._schedule_queue()

    def resume_download(self):
        """Continue failed, paused or interrupted downloads from their partial data."""
        selected = self._selected_downloads("Resume Download")
        resumable = [(d, i) for d, i in selected if i["state"] in ("error", "cancelled")]
        if selected and not resumable:
            messagebox.showinfo("Resume Download", "Only paused, cancelled or failed downloads can be resumed.")
        for dl_id, info in resumable:
            info["state"] = "queued"
            info["auto_attempts"] = 0
            self._refresh_dl_row(dl_id)
        self._save_queue()
        self._schedule_queue()

    def restart_download(self):
        """Throw away partial (or finished) data and download again from byte 0."""
        selected = self._selected_downloads("Restart Download")
        if not selected:
            return
        if not messagebox.askyesno(
            "Restart from Scratch",
            f"Discard the downloaded data of {len(selected)} item(s) and download them again from the beginning?",
        ):
            return
        for dl_id, info in selected:
            info["cancel"] = True
            info["auto_attempts"] = 0
            info["message"] = ""
            if info["worker_alive"]:
                info["pending_restart"] = True  # files are discarded once the worker has let go of them
            else:
                self._discard_data(info)
            info["state"] = "queued"
            self._refresh_dl_row(dl_id)
        self._save_queue()
        self._schedule_queue()

    def _discard_data(self, info):
        self._remove_file(downloader.part_path(info["filepath"]))
        self._remove_file(info["filepath"])
        info["done"] = 0
        info["total"] = None

    @staticmethod
    def _remove_file(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            messagebox.showwarning("Disk Delete Warning", f"Could not delete {path}:\n{e}")

    def remove_from_list(self):
        """Forget downloads; files (including .part files) stay on disk."""
        for dl_id, info in self._selected_downloads("Remove from List"):
            info["cancel"] = True
            info["state"] = "cancelled"
            del self.downloads[dl_id]
            if self.dl_tree.exists(dl_id):
                self.dl_tree.delete(dl_id)
        self._save_queue()
        self._schedule_queue()

    def delete_from_disk(self):
        selected = self._selected_downloads("Delete from Disk")
        if not selected:
            return
        if not messagebox.askyesno(
            "Confirm Delete from Disk",
            f"Delete {len(selected)} selected item(s) from the list AND permanently remove their file(s) "
            "(including partial downloads) from disk?",
        ):
            return

        base_dir = os.path.abspath(self.dl_dir_var.get().strip())
        for dl_id, info in selected:
            info["cancel"] = True
            info["state"] = "cancelled"
            del self.downloads[dl_id]
            if self.dl_tree.exists(dl_id):
                self.dl_tree.delete(dl_id)
            paths = (info["filepath"], downloader.part_path(info["filepath"]))
            if info["worker_alive"]:
                self._pending_disk_deletes[dl_id] = paths  # delete again once the worker exits
            for path in paths:
                self._remove_file(path)
            # Remove parent series directory if it is now empty
            parent_dir = os.path.dirname(info["filepath"])
            try:
                if os.path.abspath(parent_dir) != base_dir and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except OSError:
                pass

        self._save_queue()
        self._schedule_queue()

    def on_close(self):
        # Save first: rows that are still downloading are stored as interrupted and come back paused.
        self._save_queue()
        for info in self.downloads.values():
            info["cancel"] = True
        self.posters.shutdown()
        self.root.destroy()


def main():
    root = tk.Tk()
    IPTVManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
