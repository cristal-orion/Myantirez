FROM python:3.12-slim

# ffmpeg divide l'audio; yt-dlp usa deno per risolvere le sfide JavaScript di YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir "yt-dlp[default]"

RUN useradd --system --create-home --uid 10001 antirez \
    && mkdir -p /app/data && chown antirez:antirez /app/data
WORKDIR /app
COPY antirez ./antirez
COPY static ./static

# Archivio SQLite e .env scritto dalle Impostazioni vivono entrambi nel volume /app/data.
ENV HOST=0.0.0.0 \
    PORT=8765 \
    ANTIREZ_DATA_DIR=/app/data \
    ANTIREZ_ENV_FILE=/app/data/.env \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER antirez
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"]
CMD ["python", "-m", "antirez", "serve"]
