FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

# Workers must stay at 1: the scheduler and sync runner are per-process, so a
# second worker means a second scheduler claiming and running the same syncs.
# Threads are the safe dial, and 2 was too few — the sync pipeline shares this
# process, so a couple of slow provider calls could leave no thread to answer
# the dashboard and the proxy returned 502.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8080 --workers ${SYNCMETA_GUNICORN_WORKERS:-1} --threads ${SYNCMETA_GUNICORN_THREADS:-6} --timeout ${SYNCMETA_GUNICORN_TIMEOUT:-120} web:app"]
