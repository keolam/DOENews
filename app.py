from dotenv import load_dotenv
load_dotenv()
import os
import json
import time
import uuid
import re
from pathlib import Path

import requests
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
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"tracks": []}

def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

def slugify_filename(name: str) -> str:
    """
    Turn user input into a safe base filename (no extension).
    Allows letters, numbers, underscore, dash. Converts spaces to underscores.
    """
    name = (name or "").strip()
    name = name.replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9_\-]", "", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("._-")
    return name

def mp3_name_exists(base_name: str) -> bool:
    """Check both the filesystem and manifest for an existing MP3 name."""
    filename = f"{base_name}.mp3"
    if (AUDIO_DIR / filename).exists():
        return True
    manifest = load_manifest()
    for t in manifest.get("tracks", []):
        if (t.get("filename") or "").lower() == filename.lower():
            return True
    return False

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
        "Accept": "application/json",
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
"""
@app.route("/")
def index():
    # Prefer manifest so we can show nice titles; fall back to directory scan
    manifest = load_manifest()
    tracks = manifest.get("tracks", [])

    # If empty manifest, build list from mp3 folder
    if not tracks:
        files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])
        tracks = [{"title": f, "filename": f, "created_at": None} for f in files]

    return render_template("index.html", tracks=tracks)
"""
"""
@app.route("/")
def index():
    manifest = load_manifest()
    tracks = manifest.get("tracks", [])

    # Keep only tracks that actually exist
    tracks = [
        t for t in tracks
        if t.get("filename") and (AUDIO_DIR / t["filename"]).exists()
    ]

    # If none, scan folder
    if not tracks:
        files = sorted([p.name for p in AUDIO_DIR.glob("*.mp3")])
        tracks = [{"title": f, "filename": f, "created_at": None} for f in files]

    return render_template("index.html", tracks=tracks)


"""


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

    title = (request.form.get("title") or "").strip() or Path(f.filename).stem
    safe_pdf_name = secure_filename(f.filename)
    pdf_path = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_pdf_name}"
    f.save(pdf_path)

    # Admin-provided mp3 base name
    raw_mp3_name = request.form.get("mp3_name", "")
    base_mp3_name = slugify_filename(raw_mp3_name)

    if not base_mp3_name:
        abort(400, "Please provide a valid MP3 filename (letters/numbers/_/- only).")

    if mp3_name_exists(base_mp3_name):
        abort(409, f"An MP3 named '{base_mp3_name}.mp3' already exists. Choose a different name.")

    text = extract_pdf_text(pdf_path)
    if not text:
        abort(400, "Could not extract text from PDF")

    # ---- APPLY USER LIMIT BEFORE CHUNKING ----
    max_chars_form = request.form.get("max_chars", "").strip()
    try:
        max_chars_user = int(max_chars_form) if max_chars_form else 4000
    except ValueError:
        max_chars_user = 4000

    text = text[:max_chars_user]

    # Choose chunk size based on model limits (see docs) :contentReference[oaicite:5]{index=5}
    # Safe default: 9000 chars if using multilingual v2; if you use Flash/Turbo you can raise this.
    max_chars = 9000 if "multilingual" in ELEVENLABS_MODEL_ID else 35000
    chunks = chunk_text(text, max_chars=max_chars)

    # Generate one MP3 per PDF (concatenate chunks by generating multiple mp3s if you want later)
    # For MVP: generate from first chunk only if you want fast; here we generate all chunks into one file by sequentially appending.
    out_name = f"{base_mp3_name}.mp3"
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
    manifest["tracks"].insert(0, {
        "title": title,
        "filename": out_name,
        "created_at": int(time.time())
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


@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)