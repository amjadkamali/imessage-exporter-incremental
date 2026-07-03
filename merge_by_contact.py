#!/usr/bin/env python3
"""
merge_by_contact.py — merge imessage-exporter HTML exports that belong to the
SAME contact into one file, reproducing the Messages-app experience where all
of a person's phone numbers and emails appear as one conversation.

WHY THIS IS NEEDED:
iMessage stores a separate chat per handle (each phone number / email), and
imessage-exporter deliberately writes one file per chat (it guarantees a 1:1
chat->file mapping to avoid filename collisions). So one contact reachable at
a number AND an email produces two files. This tool regroups them by contact.

HOW GROUPING IS DECIDED:
Each 1:1 export file is named by its handle (e.g. +15555555555.html). We take
that filename handle, normalize it (digits-only, with the optional leading
North American "1" country code folded off so "+15551234567" and
"5551234567" match; international numbers are kept in full rather than
truncated, so numbers from different countries never collide; emails are
lowercased), and look it up in the macOS Address Book (AddressBook-v22.abcddb).
Files whose handles resolve to the same contact record are merged together.
A file whose handle matches no contact is kept as its own standalone output.

GROUP CHATS ARE HANDLED SEPARATELY AND MORE CONSERVATIVELY:
imessage-exporter names group-chat files by joining every participant's handle
with commas (e.g. +15555555555,friend@example.com.html). A group file is only
merged with another group file if BOTH have the exact same resolved set of
participants — each handle is resolved to its Address Book contact where
possible (so a member whose number/email changed still matches), and falls
back to the normalized handle itself when there's no contact match. A group
with even one different member is treated as a distinct conversation and never
merged. Group chats are never merged into, or with, a 1:1 thread.

If your exporter instead names group chats with an opaque chat ID or a saved
group display name rather than comma-joined handles, this tool won't detect
that file as a group by participant set — but it will simply fall through as
a standalone file (since it won't resolve to any single contact), so it will
never be wrongly merged. Check the --dry-run report to confirm groupings.

OUTPUT NAMING:
Each merged file is named after the handle with the LARGEST file size in the
group, used as a rough proxy for "most messages" (e.g. if the phone thread's
HTML file is bigger than the email thread's, the merged file takes the
phone-number filename). Collisions are disambiguated, never overwritten.

NOTE ON THE SIZE HEURISTIC:
File size is a fast, cheap proxy for message count, but it's not exact —
threads heavy on attachments (photos/videos) can be larger despite having
fewer messages than a pure-text thread. Check the --dry-run output if you
want to sanity-check which filename wins before writing anything.

MERGING:
Delegates to merge_html_exports.py (same directory) for the actual GUID-dedup
+ timestamp-sort. That merger is already tested against real export structure.

USAGE:
  # See the proposed groupings without writing anything (DO THIS FIRST):
  python3 merge_by_contact.py --dry-run -o merged/ export_dir/*.html

  # Once groupings look right:
  python3 merge_by_contact.py -o merged/ export_dir/*.html

  # Point at a specific Address Book if auto-detection misses:
  python3 merge_by_contact.py --addressbook ~/path/AddressBook-v22.abcddb ...
"""
import argparse, os, re, sys, glob, sqlite3
from pathlib import Path

# Reuse the tested merger in the same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import merge_html_exports as mhe
except ImportError:
    sys.exit("merge_html_exports.py must be in the same directory as this tool.")


# ---------- handle normalization ----------
def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    if not d:
        # No digits at all: this isn't a phone number, it's an alphanumeric
        # SMS sender ID (e.g. "WhatsApp" -- common for OTP codes, 
        # business notifications, and promotional texts).
        # Stripping to digits-only would collapse EVERY such sender down to
        # the same empty string, incorrectly treating completely unrelated
        # senders as if they were all the same contact. Use the sender's
        # own text as its identity instead, exactly like an email handle.
        return (s or "").strip().lower()
    # Strip a redundant leading North American country code -- "+15551234567"
    # and "5551234567" are the same real person, commonly written both ways,
    # so folding that specific case together is correct. Every OTHER number
    # is used in full rather than truncated to a fixed last-10-digits
    # window: a UK, German, or other international number blindly cut down
    # to its last 10 digits loses exactly the part of the number that
    # distinguishes it from someone else's, and two unrelated contacts from
    # different countries can end up with the same truncated key -- silently
    # merging them as if they were one person.
    if len(d) == 11 and d[0] == "1":
        return d[1:]
    return d

