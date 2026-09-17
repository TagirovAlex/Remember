FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/remember.db

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir . && \
    useradd --create-home --comment "Remember app user" remember && \
    mkdir -p /data && \
    chown -R remember:remember /app /data

VOLUME /data

USER remember

EXPOSE 8000

CMD ["python", "-m", "app"]