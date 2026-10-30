FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/main.py backend/playlist.py backend/playlist_api.py backend/playout.py ./
COPY index.html ./index.html
COPY frontend/dj.html ./frontend/dj.html
# La playlist viaja dentro de la imagen: el backend desplegado no depende de volumenes locales.
COPY music /music
ENV AUTODJ_ENABLED=true \
    AUTODJ_PLAYLIST=/music/playlist.txt \
    MUSIC_DIR=/music \
    PLAYOUT_ENGINE=continuous \
    NEWS_DIR=/data/news
RUN mkdir -p /data/news
VOLUME /data/news

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