def norm_email(s):
    return (s or "").strip().lower()

def norm_handle(h):
    return norm_email(h) if "@" in h else norm_phone(h)


# ---------- group-chat detection ----------
def is_group_stem(stem):
    """imessage-exporter joins group-chat participant handles with commas."""
    return "," in stem

GUESSED_NAME_RE = re.compile(r'\d+\s+others?\b', re.IGNORECASE)

def looks_like_guessed_name(stem):
    """
    macOS/imessage-exporter sometimes names a chat using an auto-generated
    summary like "John, Jane & 3 others" when the group has no custom name
    set. That summary is itself a guess about membership -- not a stable
    identifier -- so it can't be trusted as a basis for matching this file
    against any OTHER file by participants or contact. Files with this kind
    of name still merge with each other on an exact filename match (handled
    separately in plan_groups), since two files sharing the identical guessed
    name are still almost certainly the same conversation re-exported over
    time; they just never get matched to a differently-named file the way a
    real group chat or contact would.
    """
    return bool(GUESSED_NAME_RE.search(stem))

def parse_participants(stem):
    return [h.strip() for h in stem.split(",") if h.strip()]

def resolve_identity(handle, handle_map):
    """
    Resolve a single handle to a stable identity for comparison purposes:
    the Address Book person_key if known, otherwise the normalized handle
    itself. Two files referring to the "same person" (e.g. old number vs.
    new number) resolve to the same identity as long as the Address Book
    has both handles on file; otherwise we fall back to exact handle match.
    """
    key = norm_handle(handle)
    person = handle_map.get(key)
    return person[0] if person else f"handle:{key}"


# ---------- macOS Address Book ----------
def default_addressbook_paths():
    """Common locations of the macOS Address Book SQLite DB(s)."""
    base = os.path.expanduser("~/Library/Application Support/AddressBook")
    found = []
    # Top-level DB and per-Source DBs (iCloud/On My Mac/Exchange each have one).
    for pat in (f"{base}/AddressBook-v22.abcddb",
                f"{base}/Sources/*/AddressBook-v22.abcddb"):
        found.extend(glob.glob(pat))
    return found

def build_handle_map(ab_paths):
    """
    Return {normalized_handle: (person_key, display_name)} across all DBs.
    person_key is namespaced by db path so records from different sources
    don't collide on Z_PK. Schema is introspected defensively.
    """
    handle_map = {}
    for dbp in ab_paths:
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        except Exception as e:
            print(f"  (skipping unreadable Address Book: {dbp}: {e})", file=sys.stderr)
            continue
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "ZABCDRECORD" not in tables:
                con.close(); continue
            # Records -> names
            names = {}
            for pk, fn, ln, org in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                nm = " ".join(x for x in (fn, ln) if x) or (org or f"record{pk}")
                names[pk] = nm
            def add(owner, raw):
                if owner is None or not raw:
                    return
                key = norm_handle(raw)
                if key:
                    handle_map[key] = (f"{dbp}#{owner}", names.get(owner, f"record{owner}"))
            if "ZABCDPHONENUMBER" in tables:
                for owner, num in con.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    add(owner, num)
            if "ZABCDEMAILADDRESS" in tables:
                for owner, addr in con.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    add(owner, addr)
        except Exception as e:
            print(f"  (error reading {dbp}: {e})", file=sys.stderr)
        finally:
            con.close()
    return handle_map


# ---------- grouping ----------
def handle_from_filename(path):
    return Path(path).stem   # '+15555555555.html' -> '+15555555555'

