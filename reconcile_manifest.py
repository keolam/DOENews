import argparse
import json
import re
import uuid
from pathlib import Path

AUDIO_DIR = Path("static") / "mp3"
MANIFEST_PATH = Path("tracks.json")


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"tracks": []}


def save_manifest(m: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(m, indent=2), encoding="utf-8")


def norm(s: str) -> str:
    """Normalize strings for fuzzy matching."""
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def stem_norm(name: str) -> str:
    return norm(Path(name).stem)


def main(apply: bool, remove_orphans: bool) -> None:
    if not AUDIO_DIR.exists():
        raise SystemExit(f"Missing folder: {AUDIO_DIR}")

    disk_files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])
    disk_by_lower = {f.lower(): f for f in disk_files}
    disk_stems = {stem_norm(f): f for f in disk_files}  # first one wins if duplicates

    manifest = load_manifest()
    tracks = manifest.get("tracks", [])
    if not isinstance(tracks, list):
        raise SystemExit("tracks.json is malformed: 'tracks' must be a list")

    # Ensure every manifest track has an id
    changed = False
    for t in tracks:
        if not t.get("id"):
            t["id"] = uuid.uuid4().hex
            changed = True

    # Helper: find candidates by stem
    def candidates_for_stem(stem: str) -> list[str]:
        ns = norm(stem)
        if not ns:
            return []
        return [f for f in disk_files if stem_norm(f) == ns]

    used_disk = set()  # lower filenames used by manifest after reconciliation

    updated_links = 0
    orphans = []

    # Re-link missing manifest entries
    for t in tracks:
        fn = (t.get("filename") or "").strip()
        fn_lower = fn.lower()

        # If filename exists on disk, keep it
        if fn_lower in disk_by_lower:
            t["filename"] = disk_by_lower[fn_lower]  # normalize exact casing
            used_disk.add(t["filename"].lower())
            continue

        # Missing file → attempt to re-link
        title = (t.get("title") or "").strip()
        old_stem = Path(fn).stem if fn else ""
        title_stem = title

        cand = []
        cand += candidates_for_stem(title_stem)
        # also try old filename stem if present
        if old_stem and old_stem != title_stem:
            cand += candidates_for_stem(old_stem)

        # dedupe candidates
        cand = list(dict.fromkeys(cand))

        # If exactly one candidate, relink
        if len(cand) == 1:
            new_fn = cand[0]
            t["filename"] = new_fn
            used_disk.add(new_fn.lower())
            updated_links += 1
            changed = True
        else:
            orphans.append(t)

    # Add new files that are not referenced
    existing_filenames = {((t.get("filename") or "").lower()) for t in tracks if t.get("filename")}
    added = 0
    for f in disk_files:
        if f.lower() not in existing_filenames:
            tracks.append({
                "id": uuid.uuid4().hex,
                "title": Path(f).stem,
                "filename": f,
                "created_at": None
            })
            added += 1
            changed = True

    # Optionally remove orphans
    removed = 0
    if orphans and remove_orphans:
        orphan_ids = {t.get("id") for t in orphans}
        before = len(tracks)
        tracks = [t for t in tracks if t.get("id") not in orphan_ids]
        removed = before - len(tracks)
        manifest["tracks"] = tracks
        changed = True
    else:
        manifest["tracks"] = tracks

    # Report
    print("=== Reconcile report ===")
    print(f"MP3 files on disk:        {len(disk_files)}")
    print(f"Manifest tracks (start):  {len(load_manifest().get('tracks', []))}")
    print(f"Relinked missing tracks:  {updated_links}")
    print(f"Added new disk tracks:    {added}")
    print(f"Orphan manifest entries:  {len(orphans)}")
    if remove_orphans:
        print(f"Removed orphans:          {removed}")

    if orphans:
        print("\nOrphans (could not confidently match to a disk file):")
        for t in orphans[:25]:
            print(f"- id={t.get('id')} title={t.get('title')!r} filename={t.get('filename')!r}")
        if len(orphans) > 25:
            print(f"...and {len(orphans) - 25} more")

    if changed:
        if apply:
            # atomic-ish save
            tmp = MANIFEST_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            tmp.replace(MANIFEST_PATH)
            print("\n✅ Applied changes to tracks.json")
        else:
            print("\nℹ️ Dry run only. Re-run with --apply to write tracks.json")
    else:
        print("\n✅ No changes needed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes to tracks.json")
    ap.add_argument("--remove-orphans", action="store_true", help="Remove manifest entries whose files are missing")
    args = ap.parse_args()
    main(apply=args.apply, remove_orphans=args.remove_orphans)
