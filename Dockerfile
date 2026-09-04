# syntax=docker/dockerfile:1

# The speech engine is not built here: we take the upstream binary from its published
# image. The runtime base must stay debian:trixie-slim — that is what upstream builds
# against, and the binary's onnxruntime statics need trixie's libstdc++.
# Версия движка. Такое же значение стоит в `docker-compose.yml`, и меняются они одним
# коммитом: здесь — запас для прямого `docker build`, там — то, что берёт compose.
ARG GIGASTT_TAG=2.20.0
FROM ghcr.io/ekhodzitsky/gigastt:${GIGASTT_TAG} AS engine

FROM debian:trixie-slim

# No ffmpeg, no torch, no CUDA: the engine decodes every format it accepts itself,
# browser WebM/Opus included since 2.17.0, so the image stays small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=engine /usr/local/bin/gigastt /usr/local/bin/gigastt

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python3 -m venv "$VIRTUAL_ENV"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

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
     "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
