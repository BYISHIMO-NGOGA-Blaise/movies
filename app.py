import os
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    send_from_directory, abort, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
POSTER_DIR = os.path.join(BASE_DIR, "uploads", "posters")
DATABASE = os.path.join(BASE_DIR, "movieshub.db")

import cv2
import numpy as np

ADMIN_USERNAME = os.environ.get("MOVIESHUB_USERNAME", "owner")
ADMIN_PASSWORD = os.environ.get("MOVIESHUB_PASSWORD", "admin123")

app = Flask(__name__)
app.secret_key = os.environ.get("MOVIESHUB_SECRET", "movieshub-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(POSTER_DIR, exist_ok=True)


def get_db():
    import sqlite3
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL CHECK (category IN ('translated', 'nontranslated')),
            video_file TEXT NOT NULL,
            poster_file TEXT,
            views INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)),
        )
    conn.commit()
    conn.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_owner"):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def allowed_file(filename, extensions):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in extensions


def extract_poster_from_video(video_path, poster_path):
    """
    Extract first frame from video and save as poster
    Returns True if successful, False otherwise
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        
        ret, frame = cap.read()
        cap.release()
        
        if not ret or frame is None:
            return False
        
        # Resize frame to thumbnail size (maintains aspect ratio)
        height, width = frame.shape[:2]
        aspect_ratio = width / height
        new_height = 450
        new_width = int(new_height * aspect_ratio)
        resized_frame = cv2.resize(frame, (new_width, new_height))
        
        # Save as JPEG
        cv2.imwrite(poster_path, resized_frame)
        return True
    except Exception as e:
        print(f"Error extracting poster: {e}")
        return False


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.now().year,
        "is_owner": bool(session.get("is_owner")),
        "movie_count": movie_count(),
    }


def movie_count():
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) AS c FROM movies").fetchone()["c"]
        conn.close()
        return count
    except Exception:
        return 0


@app.route("/")
def index():
    conn = get_db()
    translated_count = conn.execute(
        "SELECT COUNT(*) AS c FROM movies WHERE category = 'translated'"
    ).fetchone()["c"]
    nontranslated_count = conn.execute(
        "SELECT COUNT(*) AS c FROM movies WHERE category = 'nontranslated'"
    ).fetchone()["c"]
    conn.close()
    return render_template(
        "choose.html",
        translated_count=translated_count,
        nontranslated_count=nontranslated_count,
    )


@app.route("/category/<category>")
def category_page(category):
    if category not in ("translated", "nontranslated"):
        abort(404)
    q = request.args.get("q", "").strip()
    conn = get_db()
    query = "SELECT * FROM movies WHERE category = ?"
    params = [category]
    if q:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY created_at DESC"
    movies = conn.execute(query, params).fetchall()
    translated_count = conn.execute(
        "SELECT COUNT(*) AS c FROM movies WHERE category = 'translated'"
    ).fetchone()["c"]
    nontranslated_count = conn.execute(
        "SELECT COUNT(*) AS c FROM movies WHERE category = 'nontranslated'"
    ).fetchone()["c"]
    conn.close()
    return render_template(
        "index.html",
        movies=movies,
        active_category=category,
        q=q,
        translated_count=translated_count,
        nontranslated_count=nontranslated_count,
    )


@app.route("/movie/<int:movie_id>")
def movie_detail(movie_id):
    conn = get_db()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if movie is None:
        conn.close()
        abort(404)
    conn.execute("UPDATE movies SET views = views + 1 WHERE id = ?", (movie_id,))
    conn.commit()
    related = conn.execute(
        "SELECT * FROM movies WHERE category = ? AND id != ? ORDER BY created_at DESC LIMIT 4",
        (movie["category"], movie_id),
    ).fetchall()
    conn.close()
    return render_template("movie.html", movie=movie, related=related)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if user is not None and check_password_hash(user["password_hash"], password):
            session["is_owner"] = True
            session["username"] = user["username"]
            flash("Murakaza neza!", "success")
            return redirect(url_for("index"))
        flash("Izina cyangwa ijambobanga ntabwo aribyo.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Wasohotse.", "info")
    return redirect(url_for("index"))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "")
        video = request.files.get("video")

        if not title or category not in ("translated", "nontranslated"):
            flash("Tanga umutwe n'icyiciro by'umwe.", "error")
            return redirect(url_for("upload"))
        if not video or not video.filename:
            flash("Video ntiboneye.", "error")
            return redirect(url_for("upload"))

        video_name = secure_filename(video.filename)
        video_path = os.path.join(UPLOAD_DIR, video_name)
        video.save(video_path)

        # Auto-generate poster from first frame of video
        poster_name = None
        poster_name_base = os.path.splitext(video_name)[0] + ".jpg"
        poster_path = os.path.join(POSTER_DIR, poster_name_base)
        
        if extract_poster_from_video(video_path, poster_path):
            poster_name = poster_name_base
        else:
            print(f"Warning: Could not extract poster from video {video_name}")

        conn = get_db()
        conn.execute(
            "INSERT INTO movies (title, description, category, video_file, poster_file, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, category, video_name, poster_name, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        flash("Filime yongeweho neza!", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")


@app.route("/delete/<int:movie_id>", methods=["POST"])
@login_required
def delete_movie(movie_id):
    conn = get_db()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if movie is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    conn.commit()
    conn.close()
    for path in (os.path.join(UPLOAD_DIR, movie["video_file"]),):
        if os.path.exists(path):
            os.remove(path)
    if movie["poster_file"]:
        poster_path = os.path.join(POSTER_DIR, movie["poster_file"])
        if os.path.exists(poster_path):
            os.remove(poster_path)
    flash("Filime yahanaguwe.", "info")
    return redirect(url_for("index"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/uploads/posters/<path:filename>")
def uploaded_poster(filename):
    return send_from_directory(POSTER_DIR, filename)


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Uruhushya nturimo. Owner gusa niwe ubashisha kongera filime."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Iyi filime ntaboneka."), 404


if __name__ == "__main__":
    init_db()
    print("MoviesHub is running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)
