from flask import Flask, render_template, send_from_directory
import os

app = Flask(__name__)

MP3_FOLDER = os.path.join(app.root_path, "static", "mp3")

@app.route("/")
def index():
    # List mp3 files in the folder
    files = [
        f for f in os.listdir(MP3_FOLDER)
        if f.lower().endswith(".mp3")
    ]
    return render_template("index.html", files=files)

@app.route("/mp3/<filename>")
def serve_mp3(filename):
    return send_from_directory(MP3_FOLDER, filename)
