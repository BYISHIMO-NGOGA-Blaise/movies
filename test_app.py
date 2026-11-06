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
    assert health.get_json()["status"] == "ok"
    assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"

    connection = sqlite3.connect(app_module.DATABASE)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert {"users", "movies", "songs", "live_matches"}.issubset(tables)
    assert client.get("/songs").status_code == 200
    assert client.get("/live").status_code == 404


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


def test_catalog_manifest_recovers_movies_after_database_reset(client):
    with open(os.path.join(app_module.UPLOAD_DIR, "persisted.mp4"), "wb") as video:
        video.write(b"persisted video")

    connection = sqlite3.connect(app_module.DATABASE)
    connection.execute(
        "INSERT INTO movies (title, description, category, video_file, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Persisted Movie", "", "nontranslated", "persisted.mp4", "2026-01-01T00:00:00"),
    )
    connection.commit()
    connection.close()
    app_module.save_catalog_manifest()

    os.remove(app_module.DATABASE)
    app_module.init_db()
    app_module.restore_catalog_manifest()

    connection = sqlite3.connect(app_module.DATABASE)
    row = connection.execute(
        "SELECT title, category FROM movies WHERE video_file = 'persisted.mp4'"
    ).fetchone()
    connection.close()
    assert row == ("Persisted Movie", "nontranslated")


def test_catalog_manifest_recovers_songs_and_live_matches_after_database_reset(client):
    with open(os.path.join(app_module.UPLOAD_DIR, "persisted.mp3"), "wb") as audio:
        audio.write(b"persisted audio")

    connection = sqlite3.connect(app_module.DATABASE)
    connection.execute(
        "INSERT INTO songs (title, artist, description, audio_file, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Persisted Song", "Artist", "", "persisted.mp3", "2026-01-01T00:00:00"),
    )
    connection.execute(
        "INSERT INTO live_matches "
        "(title, description, stream_url, embed_url, scheduled_at, is_live, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Persisted Match", "", "https://example.com/live.m3u8", "", "2026-11-07T20:00", 1, "2026-01-01T00:00:00"),
    )
    connection.commit()
    connection.close()
    app_module.save_catalog_manifest()

    os.remove(app_module.DATABASE)
    app_module.init_db()
    app_module.restore_catalog_manifest()

    connection = sqlite3.connect(app_module.DATABASE)
    song = connection.execute(
        "SELECT title, audio_file FROM songs WHERE audio_file = 'persisted.mp3'"
    ).fetchone()
    match = connection.execute(
        "SELECT title, is_live FROM live_matches WHERE title = 'Persisted Match'"
    ).fetchone()
    connection.close()
    assert song == ("Persisted Song", "persisted.mp3")
    assert match == ("Persisted Match", 1)


def test_owner_can_upload_and_play_song(client):
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner",
        "password": "admin123",
        "csrf_token": csrf_token(client),
    })
    response = client.post("/songs/upload", data={
        "csrf_token": csrf_token(client, "/songs/upload"),
        "title": "Test Song",
        "artist": "Test Artist",
        "description": "A test track",
        "audio": (io.BytesIO(b"not a real audio file"), "song.mp3"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    connection = sqlite3.connect(app_module.DATABASE)
    song = connection.execute(
        "SELECT title, artist, audio_file FROM songs"
    ).fetchone()
    connection.close()
    assert song[:2] == ("Test Song", "Test Artist")

    page = client.get("/songs")
    assert page.status_code == 200
    assert b"Test Song" in page.data
    assert client.get(f"/uploads/{song[2]}").status_code == 200


def test_owner_can_add_live_match(client, monkeypatch):
    monkeypatch.setattr(app_module, "LIVE_MATCHES_ENABLED", True)
    client.post(app_module.OWNER_LOGIN_PATH, data={
        "username": "owner",
        "password": "admin123",
        "csrf_token": csrf_token(client),
    })
    response = client.post("/live/manage", data={
        "csrf_token": csrf_token(client, "/live/manage"),
        "title": "Final Match",
        "description": "Championship final",
        "stream_url": "https://example.com/live.m3u8",
        "embed_url": "",
        "scheduled_at": "2026-11-07T20:00",
        "is_live": "1",
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    connection = sqlite3.connect(app_module.DATABASE)
    match = connection.execute(
        "SELECT title, stream_url, is_live FROM live_matches"
    ).fetchone()
    connection.close()
    assert match[0] == "Final Match"
    assert match[1] == "https://example.com/live.m3u8"
    assert match[2] == 1
    assert b"Final Match" in client.get("/live").data
