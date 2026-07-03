#!/usr/bin/env bash
#
# imessage-incremental-sync.sh — orchestrates the other three tools into one
# incremental update:
#
#   1. imessage-snapshot.sh   — read-only copy of chat.db + synced attachments
#   2. imessage-exporter      — export only messages since the last run
#   3. merge_by_contact.py    — fold the new export into your existing
#                               archive AND consolidate multi-handle contacts,
#                               in a single pass (see WHY ONE MERGE PASS below)
#
# LAYOUT — the live, browsable result sits at the top; everything that
# supports producing it is tucked into WorkingDir:
#
#   ROOT/
#     index.html                  <- generated page listing every chat,
#                                     newest first, linking into iMessageExports
#     iMessageExports/           <- TOP LEVEL, user-facing, PERSISTENT
#                                     directory (never renamed away). Its
#                                     *.html files get replaced each run with
#                                     the newly merged + deduped set.
#       Attachments/  StickerCache/  shared, additive stores synced directly
#                                     here by imessage-snapshot.sh every run
#                                     (see -c disabled note below) -- so the
#                                     actual attachment bytes travel with
#                                     this folder (e.g. if it's synced via
#                                     Nextcloud), not just the HTML.
#     WorkingDir/
#       imessage-snapshot.sh, merge_by_contact.py, merge_html_exports.py
#       venv/                     <- has beautifulsoup4; activated before merging
#       snapshots/                <- DB snapshots only now (Attachments/
#         2026-07-02_233450/          StickerCache moved to iMessageExports,
#                                      see above) -- one folder per run,
#                                      chat.db + sidecars
#       exports/                  <- this tool's raw imessage-exporter output,
#         2026-07-03_081953/          one folder per run's date window
#       archives/                 <- ONLY rotated-out, superseded backups now
#         archive-2026-07-03/         (the live one lives in iMessageExports
#                                      at the top, not here)
#
# imessage-snapshot.sh needs no changes for this: it just takes whatever
# destination path it's given as $1 and creates the stamped snapshot dir plus
# Attachments/StickerCache underneath it. Pointing it at WorkingDir/snapshots
# nests all of its own output there automatically.
#
# NO STATE FILE — the "date we last exported through" is read directly off
# the name of the newest COMPLETED folder in WorkingDir/snapshots, rather
# than tracked separately. One less thing to get out of sync with reality;
# the folder names themselves ARE the history. Tracking lives on snapshots
# rather than exports specifically so it survives even when exports (and
# archives) are fully erased at the end of a run -- see ERASING EXPORTS AND
# ARCHIVES further down.
#
# SENTINEL FILE GUARDS AGAINST TRUSTING A CRASHED RUN:
# A snapshot only counts as "the last one" if it contains a .complete marker,
# written only after imessage-exporter ALSO exits successfully (not just the
# snapshot itself -- see step 2). Without this, a run interrupted partway
# through (crash, killed process, disk full) would still leave its dated
# snapshot behind; the NEXT run would trust that folder's timestamp as fully
# covering that window and only look one day further back -- silently
# creating a permanent gap for whatever didn't finish exporting, with no
# error anywhere. An incomplete snapshot without the marker is simply
# skipped when looking for "the latest" (it's left on disk, not deleted, in
# case you want to inspect what did make it out).
#
# WHY ONE MERGE PASS DOES BOTH JOBS:
# merge_by_contact.py groups input files by resolved Address Book contact and
# delegates to merge_html_exports.py, which dedupes by message GUID. If we
# feed it BOTH your existing archive's files AND this run's fresh export
# together, two things happen for free:
#   - a contact with two handles (phone + email) gets consolidated, same as
#     any other run of that tool
#   - messages already in your archive share GUIDs with the overlapping part
#     of the new export (the 1-day buffer), so they dedupe away, and only the
#     genuinely new messages get appended
# This means "merge multi-number contacts" and "merge newest export into the
# previous one" are literally the same operation here, not two passes.
#
# WHY MERGING STILL GOES THROUGH A STAGING FOLDER FIRST:
# merge_by_contact.py's single-file passthrough uses shutil.copy2, which
# raises if source and destination are the same file. If a contact had no new
# messages this window, its archive file would be the ONLY input in its
# group -- and if we merged straight back into "iMessageExports" while
# reading from it, that copy-onto-itself would crash the whole run. So every
# run merges into a hidden staging folder inside WorkingDir/archives first,
# safely away from the live files being read.
#
# WHY THE LIVE FILES ARE UPDATED IN PLACE, NOT DELETED-AND-RECREATED:
# Once staging succeeds, each file's CONTENT is written into the existing
# "iMessageExports/<name>.html" path (same inode) rather than moving the old
# one out and moving a new one in. This matters if "iMessageExports" lives
# inside a sync tool's folder (Nextcloud, Dropbox, iCloud Drive, etc.): those
# treat an in-place content change as one ordinary "modified" event, but a
# delete followed by a recreate as two separate destructive ones -- wasting
# bandwidth re-uploading bytes that didn't change, and occasionally
# generating a bogus "conflicted copy" if the delete and recreate straddle a
# sync cycle. A COPY (not move) of the old state still lands in
# WorkingDir/archives/archive-<today's date> first, so history is preserved
# exactly as before; nothing is ever deleted there either, unless you opt
# into --keep-archives. The only time a live file is actually removed is
# when a contact's winning filename changes between runs (rare) and the old
# name has nothing left to update in place. Attachments are handled entirely
# separately now -- see step 1 above and the -c disabled note there;
# imessage-snapshot.sh syncs them directly into "iMessageExports" itself.
#
# USAGE:
#   ./imessage-incremental-sync.sh                  # normal incremental run.
#                                                        # DEFAULT keeps 0 old
#                                                        # dated archive backups
#                                                        # and 0 old raw exports
#                                                        # (beyond the single
#                                                        # newest completed one,
#                                                        # which always survives
#                                                        # regardless -- see
#                                                        # --keep-exports below).
#                                                        # DB snapshots are the
#                                                        # durable record; both
#                                                        # are reconstructable
#                                                        # from them later.
#   ./imessage-incremental-sync.sh --since 2024-01-01   # first-run bootstrap
#   ./imessage-incremental-sync.sh --full           # force full re-export
#   ./imessage-incremental-sync.sh --dry-run        # show the plan, do nothing
#   ./imessage-incremental-sync.sh --keep-archives 10   # retain more dated
#                                                        # HTML backups instead
#                                                        # of the default 0
#   ./imessage-incremental-sync.sh --keep-exports 5     # retain more raw
#                                                        # exports instead of
#                                                        # the default 0 (still
#                                                        # always keeps at
#                                                        # least the newest
#                                                        # completed one)
#   ./imessage-incremental-sync.sh --merge-only     # skip snapshot + export;
#                                                        # re-run just the merge
#                                                        # against the most
#                                                        # recent COMPLETED
#                                                        # export already on
#                                                        # disk. For recovering
#                                                        # from a merge that
#                                                        # was killed/crashed
#                                                        # after export already
#                                                        # succeeded, without
#                                                        # burning time
#                                                        # re-snapshotting and
#                                                        # re-exporting.
#
# This script targets macOS (BSD `date`, ~/Library/Messages, Address Book) —
# same platform assumption as the three tools it orchestrates.
set -euo pipefail

