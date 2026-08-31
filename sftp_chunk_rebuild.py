#!/usr/bin/env python3
"""
sftp_chunk_rebuild.py

The "build a clean folder, then swap names" approach - the same trick you
already use for zpop, applied to map chunks. Instead of figuring out
everything that exists and working out what to discard (which means
listing and touching a potentially huge number of files), this only ever
touches the small, bounded side: it copies just the PROTECTED chunks
(everything AdminZoneReset's "Generate protected chunk list" wrote to
AdminZoneReset_ProtectedChunks.txt - every safehouse, buffered, plus any
manual zones) into a brand-new map_new/ folder on the server, leaving the
live map/ folder completely untouched while that happens. Only at the very
end, once you've stopped the server, do you swap the two folder names -
a pure rename, done in an instant regardless of map size.

Two modes:

  --mode build   Copies every protected chunk from map/ into map_new/.
                 Safe to run with the server still LIVE - it never writes
                 to or deletes from the real map/ folder, only reads from
                 it and writes into the new one. Re-run this as often as
                 you like; it just re-copies the current protected chunks.

  --mode swap    STOP THE SERVER FIRST. Renames the live map/ folder to a
                 timestamped backup name, then renames map_new/ to map/.
                 This is the only part that touches the live folder, and
                 it's two renames, not a file-by-file operation - fast
                 regardless of how big the map is. The old folder is kept
                 (not deleted) so a swap is always undoable by renaming it
                 back yourself later, until you're confident and delete it
                 through your normal SFTP client to reclaim disk space.

Why chunk copies aren't free the way renames are: SFTP's rename is a pure
server-side path change, but a copy has to actually move bytes - there's no
guaranteed "copy this file to a new name" operation in plain SFTP, so this
script reads each protected chunk's bytes and writes them to the new
location. That's still fine here because the protected set is meant to be
small (a handful of bases plus a margin), not the whole map.

Setup (one-time):
    pip install paramiko

Usage - build the clean folder (server can stay running):
    python sftp_chunk_rebuild.py --mode build --host <ip> --username <user> ^
        --remote-map-dir "Saves/Multiplayer/servertest/map" ^
        --keep-list "C:\\Users\\Burst Banana\\Zomboid\\Lua\\AdminZoneReset_ProtectedChunks.txt"

Then, once the server is STOPPED, swap the folders in:
    python sftp_chunk_rebuild.py --mode swap --host <ip> --username <user> ^
        --remote-map-dir "Saves/Multiplayer/servertest/map"

Auth: omit --password/--key entirely and it'll prompt for a password
securely (never put a password on the command line). Pass --key
path\\to\\private_key instead if you log in with a key.

--remote-map-dir is the path to the map/ folder AS YOUR SFTP SESSION SEES
IT - browse to it once with your normal SFTP client to confirm the exact
path before running this, since hosts vary in whether they chroot the
SFTP account to a different root than the real filesystem path.
"""

import argparse
import getpass
import os
import sys
import time

try:
    import paramiko
except ImportError:
    print("This script needs the 'paramiko' package. Install it with:\n    pip install paramiko")
    sys.exit(1)

# Build 42: map/<floor(x/8)>/<floor(y/8)>.bin - confirmed against the actual
# source of a maintained, currently-working B42 chunk-cleaning tool (older
# B41-era guides say 10x10 tiles per chunk; that's stale for B42). Not
# actually used for math in this script (the keep-list already comes
# pre-computed in chunk coordinates from AdminZoneReset), kept here only as
# documentation of what a "chunk" means throughout this tool.
CHUNK_TILES = 8


def parse_keep_list(path):
    """AdminZoneReset_ProtectedChunks.txt: one '*chunkX/chunkY.bin' per line
    (matching the real map/<X>/<Y>.bin relative path, with a leading "*" so
    the same lines can be pasted straight into an SFTP client's "select
    files matching" box), already buffered and computed in-game by
    AdminZoneReset's own "Generate protected chunk list" menu option."""
    keep = set()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("*"):
                line = line[1:]
            if not line.endswith(".bin"):
                print(f"  [skip] malformed line in keep-list: {line!r}")
                continue
            parts = line[:-4].split("/")
            if len(parts) != 2:
                print(f"  [skip] malformed line in keep-list: {line!r}")
                continue
            try:
                keep.add((int(parts[0]), int(parts[1])))
            except ValueError:
                print(f"  [skip] malformed line in keep-list: {line!r}")
    return keep


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
    """Stream src's bytes straight into dest without ever touching local
    disk - both ends are remote SFTPFile handles, so this is a
    server-to-local-script-to-server relay, not a full download-then-upload
    round trip through a saved file."""
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


