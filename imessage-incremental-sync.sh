#!/usr/bin/env bash
#
# imessage-incremental-sync.sh — orchestrates the remaining two tools into
# one incremental update:
#
#   1. imessage-snapshot.sh   — read-only copy of chat.db + synced attachments
#   2. imessage-exporter      — export only messages since the last run,
#                               straight into a dated subfolder
#
# WHAT THIS SCRIPT NO LONGER DOES, AND WHY:
# Contact-handle consolidation (someone's phone number and email folded into
# one conversation) and cross-run message deduplication both now live in the
# web app's own database layer instead of happening here at the file level --
# see conversation_contact_group / populate_contact_groups() in indexer.py,
# and the existing cross-archive GUID dedup it already had. That means this
# script no longer needs to merge anything into an existing file at all: each
# run's export just lands in its own new, untouched, permanent dated folder,
# and the app reconciles everything else at index time. merge_by_contact.py
# and merge_html_exports.py are no longer invoked by this script.
#
# LAYOUT:
#
#   ROOT/
#     iMessageExports/            <- TOP LEVEL, user-facing, PERSISTENT
#       Attachments/  StickerCache/   shared, additive stores synced directly
#                                     here by imessage-snapshot.sh every run
#                                     (see -c disabled note below) -- the
#                                     actual attachment bytes travel with
#                                     this folder, not just the HTML.
#       Contacts/                 <- Address Book cache the web app reads
#                                     for contact-handle grouping (see the
#                                     refresh step below for why this can't
#                                     just read the live system path itself)
#       2026-07-03_233450/        <- one folder per RUN of this script (same
#       2026-07-04_071623/            timestamp format as the DB snapshots),
#                                     containing that run's raw
#                                     imessage-exporter output and nothing
#                                     else. Never rewritten by a later run --
#                                     each one is a permanent, untouched
#                                     record. Always a fresh, uniquely-named
#                                     folder, even for a second run on the
#                                     same day -- nothing ever gets appended
#                                     into an existing folder, so there's no
#                                     dependency on imessage-exporter's own
#                                     append-vs-overwrite behavior for an
#                                     already-populated output directory.
#     WorkingDir/
#       imessage-snapshot.sh
#       snapshots/                <- DB snapshots only. One folder per run,
#         2026-07-03_233450/          chat.db + sidecars. Never erased by
#                                     this script -- see NO STATE FILE below.
#
# NO STATE FILE — the "date we last exported through" is read directly off
# the name of the newest COMPLETED folder in WorkingDir/snapshots, rather
# than tracked separately. One less thing to get out of sync with reality;
# the folder names themselves ARE the history.
#
# SENTINEL FILE GUARDS AGAINST TRUSTING A CRASHED RUN:
# A snapshot only counts as "the last one" if it contains a .complete marker,
# written only after imessage-exporter ALSO exits successfully (not just the
# snapshot itself). Without this, a run interrupted partway through (crash,
# killed process, disk full) would still leave its dated snapshot behind; the
# NEXT run would trust that folder's timestamp as fully covering that window
# and only look one day further back -- silently creating a permanent gap
# for whatever didn't finish exporting, with no error anywhere. An
# incomplete snapshot without the marker is simply skipped when looking for
# "the latest" (it's left on disk, not deleted, in case you want to inspect
# what did make it out).
#
# RETENTION: dated export folders and DB snapshots are both kept forever by
# this script -- there's no pruning here anymore. Each dated folder is now
# the permanent record of that window (not a disposable intermediate the way
# the old exports/ staging folder was), and it's small (text HTML only, no
# attachment bytes duplicated into it) even accumulated over years. If you
# want retention limits later, that's a separate, deliberate feature to add,
# not something this script does implicitly.
#
# USAGE:
#   ./imessage-incremental-sync.sh                  # normal incremental run
#   ./imessage-incremental-sync.sh --since 2024-01-01   # first-run bootstrap
#   ./imessage-incremental-sync.sh --full           # force full re-export
#   ./imessage-incremental-sync.sh --dry-run        # show the plan, do nothing
#
# This script targets macOS (BSD `date`, ~/Library/Messages, Address Book) —
# same platform assumption as imessage-snapshot.sh and imessage-exporter.
set -euo pipefail

# ---- configuration ----------------------------------------------------------
# This script lives inside WorkingDir. ROOT is WorkingDir's parent, where the
# top-level "iMessageExports" folder sits alongside it. Override either
# independently if you ever need to (e.g. testing).
WORKING_DIR="${IMESSAGE_WORKING_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ROOT="${IMESSAGE_ROOT:-$(dirname "${WORKING_DIR}")}"

SNAPSHOT_SCRIPT="${WORKING_DIR}/imessage-snapshot.sh"

