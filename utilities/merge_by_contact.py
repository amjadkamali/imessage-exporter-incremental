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
Each export file is named by its handle (e.g. +15551234567.html). We take that
filename handle, normalize it (digits-only last-10 for phones, lowercased for
emails), and look it up in the macOS Address Book (AddressBook-v22.abcddb).
Files whose handles resolve to the same contact record are merged together.
A file whose handle matches no contact is kept as its own standalone output.

OUTPUT NAMING:
Each merged file is named after the handle with the MOST messages in the group
(e.g. if the phone thread has more messages than the email thread, the merged
file takes the phone-number filename). Collisions are disambiguated, never
overwritten.

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
import argparse, os, re, sys, glob, sqlite3, subprocess
from pathlib import Path


def _ensure_bs4():
    """Same self-healing venv bootstrap as merge_html_exports.py — see there
    for details. Runs here too since this file imports that module below,
    and the import needs bs4 to already be resolvable."""
    try:
        import bs4  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_BS4_VENV_BOOTSTRAPPED"):
        sys.exit("beautifulsoup4 still not importable after venv setup — "
                 "install manually: pip install beautifulsoup4 --break-system-packages")

    venv_dir = Path(__file__).resolve().parent / ".venv"
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")

    if not venv_python.exists():
        print(f"beautifulsoup4 not found. Creating a virtual environment at {venv_dir} ...",
              file=sys.stderr)
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
            print("Installing beautifulsoup4 into it...", file=sys.stderr)
            subprocess.run([str(venv_python), "-m", "pip", "install", "--quiet", "beautifulsoup4"],
                           check=True)
        except subprocess.CalledProcessError:
            sys.exit("Could not create the venv or install beautifulsoup4 automatically "
                     "(no network access?). Install manually:\n"
                     "  pip install beautifulsoup4 --break-system-packages")
        print("Done. Re-running under the virtual environment...\n", file=sys.stderr)

    env = os.environ.copy()
    env["_BS4_VENV_BOOTSTRAPPED"] = "1"
    os.execve(str(venv_python), [str(venv_python)] + sys.argv, env)


_ensure_bs4()

# Reuse the tested merger in the same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import merge_html_exports as mhe
except ImportError:
    sys.exit("merge_html_exports.py must be in the same directory as this tool.")


# ---------- handle normalization ----------
def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d   # last 10 digits = canonical key

def norm_email(s):
    return (s or "").strip().lower()

def norm_handle(h):
    return norm_email(h) if "@" in h else norm_phone(h)


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
    return Path(path).stem   # '+15551234567.html' -> '+15551234567'

# Count messages by counting UNIQUE message GUIDs in the raw text. This avoids
# building a DOM for planning (huge threads are tens of MB and slow to parse).
# Uses the SAME GUID shape the merger keys on, so the count matches the merge.
_GUID_COUNT_RE = mhe.GUID_RE

def count_messages(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0
    return len(set(_GUID_COUNT_RE.findall(text)))

def plan_groups(files, handle_map):
    """
    Returns (groups, standalone):
      groups: list of dicts {person, name, files:[(path,count)], output_name}
      standalone: list of (path, count) with no contact match
    """
    by_person = {}
    standalone = []
    for f in files:
        h = handle_from_filename(f)
        key = norm_handle(h)
        person = handle_map.get(key)
        cnt = count_messages(f)
        if person:
            by_person.setdefault(person[0], {"name": person[1], "files": []})
            by_person[person[0]]["files"].append((f, cnt))
        else:
            standalone.append((f, cnt))

    groups = []
    for pkey, info in by_person.items():
        files_sorted = sorted(info["files"], key=lambda t: t[1], reverse=True)
        # Output name = filename of the handle with the most messages.
        top_file = files_sorted[0][0]
        groups.append({
            "person": pkey,
            "name": info["name"],
            "files": files_sorted,
            "output_name": Path(top_file).name,
        })
    return groups, standalone


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


def main():
    ap = argparse.ArgumentParser(description="Merge HTML exports by contact via macOS Address Book.")
    ap.add_argument("files", nargs="+", help="HTML export files (one per handle)")
    ap.add_argument("-o", "--output-dir", required=True, help="directory for merged output")
    ap.add_argument("--addressbook", action="append", default=[],
                    help="path to AddressBook-v22.abcddb (repeatable); auto-detected if omitted")
    ap.add_argument("--dry-run", action="store_true",
                    help="show proposed groupings and output names; write nothing")
    args = ap.parse_args()

    files = [f for f in args.files if Path(f).is_file()]
    if not files:
        sys.exit("No input files found.")

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

    groups, standalone = plan_groups(files, handle_map)

    # ---- report ----
    print("PROPOSED MERGES (grouped by contact):")
    if not groups:
        print("    (none — no two files resolved to the same contact)")
    for g in groups:
        merged = sum(c for _, c in g["files"])
        print(f"  {g['name']}  ->  {g['output_name']}   [{merged} messages total]")
        for path, cnt in g["files"]:
            star = "  <- name source (most messages)" if Path(path).name == g["output_name"] else ""
            print(f"        {Path(path).name}  ({cnt} msgs){star}")
    print("\nSTANDALONE (no contact match — passed through unchanged):")
    if not standalone:
        print("    (none)")
    for path, cnt in standalone:
        print(f"        {Path(path).name}  ({cnt} msgs)")

    if args.dry_run:
        print("\n[dry-run] Nothing written. Re-run without --dry-run to produce files.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    used = set()
    print()
    # Merge each multi-file group; pass through singletons/standalone as-is.
    for g in groups:
        paths = [p for p, _ in g["files"]]
        out = unique_path(args.output_dir, g["output_name"], used)
        if len(paths) == 1:
            # Single file for this contact: still run through merge to normalize,
            # but it's effectively a copy.
            mhe.merge(paths, str(out))
        else:
            mhe.merge(paths, str(out))
        print(f"  wrote {out}  ({g['name']})")
    for path, _cnt in standalone:
        out = unique_path(args.output_dir, Path(path).name, used)
        mhe.merge([path], str(out))   # normalize + copy
        print(f"  wrote {out}  (standalone)")

    print(f"\nDone. Merged files in {args.output_dir}")


if __name__ == "__main__":
    main()