def do_build(sftp, remote_map_dir, keep, new_map_dir):
    if remote_exists(sftp, new_map_dir):
        print(f"\n{new_map_dir} already exists on the server.")
        print("Remove or rename it yourself via SFTP first, so this never overwrites a")
        print("previous build you might still need - then re-run this.")
        sys.exit(1)

    ensure_remote_dir(sftp, new_map_dir)

    copied = 0
    skipped = 0
    total_bytes = 0
    for (cx, cy) in sorted(keep):
        src = f"{remote_map_dir.rstrip('/')}/{cx}/{cy}.bin"
        if not remote_exists(sftp, src):
            # Never visited/loaded - there's nothing to copy, and nothing is
            # lost: it'll generate fresh from the base map the first time
            # someone walks there after the swap, same as any reset area.
            skipped += 1
            continue
        dest_dir = f"{new_map_dir.rstrip('/')}/{cx}"
        dest = f"{dest_dir}/{cy}.bin"
        ensure_remote_dir(sftp, dest_dir)
        try:
            total_bytes += copy_remote_file(sftp, src, dest)
            copied += 1
        except Exception as e:
            print(f"  [failed] {src} -> {dest}: {e}")

    print(f"\nCopied {copied} protected chunk(s) ({total_bytes / 1024:.1f} KB) into {new_map_dir}")
    print(f"{skipped} protected coordinate(s) had no existing chunk file yet (nothing to copy - fine)")
    print(f"\n{new_map_dir} is ready. The live {remote_map_dir} folder was NOT touched.")
    print("When you're ready to actually apply this, stop the server and run this")
    print("script again with --mode swap.")


def do_swap(sftp, remote_map_dir, new_map_dir):
    if not remote_exists(sftp, new_map_dir):
        print(f"\n{new_map_dir} doesn't exist - run --mode build first.")
        sys.exit(1)
    if not remote_exists(sftp, remote_map_dir):
        print(f"\n{remote_map_dir} doesn't exist - nothing to swap out. Aborting to be safe.")
        sys.exit(1)

    backup_dir = f"{remote_map_dir.rstrip('/')}_previous_{time.strftime('%Y%m%d_%H%M%S')}"

    print("\nThis will:")
    print(f"  1. Rename {remote_map_dir}  ->  {backup_dir}")
    print(f"  2. Rename {new_map_dir}  ->  {remote_map_dir}")
    print("\nStep 1 keeps your entire previous map fully intact under the backup name -")
    print("nothing is deleted. You can undo this whole swap later by renaming it back")
    print("yourself, until you're confident and remove it via SFTP to reclaim space.")
    print("\nMAKE SURE THE SERVER IS STOPPED before continuing.")
    confirm = input("Type SWAP (all caps) to proceed, anything else to cancel: ")
    if confirm != "SWAP":
        print("Cancelled - nothing was touched.")
        return

    sftp.posix_rename(remote_map_dir, backup_dir) if hasattr(sftp, "posix_rename") else sftp.rename(remote_map_dir, backup_dir)
    sftp.posix_rename(new_map_dir, remote_map_dir) if hasattr(sftp, "posix_rename") else sftp.rename(new_map_dir, remote_map_dir)

    print(f"\nDone. {remote_map_dir} is now the rebuilt, protected-only map.")
    print(f"Your previous map is safe under {backup_dir} until you delete it yourself.")
    print("Start the server back up and confirm everything looks right.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["build", "swap"])
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--username", required=True)
    ap.add_argument("--key", help="path to a private key file, if you log in with a key instead of a password")
    ap.add_argument("--remote-map-dir", required=True,
                     help="path to the live map/ folder on the server, as your SFTP session sees it")
    ap.add_argument("--new-map-dir", default=None,
                     help="defaults to a 'map_new' folder next to --remote-map-dir")
    ap.add_argument("--keep-list", default=None,
                     help="local path to AdminZoneReset_ProtectedChunks.txt (required for --mode build)")
    args = ap.parse_args()

    new_map_dir = args.new_map_dir or (args.remote_map_dir.rstrip("/") + "_new")

    keep = None
    if args.mode == "build":
        if not args.keep_list:
            print("--mode build requires --keep-list")
            sys.exit(1)
        if not os.path.isfile(args.keep_list):
            print(f"Keep-list not found: {args.keep_list}")
            print('Run "Generate protected chunk list" from AdminZoneReset\'s right-click menu in-game first.')
            sys.exit(1)
        print(f"Reading keep-list: {args.keep_list}")
        keep = parse_keep_list(args.keep_list)
        print(f"  {len(keep)} protected chunk(s) loaded")
        if len(keep) == 0:
            print("\nThe keep-list is EMPTY - refusing to build a map_new/ that would protect nothing.")
            sys.exit(1)

    pkey = None
    password = None
    if args.key:
        pkey = paramiko.RSAKey.from_private_key_file(args.key)
    else:
        password = getpass.getpass(f"Password for {args.username}@{args.host}: ")

    print(f"\nConnecting to {args.host}:{args.port} as {args.username} ...")
    transport = paramiko.Transport((args.host, args.port))
    try:
        transport.connect(username=args.username, password=password, pkey=pkey)
        sftp = paramiko.SFTPClient.from_transport(transport)

        if args.mode == "build":
            do_build(sftp, args.remote_map_dir, keep, new_map_dir)
        else:
            do_swap(sftp, args.remote_map_dir, new_map_dir)
    finally:
        transport.close()


if __name__ == "__main__":
    main()
