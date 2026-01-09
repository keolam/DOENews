import json, uuid
from pathlib import Path

MANIFEST_PATH = Path("tracks.json")

manifest = {"tracks": []}
if MANIFEST_PATH.exists():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

changed = False
seen = set()

for t in manifest.get("tracks", []):
    if not t.get("id"):
        t["id"] = uuid.uuid4().hex
        changed = True
    if t["id"] in seen:
        t["id"] = uuid.uuid4().hex
        changed = True
    seen.add(t["id"])

if changed:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Updated tracks.json with UUIDs.")
else:
    print("No changes needed.")
