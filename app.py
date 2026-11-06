import os
import secrets
import shutil
import subprocess
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from mimetypes import guess_type

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    send_from_directory, abort, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("MOVIESHUB_UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
POSTER_DIR = os.environ.get(
    "MOVIESHUB_POSTER_DIR", os.path.join(UPLOAD_DIR, "posters")
)
DATABASE = os.environ.get("MOVIESHUB_DATABASE", os.path.join(BASE_DIR, "movieshub.db"))

try:
    import cv2
except ImportError:
    cv2 = None

ADMIN_USERNAME = os.environ.get("MOVIESHUB_USERNAME", "owner")
OWNER_LOGIN_PATH = "/" + os.environ.get(
    "MOVIESHUB_OWNER_LOGIN_PATH", "owner-entry-7f3a9c"
).strip("/")

app = Flask(__name__)
production = os.environ.get("FLASK_ENV", "").lower() == "production"
configured_secret = os.environ.get("MOVIESHUB_SECRET")
if production and (not configured_secret or len(configured_secret) < 32):
    raise RuntimeError("MOVIESHUB_SECRET must contain at least 32 characters in production")

ADMIN_PASSWORD = os.environ.get("MOVIESHUB_PASSWORD", "admin123")
if production and (not os.environ.get("MOVIESHUB_PASSWORD") or ADMIN_PASSWORD == "admin123"):
    raise RuntimeError("MOVIESHUB_PASSWORD must be set to a strong value in production")

app.config.update(
    SECRET_KEY=configured_secret or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.environ.get("MOVIESHUB_MAX_UPLOAD_MB", "500")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=production or os.environ.get("MOVIESHUB_COOKIE_SECURE", "0") == "1",
    PREFERRED_URL_SCHEME=os.environ.get("MOVIESHUB_URL_SCHEME", "http"),
)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mkv", "avi", "webm", "mov", "m4v", "ogv"}
ALLOWED_AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "ogg", "flac"}
POSTER_WORKERS = int(os.environ.get("MOVIESHUB_POSTER_WORKERS", "1"))
poster_executor = ThreadPoolExecutor(max_workers=POSTER_WORKERS, thread_name_prefix="poster")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(POSTER_DIR, exist_ok=True)
CATALOG_MANIFEST = os.path.join(os.path.dirname(DATABASE), "movieshub-catalog.json")


def save_catalog_manifest():
    conn = get_db()
    movies = conn.execute(
        "SELECT title, description, category, video_file, poster_file, views, created_at "
        "FROM movies ORDER BY id"
    ).fetchall()
    conn.close()
    catalog = [dict(movie) for movie in movies]
    temp_path = CATALOG_MANIFEST + ".tmp"
    os.makedirs(os.path.dirname(CATALOG_MANIFEST), exist_ok=True)
    with open(temp_path, "w", encoding="utf-8") as output:
        json.dump(catalog, output, ensure_ascii=False)
    os.replace(temp_path, CATALOG_MANIFEST)


def restore_catalog_manifest():
    if not os.path.isfile(CATALOG_MANIFEST):
        return
    try:
        with open(CATALOG_MANIFEST, "r", encoding="utf-8") as source:
            catalog = json.load(source)
    except (OSError, json.JSONDecodeError):
        return

    conn = get_db()
    existing = {
        row["video_file"] for row in conn.execute("SELECT video_file FROM movies")
    }
    for movie in catalog:
        video_file = movie.get("video_file")
        if movie.get("category") not in ("translated", "nontranslated"):
            continue
        video_path = os.path.join(UPLOAD_DIR, os.path.basename(video_file or ""))
        if not video_file or video_file in existing or not os.path.isfile(video_path):
            continue
        conn.execute(
            "INSERT INTO movies "
            "(title, description, category, video_file, poster_file, views, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                movie.get("title", ""),
                movie.get("description", ""),
                movie.get("category", ""),
                video_file,
                movie.get("poster_file"),
                movie.get("views", 0),
                movie.get("created_at", datetime.now().isoformat()),
            ),
        )
        existing.add(video_file)
    conn.commit()
    conn.close()


