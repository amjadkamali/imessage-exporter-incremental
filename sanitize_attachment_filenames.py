#!/usr/bin/env python3
"""
sanitize_attachment_filenames.py

Nextcloud (and other sync tools) reject filenames containing certain
characters that are valid on macOS but not universally portable: colon,
pipe, and backslash. This script finds every attachment or sticker file
under an iMessage export root whose name contains one of these, renames
it to a sanitized form, and rewrites every reference to the old name
inside every exported .html file -- so renaming doesn't silently break
attachment display in the viewer.

Each distinct problematic character maps to a DIFFERENT number of dashes
(":" -> "---", "|" -> "--", "\\" -> "-") rather than collapsing everything
to one generic placeholder. Two benefits: which original character was
present is recoverable just by counting a dash run (useful if you ever
need to debug or reverse this), and two DIFFERENT original characters can
never collide into an identical replacement the way a single "_" for
everything easily could.

Every character is checked per path COMPONENT (one directory or file name
at a time), not against a full path string as a single unit -- this is
what correctly handles a file sitting inside a directory that ALSO needs
renaming (both the file's own name and its ancestor directory's name
change at once; sanitized_relative_path() resolves the full new path in
one step rather than patching it together from independent substring
replacements, which turned out not to be reliable -- see the function's
own docstring for the specific case this was caught on).

Usage:
    python3 sanitize_attachment_filenames.py /path/to/iMessageExports
    python3 sanitize_attachment_filenames.py /path/to/iMessageExports --dry-run

Safe to run more than once: a name with none of the target characters is
left completely untouched, so an already-sanitized tree is a no-op on a
second run.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

CHAR_TO_DASHES = {
    ':': '---',
    '|': '--',
    '\\': '-',
}

ATTACHMENT_DIR_NAMES = ('attachments', 'Attachments', 'StickerCache')


def sanitize_component(name):
    """
    Replace every occurrence of a target character in a single path
    component (a directory or file name, never a full path) with its
    assigned dash sequence. Returns (new_name, changed).
    """
    original = name
    for ch, dashes in CHAR_TO_DASHES.items():
        if ch in name:
            name = name.replace(ch, dashes)
    return name, (name != original)


def build_rename_plan(attachment_root):
    """
    Walk attachment_root and compute, for every directory level, a mapping
    of {old_component_name: new_component_name} for any directory or file
    name that needs sanitizing -- without renaming anything yet.

    If two different original names happen to sanitize to the identical
    result within the same directory, both simply map to that same new
    name -- treated as the same file rather than disambiguated with a
    suffix. Attachments are GUID-named, so a genuine collision here is
    negligible; apply_rename_plan() folds the surplus one in as a
    duplicate rather than risk manufacturing an artificial "__2" variant
    nobody asked for.
    Returns {relative_dir_path (Path): {old_name: new_name}}.
    """
    plan = {}
    for dirpath, dirnames, filenames in os.walk(attachment_root):
        rel_dir = Path(dirpath).relative_to(attachment_root)
        local_plan = {}
        for name in sorted(dirnames) + sorted(filenames):
            new_name, changed = sanitize_component(name)
            if changed:
                local_plan[name] = new_name
        if local_plan:
            plan[rel_dir] = local_plan
    return plan


def apply_rename_plan(attachment_root, plan, dry_run):
    """
    Actually perform the renames on disk, deepest paths first (so a
    directory's own contents are already settled before the directory
    itself might be renamed by its parent's turn in the plan).

    If the target name already exists (the file was already sanitized in
    a prior pass, or a genuine same-sanitized-name collision), the old
    file is treated as the same attachment and deleted rather than
    renamed on top of it -- content is assumed identical rather than
    re-verified byte for byte, consistent with how heal_resynced_duplicates()
    already treats this same situation elsewhere in this script.

    Returns the number of items renamed (deletions of an
    already-present duplicate are not counted separately here).
    """
    count = 0
    # Deepest first: more path separators means deeper in the tree.
    for rel_dir in sorted(plan.keys(), key=lambda p: len(p.parts), reverse=True):
        local_plan = plan[rel_dir]
        for old_name, new_name in local_plan.items():
            old_path = attachment_root / rel_dir / old_name
            new_path = attachment_root / rel_dir / new_name
            if not old_path.exists():
                # Already renamed as part of an ancestor directory's own
                # rename in an earlier plan entry -- shouldn't happen with
                # this bottom-up ordering, but skip defensively rather
                # than error if it ever does.
                continue
            if new_path.exists():
                print(f"{'[dry-run] ' if dry_run else ''}{old_path} -> already have {new_path}, removing duplicate")
                if not dry_run:
                    old_path.unlink()
            else:
                print(f"{'[dry-run] ' if dry_run else ''}{old_path} -> {new_path}")
                if not dry_run:
                    old_path.rename(new_path)
            count += 1
    return count


def rewrite_component(rel_dir, name, plan):
    """Return name's replacement per plan for this directory level, or name unchanged if not in it."""
    return plan.get(rel_dir, {}).get(name, name)


def sanitized_relative_path(rel_path, plan):
    """
    Given a path relative to the attachment root (e.g. "sub:dir/pic:1.jpg"),
    return what it becomes after applying plan to each of its components
    independently -- this is what makes it possible to compute a file's new
    path directly from its old one without needing to track renames as a
    live, mutating mapping.
    """
    parts = rel_path.parts
    new_parts = []
    for i, part in enumerate(parts):
        rel_dir = Path(*parts[:i]) if i > 0 else Path('.')
        new_parts.append(rewrite_component(rel_dir, part, plan))
    return Path(*new_parts) if new_parts else Path('.')


def find_attachment_roots(export_root):
    """
    Find every Attachments/StickerCache/attachments directory anywhere
    under export_root -- there can be more than one across different
    dated export subfolders, or a single shared one, depending on layout.
    """
    found = []
    for dirpath, dirnames, _ in os.walk(export_root):
        for d in list(dirnames):
            if d in ATTACHMENT_DIR_NAMES:
                found.append(Path(dirpath) / d)
    return found


ATTACHMENT_REF_RE = re.compile(r'(?:src|href)="((?:attachments|Attachments|StickerCache)/[^"]+)"')


def extract_referenced_paths(html_files):
    """
    Parse the given .html files and return the set of attachment-relative
    path strings (e.g. "Attachments/sub/pic.jpg") they reference via
    src="..." or href="...". Same pattern this project's own indexer.py
    and app.py already use to recognize an attachment reference, so
    anything they'd serve is exactly what gets found here too.
    """
    referenced = set()
    for html_path in html_files:
        text = html_path.read_text(encoding='utf-8', errors='replace')
        referenced.update(ATTACHMENT_REF_RE.findall(text))
    return referenced


def build_scoped_rename_plan(export_root, referenced_paths):
    """
    Build a rename plan restricted to ONLY the given referenced paths --
    unlike build_rename_plan(), this never considers a file just because
    it happens to share a directory with something referenced; only a
    path that's ACTUALLY in referenced_paths is ever a candidate.

    This is what makes scoping the HTML rewrite to a handful of recent
    export folders safe in the first place: a file that's only referenced
    by some OTHER, out-of-scope folder's HTML can never be renamed by a
    scoped run, so that older folder's references can never be broken by
    a run that never re-reads it. Confirmed necessary directly: an
    earlier version of this script scanned the WHOLE attachment tree
    regardless of which HTML files were in scope, and a file that
    happened to ALSO have a bad name (leftover from before this feature
    existed) but was only referenced by an out-of-scope older folder got
    renamed anyway -- silently breaking that older folder's attachment
    display, since nothing was rewriting its references.

    Only the LEAF FILENAME itself is ever renamed here, never an ancestor
    directory -- if an ancestor directory component also has a bad name,
    that path is skipped with a warning rather than fixed, since safely
    renaming a shared directory requires knowing everything else that
    lives in it (to avoid breaking an out-of-scope sibling), which a
    deliberately narrow, scoped run doesn't have visibility into. A full,
    unscoped sweep (no --html-dir) is what handles that case; this
    scoped path is only ever meant for the common case of flat,
    individually-named attachment files.

    If two different original names happen to sanitize to the identical
    result, both simply map to that same new name -- see
    apply_rename_plan()'s own docstring for why that's treated as the
    same file rather than disambiguated with a suffix.

    Returns ({attachment_root_path: {relative_dir_path: {old_filename: new_filename}}}, skipped).
    The inner shape matches build_rename_plan()'s own return per
    attachment root, so the same apply_rename_plan() and
    sanitized_relative_path() helpers work unchanged on either. skipped
    is a list of referenced paths that couldn't be safely handled in
    scoped mode (bad character in an ancestor directory).
    """
    plans_by_root = {}
    skipped_ancestor = []

    by_dir = {}
    for ref in sorted(referenced_paths):
        parts = Path(ref).parts  # e.g. ('Attachments', 'sub', 'pic:1.jpg')
        attachment_root = export_root / parts[0]
        rel_dir = Path(*parts[1:-1]) if len(parts) > 2 else Path('.')
        filename = parts[-1]
        by_dir.setdefault((attachment_root, rel_dir), []).append(filename)

    for (attachment_root, rel_dir), filenames in by_dir.items():
        # Bad character in an ANCESTOR directory component -> skip, warn.
        if any(any(ch in part for ch in CHAR_TO_DASHES) for part in rel_dir.parts):
            for fn in filenames:
                skipped_ancestor.append(str(Path(attachment_root.name) / rel_dir / fn))
            continue

        local_plan = {}
        for filename in sorted(filenames):
            new_name, changed = sanitize_component(filename)
            if changed:
                local_plan[filename] = new_name

        if local_plan:
            plans_by_root.setdefault(attachment_root, {})[rel_dir] = local_plan

    return plans_by_root, skipped_ancestor


def rewrite_html_references(export_root, attachment_root, plan, original_files, dry_run,
                             html_dirs=None, extra_replacements=None):
    """
    For every .html file under export_root (or, if html_dirs is given,
    only under those specific directories), replace any occurrence of an
    old attachment-relative path (as it would appear inside a src="..." or
    href="...") with its sanitized equivalent.

    Matched specifically as the VALUE of a src= or href= attribute (via
    regex, with the old path re.escape()'d so a regex-special character
    in a real filename -- a literal "." is the common case -- is treated
    literally, not as regex syntax), never as a bare substring search
    across the whole file. A message someone sent could, in principle,
    contain this exact path string as plain text (pasting a file path
    into a conversation, say), and a bare replace would corrupt that text
    even though it was never an actual attachment reference.

    Replacement pairs are computed per LEAF FILE (using
    sanitized_relative_path() to resolve every component of its path,
    including any renamed ANCESTOR directory), not built independently
    per directory level. That distinction matters: a file sitting inside
    a directory that also needs renaming has two things changing in its
    path at once, and computing its full new path in one step is what
    correctly handles that -- building the old/new pairs one path
    component at a time and hoping sequential substring replacements
    happen to combine correctly is fragile and isn't relied on here.

    original_files is every leaf file's path relative to attachment_root,
    captured BEFORE any renames happened (build_rename_plan doesn't
    rename anything itself, so a walk taken any time before
    apply_rename_plan runs still reflects original names).

    extra_replacements, when given, is a list of (old, new) path strings
    ALREADY fully resolved relative to export_root, added alongside
    whatever plan implies. This covers a reference to a name that was
    renamed in a PRIOR run rather than this one: imessage-exporter
    regenerates each run's HTML from chat.db's own recorded attachment
    path, which always points at the attachment's original location and
    has no idea a local rename ever happened here, so a message
    re-exported later (the sync pipeline's own one-day overlap window,
    or any other reason the same attachment gets referenced again) can
    legitimately reference an old name again even though nothing new
    needs physically renaming for it -- the sanitized file already
    exists from before, and the reference just needs pointing at it,
    not routed through rename-collision logic meant for genuinely new
    candidates.

    html_dirs, when given, restricts WHICH .html files get scanned and
    rewritten to only those under the listed directories. When html_dirs
    is None, every .html file under export_root is scanned -- the right
    default for a one-time, full-archive cleanup rather than an
    incremental per-run check.

    Returns (files_changed, replacements_made).
    """
    rel_attachment_root = attachment_root.relative_to(export_root)

    replacements = list(extra_replacements) if extra_replacements else []
    for rel_file in original_files:
        new_rel_file = sanitized_relative_path(rel_file, plan)
        if new_rel_file != rel_file:
            old_full = rel_attachment_root / rel_file
            new_full = rel_attachment_root / new_rel_file
            replacements.append((str(old_full), str(new_full)))

    if not replacements:
        return 0, 0

    # Longest-old-path-first: guards against a shorter old path being a
    # prefix/substring of a longer one and getting substituted first,
    # which would corrupt the longer path's own replacement.
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    if html_dirs is None:
        html_files = list(export_root.rglob('*.html'))
    else:
        html_files = []
        for d in html_dirs:
            html_files.extend(Path(d).resolve().rglob('*.html'))

    files_changed = 0
    total_replacements = 0
    for html_path in html_files:
        text = html_path.read_text(encoding='utf-8', errors='replace')
        original_text = text
        file_replacements = 0
        for old, new in replacements:
            # Matched specifically as the VALUE of a src= or href= attribute
            # (re.escape() on the old path so any regex-special character
            # in a real filename, like a literal ".", is treated literally,
            # not as regex syntax) -- deliberately not a bare substring
            # search-and-replace across the whole file. A message someone
            # sent could, in principle, contain this exact path string as
            # plain text (someone pasting a file path into a conversation,
            # say), and a bare replace would corrupt that text even though
            # it was never an actual attachment reference to begin with.
            pattern = re.compile(r'((?:src|href)=")' + re.escape(old) + r'(")')
            new_text, count = pattern.subn(r'\g<1>' + new.replace('\\', '\\\\') + r'\g<2>', text)
            if count:
                text = new_text
                file_replacements += count
        if text != original_text:
            files_changed += 1
            total_replacements += file_replacements
            print(f"{'[dry-run] ' if dry_run else ''}{html_path}: {file_replacements} reference(s) updated")
            if not dry_run:
                html_path.write_text(text, encoding='utf-8')

    return files_changed, total_replacements


def collect_original_files(attachment_root):
    """
    Every leaf FILE's path relative to attachment_root, captured before
    any renames happen. Directories aren't included -- only files are
    ever referenced from HTML src=/href=, so only files need an old->new
    path pair computed for the rewrite step.
    """
    files = []
    for dirpath, _, filenames in os.walk(attachment_root):
        rel_dir = Path(dirpath).relative_to(attachment_root)
        for fn in filenames:
            files.append(rel_dir / fn if str(rel_dir) != '.' else Path(fn))
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('export_root', help='Root directory containing exported .html files and Attachments/StickerCache folders')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change without renaming or writing anything')
    parser.add_argument('--html-dir', action='append', default=None, metavar='DIR',
                         help='Restrict the scan to only what these directories\' .html files actually '
                              'reference (repeatable) -- both which attachments are checked for bad names '
                              'AND which .html files get re-read and re-written. Meant for an incremental, '
                              'per-run check (e.g. just the current dated export folder) so the entire '
                              'archive is never re-scanned every time -- safe to check ONLY the current run, '
                              'since sanitizing happens before the next run\'s export ever happens, so a '
                              'later run\'s fresh HTML always already references the sanitized name for '
                              'anything renamed previously. An attachment with a bad ancestor directory name '
                              'is skipped with a warning in this mode -- safely fixing that needs the full '
                              'sweep below, since a scoped run cannot see everything else that shares that '
                              'directory. '
                              'Default (omitted): a full, one-time sweep of everything under export_root, '
                              'renaming any bad attachment name found anywhere and rewriting every .html file '
                              'that could possibly reference it. A ".attachment_rename_log.json" file is kept '
                              'at export_root\'s top level either way, recording every rename ever made -- '
                              'this is what lets a later run recognize and clean up a bad-named duplicate that '
                              'reappears after a fresh, additive sync from a live attachment source, since '
                              'that source is never itself modified by this script and has no way to know a '
                              'rename ever happened here.')
    args = parser.parse_args()

    export_root = Path(args.export_root).resolve()
    if not export_root.is_dir():
        print(f"ERROR: not a directory: {export_root}", file=sys.stderr)
        sys.exit(1)

    if args.html_dir:
        for d in args.html_dir:
            if not Path(d).is_dir():
                print(f"ERROR: --html-dir not a directory: {d}", file=sys.stderr)
                sys.exit(1)

    if args.html_dir:
        run_scoped(export_root, args.html_dir, args.dry_run)
    else:
        run_full_sweep(export_root, args.dry_run)


RENAME_LOG_FILENAME = '.attachment_rename_log.json'


def load_rename_log(export_root):
    """
    Load the persistent record of every rename this script has EVER
    performed under export_root, as {old_path_relative_to_export_root:
    new_path_relative_to_export_root}.

    This is what makes it possible to recognize and clean up a bad-named
    duplicate that reappears after a fresh sync from the live attachment
    source. The live source itself is never modified by this script --
    renaming a file there would mean touching your actual, live Messages
    data, which this deliberately never does -- so an additive,
    non-deleting sync from that source has no way to know a file was
    ever renamed on this side. It will keep seeing the old bad name as
    "missing" here and copy it back in from source, every single run,
    forever, unless something recognizes and cleans up that reappearance.

    Returns {} if no log exists yet (first run, or an export root that
    predates this feature).
    """
    log_path = export_root / RENAME_LOG_FILENAME
    if not log_path.is_file():
        return {}
    try:
        return json.loads(log_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def save_rename_log(export_root, log, dry_run):
    if dry_run:
        return
    log_path = export_root / RENAME_LOG_FILENAME
    log_path.write_text(json.dumps(log, indent=2, sort_keys=True), encoding='utf-8')


def heal_resynced_duplicates(export_root, log, dry_run):
    """
    For every rename ever recorded in the log, check whether a file has
    reappeared at the OLD (bad) path -- in practice, almost always
    because an additive sync from the live attachment source just ran
    again and copied the original, never having been told a rename ever
    happened here.

    If the sanitized copy at the NEW path still exists too, the
    reappeared old-named file is a redundant duplicate of content this
    script already has a clean copy of. iMessage attachments are
    immutable once received -- never edited in place after the fact --
    so it's safe to treat the reappeared file as identical content
    rather than re-verify it byte for byte, and simply delete it rather
    than rename it again, which would otherwise produce a needless
    "__2"-suffixed duplicate on every single subsequent run forever.

    If the new path DOESN'T exist (something else removed the sanitized
    copy), the reappeared file is left alone entirely here -- it gets
    picked up normally by the ordinary rename plan instead, rather than
    deleted with no sanitized copy to fall back on.

    Returns the number of duplicates healed.
    """
    healed = 0
    for old_rel, new_rel in log.items():
        old_path = export_root / old_rel
        new_path = export_root / new_rel
        if old_path.is_file() and new_path.is_file():
            print(f"{'[dry-run] ' if dry_run else ''}healing re-synced duplicate: "
                  f"{old_path} (already have {new_path})")
            if not dry_run:
                old_path.unlink()
            healed += 1
    return healed


def record_renames(export_root, attachment_root, plan, original_files, log):
    """
    Update log in place with every old->new path (relative to export_root)
    implied by plan, using the same sanitized_relative_path() resolution
    rewrite_html_references() relies on -- so a file's full new path is
    correctly recorded even when an ancestor directory was ALSO renamed,
    not just the file's own name.
    """
    rel_attachment_root = attachment_root.relative_to(export_root)
    for rel_file in original_files:
        new_rel_file = sanitized_relative_path(rel_file, plan)
        if new_rel_file != rel_file:
            old_full = str(rel_attachment_root / rel_file)
            new_full = str(rel_attachment_root / new_rel_file)
            log[old_full] = new_full


def run_full_sweep(export_root, dry_run):
    """
    Original, unscoped behavior: walk every Attachments/StickerCache
    directory under export_root in full, and rewrite every .html file
    under export_root that could reference anything found there. Correct
    for a one-time cleanup of an entire existing archive, since it never
    assumes anything about which HTML files might reference which
    attachments.
    """
    attachment_roots = find_attachment_roots(export_root)
    if not attachment_roots:
        print(f"No Attachments/StickerCache directories found under {export_root}.")
        return

    log = load_rename_log(export_root)
    heal_resynced_duplicates(export_root, log, dry_run)

    total_renamed = 0
    total_html_changed = 0
    total_refs_updated = 0

    for attachment_root in attachment_roots:
        print(f"\n=== {attachment_root.relative_to(export_root)} ===")
        plan = build_rename_plan(attachment_root)
        if not plan:
            print("  No problematic filenames found.")
            continue

        # Captured BEFORE renaming -- build_rename_plan() only computes a
        # plan, it doesn't touch the filesystem, so original names are
        # still in place at this point.
        original_files = collect_original_files(attachment_root)

        renamed = apply_rename_plan(attachment_root, plan, dry_run)
        total_renamed += renamed
        record_renames(export_root, attachment_root, plan, original_files, log)

        html_changed, refs_updated = rewrite_html_references(
            export_root, attachment_root, plan, original_files, dry_run
        )
        total_html_changed += html_changed
        total_refs_updated += refs_updated

    save_rename_log(export_root, log, dry_run)

    print(f"\n{'[dry-run] ' if dry_run else ''}Done: {total_renamed} file(s)/folder(s) renamed, "
          f"{total_refs_updated} reference(s) updated across {total_html_changed} HTML file(s).")


def run_scoped(export_root, html_dirs, dry_run):
    """
    Incremental mode: only ever look at what the given html_dirs actually
    reference. Never touches an attachment that isn't referenced by one
    of these specific directories' .html files, and never re-reads or
    rewrites any .html file outside of them -- see
    build_scoped_rename_plan()'s own docstring for why that pairing
    (attachment candidates always derived FROM the same html_dirs being
    rewritten) is what keeps this safe.
    """
    html_files = []
    for d in html_dirs:
        html_files.extend(Path(d).resolve().rglob('*.html'))

    if not html_files:
        print(f"No .html files found under: {', '.join(html_dirs)}")
        return

    log = load_rename_log(export_root)
    heal_resynced_duplicates(export_root, log, dry_run)

    referenced_paths = extract_referenced_paths(html_files)
    if not referenced_paths:
        print("No attachment references found in the given HTML files.")
        save_rename_log(export_root, log, dry_run)
        return

    # Split into paths this script has ALREADY renamed in some prior run
    # vs genuinely new candidates. This split matters: imessage-exporter
    # regenerates each run's HTML straight from chat.db's own recorded
    # attachment path, which always points at the original location and
    # has no idea a local rename ever happened -- so a reference to an
    # already-renamed name can legitimately reappear (the sync pipeline's
    # own overlap window, or any other reason the same attachment gets
    # referenced again) even though there's nothing left to physically
    # rename for it; the sanitized file already exists from before.
    # Feeding an already-logged path into build_scoped_rename_plan()
    # anyway would misread "old name doesn't exist, new name is already
    # taken" as a genuine collision needing a fresh "__2" suffix, when
    # really the reference just needs pointing at the file that's
    # already there. Confirmed directly: an earlier version did exactly
    # that on a second run, producing a spurious "__2"-suffixed name for
    # a file that only needed its HTML reference reattached, not a new
    # rename.
    already_logged = {ref: log[ref] for ref in referenced_paths if ref in log}
    new_candidates = referenced_paths - already_logged.keys()

    plans_by_root, skipped = build_scoped_rename_plan(export_root, new_candidates) if new_candidates else ({}, [])

    if skipped:
        print(f"WARNING: {len(skipped)} referenced attachment(s) skipped -- "
              f"a bad character is in an ANCESTOR DIRECTORY name, not the file itself, "
              f"which a scoped run can't safely fix (see --help). Run a full sweep "
              f"(no --html-dir) to handle these:")
        for s in skipped:
            print(f"    {s}")

    attachment_roots_touched = set(plans_by_root.keys())
    attachment_roots_touched.update(export_root / Path(p).parts[0] for p in already_logged)

    if not attachment_roots_touched:
        print("No problematic filenames found among the referenced attachments.")
        save_rename_log(export_root, log, dry_run)
        return

    total_renamed = 0
    total_html_changed = 0
    total_refs_updated = 0

    for attachment_root in attachment_roots_touched:
        plan = plans_by_root.get(attachment_root, {})
        print(f"\n=== {attachment_root.relative_to(export_root)} (scoped) ===")

        # original_files here is deliberately just the files THIS plan
        # covers, not a full attachment_root walk -- there's no need to
        # look at anything beyond what's actually being renamed.
        original_files = [
            (rel_dir / old_name) if str(rel_dir) != '.' else Path(old_name)
            for rel_dir, local_plan in plan.items()
            for old_name in local_plan
        ]

        if plan:
            renamed = apply_rename_plan(attachment_root, plan, dry_run)
            total_renamed += renamed
            record_renames(export_root, attachment_root, plan, original_files, log)

        this_root_extra = [
            (old, new) for old, new in already_logged.items()
            if (export_root / Path(old).parts[0]) == attachment_root
        ]

        html_changed, refs_updated = rewrite_html_references(
            export_root, attachment_root, plan, original_files, dry_run,
            html_dirs=html_dirs, extra_replacements=this_root_extra
        )
        total_html_changed += html_changed
        total_refs_updated += refs_updated

    save_rename_log(export_root, log, dry_run)

    print(f"\n{'[dry-run] ' if dry_run else ''}Done: {total_renamed} file(s) renamed, "
          f"{total_refs_updated} reference(s) updated across {total_html_changed} HTML file(s).")


if __name__ == '__main__':
    main()
