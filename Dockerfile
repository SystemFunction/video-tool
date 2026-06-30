# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    FLET_FORCE_WEB_VIEW=1 \
    FLET_SERVER_PORT=8550

# System-Pakete:
#   ffmpeg  -> Konvertierung (wird vom Tool via shutil.which() aus PATH genutzt)
#   ca-certificates -> HTTPS fuer yt-dlp / requests
#   tini    -> sauberes Signal-Handling fuer den Flet-Server
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Python-Sources rein (Originalskript bleibt unangetastet)
COPY video_tool.py entrypoint.py ./

# Persistente Pfade:
#   /downloads          -> Zielordner fuer heruntergeladene/konvertierte Videos
#   /root/.video_tool_v3 -> Config / Logs / ggf. lokal aktualisiertes yt-dlp
RUN mkdir -p /downloads /root/.video_tool_v3
VOLUME ["/downloads", "/root/.video_tool_v3"]

EXPOSE 8550

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "entrypoint.py"]
