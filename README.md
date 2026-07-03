# imessage-exporter-incremental
A wrapper script around [imessage-exporter](https://github.com/ReagentX/imessage-exporter) that turns it into an incremental backup tool. Runs on a schedule or on demand, only exports what's changed since the last run, merges it into a single continuously-updated HTML per conversation, deduplicates the full iMessage history without re-exporting everything from scratch every time.

Created to assist in maintaining an up to date iMessage archive without keeping the messages in iCloud.

I use this in conjunction with [imessage-exporter-viewer](https://github.com/amjadkamali/imessage-exporter-viewer) to make it even easier to browse.

I chose to rsync the iMessage Attachments folder and disable imessage-exporter's HEIC conversion because I preferred the longer UUID for the stronger guarantee of no collisions and I didn't want to maintain a duplicate attachments archive (both the rsync from iMessage + the converted output from imessage-exporter)

It does some cleanup like merging multiple chats when the phone numbers are in the same contact card. Some people might not like this but I wanted to replicate the iMessage UI. Make sure you know what this is doing before applying on your exports.

Heads up, most of this tool was written with Claude, including the rest of this README.

## Why this exists

`imessage-exporter` is excellent at what it does, but it has no concept of "only export what's new." Every run is a full export of your entire message history. For an active phone number with years of history and tens of thousands of attachments, that means every single backup run re-processes everything — slow, and wasteful of disk I/O for data that hasn't changed.

This pipeline solves that by tracking the last successful export's timestamp, asking `imessage-exporter` for only messages since then, and merging the result into the same per-contact HTML files it built last time — so you get one clean, ever-growing conversation file per contact, and back it up as often as you like without the cost of a full re-export.

## How it works

The pipeline runs five stages on every sync:

1. **Snapshot** — a read-only copy of `chat.db` (plus its WAL/SHM sidecars) and an additive sync of attachments, so the export step always works from a stable, consistent copy rather than the live database.
2. **Export** — runs `imessage-exporter -c disabled`, scoped to messages since the last successful snapshot (or your full history on first run, or with `--full`).
3. **Merge** — deduplicates by message GUID and merges the new export into the existing per-contact HTML files in place, so a contact's file keeps growing rather than being replaced. Files that didn't change are left untouched (byte-for-byte comparison skips the write, so their modification time stays put — sync-friendly for tools like rsync or Nextcloud).
4. **Index** — regenerates the conversation list page from the current state of the live archive.
5. **Prune** — by default, erases the working export and archive staging directories, since everything useful has already been merged into the live archive. Snapshots are never pruned; they're the only durable record of what's been backed up.

On the next run, it starts again from wherever the last snapshot left off.

## Requirements

- macOS with Messages in iCloud or a local `chat.db`
- [imessage-exporter](https://github.com/ReagentX/imessage-exporter) installed and on `PATH`
- Python 3 with a virtual environment (for the merge/index scripts)
- Bash

## Quick Start

```bash
# One-time: point the scripts at your setup
export IMESSAGE_ROOT=~/Local/imessage-snapshots
export IMESSAGE_WORKING_DIR="$IMESSAGE_ROOT/WorkingDir"
export IMESSAGE_MY_HANDLES="+15551234567 you@icloud.com"

# First run: bootstrap from a specific start date, or leave it off for full history
./imessage-incremental-sync.sh --since 2024-01-01

# Every run after that: just run it again
./imessage-incremental-sync.sh
```

Point a cron job, launchd agent, or just a recurring reminder at the second command and your archive stays current without any full re-exports.

## Directory Layout

```
ROOT/
  index.html                    <- generated conversation list
  iMessageExports/               <- live, persistent, continuously updated
    Attachments/
    StickerCache/
    +15551234567.html            <- one file per contact/group, grows over time
  WorkingDir/
    imessage-incremental-sync.sh
    imessage-snapshot.sh
    merge_by_contact.py
    merge_html_exports.py
    generate_index.py
    snapshots/                   <- chat.db snapshots only, NEVER pruned
      2026-07-03_081953/
        .complete                <- marks a fully-completed snapshot
    exports/                     <- working export output, erased after merge by default
    archives/                    <- merge staging area, erased after merge by default
```

`iMessageExports/` is the one folder you point anything else (backups, a search tool, Nextcloud sync) at — it's the only directory meant to be read by something else while a sync might be in progress.

## Flags

| Flag | Description |
|---|---|
| `--since YYYY-MM-DD` | Bootstrap start date for the very first run |
| `--full` | Re-export full history instead of incrementally |
| `--dry-run` | Show what would happen without writing anything |
| `--keep-archives N` | Retain N dated backups instead of erasing the whole folder (default: 0) |
| `--keep-exports N` | Retain N export folders instead of erasing the whole folder (default: 0) |
| `--merge-only` | Reuse the most recent export and skip snapshot + export (useful for re-running just the merge/index steps) |

## Environment Variables

| Variable | Description |
|---|---|
| `IMESSAGE_ROOT` | Parent directory containing `iMessageExports/` and `WorkingDir/` |
| `IMESSAGE_WORKING_DIR` | Working directory for scripts, snapshots, and staging |
| `IMESSAGE_VENV_PYTHON` | Path to the Python interpreter to use for the merge/index scripts |
| `IMESSAGE_MY_HANDLES` | Space-separated list of your own phone numbers/emails, used to strip your own handle out of group chat participant lists |

## License

MIT