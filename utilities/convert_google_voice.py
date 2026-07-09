#!/usr/bin/env python3
"""
convert_google_voice.py — convert Google Voice Takeout "Text" HTML exports
into imessage-exporter-compatible HTML, so they can be viewed and merged
alongside iMessage exports with merge_html_exports.py / merge_by_contact.py.

WHY THIS EXISTS:
Google Voice's Takeout format has no message GUIDs, chunks one ongoing
conversation across MANY files over time, and references MMS attachments by a
filename with NO extension (the real file sits next to the HTML with an
extension). This tool fixes all three: synthesizes a stable GUID per message,
consolidates chunks into one thread per contact (or per group, matched by
participant set), and resolves + copies attachments into a dedicated
Attachments/0-GV/ subfolder with the correct extension restored.

IDENTITY / GROUPING (no Contacts lookup needed for Google Voice):
  - "Me" is always the literal sender text for your own messages, and your
    own number is right there in that message's tel: href — read directly
    from the file, no configuration needed.
  - 1:1 threads: identity = the other participant's phone number.
  - Group threads: identity = the frozenset of participant numbers (from the
    <div class="participants"> block), excluding your own number.
  - All chunks sharing an identity are merged into ONE output file.

DEDUP: each message gets a synthetic GUID = uuid5(NAMESPACE, ts_iso|sender|text).
This is deterministic — the same message always gets the same GUID — so
overlapping chunks (if any) collapse cleanly, and it's genuinely GUID-shaped
so merge_html_exports.py's detector picks it up with no changes needed there.

USAGE:
  # Dry run: show detected threads/groups and attachment resolution, write nothing
  python3 convert_google_voice.py --dry-run --input-dir ~/Takeout/Voice/Calls \
      --output-dir ~/gv_converted --attachments-root ~/imessage-snapshots/Attachments

  # Convert for real:
  python3 convert_google_voice.py --input-dir ~/Takeout/Voice/Calls \
      --output-dir ~/gv_converted --attachments-root ~/imessage-snapshots/Attachments

If --media-dir is omitted, it defaults to --input-dir (Takeout normally puts
the HTML and its MMS media files side by side in the same folder).
"""
import argparse, glob, hashlib, html, os, re, sys, uuid, subprocess, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _ensure_bs4():
    """Same self-healing venv bootstrap as the other tools in this folder —
    creates .venv next to this script, installs beautifulsoup4, re-execs."""
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
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4 --break-system-packages")

# Reuse the Address Book reader already built and tested for merge_by_contact.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import merge_by_contact as mbc
except ImportError:
    mbc = None  # Address Book lookups become a no-op if this companion isn't present.

def _looks_like_a_number(s):
    """True if a 'name' is really just the phone number (or SMS short code)
    echoed back by GV. Short codes are 5-6 digits (e.g. "888222"), so the
    floor is lower than a full phone number."""
    digits = re.sub(r"[^\d]", "", s or "")
    return len(digits) >= 4 and len(re.sub(r"[\d\s()+-]", "", s or "")) == 0


def _normalize_number(s):
    """
    Digits-only normalization. If the source already has a '+' prefix,
    trust it and keep the full international number as-is (don't assume
    US/Canada) — Google Voice exports plenty of non-US numbers, e.g.
    France +33..., and truncating to a US-shaped 10-digit guess would
    silently produce the wrong number. Bare US numbers (no '+') get a +1
    prefix. SHORT CODES (5-6 digits, e.g. "40404" for marketing/verification
    texts) are NOT phone numbers — they're dialed as-is with no country
    code — so they pass through unprefixed rather than being mangled into
    a fake "+140404".
    """
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 4 <= len(digits) <= 6:
        return digits   # short code: use as-is, no country-code prefix
    return None


# Google's own filename convention is "<contact> - Text - <timestamp>Z.html"
# (or "- Missed -", "- Voicemail -", etc. for other categories). For an
# UNNAMED contact, <contact> IS the phone number — that's a reliable signal
# straight from Google, available even when a chunk has zero incoming
# messages to scrape a number from and no saved contact name to look up.
_FILE_CONTACT_RE = re.compile(
    r"^(.*?)[\s_]*-[\s_]*(?:Text|Missed|Placed|Received|Voicemail|Recorded)[\s_]*-",
    re.IGNORECASE)