# ---- configuration ----------------------------------------------------------
# This script lives inside WorkingDir. ROOT is WorkingDir's parent, where the
# top-level "iMessageExports" folder sits alongside it. Override either
# independently if you ever need to (e.g. testing).
WORKING_DIR="${IMESSAGE_WORKING_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ROOT="${IMESSAGE_ROOT:-$(dirname "${WORKING_DIR}")}"

SNAPSHOT_SCRIPT="${WORKING_DIR}/imessage-snapshot.sh"
MERGE_BY_CONTACT="${WORKING_DIR}/merge_by_contact.py"
MERGE_HTML_EXPORTS="${WORKING_DIR}/merge_html_exports.py"
GENERATE_INDEX="${WORKING_DIR}/generate_index.py"
INDEX_OUTPUT="${ROOT}/index.html"   # top-level, alongside iMessageExports itself
# Invoked directly by full path rather than via `source activate` + bare
# `python3` -- that indirection turned out to be unreliable (python3 kept
# resolving to the Homebrew interpreter instead of the venv's own, even
# though activation itself ran without error). A direct path sidesteps
# PATH/shell-hashing ambiguity entirely.
VENV_PYTHON="${IMESSAGE_VENV_PYTHON:-${WORKING_DIR}/venv/bin/python3}"

