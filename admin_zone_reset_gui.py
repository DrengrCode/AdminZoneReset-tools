#!/usr/bin/env python3
"""
admin_zone_reset_gui.py

A small desktop wrapper around the exact same build/swap logic as
sftp_chunk_rebuild.py - same safety guarantees, same two-step design (build
never touches the live map, swap is the only step that does, and it's a
pure rename either way) - just with fields and buttons instead of
command-line flags and a WinSCP mask editor.

What it does, in order:
  1. "Browse..." to pick your local AdminZoneReset_ProtectedChunks.txt
     (generated in-game via AdminZoneReset's "Generate protected chunk
     list" menu option).
  2. Fill in your server's connection details and the remote map/ path
     (get this from your SFTP client - hosts vary in whether the path
     your SFTP session sees matches the real filesystem path).
  3. Click "Build" - copies every protected chunk from map/ into a new
     map_new/ folder next to it. Safe to run with the server still LIVE;
     this step never writes to or deletes from the real map/ folder.
  4. Only once you've stopped the server: click "Swap" - renames map/ to
     a timestamped backup and map_new/ to map/. Two renames, not a
     file-by-file copy, so it's fast regardless of map size. You'll be
     asked to type SWAP to confirm before anything happens, same as the
     command-line version - that confirmation is a deliberate safety
     step, not something this GUI skips.

Nothing is ever deleted. The old map is kept under a timestamped name
after a swap until you remove it yourself via your normal SFTP client.

Setup (one-time):
    pip install paramiko

Run:
    python admin_zone_reset_gui.py

Your password is entered in this window's own masked field and used only
to open the SFTP connection for that one action - it is never written to
disk, logged, or sent anywhere except to the server you typed in.
"""

import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import paramiko
except ImportError:
    paramiko = None

# How long to wait for the TCP connection and SSH handshake before giving up.
# Without an explicit timeout, paramiko/socket will block INDEFINITELY if a
# host/port is unreachable (firewall silently dropping packets, wrong port,
# etc) rather than failing fast - confirmed the hard way: a real run sat at
# 0% CPU / 0 Mbps with no way to tell "still working" from "stuck forever".
CONNECT_TIMEOUT_SECONDS = 15


def open_sftp(host, port, user, password, log):
    """Opens an SFTP session with a bounded connect timeout, and a clear,
    actionable error instead of a raw traceback (or an infinite hang) if the
    host/port is wrong or unreachable. Many hosting panels expose SFTP on a
    dedicated port that's different from the port their web file manager
    runs on (2022 is common) - worth checking your host's panel for a
    separate "SFTP"/"File Access" section if this times out."""
    log(f"Connecting to {host}:{port} as {user} ...")
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    except (socket.timeout, OSError) as e:
        raise RuntimeError(
            f"Could not reach {host}:{port} within {CONNECT_TIMEOUT_SECONDS}s ({e}). "
            "Double-check the host and port - many hosting panels use a dedicated "
            "SFTP port (2022 is common) that's different from the port their web "
            "file manager uses."
        )
    transport = paramiko.Transport(sock)
    try:
        transport.banner_timeout = CONNECT_TIMEOUT_SECONDS
        transport.auth_timeout = CONNECT_TIMEOUT_SECONDS
        transport.connect(username=user, password=password)
    except Exception:
        transport.close()
        raise
    return transport, paramiko.SFTPClient.from_transport(transport)

# Build 42: map/<floor(x/8)>/<floor(y/8)>.bin - see sftp_chunk_rebuild.py
# for the full explanation. Not used for math here (the keep-list already
# comes pre-computed in chunk coordinates from AdminZoneReset).
CHUNK_TILES = 8


def parse_keep_list(path):
    """AdminZoneReset_ProtectedChunks.txt: one '*chunkX/chunkY.bin' per
    line (an optional leading "*", matching the real map/<X>/<Y>.bin
    relative path)."""
    keep = set()
    skipped = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("*"):
                line = line[1:]
            if not line.endswith(".bin"):
                skipped.append(line)
                continue
            parts = line[:-4].split("/")
            if len(parts) != 2:
                skipped.append(line)
                continue
            try:
                keep.add((int(parts[0]), int(parts[1])))
            except ValueError:
                skipped.append(line)
    return keep, skipped


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def ensure_remote_dir(sftp, path):
    if remote_exists(sftp, path):
        return
    parent = path.rsplit("/", 1)[0]
    if parent and parent != path:
        ensure_remote_dir(sftp, parent)
    sftp.mkdir(path)


def copy_remote_file(sftp, src, dest, bufsize=1024 * 256):
    with sftp.open(src, "rb") as f_in, sftp.open(dest, "wb") as f_out:
        f_out.set_pipelined(True)
        total = 0
        while True:
            chunk = f_in.read(bufsize)
            if not chunk:
                break
            f_out.write(chunk)
            total += len(chunk)
        return total


