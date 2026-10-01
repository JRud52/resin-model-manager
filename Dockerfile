FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SOURCE_DIR=/source LIBRARY_DIR=/library DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Default user baked into the image; docker-compose.yml overrides it at run
# time with `user: PUID:PGID` so files belong to your NAS user.
ARG PUID=1000
ARG PGID=1000
RUN groupadd -g ${PGID} rmm && useradd -u ${PUID} -g ${PGID} -M rmm \
    && mkdir -p /library /data && chown rmm:rmm /library /data
USER rmm

# Short git commit shown next to the version in the UI (set by the CI build).
ARG GIT_COMMIT=""
ENV GIT_COMMIT=${GIT_COMMIT}

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