SNAPSHOT_ROOT="${WORKING_DIR}/snapshots"   # passed to imessage-snapshot.sh as $1
LIVE_ARCHIVE="${ROOT}/iMessageExports"     # top-level, user-facing export root
LOCK_DIR="${WORKING_DIR}/.sync.lock"

STAMP="$(date +%Y-%m-%d_%H%M%S)"           # same format as imessage-snapshot.sh's own
RUN_DIR="${LIVE_ARCHIVE}/${STAMP}"         # this run's export lands directly here -- always a fresh folder, never reused
# Guards against the rare case of two runs landing in the exact same
# wall-clock second (a manual run colliding with a scheduled one, say):
# without this, a second run computing the identical stamp would start
# exporting into a folder the first run already populated, which is
# exactly the "depends on imessage-exporter's append behavior" situation
# a unique-per-run folder is meant to avoid in the first place.
if [[ -e "${RUN_DIR}" ]]; then
  SUFFIX=2
  while [[ -e "${LIVE_ARCHIVE}/${STAMP}_${SUFFIX}" ]]; do
    SUFFIX=$((SUFFIX + 1))
  done
  RUN_DIR="${LIVE_ARCHIVE}/${STAMP}_${SUFFIX}"
fi

# ---- argument parsing --------------------------------------------------------
SINCE_OVERRIDE=""
FORCE_FULL=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      SINCE_OVERRIDE="$2"; shift 2 ;;
    --full)
      FORCE_FULL=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    *)
      echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "${SINCE_OVERRIDE}" && ! "${SINCE_OVERRIDE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "ERROR: --since must be YYYY-MM-DD, got: ${SINCE_OVERRIDE}" >&2
  exit 1
fi

# ---- preflight ---------------------------------------------------------------
if [[ ! -f "${SNAPSHOT_SCRIPT}" ]]; then
  echo "ERROR: expected to find $(basename "${SNAPSHOT_SCRIPT}") in ${WORKING_DIR}" >&2
  exit 1
fi
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

mkdir -p "${SNAPSHOT_ROOT}"

# Prevent two runs from stomping on each other (e.g. an overlapping
# cron/launchd invocation).
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "ERROR: another run appears to be in progress (${LOCK_DIR} exists)." >&2
  echo "  If that's stale (a previous run crashed), remove it manually and retry." >&2
  exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

# ---- compute the export window ----------------------------------------------
# No end date at all: the export runs against a frozen SNAPSHOT of chat.db,
# which by definition can never contain a message timestamped after the
# moment the snapshot was taken. An upper bound couldn't exclude anything
# even if given one -- there's nothing in a frozen snapshot that ever
# exceeds "now" for it to cut off. Only -s (start date) is meaningful here.
#
# Start date is (date of the last completed snapshot - 1 day): the
# requested one-day overlap buffer, so a message right at yesterday's
# boundary can never be missed. This matters more than a same-machine
# same-timezone analysis alone suggests: if the Mac's system timezone
# changes between runs (traveling, not just DST), a date boundary computed
# under one timezone doesn't reliably land the same way once the next run
# filters under a different one -- the extra day of margin absorbs that
# shift instead of risking a silently missed message right at the edge.
# Any overlap that lands in the same file as a previous run just dedupes
# away at index time in the app now, rather than during a merge step here.

