#!/usr/bin/env bash
#
# imessage-snapshot.sh — read-only snapshot of the local Messages database.
#
# This script NEVER writes to the live database. It does not checkpoint,
# does not open the DB for writing, and does not run any PRAGMA that
# modifies state. It only copies files and reads from copies.
#
# It captures chat.db together with its -wal and -shm sidecars so the
# snapshot reflects complete, up-to-date state (recent messages still
# living in the WAL are included). SQLite reassembles them automatically
# the next time the *copy* is opened — the source is untouched.
#
# Usage:  ./imessage-snapshot.sh [destination_root] [shared_attachment_root]
# Default destination root: ~/imessage-snapshots
# Default shared attachment root: same as destination_root, if not given
#
set -euo pipefail

# ---- configuration ---------------------------------------------------------
MSG_DIR="${HOME}/Library/Messages"
SRC_DB="${MSG_DIR}/chat.db"
SRC_ATTACH="${MSG_DIR}/Attachments"
SRC_STICKERS="${MSG_DIR}/StickerCache"
DEST_ROOT="${1:-${HOME}/Local/imessage-snapshots}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
SNAP="${DEST_ROOT}/${STAMP}"
# Shared, append-only stores for ALL snapshots (no per-snapshot duplication).
# Stickers live in a SEPARATE dir from Attachments; imessage-exporter's
# --attachment-root (-r) can point at either, and chat.db's attachment.filename
# column references files in both. So we mirror both, side by side.
# This can live in a completely different location than the dated DB
# snapshots (second argument) -- e.g. inside a folder that gets synced
# elsewhere, if that's where you want the actual attachment bytes to live.
SHARED_ROOT="${2:-${DEST_ROOT}}"
SHARED_ATTACH="${SHARED_ROOT}/Attachments"
SHARED_STICKERS="${SHARED_ROOT}/StickerCache"

# ---- preflight -------------------------------------------------------------
if [[ ! -f "${SRC_DB}" ]]; then
  echo "ERROR: ${SRC_DB} not found." >&2
  echo "If it exists but is unreadable, grant your terminal Full Disk Access:" >&2
  echo "  System Settings -> Privacy & Security -> Full Disk Access" >&2
  exit 1
fi

if [[ ! -r "${SRC_DB}" ]]; then
  echo "ERROR: ${SRC_DB} is not readable (likely Full Disk Access)." >&2
  exit 1
fi

mkdir -p "${SNAP}"
echo "==> Snapshotting to: ${SNAP}"
echo "    (read-only: the live database is never modified)"

# ---- copy the database + sidecars (pure reads) -----------------------------
# Copy the sidecars FIRST, then the main DB last, to minimize the chance of a
# torn read if a checkpoint happens mid-copy. Sidecars may not exist if the DB
# was cleanly checkpointed by the OS — that's fine, hence the "|| true".
echo "==> Copying WAL/SHM sidecars (if present)..."
cp -p "${SRC_DB}-wal" "${SNAP}/" 2>/dev/null || echo "    (no -wal sidecar; already checkpointed)"
cp -p "${SRC_DB}-shm" "${SNAP}/" 2>/dev/null || echo "    (no -shm sidecar)"

echo "==> Copying chat.db..."
cp -p "${SRC_DB}" "${SNAP}/chat.db"

# ---- copy attachments into the SHARED store (additive + healing) -----------
# Plain `rsync -a` (default size/mtime comparison):
#   * skips files already present and unchanged  -> no wasteful duplication
#   * REPLACES a file whose bytes changed         -> zero-byte iCloud stubs
#                                                     heal once downloaded
#   * NEVER deletes                               -> a source wipe cannot
#                                                     propagate into the archive
# Do NOT add --ignore-existing (would freeze stubs forever) or --delete.
if [[ -d "${SRC_ATTACH}" ]]; then
  echo "==> Syncing Attachments into shared store: ${SHARED_ATTACH}"
  echo "    (additive; changed files updated; nothing ever deleted)"
  mkdir -p "${SHARED_ATTACH}"
  rsync -a "${SRC_ATTACH}/" "${SHARED_ATTACH}/"
else
  echo "    WARNING: no Attachments folder found at ${SRC_ATTACH}" >&2
fi

# StickerCache: same additive/healing rsync policy. Stickers are referenced by
# chat.db just like other attachments but resolve into this separate directory.
if [[ -d "${SRC_STICKERS}" ]]; then
  echo "==> Syncing StickerCache into shared store: ${SHARED_STICKERS}"
  mkdir -p "${SHARED_STICKERS}"
  rsync -a "${SRC_STICKERS}/" "${SHARED_STICKERS}/"
else
  echo "    (no StickerCache folder at ${SRC_STICKERS}; skipping)"
