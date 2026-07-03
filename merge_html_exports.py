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
import argparse, hashlib, os, re, sys, html
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from bs4 import BeautifulSoup, Comment
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4 --break-system-packages")

# lxml is a C-based parser and multiple times faster than Python's built-in
# html.parser on large documents (which matters a lot here, since an
# incremental sync re-parses the whole growing archive every run). It's an
# optional speed boost, not a hard requirement -- if it isn't installed,
# fall back to html.parser exactly as before with no behavior change.
try:
    import lxml  # noqa: F401
    HTML_PARSER_BACKEND = "lxml"
except ImportError:
    HTML_PARSER_BACKEND = "html.parser"

def make_soup(text):
    return BeautifulSoup(text, HTML_PARSER_BACKEND)

# Canonical iMessage GUID shape: 8-4-4-4-12 hex, case-insensitive.
GUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
# The one unambiguous source of a genuine message GUID: imessage-exporter's
# own "Reveal in Messages app" link. Other GUID-shaped strings can appear by
# coincidence -- e.g. UUIDs baked into CDN/image URLs inside link-preview
# attachments -- and must not be mistaken for message identity.
MSG_GUID_LINK_RE = re.compile(
    r"message-guid=([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
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


def set_mtime_from_timestamp(path, ts_iso):
    """
    Set a file's modified (and accessed) time to reflect its most recent
    message, instead of leaving it as "whenever this script happened to
    run" -- so the file's own timestamp in Finder/ls actually means
    something. Silently does nothing if ts_iso is empty or doesn't parse
    (e.g. the raw-text fallback path in parse_ts_to_key for a timestamp
    format that wasn't recognized) -- a merge shouldn't fail just because
    one message's date is unusual.
    """
    if not ts_iso:
        return
    try:
        epoch = datetime.fromisoformat(ts_iso).timestamp()
        os.utime(path, (epoch, epoch))
    except (ValueError, OSError):
        pass


SCROLL_TO_BOTTOM_MARKER = "data-imessage-scroll-script"

SCROLL_TO_BOTTOM_SCRIPT = f"""<script {SCROLL_TO_BOTTOM_MARKER}="1">
(function() {{
  function scrollToBottom() {{ window.scrollTo(0, document.body.scrollHeight); }}
  window.addEventListener('load', function() {{
    scrollToBottom();
    // Attachments (images/video) can still be loading and changing the
    // page's height for a moment after the load event fires, so scroll
    // again a couple times shortly after to land on the true bottom.
    setTimeout(scrollToBottom, 300);
    setTimeout(scrollToBottom, 1000);
  }});
}})();
</script>
"""

def inject_scroll_to_bottom(html_text):
    """
    Adds a small script that scrolls to the bottom of the page once it
    loads, so opening a merged export lands on the most recent messages
    instead of the oldest ones (message order in these exports is oldest
    first, so "the bottom" is "the latest").

    IDEMPOTENT BY DESIGN: an already-merged file that already has this
    script gets fed back in as input on every future incremental sync (the
    single-file passthrough path in merge_by_contact.py works at the raw
    text level, unlike mhe.merge()'s tree rebuild, which happens to already
    discard any old script when it clears the body before re-appending
    messages). Without checking for the marker attribute first, re-running
    this on the same file would stack another <script> block on top every
    single time -- the exact same class of bug as the earlier handle-tag
    stacking issue. If the marker is already present, this is a no-op.
    """
    if SCROLL_TO_BOTTOM_MARKER in html_text:
        return html_text
    idx = html_text.lower().rfind("</body>")
    if idx == -1:
        return html_text + SCROLL_TO_BOTTOM_SCRIPT
    return html_text[:idx] + SCROLL_TO_BOTTOM_SCRIPT + html_text[idx:]


def find_announcement_containers(soup):
    """
    <div class="announcement"> elements represent system events — someone
    unsent a message, renamed the conversation, was added/removed, etc.
    They carry no GUID, so find_message_containers never sees them, which
    means they'd otherwise be left behind wherever they sat in the first
    file's original HTML instead of being merged and re-sorted with
    everything else. We give each one a stable pseudo-GUID derived from its
    own text (which already includes its timestamp), so it can flow through
    the exact same dedup + sort + rebuild path as a real message.
    """
    out = []
    for div in soup.find_all("div", class_="announcement"):
        text = re.sub(r"\s+", " ", div.get_text()).strip()
        pseudo = "ANNOUNCEMENT:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
        out.append((div, pseudo))
    return out


def collect_known_guids(soup):
    """
    Collect GUIDs only from the trustworthy source: the reveal-in-Messages
    link (message-guid=...). These are the sole values allowed to represent
    message identity when deciding container boundaries.
    """
    known = set()
    for tag in soup.find_all(True):
        for attr, val in tag.attrs.items():
            valstr = " ".join(val) if isinstance(val, list) else str(val)
            for m in MSG_GUID_LINK_RE.finditer(valstr):
                known.add(m.group(1).upper())
    return known


def find_message_containers(soup):
    """
    Heuristic: a 'message container' is the smallest repeated element that
    contains exactly one GUID. We locate every GUID occurrence, then walk up
    to the nearest ancestor that contains that GUID and no other, treating that
    ancestor as the message unit. Falls back gracefully if structure is flat.

    Only GUIDs recognized as genuine message identity (via the reveal link)
    are used, both to pick anchor nodes and to decide when to stop climbing.
    Otherwise an incidental GUID-shaped string elsewhere in the same message
    -- e.g. a UUID baked into a link-preview's image URL -- looks like a
    second, unrelated message sharing that ancestor, which stops the climb
    one level too early and leaves the real message content behind.
    """
    known_guids = collect_known_guids(soup)

    guid_nodes = []
    if known_guids:
        # Primary, unambiguous path: locate each known guid via its reveal link.
        for tag in soup.find_all(True):
            for attr, val in list(tag.attrs.items()):
                valstr = " ".join(val) if isinstance(val, list) else str(val)
                m = MSG_GUID_LINK_RE.search(valstr)
                if m and m.group(1).upper() in known_guids:
                    guid_nodes.append((tag, m.group(1).upper()))
                    break
    else:
        # Fallback for export formats without the sms:// reveal link: use the
        # old broad GUID-shape scan (attributes, comments, then visible text).
        for tag in soup.find_all(True):
            for attr, val in list(tag.attrs.items()):
                valstr = " ".join(val) if isinstance(val, list) else str(val)
                if GUID_RE.search(valstr):
                    guid_nodes.append((tag, GUID_RE.search(valstr).group(0)))
                    break
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            m = GUID_RE.search(c)
            if m:
                parent = c.parent if c.parent else soup
                guid_nodes.append((parent, m.group(0)))
        if not guid_nodes:
            for el in soup.find_all(True):
                if el.string and GUID_RE.search(el.string):
                    guid_nodes.append((el, GUID_RE.search(el.string).group(0)))
        known_guids = {g.upper() for _, g in guid_nodes}

    # For each guid node, climb to an ancestor that still contains exactly this
    # one KNOWN guid, to capture the whole bubble (text + attachments + metadata).
    # Any other GUID-shaped text encountered along the way that isn't a known
    # message guid is ignored, so it can't falsely trigger an early stop.
    #
    # PERFORMANCE: the naive way to check "does this ancestor contain exactly
    # one guid" is to serialize the ancestor's whole subtree with str() and
    # regex-search it, every climb step, for every message. That looks
    # innocent but is quadratic overall: as the document grows, ancestors
    # near the top (e.g. <body>) contain nearly the whole document, so
    # serializing them once per message costs O(document size) each time,
    # for every one of the document's messages -- O(n^2) total. On a merged
    # archive that grows by re-running this on its own output every
    # incremental sync, that gets slower every single day.
    #
    # Instead, walk each guid's ancestor chain ONCE and record, for every
    # ancestor touched, which guids were seen underneath it (a set union,
    # not a re-serialization). Since ancestor chains are shallow (a handful
    # of wrapper elements, not proportional to document size), this whole
    # pass is O(n * chain depth) -- effectively linear -- rather than O(n^2).
    ancestor_guids = defaultdict(set)
    for node, guid in guid_nodes:
        cur = node
        while cur and cur.parent and cur.parent is not soup:
            cur = cur.parent
            ancestor_guids[id(cur)].add(guid)

    containers = []
    seen_ids = set()
    MAX_CLIMB = 25   # defense in depth; see note below
    for node, guid in guid_nodes:
        cur = node
        best = node
        depth = 0
        while cur and cur.parent and cur.parent is not soup:
            if ancestor_guids[id(cur.parent)] != {guid}:
                break
            best = cur.parent
            cur = cur.parent
            depth += 1
            # HARD STRUCTURAL STOP: the guid-count heuristic above silently
            # breaks down when a document contains only ONE real message --
            # there's no second guid anywhere to ever interrupt the climb,
            # so without this it would continue all the way to <html>,
            # treating the entire document (head, CSS, everything) as that
            # one message's "container". This isn't hypothetical: an
            # incremental export containing exactly one new message for a
            # given contact hits it every time. imessage-exporter always
            # wraps a whole message in <div class="message">, so once we've
            # climbed to that, stop unconditionally -- regardless of what
            # the guid-count check above would otherwise allow.
            classes = cur.get("class") or []
            if isinstance(classes, str):
                classes = classes.split()
            if "message" in classes:
                break
            if depth >= MAX_CLIMB:
                # Second-layer safety net in case some other message type
                # doesn't use the "message" class at all: never let a single
                # container swallow an unbounded chunk of the document.
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
    soup = make_soup(Path(path).read_text(encoding="utf-8", errors="replace"))
    out = []
    for container, guid in find_message_containers(soup):
        out.append({
            "guid": guid.upper(),
            "ts": extract_timestamp(container),
            "tag": container,
        })
    for container, guid in find_announcement_containers(soup):
        out.append({
            "guid": guid,
            "ts": extract_timestamp(container),
            "tag": container,
        })
    return soup, out


def inspect(paths):
    print(f"Inspecting {len(paths)} file(s). No output is written in this mode.\n")
    for p in paths:
        soup, msgs = parse_file(p)
        n_ann = sum(1 for m in msgs if m["guid"].startswith("ANNOUNCEMENT:"))
        n_msg = len(msgs) - n_ann
        ts_have = sum(1 for m in msgs if m["ts"])
        print(f"  {p}")
        print(f"      messages detected     : {n_msg}")
        print(f"      announcements detected: {n_ann}")
        print(f"      with timestamps       : {ts_have}/{len(msgs)}")
        uniq = len({m['guid'] for m in msgs})
        print(f"      unique GUIDs          : {uniq}")
        real = [m for m in msgs if not m["guid"].startswith("ANNOUNCEMENT:")]
        if real:
            s = real[0]
            print(f"      sample GUID           : {s['guid']}")
            print(f"      sample timestamp      : {s['ts'] or '(none detected)'}")
            snippet = re.sub(r"\s+", " ", str(s["tag"]))[:120]
            print(f"      sample container      : {snippet}...")
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
    # Clear the template's own original content -- we're about to rebuild the
    # whole body from the merged, sorted, deduped set below, which already
    # includes the template file's own messages/announcements (captured into
    # `merged` above). clear() only detaches children from their parent; the
    # Tag objects themselves stay alive and valid via the references already
    # held in `merged`, so nothing is lost.
    body.clear()
    # Move each container directly into place -- no re-parsing. Each `tag`
    # is a real BeautifulSoup Tag already sitting in memory from parse_file;
    # extract() detaches it from wherever it currently lives (its own
    # original per-file soup, or already-detached if it came from the
    # template and was swept up by body.clear() above -- extracting an
    # already-parentless tag is a harmless no-op) and append() moves it into
    # place. This replaces creating a brand-new BeautifulSoup parser
    # instance per message just to re-parse a string back into a tag, which
    # dominated runtime on large archives.
    for r in ordered:
        r["tag"].extract()
        body.append(r["tag"])

    final_html = inject_scroll_to_bottom(str(template_soup))
    Path(out_path).write_text(final_html, encoding="utf-8")
    if dated:
        set_mtime_from_timestamp(out_path, dated[-1]["ts"])

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