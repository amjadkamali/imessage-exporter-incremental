#!/usr/bin/env python3
"""
merge_html_exports.py — merge multiple imessage-exporter HTML exports of the
SAME conversation into one deduplicated file, keyed on message GUID.

Two overlapping monthly exports share every message from before the last run;
deduping by GUID collapses that overlap to exactly one copy of each message,
regardless of ROWID, export order, or format churn.

WHY IT'S SELF-CALIBRATING:
imessage-exporter's HTML structure has changed across versions (e.g. the 4.1.0
template rewrite) and the maintainer intentionally provides no stable machine
format. So this tool does NOT hardcode a structure. It first DISCOVERS, from
your actual files, where the GUID and timestamp live, shows you what it found,
and only merges once you've confirmed it looks right.

USAGE:
  # 1. Inspect: see what the tool detects in your real exports (merges nothing)
  python3 merge_html_exports.py --inspect export_jan.html export_apr.html

  # 2. Merge once the detection looks correct:
  python3 merge_html_exports.py -o merged.html export_jan.html export_apr.html

The GUID is a canonical UUID (e.g. 8E1628CD-C7E9-455D-A401-4493B4D86C0F). We
find, per message container, the first UUID-shaped token — whether it sits in
an attribute (data-guid, id, ...) or in the text/comments. That is robust to
class-name changes because it keys on the GUID's SHAPE, not on a fixed tag.
"""
import argparse, re, sys, html, os, subprocess
from datetime import datetime
from pathlib import Path


def _ensure_bs4():
    """
    If beautifulsoup4 isn't importable, create a venv next to this script
    (shared with any sibling script that lives alongside it), install it
    there, and re-exec this script under that venv's Python. No-op if bs4
    is already available, including on the second pass after re-exec.
    """
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

try:
    from bs4 import BeautifulSoup, Comment
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4 --break-system-packages")

# Canonical iMessage GUID shape: 8-4-4-4-12 hex, case-insensitive.
GUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
# ISO-ish timestamp shape (e.g. 2025-06-15T12:00:00 or "2025-06-15 12:00:00").
TS_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
# Human format emitted by imessage-exporter, e.g. "Jul 02, 2026  7:45:05 AM"
# (note the possible double space before the time).
TS_HUMAN_RE = re.compile(
    r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M"
)


def parse_ts_to_key(raw):
    """
    Return a sortable ISO string for a detected timestamp, or "" if none.
    Handles both the human 'Jul 02, 2026  7:45:05 AM' form and ISO forms.
    Falling back to the raw string keeps chronological-ish order even if
    parsing fails, since these formats sort reasonably as text within a format.
    """
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw).strip()
    # Try the human format first.
    m = TS_HUMAN_RE.search(raw)
    if m:
        for fmt in ("%b %d, %Y %I:%M:%S %p",):
            try:
                return datetime.strptime(m.group(0), fmt).isoformat()
            except ValueError:
                pass
    m = TS_ISO_RE.search(raw)
    if m:
        return m.group(0).replace(" ", "T")
    return raw  # last resort: sort on whatever text we found


def find_message_containers(soup):
    """
    Heuristic: a 'message container' is the smallest repeated element that
    contains exactly one GUID. We locate every GUID occurrence, then walk up
    to the nearest ancestor that contains that GUID and no other, treating that
    ancestor as the message unit. Falls back gracefully if structure is flat.
    """
    # Collect all elements/comments/attrs that contain a GUID.
    guid_nodes = []

    # 1) attributes on any tag
    for tag in soup.find_all(True):
        for attr, val in list(tag.attrs.items()):
            valstr = " ".join(val) if isinstance(val, list) else str(val)
            if GUID_RE.search(valstr):
                guid_nodes.append((tag, GUID_RE.search(valstr).group(0)))
                break

    # 2) comments (some exporters emit <!-- guid: ... -->)
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        m = GUID_RE.search(c)
        if m:
            parent = c.parent if c.parent else soup
            guid_nodes.append((parent, m.group(0)))

    # 3) visible text
    if not guid_nodes:
        for el in soup.find_all(True):
            # only leaf-ish text to avoid matching the whole document
            if el.string and GUID_RE.search(el.string):
                guid_nodes.append((el, GUID_RE.search(el.string).group(0)))

    # For each guid node, climb to an ancestor that still contains exactly this
    # one guid, to capture the whole bubble (text + attachments + metadata).
    containers = []
    seen_ids = set()
    for node, guid in guid_nodes:
        cur = node
        best = node
        while cur and cur.parent and cur.parent is not soup:
            parent_guids = set(GUID_RE.findall(str(cur.parent)))
            if parent_guids == {guid}:
                best = cur.parent
                cur = cur.parent
            else:
                break
        key = id(best)
        if key not in seen_ids:
            seen_ids.add(key)
            containers.append((best, guid))
    return containers


