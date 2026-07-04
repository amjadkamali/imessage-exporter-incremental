# imessage-exporter-incremental
A wrapper script around [imessage-exporter](https://github.com/ReagentX/imessage-exporter) that turns it into an incremental backup tool. Runs on a schedule or on demand, only exports what's changed since the last run, and skips re-exporting your entire message history from scratch every time.

Created to assist in maintaining an up to date iMessage archive without keeping the messages in iCloud.

I use this in conjunction with [imessage-exporter-viewer](https://github.com/amjadkamali/imessage-exporter-viewer) to make it even easier to browse — the viewer's own indexer handles deduplicating messages across every dated export this script produces, and merging conversations that are really the same person or group across different handles (see [Contact & Group Merging](https://github.com/amjadkamali/imessage-exporter-viewer#contact--group-merging) in its README). That merging logic used to live here; it's moved to the viewer so it works whether or not you're using this script at all, and can be checked against the actual database instead of guessed at export time.

I chose to rsync the iMessage Attachments folder and disable imessage-exporter's HEIC conversion because I preferred the longer UUID for the stronger guarantee of no collisions and I didn't want to maintain a duplicate attachments archive (both the rsync from iMessage + the converted output from imessage-exporter).

Heads up, most of this tool was written with Claude, including the rest of this README.

## Why this exists

`imessage-exporter` is excellent at what it does, but it has no concept of "only export what's new." Every run is a full export of your entire message history. For an active phone number with years of history and tens of thousands of attachments, that means every single backup run re-processes everything — slow, and wasteful of disk I/O for data that hasn't changed.

This pipeline solves that by tracking the last successful export's timestamp and asking `imessage-exporter` for only messages since then (with a one-day overlap for safety — see [How it works](#how-it-works)), landing each run in its own fresh, dated folder rather than re-touching anything from a previous run. Deduplicating those dated folders back into one continuous view per conversation is handled downstream, by whatever reads `iMessageExports/` — see [imessage-exporter-viewer](https://github.com/amjadkamali/imessage-exporter-viewer) — so this script itself stays simple: snapshot, refresh contacts, export, done.

## How it works

The pipeline runs three stages on every sync, plus a small extraction step at the end:

1. **Snapshot** — a read-only copy of `chat.db` (plus its WAL/SHM sidecars) and an additive sync of attachments, so the export step always works from a stable, consistent copy rather than the live database. Only a folder with a `.complete` sentinel counts as a real, trustworthy snapshot — an interrupted run's folder is left alone but never used to compute the next run's start date.
2. **Address Book refresh** — copies your macOS Address Book (main store plus every linked account source — iCloud, Exchange, On My Mac) into `iMessageExports/Contacts/`, then merges all of them into one combined database, since `imessage-exporter`'s own contact-name flag only accepts a single file. This runs before export specifically so the export step always has an up-to-date, single file to point at.
3. **Export** — runs `imessage-exporter -c disabled`, scoped to messages since `(last successful snapshot's date − 1 day)` — the extra day is a deliberate overlap buffer, not a bug: if your Mac's timezone changes between runs (travel, not just DST), a date boundary computed under one timezone doesn't reliably land the same way under another, and the overlap absorbs that shift instead of risking a silently missed message right at the edge. Any duplicate messages this overlap produces get cleaned up downstream at index time, not here. First run (or `--full`) exports your entire history instead.

After export, the script also does one more thing: it correlates each named or auto-generated-name group's message GUIDs against the snapshot's `chat.db` to find that group's *real* participant list (something `imessage-exporter`'s own filenames don't carry for these groups — see [The Participant Sidecar](#the-participant-sidecar)), and writes it out as a small JSON file alongside that run's export.

Each run lands in its own new, timestamped folder under `iMessageExports/` — never merged into or overwriting a previous run's folder. On the next run, it starts again from wherever the last completed snapshot left off.

## The Participant Sidecar

A named group (`Real Group Chat.html`) or an auto-generated "guessed name" group (`John, Jane & 3 others.html`) carries no reliable handle information in its filename — there's nothing to resolve against an Address Book or match against another export with. But `chat.db` itself always knows exactly who's in every chat, via its own `chat_handle_join` table.

After each export, this script takes every named/guessed-name file in that run, pulls a handful of its message GUIDs, and looks each one up against the snapshot's `chat.db` to find the real, current list of participant handles for that chat — trying the *most recent* messages in the file first, since for a long-running, high-volume group the oldest messages are the ones most likely to have already aged out of a given snapshot (macOS's own "Messages in iCloud" feature offloads older messages from local storage over time). The result is written as `group_participants.json` inside that run's export folder, mapping each named/guessed-name filename to its real handle list.

This is a point-in-time snapshot, not a live sync — a group's membership can genuinely change between one export and the next, which is expected and fine; nothing downstream requires it to stay constant. If `chat.db` has no matching record for a particular chat at export time, that file just doesn't get an entry — no error, nothing written for it, and whatever reads `iMessageExports/` falls back to treating it like it always would without this data.

## Requirements

- macOS with Messages in iCloud or a local `chat.db`
- [imessage-exporter](https://github.com/ReagentX/imessage-exporter) installed and on `PATH`
- Bash
- Python 3

## Quick Start

```bash
# One-time: point the script at your setup
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
  WorkingDir/
    imessage-incremental-sync.sh
    imessage-snapshot.sh
    snapshots/                        <- chat.db snapshots only, NEVER pruned
      2026-07-03_081953/
        chat.db
        .complete                     <- marks a fully-completed snapshot
  iMessageExports/                    <- top-level, user-facing, PERSISTENT
    Contacts/                         <- Address Book cache the web app reads
      AddressBook-v22.abcddb
      Sources/*/AddressBook-v22.abcddb
      Merged-AddressBook-v22.abcddb   <- combined, single-file view passed to imessage-exporter
    Attachments/                      <- shared, additive; referenced in place, never copied per-run
    StickerCache/
    2026-07-04_071623/                <- one fresh, dated folder PER RUN
      +15551234567.html
      Real Group Chat.html
      group_participants.json         <- real membership for named/guessed-name groups in this run
    2026-07-05_080211/                <- next run, its own separate folder
      ...
```

`iMessageExports/` is the one folder you point anything else (backups, a search tool, Nextcloud sync) at — it's the only directory meant to be read by something else while a sync might be in progress. `WorkingDir/snapshots/` is durable and load-bearing (it's what the next run's start date is computed from) but not something anything else needs to read.

## Flags

| Flag | Description |
|---|---|
| `--since YYYY-MM-DD` | Bootstrap start date for the very first run |
| `--full` | Re-export full history instead of incrementally |
| `--dry-run` | Show what would happen without writing anything |

## Environment Variables

| Variable | Description |
|---|---|
| `IMESSAGE_ROOT` | Parent directory containing `iMessageExports/` and `WorkingDir/` |
| `IMESSAGE_WORKING_DIR` | Working directory for the scripts and snapshots |
| `IMESSAGE_MY_HANDLES` | Space-separated list of your own phone numbers/emails, used to strip your own handle out of group chat participant lists when Messages occasionally (and incorrectly) inserts it |

## License

MIT