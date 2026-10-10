#!/usr/bin/env python3
"""
area_reset_gui.py  -  reset chosen AREAS of the map by moving their saved
chunk files into a trash folder (nothing is deleted until you say so).

How it works
------------
Project Zomboid regenerates any map chunk whose saved file is missing, from
the original map. So "reset this shop" == "the saved chunk files for that
shop are no longer in the server's map/ folder".

This tool:
  * reads the SAME AdminZoneReset_Shared.txt file(s) the Zone Reset Planner
    reads, so you tick shops by name - no typing coordinates,
  * works out the protected chunks itself (protected zones + safehouses with
    their 16-tile buffer, exactly as the planner does) and never touches them,
  * works out which chunk files (map/<x>/<y>.bin) cover them,
  * MOVES those files into  <map>_reset_trash/<timestamp>/  (a rename on
    the server - fast),
  * can EMPTY the trash when you are sure (there is no undo button: once the
    server starts it rebuilds the chunks, so putting old files back is moot).

IMPORTANT: stop the server first (or be certain nobody has been near the
area and the chunks are unloaded). If the server still has those chunks in
memory it will write them straight back to disk and your reset is undone.
Everything in the chunk resets: loot, broken doors, player builds, items
on the floor.

Setup (one time):   pip install paramiko
Run:                python area_reset_gui.py
Your password is only typed into the window and used for that one action.
It is never saved. Host / port / user / path / area list are saved next to
this script in area_reset_config.json.
"""

import json
import math
import os
import posixpath
import socket
import stat
import sys
import threading
import time
import queue

try:
    import paramiko
except ImportError:
    paramiko = None

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, simpledialog, ttk
except ImportError:  # lets the core logic be tested on machines without Tk
    tk = None

CONNECT_TIMEOUT_SECONDS = 15
CHUNK_TILES = 8  # Build 42: map/<floor(x/8)>/<floor(y/8)>.bin
MAX_CHUNKS_WITHOUT_EXTRA_CONFIRM = 400
# When built as an .exe, save settings next to the .exe (not in PyInstaller's temp folder).
_APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "area_reset_config.json")


# --------------------------------------------------------------------------
# core logic (works with any paramiko-like SFTP client; unit-tested)
# --------------------------------------------------------------------------

def chunks_for_area(x1, y1, x2, y2):
    """All (chunkX, chunkY) pairs covering the tile rectangle (inclusive)."""
    lx, hx = sorted((int(x1), int(x2)))
    ly, hy = sorted((int(y1), int(y2)))
    cx0, cx1 = math.floor(lx / CHUNK_TILES), math.floor(hx / CHUNK_TILES)
    cy0, cy1 = math.floor(ly / CHUNK_TILES), math.floor(hy / CHUNK_TILES)
    return [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]


def chunks_for_areas(areas):
    seen = set()
    for a in areas:
        for c in chunks_for_area(a["x1"], a["y1"], a["x2"], a["y2"]):
            seen.add(c)
    return sorted(seen)


def parse_keep_list(path):
    """AdminZoneReset_ProtectedChunks.txt: one '*chunkX/chunkY.bin' per line."""
    keep = set()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().lstrip("*")
            if not line.endswith(".bin"):
                continue
            parts = line[:-4].split("/")
            if len(parts) != 2:
                continue
            try:
                keep.add((int(parts[0]), int(parts[1])))
            except ValueError:
                pass
    return keep


def split_protected(chunks, keep):
    """-> (chunks safe to reset, chunks skipped because they are protected)"""
    safe = [c for c in chunks if c not in keep]
    skipped = [c for c in chunks if c in keep]
    return safe, skipped


# --------------------------------------------------------------------------
# planner link: reads AdminZoneReset_Shared.txt exactly like the planner does
# --------------------------------------------------------------------------

SAFEHOUSE_BUFFER = 16  # same as the planner