def extract_timestamp(container):
    """
    Real imessage-exporter output puts the time in <span class="timestamp">
    inside an <a> whose text is like 'Jul 02, 2026  7:45:05 AM'. Prefer that;
    fall back to a <time datetime> attribute, then any timestamp-shaped text.
    """
    if hasattr(container, "find"):
        span = container.find("span", class_="timestamp")
        if span:
            key = parse_ts_to_key(span.get_text())
            if key:
                return key
        t = container.find("time")
        if t and t.get("datetime"):
            key = parse_ts_to_key(t["datetime"])
            if key:
                return key
    text = container.get_text() if hasattr(container, "get_text") else str(container)
    return parse_ts_to_key(text)


def parse_file(path):
    soup = BeautifulSoup(Path(path).read_text(encoding="utf-8", errors="replace"),
                         "html.parser")
    out = []
    for container, guid in find_message_containers(soup):
        out.append({
            "guid": guid.upper(),
            "ts": extract_timestamp(container),
            "html": str(container),
        })
    return soup, out


def inspect(paths):
    print(f"Inspecting {len(paths)} file(s). No output is written in this mode.\n")
    for p in paths:
        _, msgs = parse_file(p)
        ts_have = sum(1 for m in msgs if m["ts"])
        print(f"  {p}")
        print(f"      messages detected : {len(msgs)}")
        print(f"      with timestamps   : {ts_have}/{len(msgs)}")
        uniq = len({m['guid'] for m in msgs})
        print(f"      unique GUIDs      : {uniq}")
        if msgs:
            s = msgs[0]
            print(f"      sample GUID       : {s['guid']}")
            print(f"      sample timestamp  : {s['ts'] or '(none detected)'}")
            snippet = re.sub(r"\s+", " ", s["html"])[:120]
            print(f"      sample container  : {snippet}...")
        print()
    print("If message counts and GUIDs look right, re-run without --inspect and")
    print("with -o OUTPUT to produce the merged, deduplicated file.")


def merge(paths, out_path):
    merged = {}          # guid -> record (first occurrence wins; identical anyway)
    order_meta = []      # to report per-file contribution
    template_soup = None
    for p in paths:
        soup, msgs = parse_file(p)
        if template_soup is None:
            template_soup = soup   # reuse the first file's shell (head/CSS link)
        new = 0
        for m in msgs:
            if m["guid"] not in merged:
                merged[m["guid"]] = m
                new += 1
        order_meta.append((p, len(msgs), new))

    # Sort by timestamp when present; undated records keep insertion order at end.
    records = list(merged.values())
    dated = [r for r in records if r["ts"]]
    undated = [r for r in records if not r["ts"]]
    dated.sort(key=lambda r: r["ts"])
    ordered = dated + undated

    # Rebuild: keep the first file's <head> (so the style.css link survives),
    # replace the body's message region with the deduped, ordered set.
    body = template_soup.body or template_soup
    # Remove existing message containers from the template body.
    for container, _guid in find_message_containers(template_soup):
        container.extract()
    # Append merged containers, parsed back into nodes.
    for r in ordered:
        frag = BeautifulSoup(r["html"], "html.parser")
        body.append(frag)

    Path(out_path).write_text(str(template_soup), encoding="utf-8")

    total_in = sum(n for _, n, _ in order_meta)
    print(f"Merged {len(paths)} file(s):")
    for p, n, new in order_meta:
        print(f"    {p}: {n} messages, {new} new after dedup")
    print(f"  total messages read : {total_in}")
    print(f"  unique after dedup  : {len(ordered)}")
    print(f"  duplicates removed  : {total_in - len(ordered)}")
    print(f"  dated/undated       : {len(dated)}/{len(undated)}")
    print(f"  written to          : {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Merge imessage-exporter HTML exports, dedup by GUID.")
    ap.add_argument("files", nargs="+", help="HTML export files of the SAME conversation")
    ap.add_argument("-o", "--output", help="output merged HTML path")
    ap.add_argument("--inspect", action="store_true",
                    help="show what GUIDs/timestamps are detected; write nothing")
    args = ap.parse_args()

    for f in args.files:
        if not Path(f).is_file():
            sys.exit(f"Not a file: {f}")

    if args.inspect or not args.output:
        if not args.inspect:
            print("No -o OUTPUT given; running in inspect mode.\n")
        inspect(args.files)
        return
    merge(args.files, args.output)


if __name__ == "__main__":
    main()