fi

# ---- verify the SNAPSHOT (never the source) --------------------------------
# All checks below run against the COPY in ${SNAP}, opened read-only. The live
# DB is never opened. Uses Python's built-in sqlite3 (no sqlite3 CLI needed).
echo "==> Verifying snapshot integrity..."
read -r INTEGRITY MSG_COUNT ATT_COUNT < <(
  python3 - "${SNAP}/chat.db" << 'PYEOF'
import sqlite3, sys
try:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    integ = con.execute("PRAGMA integrity_check;").fetchone()[0]
    msg = con.execute("SELECT COUNT(*) FROM message;").fetchone()[0]
    att = con.execute("SELECT COUNT(*) FROM attachment;").fetchone()[0]
    con.close()
    print(integ, msg, att)
except Exception as e:
    print(f"error:{e}".replace(" ", "_"), "?", "?")
PYEOF
)
if [[ "${INTEGRITY}" == "ok" ]]; then
  echo "    integrity_check: ok"
else
  echo "    WARNING: integrity_check returned: ${INTEGRITY}" >&2
fi
echo "    messages in snapshot:    ${MSG_COUNT}"
echo "    attachment rows in DB:   ${ATT_COUNT}"

# ---- audit EVERY referenced attachment, WITH chat names --------------------
# Two distinct failure modes, both bad for preservation:
#   * zero-byte : file exists in the shared store but has no bytes
#                 (iCloud placeholder that never downloaded)
#   * missing   : the DB references a file that is NOT in the shared store
#                 at all (wiped before first snapshot, or never downloaded
#                 and left no placeholder)
# We drive this from the DATABASE (every attachment row), not from disk, so
# fully-absent files are caught too. For each problem file we resolve the chat.
#
# IMPORTANT: runs against the SNAPSHOT COPY (${SNAP}/chat.db), opened
# read-only. The live database is never opened or touched here.
STUBS=0; MISSING=0
if [[ -d "${SHARED_ATTACH}" || -d "${SHARED_STICKERS}" ]]; then
  echo "==> Auditing every referenced attachment against the shared store..."
  # Python does the join AND the on-disk existence/size test, so both
  # failure modes are classified in one read-only pass.
  AUDIT_OUT="$(python3 - "${SNAP}/chat.db" "${SHARED_ATTACH}" "${SNAP}/attachment-audit.json" "${SHARED_STICKERS}" << 'PYEOF'
import sqlite3, sys, os, json, datetime
from collections import defaultdict
db_path, shared, audit_json = sys.argv[1], sys.argv[2], sys.argv[3]
shared_stickers = sys.argv[4] if len(sys.argv) > 4 else ""
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
except Exception as e:
    print(f"ERROR could not open snapshot db read-only: {e}")
    sys.exit(0)

# Pull every attachment with its chat name, transfer name, full stored path,
# and the sending message's timestamp. We DON'T compute the tail in SQL anymore
# because a path may live under Attachments/ OR StickerCache/ — Python picks
# the right shared dir per row based on which marker the path contains.
rows = con.execute("""
SELECT COALESCE(
    NULLIF(c.display_name, ''),
    (SELECT GROUP_CONCAT(h.id, ', ')
       FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id
      WHERE chj.chat_id = c.ROWID),
    c.chat_identifier
  ) AS chat_name,
  a.transfer_name,
  a.filename AS stored_path,
  m.date AS msg_date
FROM attachment a
JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
JOIN message m                    ON m.ROWID = maj.message_id
JOIN chat_message_join cmj        ON cmj.message_id = m.ROWID
JOIN chat c                       ON c.ROWID = cmj.chat_id
WHERE a.filename IS NOT NULL
  AND (a.filename LIKE '%Attachments/%' OR a.filename LIKE '%StickerCache/%')
""").fetchall()
con.close()

def resolve_path(stored):
    """
    Map a chat.db filename to its location in the shared stores. Splits on
    whichever marker directory the path contains, then joins the tail onto the
    matching shared dir. Returns (abs_path_or_None, tail).
    """
    for marker, base in (("StickerCache/", shared_stickers),
                         ("Attachments/", shared)):
        idx = stored.find(marker)
        if idx != -1:
            tail = stored[idx + len(marker):]
            if base:
                return os.path.join(base, tail), tail
            return None, tail
    return None, stored