# Your own phone numbers/emails, space-separated. iMessage can occasionally
# glitch and insert your own handle into a group chat's participant list;
# merge_by_contact.py strips any of these out of a group's participants
# before matching (unless that would leave a group with nobody in it, which
# means it's a genuine chat with yourself and is left alone). Leave empty to
# disable.
MY_HANDLES=""

SNAPSHOT_ROOT="${WORKING_DIR}/snapshots"   # passed to imessage-snapshot.sh as $1
RAW_ROOT="${WORKING_DIR}/exports"
ARCHIVE_ROOT="${WORKING_DIR}/archives"     # holds ONLY rotated-out dated backups
LIVE_ARCHIVE="${ROOT}/iMessageExports"    # top-level, user-facing, live archive
LOCK_DIR="${WORKING_DIR}/.sync.lock"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
TODAY="$(date +%Y-%m-%d)"
RAW_DIR="${RAW_ROOT}/${STAMP}"                           # this run's raw export
ARCHIVE_STAGING="${ARCHIVE_ROOT}/.staging-${STAMP}"      # scratch; merge writes here first

# ---- argument parsing --------------------------------------------------------
SINCE_OVERRIDE=""
FORCE_FULL=0
DRY_RUN=0
# Default is 0 for both: keep no dated backups and no old raw exports beyond
# what's structurally needed (the single newest completed export always
# survives regardless -- see the export-pruning block further down). DB
# snapshots are the durable record; exports and merged-archive backups are
# both fully reconstructable from them later if ever needed, so there's no
# reason to keep accumulating copies of either by default. Override with
# --keep-archives N / --keep-exports N to retain more.
KEEP_ARCHIVES="0"
KEEP_EXPORTS="0"
MERGE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      SINCE_OVERRIDE="$2"; shift 2 ;;
    --full)
      FORCE_FULL=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    --keep-archives)
      KEEP_ARCHIVES="$2"; shift 2 ;;
    --keep-exports)
      KEEP_EXPORTS="$2"; shift 2 ;;
    --merge-only)
      MERGE_ONLY=1; shift ;;
    *)
      echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ "${MERGE_ONLY}" -eq 1 && ( "${FORCE_FULL}" -eq 1 || -n "${SINCE_OVERRIDE}" ) ]]; then
  echo "ERROR: --merge-only doesn't export anything, so --full/--since have nothing to act on." >&2
  exit 1
fi

