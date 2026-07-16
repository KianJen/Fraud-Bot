FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/mentions.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Run as a non-root user. /data is created here, owned by that user, so a
# fresh named volume mounted over it inherits the correct ownership.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

VOLUME ["/data"]

CMD ["python", "bot.py"]
