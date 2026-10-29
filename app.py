import os
import secrets
import uuid
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

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(POSTER_DIR, exist_ok=True)


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
    if cv2 is None:
        return False

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
        "csrf_token": get_csrf_token(),
    }


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
    if production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.route("/healthz")
def health():
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok"}, 200
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


@app.route("/login", methods=["GET", "POST"])
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
            flash("Murakaza neza!", "success")
            return redirect(url_for("index"))
        flash("Izina cyangwa ijambobanga ntabwo aribyo.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    validate_csrf()
    session.clear()
    flash("Wasohotse.", "info")
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
            flash("Tanga umutwe n'icyiciro by'umwe.", "error")
            return redirect(url_for("upload"))
        if not video or not video.filename:
            flash("Video ntiboneye.", "error")
            return redirect(url_for("upload"))

        if not allowed_file(video.filename, ALLOWED_VIDEO_EXTENSIONS):
            flash("Hitamo dosiye yemewe ya video.", "error")
            return redirect(url_for("upload"))

        video_name = unique_upload_name(video.filename)
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
    validate_csrf()
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
    mimetype = guess_type(filename)[0]
    if filename.lower().endswith(".mkv"):
        mimetype = "video/x-matroska"
    return send_from_directory(UPLOAD_DIR, filename, mimetype=mimetype, conditional=True)


@app.route("/uploads/posters/<path:filename>")
def uploaded_poster(filename):
    return send_from_directory(POSTER_DIR, filename)


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Uruhushya nturimo. Owner gusa niwe ubashisha kongera filime."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Iyi filime ntaboneka."), 404


@app.errorhandler(413)
def upload_too_large(e):
    return render_template(
        "error.html",
        code=413,
        message="Dosiye ya video irarenze urugero rwemewe.",
    ), 413


@app.errorhandler(400)
def bad_request(e):
    return render_template(
        "error.html",
        code=400,
        message=e.description or "Icyifuzo ntabwo cyemewe.",
    ), 400


init_db()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("MoviesHub is running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=debug)
