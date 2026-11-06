import io
import os
import re
import sqlite3
import tempfile

import pytest

os.environ.setdefault("MOVIESHUB_USERNAME", "owner")
os.environ.setdefault("MOVIESHUB_PASSWORD", "admin123")
os.environ.setdefault("MOVIESHUB_SECRET", "test-only-secret")

import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "test.db"
    monkeypatch.setattr(app_module, "DATABASE", str(database))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "POSTER_DIR", str(tmp_path / "uploads" / "posters"))
    os.makedirs(app_module.UPLOAD_DIR, exist_ok=True)
    os.makedirs(app_module.POSTER_DIR, exist_ok=True)
    app_module.init_db()
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def csrf_token(client, path=None):
    if path is None:
        path = app_module.OWNER_LOGIN_PATH
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data[:500]
    return match.group(1).decode()


def test_public_pages_and_database(client):
    assert client.get("/").status_code == 200
    assert client.get("/category/translated?q=test").status_code == 200
    assert client.get("/category/nontranslated").status_code == 200
    assert client.get(app_module.OWNER_LOGIN_PATH).status_code == 200
    assert client.get("/login").status_code == 404
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"

    connection = sqlite3.connect(app_module.DATABASE)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert {"users", "movies"}.issubset(tables)


def test_owner_login_logout_csrf(client):
    assert client.post(app_module.OWNER_LOGIN_PATH, data={"username": "owner", "password": "admin123"}).status_code == 400

    token = csrf_token(client)
    response = client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner", "password": "admin123", "csrf_token": token
    })
    assert response.status_code == 302
    assert client.get("/upload").status_code == 200

    token = csrf_token(client, "/upload")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302
    assert client.get("/upload").status_code == 403


def test_upload_validation_and_success(client):
    token = csrf_token(client)
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner", "password": "admin123", "csrf_token": token
    })
    token = csrf_token(client, "/upload")

    response = client.post("/upload", data={
        "csrf_token": token,
        "title": "Test Movie",
        "description": "A test description",
        "category": "translated",
        "video": (io.BytesIO(b"not a real video"), "movie.txt"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    response = client.post("/upload", data={
        "csrf_token": csrf_token(client, "/upload"),
        "title": "Test Movie",
        "description": "A test description",
        "category": "translated",
        "video": (io.BytesIO(b"not a real mp4 video"), "movie.mp4"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    connection = sqlite3.connect(app_module.DATABASE)
    movie = connection.execute("SELECT * FROM movies").fetchone()
    connection.close()
    assert movie is not None


def test_uploaded_video_is_served_without_transforming_or_changing_bytes(client):
    token = csrf_token(client)
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner", "password": "admin123", "csrf_token": token
    })
    video_bytes = bytes(range(256)) * 1024

    response = client.post("/upload", data={
        "csrf_token": csrf_token(client, "/upload"),
        "title": "High Quality Movie",
        "description": "",
        "category": "translated",
        "video": (io.BytesIO(video_bytes), "movie.mp4"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    connection = sqlite3.connect(app_module.DATABASE)
    video_file = connection.execute("SELECT video_file FROM movies").fetchone()[0]
    connection.close()

    with open(os.path.join(app_module.UPLOAD_DIR, video_file), "rb") as video:
        assert video.read() == video_bytes

    response = client.get(f"/uploads/{video_file}")
    assert response.status_code == 200
    assert response.data == video_bytes
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "no-transform" in response.headers["Cache-Control"]


def test_upload_page_contains_progress_ui(client):
    token = csrf_token(client)
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner", "password": "admin123", "csrf_token": token
    })

    response = client.get("/upload")
    assert response.status_code == 200
    assert b'id="upload-progress"' in response.data
    assert b'id="upload-progress-bar"' in response.data


def test_upload_preserves_movies_in_other_category(client):
    connection = sqlite3.connect(app_module.DATABASE)
    connection.execute(
        "INSERT INTO movies (title, description, category, video_file, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Existing Original", "", "nontranslated", "original.mp4", "2026-01-01T00:00:00"),
    )
    connection.commit()
    connection.close()

    token = csrf_token(client)
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner", "password": "admin123", "csrf_token": token
    })

    response = client.post("/upload", data={
        "csrf_token": csrf_token(client, "/upload"),
        "title": "New Translation",
        "description": "",
        "category": "translated",
        "video": (io.BytesIO(b"not a real mp4 video"), "translated.mp4"),
    }, content_type="multipart/form-data")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/category/translated")

    connection = sqlite3.connect(app_module.DATABASE)
    rows = connection.execute(
        "SELECT title, category FROM movies ORDER BY id"
    ).fetchall()
    connection.close()

    assert rows == [
        ("Existing Original", "nontranslated"),
        ("New Translation", "translated"),
    ]
