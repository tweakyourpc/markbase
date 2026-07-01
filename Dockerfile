FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ingest.py queue_worker.py ./
COPY static/ ./static/

ENV MARKBASE_LIBRARY_PATH=/data/library
ENV MARKBASE_STATE_PATH=/data/state
ENV MARKBASE_HOST=0.0.0.0
ENV MARKBASE_PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host $MARKBASE_HOST --port $MARKBASE_PORT"]
