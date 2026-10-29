FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 movieshub

COPY . .
RUN mkdir -p /data/uploads/posters && chown -R movieshub:movieshub /app /data

USER movieshub
EXPOSE 8000

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
