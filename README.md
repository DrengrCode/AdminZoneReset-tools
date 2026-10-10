# AdminZoneReset chunk tools

Companion tools for the **AdminZoneReset** Project Zomboid (Build 42) mod.
AdminZoneReset lets an admin mark protected zones in-game and export a
"keep list" of every map chunk that should survive a reset (every marked
zone, plus every registered safehouse with a safety buffer). These tools
take that list and actually rebuild your server's map over SFTP, keeping
only what's protected and letting everything else regenerate completely
fresh - broken locks, looted stores, and even sledgehammered/torched-open
buildings all come back exactly as they were on day one, because this
works by deleting the *save data* for those areas and letting the game
regenerate them from the original map, not by trying to repair objects
one at a time.

**Nothing is ever deleted outright.** The design is build-then-swap: copy
the chunks you want to keep into a new folder while your server stays
live, then - only once you've stopped the server - swap that folder in
for the old one with two renames. The old map is kept under a timestamped
name until you delete it yourself.

## Which tool to use

- **`admin_zone_reset_gui.py`** - a small desktop window: fill in your
  server details, browse to your protected-chunk list, click Build, then
  (once the server's stopped) click Swap. Recommended for most people.
- **`sftp_chunk_rebuild.py`** - the same logic as a command-line tool, if
  you'd rather script it or run it headless.

- **`area_reset_gui.py`** - reset just *some areas* (shops, a town, spawn
  zones) without a full wipe. It reads the same `AdminZoneReset_Shared.txt`
  zone file the Zone Reset Planner reads, so you tick zones by name instead
  of typing coordinates. It works out the protected chunks itself (protected
  zones, plus safehouses with a 16-tile buffer) and never touches them. Reset
  *moves* the chunk files into a `map_reset_trash/` folder next to `map/`, so
  the server regenerates those areas from the original map; "Empty trash"
  deletes them for good. Build a double-click app with `build_exe.bat`
  (produces `dist\AreaReset.exe`, settings saved next to it; the password is
  never saved).

  Use it only when the server is stopped, or when nobody is near the zones: a
  running server writes loaded chunks back and silently undoes the reset.
  Back up your `map/` folder before the first real run.

The two tools above need your keep-list file, which the AdminZoneReset mod writes for
you in-game via its right-click menu's "Generate protected chunk list"
option, saved locally to your `Zomboid/Lua/AdminZoneReset_ProtectedChunks.txt`.

## Setup

Requires Python 3.8+ and the `paramiko` package:

```
pip install paramiko
```

## Usage

### GUI

```
python admin_zone_reset_gui.py
```

**Before you click Build: force a save.** Build copies whatever's
currently written to disk in each protected chunk's `.bin` file - not
the live in-memory game state. A dedicated server only writes those
files out on its own autosave cycle, so anything built or changed
recently (inside a protected zone or not) can still be missing from the
copy if no autosave has happened since. Confirmed the hard way in
testing: a fence built right next to a safehouse, well inside its
protection buffer, still didn't survive a reset because nothing had
forced a save between building it and running Build. Type `/save` in
admin chat immediately before clicking Build to force an up-to-date
snapshot - it's a normal, harmless server command, not something
specific to this tool.

Fill in your host, port, username, password, and the path to your
server's `map/` folder *as your SFTP client sees it* (browse to it once
with your normal SFTP client to confirm the exact path first - hosts
vary in whether the account is chrooted to a different root than the
real filesystem path). Browse to your protected-chunk list file, click
**Build**. That step is safe to run with the server still live - it only
ever reads from the existing map and writes into a brand-new folder next
to it.

Once you're happy with the result and have **stopped the server**, click
**Swap**. You'll be asked to type `SWAP` to confirm before anything
touches the live folder - that confirmation is intentional, not a bug to
route around.

### Command line

```
python sftp_chunk_rebuild.py --mode build --host <ip> --username <user> \
    --remote-map-dir "Saves/Multiplayer/<servername>/map" \
    --keep-list "path/to/AdminZoneReset_ProtectedChunks.txt"

python sftp_chunk_rebuild.py --mode swap --host <ip> --username <user> \
    --remote-map-dir "Saves/Multiplayer/<servername>/map"
```

Omit `--password` and you'll be prompted securely - never pass a password
as a command-line argument. Pass `--key path/to/private_key` instead if
you log in with a key.

## Safety notes

- **Build** never writes to or deletes from your live `map/` folder -
  only reads from it.
- **Swap** is the only step that touches the live folder, and it's two
  renames, not a file-by-file operation, so it's fast regardless of map
  size - and your previous map is kept, not deleted, until you remove it
  yourself.
- Chunk files are all-or-nothing (Build 42's `map/<X>/<Y>.bin`, 8x8 tiles
  per chunk) - AdminZoneReset already pads safehouse bounds by a buffer
  before exporting the keep-list specifically so a base near a chunk
  boundary doesn't lose an edge chunk.
- Your password/key is used only to open the SFTP connection for that one
  run - it is never written to disk, logged, or sent anywhere else.

## What this can't do

Nothing here can selectively repair specific broken objects while leaving
the rest of a live chunk alone - deleting a chunk's save data is an
all-or-nothing operation, which is exactly why AdminZoneReset lets you
mark precise protected zones first. There is currently no supported way
to do a true "regenerate from the original map" reset entirely in-game,
for any mod - Project Zomboid's Lua API doesn't expose file access to the
save folder or any engine hook to discard a live chunk's save and reload
it fresh, which is why this has to run as an external tool against the
save files directly, the same as every other chunk-cleaning tool in the
community.
