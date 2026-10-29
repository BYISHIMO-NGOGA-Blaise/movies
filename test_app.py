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


def csrf_token(client, path="/login"):
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data[:500]
    return match.group(1).decode()


def test_public_pages_and_database(client):
    assert client.get("/").status_code == 200
    assert client.get("/category/translated?q=test").status_code == 200
    assert client.get("/category/nontranslated").status_code == 200
    assert client.get("/login").status_code == 200
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"

    connection = sqlite3.connect(app_module.DATABASE)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert {"users", "movies"}.issubset(tables)


def test_owner_login_logout_csrf(client):
    assert client.post("/login", data={"username": "owner", "password": "admin123"}).status_code == 400

    token = csrf_token(client)
    response = client.post("/login", data={
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
    client.post("/login", data={
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