if [[ -n "${SINCE_OVERRIDE}" && ! "${SINCE_OVERRIDE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "ERROR: --since must be YYYY-MM-DD, got: ${SINCE_OVERRIDE}" >&2
  exit 1
fi

# ---- preflight ---------------------------------------------------------------
for f in "${SNAPSHOT_SCRIPT}" "${MERGE_BY_CONTACT}" "${MERGE_HTML_EXPORTS}" "${GENERATE_INDEX}"; do
  if [[ ! -f "${f}" ]]; then
    echo "ERROR: expected to find $(basename "${f}") in ${WORKING_DIR}" >&2
    exit 1
  fi
done
if [[ ! -x "${SNAPSHOT_SCRIPT}" ]]; then
  echo "ERROR: ${SNAPSHOT_SCRIPT} is not executable. Run: chmod +x '${SNAPSHOT_SCRIPT}'" >&2
  exit 1
fi
if ! command -v imessage-exporter >/dev/null 2>&1; then
  echo "ERROR: imessage-exporter not found on PATH." >&2
  echo "  Install: brew install imessage-exporter  (or cargo install imessage-exporter)" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH." >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "ERROR: rsync not found on PATH (needed to merge attachments)." >&2
  exit 1
fi

mkdir -p "${SNAPSHOT_ROOT}" "${RAW_ROOT}" "${ARCHIVE_ROOT}"

# Prevent two runs from stomping on each other (e.g. an overlapping
# cron/launchd invocation).
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "ERROR: another run appears to be in progress (${LOCK_DIR} exists)." >&2
  echo "  If that's stale (a previous run crashed), remove it manually and retry." >&2
  exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

# ---- compute the export window ----------------------------------------------
# End date is EXCLUSIVE in imessage-exporter, so "tomorrow" captures all of
# today. Start date is (date of the last completed snapshot - 1 day): the
# requested one-day overlap buffer, so a message right at yesterday's
# boundary can never be missed -- any overlap just dedupes away in the merge.
END_DATE="$(date -v+1d +%Y-%m-%d)"

# Find the most recent COMPLETED snapshot -- its name IS the state, no
# separate file to track or drift out of sync with reality. Tracking lives
# on snapshots rather than exports specifically because exports (and
# archives) are meant to be fully disposable when KEEP_EXPORTS/
# KEEP_ARCHIVES are 0 (see the erase-at-the-end step further down); nothing
# there can be relied on to still exist by the next run. Only folders with
# the .complete sentinel count (see SENTINEL FILE note above); an
# interrupted run's snapshot is left alone but not trusted.
LATEST_SNAPSHOT_DIR="$(find "${SNAPSHOT_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' \
  -exec test -e '{}/.complete' \; -print 2>/dev/null | sort | tail -1)"

BOOTSTRAP=0
if [[ "${MERGE_ONLY}" -eq 1 ]]; then
  # --merge-only re-runs just the merge against whatever the most recent
  # export folder is, by name alone -- it doesn't need (and exports no
  # longer carry) a .complete marker, since the whole point is "I already
  # saw the export succeed, just redo the merge". If a run crashes here,
  # set -e means the erase-at-the-end step is never reached, so a crashed
  # run's export folder is always still there to recover from -- there's no
  # risk of --merge-only having nothing left to work with.
  LATEST_EXPORT_DIR="$(find "${RAW_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -1)"
  if [[ -z "${LATEST_EXPORT_DIR}" ]]; then
    echo "ERROR: --merge-only needs an existing export to merge from, but none" >&2
    echo "  was found under ${RAW_ROOT}." >&2
    exit 1
  fi
  RAW_DIR="${LATEST_EXPORT_DIR}"
  START_DATE="(skipped -- reusing existing export)"
  echo "==> --merge-only: skipping snapshot + export, reusing: ${RAW_DIR}"
elif [[ "${FORCE_FULL}" -eq 1 ]]; then
  START_DATE=""   # no -s flag at all: full history
  echo "==> --full requested: exporting entire history (no start-date filter)."
elif [[ -n "${SINCE_OVERRIDE}" ]]; then
  START_DATE="${SINCE_OVERRIDE}"
  echo "==> --since override: exporting from ${START_DATE}."
elif [[ -n "${LATEST_SNAPSHOT_DIR}" ]]; then
  LAST_STAMP="$(basename "${LATEST_SNAPSHOT_DIR}")"    # 2026-07-03_081555
  LAST_DATE="${LAST_STAMP:0:10}"                        # 2026-07-03
  START_DATE="$(date -j -v-1d -f "%Y-%m-%d" "${LAST_DATE}" +%Y-%m-%d)"
  echo "==> Latest completed snapshot: ${LAST_STAMP} (${LAST_DATE}); exporting from ${START_DATE} (1-day overlap)."
else
  BOOTSTRAP=1
  START_DATE=""
  echo "==> No prior COMPLETED snapshot found under ${SNAPSHOT_ROOT}."
  echo "    First run: exporting your ENTIRE message history. This can take a"
  echo "    while for large databases. Pass --since YYYY-MM-DD instead if you"
  echo "    only want to bootstrap from a specific date."
fi

ARCHIVE_PREV=""
if [[ -d "${LIVE_ARCHIVE}" ]]; then
  ARCHIVE_PREV="${LIVE_ARCHIVE}"
fi

echo "==> Plan:"
if [[ "${MERGE_ONLY}" -eq 1 ]]; then
  echo "      mode:         merge-only (no snapshot, no export)"
else
  echo "      window:       ${START_DATE:-<full history>}  ..  ${END_DATE} (exclusive)"
fi
echo "      raw export:   ${RAW_DIR}"
echo "      prev archive: ${ARCHIVE_PREV:-<none — first archive>}"
echo "      live archive: ${LIVE_ARCHIVE}  (old one, if any, rotates to archive-${TODAY})"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[dry-run] Stopping here. Nothing was snapshotted, exported, or merged."
  exit 0
fi

# ---- step 1: read-only DB snapshot + synced attachments ---------------------
if [[ "${MERGE_ONLY}" -eq 0 ]]; then
  echo
  echo "==> [1/3] Snapshotting chat.db (read-only) and syncing attachments..."
  # Second argument tells imessage-snapshot.sh to put the shared
  # Attachments/StickerCache stores directly inside "iMessageExports"
  # itself, rather than alongside the dated DB snapshots. That's what lets
  # -c disabled below reference them in place -- and since this is the
  # folder actually being synced (e.g. to Nextcloud), the referenced files
  # travel with it instead of only existing on this one Mac.
  "${SNAPSHOT_SCRIPT}" "${SNAPSHOT_ROOT}" "${LIVE_ARCHIVE}"

  # Find the snapshot subdirectory that command just created. imessage-snapshot.sh
  # generates its own STAMP internally; newest one sorts last lexicographically.
  # No naming collision to worry about here since exports/ is a separate
  # directory from snapshots/.
  SNAP_DIR="$(find "${SNAPSHOT_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -1)"
  if [[ -z "${SNAP_DIR}" || ! -f "${SNAP_DIR}/chat.db" ]]; then
    echo "ERROR: could not locate the snapshot's chat.db under ${SNAPSHOT_ROOT}" >&2
    exit 1
  fi
  echo "    using snapshot: ${SNAP_DIR}/chat.db"

  # ---- step 2: export only the new window, from the SNAPSHOT (never the live DB) --
  # -p and -r now point at two DIFFERENT directories (chat.db still lives
  # under WorkingDir/snapshots/<stamp>, but Attachments/StickerCache now
  # live under "iMessageExports" instead), so both are given as absolute
  # paths rather than relying on a single relative cwd for both.
  #
  # -c disabled: reference attachments in place instead of copying and
  # converting them. No HEIC/CAF/video conversion happens with this -- native
  # iPhone formats (e.g. HEIC) render fine in Safari but not in every
  # browser. Chosen deliberately over -c full specifically so the actual
  # attachment bytes live inside the synced folder rather than being
  # duplicated (and converted) into a copy that only exists on this Mac.
  echo
  echo "==> [2/3] Exporting messages (${START_DATE:-<full history>} .. ${END_DATE})..."
  mkdir -p "${RAW_DIR}"
  EXPORTER_ARGS=(-f html -c disabled -p "${SNAP_DIR}/chat.db" -r "${LIVE_ARCHIVE}" -a macOS -e "${END_DATE}" -o "${RAW_DIR}")
  if [[ -n "${START_DATE}" ]]; then
    EXPORTER_ARGS+=(-s "${START_DATE}")
  fi
  imessage-exporter "${EXPORTER_ARGS[@]}"

  # -c disabled embeds the ABSOLUTE filesystem path it resolved each
  # attachment to (confirmed against imessage-exporter's own docs:
  # "Attachments are not copied; the export references them in-place by
  # filesystem path") -- there's no built-in option for relative paths.
  # But since attachments and the final merged HTML both live in the exact
  # same "iMessageExports" folder, stripping that known absolute prefix
  # turns each reference into a portable relative path instead of one that
  # only resolves on this Mac -- which matters once this folder is synced
  # elsewhere (Nextcloud, another device, etc.). Plain string replacement
  # is enough here (no HTML parsing needed), so this doesn't need the venv.
  python3 - "${RAW_DIR}" "${LIVE_ARCHIVE}" <<'PYEOF'
import sys
from pathlib import Path
raw_dir, live_archive = sys.argv[1], sys.argv[2]
prefix = live_archive.rstrip("/") + "/"
for f in Path(raw_dir).glob("*.html"):
    text = f.read_text(encoding="utf-8", errors="replace")
    if prefix in text:
        f.write_text(text.replace(prefix, ""), encoding="utf-8")
PYEOF

  # Only reached if the exporter above exited 0 (set -e aborts otherwise), so
  # this run is safe to trust as "fully covers this window" on future runs.
  # Written into the SNAPSHOT folder, not the export folder -- exports (and
  # archives) are meant to be fully disposable when KEEP_EXPORTS/
  # KEEP_ARCHIVES are 0 (see the erase-at-the-end step further down), so
  # date-tracking can't depend on anything surviving there. Snapshots are
  # never erased by this script, so this is the one place a completion
  # record can safely live long-term.
  {
    echo "start=${START_DATE:-<full history>}"
    echo "end=${END_DATE}"
    echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "${SNAP_DIR}/.complete"
else
  echo
  echo "==> [1/3] and [2/3] skipped (--merge-only)."
fi

RAW_COUNT="$(find "${RAW_DIR}" -maxdepth 1 -name '*.html' | wc -l | tr -d ' ')"
echo "    using ${RAW_COUNT} file(s) from ${RAW_DIR}"

# ---- step 3: one merge pass = contact consolidation + archive update --------
if [[ "${RAW_COUNT}" -eq 0 && -n "${ARCHIVE_PREV}" ]]; then
  echo
  echo "==> [3/3] No messages in this window and an archive already exists —"
  echo "    nothing changed, skipping the HTML merge. \"iMessageExports\" is untouched."
else
  echo
  echo "==> [3/3] Merging (contact consolidation + fold into archive)..."
  INPUT_FILES=()
  if [[ -n "${ARCHIVE_PREV}" ]]; then
    while IFS= read -r -d '' f; do INPUT_FILES+=("${f}"); done \
      < <(find "${ARCHIVE_PREV}" -maxdepth 1 -name '*.html' -print0)
  fi
  while IFS= read -r -d '' f; do INPUT_FILES+=("${f}"); done \
    < <(find "${RAW_DIR}" -maxdepth 1 -name '*.html' -print0)

  if [[ "${#INPUT_FILES[@]}" -eq 0 ]]; then
    echo "    no files to merge (empty export, no prior archive) — nothing to do."
  else
    if [[ -x "${VENV_PYTHON}" ]]; then
      PYTHON_BIN="${VENV_PYTHON}"
      echo "    using venv python3: ${PYTHON_BIN}"
    else
      PYTHON_BIN="python3"
      echo "    (no venv python3 at ${VENV_PYTHON}; using system python3)"
    fi

    MY_HANDLE_ARGS=()
    for h in ${MY_HANDLES}; do
      MY_HANDLE_ARGS+=(--my-handle "${h}")
    done

    "${PYTHON_BIN}" "${MERGE_BY_CONTACT}" -o "${ARCHIVE_STAGING}" "${MY_HANDLE_ARGS[@]}" "${INPUT_FILES[@]}"

    # Back up the OLD state (a COPY, not a move -- the live files stay put
    # and get updated in place just below) before anything in
    # "iMessageExports" changes. Attachments are deliberately never part of
    # this backup -- they're maintained directly inside "iMessageExports" by
    # imessage-snapshot.sh in step 1, entirely separately from this HTML
    # rotation, since they're additive/immutable and don't need a versioned
    # history the way the HTML does.
    if [[ -n "${ARCHIVE_PREV}" ]]; then
      BACKUP_DIR="${ARCHIVE_ROOT}/archive-${TODAY}"
      SUFFIX=2
      while [[ -e "${BACKUP_DIR}" ]]; do
        BACKUP_DIR="${ARCHIVE_ROOT}/archive-${TODAY}_${SUFFIX}"
        SUFFIX=$((SUFFIX + 1))
      done
      mkdir -p "${BACKUP_DIR}"
      find "${LIVE_ARCHIVE}" -maxdepth 1 -name '*.html' -exec cp {} "${BACKUP_DIR}/" \;
      echo "    previous archive's HTML backed up to: ${BACKUP_DIR}"
      echo "    (no attachments in the backup -- the live \"iMessageExports\" folder"
      echo "     is the only copy with working images)"
    fi
    mkdir -p "${LIVE_ARCHIVE}"
    # Update each file's CONTENT in place (same path, same inode) rather
    # than deleting the old one and creating a new one in its place. This
    # matters if "iMessageExports" lives inside a sync tool's folder (e.g.
    # Nextcloud, Dropbox, iCloud Drive): those treat an in-place content
    # change as one ordinary "file modified" event, but a delete followed
    # by a recreate as two separate destructive ones -- which wastes
    # bandwidth re-uploading unchanged bytes, and can even generate a bogus
    # "conflicted copy" if the delete and recreate straddle a sync cycle.
    # `cat > file` truncates and rewrites the existing inode's content
    # instead of unlinking it, which is exactly the "plain modification"
    # sync tools handle efficiently.
    #
    # But merge_by_contact.py reprocesses the full union of archive + fresh
    # export files every run, so a contact with zero new messages this
    # window still gets a freshly-written (byte-identical) file in staging.
    # Writing that over the live file anyway would touch it on every single
    # run regardless of whether anything actually changed -- relying on the
    # mtime (set to the latest message's timestamp) to coincidentally stay
    # put isn't a real guarantee, since two messages landing in the same
    # conversation within the same second would share a timestamp down to
    # that resolution. Comparing actual bytes first is what's actually
    # correct: if staging's output is byte-identical to what's already
    # live, skip the write (and the mtime touch) entirely -- zero disk I/O,
    # zero sync activity, for threads that genuinely didn't change.
    STAGED_NAMES=()
    for f in "${ARCHIVE_STAGING}"/*.html; do
      [[ -e "${f}" ]] || continue
      name="$(basename "${f}")"
      STAGED_NAMES+=("${name}")
      dest="${LIVE_ARCHIVE}/${name}"
      if [[ -e "${dest}" ]] && cmp -s "${f}" "${dest}"; then
        : # byte-identical to what's already live -- nothing to do
      else
        cat "${f}" > "${dest}"
        touch -r "${f}" "${dest}"   # carry over the "latest message" mtime
      fi
    done
    rm -f "${ARCHIVE_STAGING}"/*.html
    rmdir "${ARCHIVE_STAGING}" 2>/dev/null || rm -rf "${ARCHIVE_STAGING}"
    # A contact/group's winning filename can occasionally change between
    # runs (e.g. a different handle becomes the larger one, or self-handle
    # stripping changes what the consolidated name looks like). When that
    # happens the OLD name genuinely has nothing to update in place -- its
    # content already lives on under the new name, and it was just copied
    # into the dated backup above, so it's safe to remove here rather than
    # leaving a stale duplicate behind under its old name.
    for f in "${LIVE_ARCHIVE}"/*.html; do
      [[ -e "${f}" ]] || continue
      name="$(basename "${f}")"
      found=0
      for staged in "${STAGED_NAMES[@]}"; do
        [[ "${staged}" == "${name}" ]] && { found=1; break; }
      done
      if [[ "${found}" -eq 0 ]]; then
        echo "    removing stale renamed-away file: ${name}"
        rm -f "${f}"
      fi
    done
    echo "    \"iMessageExports\" updated: ${LIVE_ARCHIVE}"
  fi
fi

# ---- attachments: handled entirely by step 1 now ---------------------------
# With -c disabled, imessage-exporter references attachments in place rather
# than copying them -- and imessage-snapshot.sh already synced the current
# live Attachments/StickerCache directly into "iMessageExports" as part of
# step 1 above (additive, healing, never --delete, exactly like it always
# did for WorkingDir/snapshots before). There is nothing left to do here:
# no separate folding step, no per-run attachments subfolder to merge in,
# no backfill-from-history logic needed. Since that sync pulls from the
# LIVE source every single run, it also naturally covers anything that
# existed before this pipeline ever ran, not just what got captured in a
# past export.

# ---- regenerate the top-level index page -------------------------------------
# Lists every chat currently in "iMessageExports", newest-first, linking to
# each. Runs every time (cheap; it's just reading filenames + mtimes, not
# reprocessing message content) so it stays in sync with whatever changed
# above -- including the "removed stale renamed-away file" case. Same
# skip-if-byte-identical approach as the per-chat files, for the same
# sync-tool-friendliness reason: most runs won't actually change the chat
# list or any dates enough to matter, and there's no reason to touch this
# file (or trigger a sync) when nothing in it actually changed.
INDEX_STAGING="${WORKING_DIR}/.index-staging-${STAMP}.html"
if [[ -x "${VENV_PYTHON}" ]]; then
  INDEX_PYTHON_BIN="${VENV_PYTHON}"
else
  INDEX_PYTHON_BIN="python3"
fi
"${INDEX_PYTHON_BIN}" "${GENERATE_INDEX}" "${LIVE_ARCHIVE}" -o "${INDEX_STAGING}" > /dev/null
if [[ -e "${INDEX_OUTPUT}" ]] && cmp -s "${INDEX_STAGING}" "${INDEX_OUTPUT}"; then
  rm -f "${INDEX_STAGING}"
else
  cat "${INDEX_STAGING}" > "${INDEX_OUTPUT}"
  rm -f "${INDEX_STAGING}"
  echo "    index page updated: ${INDEX_OUTPUT}"
fi

# The snapshot just created this run tells us where the NEXT run should
# resume from -- except in --merge-only mode, which never creates a new
# snapshot at all, so the existing latest-completed one (found earlier)
# is still the right answer; nothing about the resume point changes when
# only the merge gets re-run.
if [[ "${MERGE_ONLY}" -eq 0 ]]; then
  NEXT_RUN_FROM_STAMP="$(basename "${SNAP_DIR}")"
else
  NEXT_RUN_FROM_STAMP="$(basename "${LATEST_SNAPSHOT_DIR}")"
fi
NEXT_RUN_FROM_DATE="${NEXT_RUN_FROM_STAMP:0:10}"
echo "    next run will start from $(date -j -v-1d -f "%Y-%m-%d" "${NEXT_RUN_FROM_DATE}" +%Y-%m-%d) (derived from snapshots/${NEXT_RUN_FROM_STAMP})"

# ---- ERASING EXPORTS AND ARCHIVES (or pruning to a smaller count) ----------
# KEEP_ARCHIVES/KEEP_EXPORTS default to 0, meaning "erase the ENTIRE
# archives/ / exports/ folder itself, every single run" -- not just their
# contents. Both are now fully disposable: date-tracking lives entirely on
# snapshots (see NO STATE FILE at the top), never on anything under either
# of these, so there's nothing left here that a future run depends on. DB
# snapshots are the durable record -- an erased export can always be
# regenerated later by re-running imessage-exporter against the matching
# snapshot, and an erased archive backup is just a past state of
# "iMessageExports", reconstructable the same way if it's ever needed
# again. Wiping the whole folder (not just its dated-named contents) also
# cleans up anything else that might be sitting in there, like a stale
# ".staging-<oldstamp>" leftover from a past crashed run -- there's nothing
# valid that lives ONLY in a staging folder, everything in it is already
# durably sourced from the old archive files and the fresh export it was
# built from. Both get recreated fresh (mkdir -p) the next time they're
# actually needed. Pass --keep-archives N / --keep-exports N (N > 0) to
# retain some history instead of erasing everything.
if [[ -n "${KEEP_ARCHIVES}" && "${BOOTSTRAP}" -eq 0 ]]; then
  if [[ "${KEEP_ARCHIVES}" -eq 0 ]]; then
    if [[ -d "${ARCHIVE_ROOT}" ]]; then
      echo "==> Erasing the entire archives folder: ${ARCHIVE_ROOT}"
      rm -rf "${ARCHIVE_ROOT}"
    fi
  else
    ARCHIVE_BACKUPS=($(find "${ARCHIVE_ROOT}" -mindepth 1 -maxdepth 1 -type d -name 'archive-*' | sort))
    N="${#ARCHIVE_BACKUPS[@]}"
    if [[ "${N}" -gt "${KEEP_ARCHIVES}" ]]; then
      TO_REMOVE=$((N - KEEP_ARCHIVES))
      echo "==> Pruning ${TO_REMOVE} old dated backup(s), keeping newest ${KEEP_ARCHIVES}..."
      for ((i = 0; i < TO_REMOVE; i++)); do
        echo "    removing ${ARCHIVE_BACKUPS[$i]}"
        rm -rf "${ARCHIVE_BACKUPS[$i]}"
      done
    fi
  fi
fi

if [[ -n "${KEEP_EXPORTS}" ]]; then
  if [[ "${KEEP_EXPORTS}" -eq 0 ]]; then
    if [[ -d "${RAW_ROOT}" ]]; then
      echo "==> Erasing the entire exports folder: ${RAW_ROOT}"
      rm -rf "${RAW_ROOT}"
    fi
  else
    EXPORT_DIRS=($(find "${RAW_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort))
    N="${#EXPORT_DIRS[@]}"
    if [[ "${N}" -gt "${KEEP_EXPORTS}" ]]; then
      TO_REMOVE=$((N - KEEP_EXPORTS))
      echo "==> Pruning ${TO_REMOVE} old export(s), keeping newest ${KEEP_EXPORTS}..."
      for ((i = 0; i < TO_REMOVE; i++)); do
        echo "    removing ${EXPORT_DIRS[$i]}"
        rm -rf "${EXPORT_DIRS[$i]}"
      done
    fi
  fi
fi

echo
echo "==> Done."
echo "    latest merged archive: ${LIVE_ARCHIVE}"