def do_build(sftp, remote_map_dir, keep, new_map_dir, log):
    if remote_exists(sftp, new_map_dir):
        log(f"{new_map_dir} already exists on the server.")
        log("Remove or rename it yourself via SFTP first (so this never overwrites a")
        log("previous build you might still need), then run Build again.")
        return

    ensure_remote_dir(sftp, new_map_dir)

    # Each chunk costs several separate round-trips to the server (exists
    # check, dir check, open/read/write/close), done one at a time - over a
    # real network this can take minutes for a large keep-list, and with no
    # output in between it's indistinguishable from a hang. Log periodically
    # so "still working" is always visible, not just the final summary.
    total = len(keep)
    progress_every = max(1, total // 20)  # ~20 updates over the whole run

    copied = 0
    skipped = 0
    total_bytes = 0
    for i, (cx, cy) in enumerate(sorted(keep), start=1):
        src = f"{remote_map_dir.rstrip('/')}/{cx}/{cy}.bin"
        if not remote_exists(sftp, src):
            skipped += 1
        else:
            dest_dir = f"{new_map_dir.rstrip('/')}/{cx}"
            dest = f"{dest_dir}/{cy}.bin"
            ensure_remote_dir(sftp, dest_dir)
            try:
                total_bytes += copy_remote_file(sftp, src, dest)
                copied += 1
            except Exception as e:
                log(f"  [failed] {src} -> {dest}: {e}")

        if i % progress_every == 0 or i == total:
            log(f"  ... {i}/{total} coordinates checked ({copied} copied so far)")

    log("")
    log(f"Copied {copied} protected chunk(s) ({total_bytes / 1024:.1f} KB) into {new_map_dir}")
    log(f"{skipped} protected coordinate(s) had no existing chunk file yet (nothing to copy - fine)")
    log("")
    log(f"{new_map_dir} is ready. The live {remote_map_dir} folder was NOT touched.")
    log("When you're ready to actually apply this, stop the server and click Swap.")


def do_swap(sftp, remote_map_dir, new_map_dir, backup_dir, log):
    # Confirmation already happened before this function was ever called
    # (and before any connection was opened) - see on_swap. Previously the
    # confirm prompt ran AFTER connecting, so the SFTP session sat open and
    # completely idle for however long it took a human to read the prompt
    # and type SWAP - long enough, in real testing, for the server to close
    # the idle connection out from under it ("Socket is closed", mid-rename).
    # Asking first and connecting immediately before acting removes that gap
    # entirely - there's no more idle window for anything to time out during.
    if not remote_exists(sftp, new_map_dir):
        log(f"{new_map_dir} doesn't exist - run Build first.")
        return
    if not remote_exists(sftp, remote_map_dir):
        log(f"{remote_map_dir} doesn't exist - nothing to swap out. Aborting to be safe.")
        return

    log("")
    log("Proceeding with the confirmed swap:")
    log(f"  1. Rename {remote_map_dir}  ->  {backup_dir}")
    log(f"  2. Rename {new_map_dir}  ->  {remote_map_dir}")

    if hasattr(sftp, "posix_rename"):
        sftp.posix_rename(remote_map_dir, backup_dir)
        sftp.posix_rename(new_map_dir, remote_map_dir)
    else:
        sftp.rename(remote_map_dir, backup_dir)
        sftp.rename(new_map_dir, remote_map_dir)

    log("")
    log(f"Done. {remote_map_dir} is now the rebuilt, protected-only map.")
    log(f"Your previous map is safe under {backup_dir} until you delete it yourself.")
    log("Start the server back up and confirm everything looks right.")


class App:
    def __init__(self, root):
        self.root = root
        root.title("AdminZoneReset - Chunk Rebuild")
        root.geometry("640x560")

        self.log_queue = queue.Queue()
        self.worker_running = False

        pad = {"padx": 6, "pady": 4}

        frm = ttk.Frame(root)
        frm.pack(fill="x", **pad)

        self.keep_list_var = tk.StringVar()
        ttk.Label(frm, text="Protected chunk list:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.keep_list_var, width=48).grid(row=0, column=1, sticky="we")
        ttk.Button(frm, text="Browse...", command=self.browse_keep_list).grid(row=0, column=2)

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()
        self.map_dir_var = tk.StringVar()
        self.new_map_dir_var = tk.StringVar()

        fields = [
            ("Host:", self.host_var, False),
            ("Port:", self.port_var, False),
            ("Username:", self.user_var, False),
            ("Password:", self.pass_var, True),
            ("Remote map/ path:", self.map_dir_var, False),
            ("New map folder (optional):", self.new_map_dir_var, False),
        ]
        for i, (label, var, secret) in enumerate(fields, start=1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w")
            entry = ttk.Entry(frm, textvariable=var, width=48, show="*" if secret else "")
            entry.grid(row=i, column=1, columnspan=2, sticky="we")

        frm.columnconfigure(1, weight=1)

        btn_frm = ttk.Frame(root)
        btn_frm.pack(fill="x", **pad)
        self.build_btn = ttk.Button(btn_frm, text="Build", command=self.on_build)
        self.build_btn.pack(side="left", padx=4)
        self.swap_btn = ttk.Button(btn_frm, text="Swap (server must be stopped)", command=self.on_swap)
        self.swap_btn.pack(side="left", padx=4)

        ttk.Label(root, text="Log:").pack(anchor="w", padx=6)
        self.log_widget = scrolledtext.ScrolledText(root, height=20, state="disabled")
        self.log_widget.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        if paramiko is None:
            self.log("This tool needs the 'paramiko' package. Close this window and run:")
            self.log("    pip install paramiko")
            self.build_btn.state(["disabled"])
            self.swap_btn.state(["disabled"])

        self.root.after(100, self._drain_log_queue)

    def browse_keep_list(self):
        path = filedialog.askopenfilename(
            title="Select AdminZoneReset_ProtectedChunks.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.keep_list_var.set(path)

    def log(self, message=""):
        self.log_queue.put(message)

    def _drain_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", message + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _validated_common_fields(self):
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        password = self.pass_var.get()
        map_dir = self.map_dir_var.get().strip()
        try:
            port = int(self.port_var.get().strip() or "22")
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number.")
            return None
        if not host or not user or not map_dir:
            messagebox.showerror("Missing info", "Host, username, and remote map/ path are all required.")
            return None
        new_map_dir = self.new_map_dir_var.get().strip() or (map_dir.rstrip("/") + "_new")
        return host, port, user, password, map_dir, new_map_dir

    def _run_worker(self, target):
        if self.worker_running:
            messagebox.showinfo("Busy", "Already running an operation - wait for it to finish.")
            return
        self.worker_running = True
        self.build_btn.state(["disabled"])
        self.swap_btn.state(["disabled"])

        def wrapper():
            try:
                target()
            except Exception as e:
                self.log(f"\n[error] {e}")
            finally:
                self.worker_running = False
                self.root.after(0, lambda: (self.build_btn.state(["!disabled"]), self.swap_btn.state(["!disabled"])))

        threading.Thread(target=wrapper, daemon=True).start()

    def on_build(self):
        common = self._validated_common_fields()
        if not common:
            return
        host, port, user, password, map_dir, new_map_dir = common

        keep_list_path = self.keep_list_var.get().strip()
        if not keep_list_path or not os.path.isfile(keep_list_path):
            messagebox.showerror("Missing keep list", "Pick a valid AdminZoneReset_ProtectedChunks.txt file first.")
            return

        keep, skipped = parse_keep_list(keep_list_path)
        for s in skipped:
            self.log(f"  [skip] malformed line in keep-list: {s!r}")
        self.log(f"Loaded {len(keep)} protected chunk(s) from {keep_list_path}")
        if len(keep) == 0:
            self.log("The keep-list is EMPTY - refusing to build a map_new/ that would protect nothing.")
            return

        def work():
            transport, sftp = open_sftp(host, port, user, password, self.log)
            try:
                do_build(sftp, map_dir, keep, new_map_dir, self.log)
            finally:
                transport.close()

        self._run_worker(work)

    def on_swap(self):
        common = self._validated_common_fields()
        if not common:
            return
        host, port, user, password, map_dir, new_map_dir = common
        backup_dir = f"{map_dir.rstrip('/')}_previous_{time.strftime('%Y%m%d_%H%M%S')}"

        # Confirm BEFORE opening any connection - not after. This used to
        # connect first, print the plan, and only then wait on this same
        # prompt, which left the SFTP session open and completely idle for
        # however long a human takes to read it and type SWAP. In real
        # testing that was long enough for the connection to be closed out
        # from under it mid-rename. Asking here means the connection is
        # opened and used back-to-back with no idle gap in between.
        answer = tk.simpledialog.askstring(
            "Confirm swap",
            f"This will:\n"
            f"  1. Rename {map_dir}  ->  {backup_dir}\n"
            f"  2. Rename {new_map_dir}  ->  {map_dir}\n\n"
            "Step 1 keeps your entire previous map fully intact under the backup name.\n"
            "MAKE SURE THE SERVER IS STOPPED before continuing.\n\n"
            "Type SWAP (all caps) to proceed:",
        )
        if answer != "SWAP":
            self.log("Swap cancelled - nothing was touched.")
            return

        def work():
            transport, sftp = open_sftp(host, port, user, password, self.log)
            try:
                do_swap(sftp, map_dir, new_map_dir, backup_dir, self.log)
            finally:
                transport.close()

        self._run_worker(work)


def main():
    import tkinter.simpledialog  # noqa: F401 (referenced via tk.simpledialog above)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
