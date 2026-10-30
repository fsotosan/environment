# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# A dedicated non-root user. I2C access is granted at runtime with --group-add.
RUN groupadd --system --gid 10001 envmon \
    && useradd --system --uid 10001 --gid envmon --home-dir /app --shell /usr/sbin/nologin envmon

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

# Keep the commented reference config in the image; Compose mounts the real one.
COPY config/config.example.yaml /app/config/config.example.yaml

RUN mkdir -p /data /backups \
    && chown -R envmon:envmon /app /data /backups

USER envmon

ENV ENVMON_CONFIG=/app/config/config.yaml \
    ENVMON_DATABASE__URL=sqlite:////data/envmon.db \
    ENVMON_RETENTION__BACKUP_DIR=/backups \
    ENVMON_WEB__HOST=0.0.0.0 \
    ENVMON_WEB__PORT=8080

EXPOSE 8080
VOLUME ["/data", "/backups"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/healthz', timeout=3)"]

ENTRYPOINT ["envmon"]
CMD ["run"]
