import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", str(max(2, multiprocessing.cpu_count() * 2 + 1))))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
worker_class = "gthread"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
max_requests = 1000
max_requests_jitter = 100