def parse_shared_text(text, fname=""):
    """-> (zones, houses). Same rules as the planner's parseFile()."""
    objs, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            cur = {"name": line[1:-1]}
            objs.append(cur)
            continue
        if cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    house_file = "safehouse" in fname.lower()
    reset_file = "wipe" in fname.lower()
    zones, houses = [], []
    for o in objs:
        try:
            x1, y1, x2, y2 = (int(float(o[k])) for k in ("x1", "y1", "x2", "y2"))
        except (KeyError, ValueError):
            continue
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        if house_file or o.get("type") == "safehouse":
            houses.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
            continue
        ztype = o.get("type")
        if ztype not in ("reset", "protected"):
            ztype = "reset" if reset_file else "protected"
        try:
            updated = int(float(o.get("updated", 0)))
        except ValueError:
            updated = 0
        zid = o.get("id") or f"legacy-{ztype}-{o['name']}-{x1}-{y1}-{x2}-{y2}"
        zones.append({
            "id": zid, "name": o["name"], "type": ztype, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "group": o.get("group", ""), "author": o.get("author", "unknown"),
            "updated": updated, "deleted": o.get("deleted") in ("1", 1),
        })
    return zones, houses


def merge_shared(texts):
    """texts: list of (filename, text). -> (zones by id, houses). Newest 'updated' wins."""
    zones, houses = {}, []
    for fname, text in texts:
        zs, hs = parse_shared_text(text, fname)
        for z in zs:
            old = zones.get(z["id"])
            if old is None or z["updated"] > old["updated"]:
                zones[z["id"]] = z
        if hs:
            houses = hs
    return zones, houses


def protected_from_planner(zones, houses):
    """Every chunk covered by a live protected zone or a safehouse (+16 tile buffer)."""
    prot = set()
    for z in zones.values():
        if z["type"] == "protected" and not z["deleted"]:
            prot.update(chunks_for_area(z["x1"], z["y1"], z["x2"], z["y2"]))
    for h in houses:
        b = SAFEHOUSE_BUFFER
        prot.update(chunks_for_area(h["x1"] - b, h["y1"] - b, h["x2"] + b, h["y2"] + b))
    return prot


def live_reset_zones(zones):
    out = [z for z in zones.values() if z["type"] == "reset" and not z["deleted"]]
    return sorted(out, key=lambda z: (z["group"].lower(), z["name"].lower()))


def trash_root_for(map_dir):
    return map_dir.rstrip("/") + "_reset_trash"


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def ensure_remote_dir(sftp, path):
    if remote_exists(sftp, path):
        return
    parent = posixpath.dirname(path.rstrip("/"))
    if parent and parent != path:
        ensure_remote_dir(sftp, parent)
    sftp.mkdir(path)


def _rename(sftp, src, dest):
    if hasattr(sftp, "posix_rename"):
        sftp.posix_rename(src, dest)
    else:
        sftp.rename(src, dest)


def existing_chunk_files(sftp, map_dir, chunks):
    base = map_dir.rstrip("/")
    return [(cx, cy) for cx, cy in chunks if remote_exists(sftp, f"{base}/{cx}/{cy}.bin")]


def do_preview(sftp, map_dir, chunks, log):
    found = existing_chunk_files(sftp, map_dir, chunks)
    log(f"{len(chunks)} chunk(s) cover the selected area(s); "
        f"{len(found)} of them have a saved file that would be moved.")
    log(f"{len(chunks) - len(found)} have no saved file (already default - nothing to do).")
    return found


def do_trash(sftp, map_dir, chunks, stamp, area_names, log):
    """Move each existing chunk file into <map>_reset_trash/<stamp>/<x>/<y>.bin."""
    base = map_dir.rstrip("/")
    if not remote_exists(sftp, base):
        log(f"{base} not found - check the 'Remote map/ path'. Nothing was touched.")
        return 0
    trash_dir = f"{trash_root_for(map_dir)}/{stamp}"
    found = existing_chunk_files(sftp, map_dir, chunks)
    if not found:
        log("No saved chunk files in those areas - they are already default. Nothing moved.")
        return 0
    ensure_remote_dir(sftp, trash_dir)
    moved = []
    for i, (cx, cy) in enumerate(found, start=1):
        src = f"{base}/{cx}/{cy}.bin"
        dest_dir = f"{trash_dir}/{cx}"
        dest = f"{dest_dir}/{cy}.bin"
        ensure_remote_dir(sftp, dest_dir)
        try:
            _rename(sftp, src, dest)
            moved.append((cx, cy))
        except Exception as e:  # keep going, report at the end
            log(f"  [failed] {src}: {e}")
        if i % 25 == 0 or i == len(found):
            log(f"  ... {i}/{len(found)} processed")
    manifest = "\n".join(
        ["# area reset " + stamp, "# areas: " + ", ".join(area_names)] + [f"{cx}/{cy}.bin" for cx, cy in moved]
    ) + "\n"
    with sftp.open(f"{trash_dir}/manifest.txt", "w") as f:
        f.write(manifest)
    log(f"Moved {len(moved)} chunk file(s) to {trash_dir}")
    log("Start the server: those areas will regenerate as default when the chunks load.")
    log("The old files are kept in the trash folder until you use 'Empty trash'.")
    return len(moved)


