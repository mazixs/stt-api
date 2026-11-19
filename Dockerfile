# syntax=docker/dockerfile:1

# Keep the direct-build default in sync with docker-compose.yml.
ARG GIGASTT_TAG=2.22.0
FROM ghcr.io/ekhodzitsky/gigastt:${GIGASTT_TAG} AS engine

# Keep the official Caddy binary; refresh Alpine security packages.
FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0 AS caddy
RUN apk upgrade --no-cache

# The engine needs trixie's libstdc++; security fixes can postdate the base image.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS runtime-base
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends python3 ca-certificates curl procps \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Keep pip and build-time wheels out of the runtime image.
FROM runtime-base AS dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv \
    && python3 -m venv "$VIRTUAL_ENV"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip uninstall -y pip

FROM runtime-base AS app
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=engine /usr/local/bin/gigastt /usr/local/bin/gigastt

WORKDIR /app
COPY console/ /app/console/

ENV MODEL_DIR=/models \
    DATA_DIR=/data \
    ENGINE_BIN=/usr/local/bin/gigastt \
    CONSOLE_PORT=8080
RUN mkdir -p /models /data
VOLUME ["/models", "/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "uvicorn", "console.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--no-access-log", \
     "--timeout-graceful-shutdown", "15"]