def number_from_filename(path):
    stem = Path(path).stem
    m = _FILE_CONTACT_RE.match(stem)
    contact_part = m.group(1).strip() if m else None
    if contact_part and _looks_like_a_number(contact_part):
        return _normalize_number(contact_part)
    return None

GV_GUID_NAMESPACE = uuid.UUID("6f1f7c9a-0000-4a1a-9c1a-676f6f676c65")  # fixed, arbitrary

# The exact CSS block from a real imessage-exporter export, embedded so
# converted files render identically to native exports when viewed standalone.
# This is the REAL imessage-exporter stylesheet, copied verbatim from an
# actual export, NOT a custom approximation — so GV output uses exactly the
# same class vocabulary as real SMS/iMessage exports. Plain "sent" (no
# "iMessage"/"Satellite" suffix) is already green here, which is exactly
# right for Google Voice: it's SMS, so it gets no suffix and no invented
# class, same as any real SMS message would render.
IMESSAGE_STYLE = """
:root {
    --border-radius: 25px;
    --message-padding: 15px;
    --opacity-medium: 0.6;
    --opacity-high: 0.75;
    --imessage-blue: #1982FC;
    --sent-green: #65c466;
    --received-gray: #d8d8d8;
    --border-width: thin;
    --background-color: transparent;
    --text-color: black;
    --muted-text: dimgray;
}
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
    background: var(--background-color);
    color: var(--text-color);
}
p { margin: 0; }
.message { margin: 1%; overflow-wrap: break-word; }
.message .sent, .message .received {
    border-radius: var(--border-radius);
    padding: var(--message-padding);
    max-width: 60%;
    width: fit-content;
}
.message .sent { background-color: var(--sent-green); color: white; margin-left: auto; margin-right: 0; }
.message .sent.iMessage, .message .sent.Satellite { background-color: var(--imessage-blue); }
.message .received { background-color: var(--received-gray); color: black; margin-right: auto; margin-left: 0; }
span.timestamp, span.reply_context, span.expressive, span.tapbacks, span.deleted { opacity: var(--opacity-medium); }
span.reply_anchor, span.sender { opacity: 1; }
span.subject { font-weight: 600; }
span.bubble { white-space: pre-wrap; overflow-wrap: break-word; }
img, video { max-width: 100%; max-height: 90vh; }
audio { width: 90%; margin-left: auto; margin-right: auto; display: block; }
@media (prefers-color-scheme: dark) {
    :root { --background-color: black; --text-color: white; --muted-text: lightgray; }
}
"""


def parse_gv_file(path):
    """Parse one Google Voice export chunk into (participants, [messages], own_number, label)."""
    soup = BeautifulSoup(Path(path).read_text(encoding="utf-8", errors="replace"),
                         "html.parser")

    # <title> holds the saved contact name, e.g. "Me to\nJohn Smith" or "John Smith"
    # or blank for phone-number-only (unsaved) contacts. This is the ONLY signal
    # available when a chunk contains just your own outgoing messages with no
    # reply to scrape a number from.
    title = soup.find("title")
    label = ""
    if title and title.get_text(strip=True):
        label = re.sub(r"^Me to\s*", "", title.get_text(strip=True)).strip()

    participants = []
    pdiv = soup.find("div", class_="participants")
    if pdiv:
        for cite in pdiv.find_all("cite", class_="sender"):
            a = cite.find("a", class_="tel")
            if a and a.get("href", "").startswith("tel:"):
                participants.append(a["href"][4:])

    messages = []
    own_number = None
    for msg in soup.find_all("div", class_="message"):
        abbr_dt = msg.find("abbr", class_="dt")
        if not abbr_dt or not abbr_dt.get("title"):
            continue
        try:
            ts = datetime.fromisoformat(abbr_dt["title"])
        except ValueError:
            continue

        cite = msg.find("cite", class_="sender")
        tel_a = cite.find("a", class_="tel") if cite else None
        sender_tel = tel_a["href"][4:] if tel_a and tel_a.get("href", "").startswith("tel:") else None
        sender_text = cite.get_text(strip=True) if cite else ""
        is_me = (sender_text == "Me")
        if is_me and sender_tel:
            own_number = sender_tel
        # Display name: the <span class="fn"> (or <abbr class="fn"> for "Me")
        # text; empty for unknown numbers.
        fn = cite.find(["span", "abbr"], class_="fn") if cite else None
        display_name = (fn.get_text(strip=True) if fn else "") or (sender_tel or "Unknown")

        q = msg.find("q")
        text_html = q.decode_contents() if q else ""

        # Attachments: <img src="BASENAME-NO-EXT" alt="Image MMS Attachment" />
        atts = []
        for img in msg.find_all("img"):
            src = img.get("src")
            if src:
                atts.append(src)

        messages.append({
            "ts": ts,
            "sender_tel": sender_tel,
            "sender_text": sender_text,
            "is_me": is_me,
            "display_name": display_name,
            "text_html": text_html,
            "attachments": atts,
        })
    return participants, messages, own_number, label