def _list_stamps(sftp, trash_root):
    try:
        names = sftp.listdir(trash_root)
    except IOError:
        return []
    return sorted(n for n in names if not n.endswith("_undone"))


def do_undo(sftp, map_dir, log):
    trash_root = trash_root_for(map_dir)
    stamps = _list_stamps(sftp, trash_root)
    if not stamps:
        log("Trash is empty - nothing to undo.")
        return 0
    stamp = stamps[-1]
    trash_dir = f"{trash_root}/{stamp}"
    base = map_dir.rstrip("/")
    try:
        with sftp.open(f"{trash_dir}/manifest.txt", "r") as f:
            raw = f.read()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    except IOError:
        log(f"{trash_dir} has no manifest.txt - cannot undo safely.")
        return 0
    restored = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cx_s, cy_file = line.split("/", 1)
        src = f"{trash_dir}/{cx_s}/{cy_file}"
        dest = f"{base}/{cx_s}/{cy_file}"
        if not remote_exists(sftp, src):
            continue
        if remote_exists(sftp, dest):
            skipped += 1
            log(f"  [skip] {cx_s}/{cy_file}: the server already made a new chunk here")
            continue
        ensure_remote_dir(sftp, f"{base}/{cx_s}")
        _rename(sftp, src, dest)
        restored += 1
    if skipped == 0:
        _rename(sftp, trash_dir, f"{trash_root}/{stamp}_undone")
    log(f"Restored {restored} chunk file(s) from {stamp}; skipped {skipped}.")
    if skipped:
        log("Skipped ones are left in the trash folder. Stop the server and try again if you want them back.")
    return restored


def _remove_tree(sftp, path, log):
    for attr in sftp.listdir_attr(path):
        child = f"{path}/{attr.filename}"
        if stat.S_ISDIR(attr.st_mode):
            _remove_tree(sftp, child, log)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def do_empty_trash(sftp, map_dir, log):
    trash_root = trash_root_for(map_dir)
    if not remote_exists(sftp, trash_root):
        log("No trash folder - nothing to delete.")
        return
    for name in sftp.listdir(trash_root):
        _remove_tree(sftp, f"{trash_root}/{name}", log)
        log(f"  deleted {name}")
    log("Trash emptied. Those old chunk files are gone for good.")


