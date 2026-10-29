# MoviesHub

MoviesHub is a Flask movie catalog with owner uploads, category browsing, search, SQLite storage, video streaming, and automatic poster extraction.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

The local development owner is `owner`; its password is `admin123`. Set real credentials before exposing the application:

```powershell
$env:MOVIESHUB_USERNAME="owner"
$env:MOVIESHUB_PASSWORD="replace-with-a-strong-password"
$env:MOVIESHUB_SECRET="replace-with-a-long-random-secret"
python app.py
```

Set `FLASK_DEBUG=1` only for local debugging. The database and upload limit can also be configured:

```text
MOVIESHUB_DATABASE=E:\data\movieshub.db
MOVIESHUB_MAX_UPLOAD_MB=500
MOVIESHUB_COOKIE_SECURE=1
```

## Production deployment

The repository includes a production Docker stack: Gunicorn serves Flask, nginx handles buffering and media streaming, and Docker volume storage persists SQLite and uploads.

1. Copy `.env.example` to `.env` and replace every placeholder value.
2. Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. Start the stack with `docker compose up -d --build`.
4. Put nginx behind HTTPS/TLS and a public load balancer or CDN. Do not expose the container port directly.
5. Verify the service with `docker compose ps` and `http://your-host/healthz`.

SQLite WAL and bounded database busy waits are enabled, but SQLite remains single-writer storage. A large public deployment should move the catalog to PostgreSQL, store media in object storage/CDN, and run a dedicated email-monitoring/backup process. See `gunicorn.conf.py` for worker tuning and `nginx.conf` for upload and streaming limits.

## Operations

- Back up `/data/movieshub.db` plus `/data/uploads` together.
- Set `MOVIESHUB_MAX_UPLOAD_MB` consistently in the app and nginx configuration before deployment.
- Restrict the owner password and secret to a secrets manager; rotate secrets when owner access changes.
- Keep the host reverse proxy patched and terminate TLS externally.
- Monitor `/healthz`, application logs, disk usage, database size, failed uploads, and HTTP 4xx/5xx rates.

## Tests

```powershell
python -m pytest -q
```

The automated tests cover public pages, database initialization, CSRF protection, login/logout, and upload validation.