def synth_guid(ts, sender, text):
    key = f"{ts.isoformat()}|{sender or ''}|{text}"
    return str(uuid.uuid5(GV_GUID_NAMESPACE, key)).upper()


def format_ts(ts):
    """Match the human format merge_html_exports.py already parses, without a
    leading zero on the hour (cosmetic parity with real imessage-exporter)."""
    s = ts.strftime("%b %d, %Y %I:%M:%S %p")
    return re.sub(r"(?<=\s)0(\d:)", r"\1", s)  # "07:" -> "7:" after the space


def resolve_attachment(src, media_dir):
    """
    Google Voice references attachments WITHOUT an extension. The real file
    sits next to the HTML as <src>.<ext>. Glob for it; return the matched
    path or None if not found (e.g. media wasn't included in this directory).
    """
    matches = glob.glob(os.path.join(glob.escape(media_dir), src + ".*"))
    return matches[0] if matches else None


def stash_attachment(src_path, dest_dir):
    """
    Copy into dest_dir preserving the ORIGINAL filename (Google's names are
    already effectively unique — they embed the source conversation and a
    per-message index — so there's no real dedup benefit to hashing, and it
    throws away a readable, traceable name for no reason).

    Only if a DIFFERENT file already exists under that exact name do we
    disambiguate with a short content-hash suffix, rather than overwriting.
    If the same content is already stored under that name, it's a no-op.
    """
    os.makedirs(dest_dir, exist_ok=True)
    data = Path(src_path).read_bytes()
    base_name = Path(src_path).name
    dest = Path(dest_dir) / base_name

    if dest.exists():
        if dest.read_bytes() == data:
            return base_name   # identical file already stored; nothing to do
        # Genuine collision: same name, different content. Disambiguate.
        h = hashlib.sha256(data).hexdigest()[:8]
        stem, ext = os.path.splitext(base_name)
        base_name = f"{stem}_{h}{ext}"
        dest = Path(dest_dir) / base_name

    if not dest.exists():
        dest.write_bytes(data)
    return base_name


def render_message(msg, resolved_atts):
    # Real imessage-exporter output always tags explicitly -- "sent iMessage",
    # "sent Satellite", or "sent SMS" -- rather than relying on a bare "sent"
    # meaning anything by default (confirmed against a real SMS sample, which
    # used class="sent SMS", not a bare "sent"). Google Voice is SMS, so it
    # gets that same explicit "SMS" tag. "GoogleVoice" rides alongside it as
    # an inert marker only, same as before.
    cls = "sent SMS GoogleVoice" if msg["is_me"] else "received GoogleVoice"
    sender_label = "Me" if msg["is_me"] else msg["display_name"]
    guid = synth_guid(msg["ts"], msg["sender_tel"], msg["text_html"])
    ts_text = format_ts(msg["ts"])

    body = f'<span class="bubble">{msg["text_html"]}</span>' if msg["text_html"].strip() else ""
    # Matches the REAL imessage-exporter attachment markup exactly:
    # <div class="attachment"><img src="..." loading="lazy"></div>, living
    # INSIDE message_part (not as a sibling after it).
    att_html = "".join(
        f'<div class="attachment"><img src="{ref}" loading="lazy"></div>'
        for ref in resolved_atts
    )

    return f'''<div class="message">
    <div class="{cls}">
        <p>
            <span class="timestamp">
                <a title="Google Voice message" href="sms://open?message-guid={guid}">{ts_text}</a>
            </span>
            <span class="sender">{html.escape(sender_label)}</span>
        </p>
        <hr>
        <div class="message_part">
            {body}{att_html}
        </div>
    </div>
</div>'''