# iMessage stores message.date as nanoseconds since the Apple/Cocoa epoch
# (2001-01-01 UTC), not the Unix epoch. Older databases used seconds; detect
# by magnitude and convert either way to a local-time ISO string.
APPLE_EPOCH = 978307200  # unix seconds at 2001-01-01T00:00:00Z
def apple_to_iso(v):
    if not v:
        return None
    try:
        secs = (v / 1_000_000_000) if v > 10**12 else v
        return datetime.datetime.fromtimestamp(
            secs + APPLE_EPOCH, datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None

zero_by_chat = defaultdict(list)
missing_by_chat = defaultdict(list)
zero_detail, missing_detail = [], []   # full records for the JSON file
n_zero = n_missing = n_ok = 0
for chat_name, transfer_name, stored_path, msg_date in rows:
    chat = chat_name or "(unknown chat)"
    path, tail = resolve_path(stored_path)
    label = transfer_name or tail
    sent = apple_to_iso(msg_date)
    rec = {"chat": chat, "file": label, "sent": sent,
           "tail": tail, "stored_path": stored_path}
    if path is None or not os.path.exists(path):
        missing_by_chat[chat].append((sent, label)); missing_detail.append(rec); n_missing += 1
    elif os.path.getsize(path) == 0:
        zero_by_chat[chat].append((sent, label)); zero_detail.append(rec); n_zero += 1
    else:
        n_ok += 1

# Sort problem lists OLDEST-FIRST: the earliest-sent files are closest to the
# wipe horizon, so they're the ones to rescue first. (None-dated sort last.)
_key = lambda r: (r["sent"] is None, r["sent"] or "", r["chat"], r["file"])

# Write the FULL detail to a JSON file next to the snapshot's chat.db.
# This is the durable, actionable record — not truncated like the terminal view.
audit = {
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "snapshot_db": db_path,
    "shared_attachments": shared,
    "totals": {"ok": n_ok, "zero_byte": n_zero, "missing": n_missing,
               "referenced": len(rows)},
    "zero_byte": sorted(zero_detail, key=_key),
    "missing":   sorted(missing_detail, key=_key),
}
try:
    with open(audit_json, "w") as fh:
        json.dump(audit, fh, indent=2)
except Exception as e:
    print(f"WARN could not write audit json: {e}")

# First line is the machine-readable count line the shell parses.
print(f"COUNTS {n_zero} {n_missing}")

def dump(title, groups):
    if not groups: return
    print(title)
    for chat in sorted(groups):
        files = sorted(groups[chat], key=lambda t: (t[0] is None, t[0] or ""))
        print(f"      {chat}  ({len(files)})")
        for sent, label in files[:8]:
            when = sent if sent else "date unknown"
            print(f"          - [{when}]  {label}")
        if len(files) > 8:
            print(f"          ... and {len(files) - 8} more (full list in attachment-audit.json)")

dump("    ZERO-BYTE (exists but empty — download to heal):", zero_by_chat)
dump("    MISSING (not in shared store at all):", missing_by_chat)
PYEOF
)"
  # Parse the COUNTS line, print the rest to stderr for the user.
  STUBS="$(printf '%s\n' "${AUDIT_OUT}" | awk '/^COUNTS/{print $2; exit}')"
  MISSING="$(printf '%s\n' "${AUDIT_OUT}" | awk '/^COUNTS/{print $3; exit}')"
  STUBS="${STUBS:-0}"; MISSING="${MISSING:-0}"
  printf '%s\n' "${AUDIT_OUT}" | grep -v '^COUNTS' >&2 || true

  if [[ "${STUBS}" -eq 0 && "${MISSING}" -eq 0 ]]; then
    echo "    all referenced attachments present with real bytes."
  else
    echo "    WARNING: ${STUBS} zero-byte, ${MISSING} missing — see above." >&2
    if [[ "${STUBS}" -gt 0 ]]; then
      echo "    Zero-byte: open those chats in Messages; next run heals them." >&2
    fi
    if [[ "${MISSING}" -gt 0 ]]; then
      echo "    Missing: these are not on disk. If still in iCloud, download in" >&2
      echo "    Messages and re-snapshot; if already wiped, they are unrecoverable." >&2
    fi
  fi
fi

# ---- record a small manifest -----------------------------------------------
{
  echo "snapshot_time: ${STAMP}"
  echo "source_db: ${SRC_DB}"
  echo "message_count: ${MSG_COUNT}"
  echo "attachment_rows: ${ATT_COUNT}"
  echo "integrity_check: ${INTEGRITY}"
  echo "zero_byte_attachments: ${STUBS:-0}"
  echo "missing_attachments: ${MISSING:-0}"
  echo "attachment_audit: ${SNAP}/attachment-audit.json"
  echo "shared_attachments: ${SHARED_ATTACH}"
  echo "wal_copied: $([[ -f "${SNAP}/chat.db-wal" ]] && echo yes || echo no)"
} > "${SNAP}/manifest.txt"

echo "==> Done. Manifest written to ${SNAP}/manifest.txt"
echo "    The live database was not modified at any point."