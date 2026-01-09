from dotenv import load_dotenv
load_dotenv()
import os
import json
import time
import uuid
import re
import requests

from datetime import datetime
from pathlib import Path
from pypdf import PdfReader
from flask import (
    Flask, render_template, send_from_directory,
    request, redirect, url_for, abort, jsonify
)
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Folders (note: App Platform filesystem is ephemeral) :contentReference[oaicite:3]{index=3}
BASE_DIR = Path(app.root_path)
UPLOAD_DIR = BASE_DIR / "uploads"
AUDIO_DIR = BASE_DIR / "static" / "mp3"
MANIFEST_PATH = BASE_DIR / "tracks.json"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID_BRIAN", "")
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

MAX_PDF_BYTES = 15 * 1024 * 1024  # keep MVP safe
CHUNK_SIZE = 1024

def require_admin():
    # Simple token auth: /admin?token=... OR header X-Admin-Token
    token = request.args.get("token") or request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(403)

def load_manifest():
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    else:
        manifest = {"tracks": []}

    changed = False
    tracks = manifest.get("tracks", [])
    for t in tracks:
        if not t.get("id"):
            t["id"] = uuid.uuid4().hex
            changed = True

    # Optional: ensure unique ids (rare edge case)
    seen = set()
    for t in tracks:
        if t["id"] in seen:
            t["id"] = uuid.uuid4().hex
            changed = True
        seen.add(t["id"])

    if changed:
        save_manifest(manifest)

    return manifest

def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

