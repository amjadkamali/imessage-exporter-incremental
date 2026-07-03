#!/usr/bin/env python3
"""
generate_index.py — build an index.html listing every chat in iMessageExports,
newest-first, linking to each one.

Reuses merge_by_contact.py's own Address Book resolution so a chat's
filename (which is often just a raw phone number, or several comma-joined
handles for a group) gets shown as an actual contact name wherever the
Address Book has one on file, rather than the raw handle.

USAGE:
  python3 generate_index.py /path/to/iMessageExports -o /path/to/index.html
  python3 generate_index.py /path/to/iMessageExports -o out.html --addressbook /path/to/AddressBook-v22.abcddb
"""
import argparse, re, sys
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import merge_by_contact as mbc
except ImportError:
    sys.exit("merge_by_contact.py must be in the same directory as this tool.")


GUESSED_TAIL_RE = re.compile(r'^(.*?)(\s*[&,]?\s*\d+\s+others?\s*)$', re.IGNORECASE)

def resolve_display_name(stem, handle_map):
    """
    Build a human-friendly name for one exported chat's filename stem,
    resolving whichever handle(s) it contains against the Address Book
    where possible. Falls back to the raw handle text for anything that
    doesn't resolve, so a group with one saved contact and one unsaved
    number still shows a mix of a real name and a raw handle rather than
    failing entirely.
    """
    if mbc.looks_like_guessed_name(stem):
        # The "and N others" summary itself is unreliable (that's exactly
        # why merge_by_contact.py never trusts it for matching), but
        # whatever's listed BEFORE that summary is often still real,
        # resolvable handles -- just because the exporter gave up
        # naming everyone doesn't mean it gave up naming anyone. Resolve
        # those explicitly-listed pieces for display only; this has no
        # effect on which files get merged together, only how this one
        # entry gets shown.
        m = GUESSED_TAIL_RE.match(stem)
        if m:
            explicit_part, tail = m.group(1), m.group(2)
            pieces = [p.strip() for p in explicit_part.split(",") if p.strip()]
            resolved_pieces = []
            for p in pieces:
                person = handle_map.get(mbc.norm_handle(p))
                resolved_pieces.append(person[1] if person else p)
            return ", ".join(resolved_pieces) + tail.rstrip()
        return stem
    parts = mbc.parse_participants(stem) if mbc.is_group_stem(stem) else [stem]
    names = []
    for p in parts:
        person = handle_map.get(mbc.norm_handle(p))
        names.append(person[1] if person else p)
    # Collapse consecutive duplicate names -- e.g. a contact's own phone
    # and email both resolving to the same person shouldn't show up twice.
    deduped = []
    for n in names:
        if not deduped or deduped[-1] != n:
            deduped.append(n)
    return ", ".join(deduped)


PAGE_TEMPLATE = """<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iMessage Exports</title>
<style>
:root {{
    --border-radius: 25px;
    --message-padding: 15px;
    --opacity-medium: 0.6;
    --opacity-high: 0.75;
    --imessage-blue: #1982FC;
    --received-gray: #d8d8d8;
    --background-color: transparent;
    --text-color: black;
    --muted-text: dimgray;
}}

body {{
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
    background: var(--background-color);
    color: var(--text-color);
    margin: 0;
    padding: 3vh 4vw;
}}

h1 {{
    font-weight: 600;
    margin: 0 0 4px 0;
}}

.meta {{
    color: var(--muted-text);
    opacity: var(--opacity-medium);
    margin-bottom: 3vh;
    font-size: 0.9em;
}}

.chat-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 700px;
}}

.chat-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    background-color: var(--received-gray);
    border-radius: var(--border-radius);
    padding: var(--message-padding) 25px;
    text-decoration: none;
    color: var(--text-color);
}}

.chat-row:hover {{
    background-color: var(--imessage-blue);
    color: white;
}}

.chat-row:hover .chat-date {{
    color: white;
    opacity: 0.85;
}}

.chat-name {{
    font-weight: 600;
    overflow-wrap: anywhere;
}}

.chat-date {{
    color: var(--muted-text);
    opacity: var(--opacity-medium);
    font-size: 0.9em;
    white-space: nowrap;
    flex-shrink: 0;
}}

@media (prefers-color-scheme: dark) {{
    :root {{
        --background-color: black;
        --text-color: white;
        --muted-text: lightgray;
    }}
    .chat-row {{
        background-color: #2c2c2e;
    }}
}}
</style>
</head>
<body>
<h1>iMessage Exports</h1>
<div class="meta">{count} chat(s)</div>
<div class="chat-list">
{rows}
</div>
</body></html>
"""

ROW_TEMPLATE = """  <a class="chat-row" href="{href}">
    <span class="chat-name">{name}</span>
    <span class="chat-date">{date}</span>
  </a>"""


def build_index(export_dir, handle_map):
    entries = []
    for f in sorted(Path(export_dir).glob("*.html")):
        mtime = f.stat().st_mtime
        name = resolve_display_name(f.stem, handle_map)
        entries.append((mtime, name, f.name))
    entries.sort(key=lambda e: e[0], reverse=True)   # newest chat first

    rows = []
    for mtime, name, fname in entries:
        date_str = datetime.fromtimestamp(mtime).strftime("%b %d, %Y %I:%M %p")
        href = f"iMessageExports/{fname}"
        rows.append(ROW_TEMPLATE.format(href=escape(href), name=escape(name),
                                         date=escape(date_str)))

    return PAGE_TEMPLATE.format(
        count=len(entries),
        rows="\n".join(rows) if rows else "  <p>No chats yet.</p>",
    )


def main():
    ap = argparse.ArgumentParser(description="Generate an index page linking to every exported chat.")
    ap.add_argument("export_dir", help="path to the iMessageExports directory")
    ap.add_argument("-o", "--output", required=True, help="path to write the index HTML to")
    ap.add_argument("--addressbook", action="append", default=[],
                    help="path to AddressBook-v22.abcddb (repeatable); auto-detected if omitted")
    args = ap.parse_args()

    if not Path(args.export_dir).is_dir():
        sys.exit(f"Not a directory: {args.export_dir}")

    ab_paths = args.addressbook or mbc.default_addressbook_paths()
    handle_map = mbc.build_handle_map(ab_paths) if ab_paths else {}

    html = build_index(args.export_dir, handle_map)
    Path(args.output).write_text(html, encoding="utf-8")
    n = len(list(Path(args.export_dir).glob("*.html")))
    print(f"Wrote index with {n} chat(s) to {args.output}")


if __name__ == "__main__":
    main()