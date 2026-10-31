# SnapIT Render backend
# Python runs FastAPI + yt-dlp (Instagram, TikTok, X, Pinterest, Snapchat); social.py reads picture posts and acts as a backup reader. ffmpeg joins video and audio.
# YouTube no longer goes through yt-dlp (it uses RapidAPI), so Deno is not needed any more.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt

COPY main.py social.py ./

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