def open_sftp(host, port, user, password, log):
    log(f"Connecting to {host}:{port} as {user} ...")
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    except (socket.timeout, OSError) as e:
        raise RuntimeError(
            f"Could not reach {host}:{port} within {CONNECT_TIMEOUT_SECONDS}s ({e}). "
            "Check host and port (many panels use a dedicated SFTP port such as 2022).")
    transport = paramiko.Transport(sock)
    try:
        transport.banner_timeout = CONNECT_TIMEOUT_SECONDS
        transport.auth_timeout = CONNECT_TIMEOUT_SECONDS
        transport.connect(username=user, password=password)
    except Exception:
        transport.close()
        raise
    return transport, paramiko.SFTPClient.from_transport(transport)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("Area Reset - move chunk files to trash")
        root.geometry("820x900")
        self.log_queue = queue.Queue()
        self.worker_running = False
        cfg = load_config()
        self.shared_files = [f for f in cfg.get("shared_files", []) if isinstance(f, str)]
        self.zones, self.houses = {}, []
        self.prot_planner = set()
        self.shown = []  # reset zones currently listed (same order as the listbox)

        pad = {"padx": 6, "pady": 3}

        ttk.Label(
            root, foreground="#a00",
            text="Stop the server before 'Reset'. A running server rewrites loaded chunks and undoes the reset.",
        ).pack(anchor="w", **pad)

        conn = ttk.LabelFrame(root, text="1. Server connection (password is never saved)")
        conn.pack(fill="x", **pad)
        self.host_var = tk.StringVar(value=cfg.get("host", ""))
        self.port_var = tk.StringVar(value=str(cfg.get("port", "22")))
        self.user_var = tk.StringVar(value=cfg.get("user", ""))
        self.pass_var = tk.StringVar()
        self.map_var = tk.StringVar(value=cfg.get("map_dir", ""))
        self.keep_var = tk.StringVar(value=cfg.get("keep_list", ""))
        self.no_keep_var = tk.BooleanVar(value=False)
        for i, (label, var, secret) in enumerate([
            ("Host:", self.host_var, False), ("Port:", self.port_var, False),
            ("Username:", self.user_var, False), ("Password:", self.pass_var, True),
            ("Remote map/ path:", self.map_var, False),
        ]):
            ttk.Label(conn, text=label).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Entry(conn, textvariable=var, width=60, show="*" if secret else "").grid(
                row=i, column=1, sticky="we", padx=4, pady=2)
        conn.columnconfigure(1, weight=1)

        zf = ttk.LabelFrame(root, text="2. Zones from the Zone Reset Planner (tick shops by name - no coordinates)")
        zf.pack(fill="both", **pad)
        top = ttk.Frame(zf)
        top.pack(fill="x", padx=4, pady=3)
        ttk.Button(top, text="Add zone file(s)...", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Reload files", command=self.reload_files).pack(side="left", padx=4)
        ttk.Button(top, text="Clear files", command=self.clear_files).pack(side="left")
        ttk.Label(top, text="  Group:").pack(side="left")
        self.group_var = tk.StringVar(value="All")
        self.group_box = ttk.Combobox(top, textvariable=self.group_var, state="readonly", width=18, values=["All"])
        self.group_box.pack(side="left", padx=3)
        self.group_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        ttk.Button(top, text="Select all shown", command=lambda: self.listbox.select_set(0, "end")).pack(side="left", padx=4)
        ttk.Button(top, text="Select none", command=lambda: self.listbox.select_clear(0, "end")).pack(side="left")
        self.summary_var = tk.StringVar(value="No zone files loaded yet. Click 'Add zone file(s)...' and pick your AdminZoneReset_Shared.txt "
                                              "(the one you drop into the planner).")
        ttk.Label(zf, textvariable=self.summary_var, wraplength=700, foreground="#036").pack(anchor="w", padx=4)
        lbf = ttk.Frame(zf)
        lbf.pack(fill="both", expand=True, padx=4, pady=3)
        self.listbox = tk.Listbox(lbf, selectmode="extended", height=9, exportselection=False)
        sb = ttk.Scrollbar(lbf, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Label(zf, text="Click one or more red reset zones above (Ctrl/Shift for several). "
                           "Protected zones and safehouses (+16 tile buffer) are protected automatically.").pack(anchor="w", padx=4)

        prot = ttk.LabelFrame(root, text="Extra protected list (optional - the planner's 'Keep list' file)")
        prot.pack(fill="x", padx=6, pady=3)
        ttk.Entry(prot, textvariable=self.keep_var, width=62).grid(row=0, column=0, padx=4, pady=3, sticky="we")
        ttk.Button(prot, text="Browse...", command=self.browse_keep).grid(row=0, column=1, padx=4)
        ttk.Checkbutton(
            prot, variable=self.no_keep_var,
            text="I have NO protected zones or safehouses loaded (only tick if no bases are in these areas)",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
        prot.columnconfigure(0, weight=1)

        act = ttk.LabelFrame(root, text="3. Actions")
        act.pack(fill="x", **pad)
        self.buttons = []
        for text, cmd in [
            ("Preview (changes nothing)", self.on_preview),
            ("Reset selected areas (move to trash)", self.on_reset),
            ("Empty trash (permanent)", self.on_empty),
        ]:
            b = ttk.Button(act, text=text, command=cmd)
            b.pack(side="left", padx=4, pady=4)
            self.buttons.append(b)

        ttk.Label(root, text="Log:").pack(anchor="w", padx=6)
        self.log_widget = scrolledtext.ScrolledText(root, height=14, state="disabled")
        self.log_widget.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        if paramiko is None:
            self.log("This tool needs 'paramiko'. Close this window and run:  pip install paramiko")
            for b in self.buttons:
                b.state(["disabled"])
        self.root.after(100, self._drain)
        self.reload_files()

    # ---- helpers
    def log(self, msg=""):
        self.log_queue.put(msg)

    def _drain(self):
        try:
            while True:
                m = self.log_queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", m + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _save(self):
        try:
            port = int(self.port_var.get().strip() or "22")
        except ValueError:
            port = 22
        save_config({
            "host": self.host_var.get().strip(), "port": port,
            "user": self.user_var.get().strip(), "map_dir": self.map_var.get().strip(),
            "shared_files": self.shared_files, "keep_list": self.keep_var.get().strip(),
        })

    def browse_keep(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select AdminZoneReset_ProtectedChunks.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.keep_var.set(path)
            self._save()

    # ---- planner zone files
    def add_files(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="Select AdminZoneReset_Shared.txt (you can pick several admins' files)",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        for pth in paths:
            if pth not in self.shared_files:
                self.shared_files.append(pth)
        if paths:
            self._save()
            self.reload_files()

    def clear_files(self):
        self.shared_files = []
        self._save()
        self.reload_files()

    def reload_files(self):
        """Re-read every zone file from disk (so it always matches the planner's files)."""
        texts, missing = [], []
        for pth in self.shared_files:
            try:
                with open(pth, "r", encoding="utf-8", errors="replace") as f:
                    texts.append((os.path.basename(pth), f.read()))
            except OSError:
                missing.append(pth)
        for pth in missing:
            self.log(f"Could not read {pth} - skipped.")
        self.zones, self.houses = merge_shared(texts)
        self.prot_planner = protected_from_planner(self.zones, self.houses)
        resets = live_reset_zones(self.zones)
        n_prot = sum(1 for z in self.zones.values() if z["type"] == "protected" and not z["deleted"])
        groups = sorted({z["group"] for z in resets if z["group"]}, key=str.lower)
        self.group_box.configure(values=["All"] + groups)
        if self.group_var.get() not in ["All"] + groups:
            self.group_var.set("All")
        if self.shared_files:
            self.summary_var.set(
                f"Loaded {len(self.shared_files)} file(s): {len(resets)} reset zone(s), {n_prot} protected zone(s), "
                f"{len(self.houses)} safehouse(s)  =>  {len(self.prot_planner)} protected chunk(s) that can never be reset.")
        else:
            self.summary_var.set("No zone files loaded yet. Click 'Add zone file(s)...' and pick your AdminZoneReset_Shared.txt "
                                 "(the one you drop into the planner).")
        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, "end")
        grp = self.group_var.get()
        self.shown = [z for z in live_reset_zones(self.zones) if grp == "All" or z["group"] == grp]
        for z in self.shown:
            n = len(chunks_for_area(z["x1"], z["y1"], z["x2"], z["y2"]))
            tag = f'[{z["group"]}] ' if z["group"] else ""
            self.listbox.insert("end", f'{tag}{z["name"]}   by {z["author"]}   {n} chunk(s)')

    def _all_protected(self):
        """-> set of protected chunks, or None if blocked."""
        prot = set(self.prot_planner)
        keep_path = self.keep_var.get().strip()
        if keep_path:
            if not os.path.isfile(keep_path):
                messagebox.showerror("Protected list", "That extra protected list file doesn't exist.")
                return None
            keep = parse_keep_list(keep_path)
            if not keep:
                messagebox.showerror("Protected list", "The extra protected list is empty or unreadable. Refusing to continue.")
                return None
            prot |= keep
        if not prot and not self.no_keep_var.get():
            messagebox.showerror(
                "Nothing is protected",
                "No protected zones or safehouses are loaded, so safehouses could be reset.\n\n"
                "Load your AdminZoneReset_Shared.txt (with the protected zones and safehouses in it), "
                "or tick 'I have NO protected zones or safehouses loaded' if you are sure.")
            return None
        if not prot:
            self.log("WARNING: nothing is protected.")
        return prot

    def _plan(self, areas):
        """-> (chunks to reset, protected chunks skipped) or None if blocked."""
        prot = self._all_protected()
        if prot is None:
            return None
        chunks = chunks_for_areas(areas)
        safe, skipped = split_protected(chunks, prot)
        self.log(f"{len(prot)} protected chunk(s) in total. {len(skipped)} chunk(s) in your selection are protected and will be left alone.")
        return safe, skipped

    def _selected_areas(self):
        sel = [self.shown[i] for i in self.listbox.curselection()]
        if not sel:
            messagebox.showinfo("Pick a zone", "Click at least one reset zone in the list first.")
        return sel

    def _conn(self):
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        map_dir = self.map_var.get().strip()
        try:
            port = int(self.port_var.get().strip() or "22")
        except ValueError:
            messagebox.showerror("Port", "Port must be a number.")
            return None
        if not host or not user or not map_dir:
            messagebox.showerror("Missing info", "Host, username and remote map/ path are required.")
            return None
        self._save()
        return host, port, user, self.pass_var.get(), map_dir

    def _run(self, work):
        if self.worker_running:
            messagebox.showinfo("Busy", "Wait for the current action to finish.")
            return
        self.worker_running = True
        for b in self.buttons:
            b.state(["disabled"])

        def wrapper():
            try:
                work()
            except Exception as e:
                self.log(f"\n[error] {e}")
            finally:
                self.worker_running = False
                self.root.after(0, lambda: [b.state(["!disabled"]) for b in self.buttons])

        threading.Thread(target=wrapper, daemon=True).start()

    def _with_sftp(self, conn, fn):
        host, port, user, password, map_dir = conn
        transport, sftp = open_sftp(host, port, user, password, self.log)
        try:
            fn(sftp, map_dir)
        finally:
            transport.close()

    # ---- actions
    def on_preview(self):
        areas = self._selected_areas()
        conn = self._conn() if areas else None
        if not conn:
            return
        plan = self._plan(areas)
        if plan is None:
            return
        chunks, _skipped = plan
        self.log(f"Preview for: {', '.join(a['name'] for a in areas)}")
        self._run(lambda: self._with_sftp(conn, lambda s, m: do_preview(s, m, chunks, self.log)))

    def on_reset(self):
        areas = self._selected_areas()
        conn = self._conn() if areas else None
        if not conn:
            return
        plan = self._plan(areas)
        if plan is None:
            return
        chunks, skipped_protected = plan
        if not chunks:
            self.log("Everything in those areas is protected - nothing to reset.")
            return
        names = [a["name"] for a in areas]
        extra = ""
        if skipped_protected:
            extra += f"\n{len(skipped_protected)} protected chunk(s) will be LEFT ALONE.\n"
        if len(chunks) > MAX_CHUNKS_WITHOUT_EXTRA_CONFIRM:
            extra += f"\nThat is a LOT of chunks ({len(chunks)}). Double-check your coordinates!\n"
        ans = simpledialog.askstring(
            "Confirm reset",
            f"Reset: {', '.join(names)}\n"
            f"Covers {len(chunks)} chunk(s). Their saved files will be MOVED to the trash folder.\n"
            f"{extra}\nThe SERVER MUST BE STOPPED. Type RESET to continue:")
        if ans != "RESET":
            self.log("Cancelled - nothing was touched.")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self._run(lambda: self._with_sftp(conn, lambda s, m: do_trash(s, m, chunks, stamp, names, self.log)))

    def on_empty(self):
        conn = self._conn()
        if not conn:
            return
        ans = simpledialog.askstring(
            "Empty trash",
            "This PERMANENTLY deletes every old chunk file in the reset trash folder.\n"
            "The old files cannot be recovered afterwards.\n\nType DELETE to continue:")
        if ans != "DELETE":
            self.log("Cancelled - nothing was deleted.")
            return
        self._run(lambda: self._with_sftp(conn, lambda s, m: do_empty_trash(s, m, self.log)))


def main():
    if tk is None:
        print("Tkinter is not available in this Python install.")
        sys.exit(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
