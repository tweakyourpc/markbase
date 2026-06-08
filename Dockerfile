FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MARKBASE_LIBRARY_PATH=/data/library \
    MARKBASE_STATE_PATH=/data/state \
    MARKBASE_HOST=0.0.0.0 \
    MARKBASE_PORT=8733

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir yt-dlp "markitdown[all]"

COPY . .

EXPOSE 8733

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8733"]