def format_us_date(date_str: str) -> str:
    """
    Converts 'YYYY-MM-DD' -> 'December 18, 2025'
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Invalid date format. Use YYYY-MM-DD.")
    # Remove leading zero from day (Windows-friendly)
    return dt.strftime("%B %d, %Y").replace(" 0", " ")

def is_safe_date_filename(name: str) -> bool:
    """
    Allow letters, spaces, comma, and digits only (for 'December 18, 2025').
    """
    return bool(re.fullmatch(r"[A-Za-z]+ \d{1,2}, \d{4}", name))

def mp3_filename_exists(filename: str) -> bool:
    """
    Check both filesystem and manifest for an existing filename.
    """
    if (AUDIO_DIR / filename).exists():
        return True
    manifest = load_manifest()
    for t in manifest.get("tracks", []):
        if (t.get("filename") or "").lower() == filename.lower():
            return True
    return False

def resolve_date_filename_collision(base_label: str) -> str:
    """
    Given 'December 18, 2025', returns a unique filename like:
      - 'December 18, 2025.mp3'
      - 'December 18, 2025 (2).mp3'
      - 'December 18, 2025 (3).mp3'
    """
    def exists(filename: str) -> bool:
        if (AUDIO_DIR / filename).exists():
            return True
        manifest = load_manifest()
        for t in manifest.get("tracks", []):
            if (t.get("filename") or "").lower() == filename.lower():
                return True
        return False

    # First attempt: no suffix
    filename = f"{base_label}.mp3"
    if not exists(filename):
        return filename

    # Subsequent attempts
    n = 2
    while True:
        filename = f"{base_label} ({n}).mp3"
        if not exists(filename):
            return filename
        n += 1

def prepare_narration_text(text: str) -> str:
    # Normalize whitespace (extra safety)
    text = re.sub(r"\s+", " ", text).strip()

    # Add pauses after headings (lines that look like titles)
    text = re.sub(
        r"([A-Z][A-Z \-]{6,})",
        r"\1.\n\n",
        text
    )

    # Improve list readability
    text = re.sub(r"\s-\s", ". ", text)
    text = re.sub(r"\s•\s", ". ", text)

    # Expand common acronyms (optional, opinionated)
    text = re.sub(r"\bU\.S\.\b", "United States", text)
    text = re.sub(r"\bDOE\b", "Department of Energy", text)

    # Insert soft pauses after long sentences
    text = re.sub(r"([.!?])\s+", r"\1\n", text)

    # Avoid extremely long sentences
    text = re.sub(r"(.{180,}?)([,;:])\s", r"\1.\n", text)

    return text.strip()


def extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        t = page.extract_text() or ""
        parts.append(t)
    # Basic cleanup
    text = "\n".join(parts).strip()
    return " ".join(text.split())


def chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # try to split on sentence boundary for nicer audio
        split = text.rfind(". ", start, end)
        if split != -1 and split > start + 500:
            end = split + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]

def elevenlabs_tts_to_file(text: str, out_path: Path):
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        raise RuntimeError("Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID")

    # Streaming endpoint from ElevenLabs SDK docs :contentReference[oaicite:4]{index=4}
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
    }

    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.8,
            "style": 0.0,
            "use_speaker_boost": True
        }
    }

    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"ElevenLabs error: {resp.status_code} {resp.text}")

    with out_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                f.write(chunk)


@app.route("/")
def index():
    # Reuse tracks.json merge behavior without an HTTP call
    manifest = load_manifest()
    manifest_tracks = manifest.get("tracks", [])
    disk_files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])

    by_filename = {}
    for t in manifest_tracks:
        fn = (t.get("filename") or "").strip()
        if fn and (AUDIO_DIR / fn).exists():
            by_filename[fn.lower()] = t

    tracks = list(by_filename.values())
    for fn in disk_files:
        if fn.lower() not in by_filename:
            tracks.append({"title": Path(fn).stem, "filename": fn, "created_at": None})

    return render_template("index.html", tracks=tracks)



@app.route("/tracks.json")
def tracks_json():
    manifest = load_manifest()
    manifest_tracks = manifest.get("tracks", [])

    # Index mp3 files currently on disk
    disk_files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])

    # Build a dict of manifest entries that actually exist on disk
    by_filename = {}
    for t in manifest_tracks:
        fn = (t.get("filename") or "").strip()
        if fn and (AUDIO_DIR / fn).exists():
            by_filename[fn.lower()] = t

    # Start with manifest (existing only)
    merged = list(by_filename.values())

    # Add any disk mp3s missing from manifest
    for fn in disk_files:
        if fn.lower() not in by_filename:
            merged.append({
                "title": Path(fn).stem,   # default title
                "filename": fn,
                "created_at": None
            })

    return jsonify({"tracks": merged})


@app.route("/mp3/<path:filename>")
def serve_mp3(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    require_admin()

    if request.method == "GET":
        return render_template("admin.html")

    # POST: upload PDF
    if "pdf" not in request.files:
        abort(400, "Missing file field 'pdf'")

    f = request.files["pdf"]
    if not f.filename:
        abort(400, "No selected file")

    # Basic size guard
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_PDF_BYTES:
        abort(413, "PDF too large for this MVP")
    

    # Admin-provided mp3 base name
    # Admin-provided date -> "December 18, 2025"
    raw_date = (request.form.get("mp3_date") or "").strip()
    try:
        date_label = format_us_date(raw_date)
    except ValueError as e:
        abort(400, str(e))

    if not is_safe_date_filename(date_label):
        abort(400, "Date formatting error. Expected something like 'December 18, 2025'.")

    title = (request.form.get("title") or "").strip() or date_label

    safe_pdf_name = secure_filename(f.filename)
    pdf_path = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_pdf_name}"
    f.save(pdf_path)
   
    out_name = resolve_date_filename_collision(date_label)

    try:
        text = prepare_narration_text(extract_pdf_text(pdf_path))

        if not text:
            abort(400, "Could not extract text from PDF")


        # Choose chunk size based on model limits (see docs) :contentReference[oaicite:5]{index=5}
        # Safe default: 9000 chars if using multilingual v2; if you use Flash/Turbo you can raise this.
        max_chars = 9000 if "multilingual" in ELEVENLABS_MODEL_ID else 35000
        chunks = chunk_text(text, max_chars=max_chars)

        # Generate one MP3 per PDF (concatenate chunks by generating multiple mp3s if you want later)
        # For MVP: generate from first chunk only if you want fast; here we generate all chunks into one file by sequentially appending.
        out_path = AUDIO_DIR / out_name

        # Write sequentially to same file (append each chunk audio)
        # NOTE: MP3 concatenation by raw append generally works for many players but isn’t perfect.
        # For “perfect” concatenation, you’d use ffmpeg (not always available on App Platform).
        with out_path.open("wb") as out:
            for i, chunk in enumerate(chunks):
                tmp_path = AUDIO_DIR / f".tmp_{uuid.uuid4().hex}.mp3"
                elevenlabs_tts_to_file(chunk, tmp_path)
                out.write(tmp_path.read_bytes())
                tmp_path.unlink(missing_ok=True)

        # Update manifest
        manifest = load_manifest()
        manifest.setdefault("tracks", [])
        track_id = uuid.uuid4().hex

        manifest["tracks"].insert(0, {
            "id": track_id,                 # <- UUID metadata
            "title": title,                 # e.g. "December 18, 2025"
            "filename": out_name,           # e.g. "December 18, 2025.mp3"
            "created_at": int(time.time()),
            "source_date": raw_date         # optional: keep original YYYY-MM-DD
        })

        save_manifest(manifest)

        # If called via fetch/AJAX, return JSON so the admin page can update live
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({
                "ok": True,
                "created": {"title": title, "filename": out_name},
                "tracks": load_manifest().get("tracks", [])
            })
        
        # Fallback: normal form post
        return redirect(url_for("admin", token=request.args.get("token", "")))
    finally:
        pdf_path.unlink(missing_ok=True)


@app.route("/admin/charcount", methods=["POST"])
def admin_charcount():
    require_admin()

    if "pdf" not in request.files:
        abort(400, "Missing file field 'pdf'")

    f = request.files["pdf"]
    if not f.filename:
        abort(400, "No selected file")

    # Optional: same size guard
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_PDF_BYTES:
        abort(413, "PDF too large for this MVP")

    safe_pdf_name = secure_filename(f.filename)
    pdf_path = UPLOAD_DIR / f".charcount_{uuid.uuid4().hex}_{safe_pdf_name}"
    f.save(pdf_path)

    try:
        text = prepare_narration_text(extract_pdf_text(pdf_path))
        if not text:
            abort(400, "Could not extract text from PDF")
        return jsonify({"ok": True, "char_count": len(text)})
    finally:
        pdf_path.unlink(missing_ok=True)

@app.route("/admin/tracks", methods=["GET"])
def admin_tracks():
    require_admin()

    manifest = load_manifest()
    manifest_tracks = manifest.get("tracks", [])
    disk_files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])

    by_filename = {}
    for t in manifest_tracks:
        fn = (t.get("filename") or "").strip()
        if fn and (AUDIO_DIR / fn).exists():
            by_filename[fn.lower()] = t

    tracks = list(by_filename.values())
    for fn in disk_files:
        if fn.lower() not in by_filename:
            tracks.append({
                "id": uuid.uuid4().hex,
                "title": Path(fn).stem,
                "filename": fn,
                "created_at": None
            })

    return jsonify(tracks)


@app.route("/admin/track/<track_id>", methods=["PATCH"])
def admin_update_track(track_id):
    require_admin()
    data = request.get_json(silent=True) or {}
    new_title = (data.get("title") or "").strip()

    if not new_title:
        abort(400, "Title cannot be empty")

    manifest = load_manifest()
    tracks = manifest.get("tracks", [])
    for t in tracks:
        if t.get("id") == track_id:
            t["title"] = new_title
            save_manifest(manifest)
            return jsonify({"ok": True, "track": t})

    abort(404, "Track not found")


@app.route("/admin/track/<track_id>", methods=["DELETE"])
def admin_delete_track(track_id):
    require_admin()

    manifest = load_manifest()
    tracks = manifest.get("tracks", [])

    idx = next((i for i, t in enumerate(tracks) if t.get("id") == track_id), None)
    if idx is None:
        abort(404, "Track not found")

    track = tracks.pop(idx)

    # Delete the mp3 file (ignore if missing)
    filename = track.get("filename")
    if filename:
        try:
            (AUDIO_DIR / filename).unlink(missing_ok=True)
        except Exception:
            pass

    save_manifest(manifest)
    return jsonify({"ok": True})

@app.route("/admin/reorder", methods=["POST"])
def admin_reorder():
    require_admin()
    data = request.get_json(silent=True) or {}
    order = data.get("order") or []

    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        abort(400, "order must be a list of track ids")

    manifest = load_manifest()
    tracks = manifest.get("tracks", [])

    by_id = {t.get("id"): t for t in tracks if t.get("id")}
    if set(order) != set(by_id.keys()):
        abort(400, "order must include all track ids exactly once")

    manifest["tracks"] = [by_id[tid] for tid in order]
    save_manifest(manifest)
    return jsonify({"ok": True})



@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