# Find the most recent COMPLETED snapshot -- its name IS the state, no
# separate file to track or drift out of sync with reality. Only folders
# with the .complete sentinel count (see SENTINEL FILE note above); an
# interrupted run's snapshot is left alone but not trusted.
LATEST_SNAPSHOT_DIR="$(find "${SNAPSHOT_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' \
  -exec test -e '{}/.complete' \; -print 2>/dev/null | sort | tail -1)"

BOOTSTRAP=0
if [[ "${FORCE_FULL}" -eq 1 ]]; then
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

echo "==> Plan:"
echo "      window:       ${START_DATE:-<full history>}  onward (no end date)"
echo "      output:       ${RUN_DIR}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[dry-run] Stopping here. Nothing was snapshotted or exported."
  exit 0
fi

# ---- step 1: read-only DB snapshot + synced attachments ---------------------
echo
echo "==> [1/3] Snapshotting chat.db (read-only) and syncing attachments..."
# Second argument tells imessage-snapshot.sh to put the shared
# Attachments/StickerCache stores directly inside "iMessageExports" itself,
# rather than alongside the dated DB snapshots. That's what lets -c disabled
# below reference them in place -- and since this is the folder actually
# being synced (e.g. to Nextcloud), the referenced files travel with it
# instead of only existing on this one Mac.
"${SNAPSHOT_SCRIPT}" "${SNAPSHOT_ROOT}" "${LIVE_ARCHIVE}"

# Find the snapshot subdirectory that command just created. imessage-snapshot.sh
# generates its own STAMP internally; newest one sorts last lexicographically.
SNAP_DIR="$(find "${SNAPSHOT_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -1)"
if [[ -z "${SNAP_DIR}" || ! -f "${SNAP_DIR}/chat.db" ]]; then
  echo "ERROR: could not locate the snapshot's chat.db under ${SNAPSHOT_ROOT}" >&2
  exit 1
fi
echo "    using snapshot: ${SNAP_DIR}/chat.db"

# ---- step 2: refresh the Address Book cache, BEFORE exporting ---------------
# Done before the export runs (not after) specifically so imessage-exporter
# itself can be pointed at this same local copy via -n/--contacts-path
# below, rather than relying on its own automatic detection of the live
# system path. imessage-exporter does its own contact-name resolution
# during export (populating sender names in the HTML it writes), and that
# resolution is subject to the exact same TCC restriction as everything
# else in this pipeline that touches Contacts -- when this script runs via
# the automated launchd job, imessage-exporter's own attempt to read the
# live Address Book would silently fail too, for the identical reason
# nothing else here can get a manual grant for Contacts access. Handing it
# an explicit, already-cached, ordinary file to read sidesteps that
# entirely, the same way the web app's own indexer does.
#
# This cache also lives inside "iMessageExports/Contacts" so it's visible
# from the same mounted volume the web app's container already reads
# ARCHIVE_ROOT from -- point its ADDRESSBOOK_CACHE_DIR at this same path
# and it picks this up with no separate mount needed.
#
# Refreshed from the live system copy whenever this process can actually
# read it, then whatever ends up in the cache (freshly updated or not) is
# used either way -- by imessage-exporter just below, and later by the web
# app when it next indexes. That live-system read is expected to fail on
# automated runs and succeed whenever this script is run with access (e.g.
# manually from Terminal, which already has whatever access you use
# Contacts.app with); either way, this step always leaves something usable
# in place if anything has ever succeeded before.
echo
echo "==> [2/3] Refreshing Address Book cache..."
ADDRESSBOOK_CACHE_DIR="${LIVE_ARCHIVE}/Contacts"
mkdir -p "${ADDRESSBOOK_CACHE_DIR}"

_refresh_one_addressbook_source() {
  local src="$1" dest="$2" tmp="${2}.tmp.$$"
  # No -r pre-check here on purpose: bash's -r test on macOS reflects
  # ordinary Unix permission bits, and it's not fully clear that reflects
  # whether TCC would actually block the real read -- TCC enforcement
  # generally happens at the actual open()/read() syscall level, not the
  # permission-bit level a lightweight check like that inspects. Relying
  # solely on cp's own exit code below means the thing actually being
  # trusted is a genuine attempt at the real read, not a proxy for it.
  cp "${src}" "${tmp}" 2>/dev/null || { rm -f "${tmp}"; return 1; }
  # Belt and suspenders: a real Address Book database is never legitimately
  # empty, so if cp somehow exited 0 without actually transferring
  # anything (an edge case, not the expected failure mode, but cheap to
  # guard against), treat that the same as an outright failure rather than
  # promoting a zero-byte file into the cache.
  [[ -s "${tmp}" ]] || { rm -f "${tmp}"; return 1; }
  [[ -f "${src}-wal" ]] && cp "${src}-wal" "${tmp}-wal" 2>/dev/null
  [[ -f "${src}-shm" ]] && cp "${src}-shm" "${tmp}-shm" 2>/dev/null
  mv "${tmp}" "${dest}"
  [[ -f "${tmp}-wal" ]] && mv "${tmp}-wal" "${dest}-wal"
  [[ -f "${tmp}-shm" ]] && mv "${tmp}-shm" "${dest}-shm"
  return 0
}

ADDRESSBOOK_SRC_BASE="$HOME/Library/Application Support/AddressBook"
if _refresh_one_addressbook_source \
     "${ADDRESSBOOK_SRC_BASE}/AddressBook-v22.abcddb" \
     "${ADDRESSBOOK_CACHE_DIR}/AddressBook-v22.abcddb"; then
  echo "    refreshed address book cache from system"
else
  echo "    could not read system address book directly (expected on automated runs); using cached copy if available"
fi
shopt -s nullglob
for ab_src in "${ADDRESSBOOK_SRC_BASE}/Sources"/*/AddressBook-v22.abcddb; do
  ab_source_id=$(basename "$(dirname "${ab_src}")")
  mkdir -p "${ADDRESSBOOK_CACHE_DIR}/Sources/${ab_source_id}"
  _refresh_one_addressbook_source \
    "${ab_src}" "${ADDRESSBOOK_CACHE_DIR}/Sources/${ab_source_id}/AddressBook-v22.abcddb"
done
shopt -u nullglob

# ---- step 3: export straight into this run's own timestamped folder --------
# -p and -r point at two different directories (chat.db lives under
# WorkingDir/snapshots/<stamp>, but Attachments/StickerCache live under
# "iMessageExports"), so both are given as absolute paths.
#
# -c disabled: reference attachments in place instead of copying and
# converting them. Chosen deliberately so the actual attachment bytes live
# inside the synced folder rather than being duplicated into a copy that
# only exists on this Mac.
#
# -n points imessage-exporter at the SAME cached Address Book copy step 2
# just refreshed, instead of letting it fall back to its own automatic
# detection of the live system path -- which would hit the identical TCC
# restriction on an automated run that everything else here already works
# around. Only added if that cache file actually exists yet (e.g. this
# could be the very first run, before anything has ever successfully
# refreshed it); if it doesn't, this is simply omitted and imessage-exporter
# falls back to its own default behavior, exactly as before this change.
#
# Output goes directly into RUN_DIR, timestamped the same way as the DB
# snapshot -- no separate staging/exports folder anymore, and no reliance
# on imessage-exporter's own append-vs-overwrite behavior for an
# already-populated directory either: since RUN_DIR is unique to the
# second, imessage-exporter always starts writing into a brand new, empty
# folder, full stop.
echo
echo "==> [3/3] Exporting messages (${START_DATE:-<full history>} onward) into ${RUN_DIR}..."
mkdir -p "${RUN_DIR}"
EXPORTER_ARGS=(-f html -c disabled -p "${SNAP_DIR}/chat.db" -r "${LIVE_ARCHIVE}" -a macOS -o "${RUN_DIR}")
if [[ -n "${START_DATE}" ]]; then
  EXPORTER_ARGS+=(-s "${START_DATE}")
fi
if [[ -f "${ADDRESSBOOK_CACHE_DIR}/AddressBook-v22.abcddb" ]]; then
  EXPORTER_ARGS+=(-n "${ADDRESSBOOK_CACHE_DIR}/AddressBook-v22.abcddb")
fi
imessage-exporter "${EXPORTER_ARGS[@]}"

# -c disabled embeds the ABSOLUTE filesystem path it resolved each
# attachment to (confirmed against imessage-exporter's own docs: "Attachments
# are not copied; the export references them in-place by filesystem path")
# -- there's no built-in option for relative paths. But since attachments
# and this dated export folder both live under the same "iMessageExports"
# tree, stripping that known absolute prefix turns each reference into a
# portable relative path instead of one that only resolves on this Mac --
# which matters once this folder is synced elsewhere (Nextcloud, another
# device, etc.). Plain string replacement is enough (no HTML parsing
# needed), so this only ever needed the standard library, not a venv.
python3 - "${RUN_DIR}" "${LIVE_ARCHIVE}" <<'PYEOF'
import sys
from pathlib import Path
today_dir, live_archive = sys.argv[1], sys.argv[2]
prefix = live_archive.rstrip("/") + "/"
for f in Path(today_dir).glob("*.html"):
    text = f.read_text(encoding="utf-8", errors="replace")
    if prefix in text:
        f.write_text(text.replace(prefix, ""), encoding="utf-8")
PYEOF

# Only reached if the exporter above exited 0 (set -e aborts otherwise), so
# this run is safe to trust as "fully covers this window" on future runs.
# Written into the SNAPSHOT folder (never erased by this script), not the
# dated export folder, purely to keep the two concerns separate -- the
# dated folder's own existence is now itself a permanent record, but the
# specific "which snapshot completed this" bookkeeping still belongs here.
{
  echo "start=${START_DATE:-<full history>}"
  echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${SNAP_DIR}/.complete"

RAW_COUNT="$(find "${RUN_DIR}" -maxdepth 1 -name '*.html' | wc -l | tr -d ' ')"
echo "    ${RAW_COUNT} file(s) in ${RUN_DIR}"

# ---- attachments: handled entirely by step 1 -------------------------------
# With -c disabled, imessage-exporter references attachments in place rather
# than copying them -- and imessage-snapshot.sh already synced the current
# live Attachments/StickerCache directly into "iMessageExports" as part of
# step 1 above (additive, healing, never --delete). There is nothing left to
# do here: no per-run attachments subfolder to merge in, no backfill-from-
# history logic needed. Since that sync pulls from the LIVE source every
# single run, it also naturally covers anything that existed before this
# pipeline ever ran, not just what got captured in a past export.

echo
echo "    next run will start from $(date -j -v-1d -f "%Y-%m-%d" "$(basename "${SNAP_DIR}" | cut -c1-10)" +%Y-%m-%d) (derived from snapshots/$(basename "${SNAP_DIR}"))"

echo
echo "==> Done."
echo "    today's export: ${RUN_DIR}"
echo "    iMessageExports: ${LIVE_ARCHIVE}"