def convert(input_dir, output_dir, attachments_root, media_dir,
            addressbook_paths=None, dry_run=False):
    # Google Voice Takeout puts call logs (Missed/Placed/Received) and
    # voicemails (Voicemail) as HTML in the SAME folder as text threads. Only
    # "... - Text - ..." files are actual SMS/MMS conversations; the others
    # have a different structure entirely and would parse as garbage if we
    # tried. Handles both real Takeout spacing (" - Text - ") and the
    # underscore-substituted form some upload paths produce ("_Text_").
    TEXT_MARKER = re.compile(r"(?:^|[\s_-])Text(?:[\s_-]|$)", re.IGNORECASE)
    GROUP_MARKER = re.compile(r"^Group[\s_]Conversation", re.IGNORECASE)
    NON_TEXT_MARKER = re.compile(
        r"(?:^|[\s_-])(Missed|Placed|Received|Voicemail|Recorded)(?:[\s_-]|$)",
        re.IGNORECASE)
    all_html = sorted(glob.glob(os.path.join(input_dir, "*.html")))
    files, skipped, skipped_samples = [], defaultdict(int), defaultdict(list)
    for f in all_html:
        name = Path(f).name
        m = NON_TEXT_MARKER.search(name)
        if m:
            kind = m.group(1).capitalize()
            skipped[kind] += 1
            skipped_samples[kind].append(name)
        elif TEXT_MARKER.search(name) or GROUP_MARKER.match(name):
            files.append(f)
        else:
            skipped["(unrecognized filename)"] += 1
            skipped_samples["(unrecognized filename)"].append(name)
    if skipped:
        print("Skipping non-text exports found in the same folder:")
        for kind, n in sorted(skipped.items()):
            print(f"    {kind}: {n} file(s)")
            for sample in skipped_samples[kind][:5]:
                print(f"        e.g. {sample}")
            if n > 5:
                print(f"        ... and {n - 5} more")
        print()
    if not files:
        sys.exit(f"No .html files found in {input_dir}")

    # Address Book: build BOTH directions once. number->name fills in a
    # display name when GV itself never captured one; name->number resolves
    # a thread's number when GV's title has a name but no message ever
    # showed the actual number (e.g. an all-outgoing chunk).
    handle_to_name = {}
    name_to_number = {}
    if mbc is not None:
        ab_paths = addressbook_paths or mbc.default_addressbook_paths()
        if ab_paths:
            handle_map = mbc.build_handle_map(ab_paths)  # {norm_handle: (person_key, name)}
            handle_to_name = {h: name for h, (_pk, name) in handle_map.items()}
            for h, (_pk, name) in handle_map.items():
                if h.isdigit():   # phone numbers only, not emails, for name->number
                    name_to_number.setdefault(name.lower(), []).append(h)
        else:
            print("(no macOS Address Book found; name-based fallback resolution disabled)\n")

    def ab_name_for(number):
        if not number:
            return None
        key = re.sub(r"\D", "", number)[-10:]
        return handle_to_name.get(key)

    def ab_number_for(label):
        cands = name_to_number.get((label or "").lower())
        if not cands:
            return None
        if len(cands) > 1:
            print(f"  NOTE: '{label}' has multiple numbers in Contacts ({cands}); using the first.")
        return "+1" + cands[0]   # canonical last-10-digits -> +1 prefixed for US/CA

    threads = defaultdict(list)     # identity -> [(message, source_file)]
    thread_kind = {}                # identity -> "1:1" | "group"
    own_number = None
    parsed_files = []               # (f, participants, messages, label)

    # PASS 1: parse every file, remember each file's contact label.
    for f in files:
        participants, messages, own, label = parse_gv_file(f)
        if own:
            own_number = own
        parsed_files.append((f, participants, messages, label))

    # Build label -> number from any file where the other party's number was
    # found directly (a reply was present in that chunk).
    label_to_number = {}
    for f, participants, messages, label in parsed_files:
        if not label or participants:
            continue
        others = {m["sender_tel"] for m in messages if not m["is_me"] and m["sender_tel"]}
        if others:
            label_to_number[label] = next(iter(others))

    # PASS 2: assign each file's messages to an identity. Resolution order for
    # a chunk with no reply to scrape a number from:
    #   1. the message content itself reveals the other party's number
    #   2. Google's OWN filename convention embeds the number directly, for
    #      unnamed contacts ("<number> - Text - <timestamp>Z.html")
    #   3. another GV chunk with the same label DID reveal a number
    #   4. the macOS Address Book resolves that name to a number
    #   5. last resort: keep it grouped by name only (rare)
    unresolved_labels = set()
    for f, participants, messages, label in parsed_files:
        if not messages:
            continue
        if participants:
            identity = frozenset(participants)
            thread_kind[identity] = "group"
        else:
            others = {m["sender_tel"] for m in messages if not m["is_me"] and m["sender_tel"]}
            fname_number = number_from_filename(f)
            if others:
                identity = next(iter(others))
            elif fname_number:
                identity = fname_number
            elif label and label in label_to_number:
                identity = label_to_number[label]
            elif label and ab_number_for(label):
                identity = ab_number_for(label)
            elif label:
                identity = f"label:{label}"
                unresolved_labels.add(label)
            else:
                identity = Path(f).stem
            thread_kind[identity] = "1:1"
        threads[identity].extend((m, f) for m in messages)

    if unresolved_labels:
        print(f"NOTE: {len(unresolved_labels)} contact(s) never revealed a phone number in any "
              f"file AND weren't found in Contacts: {sorted(unresolved_labels)}")
        print("      These are grouped by name instead of number.\n")

    print(f"Own number detected: {own_number or '(not found)'}")
    print(f"Found {len(files)} export file(s), consolidating into {len(threads)} thread(s).\n")

    unresolved_atts = []            # (src, source_file) that couldn't be found
    resolved_att_map = {}           # (source_file, src) -> resolved dest filename

    plan = []
    for identity, msg_file_pairs in threads.items():
        kind = thread_kind[identity]

        # Dedup by synthetic GUID first (needed before name backfill so we
        # scan the FINAL message set, not raw chunks with overlap).
        seen_guids = {}
        for msg, srcfile in msg_file_pairs:
            g = synth_guid(msg["ts"], msg["sender_tel"], msg["text_html"])
            seen_guids[g] = (msg, srcfile)
        records = list(seen_guids.values())

        # ---- display-name backfill (1:1 only; groups render per-sender) ----
        # Priority: any real name GV ever captured for this thread (not the
        # number echoed as fn) > Address Book > raw number, unchanged.
        if kind == "1:1":
            real_names = [m["display_name"] for m, _ in records
                          if not m["is_me"] and m["display_name"]
                          and not _looks_like_a_number(m["display_name"])]
            resolved_name = real_names[0] if real_names else ab_name_for(
                identity if not str(identity).startswith("label:") else None)
            if resolved_name:
                for m, _ in records:
                    if not m["is_me"]:
                        m["display_name"] = resolved_name

        # ---- filenames: match imessage-exporter's own-number convention ----
        if kind == "1:1":
            if isinstance(identity, str) and identity.startswith("label:"):
                clean = identity[len("label:"):]
                out_name = re.sub(r"[^\w.+-]", "_", clean) + ".html"
                label_display = clean
            else:
                out_name = str(identity) + ".html"
                label_display = str(identity)
        else:
            nums = sorted(identity)
            label_display = "Group: " + ", ".join(nums)
            # Unnamed-group convention: list of numbers, matching imessage-exporter.
            # But filesystems cap a filename at 255 bytes — large groups can
            # exceed that, so truncate with a "+N more" suffix when needed.
            full = ", ".join(nums)
            out_stem = full
            if len(out_stem.encode("utf-8")) > 200:   # leave room for ", +N more.html"
                kept = []
                for n in nums:
                    candidate = ", ".join(kept + [n])
                    if len(candidate.encode("utf-8")) > 160:
                        break
                    kept.append(n)
                remaining = len(nums) - len(kept)
                out_stem = ", ".join(kept) + f", +{remaining} more"
            out_name = out_stem + ".html"

        att_count = sum(len(m["attachments"]) for m, _ in records)
        plan.append({
            "identity": identity, "kind": kind, "label": label_display,
            "out_name": out_name, "records": records,
            "attachment_count": att_count,
        })

    print("PLANNED OUTPUT THREADS:")
    for p in sorted(plan, key=lambda x: -len(x["records"])):
        print(f"  [{p['kind']:5}] {p['label']:50} -> {p['out_name']}   "
              f"({len(p['records'])} msgs, {p['attachment_count']} attachment refs)")

    if dry_run:
        print("\nResolving attachments (dry run; nothing copied)...")
    total_resolved = total_missing = 0
    for p in plan:
        for msg, srcfile in p["records"]:
            srcdir = os.path.dirname(srcfile) if not media_dir else media_dir
            for src in msg["attachments"]:
                key = (srcfile, src)
                if key in resolved_att_map:
                    continue
                found = resolve_attachment(src, srcdir)
                if found:
                    total_resolved += 1
                    if not dry_run:
                        dest_name = stash_attachment(found, os.path.join(attachments_root, "0-GV"))
                        resolved_att_map[key] = f"Attachments/0-GV/{dest_name}"
                    else:
                        resolved_att_map[key] = f"Attachments/0-GV/<hash>{Path(found).suffix}"
                else:
                    total_missing += 1
                    unresolved_atts.append((src, srcfile))

    print(f"\nAttachments: {total_resolved} resolved, {total_missing} unresolved.")
    if unresolved_atts:
        print("  Unresolved (source file not found in media dir):")
        for src, srcfile in unresolved_atts[:10]:
            print(f"      {src}  (from {Path(srcfile).name})")
        if len(unresolved_atts) > 10:
            print(f"      ... and {len(unresolved_atts) - 10} more")

    if dry_run:
        print("\n[dry-run] Nothing written. Re-run without --dry-run to produce files.")
        return

    os.makedirs(output_dir, exist_ok=True)
    for p in plan:
        records = sorted(p["records"], key=lambda t: t[0]["ts"])
        body_parts = []
        for msg, srcfile in records:
            refs = []
            for src in msg["attachments"]:
                refs.append(resolved_att_map.get((srcfile, src), src))  # fallback: leave as-is
            body_parts.append(render_message(msg, refs))
        html_doc = (
            "<!DOCTYPE html><html><head>"
            f"<style>{IMESSAGE_STYLE}</style>"
            "</head><body>\n" + "\n".join(body_parts) + "\n</body></html>"
        )
        out_path = Path(output_dir) / p["out_name"]
        out_path.write_text(html_doc, encoding="utf-8")
        print(f"  wrote {out_path}  ({len(records)} messages)")

    # Group filenames can be TRUNCATED (filesystem 255-byte limit — see the
    # "+N more" suffix logic above), which would otherwise silently lose the
    # full membership list. This sidecar records every group's COMPLETE
    # participant set, keyed by its (possibly truncated) output filename.
    #
    # Format matches imessage-incremental-sync's own group_participants.json
    # exactly — a flat {filename: [handle, handle, ...]} object, no extra
    # metadata — so the same downstream indexer (populate_raw_participants)
    # reads GV-derived groups the same way it reads real iMessage ones,
    # with no special-casing needed on the consumer side.
    group_plans = [p for p in plan if p["kind"] == "group"]
    if group_plans:
        sidecar = {p["out_name"]: sorted(p["identity"]) for p in group_plans}
        sidecar_path = Path(output_dir) / "group_participants.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        print(f"  wrote {sidecar_path}  (full membership for {len(group_plans)} group(s))")

    print(f"\nDone. {len(plan)} thread(s) written to {output_dir}")
    print(f"Attachments copied under {attachments_root}/0-GV/")


def main():
    ap = argparse.ArgumentParser(description="Convert Google Voice Takeout exports to imessage-exporter-compatible HTML.")
    ap.add_argument("--input-dir", required=True, help="folder containing GV *.html export chunks")
    ap.add_argument("--output-dir", required=True, help="folder for converted per-thread HTML")
    ap.add_argument("--attachments-root", required=True,
                    help="shared Attachments root; GV media goes under <this>/0-GV/")
    ap.add_argument("--media-dir", default=None,
                    help="folder containing the MMS media files (default: same as --input-dir)")
    ap.add_argument("--addressbook", action="append", default=[],
                    help="path to AddressBook-v22.abcddb (repeatable); auto-detected if omitted")
    ap.add_argument("--dry-run", action="store_true",
                    help="show planned threads and attachment resolution; write nothing")
    args = ap.parse_args()
    convert(args.input_dir, args.output_dir, args.attachments_root,
            args.media_dir or args.input_dir, addressbook_paths=args.addressbook,
            dry_run=args.dry_run)


if __name__ == "__main__":
    main()