def get_db():
    import sqlite3
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
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
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            audio_file TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_movies_category_created
            ON movies(category, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_movies_category_title
            ON movies(category, title);
        """
    )
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
    password_hash = generate_password_hash(ADMIN_PASSWORD)
    if existing is None:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (ADMIN_USERNAME, password_hash),
        )
    else:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, existing["id"]),
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


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf():
    submitted = request.form.get("csrf_token", "")
    stored = session.get("csrf_token", "")
    if not stored or not submitted or not secrets.compare_digest(submitted, stored):
        abort(400, description="Invalid or missing CSRF token")


def unique_upload_name(filename):
    clean_name = secure_filename(filename)
    stem, extension = os.path.splitext(clean_name)
    return f"{uuid.uuid4().hex}-{stem}{extension.lower()}"


def extract_poster_from_video(video_path, poster_path):
    """
    Extract first frame from video and save as poster
    Returns True if successful, False otherwise
    """
    os.makedirs(os.path.dirname(poster_path), exist_ok=True)

    if cv2 is not None:
        try:
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                frame_count = max(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 1)
                positions = (0, max(frame_count // 100, 1), frame_count // 10)
                for position in positions:
                    if position:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                    ret, frame = cap.read()
                    if ret and frame is not None and frame.size:
                        height, width = frame.shape[:2]
                        new_height = 450
                        new_width = max(int(new_height * width / height), 1)
                        resized_frame = cv2.resize(frame, (new_width, new_height))
                        if cv2.imwrite(
                            poster_path,
                            resized_frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 95],
                        ):
                            cap.release()
                            return os.path.isfile(poster_path)
                cap.release()
        except Exception as e:
            print(f"Error extracting poster with OpenCV: {e}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        temp_poster = poster_path + ".tmp.jpg"
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y", "-ss", "1", "-i", video_path,
                    "-frames:v", "1", "-q:v", "2", temp_poster,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if result.returncode == 0 and os.path.isfile(temp_poster):
                os.replace(temp_poster, poster_path)
                return True
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"Error extracting poster with ffmpeg: {e}")
        finally:
            if os.path.exists(temp_poster):
                os.remove(temp_poster)

    return False


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.now().year,
        "is_owner": bool(session.get("is_owner")),
        "movie_count": movie_count(),
        "csrf_token": get_csrf_token(),
    }


def generate_poster_async(video_path, poster_name, poster_path, movie_id):
    if not extract_poster_from_video(video_path, poster_path):
        return
    conn = get_db()
    conn.execute("UPDATE movies SET poster_file = ? WHERE id = ?", (poster_name, movie_id))
    conn.commit()
    conn.close()


def movie_count():
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) AS c FROM movies").fetchone()["c"]
        conn.close()
        return count
    except Exception:
        return 0


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    elif request.path.startswith("/uploads/posters/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    if production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.route("/healthz")
def health():
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {
            "status": "ok",
            "storage_mounted": os.path.ismount("/data") if production else True,
            "database_persistent": os.path.dirname(DATABASE) == "/data",
            "uploads_persistent": UPLOAD_DIR.startswith("/data/"),
        }, 200
    except Exception:
        return {"status": "unhealthy"}, 503


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
    conn.execute("UPDATE movies SET views = views + 1 WHERE id = ?", (movie_id,))
    conn.commit()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if movie is None:
        conn.close()
        abort(404)
    related = conn.execute(
        "SELECT * FROM movies WHERE category = ? AND id != ? ORDER BY created_at DESC LIMIT 4",
        (movie["category"], movie_id),
    ).fetchall()
    conn.close()
    return render_template("movie.html", movie=movie, related=related)


@app.route(OWNER_LOGIN_PATH, methods=["GET", "POST"])
def login():
    if request.method == "POST":
        validate_csrf()
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
            flash("Welcome back!", "success")
            return redirect(url_for("index"))
        flash("The username or password is incorrect.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    validate_csrf()
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("index"))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        validate_csrf()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "")
        video = request.files.get("video")

        if not title or category not in ("translated", "nontranslated"):
            flash("Enter a title and select a category.", "error")
            return redirect(url_for("upload"))
        if not video or not video.filename:
            flash("No video file was selected.", "error")
            return redirect(url_for("upload"))

        if not allowed_file(video.filename, ALLOWED_VIDEO_EXTENSIONS):
            flash("Choose a supported video file.", "error")
            return redirect(url_for("upload"))

        video_name = unique_upload_name(video.filename)
        video_path = os.path.join(UPLOAD_DIR, video_name)
        temp_path = f"{video_path}.uploading"
        with open(temp_path, "wb") as output:
            chunk = video.stream.read(1024 * 1024)
            while chunk:
                output.write(chunk)
                chunk = video.stream.read(1024 * 1024)
        os.replace(temp_path, video_path)

        poster_name_base = os.path.splitext(video_name)[0] + ".jpg"
        poster_path = os.path.join(POSTER_DIR, poster_name_base)
        poster_file = None
        if extract_poster_from_video(video_path, poster_path):
            poster_file = poster_name_base

        conn = get_db()
        conn.execute(
            "INSERT INTO movies (title, description, category, video_file, poster_file, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, category, video_name, poster_file, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        save_catalog_manifest()

        flash("The movie was uploaded successfully.", "success")
        return redirect(url_for("category_page", category=category))

    return render_template("upload.html")


@app.route("/songs")
def songs():
    q = request.args.get("q", "").strip()
    conn = get_db()
    query = "SELECT * FROM songs"
    params = []
    if q:
        query += " WHERE title LIKE ? OR artist LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    query += " ORDER BY created_at DESC"
    tracks = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("songs.html", tracks=tracks, q=q)


@app.route("/songs/upload", methods=["GET", "POST"])
@login_required
def upload_song():
    if request.method == "POST":
        validate_csrf()
        title = request.form.get("title", "").strip()
        artist = request.form.get("artist", "").strip()
        description = request.form.get("description", "").strip()
        audio = request.files.get("audio")
        if not title or not audio or not audio.filename:
            flash("Enter a song title and choose an audio file.", "error")
            return redirect(url_for("upload_song"))
        if not allowed_file(audio.filename, ALLOWED_AUDIO_EXTENSIONS):
            flash("Choose a supported audio file.", "error")
            return redirect(url_for("upload_song"))

        audio_name = unique_upload_name(audio.filename)
        audio_path = os.path.join(UPLOAD_DIR, audio_name)
        temp_path = f"{audio_path}.uploading"
        with open(temp_path, "wb") as output:
            shutil.copyfileobj(audio.stream, output, length=1024 * 1024)
        os.replace(temp_path, audio_path)

        conn = get_db()
        conn.execute(
            "INSERT INTO songs (title, artist, description, audio_file, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, artist, description, audio_name, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        flash("The song was uploaded successfully.", "success")
        return redirect(url_for("songs"))

    return render_template("upload_song.html")


@app.route("/delete/<int:movie_id>", methods=["POST"])
@login_required
def delete_movie(movie_id):
    validate_csrf()
    conn = get_db()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if movie is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    conn.commit()
    conn.close()
    save_catalog_manifest()
    for path in (os.path.join(UPLOAD_DIR, movie["video_file"]),):
        if os.path.exists(path):
            os.remove(path)
    if movie["poster_file"]:
        poster_path = os.path.join(POSTER_DIR, movie["poster_file"])
        if os.path.exists(poster_path):
            os.remove(poster_path)
    flash("The movie was deleted.", "info")
    return redirect(url_for("index"))


@app.route("/songs/delete/<int:song_id>", methods=["POST"])
@login_required
def delete_song(song_id):
    validate_csrf()
    conn = get_db()
    song = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
    if song is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
    conn.commit()
    conn.close()
    audio_path = os.path.join(UPLOAD_DIR, song["audio_file"])
    if os.path.isfile(audio_path):
        os.remove(audio_path)
    flash("The song was deleted.", "info")
    return redirect(url_for("songs"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    mimetype = guess_type(filename)[0]
    if filename.lower().endswith(".mkv"):
        mimetype = "video/x-matroska"
    response = send_from_directory(UPLOAD_DIR, filename, mimetype=mimetype, conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "public, max-age=86400, immutable, no-transform"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/uploads/posters/<path:filename>")
def uploaded_poster(filename):
    response = send_from_directory(POSTER_DIR, filename, conditional=True)
    response.headers["Cache-Control"] = "public, max-age=86400, immutable, no-transform"
    return response


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Access denied. Only the owner can add movies."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="This movie was not found."), 404


@app.errorhandler(413)
def upload_too_large(e):
    return render_template(
        "error.html",
        code=413,
        message="The video file exceeds the upload limit.",
    ), 413


@app.errorhandler(400)
def bad_request(e):
    return render_template(
        "error.html",
        code=400,
        message=e.description or "Icyifuzo ntabwo cyemewe.",
    ), 400


init_db()
restore_catalog_manifest()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("MoviesHub is running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=debug)
