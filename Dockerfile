FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/logs /app/backups \
 && useradd -r -u 10001 -m -d /app appuser \
 && chown -R appuser:appuser /app

USER appuser

# Порты не открываются: бот использует Telegram long polling.
# Файлы data/logs/backups монтируются томами (см. docker-compose.yml).
CMD ["python", "main.py"]