def count_messages(path):
    """
    Rough proxy for message count: file size in bytes. Much cheaper than
    parsing the HTML, though threads with lots of attachments can skew
    larger relative to their actual message count.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

def strip_self_handles(participants, my_handles_norm):
    """
    Messages occasionally glitches and inserts your own number/email into a
    group chat's participant list -- a known, occasional bug, not a real
    additional participant. Left alone, this fragments what should be one
    conversation into several different-looking "groups" depending on
    whether the glitch happened to fire on a given export, and can make an
    entirely normal 1:1 conversation look like a 2-person group.

    If every participant is one of your own handles, this is a genuine
    chat-with-yourself and is left completely untouched. Otherwise, your own
    handles are dropped before any matching happens.
    """
    if not my_handles_norm:
        return participants
    filtered = [p for p in participants if norm_handle(p) not in my_handles_norm]
    return filtered if filtered else participants

def resolve_contact_if_single_person(participants, handle_map):
    """
    A comma-joined filename doesn't necessarily mean a multi-person group
    chat. Messages itself sometimes consolidates a single contact's several
    handles (phone + email) into one on-device thread, and imessage-exporter
    names that file the exact same way it names a real group -- all handles
    joined by commas. The two cases need different merge behavior (contact
    matching vs. exact participant-set matching), so they need to be told
    apart before grouping, not assumed from the filename shape alone.

    The distinguishing test: resolve EVERY handle in the (possibly already
    self-handle-stripped) participant list against the Address Book. Only if
    ALL of them resolve, and all to the SAME person, is this treated as a
    contact file. If even one handle fails to resolve, this must NOT be
    assumed to be that same contact -- an unresolved handle is exactly what
    a genuine group chat looks like when it has one saved contact plus other
    participants who simply aren't in the Address Book. (An earlier version
    of this check only required the RESOLVABLE handles to agree, ignoring
    unresolved ones entirely -- which silently merged a contact's solo
    thread with unrelated group chats that merely included them alongside
    other, unsaved numbers. That was wrong; this is the fix.)
    """
    resolved = [handle_map.get(norm_handle(p)) for p in participants]
    if any(r is None for r in resolved):
        return None
    person_keys = {r[0] for r in resolved}
    if len(person_keys) == 1:
        return resolved[0]
    return None

def dedupe_participants_by_identity(participants, handle_map):
    """
    Multiple raw handles in one participant list can belong to the same real
    person -- e.g. someone's phone number AND email both showing up in the
    same group name. The identity resolution used for MATCHING already
    collapses these correctly, but a name built by just joining every raw
    handle would still show that person twice. For naming purposes, keep
    only the first-seen handle per distinct resolved identity.
    """
    seen = set()
    deduped = []
    for p in participants:
        identity = resolve_identity(p, handle_map)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(p)
    return deduped

def plan_groups(files, handle_map, my_handles_norm=frozenset()):
    """
    Returns (contact_groups, group_chat_groups, guessed_name_groups, standalone):
      contact_groups:      list of dicts {person, name, files:[(path,size)], output_name}
                           — 1:1 threads merged by resolved contact. Also
                             includes comma-joined files whose every handle
                             (after stripping any of your own, see
                             strip_self_handles) resolves to this same
                             person, or that end up with only one other
                             participant once your own handle is stripped.
      group_chat_groups:   list of dicts {participants, files:[(path,size)], output_name}
                           — group threads merged only on identical resolved
                             participant sets (after self-handle stripping)
      guessed_name_groups: list of dicts {files:[(path,size)], output_name}
                           — files whose name looks auto-generated ("N others"),
                             merged only on an EXACT filename match. We can't
                             trust that name enough to match on participants or
                             an Address Book contact, but the identical literal
                             filename showing up more than once (e.g. an
                             existing archive file plus a freshly re-exported
                             one for the same chat) is still almost certainly
                             the same conversation over time -- exactly like
                             any other repeated export -- so those still merge.
      standalone: list of (path, size, reason) that were never merged
    """
    one_on_one = []   # (path, handle) -- handle to resolve against Address Book
    group_entries = []   # (path, participants) -- already self-handle-stripped
    guessed_name_files = []
    standalone = []   # (path, size, reason) -- filled in as files are excluded
    multi_handle_contact_files = []   # (path, (person_key, display_name))

    for f in files:
        stem = handle_from_filename(f)
        if looks_like_guessed_name(stem):
            guessed_name_files.append(f)
        elif is_group_stem(stem):
            raw_participants = parse_participants(stem)
            participants = strip_self_handles(raw_participants, my_handles_norm)
            if len(participants) == 1:
                # Stripping your own glitched-in handle left only one other
                # person -- this was never really a group, just a normal
                # 1:1 conversation that happened to get your own number
                # tacked onto its name.
                one_on_one.append((f, participants[0]))
            else:
                contact_match = resolve_contact_if_single_person(participants, handle_map)
                if contact_match:
                    multi_handle_contact_files.append((f, contact_match))
                else:
                    group_entries.append((f, participants))
        else:
            one_on_one.append((f, stem))

    # ---- 1:1 threads: merge by matching handle, resolved or not ------------
    # Group ALL one-on-one files by their normalized handle FIRST, regardless
    # of whether that handle resolves to a saved Address Book contact. Two
    # files sharing the same handle are the same ongoing conversation either
    # way -- ANY conversation with someone not saved in your Address Book
    # needs its existing archive file merged with each fresh export exactly
    # the same way a saved contact's does. Only a handle appearing in
    # exactly one file, resolved or not, is genuinely standalone (there's
    # nothing to merge it with).
    #
    # An earlier version sent every unresolved handle straight to standalone
    # without this grouping step. Since standalone entries are written
    # individually and unique_path() renames on any name collision, this
    # meant a conversation with anyone not in your Address Book NEVER
    # merged across runs at all -- the archive's copy and every fresh
    # export's copy of the same handle just piled up forever as
    # "+1XXXXXXXXXX.html", "+1XXXXXXXXXX_2.html", "_3.html", and so on,
    # with duplicate content, growing without bound on every single run.
    by_handle = {}
    for f, handle in one_on_one:
        key = norm_handle(handle)
        cnt = count_messages(f)
        by_handle.setdefault(key, {"handle": handle, "files": []})
        by_handle[key]["files"].append((f, cnt))

    by_person = {}
    for key, info in by_handle.items():
        person = handle_map.get(key)
        files_sorted = sorted(info["files"], key=lambda t: t[1], reverse=True)
        if len(files_sorted) == 1 and not person:
            path, cnt = files_sorted[0]
            standalone.append((path, cnt, "no contact match"))
            continue
        pkey = person[0] if person else f"handle:{key}"
        name = person[1] if person else info["handle"]
        by_person.setdefault(pkey, {"name": name, "files": []})
        by_person[pkey]["files"].extend(files_sorted)

    # Comma-joined files that turned out to be one contact's own consolidated
    # handles (not a real group) land in the exact same bucket -- so a plain
    # single-handle file for that person and a comma-joined one both merge
    # together normally through the usual contact pathway.
    for f, (pkey, pname) in multi_handle_contact_files:
        cnt = count_messages(f)
        by_person.setdefault(pkey, {"name": pname, "files": []})
        by_person[pkey]["files"].append((f, cnt))

    contact_groups = []
    for pkey, info in by_person.items():
        files_sorted = sorted(info["files"], key=lambda t: t[1], reverse=True)
        top_file = files_sorted[0][0]
        contact_groups.append({
            "person": pkey,
            "name": info["name"],
            "files": files_sorted,
            "output_name": Path(top_file).name,
        })

    # ---- group chats: merge only on identical resolved participant sets ----
    by_participant_set = {}
    for f, participants in group_entries:
        identity_set = frozenset(resolve_identity(p, handle_map) for p in participants)
        cnt = count_messages(f)
        by_participant_set.setdefault(identity_set, []).append((f, cnt, participants))

    group_chat_groups = []
    for identity_set, flist in by_participant_set.items():
        files_sorted = sorted(flist, key=lambda t: t[1], reverse=True)
        top_file, _top_cnt, top_participants = files_sorted[0]
        # Built from the actual (already self-handle-stripped) participant
        # list, not just reused from whichever input file happens to be
        # largest -- otherwise the merged file's name could still literally
        # contain your own number even after it's correctly excluded from
        # matching, since the RAW export file's original name never changes.
        # Also deduplicated by resolved identity, so a person appearing via
        # two handles (e.g. phone + email) in the same raw name shows up
        # only once.
        deduped_participants = dedupe_participants_by_identity(top_participants, handle_map)
        clean_name = ", ".join(deduped_participants) + ".html"
        group_chat_groups.append({
            "participants": identity_set,
            "files": [(f, cnt) for f, cnt, _p in files_sorted],
            "output_name": clean_name,
        })

    # ---- guessed-name files: merge only on exact filename match ----
    by_exact_name = {}
    for f in guessed_name_files:
        name = Path(f).name   # exact filename, no normalization at all
        cnt = count_messages(f)
        by_exact_name.setdefault(name, []).append((f, cnt))

    guessed_name_groups = []
    for name, flist in by_exact_name.items():
        files_sorted = sorted(flist, key=lambda t: t[1], reverse=True)
        guessed_name_groups.append({
            "files": files_sorted,
            "output_name": name,   # always exactly the original name
        })

    return contact_groups, group_chat_groups, guessed_name_groups, standalone


def unique_path(out_dir, name, used):
    """Avoid overwriting: if name taken, insert _merged / _2 / _3..."""
    p = Path(out_dir) / name
    if str(p) not in used and not p.exists():
        used.add(str(p)); return p
    stem, suf = p.stem, p.suffix
    i = 2
    while True:
        cand = Path(out_dir) / f"{stem}_{i}{suf}"
        if str(cand) not in used and not cand.exists():
            used.add(str(cand)); return cand
        i += 1


def write_output(paths, out):
    """
    Copy directly when there's only one input file (nothing to dedup/sort),
    otherwise delegate to merge_html_exports.py. Either way, the output
    file's mtime is set to its most recent message's timestamp rather than
    left as "whenever this script ran" -- mhe.merge() already does this
    itself, and the scroll-to-bottom script is injected here to match too,
    since mhe.merge() does that as part of writing its own output.
    """
    if len(paths) == 1:
        text = Path(paths[0]).read_text(encoding="utf-8", errors="replace")
        text = mhe.inject_scroll_to_bottom(text)
        Path(out).write_text(text, encoding="utf-8")
        try:
            _soup, msgs = mhe.parse_file(str(out))
            dated_ts = [m["ts"] for m in msgs if m["ts"]]
            if dated_ts:
                mhe.set_mtime_from_timestamp(out, max(dated_ts))
        except Exception:
            pass   # best-effort; a timestamp glitch shouldn't fail the merge
    else:
        mhe.merge(paths, str(out))


def expand_my_handles(raw_values, handle_map):
    """
    Each --my-handle value is a literal phone number or email of your own.
    (An earlier version also tried to accept your Address Book contact name
    and expand it to every handle on file for you -- removed because most
    people don't have themselves saved as an actual queryable contact card,
    so it usually had nothing to match against. An explicit list of your own
    handles is the reliable way to do this.)
    """
    return frozenset(norm_handle(h) for h in raw_values)
    return frozenset(result)

ORPHANED_STEM_RE = re.compile(r'^orphaned(_\d+)?$', re.IGNORECASE)

def is_empty_orphaned_file(path):
    """
    imessage-exporter writes "orphaned.html" (and sometimes "orphaned_N.html")
    for messages it couldn't associate with any real conversation. Matches
    both the bare name and any numbered variant -- an earlier version of
    this only matched the numbered form, which missed the far more common
    bare "orphaned.html" entirely. That mattered in a subtle way: if a real,
    non-empty "orphaned.html" already exists in the archive and a NEW empty
    one shows up in a fresh export, the bare name slips through unfiltered,
    collides with the existing file, and unique_path() renames it to
    "orphaned_2.html" -- creating a new junk file every single run. When one
    of these has no actual message content at all (just the usual CSS/head
    boilerplate, "empty" doesn't mean a literal 0-byte file), it's excluded
    entirely rather than treated as its own standalone chat. A file matching
    the name but that DOES have real content is left alone; only genuinely
    empty ones are skipped.
    """
    if not ORPHANED_STEM_RE.match(Path(path).stem):
        return False
    try:
        _soup, msgs = mhe.parse_file(path)
        return len(msgs) == 0
    except Exception:
        return False   # if it can't even be parsed, don't silently drop it

def main():
    ap = argparse.ArgumentParser(description="Merge HTML exports by contact via macOS Address Book.")
    ap.add_argument("files", nargs="+", help="HTML export files (one per handle)")
    ap.add_argument("-o", "--output-dir", required=True, help="directory for merged output")
    ap.add_argument("--addressbook", action="append", default=[],
                    help="path to AddressBook-v22.abcddb (repeatable); auto-detected if omitted")
    ap.add_argument("--my-handle", action="append", default=[],
                    help="your own phone number or email (repeatable). Messages can "
                         "occasionally insert your own handle into a group chat's "
                         "participant list as a glitch; any handle given here gets "
                         "stripped out of a group's participant list before matching, "
                         "unless doing so would leave no participants at all (a genuine "
                         "chat with yourself, left untouched).")
    ap.add_argument("--dry-run", action="store_true",
                    help="show proposed groupings and output names; write nothing")
    args = ap.parse_args()

    files = [f for f in args.files if Path(f).is_file()]
    if not files:
        sys.exit("No input files found.")

    skipped_orphaned = [f for f in files if is_empty_orphaned_file(f)]
    if skipped_orphaned:
        files = [f for f in files if f not in skipped_orphaned]
        print(f"Skipping {len(skipped_orphaned)} empty orphaned_#.html file(s):")
        for f in skipped_orphaned:
            print(f"    {Path(f).name}")
        print()
    if not files:
        sys.exit("No input files left after skipping empty orphaned_#.html files.")

    ab_paths = args.addressbook or default_addressbook_paths()
    if not ab_paths:
        print("WARNING: no Address Book found. Every file will be standalone.", file=sys.stderr)
        print("  Pass --addressbook /path/to/AddressBook-v22.abcddb to enable grouping.", file=sys.stderr)
    else:
        print(f"Using Address Book source(s):")
        for p in ab_paths:
            print(f"    {p}")
    handle_map = build_handle_map(ab_paths) if ab_paths else {}
    print(f"Resolved {len(handle_map)} handle(s) from contacts.\n")

    my_handles_norm = expand_my_handles(args.my_handle, handle_map)
    if my_handles_norm:
        print(f"Stripping {len(my_handles_norm)} of your own handle(s) from group participant lists.\n")

    contact_groups, group_chat_groups, guessed_name_groups, standalone = plan_groups(
        files, handle_map, my_handles_norm)

    # ---- report ----
    # This full preview is genuinely useful for --dry-run (it's the whole
    # point), but printing it on every real run too was pure noise -- the
    # execution loop below announces the exact same information again a
    # moment later as it actually happens. So it's shown only when actually
    # requested, and skipped entirely otherwise.
    if args.dry_run:
        print("PROPOSED MERGES (1:1 threads grouped by contact):")
        if not contact_groups:
            print("    (none — no two files resolved to the same contact)")
        for g in contact_groups:
            total = sum(c for _, c in g["files"])
            print(f"  {g['name']}  ->  {g['output_name']}   [{total:,} bytes total]")
            for i, (path, cnt) in enumerate(g["files"]):
                star = "  <- name source (largest file)" if i == 0 else ""
                print(f"        {Path(path).name}  ({cnt:,} bytes){star}")

        print("\nPROPOSED MERGES (group chats, matched only on identical participant sets):")
        multi_file_groups = [g for g in group_chat_groups if len(g["files"]) > 1]
        if not multi_file_groups:
            print("    (none — no two group-chat files shared the exact same participants)")
        for g in multi_file_groups:
            total = sum(c for _, c in g["files"])
            print(f"  [group of {len(g['participants'])}]  ->  {g['output_name']}   [{total:,} bytes total]")
            for path, cnt in g["files"]:
                print(f"        {Path(path).name}  ({cnt:,} bytes)")

        print("\nPROPOSED MERGES (auto-generated names like \"N others\", matched by exact filename only):")
        multi_guessed_groups = [g for g in guessed_name_groups if len(g["files"]) > 1]
        if not multi_guessed_groups:
            print("    (none — no two files shared the exact same guessed name)")
        for g in multi_guessed_groups:
            total = sum(c for _, c in g["files"])
            print(f"  {g['output_name']}   [{total:,} bytes total]")
            for path, cnt in g["files"]:
                print(f"        {Path(path).name}  ({cnt:,} bytes)")

        single_file_groups = [g for g in group_chat_groups if len(g["files"]) == 1]
        single_guessed_groups = [g for g in guessed_name_groups if len(g["files"]) == 1]
        print("\nSTANDALONE (no match — passed through unchanged):")
        if not standalone and not single_file_groups and not single_guessed_groups:
            print("    (none)")
        for path, cnt, reason in standalone:
            print(f"        {Path(path).name}  ({cnt:,} bytes)  [{reason}]")
        for g in single_file_groups:
            path, cnt = g["files"][0]
            print(f"        {Path(path).name}  ({cnt:,} bytes)  [group chat, no matching duplicate]")
        for g in single_guessed_groups:
            path, cnt = g["files"][0]
            print(f"        {Path(path).name}  ({cnt:,} bytes)  [guessed name, no matching duplicate]")

        print("\n[dry-run] Nothing written. Re-run without --dry-run to produce files.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    used = set()
    # A line prints BEFORE each REAL merge starts (not just after it
    # finishes), so if one is slow, it's visible which one -- rather than
    # output just stopping with no indication of what's currently running.
    # The full input path list is shown too, not just a count -- stdout can
    # only ever show one "wrote" line per group when it's done, so if a
    # group is actually merging more than one file, this is the only place
    # that's visible before the (possibly long) merge finishes.
    #
    # A single-file group has nothing to actually merge -- it's a contact
    # or group with no new messages this run, just passing through
    # unchanged. On a real archive most groups fall into this category most
    # runs, so announcing each one exactly like a real merge would mean a
    # wall of identical-looking noise every single time regardless of
    # whether anything happened. Those are counted instead and folded into
    # one summary line at the end; only groups where a merge actually
    # happened (2+ files) get the full verbose treatment.
    quiet_count = 0
    for g in contact_groups:
        paths = [p for p, _ in g["files"]]
        out = unique_path(args.output_dir, g["output_name"], used)
        if len(paths) > 1:
            print(f"  merging {g['name']} ({len(paths)} file(s)):")
            for p in paths:
                print(f"      {p}")
            write_output(paths, out)
            print(f"  wrote {out}  ({g['name']})")
        else:
            write_output(paths, out)
            quiet_count += 1
    # Merge group-chat clusters (including size-1 clusters, to normalize).
    for g in group_chat_groups:
        paths = [p for p, _ in g["files"]]
        out = unique_path(args.output_dir, g["output_name"], used)
        if len(paths) > 1:
            print(f"  merging {out.name} ({len(paths)} file(s)):")
            for p in paths:
                print(f"      {p}")
            write_output(paths, out)
            print(f"  wrote {out}  (group chat)")
        else:
            write_output(paths, out)
            quiet_count += 1
    # Merge guessed-name clusters (exact filename match only).
    for g in guessed_name_groups:
        paths = [p for p, _ in g["files"]]
        out = unique_path(args.output_dir, g["output_name"], used)
        if len(paths) > 1:
            print(f"  merging {out.name} ({len(paths)} file(s)):")
            for p in paths:
                print(f"      {p}")
            write_output(paths, out)
            print(f"  wrote {out}  (guessed name)")
        else:
            write_output(paths, out)
            quiet_count += 1
    for path, _cnt, _reason in standalone:
        out = unique_path(args.output_dir, Path(path).name, used)
        write_output([path], out)
        quiet_count += 1

    if quiet_count:
        print(f"  {quiet_count} file(s) had nothing new -- passed through unchanged.")

    print(f"\nDone. Merged files in {args.output_dir}")


if __name__ == "__main__":
    main()